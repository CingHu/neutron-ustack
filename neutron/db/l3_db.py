# Copyright 2012 VMware, Inc.  All rights reserved.
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

import netaddr
from oslo.config import cfg
import sqlalchemy as sa
from sqlalchemy import orm
from sqlalchemy.orm import exc

from neutron.api.rpc.agentnotifiers import l3_rpc_agent_api
from neutron.api.v2 import attributes
from neutron.common import constants as l3_constants
from neutron.common import exceptions as n_exc
from neutron.common import rpc as n_rpc
from neutron.common import uos_constants as uos_l3_constants
from neutron.common import utils
from neutron.db import model_base
from neutron.db import models_v2
from neutron.extensions import external_net
from neutron.extensions import floatingip_ratelimits as fip_rate
from neutron.extensions import l3
from neutron.extensions import uosfloatingip
from neutron import manager
from neutron.openstack.common import log as logging
from neutron.openstack.common import uuidutils
from neutron.plugins.common import constants

LOG = logging.getLogger(__name__)


DEVICE_OWNER_ROUTER_INTF = l3_constants.DEVICE_OWNER_ROUTER_INTF
DEVICE_OWNER_ROUTER_GW = l3_constants.DEVICE_OWNER_ROUTER_GW
DEVICE_OWNER_FLOATINGIP = l3_constants.DEVICE_OWNER_FLOATINGIP
EXTERNAL_GW_INFO = l3.EXTERNAL_GW_INFO

# Maps API field to DB column
# API parameter name and Database column names may differ.
# Useful to keep the filtering between API and Database.
API_TO_DB_COLUMN_MAP = {'port_id': 'fixed_port_id'}
CORE_ROUTER_ATTRS = ('id', 'name', 'tenant_id', 'admin_state_up', 'status')


class Router(model_base.BASEV2, models_v2.HasId, models_v2.HasTenant,
             models_v2.TimestampMixin):
    """Represents a v2 neutron router."""

    name = sa.Column(sa.String(255))
    status = sa.Column(sa.String(16))
    admin_state_up = sa.Column(sa.Boolean)
    gw_port_id = sa.Column(sa.String(36), sa.ForeignKey('ports.id'))
    gw_port = orm.relationship(models_v2.Port, lazy='joined')


class FloatingIP(model_base.BASEV2, models_v2.HasId, models_v2.HasTenant,
                 models_v2.TimestampMixin):
    """Represents a floating IP address.

    This IP address may or may not be allocated to a tenant, and may or
    may not be associated with an internal port/ip address/router.
    """

    floating_ip_address = sa.Column(sa.String(64), nullable=False)
    floating_network_id = sa.Column(sa.String(36), nullable=False)
    floating_port_id = sa.Column(sa.String(36), sa.ForeignKey('ports.id'),
                                 nullable=False)
    fixed_port_id = sa.Column(sa.String(36), sa.ForeignKey('ports.id'))
    fixed_ip_address = sa.Column(sa.String(64))
    router_id = sa.Column(sa.String(36), sa.ForeignKey('routers.id'))
    # Additional attribute for keeping track of the router where the floating
    # ip was associated in order to be able to ensure consistency even if an
    # aysnchronous backend is unavailable when the floating IP is disassociated
    last_known_router_id = sa.Column(sa.String(36))
    status = sa.Column(sa.String(16))
    floating_subnet_id = sa.Column(sa.String(36), nullable=True)
    uos_service_provider = sa.Column(sa.String(255), nullable=True)
    uos_name = sa.Column(sa.String(255), nullable=True)
    uos_registerno = sa.Column(sa.String(255), default='')
    # rate limit in KB
    rate_limit = sa.Column(sa.Integer(), default=1024)
    service_instance_id = sa.Column(sa.String(255),
                                    sa.ForeignKey('serviceinstances.id'),
                                    nullable=True,default='')


class L3_NAT_dbonly_mixin(l3.RouterPluginBase):
    """Mixin class to add L3/NAT router methods to db_base_plugin_v2."""

    router_device_owners = (
        DEVICE_OWNER_ROUTER_INTF,
        DEVICE_OWNER_ROUTER_GW,
        DEVICE_OWNER_FLOATINGIP
    )

    @property
    def _core_plugin(self):
        return manager.NeutronManager.get_plugin()

    def _get_router(self, context, router_id):
        try:
            router = self._get_by_id(context, Router, router_id)
        except exc.NoResultFound:
            raise l3.RouterNotFound(router_id=router_id)
        return router

    def _make_router_dict(self, router, fields=None, process_extensions=True):
        res = dict((key, router[key]) for key in CORE_ROUTER_ATTRS)
        if router['gw_port_id']:
            ext_gw_info = {'network_id': router.gw_port['network_id']}
        else:
            ext_gw_info = None
        res.update({
            EXTERNAL_GW_INFO: ext_gw_info,
            'gw_port_id': router['gw_port_id'],
        })
        # NOTE(salv-orlando): The following assumes this mixin is used in a
        # class inheriting from CommonDbMixin, which is true for all existing
        # plugins.
        if process_extensions:
            self._apply_dict_extend_functions(l3.ROUTERS, res, router)
        return self._fields(res, fields)

    def _create_router_db(self, context, router, tenant_id):
        """Create the DB object."""
        with context.session.begin(subtransactions=True):
            # pre-generate id so it will be available when
            # configuring external gw port
            router_db = Router(id=uuidutils.generate_uuid(),
                               tenant_id=tenant_id,
                               name=router['name'],
                               admin_state_up=router['admin_state_up'],
                               status="ACTIVE")
            context.session.add(router_db)
            return router_db

    def create_router(self, context, router):
        r = router['router']
        gw_info = r.pop(EXTERNAL_GW_INFO, None)
        tenant_id = self._get_tenant_id_for_create(context, r)
        with context.session.begin(subtransactions=True):
            router_db = self._create_router_db(context, r, tenant_id)
            utils.make_default_name(router_db, uos_l3_constants.UOS_PRE_ROUTER)
            if gw_info:
                self._update_router_gw_info(context, router_db['id'],
                                            gw_info, router=router_db)
        return self._make_router_dict(router_db)

    def _update_router_db(self, context, router_id, data, gw_info):
        """Update the DB object and related gw info, if available."""
        with context.session.begin(subtransactions=True):
            if gw_info != attributes.ATTR_NOT_SPECIFIED:
                self._update_router_gw_info(context, router_id, gw_info)
            router_db = self._get_router(context, router_id)
            if data:
                router_db.update(data)
            return router_db

    def update_router(self, context, id, router):
        r = router['router']
        # NOTE(gongysh) for the purpose update the router with
        # data return router created just
        r.pop('created_at', None)
        gw_info = r.pop(EXTERNAL_GW_INFO, attributes.ATTR_NOT_SPECIFIED)
        # check whether router needs and can be rescheduled to the proper
        # l3 agent (associated with given external network);
        # do check before update in DB as an exception will be raised
        # in case no proper l3 agent found
        if gw_info != attributes.ATTR_NOT_SPECIFIED:
            candidates = self._check_router_needs_rescheduling(
                context, id, gw_info)
        else:
            candidates = None
        router_db = self._update_router_db(context, id, r, gw_info)
        if candidates:
            l3_plugin = manager.NeutronManager.get_service_plugins().get(
                constants.L3_ROUTER_NAT)
            l3_plugin.reschedule_router(context, id, candidates)
        return self._make_router_dict(router_db)

    def _check_router_needs_rescheduling(self, context, router_id, gw_info):
        """Checks whether router's l3 agent can handle the given network

        When external_network_bridge is set, each L3 agent can be associated
        with at most one external network. If router's new external gateway
        is on other network then the router needs to be rescheduled to the
        proper l3 agent.
        If external_network_bridge is not set then the agent
        can support multiple external networks and rescheduling is not needed

        :return: list of candidate agents if rescheduling needed,
        None otherwise; raises exception if there is no eligible l3 agent
        associated with target external network
        """
        # TODO(obondarev): rethink placement of this func as l3 db manager is
        # not really a proper place for agent scheduling stuff
        network_id = gw_info.get('network_id') if gw_info else None
        if not network_id:
            return

        nets = self._core_plugin.get_networks(
            context, {external_net.EXTERNAL: [True]})
        # nothing to do if there is only one external network
        if len(nets) <= 1:
            return

        # first get plugin supporting l3 agent scheduling
        # (either l3 service plugin or core_plugin)
        l3_plugin = manager.NeutronManager.get_service_plugins().get(
            constants.L3_ROUTER_NAT)
        if (not utils.is_extension_supported(
                l3_plugin,
                l3_constants.L3_AGENT_SCHEDULER_EXT_ALIAS) or
            l3_plugin.router_scheduler is None):
            # that might mean that we are dealing with non-agent-based
            # implementation of l3 services
            return

        cur_agents = l3_plugin.list_l3_agents_hosting_router(
            context, router_id)['agents']
        for agent in cur_agents:
            ext_net_id = agent['configurations'].get(
                'gateway_external_network_id')
            ext_bridge = agent['configurations'].get(
                'external_network_bridge', 'br-ex')
            if (ext_net_id == network_id or
                    (not ext_net_id and not ext_bridge)):
                return

        # otherwise find l3 agent with matching gateway_external_network_id
        active_agents = l3_plugin.get_l3_agents(context, active=True)
        router = {
            'id': router_id,
            'external_gateway_info': {'network_id': network_id}
        }
        candidates = l3_plugin.get_l3_agent_candidates(context,
                                                       router,
                                                       active_agents)
        if not candidates:
            msg = (_('No eligible l3 agent associated with external network '
                     '%s found') % network_id)
            raise n_exc.BadRequest(resource='router', msg=msg)

        return candidates

    def _create_router_gw_port(self, context, router, network_id):
        # Port has no 'tenant-id', as it is hidden from user
        gw_port_data = {'tenant_id': '',  # intentionally not set
                        'network_id': network_id,
                        'mac_address': attributes.ATTR_NOT_SPECIFIED,
                        'fixed_ips': attributes.ATTR_NOT_SPECIFIED,
                        'device_id': router['id'],
                        'device_owner': DEVICE_OWNER_ROUTER_GW,
                        'admin_state_up': True,
                        'name': ''}
        shadow_subnet = cfg.CONF.unitedstack.external_shadow_subnet
        if shadow_subnet:
            subnet_ids = self._core_plugin.get_subnets(
                context.elevated(),
                filters={'name': [shadow_subnet],
                         'network_id': [network_id]},
                fields=['id'])
            if subnet_ids:
                gw_port_data['fixed_ips'] = [{'subnet_id':
                                              subnet_ids[0]['id']}]
                _tenant_id = self._get_tenant_id_for_create(
                    context, router)
                gw_port_data['name'] = _tenant_id
                #gongysh remove the tenant_id after frontier
                # fix it.
                gw_port_data['tenant_id'] = _tenant_id
        gw_port = self._core_plugin.create_port(context.elevated(), {
            'port': gw_port_data})
        if not gw_port['fixed_ips']:
            self._core_plugin.delete_port(context.elevated(), gw_port['id'],
                                          l3_port_check=False)
            msg = (_('No IPs available for external network %s') %
                   network_id)
            raise n_exc.BadRequest(resource='router', msg=msg)

        with context.session.begin(subtransactions=True):
            router.gw_port = self._core_plugin._get_port(context.elevated(),
                                                         gw_port['id'])
            context.session.add(router)

    def _validate_gw_info(self, context, gw_port, info):
        network_id = info['network_id'] if info else None
        if network_id:
            network_db = self._core_plugin._get_network(context, network_id)
            if not network_db.external:
                msg = _("Network %s is not an external network") % network_id
                raise n_exc.BadRequest(resource='router', msg=msg)
        return network_id

    def _delete_current_gw_port(self, context, router_id, router, new_network):
        """Delete gw port, if it is attached to an old network."""
        is_gw_port_attached_to_existing_network = (
            router.gw_port and router.gw_port['network_id'] != new_network)
        admin_ctx = context.elevated()
        if is_gw_port_attached_to_existing_network:
            if self.get_floatingips_count(
                admin_ctx, {'router_id': [router_id]}):
                raise l3.RouterExternalGatewayInUseByFloatingIp(
                    router_id=router_id, net_id=router.gw_port['network_id'])
            with context.session.begin(subtransactions=True):
                gw_port_id = router.gw_port['id']
                router.gw_port = None
                context.session.add(router)
            self._core_plugin.delete_port(
                admin_ctx, gw_port_id, l3_port_check=False)

    def _create_gw_port(self, context, router_id, router, new_network):
        new_valid_gw_port_attachment = (
            new_network and (not router.gw_port or
                             router.gw_port['network_id'] != new_network))
        if new_valid_gw_port_attachment:
            subnets = self._core_plugin._get_subnets_by_network(context,
                                                                new_network)
            for subnet in subnets:
                self._check_for_dup_router_subnet(context, router_id,
                                                  new_network, subnet['id'],
                                                  subnet['cidr'])
            self._create_router_gw_port(context, router, new_network)

    def _update_router_gw_info(self, context, router_id, info, router=None):
        # TODO(salvatore-orlando): guarantee atomic behavior also across
        # operations that span beyond the model classes handled by this
        # class (e.g.: delete_port)
        router = router or self._get_router(context, router_id)
        gw_port = router.gw_port
        network_id = self._validate_gw_info(context, gw_port, info)
        self._delete_current_gw_port(context, router_id, router, network_id)
        self._create_gw_port(context, router_id, router, network_id)

    def _ensure_router_not_in_use(self, context, router_id):
        admin_ctx = context.elevated()
        router = self._get_router(context, router_id)
        if self.get_floatingips_count(
            admin_ctx, filters={'router_id': [router_id]}):
            raise l3.RouterInUse(router_id=router_id)
        device_owner = self._get_device_owner(context, router)
        device_filter = {'device_id': [router_id],
                         'device_owner': [device_owner]}
        port_count = self._core_plugin.get_ports_count(
            admin_ctx, filters=device_filter)
        if port_count:
            raise l3.RouterInUse(router_id=router_id)
        return router

    def delete_router(self, context, id):
        with context.session.begin(subtransactions=True):
            router = self._ensure_router_not_in_use(context, id)

            #TODO(nati) Refactor here when we have router insertion model
            vpnservice = manager.NeutronManager.get_service_plugins().get(
                constants.VPN)
            if vpnservice:
                vpnservice.check_router_in_use(context, id)

            tunnels = manager.NeutronManager.get_service_plugins().get(
                constants.TUNNEL)
            if tunnels:
                tunnels.check_router_in_use(context, id)

            context.session.delete(router)

            # Delete the gw port after the router has been removed to
            # avoid a constraint violation.
            device_filter = {'device_id': [id],
                             'device_owner': [DEVICE_OWNER_ROUTER_GW]}
            ports = self._core_plugin.get_ports(context.elevated(),
                                                filters=device_filter)
            if ports:
                self._core_plugin._delete_port(context.elevated(),
                                               ports[0]['id'])

    def get_router(self, context, id, fields=None):
        router = self._get_router(context, id)
        return self._make_router_dict(router, fields)

    def get_routers(self, context, filters=None, fields=None,
                    sorts=None, limit=None, marker=None,
                    page_reverse=False):
        marker_obj = self._get_marker_obj(context, 'router', limit, marker)
        return self._get_collection(context, Router,
                                    self._make_router_dict,
                                    filters=filters, fields=fields,
                                    sorts=sorts,
                                    limit=limit,
                                    marker_obj=marker_obj,
                                    page_reverse=page_reverse)

    def get_routers_count(self, context, filters=None):
        return self._get_collection_count(context, Router,
                                          filters=filters)

    def _check_for_dup_router_subnet(self, context, router_id,
                                     network_id, subnet_id, subnet_cidr):
        try:
            rport_qry = context.session.query(models_v2.Port)
            rports = rport_qry.filter_by(device_id=router_id)
            # It's possible these ports are on the same network, but
            # different subnets.
            new_ipnet = netaddr.IPNetwork(subnet_cidr)
            for p in rports:
                for ip in p['fixed_ips']:
                    if ip['subnet_id'] == subnet_id:
                        msg = (_("Router already has a port on subnet %s")
                               % subnet_id)
                        raise n_exc.BadRequest(resource='router', msg=msg)
                    sub_id = ip['subnet_id']
                    cidr = self._core_plugin._get_subnet(context.elevated(),
                                                         sub_id)['cidr']
                    ipnet = netaddr.IPNetwork(cidr)
                    match1 = netaddr.all_matching_cidrs(new_ipnet, [cidr])
                    match2 = netaddr.all_matching_cidrs(ipnet, [subnet_cidr])
                    if match1 or match2:
                        data = {'subnet_cidr': subnet_cidr,
                                'subnet_id': subnet_id,
                                'cidr': cidr,
                                'sub_id': sub_id}
                        msg = (_("Cidr %(subnet_cidr)s of subnet "
                                 "%(subnet_id)s overlaps with cidr %(cidr)s "
                                 "of subnet %(sub_id)s") % data)
                        raise n_exc.BadRequest(resource='router', msg=msg)

            #TODO(gongysh) Refactor here when we have router insertion model
            vpnservice = manager.NeutronManager.get_service_plugins().get(
                constants.VPN)

            if vpnservice:
                vpnservice.check_for_dup_router_subnet(context,
                                                   router_id,
                                                   subnet_id,
                                                   subnet_cidr)
        except exc.NoResultFound:
            pass

    def _get_device_owner(self, context, router=None):
        """Get device_owner for the specified router."""
        # NOTE(armando-migliaccio): in the base case this is invariant
        return DEVICE_OWNER_ROUTER_INTF

    def _validate_interface_info(self, interface_info):
        port_id_specified = interface_info and 'port_id' in interface_info
        subnet_id_specified = interface_info and 'subnet_id' in interface_info
        if not (port_id_specified or subnet_id_specified):
            msg = _("Either subnet_id or port_id must be specified")
            raise n_exc.BadRequest(resource='router', msg=msg)
        if port_id_specified and subnet_id_specified:
            msg = _("Cannot specify both subnet-id and port-id")
            raise n_exc.BadRequest(resource='router', msg=msg)
        return port_id_specified, subnet_id_specified

    def _add_interface_by_port(self, context, router_id, port_id, owner):
        with context.session.begin(subtransactions=True):
            port = self._core_plugin._get_port(context, port_id)
            if port['device_id']:
                raise n_exc.PortInUse(net_id=port['network_id'],
                                      port_id=port['id'],
                                      device_id=port['device_id'])
            fixed_ips = [ip for ip in port['fixed_ips']]
            if len(fixed_ips) != 1:
                msg = _('Router port must have exactly one fixed IP')
                raise n_exc.BadRequest(resource='router', msg=msg)
            subnet_id = fixed_ips[0]['subnet_id']
            subnet = self._core_plugin._get_subnet(context, subnet_id)
            self._check_for_dup_router_subnet(context, router_id,
                                              port['network_id'],
                                              subnet['id'],
                                              subnet['cidr'])
            port.update({'device_id': router_id, 'device_owner': owner})
            return port

    def _add_interface_by_subnet(self, context, router_id, subnet_id, owner):
        subnet = self._core_plugin._get_subnet(context, subnet_id)
        if not subnet['gateway_ip']:
            msg = _('Subnet for router interface must have a gateway IP')
            raise n_exc.BadRequest(resource='router', msg=msg)
        self._check_for_dup_router_subnet(context, router_id,
                                          subnet['network_id'],
                                          subnet_id,
                                          subnet['cidr'])
        fixed_ip = {'ip_address': subnet['gateway_ip'],
                    'subnet_id': subnet['id']}
        return self._core_plugin.create_port(context, {
            'port':
            {'tenant_id': subnet['tenant_id'],
             'network_id': subnet['network_id'],
             'fixed_ips': [fixed_ip],
             'mac_address': attributes.ATTR_NOT_SPECIFIED,
             'admin_state_up': True,
             'device_id': router_id,
             'device_owner': owner,
             'name': ''}})

    @staticmethod
    def _make_router_interface_info(
            router_id, tenant_id, port_id, subnet_id):
        return {
            'id': router_id,
            'tenant_id': tenant_id,
            'port_id': port_id,
            'subnet_id': subnet_id
        }

    def add_router_interface(self, context, router_id, interface_info):
        add_by_port, add_by_sub = self._validate_interface_info(interface_info)
        device_owner = self._get_device_owner(context, router_id)

        if add_by_port:
            port = self._add_interface_by_port(
                context, router_id, interface_info['port_id'], device_owner)
        elif add_by_sub:
            port = self._add_interface_by_subnet(
                context, router_id, interface_info['subnet_id'], device_owner)

        return self._make_router_interface_info(
            router_id, port['tenant_id'], port['id'],
            port['fixed_ips'][0]['subnet_id'])

    def _confirm_router_interface_not_in_use(self, context, router_id,
                                             subnet_id):
        subnet_db = self._core_plugin._get_subnet(context, subnet_id)
        subnet_cidr = netaddr.IPNetwork(subnet_db['cidr'])
        fip_qry = context.session.query(FloatingIP)
        for fip_db in fip_qry.filter_by(router_id=router_id):
            if netaddr.IPAddress(fip_db['fixed_ip_address']) in subnet_cidr:
                raise l3.RouterInterfaceInUseByFloatingIP(
                    router_id=router_id, subnet_id=subnet_id)

    def _remove_interface_by_port(self, context, router_id,
                                  port_id, subnet_id, owner):
        port_db = self._core_plugin._get_port(context, port_id)
        if not (port_db['device_owner'] == owner and
                port_db['device_id'] == router_id):
            raise l3.RouterInterfaceNotFound(router_id=router_id,
                                             port_id=port_id)
        port_subnet_id = port_db['fixed_ips'][0]['subnet_id']
        if subnet_id and port_subnet_id != subnet_id:
            raise n_exc.SubnetMismatchForPort(
                port_id=port_id, subnet_id=subnet_id)
        subnet = self._core_plugin._get_subnet(context, port_subnet_id)
        self._confirm_router_interface_not_in_use(
            context, router_id, port_subnet_id)
        self._core_plugin.delete_port(context, port_db['id'],
                                      l3_port_check=False)
        return (port_db, subnet)

    def _remove_interface_by_subnet(self, context,
                                    router_id, subnet_id, owner):
        self._confirm_router_interface_not_in_use(
            context, router_id, subnet_id)
        subnet = self._core_plugin._get_subnet(context, subnet_id)

        try:
            rport_qry = context.session.query(models_v2.Port)
            ports = rport_qry.filter_by(
                device_id=router_id,
                device_owner=owner,
                network_id=subnet['network_id'])

            for p in ports:
                if p['fixed_ips'][0]['subnet_id'] == subnet_id:
                    self._core_plugin.delete_port(context, p['id'],
                                                  l3_port_check=False)
                    return (p, subnet)
        except exc.NoResultFound:
            pass
        raise l3.RouterInterfaceNotFoundForSubnet(router_id=router_id,
                                                  subnet_id=subnet_id)

    def remove_router_interface(self, context, router_id, interface_info):
        if not interface_info:
            msg = _("Either subnet_id or port_id must be specified")
            raise n_exc.BadRequest(resource='router', msg=msg)

        tunnels = manager.NeutronManager.get_service_plugins().get(
                  constants.TUNNEL)
        if tunnels:
            tunnels.check_router_interface_in_use(context, id, interface_info)

        port_id = interface_info.get('port_id')
        subnet_id = interface_info.get('subnet_id')
        device_owner = self._get_device_owner(context, router_id)
        if port_id:
            port, subnet = self._remove_interface_by_port(context, router_id,
                                                          port_id, subnet_id,
                                                          device_owner)
        elif subnet_id:
            port, subnet = self._remove_interface_by_subnet(
                context, router_id, subnet_id, device_owner)

        return self._make_router_interface_info(router_id, port['tenant_id'],
                                                port['id'], subnet['id'])

    def _get_floatingip(self, context, id):
        try:
            floatingip = self._get_by_id(context, FloatingIP, id)
        except exc.NoResultFound:
            raise l3.FloatingIPNotFound(floatingip_id=id)
        return floatingip

    def _make_floatingip_dict(self, floatingip, fields=None,
                              process_extensions=True):
        res = {'id': floatingip['id'],
               'tenant_id': floatingip['tenant_id'],
               'floating_ip_address': floatingip['floating_ip_address'],
               'floating_network_id': floatingip['floating_network_id'],
               'router_id': floatingip['router_id'],
               'port_id': floatingip['fixed_port_id'],
               'service_instance_id': floatingip['service_instance_id'],
               'fixed_ip_address': floatingip['fixed_ip_address'],
               'status': floatingip['status']}
        if process_extensions:
            self._apply_dict_extend_functions(l3.FLOATINGIPS, res, floatingip)
        return self._fields(res, fields)

    def _get_interface_ports_for_network(self, context, network_id):
        router_intf_qry = context.session.query(models_v2.Port)
        return router_intf_qry.filter_by(
            network_id=network_id,
            device_owner=DEVICE_OWNER_ROUTER_INTF)

    def _get_router_for_floatingip(self, context, internal_port,
                                   internal_subnet_id,
                                   external_network_id):
        subnet_db = self._core_plugin._get_subnet(context.elevated(),
                                                  internal_subnet_id)

        shadow_subnet = cfg.CONF.unitedstack.external_shadow_subnet
        if (shadow_subnet and shadow_subnet == subnet_db['name']):
            return internal_port['device_id']

        if not subnet_db['gateway_ip']:
            msg = (_('Cannot add floating IP to port on subnet %s '
                     'which has no gateway_ip') % internal_subnet_id)
            raise n_exc.BadRequest(resource='floatingip', msg=msg)

        router_intf_ports = self._get_interface_ports_for_network(
            context, internal_port['network_id'])

        for intf_p in router_intf_ports:
            if intf_p['fixed_ips'][0]['subnet_id'] == internal_subnet_id:
                router_id = intf_p['device_id']
                router_gw_qry = context.session.query(models_v2.Port)
                has_gw_port = router_gw_qry.filter_by(
                    network_id=external_network_id,
                    device_id=router_id,
                    device_owner=DEVICE_OWNER_ROUTER_GW).count()
                if has_gw_port:
                    return router_id
                else:
                    raise l3.ExternalGatewayForFloatingIPNotFound(
                        subnet_id=internal_subnet_id,
                        external_network_id=external_network_id,
                        port_id=internal_port['id'],
                        router_id=router_id)

        raise l3.RouterConnectToSubnetNotFound(
            subnet_id=internal_subnet_id,
            port_id=internal_port['id'])

    def _internal_fip_assoc_data(self, context, fip):
        """Retrieve internal port data for floating IP.

        Retrieve information concerning the internal port where
        the floating IP should be associated to.
        """
        internal_port = self._core_plugin._get_port(context.elevated(),
                                                    fip['port_id'])
        #gongysh fip port for gw_port has name as tenant_id
        tenant_id = internal_port['tenant_id'] or internal_port['name']
        if not tenant_id == fip['tenant_id']:
            port_id = fip['port_id']
            if 'id' in fip:
                floatingip_id = fip['id']
                data = {'port_id': port_id,
                        'floatingip_id': floatingip_id}
                msg = (_('Port %(port_id)s is associated with a different '
                         'tenant than Floating IP %(floatingip_id)s and '
                         'therefore cannot be bound.') % data)
            else:
                msg = (_('Cannot create floating IP and bind it to '
                         'Port %s, since that port is owned by a '
                         'different tenant.') % port_id)
            raise n_exc.BadRequest(resource='floatingip', msg=msg)

        internal_subnet_id = None
        if 'fixed_ip_address' in fip and fip['fixed_ip_address']:
            internal_ip_address = fip['fixed_ip_address']
            for ip in internal_port['fixed_ips']:
                if ip['ip_address'] == internal_ip_address:
                    internal_subnet_id = ip['subnet_id']
            if not internal_subnet_id:
                msg = (_('Port %(id)s does not have fixed ip %(address)s') %
                       {'id': internal_port['id'],
                        'address': internal_ip_address})
                raise n_exc.BadRequest(resource='floatingip', msg=msg)
        else:
            ips = [ip['ip_address'] for ip in internal_port['fixed_ips']]
            if not ips:
                msg = (_('Cannot add floating IP to port %s that has'
                         'no fixed IP addresses') % internal_port['id'])
                raise n_exc.BadRequest(resource='floatingip', msg=msg)
            if len(ips) > 1:
                msg = (_('Port %s has multiple fixed IPs.  Must provide'
                         ' a specific IP when assigning a floating IP') %
                       internal_port['id'])
                raise n_exc.BadRequest(resource='floatingip', msg=msg)
            internal_ip_address = internal_port['fixed_ips'][0]['ip_address']
            internal_subnet_id = internal_port['fixed_ips'][0]['subnet_id']
        return internal_port, internal_subnet_id, internal_ip_address

    def get_assoc_data(self, context, fip, floating_network_id):
        """Determine/extract data associated with the internal port.

        When a floating IP is associated with an internal port,
        we need to extract/determine some data associated with the
        internal port, including the internal_ip_address, and router_id.
        The confirmation of the internal port whether owned by the tenant who
        owns the floating IP will be confirmed by _get_router_for_floatingip.
        """
        (internal_port, internal_subnet_id,
         internal_ip_address) = self._internal_fip_assoc_data(context, fip)
        router_id = self._get_router_for_floatingip(context,
                                                    internal_port,
                                                    internal_subnet_id,
                                                    floating_network_id)

        return (fip['port_id'], internal_ip_address, router_id)

    def _check_and_get_fip_assoc(self, context, fip, floatingip_db):
        port_id = internal_ip_address = router_id = None
        if (('fixed_ip_address' in fip and fip['fixed_ip_address']) and
            not ('port_id' in fip and fip['port_id'])):
            msg = _("fixed_ip_address cannot be specified without a port_id")
            raise n_exc.BadRequest(resource='floatingip', msg=msg)
        if 'port_id' in fip and fip['port_id']:
            port_id, internal_ip_address, router_id = self.get_assoc_data(
                context,
                fip,
                floatingip_db['floating_network_id'])
            fip_qry = context.session.query(FloatingIP)
            try:
                fipquery = fip_qry.filter_by(
                    fixed_port_id=fip['port_id'],
                    floating_network_id=floatingip_db['floating_network_id'],
                    fixed_ip_address=internal_ip_address) #.one()
                for fip in fipquery:
                    LOG.info("_check_and_get_fip_assoc exists %s",fip)
                #raise l3.FloatingIPPortAlreadyAssociated(
                #    port_id=fip['port_id'],
                #    fip_id=floatingip_db['id'],
                #    floating_ip_address=floatingip_db['floating_ip_address'],
                #    fixed_ip=internal_ip_address,
                #    net_id=floatingip_db['floating_network_id'])
            except exc.NoResultFound:
                pass
        return port_id, internal_ip_address, router_id

    def _update_fip_assoc(self, context, fip, floatingip_db, external_port):
        previous_router_id = floatingip_db.router_id
        port_id, internal_ip_address, router_id = (
            self._check_and_get_fip_assoc(context, fip, floatingip_db))
        floatingip_db.update({'fixed_ip_address': internal_ip_address,
                              'fixed_port_id': port_id,
                              'router_id': router_id,
                              'last_known_router_id': previous_router_id})
        if fip_rate.RATE_LIMIT in fip:
            floatingip_db[fip_rate.RATE_LIMIT] = fip[fip_rate.RATE_LIMIT]

    def create_floatingip(self, context, floatingip,
            initial_status=l3_constants.FLOATINGIP_STATUS_ACTIVE):
        fip = floatingip['floatingip']
        tenant_id = self._get_tenant_id_for_create(context, fip)
        fip_id = uuidutils.generate_uuid()

        f_net_id = fip['floating_network_id']
        if not self._core_plugin._network_is_external(context, f_net_id):
            msg = _("Network %s is not a valid external network") % f_net_id
            raise n_exc.BadRequest(resource='floatingip', msg=msg)
        subnet_id = fip.get('floating_subnet_id')
        fip_address = fip.get('floating_ip_address')
        service_provider = fip.get(uosfloatingip.UOS_SERVICE_PROVIDER)

        fixed_ips = attributes.ATTR_NOT_SPECIFIED
        service_instance_id = fip.get('service_instance_id') 
        if subnet_id:
            fixed_ips = [{'subnet_id': subnet_id}]
        if fip_address:
            fixed_ips = [{'ip_address': fip_address}]

        with context.session.begin(subtransactions=True):
            # This external port is never exposed to the tenant.
            # it is used purely for internal system and admin use when
            # managing floating IPs.
            external_port = self._core_plugin.create_port(context.elevated(), {
                'port':
                {'tenant_id': '',  # tenant intentionally not set
                 'network_id': f_net_id,
                 'mac_address': attributes.ATTR_NOT_SPECIFIED,
                 'fixed_ips': fixed_ips,
                 'admin_state_up': True,
                 'device_id': fip_id,
                 'device_owner': DEVICE_OWNER_FLOATINGIP,
                 uosfloatingip.UOS_SERVICE_PROVIDER: service_provider,
                 'name': ''}})
            # Ensure IP addresses are allocated on external port
            if not external_port['fixed_ips']:
                raise n_exc.ExternalIpAddressExhausted(net_id=f_net_id)

            floating_fixed_ip = external_port['fixed_ips'][0]
            floating_ip_address = floating_fixed_ip['ip_address']
            floating_subnet_id = floating_fixed_ip['subnet_id']
            floatingip_db = FloatingIP(
                id=fip_id,
                tenant_id=tenant_id,
                status=initial_status,
                floating_network_id=fip['floating_network_id'],
                floating_ip_address=floating_ip_address,
                floating_port_id=external_port['id'],
                floating_subnet_id=floating_subnet_id,
                uos_service_provider = service_provider,
                uos_name=fip.get(uosfloatingip.UOS_NAME),
                service_instance_id = service_instance_id,
                uos_registerno=fip.get(uosfloatingip.UOS_REGISTERNO))
            utils.make_default_name(floatingip_db,
                   uos_l3_constants.UOS_PRE_FIP, name='uos_name')
            fip['tenant_id'] = tenant_id
            # Update association with internal port
            # and define external IP address
            self._update_fip_assoc(context, fip,
                                   floatingip_db, external_port)
            context.session.add(floatingip_db)

            LOG.info(_("tenant %s create floatingip %s successful"),
                     tenant_id, floating_ip_address)
        return self._make_floatingip_dict(floatingip_db)

    def _update_floatingip(self, context, id, floatingip):
        fip = floatingip['floatingip']
        # NOTE(gongysh) for the purpose update the floatingip with
        # data return floatingip created just
        fip.pop('created_at', None)
        with context.session.begin(subtransactions=True):
            floatingip_db = self._get_floatingip(context, id)
            old_floatingip = self._make_floatingip_dict(floatingip_db)
            fip['tenant_id'] = floatingip_db['tenant_id']
            fip['id'] = id
            fip_port_id = floatingip_db['floating_port_id']
            self._update_fip_assoc(context, fip, floatingip_db,
                                   self._core_plugin.get_port(
                                       context.elevated(), fip_port_id))
        return old_floatingip, self._make_floatingip_dict(floatingip_db)

    def _floatingips_to_router_ids(self, floatingips):
        return list(set([floatingip['router_id']
                         for floatingip in floatingips
                         if floatingip['router_id']]))

    def update_floatingip(self, context, id, floatingip):
        _old_floatingip, floatingip = self._update_floatingip(
            context, id, floatingip)
        return floatingip

    def update_floatingip_status(self, context, floatingip_id, status):
        """Update operational status for floating IP in neutron DB."""
        fip_query = self._model_query(context, FloatingIP).filter(
            FloatingIP.id == floatingip_id)
        fip_query.update({'status': status}, synchronize_session=False)

    def _delete_floatingip(self, context, id):
        floatingip = self._get_floatingip(context, id)
        router_id = floatingip['router_id']
        with context.session.begin(subtransactions=True):
            context.session.delete(floatingip)
            self._core_plugin.delete_port(context.elevated(),
                                          floatingip['floating_port_id'],
                                          l3_port_check=False)
        LOG.info(_("tenant %s delete floatingip %s successful"),
                 floatingip['tenant_id'],floatingip['floating_ip_address'])
        return router_id

    def delete_floatingip(self, context, id):
        self._delete_floatingip(context, id)

    def get_floatingip(self, context, id, fields=None):
        floatingip = self._get_floatingip(context, id)
        return self._make_floatingip_dict(floatingip, fields)

    def get_floatingips(self, context, filters=None, fields=None,
                        sorts=None, limit=None, marker=None,
                        page_reverse=False):
        marker_obj = self._get_marker_obj(context, 'floatingip', limit,
                                          marker)
        if filters is not None:
            for key, val in API_TO_DB_COLUMN_MAP.iteritems():
                if key in filters:
                    filters[val] = filters.pop(key)
        uos_ext = filters.pop('uos_ext', [])
        _ips = self._get_collection(context, FloatingIP,
                                    self._make_floatingip_dict,
                                    filters=filters, fields=fields,
                                    sorts=sorts,
                                    limit=limit,
                                    marker_obj=marker_obj,
                                    page_reverse=page_reverse)
        if not uos_ext or not _ips:
            return _ips
        return self._uos_get_floatingips(context, _ips)

    def delete_disassociated_floatingips(self, context, network_id):
        query = self._model_query(context, FloatingIP)
        query = query.filter_by(floating_network_id=network_id,
                                fixed_port_id=None,
                                router_id=None)
        for fip in query:
            self.delete_floatingip(context, fip.id)

    def get_floatingips_count(self, context, filters=None):
        return self._get_collection_count(context, FloatingIP,
                                          filters=filters)

    def prevent_l3_port_deletion(self, context, port_id):
        """Checks to make sure a port is allowed to be deleted.

        Raises an exception if this is not the case.  This should be called by
        any plugin when the API requests the deletion of a port, since some
        ports for L3 are not intended to be deleted directly via a DELETE
        to /ports, but rather via other API calls that perform the proper
        deletion checks.
        """
        port_db = self._core_plugin._get_port(context, port_id)
        if port_db['device_owner'] in self.router_device_owners:
            # Raise port in use only if the port has IP addresses
            # Otherwise it's a stale port that can be removed
            fixed_ips = port_db['fixed_ips']
            if fixed_ips:
                raise l3.L3PortInUse(port_id=port_id,
                                     device_owner=port_db['device_owner'])
            else:
                LOG.debug(_("Port %(port_id)s has owner %(port_owner)s, but "
                            "no IP address, so it can be deleted"),
                          {'port_id': port_db['id'],
                           'port_owner': port_db['device_owner']})

    def disassociate_floatingips(self, context, port_id):
        """Disassociate all floating IPs linked to specific port.

        @param port_id: ID of the port to disassociate floating IPs.
        @param do_notify: whether we should notify routers right away.
        @return: set of router-ids that require notification updates
                 if do_notify is False, otherwise None.
        """
        router_ids = set()

        with context.session.begin(subtransactions=True):
            fip_qry = context.session.query(FloatingIP)
            floating_ips = fip_qry.filter_by(fixed_port_id=port_id)
            for floating_ip in floating_ips:
                router_ids.add(floating_ip['router_id'])
                floating_ip.update({'fixed_port_id': None,
                                    'fixed_ip_address': None,
                                    'router_id': None})
        return router_ids

    def _build_routers_list(self, context, routers, gw_ports):
        for router in routers:
            gw_port_id = router['gw_port_id']
            # Collect gw ports only if available
            if gw_port_id and gw_ports.get(gw_port_id):
                router['gw_port'] = gw_ports[gw_port_id]
        return routers

    def _get_sync_routers(self, context, router_ids=None, active=None):
        """Query routers and their gw ports for l3 agent.

        Query routers with the router_ids. The gateway ports, if any,
        will be queried too.
        l3 agent has an option to deal with only one router id. In addition,
        when we need to notify the agent the data about only one router
        (when modification of router, its interfaces, gw_port and floatingips),
        we will have router_ids.
        @param router_ids: the list of router ids which we want to query.
                           if it is None, all of routers will be queried.
        @return: a list of dicted routers with dicted gw_port populated if any
        """
        filters = {'id': router_ids} if router_ids else {}
        if active is not None:
            filters['admin_state_up'] = [active]
        router_dicts = self.get_routers(context, filters=filters)
        gw_port_ids = []
        if not router_dicts:
            return []
        for router_dict in router_dicts:
            gw_port_id = router_dict['gw_port_id']
            if gw_port_id:
                gw_port_ids.append(gw_port_id)
        gw_ports = []
        if gw_port_ids:
            gw_ports = dict((gw_port['id'], gw_port)
                            for gw_port in
                            self.get_sync_gw_ports(context, gw_port_ids))
        # NOTE(armando-migliaccio): between get_routers and get_sync_gw_ports
        # gw ports may get deleted, which means that router_dicts may contain
        # ports that gw_ports does not; we should rebuild router_dicts, but
        # letting the callee check for missing gw_ports sounds like a good
        # defensive approach regardless
        return self._build_routers_list(context, router_dicts, gw_ports)

    def _get_sync_floating_ips(self, context, router_ids):
        """Query floating_ips that relate to list of router_ids."""
        if not router_ids:
            return []
        return self.get_floatingips(context, {'router_id': router_ids})

    def get_sync_gw_ports(self, context, gw_port_ids):
        if not gw_port_ids:
            return []
        filters = {'id': gw_port_ids}
        gw_ports = self._core_plugin.get_ports(context, filters)
        if gw_ports:
            self._populate_subnet_for_ports(context, gw_ports)
        return gw_ports

    def get_sync_interfaces(self, context, router_ids, device_owners=None):
        """Query router interfaces that relate to list of router_ids."""
        device_owners = device_owners or [DEVICE_OWNER_ROUTER_INTF]
        if not router_ids:
            return []
        filters = {'device_id': router_ids,
                   'device_owner': device_owners}
        interfaces = self._core_plugin.get_ports(context, filters)
        if interfaces:
            self._populate_subnet_for_ports(context, interfaces)
        return interfaces

    def _populate_subnet_for_ports(self, context, ports):
        """Populate ports with subnet.

        These ports already have fixed_ips populated.
        """
        if not ports:
            return

        def each_port_with_ip():
            for port in ports:
                fixed_ips = port.get('fixed_ips', [])
                if len(fixed_ips) > 1:
                    LOG.info(_("Ignoring multiple IPs on router port %s"),
                             port['id'])
                    continue
                elif not fixed_ips:
                    # Skip ports without IPs, which can occur if a subnet
                    # attached to a router is deleted
                    LOG.info(_("Skipping port %s as no IP is configure on it"),
                             port['id'])
                    continue
                yield (port, fixed_ips[0])

        network_ids = set(p['network_id'] for p, _ in each_port_with_ip())
        filters = {'network_id': [id for id in network_ids]}
        fields = ['id', 'cidr', 'gateway_ip',
                  'network_id', 'ipv6_ra_mode', 'name']

        subnets_by_network = dict((id, []) for id in network_ids)
        for subnet in self._core_plugin.get_subnets(context, filters, fields):
            subnets_by_network[subnet['network_id']].append(subnet)

        for port, fixed_ip in each_port_with_ip():
            port['extra_subnets'] = []
            for subnet in subnets_by_network[port['network_id']]:
                subnet_info = {'id': subnet['id'],
                               'cidr': subnet['cidr'],
                               'gateway_ip': subnet['gateway_ip'],
                               'ipv6_ra_mode': subnet['ipv6_ra_mode']}

                if subnet['id'] == fixed_ip['subnet_id']:
                    port['subnet'] = subnet_info
                else:
                    shadow_subnet = cfg.CONF.unitedstack.external_shadow_subnet
                    if subnet['name'] == shadow_subnet:
                        continue
                    port['extra_subnets'].append(subnet_info)

    def _process_floating_ips(self, context, routers_dict, floating_ips):
        for floating_ip in floating_ips:
            router = routers_dict.get(floating_ip['router_id'])
            if router:
                router_floatingips = router.get(l3_constants.FLOATINGIP_KEY,
                                                [])
                router_floatingips.append(floating_ip)
                router[l3_constants.FLOATINGIP_KEY] = router_floatingips

    def _process_interfaces(self, routers_dict, interfaces):
        for interface in interfaces:
            router = routers_dict.get(interface['device_id'])
            if router:
                router_interfaces = router.get(l3_constants.INTERFACE_KEY, [])
                router_interfaces.append(interface)
                router[l3_constants.INTERFACE_KEY] = router_interfaces

    def _get_router_info_list(self, context, router_ids=None, active=None,
                              device_owners=None):
        """Query routers and their related floating_ips, interfaces."""
        with context.session.begin(subtransactions=True):
            routers = self._get_sync_routers(context,
                                             router_ids=router_ids,
                                             active=active)
            router_ids = [router['id'] for router in routers]
            interfaces = self.get_sync_interfaces(
                context, router_ids, device_owners)
            floating_ips = self._get_sync_floating_ips(context, router_ids)
            return (routers, interfaces, floating_ips)

    def get_sync_data(self, context, router_ids=None, active=None):
        routers, interfaces, floating_ips = self._get_router_info_list(
            context, router_ids=router_ids, active=active)
        routers_dict = dict((router['id'], router) for router in routers)
        self._process_floating_ips(context, routers_dict, floating_ips)
        self._process_interfaces(routers_dict, interfaces)
        return routers_dict.values()


class L3RpcNotifierMixin(object):
    """Mixin class to add rpc notifier attribute to db_base_plugin_v2."""

    @property
    def l3_rpc_notifier(self):
        if not hasattr(self, '_l3_rpc_notifier'):
            self._l3_rpc_notifier = l3_rpc_agent_api.L3AgentNotifyAPI()
        return self._l3_rpc_notifier

    @l3_rpc_notifier.setter
    def l3_rpc_notifier(self, value):
        self._l3_rpc_notifier = value

    def notify_router_updated(self, context, router_id,
                              operation=None, data=None):
        if router_id:
            self.l3_rpc_notifier.routers_updated(
                context, [router_id], operation, data)

    def notify_routers_updated(self, context, router_ids,
                               operation=None, data=None):
        if router_ids:
            self.l3_rpc_notifier.routers_updated(
                context, router_ids, operation, data)

    def notify_router_deleted(self, context, router_id):
        self.l3_rpc_notifier.router_deleted(context, router_id)


class L3_NAT_db_mixin(L3_NAT_dbonly_mixin, L3RpcNotifierMixin):
    """Mixin class to add rpc notifier methods to db_base_plugin_v2."""

    def update_router(self, context, id, router):
        r = router['router']
        payload = {'gw_exists':
                   r.get(EXTERNAL_GW_INFO, attributes.ATTR_NOT_SPECIFIED) !=
                   attributes.ATTR_NOT_SPECIFIED}
        router_dict = super(L3_NAT_db_mixin, self).update_router(context,
                                                                 id, router)
        self.notify_router_updated(context, router_dict['id'], None, payload)
        return router_dict

    def delete_router(self, context, id):
        super(L3_NAT_db_mixin, self).delete_router(context, id)
        self.notify_router_deleted(context, id)

    def notify_router_interface_action(
            self, context, router_interface_info, action):
        l3_method = '%s_router_interface' % action
        super(L3_NAT_db_mixin, self).notify_routers_updated(
            context, [router_interface_info['id']], l3_method,
            {'subnet_id': router_interface_info['subnet_id']})

        mapping = {'add': 'create', 'remove': 'delete'}
        notifier = n_rpc.get_notifier('network')
        router_event = 'router.interface.%s' % mapping[action]
        notifier.info(context, router_event,
                      {'router_interface': router_interface_info})

    def add_router_interface(self, context, router_id, interface_info):
        router_interface_info = super(
            L3_NAT_db_mixin, self).add_router_interface(
                context, router_id, interface_info)
        self.notify_router_interface_action(
            context, router_interface_info, 'add')
        return router_interface_info

    def remove_router_interface(self, context, router_id, interface_info):
        router_interface_info = super(
            L3_NAT_db_mixin, self).remove_router_interface(
                context, router_id, interface_info)
        self.notify_router_interface_action(
            context, router_interface_info, 'remove')
        return router_interface_info

    def create_floatingip(self, context, floatingip,
            initial_status=l3_constants.FLOATINGIP_STATUS_ACTIVE):
        floatingip_dict = super(L3_NAT_db_mixin, self).create_floatingip(
            context, floatingip, initial_status)
        router_id = floatingip_dict['router_id']
        self.notify_router_updated(context, router_id, 'create_floatingip', {})
        return floatingip_dict

    def update_floatingip(self, context, id, floatingip):
        if ('port_id' in floatingip['floatingip'] and floatingip.get(
                'floatingip').get('port_id') == None) or len(
                floatingip['floatingip']) == 0:
            tunnels = manager.NeutronManager.get_service_plugins().get(
                constants.TUNNEL)
            if tunnels:
                tunnels.check_floatingip_in_use(context, id)
        old_floatingip, floatingip = self._update_floatingip(
            context, id, floatingip)
        router_ids = self._floatingips_to_router_ids(
            [old_floatingip, floatingip])
        super(L3_NAT_db_mixin, self).notify_routers_updated(
            context, router_ids, 'update_floatingip', {})

        return floatingip

    def delete_floatingip(self, context, id):
        router_id = self._delete_floatingip(context, id)
        self.notify_router_updated(context, router_id, 'delete_floatingip', {})

    def disassociate_floatingips(self, context, port_id, do_notify=True):
        """Disassociate all floating IPs linked to specific port.

        @param port_id: ID of the port to disassociate floating IPs.
        @param do_notify: whether we should notify routers right away.
        @return: set of router-ids that require notification updates
                 if do_notify is False, otherwise None.
        """
        router_ids = super(L3_NAT_db_mixin, self).disassociate_floatingips(
            context, port_id)
        if do_notify:
            self.notify_routers_updated(context, router_ids)
            # since caller assumes that we handled notifications on its
            # behalf, return nothing
            return

        return router_ids

    def notify_routers_updated(self, context, router_ids):
        super(L3_NAT_db_mixin, self).notify_routers_updated(
            context, list(router_ids), 'disassociate_floatingips', {})

#    # hxn add for servicevm
#    def get_sync_internal_interfaces(self, context, service_lists):
#        """Query router interfaces that relate to list of router_ids."""
#        interfaces = self._core_plugin.get_internal_ports(context, service_lists)
#        if interfaces:
#            self._populate_subnet_for_ports(context, interfaces)
#        return interfaces
#
#    def get_sync_svm_ports(self, context, device_ids, service_type):
#        if not device_ids:
#            return []
#
#        filters = {'service_instance_id': device_ids,
#                   'servicevm_type': service_type } if device_ids else {}
#        ports = self._core_plugin.get_ports(context, filters)
#        if ports:
#            self._populate_subnet_for_ports(context, ports)
#        return ports
#
#    def _build_services_list(self, context, service_lists, gw_ports):
#        for s in service_lists:
#            device_id = s['devices']['id']
#            # Collect gw ports only if available
#            if device_id and gw_ports.get(device_id):
#                s[l3_constants.GW_INTERFACE_KEY] = gw_ports[device_id]
#        return service_lists
#
#    def get_svm_gw_ports(self, context, service_lists):
#        device_ids = [s['devices']['id'] for s in service_lists]
#        servicevm_type = l3_constants.SERVICEVM_OWNER_ROUTER_GW
#        gw_ports = dict((gw_port['service_instance_id'], gw_port)
#                         for gw_port in
#                         self.get_sync_svm_ports(context, device_ids,
#                                                    servicevm_type))
#        return self._build_services_list(context, service_lists, gw_ports)
#
#    def get_svm_internal_ports(self, context, service_lists):
#        service_dicts = [s['devices']['id'] for s in service_lists]
#        servicevm_type = l3_constants.SERVICEVM_OWNER_ROUTER_INTF
#        interfaces = self.get_sync_svm_ports(context, service_dicts.keys(),
#                                             servicevm_type)
#        for interface in interfaces:
#            service = service_dicts.get(interface['service_instance_id'])
#            if service:
#                internal_interfaces = service.get(l3_constants.INTERFACE_KEY, [])
#                internal_interfaces.append(interface)
#                service[l3_constants.INTERFACE_KEY] = internal_interfaces
#        return service_lists
