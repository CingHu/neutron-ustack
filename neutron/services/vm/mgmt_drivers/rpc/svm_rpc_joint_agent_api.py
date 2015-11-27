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
from neutron.common import rpc as n_rpc 
from neutron.common import topics
from neutron.db import agents_db
from neutron.services.vm.mgmt_drivers import constants
from neutron.common import rpc as n_rpc
from neutron.services.vm.common import topics as n_topics
from neutron.services.vm.common import constants
from neutron.services.vm.rpc import device_svmagent_rpc_cb as svm_rpc
from neutron.services.vm.mgmt_drivers import abstract_driver


LOG = logging.getLogger(__name__)

class ServiceVMJointAgentNotifyAPI(object):
    """API for plugin to notify servicevm agent."""

    def __init__(self, sevicevm_plugin):
        self._sevicevm_plugin = sevicevm_plugin 
        self.topic = constants.SERVICEVM_AGENT_NOTIFY
        target = messaging.Target(topic=self.topic, version='1.0')
        self.client = n_rpc.get_client(target)

    def _agent_notification(self, context, method, resource_value, operation, data):
        """Notify individual servicevm ."""
        admin_context = context if context.is_admin else context.elevated()
        for r in resource:
            agents = self._sevicevm_plugin.get_servicevm_agents(
                              admin_context, admin_state_up=True,
                              active=True, reserved=True)
            for agent in agents:
                LOG.debug('Notify %(agent_type)s at %(topic)s.%(host)s the '
                          'message %(method)s',
                          {'agent_type': agent.agent_type,
                           'topic':constants.SERVICEVM_AGENT_NOTIFY, 
                           'host': agent.host,
                           'method': method})
                cctxt = self.client.prepare(server=agent.host)
                cctxt.cast(context, method, resource_value=s)

    def subnet_deleted(self, context, subnets):
        """Notifies agents about a deleted subnet."""
        self._agent_notification(context, 'subnet_deleted', subnets,
                                 operation=None, data=None)

    def subnet_updated(self, context, subnets, operation=None, data=None):
        """Notifies agents about configuration changes to subnet.
        """
        self._agent_notification(context, 'subnet_updated', subnets,
                                 operation, data)

    def subnet_created(self, context, subnets):
        """Notifies agents about a created subnet."""
        self._agent_notification(context, 'subnet_created', subnets,
                                 operation=None, data=None)

class ServiceVMAgentNotifyApi(n_rpc.RpcProxy):
    BASE_RPC_API_VERSION = '1.0'

    def __init__(self, topic=topics.SERVICEVM_AGENT):
        super(ServiceVMAgentRpcApi, self).__init__(
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
    def subnet_delete(self, context, subnet_id):
        self.fanout_cast(context,
                         self.make_msg('subnet_delete',
                                       subnet_id=subnet_id),
                         topic=self.topic_subnet_delete)
    def subnet_create(self, context, subnet_id):
        self.fanout_cast(context,
                         self.make_msg('subnet_create',
                                       subnet_id=subnet_id),
                         topic=self.topic_subnet_create)

    def subnet_update(self, context, subnet_id):
        self.fanout_cast(context,
                         self.make_msg('subnet_update',
                                       subnet_id=subnet_id),
                         topic=self.topic_subnet_update)
    #cinghu
    def rpc_cast(self, context, method, kwargs):
        self.cast(context, self.make_msg(method, **kwargs))

