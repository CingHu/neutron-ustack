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
# @author: Zhi Chang, UnitedStack, Inc

import eventlet
eventlet.monkey_patch()
import pprint
import sys
import time

from oslo.config import cfg
from oslo import messaging

from neutron.openstack.common import lockutils
from neutron.openstack.common import timeutils
from neutron.openstack.common import importutils
from neutron.openstack.common import excutils
from neutron.openstack.common.gettextutils import _LE, _LI, _LW
from neutron.agent.common import config
from neutron.agent.linux import external_process
from neutron.agent.linux import interface
from neutron.agent import rpc as agent_rpc
from neutron.common import config as common_config
from neutron.common import rpc as n_rpc
from neutron.common import topics
from neutron.common import constants
from neutron import context as n_context
from neutron import manager
from neutron import service as neutron_service
from neutron.openstack.common import log as logging
from neutron.openstack.common import loopingcall
from neutron.openstack.common import periodic_task
from neutron.openstack.common import service
from neutron.services.vm.agent import device_status
from neutron.services.vm.agent import driver_mgt
from neutron.services.vm.agent import config as vm_config
from neutron.services.vm.agent import svmagt_exception
from neutron.services.vm.agent.ovs import ovs_driver
from neutron.services.vm.common import topics as n_topics
LOG = logging.getLogger(__name__)

# Constants for agent registration.
REGISTRATION_RETRY_DELAY = 2
MAX_REGISTRATION_ATTEMPTS = 30
DRIVERS_PATH = "neutron.services.vm.agent.device_drivers."


class DeviceManagementApi(object):
    """Agent side of the device manager RPC API."""

    def __init__(self, topic, host):
        self.host = host
        target = messaging.Target(topic=topic, version='1.0')
        self.client = n_rpc.get_client(target)

    def register_agent_devices(self, context, resources=None):
        """Report that a device cannot be contacted (presumed dead).

        :param: context: session context
        :param: resources: include dead and active devices
        :return: None
        """
        cctxt = self.client.prepare()
        cctxt.cast(context, 'register_agent_devices',
                   host=self.host, resources=resources)

    def register_for_duty(self, context):
        """Report that a servicevm agent is ready for duty."""
        cctxt = self.client.prepare()
        return cctxt.call(context, 'register_for_duty', host=self.host)

    def get_devices_info_by_host(self, context):
        """Get devices info by host.

        :param: context: session context
        :return: dict
        """
        cctxt = self.client.prepare()
        return cctxt.call(context, 'get_devices_info_by_host', host=self.host)


class ServiceVMManagementApi(DeviceManagementApi):
    def __init__(self, topic, host):
        super(ServiceVMManagementApi, self).__init__(topic, host)

    def get_devices_on_host(self, context, host):
        # NOTE(changzhi)
        """Get devices from neutron-server plugin. """
        cctxt = self.client.prepare()
        return cctxt.call(context, 'get_devices_on_host', host=self.host)

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

    def get_service_instances(self, context, service_instance_ids=None, device_ids=None):
        """Make a remote process call to retrieve the sync data for routers.

        :param context: session context
        :param router_ids: list of  service instances to fetch
        :param device_ids: hosting device ids, only service instances assigned to these
                        hosting devices will be returned.
        """
        cctxt = self.client.prepare()
        return cctxt.call(context, 'sync_service_instances', host=self.host,
                service_instance_ids=service_instance_ids,
                device_ids=device_ids)

    def get_devices_details_list(self, context, devices):
        cctxt = self.client.prepare()
        return cctxt.call(context, 'get_devices_details_list', devices=devices,
                         host=self.host)

    def fetch_service_ids(self, context, device_ids=None):
        cctxt = self.client.prepare()
        return cctxt.call(context, 'sync_service_instance_ids', host=self.host,
                device_ids=device_ids)


class ServiceVMAgent(manager.Manager):
    """ServiceVM Agent.

    This class defines a generic configuration agent for devices which
    implement network services in the cloud backend. It is based on the
    (reference) l3-agent, but has been enhanced to support multiple services
     in addition to routing.

    The agent acts like as a container for services and does not do any
    service specific processing or configuration itself.
    All service specific processing is delegated to service helpers which
    the agent loads. Thus routing specific updates are processed by the
    routing service helper, firewall by firewall helper etc.
    A further layer of abstraction is implemented by using device drivers for
    encapsulating all configuration operations of a service on a device.
    Device drivers are specific to a particular device/service VM eg: CSR1kv.

    The main entry points in this class are the `process_services()` and
    `_backlog_task()` .
    """
    target = messaging.Target(version='1.0')

    def __init__(self, host, conf=None):
        self.conf = conf or cfg.CONF
        super(ServiceVMAgent, self).__init__(host=self.conf.host)
        self._dev_status = device_status.DeviceStatus()
        self.context = n_context.get_admin_context_without_session()
        self.driver_mgt = driver_mgt.DeviceDriverManager()
        # get all devices from this host and check them status
        self._get_devices_on_host()
        self._initialize_service_helpers(host)
        self._start_periodic_tasks()

    def _initialize_rpc(self, host):
        self.plugin_rpc = ServiceVMManagementApi(n_topics.SVMDEVICE_DRIVER_TOPIC,
                                                 host)
    def _initialize_service_helpers(self, host):
        svc_helper_class = self.conf.servicevm_agent.svc_helper_class
        try:
            self.service_helper = importutils.import_object(
                svc_helper_class, host, self.conf, self, self.plugin_rpc)
        except ImportError as e:
            LOG.warning(_LW("Error in loading service helper. Class "
                       "specified is %(class)s. Reason:%(reason)s"),
                     {'class': self.conf.servicevm_agent.svc_helper_class,
                      'reason': e})
            self.service_helper = None

    def _start_periodic_tasks(self):
        self.loop = loopingcall.FixedIntervalLoopingCall(self.process_services)
        self.loop.start(interval=2)

    def after_start(self):
        LOG.info("servicevm agent started")

    # Periodic tasks ##
    @periodic_task.periodic_task(spacing=5)
    def _backlog_task(self, context):
        """Process backlogged devices."""
        self._process_backlogged_devices(context)

    def _get_devices_on_host(self):
        devices = self.plugin_rpc.get_devices_info_by_host(self.context)
        LOG.info("Finish get all devices in host: %(host)s devices: %(devices)s",
                 {"host": self.host, "devices": devices})
        for device in devices:
            self.driver_mgt.set_driver(device)

    def get_devices_details_list(self, context, devices):
        res= self.plugin_rpc.get_devices_details_list(self.context, devices)
        return res


    @lockutils.synchronized('servicevm-agent', 'neutron-')
    def process_services(self, device_ids=None, removed_devices=None):
        LOG.debug("Processing services started")
        if self.service_helper:
             self.service_helper.process_services(device_ids, removed_devices)
 
        LOG.debug("Processing services completed")

    def _process_backlogged_devices(self, context):
        """Process currently backlogged devices.

        Go through the currently backlogged devices and process them.
        For devices which are now reachable (compared to last time), we call
        `process_services()` passing the now reachable device's id.
        For devices which have passed the `device_dead_timeout` and
        hence presumed dead, execute a RPC to the plugin informing that.
        :param context: RPC context
        :return: None
        """
        #TODO vm monitor
        res = self._dev_status.check_backlogged_devices()
        LOG.debug("Report devices status to server,  %s ", res)
        if res['reachable']:
            self.process_services(device_ids=res['reachable'])
        if res['dead'] or res['reachable']:
            self.plugin_rpc.register_agent_devices(context,
                                                resources=res)

    def devices_removed(self, context, payload):
        """Deal with device removed RPC message."""
        try:
            if payload['device_data']:
                if payload['device_data'].keys():
                    self.process_services(removed_devices=payload)
        except KeyError as e:
            LOG.error("Invalid payload format for received RPC message "
                        "`devices_removed`. Error is %(error)s. "
                        "Payload is %(payload)s",
                      {'error': e, 'payload': payload})

    #=======================================================================
    # service process

    def create_device(self, context, device):
        LOG.info(_('create_device %s'), device)
        self._dev_status.add_backlog_device([device])
        self.driver_mgt.set_driver(device)

    def update_device(self, context, device):
        LOG.info(_('update_device %s'), device)
        self._dev_status.remove_backlog_device([device])
        self._dev_status.add_backlog_device([device])

    def delete_device(self, context, device):
        LOG.info(_('delete_device %s'), device)
        self._dev_status.remove_backlog_device([device])
        self.driver_mgt.remove_driver(device)

    def create_service_instance(self, context, device, service_instance):
        LOG.info(_('create_service %(device)s %(service_instance)s'),
                  {'device': device, 'service_instance': service_instance})
        self.service_helper.create_service_instance(context, device, service_instance)

    def update_service_instance(self, context, device, service_instance, kwargs):
        LOG.info(_('update_service %(device)s %(service_instance)s %(kwargs)s'),
                  {'device': device, 'service_instance': service_instance, 'kwargs': kwargs})
        self.service_helper.update_service_instance(context, device, service_instance)

    def delete_service_instance(self, context, device, service_instance, kwargs):
        LOG.info(_('delete_service %(device)s %(service_instance)s %(kwargs)s'),
                  {'device': device, 'service_instance': service_instance,
                   'kwargs': kwargs})
        self.service_helper.delete_service_instance(context, device, service_instance)

    #========================================================================
    #nofity msg
    def subnet_update(self, context, **kwargs):
        LOG.info(_('subnet update %(kwargs)'),
                  { 'kwags': kwargs})

    def subnet_delete(self, context, **kwargs):
        LOG.info(_('subnet delete %(kwargs)'),
                  { 'kwags': kwargs})

    def subnet_create(self, context, **kwargs):
        LOG.info(_('subnet create %(kwargs)'),
                  { 'kwags': kwargs})

class ServiceVMAgentWithStateReport(ServiceVMAgent):

    def __init__(self, host, conf=None):
        self.state_rpc = agent_rpc.PluginReportStateAPI(n_topics.SVMDEVICE_DRIVER_TOPIC)
        self.agent_state = {
            'binary': 'neutron-servicevm-agent',
            'host': host,
            'topic': topics.SERVICEVM_AGENT,
            'configurations': {},
            'start_flag': True,
            'agent_type': constants.AGENT_TYPE_SERVICEVM}
        report_interval = cfg.CONF.AGENT.report_interval
        self.use_call = True
        self._initialize_rpc(host)
        self.topic = topics.SERVICEVM
        self.endpoints = [self]
        consumers = [[topics.SUBNET, topics.UPDATE],
                     [topics.SUBNET, topics.DELETE],
                     [topics.SUBNET, topics.CREATE]]
        self.connection = agent_rpc.create_consumers(self.endpoints,
                                                     self.topic,
                                                     consumers)
        #self._agent_registration()
        super(ServiceVMAgentWithStateReport, self).__init__(host=host,
                                                           conf=conf)
        if report_interval:
            self.heartbeat = loopingcall.FixedIntervalLoopingCall(
                self._report_state)
            #self.heartbeat.start(interval=report_interval)
            self.heartbeat.start(interval=2)

    def _agent_registration(self):
        """Register this agent with the server.

        This method registers the cfg agent with the neutron server so 
        devices can be assigned to it. In case the server is not ready to
        accept registration (it sends a False) then we retry registration
        for `MAX_REGISTRATION_ATTEMPTS` with a delay of
        `REGISTRATION_RETRY_DELAY`. If there is no server response or a
        failure to register after the required number of attempts,
        the agent stops itself.
        """
        for attempts in xrange(MAX_REGISTRATION_ATTEMPTS):
            context = n_context.get_admin_context_without_session()
            self.send_agent_report(self.agent_state, context)
            res = self.plugin_rpc.register_for_duty(context)
            if res is True:
                LOG.info("[Agent registration] Agent successfully "
                           "registered")
                return
            elif res is False:
                LOG.warning("[Agent registration] Neutron server said "
                                "that device manager was not ready. Retrying "
                                "in %0.2f seconds "), REGISTRATION_RETRY_DELAY
                time.sleep(REGISTRATION_RETRY_DELAY)
            elif res is None:
                LOG.error("[Agent registration] Neutron server said that "
                              "no device manager was found. Cannot continue. "
                              "Exiting!")
                raise SystemExit("ServiceVM Agent exiting")
        LOG.error("[Agent registration] %d unsuccessful registration "
                    "attempts. Exiting!", MAX_REGISTRATION_ATTEMPTS)
        raise SystemExit("ServiceVM Agent exiting")

    def _report_state(self):
        """Report state to the plugin.

        This task run every `report_interval` period.
        Collects, creates and sends a summary of the services currently
        managed by this agent. Data is collected from the service helper(s).
        Refer the `configurations` dict for the parameters reported.
        :return: None
        """
        LOG.debug("Report state task started")
        configurations = {}
        #TODO you may add configuration about for services
        LOG.debug("Backlogged devices are %s",
                   self._dev_status.get_backlogged_devices()) 
        non_responding = {}
        configurations['non_responding_devices'] = non_responding
        self.agent_state['configurations'] = configurations
        self.agent_state['local_time'] = str(timeutils.utcnow())
        LOG.debug("State report data: %s", pprint.pformat(self.agent_state))
        self.send_agent_report(self.agent_state, self.context)

    def send_agent_report(self, report, context):
        """Send the agent report via RPC."""
        try:
            self.state_rpc.report_state(context, report, self.use_call)
            report.pop('start_flag', None)
            self.use_call = False
            LOG.debug("Send agent report successfully completed")
        except AttributeError:
            # This means the server does not support report_state
            LOG.warning("Neutron server does not support state report. "
                       "State report for this agent will be disabled.")
            self.heartbeat.stop()
            return
        except Exception:
            LOG.exception("Failed sending agent report!")

def _register_options(conf):
    config.register_agent_state_opts_helper(conf)
    config.register_root_helper(conf)
    conf.register_opts(interface.OPTS)
    conf.register_opts(external_process.OPTS)
    conf.register_opts(vm_config.OPTS, "servicevm_agent")

def main(manager='neutron.services.vm.agent'
                 '.agent.ServiceVMAgentWithStateReport'):
    conf = cfg.CONF
    _register_options(conf)
    common_config.init(sys.argv[1:])
    conf(project='neutron')
    config.setup_logging(conf)
    server = neutron_service.Service.create(
        binary='neutron-servicevm-agent',
        topic=topics.SERVICEVM_AGENT,
        report_interval=cfg.CONF.AGENT.report_interval,
        manager=manager)
    service.launch(server).wait()

