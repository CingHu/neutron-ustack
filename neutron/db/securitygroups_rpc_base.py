# Copyright 2012, Nachi Ueno, NTT MCL, Inc.
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

import netaddr
from sqlalchemy.orm import exc

from neutron.common import constants as q_const
from neutron.common import ipv6_utils as ipv6
from neutron.common import utils
from neutron.db import allowedaddresspairs_db as addr_pair
from neutron.db import models_v2
from neutron.db import securitygroups_db as sg_db
from neutron.extensions import securitygroup as ext_sg
from neutron.openstack.common import log as logging

LOG = logging.getLogger(__name__)


IP_MASK = {q_const.IPv4: 32,
           q_const.IPv6: 128}


DIRECTION_IP_PREFIX = {'ingress': 'source_ip_prefix',
                       'egress': 'dest_ip_prefix'}


class SecurityGroupServerRpcMixin(sg_db.SecurityGroupDbMixin):
    """Mixin class to add agent-based security group implementation."""

    def get_port_from_device(self, device):
        """Get port dict from device name on an agent.

        Subclass must provide this method.

        :param device: device name which identifies a port on the agent side.
        What is specified in "device" depends on a plugin agent implementation.
        For example, it is a port ID in OVS agent and netdev name in Linux
        Bridge agent.
        :return: port dict returned by DB plugin get_port(). In addition,
        it must contain the following fields in the port dict returned.
        - device
        - security_groups
        - security_group_rules,
        - security_group_source_groups
        - fixed_ips
        """
        raise NotImplementedError(_("%s must implement get_port_from_device.")
                                  % self.__class__.__name__)

    def create_security_group_rule(self, context, security_group_rule):
        bulk_rule = {'security_group_rules': [security_group_rule]}
        rule = self.create_security_group_rule_bulk_native(context,
                                                           bulk_rule)[0]
        sgids = [rule['security_group_id']]
        self.notifier.security_groups_rule_updated(context, sgids)
        return rule

    def create_security_group_rule_bulk(self, context,
                                        security_group_rule):
        rules = super(SecurityGroupServerRpcMixin,
                      self).create_security_group_rule_bulk_native(
                          context, security_group_rule)
        sgids = set([r['security_group_id'] for r in rules])
        self.notifier.security_groups_rule_updated(context, list(sgids))
        return rules

    def delete_security_group_rule(self, context, sgrid):
        rule = self.get_security_group_rule(context, sgrid)
        super(SecurityGroupServerRpcMixin,
              self).delete_security_group_rule(context, sgrid)
        self.notifier.security_groups_rule_updated(context,
                                                   [rule['security_group_id']])

    def update_security_group_on_port(self, context, id, port,
                                      original_port, updated_port):
        """Update security groups on port.

        This method returns a flag which indicates request notification
        is required and does not perform notification itself.
        It is because another changes for the port may require notification.
        """
        need_notify = False
        port_updates = port['port']
        if (ext_sg.SECURITYGROUPS in port_updates and
            not utils.compare_elements(
                original_port.get(ext_sg.SECURITYGROUPS),
                port_updates[ext_sg.SECURITYGROUPS])):
            # delete the port binding and read it with the new rules
            port_updates[ext_sg.SECURITYGROUPS] = (
                self._get_security_groups_on_port(context, port))
            self._delete_port_security_group_bindings(context, id)
            self._process_port_create_security_group(
                context,
                updated_port,
                port_updates[ext_sg.SECURITYGROUPS])
            need_notify = True
        else:
            updated_port[ext_sg.SECURITYGROUPS] = (
                original_port[ext_sg.SECURITYGROUPS])
        return need_notify

    def is_security_group_member_updated(self, context,
                                         original_port, updated_port):
        """Check security group member updated or not.

        This method returns a flag which indicates request notification
        is required and does not perform notification itself.
        It is because another changes for the port may require notification.
        """
        need_notify = False
        if (original_port['fixed_ips'] != updated_port['fixed_ips'] or
            not utils.compare_elements(
                original_port.get(ext_sg.SECURITYGROUPS),
                updated_port.get(ext_sg.SECURITYGROUPS))):
            need_notify = True
        return need_notify

    def notify_security_groups_member_updated(self, context, port):
        """Notify update event of security group members.

        The agent setups the iptables rule to allow
        ingress packet from the dhcp server (as a part of provider rules),
        so we need to notify an update of dhcp server ip
        address to the plugin agent.
        security_groups_provider_updated() just notifies that an event
        occurs and the plugin agent fetches the update provider
        rule in the other RPC call (security_group_rules_for_devices).
        """
        if port['device_owner'] == q_const.DEVICE_OWNER_DHCP:
            self.notifier.security_groups_provider_updated(context)
        # For IPv6, provider rule need to be updated in case router
        # interface is created or updated after VM port is created.
        elif port['device_owner'] == q_const.DEVICE_OWNER_ROUTER_INTF:
            if any(netaddr.IPAddress(fixed_ip['ip_address']).version == 6
                   for fixed_ip in port['fixed_ips']):
                self.notifier.security_groups_provider_updated(context)
        else:
            self.notifier.security_groups_member_updated(
                context, port.get(ext_sg.SECURITYGROUPS))

    def _select_rules_for_ports(self, context, ports):
        if not ports:
            return []
        sg_binding_port = sg_db.SecurityGroupPortBinding.port_id
        sg_binding_sgid = sg_db.SecurityGroupPortBinding.security_group_id

        sgr_sgid = sg_db.SecurityGroupRule.security_group_id

        query = context.session.query(sg_db.SecurityGroupPortBinding,
                                      sg_db.SecurityGroupRule)
        query = query.join(sg_db.SecurityGroupRule,
                           sgr_sgid == sg_binding_sgid)
        query = query.filter(sg_binding_port.in_(ports.keys()))
        return query.all()

    def _select_ips_for_remote_group(self, context, remote_group_ids):
        ips_by_group = {}
        if not remote_group_ids:
            return ips_by_group
        for remote_group_id in remote_group_ids:
            ips_by_group[remote_group_id] = set()

        ip_port = models_v2.IPAllocation.port_id
        sg_binding_port = sg_db.SecurityGroupPortBinding.port_id
        sg_binding_sgid = sg_db.SecurityGroupPortBinding.security_group_id

        # Join the security group binding table directly to the IP allocation
        # table instead of via the Port table skip an unnecessary intermediary
        query = context.session.query(sg_binding_sgid,
                                      models_v2.IPAllocation.ip_address,
                                      addr_pair.AllowedAddressPair.ip_address)
        query = query.join(models_v2.IPAllocation,
                           ip_port == sg_binding_port)
        # Outerjoin because address pairs may be null and we still want the
        # IP for the port.
        query = query.outerjoin(
            addr_pair.AllowedAddressPair,
            sg_binding_port == addr_pair.AllowedAddressPair.port_id)
        query = query.filter(sg_binding_sgid.in_(remote_group_ids))
        # Each allowed address pair IP record for a port beyond the 1st
        # will have a duplicate regular IP in the query response since
        # the relationship is 1-to-many. Dedup with a set
        for security_group_id, ip_address, allowed_addr_ip in query:
            ips_by_group[security_group_id].add(ip_address)
            if allowed_addr_ip:
                ips_by_group[security_group_id].add(allowed_addr_ip)
        return ips_by_group

    def _select_remote_group_ids(self, ports):
        remote_group_ids = []
        for port in ports.values():
            for rule in port.get('security_group_rules'):
                remote_group_id = rule.get('remote_group_id')
                if remote_group_id:
                    remote_group_ids.append(remote_group_id)
        return remote_group_ids

    def _select_network_ids(self, ports):
        return set((port['network_id'] for port in ports.values()))

    def _select_dhcp_ips_for_network_ids(self, context, network_ids):
        if not network_ids:
            return {}
        query = context.session.query(models_v2.Port,
                                      models_v2.IPAllocation.ip_address)
        query = query.join(models_v2.IPAllocation)
        query = query.filter(models_v2.Port.network_id.in_(network_ids))
        owner = q_const.DEVICE_OWNER_DHCP
        query = query.filter(models_v2.Port.device_owner == owner)
        ips = {}

        for network_id in network_ids:
            ips[network_id] = []

        for port, ip in query:
            ips[port['network_id']].append(ip)
        return ips

    def _select_ra_ips_for_network_ids(self, context, network_ids):
        """Select IP addresses to allow sending router advertisement from.

        If OpenStack dnsmasq sends RA, get link local address of
        gateway and allow RA from this Link Local address.
        The gateway port link local address will only be obtained
        when router is created before VM instance is booted and
        subnet is attached to router.

        If OpenStack doesn't send RA, allow RA from gateway IP.
        Currently, the gateway IP needs to be link local to be able
        to send RA to VM.
        """
        if not network_ids:
            return {}
        ips = {}
        for network_id in network_ids:
            ips[network_id] = set([])
        query = context.session.query(models_v2.Subnet)
        subnets = query.filter(models_v2.Subnet.network_id.in_(network_ids))
        for subnet in subnets:
            gateway_ip = subnet['gateway_ip']
            if subnet['ip_version'] != 6 or not gateway_ip:
                continue
            if not netaddr.IPAddress(gateway_ip).is_link_local():
                if subnet['ipv6_ra_mode']:
                    gateway_ip = self._get_lla_gateway_ip_for_subnet(context,
                                                                     subnet)
                else:
                    # TODO(xuhanp):Figure out how to allow gateway IP from
                    # existing device to be global address and figure out the
                    # link local address by other method.
                    continue
            if gateway_ip:
                ips[subnet['network_id']].add(gateway_ip)

        return ips

    def _get_lla_gateway_ip_for_subnet(self, context, subnet):
        query = context.session.query(models_v2.Port)
        query = query.join(models_v2.IPAllocation)
        query = query.filter(
            models_v2.IPAllocation.subnet_id == subnet['id'])
        query = query.filter(
            models_v2.IPAllocation.ip_address == subnet['gateway_ip'])
        query = query.filter(models_v2.Port.device_owner ==
                             q_const.DEVICE_OWNER_ROUTER_INTF)
        try:
            gateway_port = query.one()
        except (exc.NoResultFound, exc.MultipleResultsFound):
            LOG.warn(_('No valid gateway port on subnet %s is '
                       'found for IPv6 RA'), subnet['id'])
            return
        mac_address = gateway_port['mac_address']
        lla_ip = str(ipv6.get_ipv6_addr_by_EUI64(
            q_const.IPV6_LLA_PREFIX,
            mac_address))
        return lla_ip

    def _convert_remote_group_id_to_ip_prefix(self, context, ports):
        remote_group_ids = self._select_remote_group_ids(ports)
        ips = self._select_ips_for_remote_group(context, remote_group_ids)
        for port in ports.values():
            updated_rule = []
            for rule in port.get('security_group_rules'):
                remote_group_id = rule.get('remote_group_id')
                direction = rule.get('direction')
                direction_ip_prefix = DIRECTION_IP_PREFIX[direction]
                if not remote_group_id:
                    updated_rule.append(rule)
                    continue

                port['security_group_source_groups'].append(remote_group_id)
                base_rule = rule
                for ip in ips[remote_group_id]:
                    if ip in port.get('fixed_ips', []):
                        continue
                    ip_rule = base_rule.copy()
                    version = netaddr.IPNetwork(ip).version
                    ethertype = 'IPv%s' % version
                    if base_rule['ethertype'] != ethertype:
                        continue
                    ip_rule[direction_ip_prefix] = str(
                        netaddr.IPNetwork(ip).cidr)
                    updated_rule.append(ip_rule)
            port['security_group_rules'] = updated_rule
        return ports

    def _add_ingress_dhcp_rule(self, port, ips):
        dhcp_ips = ips.get(port['network_id'])
        for dhcp_ip in dhcp_ips:
            if not netaddr.IPAddress(dhcp_ip).version == 4:
                return

            dhcp_rule = {'direction': 'ingress',
                         'ethertype': q_const.IPv4,
                         'protocol': 'udp',
                         'port_range_min': 68,
                         'port_range_max': 68,
                         'source_port_range_min': 67,
                         'source_port_range_max': 67}
            dhcp_rule['source_ip_prefix'] = "%s/%s" % (dhcp_ip,
                                                       IP_MASK[q_const.IPv4])
            port['security_group_rules'].append(dhcp_rule)

    def _add_ingress_ra_rule(self, port, ips):
        ra_ips = ips.get(port['network_id'])
        for ra_ip in ra_ips:
            if not netaddr.IPAddress(ra_ip).version == 6:
                return

            ra_rule = {'direction': 'ingress',
                       'ethertype': q_const.IPv6,
                       'protocol': q_const.PROTO_NAME_ICMP_V6,
                       'source_ip_prefix': ra_ip,
                       'source_port_range_min': q_const.ICMPV6_TYPE_RA}
            port['security_group_rules'].append(ra_rule)

    def _apply_provider_rule(self, context, ports):
        network_ids = self._select_network_ids(ports)
        ips_dhcp = self._select_dhcp_ips_for_network_ids(context, network_ids)
        ips_ra = self._select_ra_ips_for_network_ids(context, network_ids)
        for port in ports.values():
            self._add_ingress_ra_rule(port, ips_ra)
            self._add_ingress_dhcp_rule(port, ips_dhcp)

    def security_group_rules_for_ports(self, context, ports):
        rules_in_db = self._select_rules_for_ports(context, ports)
        for (binding, rule_in_db) in rules_in_db:
            port_id = binding['port_id']
            port = ports[port_id]
            direction = rule_in_db['direction']
            rule_dict = {
                'security_group_id': rule_in_db['security_group_id'],
                'direction': direction,
                'ethertype': rule_in_db['ethertype'],
            }
            for key in ('protocol', 'port_range_min', 'port_range_max',
                        'remote_ip_prefix', 'remote_group_id'):
                if rule_in_db.get(key):
                    if key == 'remote_ip_prefix':
                        direction_ip_prefix = DIRECTION_IP_PREFIX[direction]
                        rule_dict[direction_ip_prefix] = rule_in_db[key]
                        continue
                    rule_dict[key] = rule_in_db[key]
            port['security_group_rules'].append(rule_dict)
        self._apply_provider_rule(context, ports)
        return self._convert_remote_group_id_to_ip_prefix(context, ports)
