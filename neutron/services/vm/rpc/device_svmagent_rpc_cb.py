# Copyright 2014 Cisco Systems, Inc.  All rights reserved.
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

from oslo.serialization import jsonutils

from neutron import manager
from neutron import context as neutron_context
from neutron.common import rpc as n_rpc
from neutron.extensions import servicevm
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants

LOG = logging.getLogger(__name__)

BASE_GRE_VERSION = '1.0'

class DeviceSVMRpcCallbackBase(n_rpc.RpcCallback):
    def __init__(self):
        super(DeviceSVMRpcCallbackBase, self).__init__()
   
    @property
    def l3_plugin(self):
        return manager.NeutronManager.get_service_plugins().get(
            constants.L3_ROUTER_NAT)

    @property
    def core_plugin(self):
        return manager.NeutronManager.get_plugin()
 
    @property
    def service_plugin(self):
        return manager.NeutronManager.get_service_plugins().get(
            constants.SERVICEVM)

class DeviceSVMRpcCallbackMixin(DeviceSVMRpcCallbackBase):
    """Mixin for ServiceVm agent device reporting rpc support."""

    RPC_API_VERSION = BASE_GRE_VERSION

    def __init__(self):
        super(DeviceSVMRpcCallbackMixin, self).__init__()

    def register_for_duty(self, context, host):
        """Report that ServiceVM agent is ready for duty.

        This function is supposed to be called when the agent has started,
        is ready to take on assignments and before any callbacks to fetch
        logical resources are issued.

        @param: context - contains user information
        @param: host - originator of callback
        @return: True if successfully registered, False if not successfully
                 registered, None if no handler found
                 If unsuccessful the agent should retry registration a few
                 seconds later
        """
        #TODO
        LOG.warn("agent register host, %s" % host)
        pass
        # schedule any non-handled devices
        #return self._l3plugin.auto_schedule_hosting_devices(context, host)

    def get_devices_on_host(self, context, host):
        # NOTE(changzhi)
        """Get all devices from neutron-servicevm plugin. """
        return self.service_plugin.get_devices_on_host(context, host)

    def register_agent_devices(self, context, resources, host):
        LOG.warn("Register agent devices, %s host %s", resources, host)
        self.service_plugin.register_agent_devices(context, resources, host)

    def get_service_instances(self, context, service_instances_id=None, device_ids=None):
        """Get service instances"""
        return self.service_plugin.get_service_instances(context, service_instances_id, device_ids)

    def get_devices_details_list(self, context, devices=None, host=None):
        """Get service instances"""
        return self.service_plugin.get_devices_details_list(context, devices, host)

    def get_devices_info_by_host(self, context, host):
        """Get devices info by host"""
        return self.service_plugin.get_devices_info_by_host(context, host)

    def callback_call(self, context, plugin, method, **kwargs):
        n_method = self.get(plugin).get(method, None)
        if not n_method: 
            raise RPCNotFonundMethod(plugin=self.get(plugin), method=n_method)
       
        return n_method(context, **kwargs)

    def sync_service_instances(self, context, host,
                         service_instance_ids=None,
                         device_ids=None):
        """Sync routers according to filters to a specific Cisco cfg agent.

        @param context: contains user information
        @param host - originator of callback
        @param service_instance_ids- list of service instance ids to return information about
        @param device_ids - list of hosting device ids to get
        routers for.
        @return: a list of service_instances
                 with their hosting devices, interfaces and floating_ips
        """
        context = neutron_context.get_admin_context()
        try:
            service_instances = (
                self.service_plugin.sync_service_instances(
                    context, host, service_instance_ids, device_ids))
        except AttributeError:
            service_instances = []
        LOG.debug('Service Instances returned to servicevm agent@%(agt)s:\n %(routers)s',
                  {'agt': host, 'service_instances': jsonutils.dumps(service_instances, indent=5)})
        return service_instances


    def sync_service_instance_ids(self, context, host,
                                  device_ids=None):
        context = neutron_context.get_admin_context()
        try:
            service_instance_ids = (
                self.service_plugin.sync_service_instance_ids(
                    context, host, device_ids))
        except AttributeError:
            service_instance_ids = []
        LOG.debug('Service Instances returned to servicevm agent@%(agt)s:\n %(routers)s',
                  {'agt': host, 'service_instances': jsonutils.dumps(service_instance_ids, indent=5)})
        return service_instance_ids
