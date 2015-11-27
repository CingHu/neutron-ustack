
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
#    (c) Copyright 2014 UnitedStack Inc.
#    All Rights Reserved.
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
#
# @author: Yong Sheng Gong, UnitedStack Inc.

from neutron.db.vpn import pptp_vpn_db
from neutron.db.vpn import openvpn_db
from neutron.db.vpn import vpnuser_db
from neutron.db.vpn import vpn_db
from neutron.services.vpn.common import constants as vpn_connstants
from neutron.services.vpn.service_drivers import pptp
from neutron.services.vpn.service_drivers import ipsec
from neutron.services.vpn.service_drivers import openvpn

class VPNPlugin(pptp_vpn_db.PPTPDVPNDbMixin,
                openvpn_db.OpenVPNDbMixin,
                vpnuser_db.VPNUserNDbMixin,
                vpn_db.VPNPluginDb,
                vpn_db.VPNPluginRpcDbMixin,
                pptp_vpn_db.VPNPluginRpcDbMixin,
                openvpn_db.OpenVPNPluginRpcDbMixin):

    """Implementation of the VPN Service Plugin db.

    This class manages the workflow of VPNaaS request/response.
    Most DB related works are implemented in class
    vpn_db.VPNPluginDb.
    """
    supported_extension_aliases = ["vpn_user", "pptp_vpnaas","openvpn_vpnaas", "vpnaas", "service-type"]

    def __init__(self):
        """Do the initialization for the vpn service plugin here."""
        self.pptp_driver = pptp.PPTPVPNDriver(self)
        self.ipsec_driver = ipsec.IPsecVPNDriver(self)
        self.openvpn_driver = openvpn.OpenVPNDriver(self)
        self.vpnuser_notifier = self
        self.get_vpn_services_funs = {}
        self.get_vpn_services_funs[vpn_connstants.PPTP] = (
            self._get_vpn_services_by_routers)
        self.get_vpn_services_funs[vpn_connstants.IPSEC] = (
            vpn_db.VPNPluginRpcDbMixin._get_agent_hosting_vpn_services)
        self.get_vpn_services_funs[vpn_connstants.OPENVPN] = (
            openvpn_db.OpenVPNPluginRpcDbMixin()._get_vpn_services_by_routers)
        self.update_status_by_agent_funs = {}
        self.update_status_by_agent_funs[vpn_connstants.PPTP] = (
            self._update_status_by_agent)
        self.update_status_by_agent_funs[vpn_connstants.IPSEC] = (
            self.update_ipsec_status_by_agent)
        self.update_status_by_agent_funs[vpn_connstants.OPENVPN] = (
            openvpn_db.OpenVPNPluginRpcDbMixin()._update_status_by_agent)
        self.check_for_dup_router_subnet_funs= {}
        self.check_for_dup_router_subnet_funs[vpn_connstants.PPTP] = (
             pptp_vpn_db.PPTPDVPNDbMixin().check_for_dup_router_subnet)
        self.check_for_dup_router_subnet_funs[vpn_connstants.OPENVPN] = (
             openvpn_db.OpenVPNDbMixin().check_for_dup_router_subnet)
        self.check_router_in_use_funcs= {}
        self.check_router_in_use_funcs[vpn_connstants.PPTP] = (
             pptp_vpn_db.PPTPDVPNDbMixin().check_router_in_use)
        self.check_router_in_use_funcs[vpn_connstants.OPENVPN] = (
             openvpn_db.OpenVPNDbMixin().check_router_in_use)

    def get_vpn_services_on_host(self, context, routers,
                                 host, driver_type):
        _func = self.get_vpn_services_funs.get(driver_type)
        if _func:
            return _func(context, routers, host=host)

    def update_status_by_agent(self, context, status, driver_type):
        _func = self.update_status_by_agent_funs.get(driver_type)
        if _func:
            return _func(context, status)

    def notify_user_change(self, context):
        self.pptp_driver.notify_user_change(context)

    def check_router_in_use(self, context, router_id):
        for driver_type in [vpn_connstants.PPTP, vpn_connstants.OPENVPN]:
            _func = self.check_router_in_use_funcs.get(driver_type)
            if _func:
                _func(context, router_id)

    def check_for_dup_router_subnet(self, context, router_id,
                                    subnet_id, subnet_cidr):
        for driver_type in [vpn_connstants.PPTP, vpn_connstants.OPENVPN]:
            _func = self.check_for_dup_router_subnet_funs.get(driver_type)
            if _func:
                _func(context, router_id, subnet_id, subnet_cidr)

    def _get_driver_for_vpnservice(self, vpnservice):
        return self.ipsec_driver

    def _get_driver_for_ipsec_site_connection(self, context,
                                              ipsec_site_connection):
        #TODO(nati) get vpnservice when we support service type framework
        vpnservice = None
        return self._get_driver_for_vpnservice(vpnservice)

    def _get_validator(self):
        return self.ipsec_driver.validator

    def create_ipsec_site_connection(self, context, ipsec_site_connection):
        ipsec_site_connection = super(
            VPNPlugin, self).create_ipsec_site_connection(
                context, ipsec_site_connection)
        driver = self._get_driver_for_ipsec_site_connection(
            context, ipsec_site_connection)
        driver.create_ipsec_site_connection(context, ipsec_site_connection)
        return ipsec_site_connection

    def delete_ipsec_site_connection(self, context, ipsec_conn_id):
        ipsec_site_connection = self.get_ipsec_site_connection(
            context, ipsec_conn_id)
        super(VPNPlugin, self).delete_ipsec_site_connection(
            context, ipsec_conn_id)
        driver = self._get_driver_for_ipsec_site_connection(
            context, ipsec_site_connection)
        driver.delete_ipsec_site_connection(context, ipsec_site_connection)

    def update_ipsec_site_connection(
            self, context,
            ipsec_conn_id, ipsec_site_connection):
        old_ipsec_site_connection = self.get_ipsec_site_connection(
            context, ipsec_conn_id)
        ipsec_site_connection = super(
            VPNPlugin, self).update_ipsec_site_connection(
                context,
                ipsec_conn_id,
                ipsec_site_connection)
        driver = self._get_driver_for_ipsec_site_connection(
            context, ipsec_site_connection)
        driver.update_ipsec_site_connection(
            context, old_ipsec_site_connection, ipsec_site_connection)
        return ipsec_site_connection

    def update_vpnservice(self, context, vpnservice_id, vpnservice):
        old_vpn_service = self.get_vpnservice(context, vpnservice_id)
        new_vpn_service = super(
            VPNPlugin, self).update_vpnservice(context, vpnservice_id,
                                                     vpnservice)
        driver = self._get_driver_for_vpnservice(old_vpn_service)
        driver.update_vpnservice(context, old_vpn_service, new_vpn_service)
        return new_vpn_service

    def delete_vpnservice(self, context, vpnservice_id):
        vpnservice = self._get_vpnservice(context, vpnservice_id)
        super(VPNPlugin, self).delete_vpnservice(context, vpnservice_id)
        driver = self._get_driver_for_vpnservice(vpnservice)
        driver.delete_vpnservice(context, vpnservice)
