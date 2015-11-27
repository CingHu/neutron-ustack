# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright 2013, 2014 Intel Corporation.
# Copyright 2013, 2014 Isaku Yamahata <isaku.yamahata at intel com>
#                                     <isaku.yamahata at gmail com>
# All Rights Reserved.
#
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
# @author: Isaku Yamahata, Intel Corporation.

import abc
import six

from oslo.config import cfg

from neutron.api import extensions
from neutron.agent.common import config
from neutron.common import constants as l3_constants
from neutron.extensions import servicevm
from neutron.openstack.common import log as logging
from neutron.openstack.common import jsonutils
from neutron.services.vm import constants
from neutron import context as n_context

LOG = logging.getLogger(__name__)

MGMT_SUBNET = l3_constants.SERVICEVM_OWNER_MGMT

@six.add_metaclass(abc.ABCMeta)
class DeviceMGMTAbstractDriver(extensions.PluginInterface):

    @abc.abstractmethod
    def get_type(self):
        """Return one of predefined type of the hosting device drivers."""
        pass

    @abc.abstractmethod
    def get_name(self):
        """Return a symbolic name for the service VM plugin."""
        pass

    @abc.abstractmethod
    def get_description(self):
        pass

    def mgmt_create_pre(self, plugin, context, device):
        pass

    def mgmt_create_post(self, plugin, context, device):
        pass

    def mgmt_update_pre(self, plugin, context, device):
        pass

    def mgmt_update_post(self, plugin, context, device):
        pass

    def mgmt_delete_pre(self, plugin, context, device):
        pass

    def mgmt_delete_post(self, plugin, context, device):
        pass

    def mgmt_get_config(self, plugin, context, device):
        """
        returns dict of file-like objects which will be passed to hosting
        device.
        It depends on drivers how to use it.
        for nova case, it can be used for meta data, file injection or
        config drive
        i.e.
        metadata case: nova --meta <key>=<value>
        file injection case: nova --file <dst-path>:<src-path>
        config drive case: nova --config-drive=true --file \
                                <dst-path>:<src-path>
        """
        return {}

    @abc.abstractmethod
    def mgmt_url(self, plugin, context, device):
        pass

    @abc.abstractmethod
    def mgmt_call(self, plugin, context, device, kwargs):
        pass

    def mgmt_service_driver(self, plugin, context, device, service_instance):
        # use same mgmt driver to communicate with service
        return self.get_name()

    def mgmt_service_create_pre(self, plugin, context, device,
                                service_instance):
        pass

    def mgmt_service_create_post(self, plugin, context, device,
                                 service_instance):
        pass

    def mgmt_service_update_pre(self, plugin, context, device,
                                service_instance):
        pass

    def mgmt_service_update_post(self, plugin, context, device,
                                 service_instance):
        pass

    def mgmt_service_delete_pre(self, plugin, context, device,
                                service_instance):
        pass

    def mgmt_service_delete_post(self, plugin, context, device,
                                 service_instance):
        pass

    @abc.abstractmethod
    def mgmt_service_address(self, plugin, context, device, service_instance):
        pass

    @abc.abstractmethod
    def mgmt_service_call(self, plugin, context, device,
                          service_instance):
        pass

#    @abc.abstractmethod
#    def attach_interface(self, plugin, context, device,
#                          service_instance, kwargs):
#        pass
#
    #@abc.abstractmethod
    #def detach_interface(self, plugin, context, device,
    #                      service_instance, kwargs):
    #    pass


class DeviceMGMTByNetwork(DeviceMGMTAbstractDriver):
    def mgmt_url(self, plugin, context, device):
        mgmt_entries = [sc_entry for sc_entry in device.service_context
                        if (sc_entry.role == constants.ROLE_MGMT and
                            sc_entry.port_id)]
        if not mgmt_entries:
            return
        port = plugin._core_plugin.get_port(context, mgmt_entries[0].port_id)
        if not port:
            return
        mgmt_url = port['fixed_ips'][0]     # subnet_id and ip_address
        mgmt_url['network_id'] = port['network_id']
        mgmt_url['port_id'] = port['id']
        mgmt_url['mac_address'] = port['mac_address']
        return jsonutils.dumps(mgmt_url)

    def mgmt_service_address(self, plugin, context, device, service_instance):
        mgmt_entries = [sc_entry for sc_entry
                        in service_instance.service_context
                        if (sc_entry.role == constants.ROLE_MGMT and
                            sc_entry.port_id)]
        if not mgmt_entries:
            return
        port = plugin._core_plugin.get_port(context, mgmt_entries[0].port_id)
        if not port:
            return
        mgmt_url = port['fixed_ips'][0]     # subnet_id and ip_address
        mgmt_url['network_id'] = port['network_id']
        mgmt_url['port_id'] = port['id']
        mgmt_url['mac_address'] = port['mac_address']
        return jsonutils.dumps(mgmt_url)

class DeviceMGMTServiceNetwork(DeviceMGMTAbstractDriver):
    def mgmt_url(self, plugin, context, device): 
        mgmt_url = dict()
        admin_context = n_context.get_admin_context()
        filters={'servicevm_device':[device['id']]}
        ports = plugin._core_plugin.get_ports(admin_context, filters=filters)
        port_list = [port for port in ports if port['servicevm_type'] == MGMT_SUBNET]
        if not port_list:
            raise servicevm.NoAvaliableMgmtport(device_id=device['id'])

        LOG.info("the mgmt url info for device_id %s,"
                  " %s" % (device['id'], port_list))

        mgmt_url = port_list[0]['fixed_ips'][0]     # subnet_id and ip_address
        mgmt_url['network_id'] = port_list[0]['network_id']
        mgmt_url['port_id'] = port_list[0]['id']
        mgmt_url['mac_address'] = port_list[0]['mac_address']

        return mgmt_url

    def mgmt_service_address(self, plugin, context, device, service_instance):
        return service_instance.get('mgmt_url', None) or device['mgmt_url']
