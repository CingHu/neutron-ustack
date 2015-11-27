# Copyright 2015 Intel Corporation.
# Copyright 2014 Isaku Yamahata <isaku.yamahata at intel com>
#                               <isaku.yamahata at gmail com>
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

import logging

from oslo import messaging
from neutron.common import rpc as rpc_compat
from neutron.common import topics
from neutron.db import agents_db
from neutron.services.vm.mgmt_drivers import constants
from neutron.common import rpc as n_rpc
from neutron.services.vm.common import topics as n_topics
from neutron.services.vm.rpc import device_svmagent_rpc_cb as svm_rpc
from neutron.services.vm.mgmt_drivers import abstract_driver

LOG = logging.getLogger(__name__)


class ServiceVMAgentRpcApi(rpc_compat.RpcProxy):
    BASE_RPC_API_VERSION = '1.0'

    def __init__(self, topic=topics.SERVICEVM_AGENT):
        super(ServiceVMAgentRpcApi, self).__init__(
            topic=topic, default_version=self.BASE_RPC_API_VERSION)
    #cinghu
    def rpc_cast(self, context, method, kwargs):
        self.cast(context, self.make_msg(method, **kwargs))


class ServiceVMPluginRpcCallbacks(svm_rpc.DeviceSVMRpcCallbackMixin):

    target = messaging.Target(version='1.0')

    def __init__(self, servicevm_plugin, notifier):
        super(ServiceVMPluginRpcCallbacks, self).__init__()
        self._svm_plugin = servicevm_plugin
        self.notifier = notifier 

    @property
    def _core_plugin(self):
        return manager.NeutronManager.get_plugin()

class ServiceVMAgentNotifyApi(n_rpc.RpcProxy):
    BASE_RPC_API_VERSION = '1.0'

    def __init__(self, topic=topics.SERVICEVM_AGENT):
        super(ServiceVMAgentNotifyApi, self).__init__(
            topic=topic, default_version=self.BASE_RPC_API_VERSION)
        self.topic_subnet_delete = topics.get_topic_name(topic,
                                                  topics.SUBNET,
                                                  topics.DELETE)
        self.topic_subnet_create = topics.get_topic_name(topic,
                                                  topics.SUBNET,
                                                  topics.CREATE)
        self.topic_subnet_update = topics.get_topic_name(topic,
                                                  topics.SUBNET,
                                                  topics.UPDATE)
    def subnet_delete(self, context, subnet):
        self.fanout_cast(context,
                         self.make_msg('subnet_delete',
                                        subnet_dict=subnet),
                         topic=self.topic_subnet_delete)
    def subnet_create(self, context, subnet):
        self.fanout_cast(context,
                         self.make_msg('subnet_create',
                                        subnet_dict=subnet),
                         topic=self.topic_subnet_create)

    def subnet_update(self, context, subnet):
        self.fanout_cast(context,
                         self.make_msg('subnet_update',
                                        subnet_dict=subnet),
                         topic=self.topic_subnet_update)
    #cinghu
    def rpc_cast(self, context, method, kwargs):
        self.cast(context, self.make_msg(method, **kwargs))

# TODO(yamahata): port this to oslo.messaging
#                 address format needs be changed to
#                 oslo.messaging.target.Target
class AgentRpcMGMTDriver(abstract_driver.DeviceMGMTServiceNetwork):
    _BASE_SERVICEVM_VERSION = 1.0
    _TOPIC = topics.SERVICEVM_AGENT     # can be overridden by subclass
    _RPC_API = {}       # topic -> ServiceVMAgentRpcApi

    def __init__(self):
        super(AgentRpcMGMTDriver, self).__init__()
        self.topic = n_topics.SVMDEVICE_DRIVER_TOPIC
        self.notifier = ServiceVMAgentNotifyApi(topics.SERVICEVM)
        self.endpoints = [ServiceVMPluginRpcCallbacks(self, self.notifier),
                          agents_db.AgentExtRpcCallback()]
        self.conn = n_rpc.create_connection(new=True)
        self.conn.create_consumer(
            self.topic, self.endpoints, fanout=False)
        self.conn.consume_in_threads()

    @property
    def _rpc_api(self):
        topic = self._TOPIC
        api = self._RPC_API.get(topic)
        if api is None:
            api = ServiceVMAgentRpcApi(topic=topic)
        return api

    def get_type(self):
        return 'agent-rpc'

    def get_name(self):
        return 'agent-rpc'

    def get_description(self):
        return 'agent-rpc'

    def mgmt_get_config(self, plugin, context, device):
        return {'/etc/neutron/servicevm-agent.ini':
                '[servicevm]\n'
                'topic = %s\n'
                'device_id = %s\n'
                % (self._TOPIC, device['id'])}

    @staticmethod
    def _address(topic, server):
        return '%s.%s' % (topic, server)

    def _mgmt_server(self, device):
        return device['id']

    def _mgmt_topic(self, device):
        return '%s-%s' % (self._TOPIC, self._mgmt_server(device))

    def mgmt_url(self, plugin, context, device):
        return super(AgentRpcMGMTDriver, self).mgmt_url(plugin, context, device)

    def mgmt_call(self, plugin, context, device, kwargs):
        method = kwargs[constants.KEY_ACTION]
        kwargs_ = kwargs[constants.KEY_KWARGS]
        self._rpc_api.rpc_cast(context, method, kwargs_)

    def _mgmt_service_server(self, device, service_instance):
        return '%s-%s' % (device['id'], service_instance['id'])

    def _mgmt_service_topic(self, device, service_instance):
        return '%s-%s' % (self._TOPIC,
                          self._mgmt_service_server(device, service_instance))

    def mgmt_service_address(self, plugin, context, device, service_instance):
        return self.mgmt_url(plugin, context, device)

    def mgmt_service_call(self, plugin, context, device,
                          service_instance, kwargs):
        method = kwargs[constants.KEY_ACTION]
        kwargs_ = kwargs[constants.KEY_KWARGS]
        self._rpc_api.rpc_cast(context, method, kwargs_)

    def mgmt_msg_fanout(self, plugin, context, kwargs):
        method = kwargs[constants.KEY_ACTION]
        kwargs_ = kwargs[constants.KEY_KWARGS]
        if hasattr(self.notifier, method):
            getattr(self.notifier, method)(context, **kwargs_)
