# Copyright (c) 2012 OpenStack Foundation.
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
import copy
import random

import netaddr
from oslo.config import cfg
from sqlalchemy import and_
from sqlalchemy import event
from sqlalchemy import orm
from sqlalchemy.orm import exc

from neutron.api.v2 import attributes
from neutron.common import constants
from neutron.common import exceptions as n_exc
from neutron.common import ipv6_utils
from neutron.common import uos_constants
from neutron.common import utils
from neutron import context as ctx
from neutron.db import common_db_mixin
from neutron.db import models_v2
from neutron.db import sqlalchemyutils
from neutron.extensions import l3
from neutron import manager
from neutron import neutron_plugin_base_v2
from neutron.openstack.common import excutils
from neutron.openstack.common import log as logging
from neutron.openstack.common import uuidutils
from neutron.plugins.common import constants as service_constants
from neutron.services.vm.common import constants as s_constants
from neutron.services.vm.mgmt_drivers.rpc import svm_rpc_joint_agent_api


LOG = logging.getLogger(__name__)

# Ports with the following 'device_owner' values will not prevent
# network deletion.  If delete_network() finds that all ports on a
# network have these owners, it will explicitly delete each port
# and allow network deletion to continue.  Similarly, if delete_subnet()
# finds out that all existing IP Allocations are associated with ports
# with these owners, it will allow subnet deletion to proceed with the
# IP allocations being cleaned up by cascade.
AUTO_DELETE_PORT_OWNERS = [constants.DEVICE_OWNER_DHCP]


class NeutronDbPluginV2(neutron_plugin_base_v2.NeutronPluginBaseV2,
                        common_db_mixin.CommonDbMixin):
    """V2 Neutron plugin interface implementation using SQLAlchemy models.

    Whenever a non-read call happens the plugin will call an event handler
    class method (e.g., network_created()).  The result is that this class
    can be sub-classed by other classes that add custom behaviors on certain
    events.
    """

    # This attribute specifies whether the plugin supports or not
    # bulk/pagination/sorting operations. Name mangling is used in
    # order to ensure it is qualified by class
    __native_bulk_support = True
    __native_pagination_support = True
    __native_sorting_support = True

    def __init__(self):
        if cfg.CONF.notify_nova_on_port_status_changes:
            from neutron.notifiers import nova
            # NOTE(arosen) These event listeners are here to hook into when
            # port status changes and notify nova about their change.
            self.nova_notifier = nova.Notifier()
            event.listen(models_v2.Port, 'after_insert',
                         self.nova_notifier.send_port_status)
            event.listen(models_v2.Port, 'after_update',
                         self.nova_notifier.send_port_status)
            event.listen(models_v2.Port.status, 'set',
                         self.nova_notifier.record_port_status_changed)

    @classmethod
    def register_dict_extend_funcs(cls, resource, funcs):
        cur_funcs = cls._dict_extend_functions.get(resource, [])
        cur_funcs.extend(funcs)
        cls._dict_extend_functions[resource] = cur_funcs

    def _get_network(self, context, id):
        try:
            network = self._get_by_id(context, models_v2.Network, id)
        except exc.NoResultFound:
            raise n_exc.NetworkNotFound(net_id=id)
        return network

    def _get_subnet(self, context, id):
        try:
            subnet = self._get_by_id(context, models_v2.Subnet, id)
        except exc.NoResultFound:
            raise n_exc.SubnetNotFound(subnet_id=id)
        return subnet

    def _get_port(self, context, id):
        try:
            port = self._get_by_id(context, models_v2.Port, id)
        except exc.NoResultFound:
            raise n_exc.PortNotFound(port_id=id)
        return port

    def _get_dns_by_subnet(self, context, subnet_id):
        dns_qry = context.session.query(models_v2.DNSNameServer)
        return dns_qry.filter_by(subnet_id=subnet_id).all()

    def _get_route_by_subnet(self, context, subnet_id):
        route_qry = context.session.query(models_v2.SubnetRoute)
        return route_qry.filter_by(subnet_id=subnet_id).all()

    def _get_subnets_by_network(self, context, network_id):
        subnet_qry = context.session.query(models_v2.Subnet)
        return subnet_qry.filter_by(network_id=network_id).all()

    def _get_all_subnets(self, context):
        # NOTE(salvatore-orlando): This query might end up putting
        # a lot of stress on the db. Consider adding a cache layer
        return context.session.query(models_v2.Subnet).all()

    @staticmethod
    def _generate_mac(context, network_id):
        base_mac = cfg.CONF.base_mac.split(':')
        max_retries = cfg.CONF.mac_generation_retries
        for i in range(max_retries):
            mac = [int(base_mac[0], 16), int(base_mac[1], 16),
                   int(base_mac[2], 16), random.randint(0x00, 0xff),
                   random.randint(0x00, 0xff), random.randint(0x00, 0xff)]
            if base_mac[3] != '00':
                mac[3] = int(base_mac[3], 16)
            mac_address = ':'.join(map(lambda x: "%02x" % x, mac))
            if NeutronDbPluginV2._check_unique_mac(context, network_id,
                                                   mac_address):
                LOG.debug(_("Generated mac for network %(network_id)s "
                            "is %(mac_address)s"),
                          {'network_id': network_id,
                           'mac_address': mac_address})
                return mac_address
            else:
                LOG.debug(_("Generated mac %(mac_address)s exists. Remaining "
                            "attempts %(max_retries)s."),
                          {'mac_address': mac_address,
                           'max_retries': max_retries - (i + 1)})
        LOG.error(_("Unable to generate mac address after %s attempts"),
                  max_retries)
        raise n_exc.MacAddressGenerationFailure(net_id=network_id)

    @staticmethod
    def _check_unique_mac(context, network_id, mac_address):
        mac_qry = context.session.query(models_v2.Port)
        try:
            mac_qry.filter_by(network_id=network_id,
                              mac_address=mac_address).one()
        except exc.NoResultFound:
            return True
        return False

    @staticmethod
    def _delete_ip_allocation(context, network_id, subnet_id, ip_address):

        # Delete the IP address from the IPAllocate table
        LOG.debug(_("Delete allocated IP %(ip_address)s "
                    "(%(network_id)s/%(subnet_id)s)"),
                  {'ip_address': ip_address,
                   'network_id': network_id,
                   'subnet_id': subnet_id})
        context.session.query(models_v2.IPAllocation).filter_by(
            network_id=network_id,
            ip_address=ip_address,
            subnet_id=subnet_id).delete()

    @staticmethod
    def _check_if_subnet_uses_eui64(subnet):
        """Check if ipv6 address will be calculated via EUI64."""
        return (subnet['ipv6_address_mode'] == constants.IPV6_SLAAC
                or subnet['ipv6_address_mode'] == constants.DHCPV6_STATELESS)

    @staticmethod
    def _store_ip_allocation(context, ip_address, network_id, subnet_id,
                             port_id):
        LOG.debug("Allocated IP %(ip_address)s "
                  "(%(network_id)s/%(subnet_id)s/%(port_id)s)",
                  {'ip_address': ip_address,
                   'network_id': network_id,
                   'subnet_id': subnet_id,
                   'port_id': port_id})
        allocated = models_v2.IPAllocation(
            network_id=network_id,
            port_id=port_id,
            ip_address=ip_address,
            subnet_id=subnet_id
        )
        context.session.add(allocated)

    @staticmethod
    def _generate_ip(context, subnets):
        try:
            return NeutronDbPluginV2._try_generate_ip(context, subnets)
        except n_exc.IpAddressGenerationFailure:
            NeutronDbPluginV2._rebuild_availability_ranges(context, subnets)

        return NeutronDbPluginV2._try_generate_ip(context, subnets)

    @staticmethod
    def _try_generate_ip(context, subnets):
        """Generate an IP address.

        The IP address will be generated from one of the subnets defined on
        the network.
        """
        range_qry = context.session.query(
            models_v2.IPAvailabilityRange).join(
                models_v2.IPAllocationPool).with_lockmode('update')
        for subnet in subnets:
            ip_range = range_qry.filter_by(subnet_id=subnet['id']).first()
            if not ip_range:
                LOG.debug(_("All IPs from subnet %(subnet_id)s (%(cidr)s) "
                            "allocated"),
                          {'subnet_id': subnet['id'], 'cidr': subnet['cidr']})
                continue
            ip_address = ip_range['first_ip']
            LOG.debug(_("Allocated IP - %(ip_address)s from %(first_ip)s "
                        "to %(last_ip)s"),
                      {'ip_address': ip_address,
                       'first_ip': ip_range['first_ip'],
                       'last_ip': ip_range['last_ip']})
            if ip_range['first_ip'] == ip_range['last_ip']:
                # No more free indices on subnet => delete
                LOG.debug(_("No more free IP's in slice. Deleting allocation "
                            "pool."))
                context.session.delete(ip_range)
            else:
                # increment the first free
                ip_range['first_ip'] = str(netaddr.IPAddress(ip_address) + 1)
            return {'ip_address': ip_address, 'subnet_id': subnet['id']}
        raise n_exc.IpAddressGenerationFailure(net_id=subnets[0]['network_id'])

    @staticmethod
    def _rebuild_availability_ranges(context, subnets):
        ip_qry = context.session.query(
            models_v2.IPAllocation).with_lockmode('update')
        # PostgreSQL does not support select...for update with an outer join.
        # No join is needed here.
        pool_qry = context.session.query(
            models_v2.IPAllocationPool).options(
                orm.noload('available_ranges')).with_lockmode('update')
        for subnet in sorted(subnets):
            LOG.debug(_("Rebuilding availability ranges for subnet %s")
                      % subnet)

            # Create a set of all currently allocated addresses
            ip_qry_results = ip_qry.filter_by(subnet_id=subnet['id'])
            allocations = netaddr.IPSet([netaddr.IPAddress(i['ip_address'])
                                        for i in ip_qry_results])

            for pool in pool_qry.filter_by(subnet_id=subnet['id']):
                # Create a set of all addresses in the pool
                poolset = netaddr.IPSet(netaddr.iter_iprange(pool['first_ip'],
                                                             pool['last_ip']))

                # Use set difference to find free addresses in the pool
                available = poolset - allocations

                # Generator compacts an ip set into contiguous ranges
                def ipset_to_ranges(ipset):
                    first, last = None, None
                    for cidr in ipset.iter_cidrs():
                        if last and last + 1 != cidr.first:
                            yield netaddr.IPRange(first, last)
                            first = None
                        first, last = first if first else cidr.first, cidr.last
                    if first:
                        yield netaddr.IPRange(first, last)

                # Write the ranges to the db
                for ip_range in ipset_to_ranges(available):
                    available_range = models_v2.IPAvailabilityRange(
                        allocation_pool_id=pool['id'],
                        first_ip=str(netaddr.IPAddress(ip_range.first)),
                        last_ip=str(netaddr.IPAddress(ip_range.last)))
                    context.session.add(available_range)

    @staticmethod
    def _allocate_specific_ip(context, subnet_id, ip_address):
        """Allocate a specific IP address on the subnet."""
        ip = int(netaddr.IPAddress(ip_address))
        range_qry = context.session.query(
            models_v2.IPAvailabilityRange).join(
                models_v2.IPAllocationPool).with_lockmode('update')
        results = range_qry.filter_by(subnet_id=subnet_id)
        for ip_range in results:
            first = int(netaddr.IPAddress(ip_range['first_ip']))
            last = int(netaddr.IPAddress(ip_range['last_ip']))
            if first <= ip <= last:
                if first == last:
                    context.session.delete(ip_range)
                    return
                elif first == ip:
                    new_first_ip = str(netaddr.IPAddress(ip_address) + 1)
                    ip_range['first_ip'] = new_first_ip
                    return
                elif last == ip:
                    new_last_ip = str(netaddr.IPAddress(ip_address) - 1)
                    ip_range['last_ip'] = new_last_ip
                    return
                else:
                    # Split into two ranges
                    new_first = str(netaddr.IPAddress(ip_address) + 1)
                    new_last = ip_range['last_ip']
                    new_last_ip = str(netaddr.IPAddress(ip_address) - 1)
                    ip_range['last_ip'] = new_last_ip
                    ip_range = models_v2.IPAvailabilityRange(
                        allocation_pool_id=ip_range['allocation_pool_id'],
                        first_ip=new_first,
                        last_ip=new_last)
                    context.session.add(ip_range)
                    return

    @staticmethod
    def _check_unique_ip(context, network_id, subnet_id, ip_address):
        """Validate that the IP address on the subnet is not in use."""
        ip_qry = context.session.query(models_v2.IPAllocation)
        try:
            ip_qry.filter_by(network_id=network_id,
                             subnet_id=subnet_id,
                             ip_address=ip_address).one()
        except exc.NoResultFound:
            return True
        return False

    @classmethod
    def _check_gateway_in_subnet(cls, cidr, gateway):
        """Validate that the gateway is on the subnet."""
        ip = netaddr.IPAddress(gateway)
        if ip.version == 4 or (ip.version == 6 and not ip.is_link_local()):
            return cls._check_subnet_ip(cidr, gateway)
        return True

    @classmethod
    def _check_subnet_ip(cls, cidr, ip_address):
        """Validate that the IP address is on the subnet."""
        ip = netaddr.IPAddress(ip_address)
        net = netaddr.IPNetwork(cidr)
        # Check that the IP is valid on subnet. This cannot be the
        # network or the broadcast address
        if (ip != net.network and
                ip != net.broadcast and
                net.netmask & ip == net.network):
            return True
        return False

    @staticmethod
    def _check_ip_in_allocation_pool(context, subnet_id, gateway_ip,
                                     ip_address):
        """Validate IP in allocation pool.

        Validates that the IP address is either the default gateway or
        in the allocation pools of the subnet.
        """
        # Check if the IP is the gateway
        if ip_address == gateway_ip:
            # Gateway is not in allocation pool
            return False

        # Check if the requested IP is in a defined allocation pool
        pool_qry = context.session.query(models_v2.IPAllocationPool)
        allocation_pools = pool_qry.filter_by(subnet_id=subnet_id)
        ip = netaddr.IPAddress(ip_address)
        for allocation_pool in allocation_pools:
            allocation_pool_range = netaddr.IPRange(
                allocation_pool['first_ip'],
                allocation_pool['last_ip'])
            if ip in allocation_pool_range:
                return True
        return False

    def _get_subnet_allocation_pool(self, context, subnet_id):
        ctx_admin = ctx.get_admin_context()
        pool_qry = ctx_admin.session.query(models_v2.IPAllocationPool)
        allocation_pools = pool_qry.filter_by(subnet_id=subnet_id)
        exist_allocation_pools = [{'start': pool['first_ip'],
                                   'end': pool['last_ip']} for pool in allocation_pools]
        return exist_allocation_pools

    def _test_fixed_ips_for_port(self, context, network_id, fixed_ips, device_owner=None):
        """Test fixed IPs for port.

        Check that configured subnets are valid prior to allocating any
        IPs. Include the subnet_id in the result if only an IP address is
        configured.

        :raises: InvalidInput, IpAddressInUse
        """
        fixed_ip_set = []
        for fixed in fixed_ips:
            found = False
            if 'subnet_id' not in fixed:
                if 'ip_address' not in fixed:
                    msg = _('IP allocation requires subnet_id or ip_address')
                    raise n_exc.InvalidInput(error_message=msg)

                filter = {'network_id': [network_id]}
                subnets = self.get_subnets(context, filters=filter)
                for subnet in subnets:
                    if self._check_subnet_ip(subnet['cidr'],
                                             fixed['ip_address']):
                        found = True
                        subnet_id = subnet['id']
                        break
                if not found:
                    msg = _('IP address %s is not a valid IP for the defined '
                            'networks subnets') % fixed['ip_address']
                    raise n_exc.InvalidInput(error_message=msg)
            else:
                subnet = self._get_subnet(context, fixed['subnet_id'])
                if subnet['network_id'] != network_id:
                    msg = (_("Failed to create port on network %(network_id)s"
                             ", because fixed_ips included invalid subnet "
                             "%(subnet_id)s") %
                           {'network_id': network_id,
                            'subnet_id': fixed['subnet_id']})
                    raise n_exc.InvalidInput(error_message=msg)
                subnet_id = subnet['id']

            if 'ip_address' in fixed:
                # Ensure that the IP's are unique
                if not NeutronDbPluginV2._check_unique_ip(context, network_id,
                                                          subnet_id,
                                                          fixed['ip_address']):
                    raise n_exc.IpAddressInUse(net_id=network_id,
                                               ip_address=fixed['ip_address'])

                # Ensure that the IP is valid on the subnet
                if (not found and
                    not self._check_subnet_ip(subnet['cidr'],
                                              fixed['ip_address'])):
                    msg = _('IP address %s is not a valid IP for the defined '
                            'subnet') % fixed['ip_address']
                    raise n_exc.InvalidInput(error_message=msg)

                fixed_ip_set.append({'subnet_id': subnet_id,
                                     'ip_address': fixed['ip_address']})
            else:
                fixed_ip_set.append({'subnet_id': subnet_id})
        service_ports = cfg.CONF.unitedstack.service_port_owners
        if (device_owner not in service_ports) and (
                len(fixed_ip_set) > cfg.CONF.max_fixed_ips_per_port):
            msg = _('Exceeded maximim amount of fixed ips per port')
            raise n_exc.InvalidInput(error_message=msg)
        return fixed_ip_set

    def _allocate_fixed_ips(self, context, fixed_ips):
        """Allocate IP addresses according to the configured fixed_ips."""
        ips = []
        for fixed in fixed_ips:
            if 'ip_address' in fixed:
                # Remove the IP address from the allocation pool
                NeutronDbPluginV2._allocate_specific_ip(
                    context, fixed['subnet_id'], fixed['ip_address'])
                ips.append({'ip_address': fixed['ip_address'],
                            'subnet_id': fixed['subnet_id']})
            # Only subnet ID is specified => need to generate IP
            # from subnet
            else:
                subnets = [self._get_subnet(context, fixed['subnet_id'])]
                # IP address allocation
                result = self._generate_ip(context, subnets)
                ips.append({'ip_address': result['ip_address'],
                            'subnet_id': result['subnet_id']})
        return ips

    def _update_ips_for_port(self, context, network_id, port_id, original_ips,
                             new_ips, device_owner=None):
        """Add or remove IPs from the port."""
        ips = []
        # These ips are still on the port and haven't been removed
        prev_ips = []

        # the new_ips contain all of the fixed_ips that are to be updated
        service_ports = cfg.CONF.unitedstack.service_port_owners
        if (device_owner not in service_ports) and (
                len(new_ips) > cfg.CONF.max_fixed_ips_per_port):
            msg = _('Exceeded maximim amount of fixed ips per port')
            raise n_exc.InvalidInput(error_message=msg)

        # Remove all of the intersecting elements
        for original_ip in original_ips[:]:
            for new_ip in new_ips[:]:
                if ('ip_address' in new_ip and
                    original_ip['ip_address'] == new_ip['ip_address']):
                    original_ips.remove(original_ip)
                    new_ips.remove(new_ip)
                    prev_ips.append(original_ip)

        # Check if the IP's to add are OK
        to_add = self._test_fixed_ips_for_port(context, network_id, new_ips, device_owner)
        for ip in original_ips:
            LOG.debug(_("Port update. Hold %s"), ip)
            NeutronDbPluginV2._delete_ip_allocation(context,
                                                    network_id,
                                                    ip['subnet_id'],
                                                    ip['ip_address'])

        if to_add:
            LOG.debug(_("Port update. Adding %s"), to_add)
            ips = self._allocate_fixed_ips(context, to_add)
        return ips, prev_ips

    def _allocate_ips_for_port(self, context, port):
        """Allocate IP addresses for the port.

        If port['fixed_ips'] is set to 'ATTR_NOT_SPECIFIED', allocate IP
        addresses for the port. If port['fixed_ips'] contains an IP address or
        a subnet_id then allocate an IP address accordingly.
        """
        p = port['port']
        ips = []

        fixed_configured = p['fixed_ips'] is not attributes.ATTR_NOT_SPECIFIED
        if fixed_configured:
            configured_ips = self._test_fixed_ips_for_port(context,
                                        p["network_id"],
                                        p['fixed_ips'],
                                        p.get('device_owner'))
            ips = self._allocate_fixed_ips(context, configured_ips)
        else:
            filter = {'network_id': [p['network_id']]}
            subnets = self.get_subnets(context, filters=filter)
            # Split into v4 and v6 subnets
            v4 = []
            v6 = []
            for subnet in subnets:

                subnet_service_provider = subnet['uos:service_provider']

                provider_name = p.get('uos:service_provider', None)
                if ( provider_name and
                    (p.get('device_owner') == constants.DEVICE_OWNER_FLOATINGIP)
                    and (subnet_service_provider != provider_name) ):
                    continue
                shadow_subnet = cfg.CONF.unitedstack.external_shadow_subnet
                if (shadow_subnet and shadow_subnet == subnet['name']):
                    continue
                subnets_to_exclude = cfg.CONF.unitedstack.subnets_to_exclude
                if subnet['id'] in subnets_to_exclude:
                    continue
                if subnet['ip_version'] == 4:
                    v4.append(subnet)
                else:
                    v6.append(subnet)
            for subnet in v6:
                if self._check_if_subnet_uses_eui64(subnet):
                    #(dzyu) If true, calculate an IPv6 address
                    # by mac address and prefix, then remove this
                    # subnet from the array of subnets that will be passed
                    # to the _generate_ip() function call, since we just
                    # generated an IP.
                    mac = p['mac_address']
                    prefix = subnet['cidr']
                    ip_address = ipv6_utils.get_ipv6_addr_by_EUI64(
                        prefix, mac)
                    if not self._check_unique_ip(
                        context, p['network_id'],
                        subnet['id'], ip_address.format()):
                        raise n_exc.IpAddressInUse(
                            net_id=p['network_id'],
                            ip_address=ip_address.format())
                    ips.append({'ip_address': ip_address.format(),
                                'subnet_id': subnet['id']})
                    v6.remove(subnet)
            version_subnets = [v4, v6]
            for subnets in version_subnets:
                if subnets:
                    result = NeutronDbPluginV2._generate_ip(context, subnets)
                    ips.append({'ip_address': result['ip_address'],
                                'subnet_id': result['subnet_id']})
        return ips

    def _validate_subnet_cidr(self, context, network, new_subnet_cidr):
        """Validate the CIDR for a subnet.

        Verifies the specified CIDR does not overlap with the ones defined
        for the other subnets specified for this network, or with any other
        CIDR if overlapping IPs are disabled.
        """
        new_subnet_ipset = netaddr.IPSet([new_subnet_cidr])
        if cfg.CONF.allow_overlapping_ips:
            subnet_list = network.subnets
        else:
            subnet_list = self._get_all_subnets(context)
        for subnet in subnet_list:
            if (netaddr.IPSet([subnet.cidr]) & new_subnet_ipset):
                # don't give out details of the overlapping subnet
                err_msg = (_("Requested subnet with cidr: %(cidr)s for "
                             "network: %(network_id)s overlaps with another "
                             "subnet") %
                           {'cidr': new_subnet_cidr,
                            'network_id': network.id})
                LOG.info(_("Validation for CIDR: %(new_cidr)s failed - "
                           "overlaps with subnet %(subnet_id)s "
                           "(CIDR: %(cidr)s)"),
                         {'new_cidr': new_subnet_cidr,
                          'subnet_id': subnet.id,
                          'cidr': subnet.cidr})
                raise n_exc.InvalidInput(error_message=err_msg)

    def _validate_allocation_pools(self, ip_pools, subnet_cidr):
        """Validate IP allocation pools.

        Verify start and end address for each allocation pool are valid,
        ie: constituted by valid and appropriately ordered IP addresses.
        Also, verify pools do not overlap among themselves.
        Finally, verify that each range fall within the subnet's CIDR.
        """
        subnet = netaddr.IPNetwork(subnet_cidr)
        subnet_first_ip = netaddr.IPAddress(subnet.first + 1)
        subnet_last_ip = netaddr.IPAddress(subnet.last - 1)

        LOG.debug(_("Performing IP validity checks on allocation pools"))
        ip_sets = []
        for ip_pool in ip_pools:
            try:
                start_ip = netaddr.IPAddress(ip_pool['start'])
                end_ip = netaddr.IPAddress(ip_pool['end'])
            except netaddr.AddrFormatError:
                LOG.info(_("Found invalid IP address in pool: "
                           "%(start)s - %(end)s:"),
                         {'start': ip_pool['start'],
                          'end': ip_pool['end']})
                raise n_exc.InvalidAllocationPool(pool=ip_pool)
            if (start_ip.version != subnet.version or
                    end_ip.version != subnet.version):
                LOG.info(_("Specified IP addresses do not match "
                           "the subnet IP version"))
                raise n_exc.InvalidAllocationPool(pool=ip_pool)
            if end_ip < start_ip:
                LOG.info(_("Start IP (%(start)s) is greater than end IP "
                           "(%(end)s)"),
                         {'start': ip_pool['start'], 'end': ip_pool['end']})
                raise n_exc.InvalidAllocationPool(pool=ip_pool)
            if start_ip < subnet_first_ip or end_ip > subnet_last_ip:
                LOG.info(_("Found pool larger than subnet "
                           "CIDR:%(start)s - %(end)s"),
                         {'start': ip_pool['start'],
                          'end': ip_pool['end']})
                raise n_exc.OutOfBoundsAllocationPool(
                    pool=ip_pool,
                    subnet_cidr=subnet_cidr)
            # Valid allocation pool
            # Create an IPSet for it for easily verifying overlaps
            ip_sets.append(netaddr.IPSet(netaddr.IPRange(
                ip_pool['start'],
                ip_pool['end']).cidrs()))

        LOG.debug(_("Checking for overlaps among allocation pools "
                    "and gateway ip"))
        ip_ranges = ip_pools[:]

        # Use integer cursors as an efficient way for implementing
        # comparison and avoiding comparing the same pair twice
        for l_cursor in range(len(ip_sets)):
            for r_cursor in range(l_cursor + 1, len(ip_sets)):
                if ip_sets[l_cursor] & ip_sets[r_cursor]:
                    l_range = ip_ranges[l_cursor]
                    r_range = ip_ranges[r_cursor]
                    LOG.info(_("Found overlapping ranges: %(l_range)s and "
                               "%(r_range)s"),
                             {'l_range': l_range, 'r_range': r_range})
                    raise n_exc.OverlappingAllocationPools(
                        pool_1=l_range,
                        pool_2=r_range,
                        subnet_cidr=subnet_cidr)

    def _validate_host_route(self, route, ip_version):
        try:
            netaddr.IPNetwork(route['destination'])
            netaddr.IPAddress(route['nexthop'])
        except netaddr.core.AddrFormatError:
            err_msg = _("Invalid route: %s") % route
            raise n_exc.InvalidInput(error_message=err_msg)
        except ValueError:
            # netaddr.IPAddress would raise this
            err_msg = _("Invalid route: %s") % route
            raise n_exc.InvalidInput(error_message=err_msg)
        self._validate_ip_version(ip_version, route['nexthop'], 'nexthop')
        self._validate_ip_version(ip_version, route['destination'],
                                  'destination')

    def _allocate_pools_for_subnet(self, context, subnet):
        """Create IP allocation pools for a given subnet

        Pools are defined by the 'allocation_pools' attribute,
        a list of dict objects with 'start' and 'end' keys for
        defining the pool range.
        """
        pools = []
        # Auto allocate the pool around gateway_ip
        net = netaddr.IPNetwork(subnet['cidr'])
        first_ip = net.first + 1
        last_ip = net.last - 1
        gw_ip = int(netaddr.IPAddress(subnet['gateway_ip'] or net.last))
        # Use the gw_ip to find a point for splitting allocation pools
        # for this subnet
        split_ip = min(max(gw_ip, net.first), net.last)
        if split_ip > first_ip:
            pools.append({'start': str(netaddr.IPAddress(first_ip)),
                          'end': str(netaddr.IPAddress(split_ip - 1))})
        if split_ip < last_ip:
            pools.append({'start': str(netaddr.IPAddress(split_ip + 1)),
                          'end': str(netaddr.IPAddress(last_ip))})
        # return auto-generated pools
        # no need to check for their validity
        return pools

    def _validate_shared_update(self, context, id, original, updated):
        # The only case that needs to be validated is when 'shared'
        # goes from True to False
        if updated['shared'] == original.shared or updated['shared']:
            return
        ports = self._model_query(
            context, models_v2.Port).filter(
                and_(
                    models_v2.Port.network_id == id,
                    models_v2.Port.device_owner !=
                    constants.DEVICE_OWNER_ROUTER_GW,
                    models_v2.Port.device_owner !=
                    constants.DEVICE_OWNER_FLOATINGIP))
        subnets = self._model_query(
            context, models_v2.Subnet).filter(
                models_v2.Subnet.network_id == id)
        tenant_ids = set([port['tenant_id'] for port in ports] +
                         [subnet['tenant_id'] for subnet in subnets])
        # raise if multiple tenants found or if the only tenant found
        # is not the owner of the network
        if (len(tenant_ids) > 1 or len(tenant_ids) == 1 and
            tenant_ids.pop() != original.tenant_id):
            raise n_exc.InvalidSharedSetting(network=original.name)

    def _validate_ipv6_attributes(self, subnet, cur_subnet):
        ra_mode_set = attributes.is_attr_set(subnet.get('ipv6_ra_mode'))
        address_mode_set = attributes.is_attr_set(
            subnet.get('ipv6_address_mode'))
        if cur_subnet:
            ra_mode = (subnet['ipv6_ra_mode'] if ra_mode_set
                       else cur_subnet['ipv6_ra_mode'])
            addr_mode = (subnet['ipv6_address_mode'] if address_mode_set
                         else cur_subnet['ipv6_address_mode'])
            if ra_mode_set or address_mode_set:
                # Check that updated subnet ipv6 attributes do not conflict
                self._validate_ipv6_combination(ra_mode, addr_mode)
            self._validate_ipv6_update_dhcp(subnet, cur_subnet)
        else:
            self._validate_ipv6_dhcp(ra_mode_set, address_mode_set,
                                     subnet['enable_dhcp'])
            if ra_mode_set and address_mode_set:
                self._validate_ipv6_combination(subnet['ipv6_ra_mode'],
                                                subnet['ipv6_address_mode'])

    def _validate_ipv6_combination(self, ra_mode, address_mode):
        if ra_mode != address_mode:
            msg = _("ipv6_ra_mode set to '%(ra_mode)s' with ipv6_address_mode "
                    "set to '%(addr_mode)s' is not valid. "
                    "If both attributes are set, they must be the same value"
                    ) % {'ra_mode': ra_mode, 'addr_mode': address_mode}
            raise n_exc.InvalidInput(error_message=msg)

    def _validate_ipv6_dhcp(self, ra_mode_set, address_mode_set, enable_dhcp):
        if (ra_mode_set or address_mode_set) and not enable_dhcp:
            msg = _("ipv6_ra_mode or ipv6_address_mode cannot be set when "
                    "enable_dhcp is set to False.")
            raise n_exc.InvalidInput(error_message=msg)

    def _validate_ipv6_update_dhcp(self, subnet, cur_subnet):
        if ('enable_dhcp' in subnet and not subnet['enable_dhcp']):
            msg = _("Cannot disable enable_dhcp with "
                    "ipv6 attributes set")

            ra_mode_set = attributes.is_attr_set(subnet.get('ipv6_ra_mode'))
            address_mode_set = attributes.is_attr_set(
                subnet.get('ipv6_address_mode'))

            if ra_mode_set or address_mode_set:
                raise n_exc.InvalidInput(error_message=msg)

            old_ra_mode_set = attributes.is_attr_set(
                cur_subnet.get('ipv6_ra_mode'))
            old_address_mode_set = attributes.is_attr_set(
                cur_subnet.get('ipv6_address_mode'))

            if old_ra_mode_set or old_address_mode_set:
                raise n_exc.InvalidInput(error_message=msg)

    def _make_network_dict(self, network, fields=None,
                           process_extensions=True):
        res = {'id': network['id'],
               'name': network['name'],
               'tenant_id': network['tenant_id'],
               'admin_state_up': network['admin_state_up'],
               'status': network['status'],
               'shared': network['shared'],
               'provider:unmanaged_network':network['unmanaged_network'],
               'subnets': [subnet['id']
                           for subnet in network['subnets']]}
        # Call auxiliary extend functions, if any
        if process_extensions:
            self._apply_dict_extend_functions(
                attributes.NETWORKS, res, network)
        return self._fields(res, fields)

    def _make_subnet_dict(self, subnet, fields=None, process_extensions=True):
        res = {'id': subnet['id'],
               'name': subnet['name'],
               'tenant_id': subnet['tenant_id'],
               'network_id': subnet['network_id'],
               'ip_version': subnet['ip_version'],
               'cidr': subnet['cidr'],
               'allocation_pools': [{'start': pool['first_ip'],
                                     'end': pool['last_ip']}
                                    for pool in subnet['allocation_pools']],
               'gateway_ip': subnet['gateway_ip'],
               'enable_dhcp': subnet['enable_dhcp'],
               'ipv6_ra_mode': subnet['ipv6_ra_mode'],
               'ipv6_address_mode': subnet['ipv6_address_mode'],
               'dns_nameservers': [dns['address']
                                   for dns in subnet['dns_nameservers']],
               'host_routes': [{'destination': route['destination'],
                                'nexthop': route['nexthop']}
                               for route in subnet['routes']],
               'shared': subnet['shared']
               }
        # Call auxiliary extend functions, if any
        self._apply_dict_extend_functions(attributes.SUBNETS, res, subnet)
        return self._fields(res, fields)

    def _make_port_dict(self, port, fields=None,
                        process_extensions=True):
        res = {"id": port["id"],
               'name': port['name'],
               "network_id": port["network_id"],
               'tenant_id': port['tenant_id'],
               "mac_address": port["mac_address"],
               "admin_state_up": port["admin_state_up"],
               "status": port["status"],
               "fixed_ips": [{'subnet_id': ip["subnet_id"],
                              'ip_address': ip["ip_address"]}
                             for ip in port["fixed_ips"]],
               "device_id": port["device_id"],
               "disable_anti_spoofing": port["disable_anti_spoofing"],
               "servicevm_device": port["servicevm_device"],
               "service_instance_id": port["service_instance_id"],
               "servicevm_type": port["servicevm_type"],
               "device_owner": port["device_owner"]}
        # Call auxiliary extend functions, if any
        if process_extensions:
            self._apply_dict_extend_functions(
                attributes.PORTS, res, port)
        return self._fields(res, fields)

    def _create_bulk(self, resource, context, request_items):
        objects = []
        collection = "%ss" % resource
        items = request_items[collection]
        context.session.begin(subtransactions=True)
        try:
            for item in items:
                obj_creator = getattr(self, 'create_%s' % resource)
                objects.append(obj_creator(context, item))
            context.session.commit()
        except Exception:
            context.session.rollback()
            with excutils.save_and_reraise_exception():
                LOG.error(_("An exception occurred while creating "
                            "the %(resource)s:%(item)s"),
                          {'resource': resource, 'item': item})
        return objects

    def create_network_bulk(self, context, networks):
        return self._create_bulk('network', context, networks)

    def add_extra_net_data(self, context, network, netdb):
        pass

    def create_network(self, context, network):
        """Handle creation of a single network."""
        # single request processing
        n = network['network']
        # NOTE(jkoelker) Get the tenant_id outside of the session to avoid
        #                unneeded db action if the operation raises
        tenant_id = self._get_tenant_id_for_create(context, n)
        with context.session.begin(subtransactions=True):
            args = {'tenant_id': tenant_id,
                    'id': n.get('id') or uuidutils.generate_uuid(),
                    'name': n['name'],
                    'admin_state_up': n['admin_state_up'],
                    'shared': n['shared'],
                    'unmanaged_network': n['provider:unmanaged_network'],
                    'status': n.get('status', constants.NET_STATUS_ACTIVE)}
            utils.make_default_name(args, uos_constants.UOS_PRE_NET)
            network = models_v2.Network(**args)
            context.session.add(network)
            self.add_extra_net_data(context, n, network)
        return self._make_network_dict(network, process_extensions=False)

    def update_extra_net_data(self, context, network, netdb):
        pass

    def update_network(self, context, id, network):
        n = network['network']
        # NOTE(gongysh) for the purpose update the network with
        # data return network created just
        n.pop('created_at', None)
        with context.session.begin(subtransactions=True):
            network = self._get_network(context, id)
            # validate 'shared' parameter
            if 'shared' in n:
                self._validate_shared_update(context, id, network, n)
            network.update(n)
            # also update shared in all the subnets for this network
            subnets = self._get_subnets_by_network(context, id)
            for subnet in subnets:
                subnet['shared'] = network['shared']
            self.update_extra_net_data(context, n, network)
        return self._make_network_dict(network)

    def delete_network(self, context, id):
        with context.session.begin(subtransactions=True):
            network = self._get_network(context, id)

            filters = {'network_id': [id]}
            # NOTE(armando-migliaccio): stick with base plugin
            query = context.session.query(
                models_v2.Port).enable_eagerloads(False)
            ports = self._apply_filters_to_query(
                query, models_v2.Port, filters).with_lockmode('update')

            # check if there are any tenant owned ports in-use
            only_auto_del = all(p['device_owner'] in AUTO_DELETE_PORT_OWNERS
                                for p in ports)

            if not only_auto_del:
                raise n_exc.NetworkInUse(net_id=id)

            # clean up network owned ports
            for port in ports:
                self._delete_port(context, port['id'])

            # clean up subnets
            subnets_qry = context.session.query(models_v2.Subnet)
            subnets_qry.filter_by(network_id=id).delete()
            context.session.delete(network)

    def get_network(self, context, id, fields=None):
        network = self._get_network(context, id)
        return self._make_network_dict(network, fields)

    def get_networks(self, context, filters=None, fields=None,
                     sorts=None, limit=None, marker=None,
                     page_reverse=False):
        marker_obj = self._get_marker_obj(context, 'network', limit, marker)
        return self._get_collection(context, models_v2.Network,
                                    self._make_network_dict,
                                    filters=filters, fields=fields,
                                    sorts=sorts,
                                    limit=limit,
                                    marker_obj=marker_obj,
                                    page_reverse=page_reverse)

    def get_networks_count(self, context, filters=None):
        return self._get_collection_count(context, models_v2.Network,
                                          filters=filters)

    def create_subnet_bulk(self, context, subnets):
        return self._create_bulk('subnet', context, subnets)

    def _validate_ip_version(self, ip_version, addr, name):
        """Check IP field of a subnet match specified ip version."""
        ip = netaddr.IPNetwork(addr)
        if ip.version != ip_version:
            data = {'name': name,
                    'addr': addr,
                    'ip_version': ip_version}
            msg = _("%(name)s '%(addr)s' does not match "
                    "the ip_version '%(ip_version)s'") % data
            raise n_exc.InvalidInput(error_message=msg)

    def _validate_subnet(self, context, s, cur_subnet=None):
        """Validate a subnet spec."""

        # This method will validate attributes which may change during
        # create_subnet() and update_subnet().
        # The method requires the subnet spec 's' has 'ip_version' field.
        # If 's' dict does not have 'ip_version' field in an API call
        # (e.g., update_subnet()), you need to set 'ip_version' field
        # before calling this method.

        ip_ver = s['ip_version']

        if 'cidr' in s:
            self._validate_ip_version(ip_ver, s['cidr'], 'cidr')

        if attributes.is_attr_set(s.get('gateway_ip')):
            self._validate_ip_version(ip_ver, s['gateway_ip'], 'gateway_ip')
            if (cfg.CONF.force_gateway_on_subnet and
                not self._check_gateway_in_subnet(
                    s['cidr'], s['gateway_ip'])):
                error_message = _("Gateway is not valid on subnet")
                raise n_exc.InvalidInput(error_message=error_message)
            # Ensure the gateway IP is not assigned to any port
            # skip this check in case of create (s parameter won't have id)
            # NOTE(salv-orlando): There is slight chance of a race, when
            # a subnet-update and a router-interface-add operation are
            # executed concurrently
            if cur_subnet:
                alloc_qry = context.session.query(models_v2.IPAllocation)
                allocated = alloc_qry.filter_by(
                    ip_address=cur_subnet['gateway_ip'],
                    subnet_id=cur_subnet['id']).first()
                if allocated and allocated['port_id']:
                    raise n_exc.GatewayIpInUse(
                        ip_address=cur_subnet['gateway_ip'],
                        port_id=allocated['port_id'])

        if attributes.is_attr_set(s.get('dns_nameservers')):
            if len(s['dns_nameservers']) > cfg.CONF.max_dns_nameservers:
                raise n_exc.DNSNameServersExhausted(
                    subnet_id=s.get('id', _('new subnet')),
                    quota=cfg.CONF.max_dns_nameservers)
            for dns in s['dns_nameservers']:
                try:
                    netaddr.IPAddress(dns)
                except Exception:
                    raise n_exc.InvalidInput(
                        error_message=(_("Error parsing dns address %s") %
                                       dns))
                self._validate_ip_version(ip_ver, dns, 'dns_nameserver')

        if attributes.is_attr_set(s.get('host_routes')):
            if len(s['host_routes']) > cfg.CONF.max_subnet_host_routes:
                raise n_exc.HostRoutesExhausted(
                    subnet_id=s.get('id', _('new subnet')),
                    quota=cfg.CONF.max_subnet_host_routes)
            # check if the routes are all valid
            for rt in s['host_routes']:
                self._validate_host_route(rt, ip_ver)

        if ip_ver == 4:
            if attributes.is_attr_set(s.get('ipv6_ra_mode')):
                raise n_exc.InvalidInput(
                    error_message=(_("ipv6_ra_mode is not valid when "
                                     "ip_version is 4")))
            if attributes.is_attr_set(s.get('ipv6_address_mode')):
                raise n_exc.InvalidInput(
                    error_message=(_("ipv6_address_mode is not valid when "
                                     "ip_version is 4")))
        if ip_ver == 6:
            self._validate_ipv6_attributes(s, cur_subnet)

    def _validate_gw_out_of_pools(self, gateway_ip, pools):
        for allocation_pool in pools:
            pool_range = netaddr.IPRange(
                allocation_pool['start'],
                allocation_pool['end'])
            if netaddr.IPAddress(gateway_ip) in pool_range:
                raise n_exc.GatewayConflictWithAllocationPools(
                    pool=pool_range,
                    ip_address=gateway_ip)

    def add_extra_subnet_data(self, context, subnet, subnetdb):
        pass

    def update_extra_subnet_data(self, context, subnet, subnetdb):
        pass

    def create_subnet(self, context, subnet):

        net = netaddr.IPNetwork(subnet['subnet']['cidr'])
        # turn the CIDR into a proper subnet
        subnet['subnet']['cidr'] = '%s/%s' % (net.network, net.prefixlen)

        s = subnet['subnet']

        if s['gateway_ip'] is attributes.ATTR_NOT_SPECIFIED:
            s['gateway_ip'] = str(netaddr.IPAddress(net.first + 1))

        if s['allocation_pools'] == attributes.ATTR_NOT_SPECIFIED:
            s['allocation_pools'] = self._allocate_pools_for_subnet(context, s)
        else:
            self._validate_allocation_pools(s['allocation_pools'], s['cidr'])
            if s['gateway_ip'] is not None:
                self._validate_gw_out_of_pools(s['gateway_ip'],
                                               s['allocation_pools'])

        self._validate_subnet(context, s)

        tenant_id = self._get_tenant_id_for_create(context, s)
        with context.session.begin(subtransactions=True):
            network = self._get_network(context, s["network_id"])
            self._validate_subnet_cidr(context, network, s['cidr'])
            # The 'shared' attribute for subnets is for internal plugin
            # use only. It is not exposed through the API
            args = {'tenant_id': tenant_id,
                    'id': s.get('id') or uuidutils.generate_uuid(),
                    'name': s['name'],
                    'network_id': s['network_id'],
                    'ip_version': s['ip_version'],
                    'cidr': s['cidr'],
                    'enable_dhcp': s['enable_dhcp'],
                    'gateway_ip': s['gateway_ip'],
                    'shared': network.shared}
            utils.make_default_name(args, uos_constants.UOS_PRE_SUBNET)
            if s['ip_version'] == 6 and s['enable_dhcp']:
                if attributes.is_attr_set(s['ipv6_ra_mode']):
                    args['ipv6_ra_mode'] = s['ipv6_ra_mode']
                if attributes.is_attr_set(s['ipv6_address_mode']):
                    args['ipv6_address_mode'] = s['ipv6_address_mode']
            subnet = models_v2.Subnet(**args)

            context.session.add(subnet)
            self.add_extra_subnet_data(context, s, subnet)

            if (s['dns_nameservers'] is attributes.ATTR_NOT_SPECIFIED and
                cfg.CONF.uos_subnet_nameservers):
                s['dns_nameservers'] = cfg.CONF.uos_subnet_nameservers
            if s['dns_nameservers'] is not attributes.ATTR_NOT_SPECIFIED:
                for addr in s['dns_nameservers']:
                    ns = models_v2.DNSNameServer(address=addr,
                                                 subnet_id=subnet.id)
                    context.session.add(ns)

            if s['host_routes'] is not attributes.ATTR_NOT_SPECIFIED:
                for rt in s['host_routes']:
                    route = models_v2.SubnetRoute(
                        subnet_id=subnet.id,
                        destination=rt['destination'],
                        nexthop=rt['nexthop'])
                    context.session.add(route)

            for pool in s['allocation_pools']:
                ip_pool = models_v2.IPAllocationPool(subnet=subnet,
                                                     first_ip=pool['start'],
                                                     last_ip=pool['end'])
                context.session.add(ip_pool)
                ip_range = models_v2.IPAvailabilityRange(
                    ipallocationpool=ip_pool,
                    first_ip=pool['start'],
                    last_ip=pool['end'])
                context.session.add(ip_range)

            svm_subnet = {}
            svm_subnet['new_pools'] = s['allocation_pools']
            self._notify_servicevm(context, 'create', 'subnet', svm_subnet)
        return self._make_subnet_dict(subnet, process_extensions=False)

    def _update_subnet_dns_nameservers(self, context, id, s):
        old_dns_list = self._get_dns_by_subnet(context, id)
        new_dns_addr_set = set(s["dns_nameservers"])
        old_dns_addr_set = set([dns['address']
                                for dns in old_dns_list])

        new_dns = list(new_dns_addr_set)
        for dns_addr in old_dns_addr_set - new_dns_addr_set:
            for dns in old_dns_list:
                if dns['address'] == dns_addr:
                    context.session.delete(dns)
        for dns_addr in new_dns_addr_set - old_dns_addr_set:
            dns = models_v2.DNSNameServer(
                address=dns_addr,
                subnet_id=id)
            context.session.add(dns)
        del s["dns_nameservers"]
        return new_dns

    def _update_subnet_host_routes(self, context, id, s):

        def _combine(ht):
            return ht['destination'] + "_" + ht['nexthop']

        old_route_list = self._get_route_by_subnet(context, id)

        new_route_set = set([_combine(route)
                             for route in s['host_routes']])

        old_route_set = set([_combine(route)
                             for route in old_route_list])

        for route_str in old_route_set - new_route_set:
            for route in old_route_list:
                if _combine(route) == route_str:
                    context.session.delete(route)
        for route_str in new_route_set - old_route_set:
            route = models_v2.SubnetRoute(
                destination=route_str.partition("_")[0],
                nexthop=route_str.partition("_")[2],
                subnet_id=id)
            context.session.add(route)

        # Gather host routes for result
        new_routes = []
        for route_str in new_route_set:
            new_routes.append(
                {'destination': route_str.partition("_")[0],
                 'nexthop': route_str.partition("_")[2]})
        del s["host_routes"]
        return new_routes

    def _update_subnet_allocation_pools(self, context, id, s):
        context.session.query(models_v2.IPAllocationPool).filter_by(
            subnet_id=id).delete()
        new_pools = [models_v2.IPAllocationPool(
            first_ip=p['start'], last_ip=p['end'],
            subnet_id=id) for p in s['allocation_pools']]
        context.session.add_all(new_pools)
        NeutronDbPluginV2._rebuild_availability_ranges(context, [s])
        #Gather new pools for result:
        result_pools = [{'start': pool['start'],
                         'end': pool['end']}
                        for pool in s['allocation_pools']]
        del s['allocation_pools']
        return result_pools

    def _notify_servicevm(self, context, action, resource, value):
        LOG.info('nofiter msg to servicevm, action:%(action)s, '
                 'resource:%(resource)s, value:%(value)s',
                 {'action':action, 'resource':resource, 'value':value})
        method = resource+'_'+action
        svm_plugin = manager.NeutronManager.get_service_plugins().get(
                     service_constants.SERVICEVM)
        if svm_plugin and hasattr(svm_plugin, method):
            getattr(svm_plugin, method)(context, value)

    def update_subnet(self, context, id, subnet):
        """Update the subnet with new info.

        The change however will not be realized until the client renew the
        dns lease or we support gratuitous DHCP offers
        """
        s = subnet['subnet']
        # NOTE(gongysh) for the purpose update the subnet with
        # data return subnet created just
        s.pop('created_at', None)
        changed_host_routes = False
        changed_dns = False
        changed_allocation_pools = False
        db_subnet = self._get_subnet(context, id)
        # Fill 'ip_version' and 'allocation_pools' fields with the current
        # value since _validate_subnet() expects subnet spec has 'ip_version'
        # and 'allocation_pools' fields.
        s['ip_version'] = db_subnet.ip_version
        s['cidr'] = db_subnet.cidr
        s['id'] = db_subnet.id
        self._validate_subnet(context, s, cur_subnet=db_subnet)

        if 'gateway_ip' in s and s['gateway_ip'] is not None:
            allocation_pools = [{'start': p['first_ip'], 'end': p['last_ip']}
                                for p in db_subnet.allocation_pools]
            self._validate_gw_out_of_pools(s["gateway_ip"], allocation_pools)

        with context.session.begin(subtransactions=True):
            if "dns_nameservers" in s:
                changed_dns = True
                new_dns = self._update_subnet_dns_nameservers(context, id, s)

            if "host_routes" in s:
                changed_host_routes = True
                new_routes = self._update_subnet_host_routes(context, id, s)

            if "allocation_pools" in s:
                self._validate_allocation_pools(s['allocation_pools'],
                                                s['cidr'])
                changed_allocation_pools = True
                old_pools = self._get_subnet_allocation_pool(context, s['id'])
                new_pools = self._update_subnet_allocation_pools(context,
                                                                 id, s)
                svm_subnet = copy.deepcopy(s)
                svm_subnet.update({'new_pools': new_pools,
                                  'old_pools': old_pools})
                self._notify_servicevm(context, 'update', 'subnet', svm_subnet)

            subnet = self._get_subnet(context, id)
            subnet.update(s)
            self.update_extra_subnet_data(context, s, subnet)

        result = self._make_subnet_dict(subnet)
        # Keep up with fields that changed
        if changed_dns:
            result['dns_nameservers'] = new_dns
        if changed_host_routes:
            result['host_routes'] = new_routes
        if changed_allocation_pools:
            result['allocation_pools'] = new_pools
        return result

    def delete_subnet(self, context, id):
        with context.session.begin(subtransactions=True):
            subnet = self._get_subnet(context, id)
            # Check if any tenant owned ports are using this subnet
            allocated = (context.session.query(models_v2.IPAllocation).
                         filter_by(subnet_id=subnet['id']).
                         join(models_v2.Port).
                         filter_by(network_id=subnet['network_id']).
                         with_lockmode('update'))

            # remove network owned ports
            for a in allocated:
                if a.ports.device_owner in AUTO_DELETE_PORT_OWNERS:
                    NeutronDbPluginV2._delete_ip_allocation(
                        context, subnet.network_id, id, a.ip_address)
                else:
                    raise n_exc.SubnetInUse(subnet_id=id)

            context.session.delete(subnet)
            svm_subnet = {}
            svm_subnet['old_pools'] = allocated

            self._notify_servicevm(context, 'delete', 'subnet', svm_subnet)

    def get_subnet(self, context, id, fields=None):
        subnet = self._get_subnet(context, id)
        return self._make_subnet_dict(subnet, fields)

    def get_subnets(self, context, filters=None, fields=None,
                    sorts=None, limit=None, marker=None,
                    page_reverse=False):
        marker_obj = self._get_marker_obj(context, 'subnet', limit, marker)
        return self._get_collection(context, models_v2.Subnet,
                                    self._make_subnet_dict,
                                    filters=filters, fields=fields,
                                    sorts=sorts,
                                    limit=limit,
                                    marker_obj=marker_obj,
                                    page_reverse=page_reverse)

    def get_subnets_count(self, context, filters=None):
        return self._get_collection_count(context, models_v2.Subnet,
                                          filters=filters)

    def create_port_bulk(self, context, ports):
        return self._create_bulk('port', context, ports)

    def create_port(self, context, port):
        p = port['port']
        servicevm_device = p.get('servicevm_device')
        service_instance_id = p.get('service_instance_id', None)
        servicevm_type = p.get('servicevm_type')
        port_id = p.get('id') or uuidutils.generate_uuid()
        network_id = p['network_id']
        # NOTE(jkoelker) Get the tenant_id outside of the session to avoid
        #                unneeded db action if the operation raises
        tenant_id = self._get_tenant_id_for_create(context, p)
        if p.get('device_owner') == constants.DEVICE_OWNER_ROUTER_INTF:
            self._enforce_device_owner_not_router_intf_or_device_id(context, p,
                                                                    tenant_id)

        with context.session.begin(subtransactions=True):
            # Ensure that the network exists.
            network = self._get_network(context, network_id)

            # NOTE(changzhi) Get port's network unmanaged_nework flag.
            # Because it is not allowed POST when create a port. So whether
            # a port's disable_anti_spoofing flag is judge by its network.
            is_unmanaged = network.unmanaged_network
            if is_unmanaged:
                port_disable_anti_spoofing = True
            else:
                port_disable_anti_spoofing = False

            # Ensure that a MAC address is defined and it is unique on the
            # network
            if p['mac_address'] is attributes.ATTR_NOT_SPECIFIED:
                #Note(scollins) Add the generated mac_address to the port,
                #since _allocate_ips_for_port will need the mac when
                #calculating an EUI-64 address for a v6 subnet
                p['mac_address'] = NeutronDbPluginV2._generate_mac(context,
                                                                   network_id)
            else:
                # Ensure that the mac on the network is unique
                if not NeutronDbPluginV2._check_unique_mac(context,
                                                           network_id,
                                                           p['mac_address']):
                    raise n_exc.MacAddressInUse(net_id=network_id,
                                                mac=p['mac_address'])

            # Returns the IP's for the port
            ips = self._allocate_ips_for_port(context, port)

            if 'status' not in p:
                status = constants.PORT_STATUS_ACTIVE
            else:
                status = p['status']

            if (p['device_owner'] and
                p['device_owner'].startswith(
                    constants.DEVICE_OWNER_COMPUTE_PRE)):
                status = constants.PORT_STATUS_BUILD

            # NOTE(changzhi) Add a new attribute named 'disable_anti_spoofing'
            # default value is False, means this port enable anti-spoofing.
            port = models_v2.Port(tenant_id=tenant_id,
                                  name=p['name'],
                                  id=port_id,
                                  network_id=network_id,
                                  mac_address=p['mac_address'],
                                  admin_state_up=p['admin_state_up'],
                                  status=status,
                                  device_id=p['device_id'],
                                  device_owner=p['device_owner'],
                                  servicevm_device=servicevm_device,
                                  service_instance_id=service_instance_id,
                                  servicevm_type=servicevm_type,
                                  disable_anti_spoofing=port_disable_anti_spoofing)
            utils.make_default_name(port, uos_constants.UOS_PRE_PORT)
            context.session.add(port)

            # Update the allocated IP's
            if ips:
                for ip in ips:
                    ip_address = ip['ip_address']
                    subnet_id = ip['subnet_id']
                    NeutronDbPluginV2._store_ip_allocation(
                        context, ip_address, network_id, subnet_id, port_id)

        return self._make_port_dict(port, process_extensions=False)

    def update_port(self, context, id, port):
        p = port['port']
        # NOTE(gongysh) for the purpose update the port with
        # data return port created just
        p.pop('created_at', None)
        changed_ips = False
        changed_device_id = False
        with context.session.begin(subtransactions=True):
            port = self._get_port(context, id)

            spoofing = p.get('binding:disable_anti_spoofing', None)

            # NOTE(changzhi) Spoofing is None when API update port's attributes
            # except 'binding:disable_anti_spoofing'. So we leave the code alone.
            # We get flag value(True or False) when API update a port's
            # binding:disable_anti_spoofing flag in function '_process_port_binding'.
            if spoofing is None:
                pass
            else:
                p['disable_anti_spoofing'] = spoofing

            if 'device_owner' in p:
                current_device_owner = p['device_owner']
                changed_device_owner = True
            else:
                current_device_owner = port['device_owner']
                changed_device_owner = False
            if p.get('device_id') != port['device_id']:
                changed_device_id = True

            device_id = p.get('device_id', None)
            device_owner = p.get('device_owner', None)
            if (device_id and device_owner and device_owner.startswith(
                constants.DEVICE_OWNER_COMPUTE_PRE)):
                p['status'] = constants.PORT_STATUS_BUILD

            # if the current device_owner is compute and the device_owner
            # will be changed (to None), we must also change the status to
            # PORT_STATUS_BUILD to avoid the end-user continues to do other
            # operations before the update status down rpc applied from agent.
            if (port.device_id and port.device_owner and
               port.device_owner.startswith(constants.DEVICE_OWNER_COMPUTE_PRE)
               and device_id in p and device_owner in p
               and device_id != port.device_id and device_owner != port.device_owner):
                p['status'] = constants.PORT_STATUS_BUILD
                LOG.debug(_("change port id %(port)s status to PORT_STATUS_BUILD"
                     " for the device_owner changed"),
                     {'port':port.id})

            # if the current device_owner is ROUTER_INF and the device_id or
            # device_owner changed check device_id is not another tenants
            # router
            if ((current_device_owner in [constants.DEVICE_OWNER_ROUTER_INTF,
                                          constants.DEVICE_OWNER_ROUTER_GW])
                    and (changed_device_id or changed_device_owner)):
                self._enforce_device_owner_not_router_intf_or_device_id(
                    context, p, port['tenant_id'], port)

            # Check if the IPs need to be updated
            if 'fixed_ips' in p:
                changed_ips = True
                original = self._make_port_dict(port, process_extensions=False)
                added_ips, prev_ips = self._update_ips_for_port(
                    context, port["network_id"], id, original["fixed_ips"],
                    p['fixed_ips'], port.device_owner)

                # Update ips if necessary
                for ip in added_ips:
                    NeutronDbPluginV2._store_ip_allocation(
                        context, ip['ip_address'], port['network_id'],
                        ip['subnet_id'], port.id)
            # Remove all attributes in p which are not in the port DB model
            # and then update the port
            port.update(self._filter_non_model_columns(p, models_v2.Port))

        result = self._make_port_dict(port)
        # Keep up with fields that changed
        if changed_ips:
            result['fixed_ips'] = prev_ips + added_ips
        return result

    def delete_port(self, context, id):
        with context.session.begin(subtransactions=True):
            self._delete_port(context, id)

    def delete_ports_by_device_id(self, context, device_id, network_id=None):
        query = (context.session.query(models_v2.Port.id)
                 .enable_eagerloads(False)
                 .filter(models_v2.Port.device_id == device_id))
        if network_id:
            query = query.filter(models_v2.Port.network_id == network_id)
        port_ids = [p[0] for p in query]
        for port_id in port_ids:
            try:
                self.delete_port(context, port_id)
            except n_exc.PortNotFound:
                # Don't raise if something else concurrently deleted the port
                LOG.debug(_("Ignoring PortNotFound when deleting port '%s'. "
                            "The port has already been deleted."),
                          port_id)

    def _delete_port(self, context, id):
        query = (context.session.query(models_v2.Port).
                 enable_eagerloads(False).filter_by(id=id))
        if not context.is_admin:
            query = query.filter_by(tenant_id=context.tenant_id)
        query.delete()

    def get_port(self, context, id, fields=None):
        port = self._get_port(context, id)
        return self._make_port_dict(port, fields)

    def _get_ports_query(self, context, filters=None, sorts=None, limit=None,
                         marker_obj=None, page_reverse=False):
        Port = models_v2.Port
        IPAllocation = models_v2.IPAllocation

        if not filters:
            filters = {}

        query = self._model_query(context, Port)

        fixed_ips = filters.pop('fixed_ips', {})
        ip_addresses = fixed_ips.get('ip_address')
        subnet_ids = fixed_ips.get('subnet_id')
        if ip_addresses or subnet_ids:
            query = query.join(Port.fixed_ips)
            if ip_addresses:
                query = query.filter(IPAllocation.ip_address.in_(ip_addresses))
            if subnet_ids:
                query = query.filter(IPAllocation.subnet_id.in_(subnet_ids))

        query = self._apply_filters_to_query(query, Port, filters)
        if limit and page_reverse and sorts:
            sorts = [(s[0], not s[1]) for s in sorts]
        query = sqlalchemyutils.paginate_query(query, Port, limit,
                                               sorts, marker_obj)
        return query

    def get_ports(self, context, filters=None, fields=None,
                  sorts=None, limit=None, marker=None,
                  page_reverse=False):
        marker_obj = self._get_marker_obj(context, 'port', limit, marker)
        query = self._get_ports_query(context, filters=filters,
                                      sorts=sorts, limit=limit,
                                      marker_obj=marker_obj,
                                      page_reverse=page_reverse)
        items = [self._make_port_dict(c, fields) for c in query]
        if limit and page_reverse:
            items.reverse()
        return items

    def get_ports_count(self, context, filters=None):
        return self._get_ports_query(context, filters).count()

    def _enforce_device_owner_not_router_intf_or_device_id(self, context,
                                                           port_request,
                                                           tenant_id,
                                                           db_port=None):
        if not context.is_admin:
            # find the device_id. If the call was update_port and the
            # device_id was not passed in we use the device_id from the
            # db.
            device_id = port_request.get('device_id')
            if not device_id and db_port:
                device_id = db_port.get('device_id')
            # check to make sure device_id does not match another tenants
            # router.
            if device_id:
                if hasattr(self, 'get_router'):
                    try:
                        ctx_admin = ctx.get_admin_context()
                        router = self.get_router(ctx_admin, device_id)
                    except l3.RouterNotFound:
                        return
                else:
                    l3plugin = (
                        manager.NeutronManager.get_service_plugins().get(
                            service_constants.L3_ROUTER_NAT))
                    if l3plugin:
                        try:
                            ctx_admin = ctx.get_admin_context()
                            router = l3plugin.get_router(ctx_admin,
                                                         device_id)
                        except l3.RouterNotFound:
                            return
                    else:
                        # raise as extension doesn't support L3 anyways.
                        raise n_exc.DeviceIDNotOwnedByTenant(
                            device_id=device_id)
                if tenant_id != router['tenant_id']:
                    raise n_exc.DeviceIDNotOwnedByTenant(device_id=device_id)

    # hxn add for servicevm
    def get_sync_ports(self, context, filters):
        Port = models_v2.Port
        IPAllocation = models_v2.IPAllocation

        query = self._model_query(context, Port)
        if 'device_ids' in filters:
            query = query.filter(Port.device_id.in_(filters['device_ids']))
        if 'subnet_ids' in filters:
            query = query.filter(IPAllocation.subnet_id.in_(filters['subnet_ids']))
        if 'ids' in filters:
            query = query.filter(Port.id.in_(filters['ids']))
        items = [self._make_port_dict(c) for c in query]
        return items

