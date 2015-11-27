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
# @author: CingHu, UnitedStack, Inc

import abc
import eventlet
eventlet.monkey_patch()
import pprint
import six
import sys
import time

from oslo.config import cfg
from oslo import messaging


from neutron.openstack.common import log as logging

LOG = logging.getLogger(__name__)


@six.add_metaclass(abc.ABCMeta)
class ServiceHelperBase(object):

    def __init__(self):
        self._observers = []

    def register(self, observer):
        LOG.debug("Attaching observer: %(ob)s to subject: %(sub)s",
                  {'ob': observer.__class__.__name__,
                   'sub': self.__class__.__name__})
        if observer not in self._observers:
            self._observers.append(observer)
        else:
            raise ValueError(_("Observer: %(ob)s is already registered to "
                             "subject: %(sub)s"),
                             {'ob': observer.__class__.__name__,
                              'sub': self.__class__.__name__})

    def unregister(self, observer):
        LOG.debug("Dettaching observer: %(ob)s from subject: %(sub)s",
                  {'ob': observer.__class__.__name__,
                   'sub': self.__class__.__name__})
        if observer in self._observers:
            self._observers.remove(observer)
        else:
            raise ValueError(_("Observer: %(ob)s is not attached to "
                               "subject: %(sub)s"),
                             {'ob': observer.__class__.__name__,
                              'sub': self.__class__.__name__})

    def notify(self, resource, **kwargs):
        """Calls all observers attached to the given subject."""
        LOG.debug("Notifying all observers of this subject")
        for observer in self._observers:
            LOG.debug("Notifying observer: %s", observer.__class__.__name__)
            observer.update(resource, **kwargs)

    def update(self, resource, **kwargs):
        """For future support."""
        LOG.debug("Update received")

    @abc.abstractmethod
    def process_service(self, *args, **kwargs):
        raise NotImplementedError


class ServiceVMPluginApi(object):
    """ServiceVMServiceHelper(Agent) side of the  routing RPC API."""

    def __init__(self, topic, host):
        self.host = host
        target = oslo_messaging.Target(topic=topic, version='1.0')
        self.client = n_rpc.get_client(target)

    def plugin_callback_call(self, context, plugin, method, **kwargs):
        """Make a remote process call to retrieve the sync data. 

        :param context: session context
        :param plugin:  It is plugin of method
        :param method:  call method
        """
        # NOTE(xining)
        #cctxt = self.client.prepare(version='1.1')
        cctxt = self.client.prepare()
        return cctxt.call(context, 'plugin_callback_call', plugin, method, **kwargs)

class ServiceInstanceHelper(object):

    def __init__(self, host, conf, cfg_agent):
        self.conf = conf
        self.svm_agent = svm_agent
        self.context = n_context.get_admin_context_without_session()
        self.plugin_rpc = ServiceVMPluginApi(topics.SERVICEVM, host)
        self._dev_status = device_status.DeviceStatus()
        #self._drivermgr = driver_mgr.DeviceDriverManager()

        self.topic = '%s.%s' % (c_constants.CFG_AGENT_L3_ROUTING, host)

        self._setup_rpc()

    def _setup_rpc(self):
        self.conn = n_rpc.create_connection(new=True)
        self.endpoints = [self]
        self.conn.create_consumer(self.topic, self.endpoints, fanout=False)
        self.conn.consume_in_threads()

    ### Notifications from Plugin ####
