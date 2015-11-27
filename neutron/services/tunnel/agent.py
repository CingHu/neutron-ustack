# Copyright 2015, UnitedStack,  Inc.
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
#   @Author: wei wang<wangwei@unitedstack.com>

import sys

from oslo.config import cfg

from neutron import context
from neutron import manager
from neutron import service as neutron_service
from neutron.agent import l3_agent
from neutron.agent import rpc as agent_rpc
from neutron.agent.common import config
from neutron.api.rpc.agentnotifiers import helo_rpc_agent_api
from neutron.extensions import tunnelaas
from neutron.common import config as common_config
from neutron.common import constants as n_constants
from neutron.common import topics as n_topics
from neutron.openstack.common import importutils
from neutron.openstack.common import log as logging
from neutron.openstack.common import loopingcall
from neutron.openstack.common import periodic_task
from neutron.openstack.common import service
from neutron.services.tunnel.common import topics

LOG = logging.getLogger(__name__)
NS_PREFIX = l3_agent.NS_PREFIX

tunnel_agent_opts = [
    cfg.MultiStrOpt(
        'tunnel_device_driver',
        default=['neutron.services.tunnel.device_drivers.'
                 'iproute2.Iproute2Agent'],
        help=_("The tunnel device drivers Neutron will use")),
    cfg.IntOpt('rpc_loop_interval',
        default='1',
        help=_("The tunnel device drivers rpc loop interval")),
    cfg.IntOpt('report_interval',
        default=60,
        help=_("Interval for report")),
    cfg.StrOpt('iproute_state_dir',
        default='$state_path/iproute',
        help=("Directory to store iproute status"))

]
cfg.CONF.register_opts(tunnel_agent_opts, 'tunnel_agent')


class L3TunnelInfo(object):
    """Represents a L3 tunnel."""
    def __init__(self, tunnel):
        self.type = tunnel['type']
        assert self.type == 3
        connection = tunnel['tunnel_connections'].values()
        #NOTE(weiw): for logic in judge be consistence
        self.connections = tunnel['tunnel_connections']
        if len(connection) != 0:
            connection = connection[0]
            self.key = connection['key']
            self.key_type = connection['key_type']
            self.checksum = connection['checksum']
            self.remote_ip = connection['remote_ip']
        else:
            self.key = None
            self.key_type = None
            self.checksum = None
            self.remote_ip = None
        self.id = tunnel['id']
        self.admin_state_up = tunnel['admin_state_up']
        self.router = tunnel['router_id']
        self.gw_port_id = tunnel['gw_port_id']
        self.local_ip = tunnel['local_ip']
        self.target_networks = []
        for target_network in tunnel['target_networks'].values():
            self.target_networks.append(target_network['network_cidr'])
        self._setup()

    def _setup(self):
        self.gw_device = 'qg-' + self.gw_port_id[:11]
        self.gre_device = 'qgr-' + self.id[:11]
        key_type_dict = {0:None, 1:'okey', 2:'ikey', 3:'key'}
        checksum_dict = {0:None, 1:'ocsum', 2:'icsum', 3:'csum'}
        if len(self.connections) != 0:
            self.key_type = key_type_dict[self.key_type]
            self.checksum = checksum_dict[self.checksum]

    def __eq__(self, t):
        for key in ['id', 'router', 'gw_port_id', 'local_ip', 'key', 'key_type',
                    'admin_state_up', 'checksum','remote_ip']:
            if getattr(self, key) != getattr(t, key):
                return False
        if set(self.target_networks) != t.target_networks:
            return False
        return True

    def __ne__(self, t):
        return not self.__eq__(t)


class L2TunnelInfo(object):
    """Represents a L2 tunnel."""
    def __init__(self, tunnel):
        self.type = tunnel['type']
        assert self.type == 2
        self.connections = tunnel['tunnel_connections']
        self.id = tunnel['id']
        self.admin_state_up = tunnel['admin_state_up']
        self.router = tunnel['router_id']
        self.gw_port_id = tunnel['gw_port_id']
        self.local_ip = tunnel['local_ip']
        self.ri_port_id = tunnel['ri_port_id']
        self.ri_port_ip = tunnel['ri_port_ip']
        self._setup()
 
    def _setup(self):
        self.gw_device = 'qg-' + self.gw_port_id[:11]
        self.ri_device = 'qr-' + self.ri_port_id[:11]
        self.grebr_device = 'qgb-' + self.id[:11]
        key_type_dict = {0:None, 1:'okey', 2:'ikey', 3:'key'}
        checksum_dict = {0:None, 1:'ocsum', 2:'icsum', 3:'csum'}
        self.gretap_device = {}
        for conn in self.connections:
            self.gretap_device[conn] = 'qgt-' + conn[:11]
            self.connections[conn]['key_type'] = key_type_dict[
                    self.connections[conn]['key_type']]
            self.connections[conn]['checksum'] = checksum_dict[
                    self.connections[conn]['checksum']]

    def __eq__(self, t):
        for key in ['id', 'admin_state_up', 'router', 'gw_port_id',
                    'ri_port_id', 'ri_port_ip']:
            if getattr(self, key) != getattr(t, key):
                return False
        if set(self.connections.keys()) != set(t.connections.keys()):
            return False
        for conn in self.connections:
            if (self.connections[conn] != t.connections[conn]):
                return False
        return True

    def __ne__(self, t):
        return not self.__eq__(t)


class TunnelAgent(manager.Manager, helo_rpc_agent_api.HeloRpcCallbackMixin):
    """TunnelAgent class which can handle tunnel service drivers."""
    def __init__(self, host, conf=None):
        super(TunnelAgent, self).__init__(host=host)
        if conf:
            self.conf = conf
        else:
            self.conf = cfg.CONF
        config.register_root_helper(self.conf)
        self.context = context.get_admin_context_without_session()
        self.root_helper = config.get_root_helper(self.conf)
        self.setup_device_drivers(host)
        self.sync_tunnels(self.context)
        self.rpc_loop = loopingcall.FixedIntervalLoopingCall(
                         self._rpc_loop_nosync)
        self.rpc_loop.start(interval=
            cfg.CONF.tunnel_agent.rpc_loop_interval)

    def setup_device_drivers(self, host):
        """Setting up device drivers.

        :param host: hostname. This is needed for rpc
        Each devices will stays as processes.
        They will communicate with
        server side service plugin using rpc with
        device specific rpc topic.
        :returns: None
        """
        device_drivers = cfg.CONF.tunnel_agent.tunnel_device_driver
        self.devices = []
        for device_driver in device_drivers:
            try:
                self.devices.append(
                    importutils.import_object(device_driver, self, host))
            except ImportError:
                raise tunnelaas.DeviceDriverImportError(
                    device_driver=device_driver)

    def get_namespace(self, router_id):
        """Get namespace of router.

        :router_id: router_id
        :returns: namespace string.
            Note if the router is not exist, this function
            returns None
        """
        return NS_PREFIX + router_id

    def _rpc_loop_nosync(self):
        for device in self.devices:
            if getattr(device, '_rpc_loop_nosync'):
                device._rpc_loop_nosync()

    @periodic_task.periodic_task(space=90)
    def sync_tunnels(self, context):
        for device in self.devices:
            if getattr(device, '_sync_tunnels'):
                device._sync_tunnels()

    def _report_state(self):
        try:
            self.state_rpc.report_state(self.context, self.agent_state,
                                        self.use_call)
            self.agent_state.pop('start_flag', None)
            self.use_call = False
            LOG.info(_("Report state task successfully completed"))
        except AttributeError:
            raise
            # This means the server does not support report_state
            LOG.warn(_("Neutron server does not support state report."
                       " State report for this agent will be disabled."))
            self.heartbeat.stop()
            return
        except Exception:
            LOG.exception(_("Failed reporting state!"))


class TunnelAgentWithStateReport(TunnelAgent):
    def __init__(self, host, conf=None):
        super(TunnelAgentWithStateReport, self).__init__(host=host, conf=conf)
        self.state_rpc = agent_rpc.PluginReportStateAPI(topics.GRE_DRIVER_TOPIC)
        self.agent_state = {
            'binary': 'neutron-tunnel-agent',
            'host': host,
            'topic': n_topics.TUNNEL_AGENT,
            'configurations': {
                'device_driver': self.conf.tunnel_agent.tunnel_device_driver},
            'agent_type': n_constants.AGENT_TYPE_TUNNEL,
            'start_flag': True}
        report_interval = cfg.CONF.tunnel_agent.report_interval
        self.use_call = False
        if report_interval:
            self.heartbeat = loopingcall.FixedIntervalLoopingCall(
                    self._report_state)
            self.heartbeat.start(interval=report_interval)


def main(manager='neutron.services.tunnel.agent.TunnelAgentWithStateReport'):
    common_config.init(sys.argv[1:])
    config.setup_logging(cfg.CONF)
    server = neutron_service.Service.create(
            binary='neutron-tunnel-agent',
            topic=n_topics.TUNNEL_AGENT,
            report_interval=cfg.CONF.tunnel_agent.report_interval,
            manager=manager)
    service.launch(server).wait()
