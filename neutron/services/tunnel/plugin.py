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

from neutron.db.tunnel import tunnel_db
from neutron.db import agents_db
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants
from neutron.services import service_base

LOG = logging.getLogger(__name__)


class TunnlePlugin(tunnel_db.TunnelPluginDb):

    """Implementation of the Tunnel Service Plugin.

    This class manages the workflow of TUNNELEaaS request/response.
    Most DB related works are implemented in class
    tunnel_db.TunnelPluginDb.
    """
    supported_extension_aliases = ["tunnelaas", "service-type", "agent"]


class TunnelDriverPlugin(TunnlePlugin, tunnel_db.TunnelPluginRpcDbMixin,
        agents_db.AgentDbMixin):
    """VpnPlugin which supports VPN Service Drivers."""
    #TODO(WeiW) handle tunnel update usecase
    def __init__(self):
        super(TunnlePlugin, self).__init__()
        # Load the service driver from neutron.conf.
        drivers, default_provider = service_base.load_drivers(
            constants.TUNNEL, self)
        LOG.info(_("Tunnel plugin using service driver: %s"), default_provider)
        self.gre_driver = drivers[default_provider]

    def _get_driver_for_gre(self, tunnel_mode):
        return self.gre_driver

    def _get_driver_for_tunnel(self, context, tunnel=None, tunnel_id=None):
        #TODO(WeiW) get tunnel mode when we support service type framework
        tunnel_mode = None
        return self._get_driver_for_gre(tunnel_mode)

    def _get_validator(self):
        return self.gre_driver.validator

    def create_tunnel(self, context, tunnel):
        tunnel = super(TunnlePlugin, self).create_tunnel(context, tunnel)
        driver = self._get_driver_for_tunnel(context, tunnel=tunnel)
        driver.create_tunnel(context, tunnel)
        return tunnel

    def delete_tunnel(self, context, tunnel_id):
        tunnel = self.get_tunnel(
            context, tunnel_id)
        super(TunnlePlugin, self).delete_tunnel(
            context, tunnel_id)
        driver = self._get_driver_for_tunnel(
            context, tunnel=tunnel)
        driver.delete_tunnel(context, tunnel)

    def update_tunnel(self, context, tunnel_id, tunnel):
        old_tunnel = self.get_tunnel(
            context, tunnel_id)
        tunnel = super(
            TunnlePlugin, self).update_tunnel(
                context,
                tunnel_id,
                tunnel)
        driver = self._get_driver_for_tunnel(
            context, tunnel=tunnel)
        driver.update_tunnel(
            context, old_tunnel, tunnel)
        return tunnel

    def create_tunnel_connection(self, context, tunnel_connection):
        tunnel_conn = super(
            TunnlePlugin, self).create_tunnel_connection(context,
                tunnel_connection)
        driver = self._get_driver_for_tunnel(
            context, tunnel_id=tunnel_conn['tunnel_id'])
        driver.create_tunnel_connection(context, tunnel_conn)
        return tunnel_conn

    def delete_tunnel_connection(self, context, tunnel_connection_id):
        tunnel_conn = self.get_tunnel_connection(
            context, tunnel_connection_id)
        super(TunnlePlugin, self).delete_tunnel_connection(
            context, tunnel_connection_id)
        driver = self._get_driver_for_tunnel(
            context, tunnel_id=tunnel_conn['tunnel_id'])
        driver.delete_tunnel_connection(context, tunnel_conn)

    def create_target_network(self, context, target_network):
        target_network = super(
            TunnlePlugin, self).create_target_network(context, target_network)
        driver = self._get_driver_for_tunnel(
            context, tunnel_id=target_network['tunnel_id'])
        driver.create_target_network(context, target_network)
        return target_network

    def delete_target_network(self, context, target_network_id):
        target_network = self.get_target_network(
            context, target_network_id)
        super(TunnlePlugin, self).delete_target_network(
            context, target_network_id)
        driver = self._get_driver_for_tunnel(
            context, tunnel_id=target_network['tunnel_id'])
        driver.delete_target_network(context, target_network)
