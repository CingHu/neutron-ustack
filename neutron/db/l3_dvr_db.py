# Copyright (c) 2014 OpenStack Foundation.  All rights reserved.
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

from oslo.config import cfg

from neutron.api.v2 import attributes
from neutron.common import constants as l3_const
from neutron.common import exceptions as n_exc
from neutron.common import utils as n_utils
from neutron.db import l3_attrs_db
from neutron.db import l3_db
from neutron.db import l3_dvrscheduler_db as l3_dvrsched_db
from neutron.db import models_v2
from neutron.extensions import floatingip_ratelimits as fip_rate
from neutron.extensions import l3
from neutron.extensions import portbindings
from neutron.openstack.common import log as logging
from neutron import manager
from neutron.plugins.common import constants


LOG = logging.getLogger(__name__)

DEVICE_OWNER_DVR_INTERFACE = l3_const.DEVICE_OWNER_DVR_INTERFACE
DEVICE_OWNER_DVR_SNAT = l3_const.DEVICE_OWNER_ROUTER_SNAT
FLOATINGIP_AGENT_INTF_KEY = l3_const.FLOATINGIP_AGENT_INTF_KEY
DEVICE_OWNER_AGENT_GW = l3_const.DEVICE_OWNER_AGENT_GW
SNAT_ROUTER_INTF_KEY = l3_const.SNAT_ROUTER_INTF_KEY


router_distributed_opts = [
    cfg.BoolOpt('router_distributed',
                default=False,
                help=_("System-wide flag to determine the type of router "
                       "that tenants can create. Only admin can override.")),
]
cfg.CONF.register_opts(router_distributed_opts)


class L3_NAT_with_dvr_db_mixin(l3_db.L3_NAT_db_mixin,
                               l3_attrs_db.ExtraAttributesMixin):
    """Mixin class to enable DVR support."""

    router_device_owners = (
        l3_db.L3_NAT_db_mixin.router_device_owners +
        (DEVICE_OWNER_DVR_INTERFACE,))

    extra_attributes = (
        l3_attrs_db.ExtraAttributesMixin.extra_attributes + [{
            'name': "distributed",
            'default': cfg.CONF.router_distributed
        }])

    def _create_router_db(self, context, router, tenant_id):
        """Create a router db object with dvr additions."""
        router['distributed'] = _is_distributed_router(router)
        with context.session.begin(subtransactions=True):
            router_db = super(
                L3_NAT_with_dvr_db_mixin, self)._create_router_db(
                    context, router, tenant_id)
            self._process_extra_attr_router_create(context, router_db, router)
            return router_db

    def _validate_router_migration(self, router_db, router_res):
        """Allow centralized -> distributed state transition only."""
        if (router_db.extra_attributes.distributed and
            router_res.get('distributed') is False):
            LOG.info(_("Centralizing distributed router %s "
                       "is not supported"), router_db['id'])
            raise NotImplementedError()

    def _update_distributed_attr(
        self, context, router_id, router_db, data, gw_info):
        """Update the model to support the dvr case of a router."""
        if not attributes.is_attr_set(gw_info) and data.get('distributed'):
            admin_ctx = context.elevated()
            filters = {'device_id': [router_id],
                       'device_owner': [l3_const.DEVICE_OWNER_ROUTER_INTF]}
            ports = self._core_plugin.get_ports(admin_ctx, filters=filters)
            for p in ports:
                port_db = self._core_plugin._get_port(admin_ctx, p['id'])
                port_db.update({'device_owner': DEVICE_OWNER_DVR_INTERFACE})

    def _update_router_db(self, context, router_id, data, gw_info):
        with context.session.begin(subtransactions=True):
            router_db = super(
                L3_NAT_with_dvr_db_mixin, self)._update_router_db(
                    context, router_id, data, gw_info)
            self._validate_router_migration(router_db, data)
            # FIXME(swami): need to add migration status so that the scheduler
            # can pick the migration request and move stuff over. For now
            # only the distributed flag and router interface's owner change.
            # Instead of complaining on _validate_router_migration, let's
            # succeed here and complete the task in a follow-up patch
            router_db.extra_attributes.update(data)
            self._update_distributed_attr(
                context, router_id, router_db, data, gw_info)
            return router_db

    def _delete_current_gw_port(self, context, router_id, router, new_network):
        super(L3_NAT_with_dvr_db_mixin,
              self)._delete_current_gw_port(context, router_id,
                                            router, new_network)
        if router.extra_attributes.distributed:
            self.delete_csnat_router_interface_ports(
                context.elevated(), router)

    def _create_gw_port(self, context, router_id, router, new_network):
        super(L3_NAT_with_dvr_db_mixin,
              self)._create_gw_port(context, router_id,
                                    router, new_network)
        if router.extra_attributes.distributed and router.gw_port:
            snat_p_list = self.create_snat_intf_ports_if_not_exists(
                context.elevated(), router['id'])
            if not snat_p_list:
                LOG.debug("SNAT interface ports not created: %s", snat_p_list)

    def _get_device_owner(self, context, router=None):
        """Get device_owner for the specified router."""
        router_is_uuid = isinstance(router, basestring)
        if router_is_uuid:
            router = self._get_router(context, router)
        if _is_distributed_router(router):
            return DEVICE_OWNER_DVR_INTERFACE
        return super(L3_NAT_with_dvr_db_mixin,
                     self)._get_device_owner(context, router)

    def _get_interface_ports_for_network(self, context, network_id):
        router_intf_qry = (context.session.query(models_v2.Port).
                           filter_by(network_id=network_id))
        return (router_intf_qry.
                filter(models_v2.Port.device_owner.in_(
                       [l3_const.DEVICE_OWNER_ROUTER_INTF,
                        DEVICE_OWNER_DVR_INTERFACE])))

    def _update_fip_assoc(self, context, fip, floatingip_db, external_port):
        previous_router_id = floatingip_db.router_id
        port_id, internal_ip_address, router_id = (
            self._check_and_get_fip_assoc(context, fip, floatingip_db))
        admin_ctx = context.elevated()
        if (not ('port_id' in fip and fip['port_id'])) and (
            floatingip_db['fixed_port_id'] is not None):
            self.clear_unused_fip_agent_gw_port(
                admin_ctx, floatingip_db, fip['id'])
        floatingip_db.update({'fixed_ip_address': internal_ip_address,
                              'fixed_port_id': port_id,
                              'router_id': router_id,
                              'last_known_router_id': previous_router_id})
        if fip_rate.RATE_LIMIT in fip:
            floatingip_db[fip_rate.RATE_LIMIT] = fip[fip_rate.RATE_LIMIT]
        try:
            if port_id is not None:
                external_port = self._core_plugin.get_port(context, port_id)
                if (('device_owner' in external_port) and (
                    external_port['device_owner'] == l3_const.DEVICE_OWNER_ROUTER_GW)):
                     vpnservice = manager.NeutronManager.get_service_plugins().get(
                                      constants.VPN)
                     if vpnservice and getattr(vpnservice,'notify_router_update'):
                         vpnservice.notify_router_update(context, [router_id],
                                 update_driver=True)
        except:
            LOG.error('vpn service update failed')


    def clear_unused_fip_agent_gw_port(
            self, context, floatingip_db, fip_id):
        """Helper function to check for fip agent gw port and delete.

        This function checks on compute nodes to make sure if there
        are any VMs using the FIP agent gateway port. If no VMs are
        using the FIP agent gateway port, it will go ahead and delete
        the FIP agent gateway port. If even a single VM is using the
        port it will not delete.
        """
        fip_hostid = self.get_vm_port_hostid(
            context, floatingip_db['fixed_port_id'])
        if fip_hostid and self.check_fips_availability_on_host(
            context, fip_id, fip_hostid):
            LOG.debug('Deleting the Agent GW Port on host: %s', fip_hostid)
            self.delete_floatingip_agent_gateway_port(context, fip_hostid)

    def delete_floatingip(self, context, id):
        floatingip = self._get_floatingip(context, id)
        if floatingip['fixed_port_id']:
            admin_ctx = context.elevated()
            self.clear_unused_fip_agent_gw_port(
                admin_ctx, floatingip, id)
        super(L3_NAT_with_dvr_db_mixin,
              self).delete_floatingip(context, id)

    def add_router_interface(self, context, router_id, interface_info):
        add_by_port, add_by_sub = self._validate_interface_info(interface_info)
        router = self._get_router(context, router_id)
        device_owner = self._get_device_owner(context, router)

        if add_by_port:
            port = self._add_interface_by_port(
                context, router_id, interface_info['port_id'], device_owner)
        elif add_by_sub:
            port = self._add_interface_by_subnet(
                context, router_id, interface_info['subnet_id'], device_owner)

        if router.extra_attributes.distributed and router.gw_port:
            self.add_csnat_router_interface_port(
                context.elevated(), router_id, port['network_id'],
                port['fixed_ips'][0]['subnet_id'])

        router_interface_info = self._make_router_interface_info(
            router_id, port['tenant_id'], port['id'],
            port['fixed_ips'][0]['subnet_id'])

        self.notify_router_interface_action(
            context, router_interface_info, 'add')
        try:
            vpnservice = manager.NeutronManager.get_service_plugins().get(
                       constants.VPN)
            if vpnservice and getattr(vpnservice, 'notify_router_update'):
                vpnservice.notify_router_update(context, [router_id])
        except:
            LOG.error('vpn service update failed')
            pass

        return router_interface_info

    def remove_router_interface(self, context, router_id, interface_info):
        if not interface_info:
            msg = _("Either subnet_id or port_id must be specified")
            raise n_exc.BadRequest(resource='router', msg=msg)

        port_id = interface_info.get('port_id')
        subnet_id = interface_info.get('subnet_id')
        router = self._get_router(context, router_id)
        device_owner = self._get_device_owner(context, router)

        if port_id:
            port, subnet = self._remove_interface_by_port(
                context, router_id, port_id, subnet_id, device_owner)
        elif subnet_id:
            port, subnet = self._remove_interface_by_subnet(
                context, router_id, subnet_id, device_owner)

        if router.extra_attributes.distributed and router.gw_port:
            self.delete_csnat_router_interface_ports(
                context.elevated(), router, subnet_id=subnet_id)

        router_interface_info = self._make_router_interface_info(
            router_id, port['tenant_id'], port['id'],
            port['fixed_ips'][0]['subnet_id'])
        self.notify_router_interface_action(
            context, router_interface_info, 'remove')
        try:
            vpnservice = manager.NeutronManager.get_service_plugins().get(
                       constants.VPN)
            if vpnservice and getattr(vpnservice, 'notify_router_update'):
                vpnservice.notify_router_update(context, [router_id])
        except:
            LOG.error('vpn service update failed')
            pass

        return router_interface_info

    def get_snat_sync_interfaces(self, context, router_ids):
        """Query router interfaces that relate to list of router_ids."""
        if not router_ids:
            return []
        filters = {'device_id': router_ids,
                   'device_owner': [DEVICE_OWNER_DVR_SNAT]}
        interfaces = self._core_plugin.get_ports(context, filters)
        LOG.debug("Return the SNAT ports: %s", interfaces)
        if interfaces:
            self._populate_subnet_for_ports(context, interfaces)
        return interfaces

    def _build_routers_list(self, context, routers, gw_ports):
        # Perform a single query up front for all routers
        router_ids = [r['id'] for r in routers]
        snat_binding = l3_dvrsched_db.CentralizedSnatL3AgentBinding
        query = (context.session.query(snat_binding).
                 filter(snat_binding.router_id.in_(router_ids))).all()
        bindings = dict((b.router_id, b) for b in query)

        for rtr in routers:
            gw_port_id = rtr['gw_port_id']
            # Collect gw ports only if available
            if gw_port_id and gw_ports.get(gw_port_id):
                rtr['gw_port'] = gw_ports[gw_port_id]
                if 'enable_snat' in rtr[l3.EXTERNAL_GW_INFO]:
                    rtr['enable_snat'] = (
                        rtr[l3.EXTERNAL_GW_INFO]['enable_snat'])

                binding = bindings.get(rtr['id'])
                if not binding:
                    rtr['gw_port_host'] = None
                    LOG.debug('No snat is bound to router %s', rtr['id'])
                    continue

                rtr['gw_port_host'] = binding.l3_agent.host

        return routers

    def _process_routers(self, context, routers):
        routers_dict = {}
        for router in routers:
            routers_dict[router['id']] = router
            router_ids = [router['id']]
            if router['gw_port_id']:
                snat_router_intfs = self.get_snat_sync_interfaces(context,
                                                                  router_ids)
                LOG.debug("SNAT ports returned: %s ", snat_router_intfs)
                router[SNAT_ROUTER_INTF_KEY] = snat_router_intfs
        return routers_dict

    def _uos_process_floating_ips(self, context, routers_dict, floating_ips):
        for floating_ip in floating_ips:
            router = routers_dict.get(floating_ip['router_id'])
            if router:
                router_floatingips = router.get(l3_const.UOS_GW_FIP_KEY, [])
                router_floatingips.append(floating_ip)
                router[l3_const.UOS_GW_FIP_KEY] = router_floatingips

    def _process_floating_ips(self, context, routers_dict, floating_ips):
        for floating_ip in floating_ips:
            router = routers_dict.get(floating_ip['router_id'])
            if router:
                router_floatingips = router.get(l3_const.FLOATINGIP_KEY, [])
                floatingip_agent_intfs = []
                if router['distributed']:
                    floating_ip['host'] = self.get_vm_port_hostid(
                        context, floating_ip['port_id'])
                    LOG.debug("Floating IP host: %s", floating_ip['host'])
                    fip_agent = self._get_agent_by_type_and_host(
                        context, l3_const.AGENT_TYPE_L3,
                        floating_ip['host'])
                    LOG.debug("FIP Agent : %s ", fip_agent['id'])
                    floatingip_agent_intfs = self.get_fip_sync_interfaces(
                        context, fip_agent['id'])
                    LOG.debug("FIP Agent ports: %s", floatingip_agent_intfs)
                router_floatingips.append(floating_ip)
                router[l3_const.FLOATINGIP_KEY] = router_floatingips
                router[l3_const.FLOATINGIP_AGENT_INTF_KEY] = (
                    floatingip_agent_intfs)
                self._uos_process_router_gateways(context, router,
                                                  router_floatingips)

    def get_fip_sync_interfaces(self, context, fip_agent_id):
        """Query router interfaces that relate to list of router_ids."""
        if not fip_agent_id:
            return []
        filters = {'device_id': [fip_agent_id],
                   'device_owner': [DEVICE_OWNER_AGENT_GW]}
        interfaces = self._core_plugin.get_ports(context.elevated(), filters)
        LOG.debug("Return the FIP ports: %s ", interfaces)
        if interfaces:
            self._populate_subnet_for_ports(context, interfaces)
        return interfaces

    def get_sync_data(self, context, router_ids=None, active=None):
        routers, interfaces, floating_ips = self._get_router_info_list(
            context, router_ids=router_ids, active=active,
            device_owners=[l3_const.DEVICE_OWNER_ROUTER_INTF,
                           DEVICE_OWNER_DVR_INTERFACE])
        # Add the port binding host to the floatingip dictionary
        for fip in floating_ips:
            fip['host'] = self.get_vm_port_hostid(context, fip['port_id'])
        routers_dict = self._process_routers(context, routers)
        floating_ips, gw_fips = self._uos_process_fip_sync(context, routers_dict,
                                                  floating_ips)
        self._uos_process_floating_ips(context, routers_dict, gw_fips)
        self._process_floating_ips(context, routers_dict, floating_ips)
        self._process_interfaces(routers_dict, interfaces)
        return routers_dict.values()

    def get_vm_port_hostid(self, context, port_id, port=None):
        """Return the portbinding host_id."""
        vm_port_db = port or self._core_plugin.get_port(context, port_id)
        device_owner = vm_port_db['device_owner'] if vm_port_db else ""
        if (n_utils.is_dvr_serviced(device_owner) or
            device_owner == DEVICE_OWNER_AGENT_GW):
            return vm_port_db[portbindings.HOST_ID]

    def get_agent_gw_ports_exist_for_network(
            self, context, network_id, host, agent_id):
        """Return agent gw port if exist, or None otherwise."""
        if not network_id:
            LOG.debug("Network not specified")
            return

        filters = {
            'network_id': [network_id],
            'device_id': [agent_id],
            'device_owner': [DEVICE_OWNER_AGENT_GW]
        }
        ports = self._core_plugin.get_ports(context, filters)
        if ports:
            return ports[0]

    def check_fips_availability_on_host(self, context, fip_id, host_id):
        """Query all floating_ips and filter by particular host."""
        fip_count_on_host = 0
        with context.session.begin(subtransactions=True):
            routers = self._get_sync_routers(context, router_ids=None)
            router_ids = [router['id'] for router in routers]
            floating_ips = self._get_sync_floating_ips(context, router_ids)
            # Check for the active floatingip in the host
            for fip in floating_ips:
                f_host = self.get_vm_port_hostid(context, fip['port_id'])
                if f_host == host_id:
                    fip_count_on_host += 1
            # If fip_count greater than 1 or equal to zero no action taken
            # if the fip_count is equal to 1, then this would be last active
            # fip in the host, so the agent gateway port can be deleted.
            if fip_count_on_host == 1:
                return True
            return False

    def delete_floatingip_agent_gateway_port(self, context, host_id):
        """Function to delete the FIP agent gateway port on host."""
        # delete any fip agent gw port
        device_filter = {'device_owner': [DEVICE_OWNER_AGENT_GW]}
        ports = self._core_plugin.get_ports(context,
                                            filters=device_filter)
        for p in ports:
            if self.get_vm_port_hostid(context, p['id'], p) == host_id:
                self._core_plugin._delete_port(context, p['id'])
                return

    def create_fip_agent_gw_port_if_not_exists(
        self, context, network_id, host):
        """Function to return the FIP Agent GW port.

        This function will create a FIP Agent GW port
        if required. If the port already exists, it
        will return the existing port and will not
        create a new one.
        """
        l3_agent_db = self._get_agent_by_type_and_host(
            context, l3_const.AGENT_TYPE_L3, host)
        if l3_agent_db:
            LOG.debug("Agent ID exists: %s", l3_agent_db['id'])
            f_port = self.get_agent_gw_ports_exist_for_network(
                context, network_id, host, l3_agent_db['id'])
            if not f_port:
                LOG.info(_('Agent Gateway port does not exist,'
                           ' so create one: %s'), f_port)
                agent_port = self._core_plugin.create_port(
                    context,
                    {'port': {'tenant_id': '',
                              'network_id': network_id,
                              'mac_address': attributes.ATTR_NOT_SPECIFIED,
                              'fixed_ips': attributes.ATTR_NOT_SPECIFIED,
                              'device_id': l3_agent_db['id'],
                              'device_owner': DEVICE_OWNER_AGENT_GW,
                              'admin_state_up': True,
                              'name': ''}})
                if agent_port:
                    self._populate_subnet_for_ports(context, [agent_port])
                    return agent_port
                msg = _("Unable to create the Agent Gateway Port")
                raise n_exc.BadRequest(resource='router', msg=msg)
            else:
                self._populate_subnet_for_ports(context, [f_port])
                return f_port

    def get_snat_interface_ports_for_router(self, context, router_id):
        """Return all existing snat_router_interface ports."""
        filters = {'device_id': [router_id],
                   'device_owner': [DEVICE_OWNER_DVR_SNAT]}
        return self._core_plugin.get_ports(context, filters)

    def add_csnat_router_interface_port(
            self, context, router_id, network_id, subnet_id, do_pop=True):
        """Add SNAT interface to the specified router and subnet."""
        snat_port = self._core_plugin.create_port(
            context,
            {'port': {'tenant_id': '',
                      'network_id': network_id,
                      'mac_address': attributes.ATTR_NOT_SPECIFIED,
                      'fixed_ips': [{'subnet_id': subnet_id}],
                      'device_id': router_id,
                      'device_owner': DEVICE_OWNER_DVR_SNAT,
                      'admin_state_up': True,
                      'name': ''}})
        if not snat_port:
            msg = _("Unable to create the SNAT Interface Port")
            raise n_exc.BadRequest(resource='router', msg=msg)
        elif do_pop:
            return self._populate_subnet_for_ports(context, [snat_port])
        return snat_port

    def create_snat_intf_ports_if_not_exists(
        self, context, router_id):
        """Function to return the snat interface port list.

        This function will return the snat interface port list
        if it exists. If the port does not exist it will create
        new ports and then return the list.
        """
        port_list = self.get_snat_interface_ports_for_router(
            context, router_id)
        if port_list:
            self._populate_subnet_for_ports(context, port_list)
            return port_list
        port_list = []
        filters = {
            'device_id': [router_id],
            'device_owner': [DEVICE_OWNER_DVR_INTERFACE]}
        int_ports = self._core_plugin.get_ports(context, filters)
        LOG.info(_('SNAT interface port list does not exist,'
                   ' so create one: %s'), port_list)
        for intf in int_ports:
            if intf.get('fixed_ips'):
                # Passing the subnet for the port to make sure the IP's
                # are assigned on the right subnet if multiple subnet
                # exists
                snat_port = self.add_csnat_router_interface_port(
                    context, router_id, intf['network_id'],
                    intf['fixed_ips'][0]['subnet_id'], do_pop=False)
                port_list.append(snat_port)
        if port_list:
            self._populate_subnet_for_ports(context, port_list)
        return port_list

    def dvr_vmarp_table_update(self, context, port_id, action):
        """Notify the L3 agent of VM ARP table changes.

        Provide the details of the VM ARP to the L3 agent when
        a Nova instance gets created or deleted.
        """
        port_dict = self._core_plugin._get_port(context, port_id)
        # Check this is a valid VM port
        if ("compute:" not in port_dict['device_owner'] or
            not port_dict['fixed_ips']):
            return
        ip_address = port_dict['fixed_ips'][0]['ip_address']
        subnet = port_dict['fixed_ips'][0]['subnet_id']
        filters = {'fixed_ips': {'subnet_id': [subnet]}}
        ports = self._core_plugin.get_ports(context, filters=filters)
        for port in ports:
            if port['device_owner'] == DEVICE_OWNER_DVR_INTERFACE:
                router_id = port['device_id']
                router_dict = self._get_router(context, router_id)
                if router_dict.extra_attributes.distributed:
                    arp_table = {'ip_address': ip_address,
                                 'mac_address': port_dict['mac_address'],
                                 'subnet_id': subnet}
                    if action == "add":
                        notify_action = self.l3_rpc_notifier.add_arp_entry
                    elif action == "del":
                        notify_action = self.l3_rpc_notifier.del_arp_entry
                    notify_action(context, router_id, arp_table)
                    return

    def delete_csnat_router_interface_ports(self, context,
                                            router, subnet_id=None):
        # Each csnat router interface port is associated
        # with a subnet, so we need to pass the subnet id to
        # delete the right ports.
        device_filter = {
            'device_id': [router['id']],
            'device_owner': [DEVICE_OWNER_DVR_SNAT]}
        c_snat_ports = self._core_plugin.get_ports(
            context, filters=device_filter)
        for p in c_snat_ports:
            if subnet_id is None:
                self._core_plugin.delete_port(context,
                                              p['id'],
                                              l3_port_check=False)
            else:
                if p['fixed_ips'][0]['subnet_id'] == subnet_id:
                    LOG.debug("Subnet matches: %s", subnet_id)
                    self._core_plugin.delete_port(context,
                                                  p['id'],
                                                  l3_port_check=False)


def _is_distributed_router(router):
    """Return True if router to be handled is distributed."""
    try:
        # See if router is a DB object first
        requested_router_type = router.extra_attributes.distributed
    except AttributeError:
        # if not, try to see if it is a request body
        requested_router_type = router.get('distributed')
    if attributes.is_attr_set(requested_router_type):
        return requested_router_type
    return cfg.CONF.router_distributed
