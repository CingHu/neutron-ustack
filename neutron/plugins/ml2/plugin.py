# Copyright (c) 2013 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import contextlib
from eventlet import greenthread

from oslo.config import cfg
from oslo.db import exception as os_db_exception
from sqlalchemy import exc as sql_exc
from sqlalchemy.orm import exc as sa_exc

from neutron.agent import securitygroups_rpc as sg_rpc
from neutron.api.rpc.agentnotifiers import dhcp_rpc_agent_api
from neutron.api.rpc.handlers import dhcp_rpc
from neutron.api.rpc.handlers import dvr_rpc
from neutron.api.rpc.handlers import securitygroups_rpc
from neutron.api.v2 import attributes
from neutron.common import constants as const
from neutron.common import exceptions as exc
from neutron.common import rpc as n_rpc
from neutron.common import topics
from neutron.common import uos_utils
from neutron.common import utils
from neutron import context as n_context
from neutron.db import agents_db
from neutron.db import tenants_db
from neutron.db import agentschedulers_db
from neutron.db import allowedaddresspairs_db as addr_pair_db
from neutron.db import api as db_api
from neutron.db import db_base_plugin_v2
from neutron.db import dvr_mac_db
from neutron.db import external_net_db
from neutron.db import extradhcpopt_db
from neutron.db import models_v2
from neutron.db import quota_db  # noqa
from neutron.db import securitygroups_rpc_base as sg_db_rpc
from neutron.db import uos_db  # noqa
from neutron.db import uos_net_ratelimit_db
from neutron.db import uos_service_provider_db
from neutron.extensions import allowedaddresspairs as addr_pair
from neutron.extensions import extra_dhcp_opt as edo_ext
from neutron.extensions import portbindings
from neutron.extensions import providernet as provider
from neutron.extensions import securitygroup as ext_sg
from neutron import manager
from neutron.openstack.common import excutils
from neutron.openstack.common import importutils
from neutron.openstack.common import jsonutils
from neutron.openstack.common import lockutils
from neutron.openstack.common import log
from neutron.openstack.common import uuidutils
from neutron.plugins.common import constants as service_constants
from neutron.plugins.ml2.common import exceptions as ml2_exc
from neutron.plugins.ml2 import config  # noqa
from neutron.plugins.ml2 import db
from neutron.plugins.ml2 import driver_api as api
from neutron.plugins.ml2 import driver_context
from neutron.plugins.ml2 import managers
from neutron.plugins.ml2 import models
from neutron.plugins.ml2 import rpc

LOG = log.getLogger(__name__)

MAX_BIND_TRIES = 10

# REVISIT(rkukura): Move this and other network_type constants to
# providernet.py?
TYPE_MULTI_SEGMENT = 'multi-segment'

TAP_DEVICE_PREFIX = 'tap'
TAP_DEVICE_PREFIX_LENGTH = 3


class Ml2Plugin(db_base_plugin_v2.NeutronDbPluginV2,
                dvr_mac_db.DVRDbMixin,
                external_net_db.External_net_db_mixin,
                sg_db_rpc.SecurityGroupServerRpcMixin,
                agentschedulers_db.DhcpAgentSchedulerDbMixin,
                addr_pair_db.AllowedAddressPairsMixin,
                extradhcpopt_db.ExtraDhcpOptMixin,
                uos_net_ratelimit_db.Uos_net_ratelimit_db_mixin,
                uos_service_provider_db.Uos_subnet_service_provider_db_mixin,
                tenants_db.TenantDbMixin,
                ):

    """Implement the Neutron L2 abstractions using modules.

    Ml2Plugin is a Neutron plugin based on separately extensible sets
    of network types and mechanisms for connecting to networks of
    those types. The network types and mechanisms are implemented as
    drivers loaded via Python entry points. Networks can be made up of
    multiple segments (not yet fully implemented).
    """

    # This attribute specifies whether the plugin supports or not
    # bulk/pagination/sorting operations. Name mangling is used in
    # order to ensure it is qualified by class
    __native_bulk_support = True
    __native_pagination_support = True
    __native_sorting_support = True

    # List of supported extensions
    _supported_extension_aliases = ["provider", "external-net", "binding",
                                    "quotas", "security-group", "agent",
                                    "dhcp_agent_scheduler",
                                    "multi-provider", "allowed-address-pairs",
                                    "extra_dhcp_opt", "uos","tenant",
                                    "uos-net-ratelimit","uos-service-provider"]

    @property
    def supported_extension_aliases(self):
        if not hasattr(self, '_aliases'):
            aliases = self._supported_extension_aliases[:]
            aliases += self.extension_manager.extension_aliases()
            sg_rpc.disable_security_group_extension_by_config(aliases)
            self._aliases = aliases
        return self._aliases

    def __init__(self):
        # First load drivers, then initialize DB, then initialize drivers
        self.type_manager = managers.TypeManager()
        self.extension_manager = managers.ExtensionManager()
        self.mechanism_manager = managers.MechanismManager()
        super(Ml2Plugin, self).__init__()
        super(Ml2Plugin, self).__init_sg_db_mixin__()
        self.type_manager.initialize()
        self.extension_manager.initialize()
        self.mechanism_manager.initialize()
        # bulk support depends on the underlying drivers
        self.__native_bulk_support = self.mechanism_manager.native_bulk_support

        self._setup_rpc()

        # REVISIT(rkukura): Use stevedore for these?
        self.network_scheduler = importutils.import_object(
            cfg.CONF.network_scheduler_driver
        )

        LOG.info(_("Modular L2 Plugin initialization complete"))

    def _setup_rpc(self):
        self.notifier = rpc.AgentNotifierApi(topics.AGENT)
        self.agent_notifiers[const.AGENT_TYPE_DHCP] = (
            dhcp_rpc_agent_api.DhcpAgentNotifyAPI()
        )

    def start_rpc_listeners(self):
        self.endpoints = [rpc.RpcCallbacks(self.notifier, self.type_manager),
                          securitygroups_rpc.SecurityGroupServerRpcCallback(),
                          dvr_rpc.DVRServerRpcCallback(),
                          dhcp_rpc.DhcpRpcCallback(),
                          agents_db.AgentExtRpcCallback()]
        self.topic = topics.PLUGIN
        self.conn = n_rpc.create_connection(new=True)
        self.conn.create_consumer(self.topic, self.endpoints,
                                  fanout=False)
        return self.conn.consume_in_threads()

    def _filter_nets_provider(self, context, nets, filters):
        # TODO(rkukura): Implement filtering.
        return nets

    def _process_port_binding(self, mech_context, context, attrs):
        binding = mech_context._binding
        port = mech_context.current
        changes = False

        host = attrs and attrs.get(portbindings.HOST_ID)
        if (attributes.is_attr_set(host) and
            binding.host != host):
            binding.host = host
            changes = True
            # Whenever a DVR serviceable port comes up on a
            # node, it has to be communicated to the L3 Plugin
            # and agent for creating the respective namespaces.
            if (utils.is_dvr_serviced(port['device_owner'])):
                l3plugin = manager.NeutronManager.get_service_plugins().get(
                    service_constants.L3_ROUTER_NAT)
                if (utils.is_extension_supported(
                    l3plugin, const.L3_DISTRIBUTED_EXT_ALIAS)):
                    l3plugin.dvr_update_router_addvm(context, port)

        vnic_type = attrs and attrs.get(portbindings.VNIC_TYPE)
        if (attributes.is_attr_set(vnic_type) and
            binding.vnic_type != vnic_type):
            binding.vnic_type = vnic_type
            changes = True

        # treat None as clear of profile.
        profile = None
        if attrs and portbindings.PROFILE in attrs:
            profile = attrs.get(portbindings.PROFILE) or {}

        if profile not in (None, attributes.ATTR_NOT_SPECIFIED,
                           self._get_profile(binding)):
            binding.profile = jsonutils.dumps(profile)
            if len(binding.profile) > models.BINDING_PROFILE_LEN:
                msg = _("binding:profile value too large")
                raise exc.InvalidInput(error_message=msg)
            changes = True

        # Unbind the port if needed.
        if changes:
            binding.vif_type = portbindings.VIF_TYPE_UNBOUND
            binding.vif_details = ''
            binding.driver = None
            binding.segment = None

        if port['device_owner'] == const.DEVICE_OWNER_DVR_INTERFACE:
            binding.vif_type = portbindings.VIF_TYPE_DISTRIBUTED
            binding.vif_details = ''
            binding.driver = None
            binding.segment = None
            binding.host = ''

        self._update_port_dict_binding(port, binding)
        return changes

    def _bind_port_if_needed(self, context, allow_notify=False,
                             need_notify=False):
        plugin_context = context._plugin_context
        port_id = context._port['id']

        # Since the mechanism driver bind_port() calls must be made
        # outside a DB transaction locking the port state, it is
        # possible (but unlikely) that the port's state could change
        # concurrently while these calls are being made. If another
        # thread or process succeeds in binding the port before this
        # thread commits its results, the already committed results are
        # used. If attributes such as binding:host_id,
        # binding:profile, or binding:vnic_type are updated
        # concurrently, this loop retries binding using the new
        # values.
        count = 0
        while True:
            # First, determine whether it is necessary and possible to
            # bind the port.
            binding = context._binding
            if (binding.vif_type != portbindings.VIF_TYPE_UNBOUND
                or not binding.host):
                # We either don't need to bind the port, or can't, so
                # notify if needed and return.
                if allow_notify and need_notify:
                    self._notify_port_updated(context)
                return context

            # Limit binding attempts to avoid any possibility of
            # infinite looping and to ensure an error is logged
            # instead. This does not need to be tunable because no
            # more than a couple attempts should ever be required in
            # normal operation. Log at info level if not 1st attempt.
            count += 1
            if count > MAX_BIND_TRIES:
                LOG.error(_("Failed to commit binding results for %(port)s "
                            "after %(max)s tries"),
                          {'port': port_id, 'max': MAX_BIND_TRIES})
                return context
            if count > 1:
                greenthread.sleep(0)  # yield
                LOG.info(_("Attempt %(count)s to bind port %(port)s"),
                         {'count': count, 'port': port_id})

            # The port isn't already bound and the necessary
            # information is available, so attempt to bind the port.
            bind_context = self._bind_port(context)

            # Now try to commit result of attempting to bind the port.
            new_context, did_commit = self._commit_port_binding(
                plugin_context, port_id, binding, bind_context)
            if not new_context:
                # The port has been deleted concurrently, so just
                # return the unbound result from the initial
                # transaction that completed before the deletion.
                LOG.debug("Port %s has been deleted concurrently",
                          port_id)
                return context
            # Need to notify if we succeed and our results were
            # committed.
            if did_commit and (new_context._binding.vif_type !=
                               portbindings.VIF_TYPE_BINDING_FAILED):
                need_notify = True
            context = new_context

    def _bind_port(self, orig_context):
        # Construct a new PortContext from the one from the previous
        # transaction.
        port = orig_context._port
        orig_binding = orig_context._binding
        new_binding = models.PortBinding(
            host=orig_binding.host,
            vnic_type=orig_binding.vnic_type,
            profile=orig_binding.profile,
            vif_type=portbindings.VIF_TYPE_UNBOUND,
            vif_details=''
        )
        self._update_port_dict_binding(port, new_binding)
        new_context = driver_context.PortContext(
            self, orig_context._plugin_context, port,
            orig_context._network_context._network, new_binding)

        # Attempt to bind the port and return the context with the
        # result.
        self.mechanism_manager.bind_port(new_context)
        return new_context

    def _commit_port_binding(self, plugin_context, port_id, orig_binding,
                             new_context):
        session = plugin_context.session
        new_binding = new_context._binding

        # After we've attempted to bind the port, we begin a
        # transaction, get the current port state, and decide whether
        # to commit the binding results.
        #
        # REVISIT: Serialize this operation with a semaphore to
        # prevent deadlock waiting to acquire a DB lock held by
        # another thread in the same process, leading to 'lock wait
        # timeout' errors.
        with contextlib.nested(lockutils.lock('db-access'),
                               session.begin(subtransactions=True)):
            # Get the current port state and build a new PortContext
            # reflecting this state as original state for subsequent
            # mechanism driver update_port_*commit() calls.
            port_db, cur_binding = db.get_locked_port_and_binding(session,
                                                                  port_id)
            if not port_db:
                # The port has been deleted concurrently.
                return (None, None)
            oport = self._make_port_dict(port_db)
            port = self._make_port_dict(port_db)
            network = self.get_network(plugin_context, port['network_id'])
            cur_context = driver_context.PortContext(
                self, plugin_context, port, network, cur_binding,
                original_port=oport)

            # Commit our binding results only if port has not been
            # successfully bound concurrently by another thread or
            # process and no binding inputs have been changed.
            commit = ((cur_binding.vif_type in
                       [portbindings.VIF_TYPE_UNBOUND,
                        portbindings.VIF_TYPE_BINDING_FAILED]) and
                      orig_binding.host == cur_binding.host and
                      orig_binding.vnic_type == cur_binding.vnic_type and
                      orig_binding.profile == cur_binding.profile)

            if commit:
                # Update the port's binding state with our binding
                # results.
                cur_binding.vif_type = new_binding.vif_type
                cur_binding.vif_details = new_binding.vif_details
                cur_binding.driver = new_binding.driver
                cur_binding.segment = new_binding.segment

                # REVISIT(rkukura): The binding:profile attribute is
                # supposed to be input-only, but the Mellanox driver
                # currently modifies it while binding. Remove this
                # code when the Mellanox driver has been updated to
                # use binding:vif_details instead.
                if cur_binding.profile != new_binding.profile:
                    cur_binding.profile = new_binding.profile

                # Update PortContext's port dictionary to reflect the
                # updated binding state.
                self._update_port_dict_binding(port, cur_binding)

                # Update the port status if requested by the bound driver.
                if new_binding.segment and new_context._new_port_status:
                    port_db.status = new_context._new_port_status
                    port['status'] = new_context._new_port_status

                # Call the mechanism driver precommit methods, commit
                # the results, and call the postcommit methods.
                self.mechanism_manager.update_port_precommit(cur_context)
        if commit:
            self.mechanism_manager.update_port_postcommit(cur_context)

        # Continue, using the port state as of the transaction that
        # just finished, whether that transaction committed new
        # results or discovered concurrent port state changes.
        return (cur_context, commit)

    def _update_port_dict_binding(self, port, binding):
        port[portbindings.HOST_ID] = binding.host
        port[portbindings.VNIC_TYPE] = binding.vnic_type
        port[portbindings.PROFILE] = self._get_profile(binding)
        port[portbindings.VIF_TYPE] = binding.vif_type
        port[portbindings.VIF_DETAILS] = self._get_vif_details(binding)
        port[portbindings.DISABLE_ANTI_SPOOFING] = port['disable_anti_spoofing']

    def _get_vif_details(self, binding):
        if binding.vif_details:
            try:
                return jsonutils.loads(binding.vif_details)
            except Exception:
                LOG.error(_("Serialized vif_details DB value '%(value)s' "
                            "for port %(port)s is invalid"),
                          {'value': binding.vif_details,
                           'port': binding.port_id})
        return {}

    def _get_profile(self, binding):
        if binding.profile:
            try:
                return jsonutils.loads(binding.profile)
            except Exception:
                LOG.error(_("Serialized profile DB value '%(value)s' for "
                            "port %(port)s is invalid"),
                          {'value': binding.profile,
                           'port': binding.port_id})
        return {}

    def _ml2_extend_port_dict_binding(self, port_res, port_db):
        # None when called during unit tests for other plugins.
        if port_db.port_binding:
            self._update_port_dict_binding(port_res, port_db.port_binding)

    db_base_plugin_v2.NeutronDbPluginV2.register_dict_extend_funcs(
        attributes.PORTS, ['_ml2_extend_port_dict_binding'])

    # Register extend dict methods for network and port resources.
    # Each mechanism driver that supports extend attribute for the resources
    # can add those attribute to the result.
    db_base_plugin_v2.NeutronDbPluginV2.register_dict_extend_funcs(
               attributes.NETWORKS, ['_ml2_md_extend_network_dict'])
    db_base_plugin_v2.NeutronDbPluginV2.register_dict_extend_funcs(
               attributes.PORTS, ['_ml2_md_extend_port_dict'])
    db_base_plugin_v2.NeutronDbPluginV2.register_dict_extend_funcs(
               attributes.SUBNETS, ['_ml2_md_extend_subnet_dict'])

    def _ml2_md_extend_network_dict(self, result, netdb):
        session = db_api.get_session()
        with session.begin(subtransactions=True):
            self.extension_manager.extend_network_dict(session, result)

    def _ml2_md_extend_port_dict(self, result, portdb):
        session = db_api.get_session()
        with session.begin(subtransactions=True):
            self.extension_manager.extend_port_dict(session, result)

    def _ml2_md_extend_subnet_dict(self, result, subnetdb):
        session = db_api.get_session()
        with session.begin(subtransactions=True):
            self.extension_manager.extend_subnet_dict(session, result)

    # Note - The following hook methods have "ml2" in their names so
    # that they are not called twice during unit tests due to global
    # registration of hooks in portbindings_db.py used by other
    # plugins.

    def _ml2_port_model_hook(self, context, original_model, query):
        query = query.outerjoin(models.PortBinding,
                                (original_model.id ==
                                 models.PortBinding.port_id))
        return query

    def _ml2_port_result_filter_hook(self, query, filters):
        values = filters and filters.get(portbindings.HOST_ID, [])
        if not values:
            return query
        return query.filter(models.PortBinding.host.in_(values))

    db_base_plugin_v2.NeutronDbPluginV2.register_model_query_hook(
        models_v2.Port,
        "ml2_port_bindings",
        '_ml2_port_model_hook',
        None,
        '_ml2_port_result_filter_hook')

    def _notify_port_updated(self, mech_context):
        port = mech_context._port
        segment = mech_context.bound_segment
        if not segment:
            # REVISIT(rkukura): This should notify agent to unplug port
            network = mech_context.network.current
            LOG.warning(_("In _notify_port_updated(), no bound segment for "
                          "port %(port_id)s on network %(network_id)s"),
                        {'port_id': port['id'],
                         'network_id': network['id']})
            return
        self.notifier.port_update(mech_context._plugin_context, port,
                                  segment[api.NETWORK_TYPE],
                                  segment[api.SEGMENTATION_ID],
                                  segment[api.PHYSICAL_NETWORK])

    # TODO(apech): Need to override bulk operations

    def update_extra_net_data(self, context, network, netdb):
        self._process_uos_ratelimit_update(context, network, netdb)

    def add_extra_net_data(self, context, network, netdb):
        self._process_uos_ratelimit_create(context, network, netdb)

    def update_extra_subnet_data(self, context, subnet, subnetdb):
        self._process_uos_service_provider_update(context, subnet, subnetdb)

    def add_extra_subnet_data(self, context, subnet, subnetdb):
        self._process_uos_service_provider_create(context, subnet, subnetdb)

    def create_network(self, context, network):
        net_data = network['network']
        tenant_id = self._get_tenant_id_for_create(context, net_data)
        session = context.session
        with session.begin(subtransactions=True):
            self._ensure_default_security_group(context, tenant_id)
            result = super(Ml2Plugin, self).create_network(context, network)
            self.extension_manager.process_create_network(session, net_data,
                                                          result)
            self._process_l3_create(context, result, net_data)
            net_data['id'] = result['id']
            self.type_manager.create_network_segments(context, net_data,
                                                      tenant_id)
            self.type_manager._extend_network_dict_provider(context, result)
            mech_context = driver_context.NetworkContext(self, context,
                                                         result)
            self.mechanism_manager.create_network_precommit(mech_context)

        try:
            self.mechanism_manager.create_network_postcommit(mech_context)
        except ml2_exc.MechanismDriverError:
            with excutils.save_and_reraise_exception():
                LOG.error(_("mechanism_manager.create_network_postcommit "
                            "failed, deleting network '%s'"), result['id'])
                self.delete_network(context, result['id'])
        return result

    def update_network(self, context, id, network):
        provider._raise_if_updates_provider_attributes(network['network'])

        session = context.session
        with session.begin(subtransactions=True):
            original_network = super(Ml2Plugin, self).get_network(context, id)
            updated_network = super(Ml2Plugin, self).update_network(context,
                                                                    id,
                                                                    network)
            self.extension_manager.process_update_network(session, network,
                                                          original_network)
            self._process_l3_update(context, updated_network,
                                    network['network'])
            self.type_manager._extend_network_dict_provider(context,
                                                            updated_network)
            mech_context = driver_context.NetworkContext(
                self, context, updated_network,
                original_network=original_network)
            self.mechanism_manager.update_network_precommit(mech_context)

        # TODO(apech) - handle errors raised by update_network, potentially
        # by re-calling update_network with the previous attributes. For
        # now the error is propogated to the caller, which is expected to
        # either undo/retry the operation or delete the resource.
        self.mechanism_manager.update_network_postcommit(mech_context)
        return updated_network

    def get_network(self, context, id, fields=None):
        session = context.session
        with session.begin(subtransactions=True):
            result = super(Ml2Plugin, self).get_network(context, id, None)
            self.type_manager._extend_network_dict_provider(context, result)

        return self._fields(result, fields)

    def get_networks(self, context, filters=None, fields=None,
                     sorts=None, limit=None, marker=None, page_reverse=False):
        session = context.session
        with session.begin(subtransactions=True):
            nets = super(Ml2Plugin,
                         self).get_networks(context, filters, None, sorts,
                                            limit, marker, page_reverse)
            for net in nets:
                self.type_manager._extend_network_dict_provider(context, net)

            nets = self._filter_nets_provider(context, nets, filters)
            nets = self._filter_nets_l3(context, nets, filters)

        return [self._fields(net, fields) for net in nets]

    def delete_network(self, context, id):
        # REVISIT(rkukura) The super(Ml2Plugin, self).delete_network()
        # function is not used because it auto-deletes ports and
        # subnets from the DB without invoking the derived class's
        # delete_port() or delete_subnet(), preventing mechanism
        # drivers from being called. This approach should be revisited
        # when the API layer is reworked during icehouse.

        LOG.debug(_("Deleting network %s"), id)
        session = context.session
        while True:
            try:
                # REVISIT(rkukura): Its not clear that
                # with_lockmode('update') is really needed in this
                # transaction, and if not, the semaphore can also be
                # removed.
                #
                # REVISIT: Serialize this operation with a semaphore
                # to prevent deadlock waiting to acquire a DB lock
                # held by another thread in the same process, leading
                # to 'lock wait timeout' errors.
                with contextlib.nested(lockutils.lock('db-access'),
                                       session.begin(subtransactions=True)):
                    self._process_l3_delete(context, id)
                    # Get ports to auto-delete.
                    ports = (session.query(models_v2.Port).
                             enable_eagerloads(False).
                             filter_by(network_id=id).
                             with_for_update().all())
                    LOG.debug(_("Ports to auto-delete: %s"), ports)
                    only_auto_del = all(p.device_owner
                                        in db_base_plugin_v2.
                                        AUTO_DELETE_PORT_OWNERS
                                        for p in ports)
                    if not only_auto_del:
                        LOG.debug(_("Tenant-owned ports exist"))
                        raise exc.NetworkInUse(net_id=id)

                    # Get subnets to auto-delete.
                    subnets = (session.query(models_v2.Subnet).
                               enable_eagerloads(False).
                               filter_by(network_id=id).
                               with_lockmode('update').all())
                    LOG.debug(_("Subnets to auto-delete: %s"), subnets)

                    if not (ports or subnets):
                        network = self.get_network(context, id)
                        mech_context = driver_context.NetworkContext(self,
                                                                     context,
                                                                     network)
                        self.mechanism_manager.delete_network_precommit(
                            mech_context)

                        self.type_manager.release_network_segments(session, id)
                        record = self._get_network(context, id)
                        LOG.debug(_("Deleting network record %s"), record)
                        session.delete(record)

                        # The segment records are deleted via cascade from the
                        # network record, so explicit removal is not necessary.
                        LOG.debug(_("Committing transaction"))
                        break
            except os_db_exception.DBError as e:
                with excutils.save_and_reraise_exception() as ctxt:
                    if isinstance(e.inner_exception, sql_exc.IntegrityError):
                        ctxt.reraise = False
                        msg = _("A concurrent port creation has occurred")
                        LOG.warning(msg)
                        continue

            for port in ports:
                try:
                    self.delete_port(context, port.id)
                except Exception:
                    with excutils.save_and_reraise_exception():
                        LOG.exception(_("Exception auto-deleting port %s"),
                                      port.id)

            for subnet in subnets:
                try:
                    self.delete_subnet(context, subnet.id)
                except Exception:
                    with excutils.save_and_reraise_exception():
                        LOG.exception(_("Exception auto-deleting subnet %s"),
                                      subnet.id)

        try:
            self.mechanism_manager.delete_network_postcommit(mech_context)
        except ml2_exc.MechanismDriverError:
            # TODO(apech) - One or more mechanism driver failed to
            # delete the network.  Ideally we'd notify the caller of
            # the fact that an error occurred.
            LOG.error(_("mechanism_manager.delete_network_postcommit failed"))
        self.notifier.network_delete(context, id)

    def create_subnet(self, context, subnet):
        session = context.session
        with session.begin(subtransactions=True):
            result = super(Ml2Plugin, self).create_subnet(context, subnet)
            self.extension_manager.process_create_subnet(session, subnet,
                                                         result)
            mech_context = driver_context.SubnetContext(self, context, result)
            self.mechanism_manager.create_subnet_precommit(mech_context)

        try:
            self.mechanism_manager.create_subnet_postcommit(mech_context)
        except ml2_exc.MechanismDriverError:
            with excutils.save_and_reraise_exception():
                LOG.error(_("mechanism_manager.create_subnet_postcommit "
                            "failed, deleting subnet '%s'"), result['id'])
                self.delete_subnet(context, result['id'])
        return result

    def update_subnet(self, context, id, subnet):
        session = context.session
        with session.begin(subtransactions=True):
            original_subnet = super(Ml2Plugin, self).get_subnet(context, id)
            updated_subnet = super(Ml2Plugin, self).update_subnet(
                context, id, subnet)
            self.extension_manager.process_update_subnet(session, subnet,
                                                         original_subnet)
            mech_context = driver_context.SubnetContext(
                self, context, updated_subnet, original_subnet=original_subnet)
            self.mechanism_manager.update_subnet_precommit(mech_context)

        # TODO(apech) - handle errors raised by update_subnet, potentially
        # by re-calling update_subnet with the previous attributes. For
        # now the error is propogated to the caller, which is expected to
        # either undo/retry the operation or delete the resource.
        self.mechanism_manager.update_subnet_postcommit(mech_context)
        return updated_subnet

    def delete_subnet(self, context, id):
        # REVISIT(rkukura) The super(Ml2Plugin, self).delete_subnet()
        # function is not used because it deallocates the subnet's addresses
        # from ports in the DB without invoking the derived class's
        # update_port(), preventing mechanism drivers from being called.
        # This approach should be revisited when the API layer is reworked
        # during icehouse.

        LOG.debug(_("Deleting subnet %s"), id)
        session = context.session
        while True:
            # REVISIT: Serialize this operation with a semaphore to
            # prevent deadlock waiting to acquire a DB lock held by
            # another thread in the same process, leading to 'lock
            # wait timeout' errors.
            with contextlib.nested(lockutils.lock('db-access'),
                                   session.begin(subtransactions=True)):
                subnet = self.get_subnet(context, id)
                # Get ports to auto-deallocate
                allocated = (session.query(models_v2.IPAllocation).
                             filter_by(subnet_id=id).
                             join(models_v2.Port).
                             filter_by(network_id=subnet['network_id']).
                             with_lockmode('update').all())
                LOG.debug(_("Ports to auto-deallocate: %s"), allocated)
                only_auto_del = all(not a.port_id or
                                    a.ports.device_owner in db_base_plugin_v2.
                                    AUTO_DELETE_PORT_OWNERS
                                    for a in allocated)
                if not only_auto_del:
                    LOG.debug(_("Tenant-owned ports exist"))
                    raise exc.SubnetInUse(subnet_id=id)

                if not allocated:
                    mech_context = driver_context.SubnetContext(self, context,
                                                                subnet)
                    self.mechanism_manager.delete_subnet_precommit(
                        mech_context)

                    LOG.debug(_("Deleting subnet record"))
                    record = self._get_subnet(context, id)
                    session.delete(record)

                    LOG.debug(_("Committing transaction"))
                    break

            for a in allocated:
                if a.port_id:
                    # calling update_port() for each allocation to remove the
                    # IP from the port and call the MechanismDrivers
                    data = {'port':
                            {'fixed_ips': [{'subnet_id': ip.subnet_id,
                                            'ip_address': ip.ip_address}
                                           for ip in a.ports.fixed_ips
                                           if ip.subnet_id != id]}}
                    try:
                        self.update_port(context, a.port_id, data)
                    except Exception:
                        with excutils.save_and_reraise_exception():
                            LOG.exception(_("Exception deleting fixed_ip from "
                                            "port %s"), a.port_id)
                session.delete(a)

        try:
            self.mechanism_manager.delete_subnet_postcommit(mech_context)
        except ml2_exc.MechanismDriverError:
            # TODO(apech) - One or more mechanism driver failed to
            # delete the subnet.  Ideally we'd notify the caller of
            # the fact that an error occurred.
            LOG.error(_("mechanism_manager.delete_subnet_postcommit failed"))

    def create_port(self, context, port):
        attrs = port['port']
        attrs['status'] = const.PORT_STATUS_DOWN

        uos_limits = cfg.CONF.unitedstack.uos_pps_limits
        if cfg.CONF.unitedstack.uos_pps_limits_enable and uos_limits:
            attrs[portbindings.PROFILE] = {}
            attrs[portbindings.PROFILE]['uos_pps_limits'] = uos_limits

        # mark must more than 2, mark: ratelimit in kbps
        uos_marks = cfg.CONF.unitedstack.uos_marks
        uos_mark_actions = cfg.CONF.unitedstack.uos_mark_actions
        uos_marks_dict = {}
        for uos_mark in uos_marks:
            marks = uos_mark.split(":")
            uos_marks_dict[marks[0]] = marks[1]
        if uos_marks_dict:
            attrs[portbindings.PROFILE] = {
                'uos_marks': uos_marks_dict,
                'uos_mark_actions': uos_mark_actions}
        session = context.session
        with session.begin(subtransactions=True):
            self._ensure_default_security_group_on_port(context, port)
            sgids = self._get_security_groups_on_port(context, port)
            dhcp_opts = port['port'].get(edo_ext.EXTRADHCPOPTS, [])
            result = super(Ml2Plugin, self).create_port(context, port)
            self.extension_manager.process_create_port(session, attrs, result)
            self._process_port_create_security_group(context, result, sgids)
            network = self.get_network(context, result['network_id'])
            binding = db.add_port_binding(session, result['id'])
            mech_context = driver_context.PortContext(self, context, result,
                                                      network, binding)
            self._process_port_binding(mech_context, context, attrs)

            result[addr_pair.ADDRESS_PAIRS] = (
                self._process_create_allowed_address_pairs(
                    context, result,
                    attrs.get(addr_pair.ADDRESS_PAIRS)))
            self._process_port_create_extra_dhcp_opts(context, result,
                                                      dhcp_opts)
            self.mechanism_manager.create_port_precommit(mech_context)

        try:
            self.mechanism_manager.create_port_postcommit(mech_context)
        except ml2_exc.MechanismDriverError:
            with excutils.save_and_reraise_exception():
                LOG.error(_("mechanism_manager.create_port_postcommit "
                            "failed, deleting port '%s'"), result['id'])
                self.delete_port(context, result['id'])

        # REVISIT(rkukura): Is there any point in calling this before
        # a binding has been successfully established?
        self.notify_security_groups_member_updated(context, result)

        try:
            bound_context = self._bind_port_if_needed(mech_context)
        except ml2_exc.MechanismDriverError:
            with excutils.save_and_reraise_exception():
                LOG.error(_("_bind_port_if_needed "
                            "failed, deleting port '%s'"), result['id'])
                self.delete_port(context, result['id'])
        return bound_context._port

    def update_port(self, context, id, port):
        attrs = port['port']
        need_port_update_notify = False
        if portbindings.PROFILE in attrs:
            if 'uos_marks' in attrs[portbindings.PROFILE]:
                uos_mark_dict = {}
                uos_marks = attrs[portbindings.PROFILE]['uos_marks']
                #"1->10;20>20"
                if uos_marks:
                    for uos_mark in uos_marks.split(";"):
                        if uos_mark:
                            marks = uos_mark.split("->")
                            uos_mark_dict[marks[0]] = marks[1]
                    mark_len = len(uos_mark_dict.keys())
                    attrs[portbindings.PROFILE]['uos_marks'] = uos_mark_dict
                    mark_actions = (attrs[portbindings.PROFILE][
                                        'uos_mark_actions'])
                    action_len = 0
                    if mark_actions:
                        actons = mark_actions.split(";")
                        attrs[portbindings.PROFILE]['uos_mark_actions'] = (
                            actons)
                        action_len = len(actons)
                    if action_len != mark_len:
                        msg = _("in valid marks")
                        raise exc.InvalidInput(error_message=msg)

        session = context.session

        # REVISIT: Serialize this operation with a semaphore to
        # prevent deadlock waiting to acquire a DB lock held by
        # another thread in the same process, leading to 'lock wait
        # timeout' errors.
        with contextlib.nested(lockutils.lock('db-access'),
                               session.begin(subtransactions=True)):
            port_db, binding = db.get_locked_port_and_binding(session, id)
            if not port_db:
                raise exc.PortNotFound(port_id=id)
            original_port = self._make_port_dict(port_db)
            updated_port = super(Ml2Plugin, self).update_port(context, id,
                                                              port)
            self.extension_manager.process_update_port(session, attrs,
                                                       original_port)
            if addr_pair.ADDRESS_PAIRS in port['port']:
                need_port_update_notify |= (
                    self.update_address_pairs_on_port(context, id, port,
                                                      original_port,
                                                      updated_port))
            need_port_update_notify |= self.update_security_group_on_port(
                context, id, port, original_port, updated_port)
            network = self.get_network(context, original_port['network_id'])
            need_port_update_notify |= self._update_extra_dhcp_opts_on_port(
                context, id, port, updated_port)
            mech_context = driver_context.PortContext(
                self, context, updated_port, network, binding,
                original_port=original_port)
            need_port_update_notify |= self._process_port_binding(
                mech_context, context, attrs)
            self.mechanism_manager.update_port_precommit(mech_context)

        # TODO(apech) - handle errors raised by update_port, potentially
        # by re-calling update_port with the previous attributes. For
        # now the error is propogated to the caller, which is expected to
        # either undo/retry the operation or delete the resource.
        self.mechanism_manager.update_port_postcommit(mech_context)

        need_port_update_notify |= self.is_security_group_member_updated(
            context, original_port, updated_port)

        if need_port_update_notify:
            LOG.info(_("notify old security group member"
                          " change for port %s"), id)
            self.notifier.security_groups_member_updated(
                context, original_port.get(ext_sg.SECURITYGROUPS, []))

        if original_port['admin_state_up'] != updated_port['admin_state_up']:
            need_port_update_notify = True

        # NOTE(changzhi) Notify agent when disable_anti_spoofing is changed.
        if original_port['disable_anti_spoofing'] != updated_port['disable_anti_spoofing']:
            need_port_update_notify = True

        bound_port = self._bind_port_if_needed(
            mech_context,
            allow_notify=True,
            need_notify=need_port_update_notify)
        return bound_port._port

    def _process_dvr_port_binding(self, mech_context, context, attrs):
        binding = mech_context._binding
        port = mech_context.current

        if binding.vif_type != portbindings.VIF_TYPE_UNBOUND:
            binding.vif_details = ''
            binding.vif_type = portbindings.VIF_TYPE_UNBOUND
            binding.driver = None
            binding.segment = None
            binding.host = ''

        self._update_port_dict_binding(port, binding)
        binding.host = attrs and attrs.get(portbindings.HOST_ID)

    def update_dvr_port_binding(self, context, id, port):
        attrs = port['port']

        host = attrs and attrs.get(portbindings.HOST_ID)
        host_set = attributes.is_attr_set(host)

        if not host_set:
            LOG.error(_("No Host supplied to bind DVR Port %s"), id)
            return

        session = context.session
        binding = db.get_dvr_port_binding_by_host(session, id, host)
        if (not binding or
            binding.vif_type == portbindings.VIF_TYPE_BINDING_FAILED):
            with session.begin(subtransactions=True):
                if not binding:
                    binding = db.ensure_dvr_port_binding(
                        session, id, host, router_id=attrs['device_id'])
                orig_port = super(Ml2Plugin, self).get_port(context, id)
                network = self.get_network(context, orig_port['network_id'])
                mech_context = driver_context.DvrPortContext(self,
                    context, orig_port, network,
                    binding, original_port=orig_port)
                self._process_dvr_port_binding(mech_context, context, attrs)
            self.mechanism_manager.bind_port(mech_context)
            # Now try to commit result of attempting to bind the port.
            self._commit_dvr_port_binding(mech_context._plugin_context,
                                          orig_port['id'],
                                          host,
                                          mech_context)

    def _commit_dvr_port_binding(self, plugin_context,
                                 port_id, host,
                                 mech_context):
        session = plugin_context.session
        new_binding = mech_context._binding
        with contextlib.nested(lockutils.lock('db-access'),
                               session.begin(subtransactions=True)):
            # Get the current port state and build a new PortContext
            # reflecting this state as original state for subsequent
            # mechanism driver update_port_*commit() calls.
            cur_binding = db.get_dvr_port_binding_by_host(session,
                                                          port_id,
                                                          host)
            # Commit our binding results only if port has not been
            # successfully bound concurrently by another thread or
            # process and no binding inputs have been changed.
            commit = ((cur_binding.vif_type in
                       [portbindings.VIF_TYPE_UNBOUND,
                        portbindings.VIF_TYPE_BINDING_FAILED]) and
                      new_binding.host == cur_binding.host and
                      new_binding.vnic_type == cur_binding.vnic_type and
                      new_binding.profile == cur_binding.profile)

            if commit:
                # Update the port's binding state with our binding
                # results.
                cur_binding.vif_type = new_binding.vif_type
                cur_binding.vif_details = new_binding.vif_details
                cur_binding.driver = new_binding.driver
                cur_binding.segment = new_binding.segment
                if cur_binding.profile != new_binding.profile:
                    cur_binding.profile = new_binding.profile

    def delete_port(self, context, id, l3_port_check=True, **kwargs):
        LOG.debug(_("Deleting port %s"), id)
        removed_routers = []
        l3plugin = manager.NeutronManager.get_service_plugins().get(
            service_constants.L3_ROUTER_NAT)
        is_dvr_enabled = utils.is_extension_supported(
            l3plugin, const.L3_DISTRIBUTED_EXT_ALIAS)
        if l3plugin and l3_port_check:
            l3plugin.prevent_l3_port_deletion(context, id)

        session = context.session
        # REVISIT: Serialize this operation with a semaphore to
        # prevent deadlock waiting to acquire a DB lock held by
        # another thread in the same process, leading to 'lock wait
        # timeout' errors.
        with contextlib.nested(lockutils.lock('db-access'),
                               session.begin(subtransactions=True)):
            port_db, binding = db.get_locked_port_and_binding(session, id)
            if not port_db:
                # the port existed when l3plugin.prevent_l3_port_deletion
                # was called but now is already gone
                LOG.debug(_("The port '%s' was deleted"), id)
                return
            port = self._make_port_dict(port_db)
            from_nova = (kwargs and 'filters' in kwargs and
                         '_x_nova' in kwargs['filters'])
            if not from_nova:
                device_owner = port['device_owner']
                device_id = port['device_id']
                if (device_id and
                    device_owner.startswith(const.DEVICE_OWNER_COMPUTE_PRE)):
                    raise exc.PortInUseByNova(port_id=port['id'],
                                              device_id=device_id)
            network = self.get_network(context, port['network_id'])
            mech_context = None
            if port['device_owner'] == const.DEVICE_OWNER_DVR_INTERFACE:
                bindings = db.get_dvr_port_bindings(context.session, id)
                for bind in bindings:
                    mech_context = driver_context.DvrPortContext(
                        self, context, port, network, bind)
                    self.mechanism_manager.delete_port_precommit(mech_context)
            else:
                mech_context = driver_context.PortContext(self, context, port,
                                                          network, binding)
                if "compute:" in port['device_owner'] and is_dvr_enabled:
                    router_info = l3plugin.dvr_deletens_if_no_vm(context, id)
                    removed_routers += router_info
                self.mechanism_manager.delete_port_precommit(mech_context)
                self._delete_port_security_group_bindings(context, id)
            if l3plugin:
                router_ids = l3plugin.disassociate_floatingips(
                    context, id, do_notify=False)
                if is_dvr_enabled:
                    l3plugin.dvr_vmarp_table_update(context, id, "del")

            LOG.debug("Calling delete_port for %(port_id)s owned by %(owner)s"
                      % {"port_id": id, "owner": port['device_owner']})
            super(Ml2Plugin, self).delete_port(context, id)

        # now that we've left db transaction, we are safe to notify
        if l3plugin:
            l3plugin.notify_routers_updated(context, router_ids)
            for router in removed_routers:
                l3plugin.remove_router_from_l3_agent(
                    context, router['agent_id'], router['router_id'])

        try:
            # for both normal and DVR Interface ports, only one invocation of
            # delete_port_postcommit.  We use gather/scatter technique for DVR
            # interface ports, where the bindings are gathered in
            # delete_port_precommit() call earlier and scattered as l2pop
            # rules to cloud nodes in delete_port_postcommit() here
            if mech_context:
                self.mechanism_manager.delete_port_postcommit(mech_context)
        except ml2_exc.MechanismDriverError:
            # TODO(apech) - One or more mechanism driver failed to
            # delete the port.  Ideally we'd notify the caller of the
            # fact that an error occurred.
            LOG.error(_("mechanism_manager.delete_port_postcommit failed for "
                        "port %s"), id)
        self.notify_security_groups_member_updated(context, port)

    def get_bound_port_context(self, plugin_context, port_id, host=None):
        session = plugin_context.session
        with session.begin(subtransactions=True):
            try:
                port_db = (session.query(models_v2.Port).
                           enable_eagerloads(False).
                           filter(models_v2.Port.id.startswith(port_id)).
                           one())
            except sa_exc.NoResultFound:
                return
            except exc.MultipleResultsFound:
                LOG.error(_("Multiple ports have port_id starting with %s"),
                          port_id)
                return
            port = self._make_port_dict(port_db)
            network = self.get_network(plugin_context, port['network_id'])
            if port['device_owner'] == const.DEVICE_OWNER_DVR_INTERFACE:
                binding = db.get_dvr_port_binding_by_host(
                    session, port['id'], host)
                if not binding:
                    LOG.error(_("Binding info for DVR port %s not found"),
                              port_id)
                    return None
                port_context = driver_context.DvrPortContext(
                    self, plugin_context, port, network, binding)
            else:
                port_context = driver_context.PortContext(
                    self, plugin_context, port, network, port_db.port_binding)

        return self._bind_port_if_needed(port_context)

    def update_port_status(self, context, port_id, status, host=None):
        """
        Returns port_id (non-truncated uuid) if the port exists.
        Otherwise returns None.
        """
        updated = False
        session = context.session
        # REVISIT: Serialize this operation with a semaphore to
        # prevent deadlock waiting to acquire a DB lock held by
        # another thread in the same process, leading to 'lock wait
        # timeout' errors.
        with contextlib.nested(lockutils.lock('db-access'),
                               session.begin(subtransactions=True)):
            port = db.get_port(session, port_id)
            tenant_id = port['tenant_id']
            if not port:
                LOG.warning(_("Port %(port)s updated up by agent not found"),
                            {'port': port_id})
                return None
            if (port.status != status and
                port['device_owner'] != const.DEVICE_OWNER_DVR_INTERFACE):
                original_port = self._make_port_dict(port)
                port.status = status
                updated_port = self._make_port_dict(port)
                network = self.get_network(context,
                                           original_port['network_id'])
                mech_context = driver_context.PortContext(
                    self, context, updated_port, network, port.port_binding,
                    original_port=original_port)
                self.mechanism_manager.update_port_precommit(mech_context)
                updated = True
            elif port['device_owner'] == const.DEVICE_OWNER_DVR_INTERFACE:
                binding = db.get_dvr_port_binding_by_host(
                    session, port['id'], host)
                if not binding:
                    return
                binding['status'] = status
                binding.update(binding)
                updated = True

        if (updated and
            port['device_owner'] == const.DEVICE_OWNER_DVR_INTERFACE):
            with contextlib.nested(lockutils.lock('db-access'),
                                   session.begin(subtransactions=True)):
                port = db.get_port(session, port_id)
                if not port:
                    LOG.warning(_("Port %s not found during update"), port_id)
                    return
                original_port = self._make_port_dict(port)
                network = self.get_network(context,
                                           original_port['network_id'])
                port.status = db.generate_dvr_port_status(session, port['id'])
                updated_port = self._make_port_dict(port)
                mech_context = (driver_context.DvrPortContext(
                    self, context, updated_port, network,
                    binding, original_port=original_port))
                self.mechanism_manager.update_port_precommit(mech_context)

        if updated:
            self.mechanism_manager.update_port_postcommit(mech_context)
            _ctx = n_context.Context('', tenant_id)
            payload = {'id': port_id, 'status': status}
            uos_utils.send_notification(_ctx,
                          'port.update_status.end', payload)
        if port['device_owner'] == const.DEVICE_OWNER_DVR_INTERFACE:
            db.delete_dvr_port_binding_if_stale(session, binding)

        return port['id']

    def port_bound_to_host(self, context, port_id, host):
        port = db.get_port(context.session, port_id)
        if not port:
            LOG.debug("No Port match for: %s", port_id)
            return False
        if port['device_owner'] == const.DEVICE_OWNER_DVR_INTERFACE:
            bindings = db.get_dvr_port_bindings(context.session, port_id)
            for b in bindings:
                if b.host == host:
                    return True
            LOG.debug("No binding found for DVR port %s", port['id'])
            return False
        else:
            port_host = db.get_port_binding_host(port_id)
            return (port_host == host)

    def get_port_from_device(self, device):
        port_id = self._device_to_port_id(device)
        port = db.get_port_and_sgs(port_id)
        if port:
            port['device'] = device
        return port

    def _device_to_port_id(self, device):
        # REVISIT(rkukura): Consider calling into MechanismDrivers to
        # process device names, or having MechanismDrivers supply list
        # of device prefixes to strip.
        if device.startswith(TAP_DEVICE_PREFIX):
            return device[TAP_DEVICE_PREFIX_LENGTH:]
        else:
            # REVISIT(irenab): Consider calling into bound MD to
            # handle the get_device_details RPC, then remove the 'else' clause
            if not uuidutils.is_uuid_like(device):
                port = db.get_port_from_device_mac(device)
                if port:
                    return port.id
        return device
