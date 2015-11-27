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

BASE_PPTP_VERSION = '1.0'


class PPTPVpnDriverCallBack(object):
    """Callback for PPTP rpc."""

    # history
    #   1.0 Initial version

    RPC_API_VERSION = BASE_PPTP_VERSION

    def __init__(self, driver):
        self.driver = driver

    def create_rpc_dispatcher(self):
        return [self]

    def get_vpn_services_on_host(self, context, routers=[], host=None):
        """Retuns the vpnservices on the host."""
        plugin = self.driver.service_plugin
        vpnservices = plugin.get_vpn_services_on_host(
            context, routers, host, vpn_connstants.PPTP)
        return vpnservices

    def get_vpn_users(self, context, tenant_ids):
        """Retuns the vpnusers of a tenant."""
        plugin = self.driver.service_plugin
        vpnusers = plugin.get_vpnusers(
            context, filters={'tenant_id': tenant_ids})
        return vpnusers

    def update_status(self, context, status):
        """Update status of vpnservices."""
        plugin = self.driver.service_plugin
        plugin.update_status_by_agent(context, status, vpn_connstants.PPTP)


class PPTPVpnAgentApi(n_rpc.RpcProxy):
    """Agent RPC API for IPsecVPNAgent."""

    RPC_API_VERSION = BASE_PPTP_VERSION

    def _agent_notification(self, context, method, pptpconnection,
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
            adminContext, [pptpconnection['router_id']],
            admin_state_up=True)
        for l3_agent in l3_agents:
            LOG.debug(_('Notify agent at %(topic)s.%(host)s the message '
                        '%(method)s'),
                      {'topic': topics.PPTP_AGENT_TOPIC,
                       'host': l3_agent.host,
                       'method': method})
            self.cast(
                context, self.make_msg(method,
                                       router_id=pptpconnection['router_id']),
                version=version,
                topic='%s.%s' % (topics.PPTP_AGENT_TOPIC, l3_agent.host))

    def vpnservice_created(self, context, pptpconnection):
        """Send update event of pptpservice_created."""
        self._agent_notification(context, 'vpnservice_created', pptpconnection)

    def vpnservice_updated(self, context, pptpconnection):
        """Send update event of pptpservice_updated."""
        self._agent_notification(context, 'vpnservice_updated', pptpconnection)

    def vpnservice_deleted(self, context, pptpconnection):
        """Send update event of pptpservice_deleted."""
        self._agent_notification(context, 'vpnservice_deleted',
                                 pptpconnection)

    def vpnuser_updated(self, context):
        """Send update event of user changed."""
        self.fanout_cast(
            context, self.make_msg('vpnuser_updated',
                                   tenant_id=context.tenant_id),
            version=self.RPC_API_VERSION,
            topic='%s' % (topics.PPTP_AGENT_TOPIC))


class PPTPVPNDriver(service_drivers.VpnDriver):
    """VPN Service Driver class for IPsec."""

    def __init__(self, service_plugin):
        self.callbacks = PPTPVpnDriverCallBack(self)
        self.service_plugin = service_plugin
        self.conn = n_rpc.create_connection(new=True)
        self.conn.create_consumer(
            topics.PPTP_DRIVER_TOPIC,
            self.callbacks.create_rpc_dispatcher(),
            fanout=False)
        self.conn.consume_in_threads()
        self.agent_rpc = PPTPVpnAgentApi(
            topics.PPTP_AGENT_TOPIC, BASE_PPTP_VERSION)

    @property
    def service_type(self):
        return vpn_connstants.PPTP

    def create_vpnservice(self, context, pptp_connection):
        self.agent_rpc.vpnservice_created(
            context, pptp_connection)

    def update_vpnservice(self, context,
                          old_pptp_connection, pptp_connection):
        self.agent_rpc.vpnservice_updated(
            context, pptp_connection)

    def delete_vpnservice(self, context, pptp_connection):
        self.agent_rpc.vpnservice_deleted(
            context, pptp_connection)

    def notify_user_change(self, context):
        self.agent_rpc.vpnuser_updated(context)

    def create_ipsec_site_connection(self, context, ipsec_site_connection):
        pass

    def update_ipsec_site_connection(self, context, old_ipsec_site_connection,
                                     ipsec_site_connection):
        pass

    def delete_ipsec_site_connection(self, context, ipsec_site_connection):
        pass
