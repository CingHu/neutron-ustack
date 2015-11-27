# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright 2013 UnitedStack, Inc.
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
#
# @author: huxining, UnitedStack, Inc

import eventlet
import netaddr
import sys
import time

from oslo.config import cfg

from neutron.openstack.common import periodic_task
from neutron import context as n_context
from neutron.agent.linux import ip_lib
from neutron.agent.linux import ovs_lib
from neutron.agent.linux import utils
from neutron.common import uos_constants
from neutron.common import exceptions as qexception
from neutron.openstack.common import excutils
from neutron.openstack.common import log as logging
from neutron.openstack.common.gettextutils import _
from neutron import context
from neutron.services.vm.agent import driver_mgt
from neutron.agent.linux import utils as linux_utils
from neutron.services.vm.agent.device_drivers.hillstone.hillstone_client\
    import HillStoneRestClient
from neutron.services.vm.agent.qos import qos_driver
from neutron.services.vm.agent.ovs import ovs_driver

LOG = logging.getLogger('hillstone')

DEFAULT_VROUTER_NAME = 'vr-trust'

class DriverException(qexception.NeutronException):
    """Exception created by the Driver class."""

class DeviceNotConnection(DriverException):
    message = _("device init fail")

class Hillstone():

#    _instance = None
#    def __new__(cls, **device_params):
#        if not cls._instance:
#            cls._instance = super(Hillstone, cls).__new__(cls)
#        return cls._instance

    def __init__(self, **device_params):
        self.mgmt_url = device_params['mgmt_url']
        self.auth = device_params['auth']
        self.root_helper = cfg.CONF.AGENT.root_helper
        self._pool = eventlet.GreenPool()
        self.init_flag = True
        self.init_device()

    def spawn_n(self, function, *args, **kwargs):
        self._pool.spawn_n(function, *args, **kwargs)

    def init_device(self):
        self._pool.spawn_n(self._init_device)

    def _init_device(self):
        self.qos_driver = qos_driver.QosDriver()
        self.ovs_driver = ovs_driver.OVSDriver()

        while(self.init_flag):
            try:
                self.client = HillStoneRestClient(self.mgmt_url['ip_address'])
                self._create_common_user(self.auth)
            except Exception as e:
                LOG.error('init device fail, mgmt_url:%s, auth:%s',
                          self.mgmt_url['ip_address'], self.auth)
                LOG.error(e)
                self.init_flag = True
                time.sleep(10)
                continue

            LOG.info('device %s init completly', self.mgmt_url['ip_address'])
            self.init_flag = False

    def _create_common_user(self, auth):
       if not auth:
           return
       for user in auth:
           self.client.create_operator_user(user['username'],
                                            user['password'])

    ###### Public Functions ########
    def router_added(self, ri):
        router_data = {}
        router_data = {'name': ri.router_name}
        self.client.add_router(router_data)

    def router_removed(self, ri):
        router_data = {}
        router_data = {'name': ri.router_name}
        self.client.delete_router(router_data)

    def internal_network_added(self, ri, port):
        cidr = netaddr.IPNetwork(port['subnet']['cidr']).netmask
        driver_ip = {'mac':port['mac_address'],
                     'address': port['fixed_ips'][0]['ip_address'],
                     'router_name':ri.router_name,
                     'netmask': str(cidr)}
        LOG.info('add internal network interface, %s' % driver_ip)
        self.client.set_ip_address_to_interface(driver_ip)

    def internal_network_removed(self, ri, port):
        pass

    def external_gateway_added(self, ri, ex_gw_fips, ex_gw_port):
        """1. create subinterface
           2. config fip_address
           3. add default_route
        """
        driver_ips = []
        mgmt_ip = self.mgmt_url['ip_address']
        cidr = ex_gw_port['extra_subnets'][0]['cidr']
        netmask = netaddr.IPNetwork(cidr).netmask
        gateway = ex_gw_port['extra_subnets'][0]['gateway_ip']
        for ex_gw in ex_gw_fips:
            driver_ip = {
                         'mac':ex_gw_port['mac_address'],
                         'netmask': str(netmask),
                         'address': ex_gw['floating_ip_address'],
                         'router_name':ri.router_name,
                         'destination': '0.0.0.0',
                         'gateway': gateway,
                         'mask': cidr.split("/")[1]}
            driver_ips.append(driver_ip)

        for driver_ip in driver_ips:
            self.client.set_ip_address_to_interface(driver_ip)
        if driver_ips:
            self.client.set_default_route(driver_ips)

    def external_gateway_removed(self, ri, ex_gw_fips, ex_gw_port):
        """1. remove subinterface
           2. remove fip_address
           3. remove default_route
        """
        interface = {'mac': ex_gw_port['mac_address'],
                     'address': '0',
                     'netmask': '0'
                    }
        self.client.set_ip_address_to_interface(interface)

    def enable_internal_network_NAT(self, ri, port, ex_gw_port,
                                    ex_gw_fips):
        """
           1. add snat rule
        """
        snat_data = []
        policy_data = []
        mgmt_ip = self.mgmt_url['ip_address']
        for ex_gw in ex_gw_fips:
            cidr = port['subnet']['cidr']
            snat = {'trans_to': '%s/32' % ex_gw['floating_ip_address'],
                    'mac_address':ex_gw_port['mac_address'],
                    'router_name':ri.router_name,
                    'from': cidr}
            policy = {'ip':cidr.split('/')[0],
                      'netmask':cidr.split('/')[1]}
            snat_data.append(snat)
            policy_data.append(policy)
            self.qos_driver.add_floatingip_qos(
                                  ex_gw['floating_ip_address'],
                                  ex_gw['rate_limit'], ex_gw_port['id'])
            self.ovs_driver.add_floatingip_filter(ex_gw_port['id'],
                                                  ex_gw['floating_ip_address'])
        if snat_data and policy_data:
            self.client.add_snat_rule(snat_data)
            self.client.add_snat_policy_rule(policy_data)

    def disable_internal_network_NAT(self, ri, port, ex_gw_port,
                                     ex_gw_fips):
        """
           1. delete snat rule
        """
        snat_data = []
        policy_data = []
        mgmt_ip = self.mgmt_url['ip_address']
        cidr = port['subnet']['cidr']
        snat = { 'mac_address':ex_gw_port['mac_address'],
                'router_name':ri.router_name,
                'from': cidr}
        policy = {'ip':cidr.split('/')[0],
                  'netmask':cidr.split('/')[1]}
        snat_data.append(snat)
        policy_data.append(policy)
        self.client.delete_snat_rule(snat_data)
        self.client.del_snat_policy_rule(policy_data)
        self.qos_driver.del_floatingip_qos(
                               ex_gw_fips[0]['floating_ip_address'],
                               ex_gw_port['id'])
        self.ovs_driver.del_floatingip_filter(ex_gw_port['id'],
                               ex_gw_fips[0]['floating_ip_address'])

    def floating_ip_added(self, ri, ex_gw_port,
                          floating_ip, rate_limit, fixed_ip):
        mgmt_ip = self.mgmt_url['ip_address']
        dnat = {'trans_to': '%s/32' % fixed_ip,
                'mac_address':ex_gw_port['mac_address'],
                'router_name':ri.router_name,
                'to': '%s/32' % floating_ip}
        policy_data = [{'ip':floating_ip, 'netmask':32}]
        self.client.add_dnat_rule([dnat])
        self.client.add_dnat_policy_rule(policy_data)
        self.qos_driver.add_floatingip_qos(floating_ip, rate_limit,
                                            ex_gw_port['id'])
        self.ovs_driver.add_floatingip_filter(ex_gw_port['id'], floating_ip)

    def floating_ip_removed(self, ri, ex_gw_port,
                           floating_ip, fixed_ip):
        mgmt_ip = self.mgmt_url['ip_address']
        dnat = {'trans_to': '%s/32' % fixed_ip,
                'mac_address':ex_gw_port['mac_address'],
                'router_name':ri.router_name,
                'to': '%s/32' % floating_ip}
        policy_data = [{'ip':floating_ip, 'netmask':32}]
        self.client.del_dnat_policy_rule(policy_data)
        self.client.delete_dnat_rule([dnat])
        self.qos_driver.del_floatingip_qos(floating_ip, ex_gw_port['id'])
        self.ovs_driver.del_floatingip_filter(ex_gw_port['id'], floating_ip)

    def routes_updated(self, ri, action, route):
        "Do not implement"
        pass
