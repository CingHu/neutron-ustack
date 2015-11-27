#
# Copyright 2014, yong sheng gong Unitedstack inc.
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

from neutron.common import rpc as n_rpc
from neutron import manager
from neutron.openstack.common import log as logging
#from neutron.openstack.common import rpc
from neutron.plugins.common import constants
from neutron.services.vpn.common import constants as vpn_connstants
from neutron.services.vpn.common import topics
from neutron.services.vpn import service_drivers


LOG = logging.getLogger(__name__)

BASE_OPENVPN_VERSION = '1.0'


class OpenVPNDriverCallBack(object):
    """Callback for OpenVPN rpc."""

    # history
    #   1.0 Initial version

    RPC_API_VERSION = BASE_OPENVPN_VERSION

    def __init__(self, driver):
        self.driver = driver

    def create_rpc_dispatcher(self):
        return [self]

    def get_vpn_services_on_host(self, context, routers=[], host=None):
        """Retuns the vpnservices on the host."""
        plugin = self.driver.service_plugin
        vpnservices = plugin.get_vpn_services_on_host(
            context, routers, host, vpn_connstants.OPENVPN)
        return vpnservices

    def update_status(self, context, status):
        """Update status of vpnservices."""
        plugin = self.driver.service_plugin
        plugin.update_status_by_agent(context, status, vpn_connstants.OPENVPN)


class OpenVPNAgentApi(n_rpc.RpcProxy):
    """Agent RPC API for OpenVPNAgent."""

    RPC_API_VERSION = BASE_OPENVPN_VERSION

    def _agent_notification(self, context, method, openvpn_connection,
                            version=None):
        """Notify update for the agent.

        This method will find where is the router, and
        dispatch notification for the agent.
        """
        adminContext = context.is_admin and context or context.elevated()
        plugin = manager.NeutronManager.get_service_plugins().get(
            constants.L3_ROUTER_NAT)
        if not version:
            version = self.RPC_API_VERSION
        l3_agents = plugin.get_l3_agents_hosting_routers(
            adminContext, [openvpn_connection['router_id']],
            admin_state_up=True)
        for l3_agent in l3_agents:
            LOG.debug(_('Notify agent at %(topic)s.%(host)s the message '
                        '%(method)s'),
                      {'topic': topics.OPENVPN_AGENT_TOPIC,
                       'host': l3_agent.host,
                       'method': method})
            self.cast(
                context, self.make_msg(method,
                                       router_id=openvpn_connection['router_id']),
                version=version,
                topic='%s.%s' % (topics.OPENVPN_AGENT_TOPIC, l3_agent.host))

    def vpnservice_created(self, context, openvpn_connection):
        """Send update event of openvpnservice_created."""
        self._agent_notification(context, 'vpnservice_created', openvpn_connection)

    def vpnservice_updated(self, context, openvpn_connection):
        """Send update event of openvpnservice_updated."""
        self._agent_notification(context, 'vpnservice_updated', openvpn_connection)

    def vpnservice_deleted(self, context, openvpn_connection):
        """Send update event of openvpn_service_deleted."""
        self._agent_notification(context, 'vpnservice_deleted',
                                 openvpn_connection)


class OpenVPNDriver(service_drivers.VpnDriver):
    """VPN Service Driver class for OpenVPN."""

    def __init__(self, service_plugin):
        self.callbacks = OpenVPNDriverCallBack(self)
        self.service_plugin = service_plugin
        self.conn = n_rpc.create_connection(new=True)
        self.conn.create_consumer(
            topics.OPENVPN_DRIVER_TOPIC,
            self.callbacks.create_rpc_dispatcher(),
            fanout=False)
        self.conn.consume_in_threads()
        self.agent_rpc = OpenVPNAgentApi(
            topics.OPENVPN_AGENT_TOPIC, BASE_OPENVPN_VERSION)


    @property
    def service_type(self):
        return vpn_connstants.OPENVPN

    def create_vpnservice(self, context, openvpn_connection):
        self.agent_rpc.vpnservice_created(
            context, openvpn_connection)

    def update_vpnservice(self, context,
                          old_openvpn_connection, openvpn_connection):
        self.agent_rpc.vpnservice_updated(
            context, openvpn_connection)

    def delete_vpnservice(self, context, openvpn_connection):
        self.agent_rpc.vpnservice_deleted(
            context, openvpn_connection)

    def create_ipsec_site_connection(self, context, ipsec_site_connection):
        pass

    def update_ipsec_site_connection(self, context, old_ipsec_site_connection,
                                     ipsec_site_connection):
        pass

    def delete_ipsec_site_connection(self, context, ipsec_site_connection):
        pass
