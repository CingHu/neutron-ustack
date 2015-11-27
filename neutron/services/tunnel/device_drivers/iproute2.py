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
import abc
import copy
import traceback
import os
import re
import shutil

import jinja2
import netaddr
from oslo.config import cfg
from oslo import messaging
import cPickle as pickle
import six

from neutron import context
from neutron.agent.linux import ip_lib
from neutron.agent.linux import utils
from neutron.common import rpc as n_rpc
from neutron.extensions import tunnelaas
from neutron.openstack.common import lockutils
from neutron.openstack.common import log as logging
from neutron.openstack.common import loopingcall
from neutron.openstack.common import excutils
from neutron.plugins.common import constants
from neutron.plugins.common import utils as plugin_utils
from neutron.services.tunnel.common import topics
from neutron.services.tunnel import agent

LOG = logging.getLogger(__name__)

iproute_opts = [
    cfg.IntOpt('gre_status_check_interval',
               default=60,
               help=_("Interval for checking gre status"))
]
cfg.CONF.register_opts(iproute_opts, 'iproute')


@six.add_metaclass(abc.ABCMeta)
class BaseIproute2Driver():
    """Base iproute2 driver

    iproute2 will implement create/delete tunnel, add/delete
    route as base API
    """

    def __init__(self, root_helper, namespace, tunnel_info):
        self.root_helper = root_helper
        self.tunnel = tunnel_info
        self.namespace = namespace
        self.type = self.tunnel.type

    @property
    def status(self):
        if self.get_status():
            return constants.ACTIVE
        return constants.DOWN

    @abc.abstractmethod
    def get_status():
        pass

    @abc.abstractmethod
    def enable(self):
        pass

    @abc.abstractmethod
    def disable(self):
        pass

    @abc.abstractmethod
    def create_tunnel(self):
        pass

    @abc.abstractmethod
    def delete_tunnel(self):
        pass

    @abc.abstractmethod
    def add_route(self):
        pass

    @abc.abstractmethod
    def remove_route(self):
        pass

class Iproute2Driver(BaseIproute2Driver):
    """Iproute2 driver

    This class will use 2 commands:
    (1) ip tunnel: ip-tunnel configuration
    (2) ip route: routing table management
    """

    def __init__(self, root_helper, namespace, tunnel_info):
        super(Iproute2Driver, self).__init__(
            root_helper, namespace, tunnel_info)

    def _execute(self, cmd, check_exit_code=True):
        """Excute command on namespace"""
        ip_wrapper = ip_lib.IPWrapper(self.root_helper, self.namespace)
        return ip_wrapper.netns.execute(
            cmd, check_exit_code=check_exit_code)

    def get_status(self):
        """Get tunnel's status"""
        if not self.namespace:
            return False

        if self.type == 3:
            link = self.tunnel.gre_device
        elif self.type == 2:
            link = self.tunnel.grebr_device
        try:
            status = self._execute(['ip', 'link', 'show',
                                    link, 'up'])
        except RuntimeError:
            return False
        # TODO(WeiW): Check this
        if status:
            return True
        else:
            return False

    def enable(self):
        if self.type == 3:
            link = self.tunnel.gre_device
            self._execute(['ip', 'link', 'set', link, 'up'])
        elif self.type == 2:
            for link in self.tunnel.gretap_device.values():
                self._execute(['ip', 'link', 'set', link, 'up'])

    def disable(self):
        if self.type == 3:
            link = self.tunnel.gre_device
        elif self.type == 2:
            link = self.tunnel.grebr_device
        self._execute(['ip', 'link', 'set', link, 'down'])

    def create_tunnel(self):
        if self.type == 3:
            command = ['ip', 'tunnel', 'add', self.tunnel.gre_device,
                       'mode','gre', 'remote', self.tunnel.remote_ip,
                       'local',self.tunnel.local_ip]
            if self.tunnel.key_type != None:
                command.extend([self.tunnel.key_type, self.tunnel.key])
            if self.tunnel.checksum != None:
                command.append(self.tunnel.checksum)
            command.extend(['ttl', 255, 'dev',self.tunnel.gw_device])
            try:
                self._execute(command)
            except RuntimeError as e:
                if 'File exists' in e.message:
                    LOG.warn(_('Device exists: %s'), e.message)
            self.enable()
            for route in self.tunnel.target_networks:
                self.add_route(route)
            LOG.info(_('GRE device %s in namespace %s setup complete'),
                    self.tunnel.gre_device, self.namespace)
        elif self.type == 2:
            self._execute(['ip', 'link', 'add', self.tunnel.grebr_device,
                           'type', 'bridge'])
            self._execute(['ip', 'link', 'set', self.tunnel.grebr_device, 'up'])
            for conn_id in self.tunnel.connections:
                conn = self.tunnel.connections[conn_id]
                link = self.tunnel.gretap_device[conn_id]
                command = ['ip', 'link', 'add', link,
                        'type', 'gretap', 'local', self.tunnel.local_ip,
                        'remote', conn['remote_ip'], 'ttl', '255']
                if conn['key_type'] != None:
                    command.extend([conn['key_type'], conn['key']])
                if conn['checksum'] != None:
                    command.append(conn['checksum'])
                command.extend(['dev',self.tunnel.gw_device])
                try:
                    self._execute(command)
                except RuntimeError as e:
                    if 'File exists' in e.message:
                        LOG.warn(_('Device exists: %s'), e.message)
                self._execute(['ip', 'link', 'set', link, 'up'])
                self._execute(['ip', 'link', 'set', link, 'master',
                               self.tunnel.grebr_device])
            self._execute(['ip', 'addr', 'delete', self.tunnel.ri_port_ip,
                           'dev', self.tunnel.ri_device])
            self._execute(['ip', 'link', 'set', self.tunnel.ri_device,
                           'master',self.tunnel.grebr_device])
            self._execute(['ip', 'addr', 'add', self.tunnel.ri_port_ip, 'dev',
                           self.tunnel.grebr_device])

    def delete_tunnel(self):
        if self.type == 3:
            for tn in self.tunnel.target_networks:
                self.remove_route(tn)
            try:
                self._execute(['ip', 'tunnel', 'delete',
                               self.tunnel.gre_device])
            except RuntimeError as e:
                if "Cannot find" in e.message:
                    LOG.warn(_('Device already deleted: %s'),
                             self.tunnel.gre_device)
        elif self.type == 2:
            for gretap in self.tunnel.gretap_device.values():
                try:
                    self._execute(['ip', 'link', 'delete', gretap])
                except RuntimeError as e:
                    if "Cannot find" in e.message:
                        LOG.warn(_('Device already deleted: %s'), gretap)
            try:
                self._execute(['ip', 'link', 'delete',
                               self.tunnel.grebr_device])
            except RuntimeError as e:
                if "Cannot find" in e.message:
                    LOG.warn(_('Device already deleted: %s'),
                             self.tunnel.grebr_device)
            #NOTE(weiw): This incrediable ugly!
            gretaps = self._execute(['ip', 'link']).split('qgt-')[1:]
            grebridges = self._execute(['ip', 'link']).split('qgb-')[1:]
            for gretap in gretaps:
                link = 'qgt-' + gretap[:11]
                LOG.info(_('Delete gretap: %s'), link)
                self._execute(['ip', 'link', 'delete', link])
            for grebridge in grebridges:
                link = 'qgb-' + grebridge[:11]
                LOG.info(_('Delete grebridge: %s'), link)
                self._execute(['ip', 'link', 'delete', link])
            self.recover()

    def add_route(self, route):
        if self.type != 3:
            raise NotImplemented
        self._execute(['ip', 'route', 'add', route,
                        'dev', self.tunnel.gre_device])

    def remove_route(self, route):
        if self.type != 3:
            raise NotImplemented
        self._execute(['ip', 'route', 'delete', route,
                        'dev', self.tunnel.gre_device])

    def recover(self):
        if self.type != 2:
            raise NotImplemented
        try:
            self._execute(['ip', 'addr', 'add', self.tunnel.ri_port_ip, 'dev',
                       self.tunnel.ri_device])
        except RuntimeError as e:
            if 'File exists' in e.message:
                LOG.warn(_('Device exists: %s'), e.message)


class GRETunnelDriverApi(n_rpc.RpcProxy):
    """GRETunnelDriver RPC api."""
    TUNNEL_PLUGIN_VERSION = '1.0'

    def get_agent_host_tunnels(self, context, host, tunnels):
        """Returns the tunnels on the host."""
        return self.call(context,
                         self.make_msg('get_agent_host_tunnels',
                                       host=host,
                                       tunnels=tunnels),
                         version=self.TUNNEL_PLUGIN_VERSION)

    def update_status(self, context, status):
        """Update status of tunnels."""
        return self.cast(context,
                         self.make_msg('update_status',
                                       status=status),
                         version=self.TUNNEL_PLUGIN_VERSION)


class Iproute2Agent(object):
    """ Iproute2 Agent Driver

    This is the agent's driver.
    It will consume messages and dispatch, get info, etc.
    """

    RPC_API_VERSION = '1.0'
    target = messaging.Target(version=RPC_API_VERSION)

    def __init__(self, agent, host):
        self.agent = agent
        self.conf = self.agent.conf
        self.root_helper = self.agent.root_helper
        self.context = context.get_admin_context_without_session()
        self.host = host
        self.tunnels = {}

        self.updated_tunnels = set()
        self.deleted_tunnels = set()

        self.conn = n_rpc.create_connection(new=True)
        self.topic = topics.GRE_AGENT_TOPIC
        node_topic = '%s.%s' % (self.topic, self.host)
        self.conn.create_consumer(
            node_topic,
            self.create_rpc_dispatcher(),
            fanout=False)

        fan_topic = '%s' % (self.topic)
        self.conn.create_consumer(
            fan_topic,
            self.create_rpc_dispatcher(),
            fanout=True)
        self.agent_rpc = GRETunnelDriverApi(topics.GRE_DRIVER_TOPIC, '1.0')

        self._init_sync_flag = False
        self._init_sync()
        self.conn.consume_in_threads()

    def _init_sync(self):
        LOG.info(_('Now run init sync tunnels'))
        try:
            data_path = file(os.path.join(
                self.conf.tunnel_agent.iproute_state_dir,
                "tunnel-agent.pickle"), 'r')
            self.tunnels = pickle.load(data_path)
        except Exception:
            LOG.info(_("First run"))
            self._init_sync_flag = True
            return
        LOG.info(_("Init sync completed, get %s tunnels."), len(self.tunnels))
        for tunnel_id in self.tunnels:
            if not self.agent_rpc.get_agent_host_tunnels(
                    self.context, self.host, [tunnel_id]):
                LOG.info(_("Tunnel %s already deleted in db"), tunnel_id)
                self._delete_tunnel(self.tunnels[tunnel_id])
        self._init_sync_flag = True

    def create_rpc_dispatcher(self):
        # return q_rpc.PluginRpcDispatcher([self])
        return [self]

    def tunnel_updated(self, context, tunnel_id):
        self.updated_tunnels.add(tunnel_id)

    def tunnel_deleted(self, context, tunnel_id):
        self.deleted_tunnels.add(tunnel_id)

    def _rpc_loop_nosync(self):
        updated_tunnel_ids = list(self.updated_tunnels)
        self.updated_tunnels.clear()
        self.deleted_tunnels.clear()
        try:
            if self.deleted_tunnels:
                pass
            if updated_tunnel_ids:
                LOG.info(_('Get new update tunnel: %s'), updated_tunnel_ids)
                self._process_tunnels_update(updated_tunnel_ids)
        except Exception as e:
            LOG.error(traceback.format_exc())
            LOG.error(_('Rpc loop failed'))

    def _make_tunnel_info(self, tunnel):
        if tunnel['type'] == 3:
            tunnel_info = agent.L3TunnelInfo(tunnel)
        elif tunnel['type'] == 2:
            tunnel_info = agent.L2TunnelInfo(tunnel)
        else:
            raise NotImplemented()
        return tunnel_info

    def _process_tunnels_update(self, tunnels_id):
        tunnels = self.agent_rpc.get_agent_host_tunnels(
            self.context, self.host, tunnels_id)
        LOG.info(_('Get tunnels\' info: %s'), tunnels)
        for tunnel_db in tunnels:
            if not tunnel_db:
                # tunnel_db maybe NoneType like fip on router removed
                continue
            tunnel = self._make_tunnel_info(tunnel_db)
            tunnels_id.remove(tunnel.id)
            local_tunnel = self.tunnels.get(tunnel.id)
            if local_tunnel and local_tunnel == tunnel:
                LOG.info(_('No update to tunnel %s'), tunnel.id)
                continue
            elif local_tunnel and len(tunnel.connections) == 0:
                # Means the connection is deleted
                LOG.info(_('Seems tunnel %s \'s conns are discard'), tunnel.id)
                self._delete_tunnel(tunnel)
                if tunnel.id in self.tunnels:
                    del self.tunnels[tunnel.id]
            elif len(tunnel.connections) == 0:
                # Means host doesn't have, but its conn is empty, so does not
                # need to create
                LOG.info(_('Seems no need crate conns for tunnel %s'),
                         tunnel.id)
                continue
            elif tunnel.id not in self.tunnels:
                LOG.info(_('New tunnel in this host: %s'), tunnel.id)
                self.tunnels[tunnel.id] = tunnel
                self._setup_tunnel(self.tunnels[tunnel.id])
            elif tunnel != self.tunnels[tunnel.id]:
                LOG.info(_('Tunnel %s is updating'), tunnel.id)
                self._delete_tunnel(self.tunnels[tunnel.id])
                self.tunnels[tunnel.id] = tunnel
                self._setup_tunnel(self.tunnels[tunnel.id])
            else:
                LOG.info(_('What happend to tunnel %s ??'), tunnel.id)
        if tunnels_id:
            LOG.warn(_('Seems this tunnels already deleted: %s'), tunnels)
            for tunnel in tunnels_id:
                if self.tunnels.get(tunnel):
                    self._delete_tunnel(self.tunnels.get(tunnel))
                    del self.tunnels[tunnel]

    def _delete_tunnel(self, tunnel):
        ns = self.agent.get_namespace(tunnel.router)
        iproute = Iproute2Driver(self.root_helper, ns, tunnel)
        try:
            iproute.delete_tunnel()
        except RuntimeError as e:
            if "Cannot find" in e.message:
                LOG.warn(_('Device already deleted'))
            if "Cannot open network namespace" in e.message:
                LOG.warn(_('Namespace escaped'))
        conn_status = {}.fromkeys(tunnel.connections.keys(), 'INACTIVE')
        status = [{'id':tunnel.id, 'status':'INACTIVE',
            'tunnel_connections':conn_status}]
        try:
            self.agent_rpc.update_status(self.context, status)
        except Exception:
            LOG.warn(_('Update tunnel %s \'s satus to inactive failed'
                ), tunnel.id)

    def _setup_tunnel(self, tunnel):
        ns = self.agent.get_namespace(tunnel.router)
        iproute = Iproute2Driver(self.root_helper, ns, tunnel)
        try:
            iproute.create_tunnel()
        except RuntimeError as e:
            if 'File exists' in e.message:
                LOG.warn(_('Device exists: %s'), e.message)
            if "Cannot open network namespace" in e.message:
                LOG.warn(_('Namespace escaped'))
        if tunnel.admin_state_up == "DOWN":
            iproute.disable()
        conn_status = {}.fromkeys(tunnel.connections.keys(), 'ACTIVE')
        status = [{'id':tunnel.id, 'status':'ACTIVE',
            'tunnel_connections':conn_status}]
        self.agent_rpc.update_status(self.context, status)

    def _sync_tunnels(self):
        if self._init_sync_flag == False:
            return
        LOG.info(_('Now run sync tunnels'))
        try:
            data_path = file(os.path.join(
                self.conf.tunnel_agent.iproute_state_dir,
                "tunnel-agent.pickle"), 'w')
        except IOError as e:
            if e.errno == 2:
                os.makedirs(self.conf.tunnel_agent.iproute_state_dir)
                data_path = file(os.path.join(
                        self.conf.tunnel_agent.iproute_state_dir,
                        "tunnel-agent.pickle"), 'w')
        pickle.dump(self.tunnels, data_path)
        tunnels = self.agent_rpc.get_agent_host_tunnels(self.context,
                self.host, [])
        local_tunnels = self.tunnels.keys()
        LOG.info(_('Sync completed, get %s tunnels'), len(tunnels))
        for tunnel in tunnels:
            if not tunnel:
                # tunnel_db maybe NoneType like fip on router removed
                continue
            if tunnel['id'] in self.tunnels:
                local_tunnels.remove(tunnel['id'])
            if tunnel['status'] == 'ACTIVE' and (
                    tunnel['id'] not in self.tunnels):
                _tunnel = self._make_tunnel_info(tunnel)
                self.tunnels[tunnel['id']] = _tunnel
            elif tunnel['status'] == 'INACTIVE':
                self.tunnel_updated(self.context, tunnel['id'])
        for tunnel in local_tunnels:
            LOG.warn(_("Server has no info to tunnel %s, which agent host"),
                     tunnel)
            self.tunnel_updated(self.context, tunnel)
