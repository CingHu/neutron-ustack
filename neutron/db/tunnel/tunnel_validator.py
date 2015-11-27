#    Copyright (c) 2015 UnitedStack Inc.
#    All rights reserved.
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
# @author: Wei Wang, UnitedStack

import netaddr

from neutron.db import l3_db
from neutron.extensions import tunnelaas
from neutron import manager
from neutron.plugins.common import constants


class TunnelReferenceValidator(object):
    """Baseline validation routines for tunnel resources.

    This validator will validate routines which we can't validate in
    extension's validator:

    For tunnels:
    1. Check the local_subnet is already connect to router
        if tunnel type is l2;
    2. Check the router doesn't has any tunnel if tunnel type is l2;
    3. Check the router doesn't has any l2 tunnel if tunnel type is l3;
    4. Check whether the router already has bind a public ip;
    5. Check the router is already exists;

    For target networks:
    1. Check target networks are not overlap between each other;
    2. Check target networks are not overlap to router's connected subnet;
    3. Check the tunnel is already exists;

    For tunnel connections:
    1. Check whether the tunnel is already exists;
    2. Check the tunnel doesn't has any connection if tunnel type is l2;
    """


    @property
    def l3_plugin(self):
        try:
            return self._l3_plugin
        except AttributeError:
            self._l3_plugin = manager.NeutronManager.get_service_plugins().get(
                constants.L3_ROUTER_NAT)
            return self._l3_plugin

    @property
    def core_plugin(self):
        try:
            return self._core_plugin
        except AttributeError:
            self._core_plugin = manager.NeutronManager.get_plugin()
            return self._core_plugin

    @property
    def tunnel_plugin(self):
        try:
            return self._tunnel_plugin
        except AttributeError:
            self._tunnel_plugin = \
                manager.NeutronManager.get_service_plugins().get(
                constants.TUNNEL)
            return self._tunnel_plugin

    def _check_router(self, context, router_id, local_subnet_id,
                      tunnel_mode, tunnel_type):
        if tunnel_mode != 'gre':
            raise NotImplementedError

        # Check #5
        router = self.l3_plugin.get_router(context, router_id)

        # Check #2 and #3
        # TODO(WeiW): Add member tunnel to router
        tunnels = router.get('tunnels')
        if tunnel_type == 2 and tunnels:
            raise tunnelaas.ConflictWithL2Tunnel
        if tunnel_type == 3:
            for tunnel in tunnels:
                t = self.tunnel_plugin.get_tunnel(context, tunnel)
                if t['type'] == 2:
                    raise tunnelaas.ConflictWithOtherTunnel

        # Check #4
        if not(router['gw_port_id'] and self.l3_plugin.get_floatingips(
                context, filters={'fixed_port_id':[router['gw_port_id']]})):
            raise tunnelaas.NoGatewayOrFIPFound

        if tunnel_type == 3 and local_subnet_id:
            raise tunnelaas.DoNotNeedLocalSubnetInL3
        elif tunnel_type == 3:
            return
        elif tunnel_type == 2 and not local_subnet_id:
            raise tunnelaas.NoLocalSubnetFound()
        # Check #1
        # Note(WeiW): Call private method is not beautiful but most convenient
        ports = self.core_plugin._get_ports_query(context, filters={
            'device_id': [router_id],
            'device_owner': ['network:router_interface']})
        for port in ports:
            if any(fixedip.subnet_id == local_subnet_id
                   for fixedip in port.fixed_ips):
                return
        raise tunnelaas.NoCorrectInterfaceFound

    def _check_overlap(self, cidrs, cidr):
        """ Check a network cidr is whether overlap with other cidrs
        """

        cidr = netaddr.IPNetwork(cidr)
        for c in cidrs:
            c = netaddr.IPNetwork(c)
            if cidr in c or c in cidr:
                raise tunnelaas.NetworkCIDRConflict

    def validate_tunnel(self, context, tunnel):
        self._check_router(context, tunnel['router_id'],
                           tunnel['local_subnet'],
                           tunnel['mode'],
                           tunnel['type'])

    def validate_tunnel_connection(self, context, tunnel_conn):
        tunnel = self.tunnel_plugin.get_tunnel(context, tunnel_conn['tunnel_id'])
        if tunnel['type'] == 2:
            return
        elif self.tunnel_plugin.get_tunnel_connections(
            context,
            filters={'tunnel_id':[tunnel_conn['tunnel_id']]}):
            raise tunnelaas.TunnelConnectionExists

    def validate_target_network(self, context, target_network):
        tunnel = self.tunnel_plugin.get_tunnel(context, target_network['tunnel_id'])
        if tunnel['type'] == 2:
            raise tunnelaas.DoNotNeedTargetNetworkInL2

        # Check #1
        target_networks = self.tunnel_plugin.get_target_networks(
            context,
            filters={'tunnel_id': [target_network['tunnel_id']]})
        cidrs = []
        for tn in target_networks:
            cidrs.append(tn['network_cidr'])
        self._check_overlap(cidrs, target_network['network_cidr'])

        #Check #2
        router_id = tunnel['router_id']
        ports = self.core_plugin.get_ports(context, filters={
            'device_id': [router_id],
            'device_owner': ['network:router_interface']})
        cidrs = []
        for port in ports:
            subnets= [p['subnet_id'] for p in port['fixed_ips']]
            map(lambda s: cidrs.append(self.core_plugin.get_subnet(context,
                    s)['cidr']), subnets)
        self._check_overlap(cidrs, target_network['network_cidr'])
