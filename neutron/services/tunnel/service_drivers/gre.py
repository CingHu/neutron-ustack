# Copyright (c) 2015 UnitedStack Inc.
# All rights reserved.
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

from neutron.common import rpc as n_rpc
from neutron.db import agents_db
from neutron.extensions import tunnelaas
from neutron.openstack.common import log as logging
from neutron.services.tunnel.common import topics
from neutron.services.tunnel import service_drivers


LOG = logging.getLogger(__name__)

GRE = 'GRE'
BASE_GRE_VERSION = '1.0'


class GRETunnelDriverCallBack(n_rpc.RpcCallback):
    """Callback for GRE Tunnel Driver rpc."""

    # history
    #   1.0 Initial version

    RPC_API_VERSION = BASE_GRE_VERSION

    def __init__(self, driver):
        super(GRETunnelDriverCallBack, self).__init__()
        self.driver = driver

    def get_agent_host_tunnels(self, context, host, tunnels):
        """Returns the tunnels on the host."""
        plugin = self.driver.service_plugin
        result = plugin._get_agent_hosting_tunnels(
            context, host, tunnels)
        return [self.driver.make_tunnel_dict(context, tunnel)
                for tunnel in result]

    def update_status(self, context, status):
        """Update status of tunnels."""
        plugin = self.driver.service_plugin
        plugin.update_status_by_agent(context, status)


class GRETunnelAgentApi(service_drivers.BaseGRETunnelAgentApi,
                        n_rpc.RpcCallback):
    """Agent RPC API for call GreTunnelAgent."""

    RPC_API_VERSION = BASE_GRE_VERSION

    def __init__(self, topic, default_version, driver):
        super(GRETunnelAgentApi, self).__init__(
            topic, default_version, driver)


class GRETunnelDriver(service_drivers.TunnelDriver):
    """Tunnel Service Driver class for GRE."""

    def __init__(self, service_plugin):
        super(GRETunnelDriver, self).__init__(service_plugin)
        self.endpoints = [GRETunnelDriverCallBack(self),
                agents_db.AgentExtRpcCallback(self.service_plugin)]
        self.conn = n_rpc.create_connection(new=True)
        self.conn.create_consumer(
            topics.GRE_DRIVER_TOPIC, self.endpoints, fanout=False)
        self.conn.consume_in_threads()
        self.agent_rpc = GRETunnelAgentApi(
            topics.GRE_AGENT_TOPIC, BASE_GRE_VERSION, self)

    @property
    def service_type(self):
        return GRE

    def create_tunnel(self, context, tunnel):
        pass

    def update_tunnel(self, context, old_tunnel, tunnel):
        pass

    def delete_tunnel(self, context, tunnel):
        pass

    def create_tunnel_connection(self, context, tunnel_connection):
        tunnel = self.service_plugin._get_tunnel(
            context, tunnel_connection['tunnel_id'])
        router_id = tunnel['router_id']
        self.agent_rpc.tunnel_updated(context, router_id,
                tunnel_id=tunnel['id'])

    def delete_tunnel_connection(self, context, tunnel_connection):
        tunnel = self.service_plugin._get_tunnel(
            context, tunnel_connection['tunnel_id'])
        router_id = tunnel['router_id']
        self.agent_rpc.tunnel_updated(context, router_id,
                tunnel_id=tunnel['id'])

    def update_tunnel_connection(self, context, old_tunnel_conn, tunnel_conn):
        pass

    def create_target_network(self, context, target_network):
        tunnel = self.service_plugin._get_tunnel(
            context, target_network['tunnel_id'])
        router_id = tunnel['router_id']
        self.agent_rpc.tunnel_updated(context, router_id,
                tunnel_id=tunnel['id'])

    def delete_target_network(self, context, target_network):
        tunnel = self.service_plugin._get_tunnel(
            context, target_network['tunnel_id'])
        router_id = tunnel['router_id']
        self.agent_rpc.tunnel_updated(context, router_id,
                tunnel_id=tunnel['id'])

    def update_target_network(self, context, old_target_network,
                              target_network):
        pass

    def make_tunnel_dict(self, context, tunnel):
        """Convert tunnel information for tunnel agent.

        The input is SQLAlchemy query.
        also converting parameter name for tunnel agent driver
        """
        tunnel_dict = dict(tunnel)
        router = tunnel.router
        gw_port_id = router['gw_port_id']

        # Get floatingip on router
        local_ip = self.l3_plugin.get_floatingips(
                context, filters={'fixed_port_id':[gw_port_id]})
        if len(local_ip) == 0:
            LOG.error(_("Can not find fip on router %s !"), router['id'])
            return

        # Get router interface port for l2 tunnel
        if tunnel['type'] == 2:
            ri_port_ids = self.core_plugin._get_ports_query(context, filters={
                'device_id': [router.id],
                'device_owner': ['network:router_interface']})
            for port in ri_port_ids:
                for fixed_ip in port.fixed_ips:
                    if fixed_ip.subnet_id == tunnel_dict['local_subnet']:
                        ri_port_id = port.id
                        ri_port_ip = fixed_ip.ip_address
                        break
            tunnel_dict['ri_port_id'] = ri_port_id
            mask = self.core_plugin.get_subnet(
                context, tunnel_dict['local_subnet'])['cidr'].split('/')[1]
            tunnel_dict['ri_port_ip'] = ri_port_ip + '/' + mask

        tunnel_dict['id'] = tunnel['id']
        tunnel_dict['type'] = tunnel['type']
        tunnel_dict['router_id'] = tunnel['router_id']
        tunnel_dict['gw_port_id'] = gw_port_id
        tunnel_dict['local_ip'] = local_ip[0]['floating_ip_address']
        tunnel_dict['tunnel_connections'] = {}
        tunnel_dict['target_networks'] = {}
        for tunnel_conn in tunnel.tunnel_connections:
            tunnel_dict['tunnel_connections'][tunnel_conn['id']] = dict(
                    tunnel_conn)
        for target_network in tunnel.target_networks:
            tunnel_dict['target_networks'][target_network['id']] = dict(
                    target_network)
        return tunnel_dict
