# Copyright 2013 New Dream Network, LLC (DreamHost)
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
# @author: Mark McClain, DreamHost
import os
import shutil
import socket

import netaddr
from oslo.config import cfg
from neutron.agent.common import config
from neutron.agent.linux import ip_lib
from neutron.agent.linux import utils
from neutron.agent.linux import iptables_namespace_firewall as firewall
from neutron.agent import securitygroups_rpc as sg_rpc
from neutron.agent import rpc as agent_rpc
from neutron.common import exceptions
from neutron.common import rpc as n_rpc
from neutron.common import topics
from neutron.common import utils as n_utils
from neutron.common import constants as device_constants
from neutron.openstack.common import excutils
from neutron.openstack.common import importutils
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants
from neutron.services.loadbalancer.agent import agent_api
from neutron.services.loadbalancer.agent import agent_device_driver
from neutron.services.loadbalancer import constants as lb_const
from neutron.services.loadbalancer.drivers.haproxy import uos_jinja_cfg as haproxycfg

LOG = logging.getLogger(__name__)
NS_PREFIX = 'qlbaas-'
DRIVER_NAME = 'haproxy_ns'

STATE_PATH_DEFAULT = '$state_path/lbaas'
USER_GROUP_DEFAULT = 'nogroup'
OPTS = [
    cfg.StrOpt(
        'loadbalancer_state_path',
        default=STATE_PATH_DEFAULT,
        help=_('Location to store config and state files'),
        deprecated_opts=[cfg.DeprecatedOpt('loadbalancer_state_path',
                                           group='DEFAULT')],
    ),
    cfg.StrOpt(
        'user_group',
        default=USER_GROUP_DEFAULT,
        help=_('The user group'),
        deprecated_opts=[cfg.DeprecatedOpt('user_group', group='DEFAULT')],
    ),
    cfg.IntOpt(
        'send_gratuitous_arp',
        default=3,
        help=_('When delete and re-add the same vip, send this many '
               'gratuitous ARPs to flush the ARP cache in the Router. '
               'Set it below or equal to 0 to disable this feature.'),
    )
]

STATE_PATH_V2_APPEND = 'v2'
cfg.CONF.register_opts(OPTS, 'haproxy')
DEFAULT_INTERFACE_DRIVER = 'neutron.agent.linux.interface.OVSInterfaceDriver'

class OVSPluginApi(agent_rpc.PluginApi,
                   sg_rpc.SecurityGroupServerRpcApiMixin):
    pass


class SecurityGroupAgentRpcNamespaceMixin(object):
    """A mix-in that enable SecurityGroup in Namespace agent
    support in agent implementations.
    """
    # device --> iptables_manager
    device_iptables = {}
    device_sg = {}
    devices_to_refilter = set()
    global_refresh_firewall = False
    defer_refresh_firewall = False

    def security_groups_rule_updated(self, context, **kwargs):
        """Callback for security group rule update.

        :param security_groups: list of updated security_groups
        """
        security_groups = kwargs.get('security_groups', [])
        LOG.debug(
            _("Security group rule updated on remote: %s"), security_groups)
        self._security_groups_rule_updated(security_groups)

    def security_groups_member_updated(self, context, **kwargs):
        """Callback for security group member update.

        :param security_groups: list of updated security_groups
        """
        security_groups = kwargs.get('security_groups', [])
        LOG.debug(
            _("Security group member updated on remote: %s"), security_groups)
        self._security_groups_member_updated(security_groups)

    def security_groups_provider_updated(self, context, **kwargs):
        """Callback for security group provider update."""
        LOG.debug(_("Provider rule updated"))
        self._security_groups_provider_updated()

    def port_update(self, context, **kwargs):
        port = kwargs.get('port')
        LOG.info(_("port_update message processed for port %s"), port['id'])
        if (port['device_owner']
                == device_constants.DEVICE_OWNER_LOADBALANCER):
            self._port_update(port)

    @n_utils.synchronized('haproxy-driver')
    def _port_update(self, port):
        port_set = set()
        port_set.add(port['id'])
        self.refresh_firewall(port_set)

    @n_utils.synchronized('haproxy-driver')
    def _security_groups_member_updated(self, security_groups):
        LOG.info(_("Security group "
                   "member updated %r"), security_groups)
        self._security_group_updated(
            security_groups,
            'security_group_source_groups')

    @n_utils.synchronized('haproxy-driver')
    def _security_groups_rule_updated(self, security_groups):
        LOG.info(_("Security group "
                   "rule updated %r"), security_groups)
        self._security_group_updated(
            security_groups,
            'security_groups')

    @n_utils.synchronized('haproxy-driver')
    def _security_groups_provider_updated(self):
        LOG.info(_("Provider rule updated"))
        if self.defer_refresh_firewall:
            self.global_refresh_firewall = True
        else:
            self.refresh_firewall()

    def _security_group_updated(self, security_groups, attribute):
        LOG.info("_security_group_updated for %s",security_groups)
        devices = []
        sec_grp_set = set(security_groups)
        for device in self.device_sg.values():
            LOG.info("_security_group_updated for device %s",device)
            if sec_grp_set & set(device.get(attribute, [])):
                devices.append(device['device'])
        if devices:
            if self.defer_refresh_firewall:
                LOG.info(_("Adding %s devices to the list of devices "
                            "for which firewall needs to be refreshed"),
                          devices)
                self.devices_to_refilter |= set(devices)
            else:
                LOG.info("_security_group_updated refresh"
                             "_firewall for device %s",device['device'])
                self.refresh_firewall(devices)

    def get_firewall(self, device_id, loadbalancer_id=None, with_create=None):
        LOG.info("get_firewall for %s", device_id)
        LOG.info("device_iptables key %s",self.device_iptables.keys())
        firewall_driver = self.device_iptables.get(device_id, None)
        if firewall_driver is None and loadbalancer_id and with_create:
            namespace = get_ns_name(loadbalancer_id)
            self.device_iptables[device_id] = firewall.OVSHybridIptablesFirewallDriver(namespace)
            return self.device_iptables[device_id]

        return firewall_driver

    def prepare_devices_filter(self, device_ids):
        if not device_ids:
            return
        LOG.info(_("Preparing filters for devices %s"), device_ids)
        devices = self.ovs_plugin_rpc.security_group_rules_for_devices(
            self.context, list(device_ids))
        for device in devices.values():
            LOG.info(_("Prepare port filter for %s"), device['device'])
            firewall = self.get_firewall(device['device'], device['device_id'], True)
            with firewall.defer_apply():
                firewall.prepare_port_filter(device)
            self.device_sg[device['device']] = device

    def refresh_firewall(self, device_ids = None):
        if not device_ids:
            device_ids = self.device_sg.keys()
            if not device_ids:
                LOG.info(_("No ports here to refresh firewall"))
                return
        devices = self.ovs_plugin_rpc.security_group_rules_for_devices(
            self.context, device_ids)
        for device in devices.values():
            LOG.info(_("Update port filter for %s"), device['device'])
            firewall = self.get_firewall(device['device'])
            if firewall:
                with firewall.defer_apply():
                    firewall.prepare_port_filter(device)
            else:
                LOG.error(_("Not found firewall when update port"
                                " filter for %s"), device['device'])

    def setup_port_filters(self, new_devices):
        if new_devices:
            LOG.info(_("Preparing device filters for %d new devices"),
                      len(new_devices))
            # prepare_devices_filter will add the new_devices
            # to firewall.filtered_ports
            self.prepare_devices_filter(new_devices)

        devices_to_refilter = self.devices_to_refilter
        global_refresh_firewall = self.global_refresh_firewall
        self.devices_to_refilter = set()
        self.global_refresh_firewall = False

        if global_refresh_firewall:
            LOG.info(_("Refreshing firewall for all filtered devices"))
            self.refresh_firewall()
        else:
            # If a device is both in new and updated devices
            # avoid reprocessing it
            updated_devices = (devices_to_refilter - new_devices)
            if updated_devices:
                LOG.info(_("Refreshing firewall for %d devices"),
                          len(updated_devices))
                self.refresh_firewall(updated_devices)

    def clean_device(self, device):
        LOG.info(_("Clean port filter info for %s"), device)
        if device:
            self.device_sg.pop(device, None)
            self.device_iptables.pop(device, None)


class HaproxyNSDriver(n_rpc.RpcCallback,SecurityGroupAgentRpcNamespaceMixin,
                      agent_device_driver.AgentDeviceDriver):

    RPC_API_VERSION = '1.3'

    def __init__(self, conf, plugin_rpc, context):
        super(HaproxyNSDriver, self).__init__()
        self.conf = conf
        self.context = context

        self.ovs_plugin_rpc = OVSPluginApi(topics.PLUGIN)
        self.root_helper = config.get_root_helper(conf)
        #the config file,the pid file and the sock file will be stored under
        #the specified state_path.
        self.state_path = '/'.join([self.conf.haproxy.loadbalancer_state_path,
                                    STATE_PATH_V2_APPEND])
        try:
            vif_driver = importutils.import_object(conf.interface_driver, conf)
        except ImportError:
            with excutils.save_and_reraise_exception():
                msg = (_('Error importing interface driver: %s')
                       % conf.interface_driver)
                LOG.error(msg)

        self.vif_driver = vif_driver
        self.plugin_rpc = plugin_rpc
        self.loadbalancer_to_port_id = {}

        self._setup_sg_rpc()


    def _setup_sg_rpc(self):
        # RPC network init
        # Handle updates from service
        self.endpoints = [self]
        # Define the listening consumers for the agent
        consumers = [[topics.SECURITY_GROUP, topics.UPDATE, self.conf.host],
                     [topics.PORT, topics.UPDATE, self.conf.host]]

        self.connection = agent_rpc.create_consumers(self.endpoints,
                                                     topics.AGENT,
                                                     consumers)


    @classmethod
    def get_name(cls):
        return DRIVER_NAME

    def create_vip(self, vip):
        pass

    def update_vip(self, old_vip, vip):
        pass

    def delete_vip(self, vip):
        pass

    def create_pool(self, pool):
        pass

    def update_pool(self, old_pool, pool):
        pass

    def delete_pool(self, pool):
        pass

    def create_member(self, member):
        pass

    def update_member(self, old_member, member):
        pass

    def delete_member(self, member):
        pass

    def create_loadbalancer(self, loadbalancer):
        self._refresh_device(loadbalancer['id'])

    def update_loadbalancer(self, loadbalancer):
        self._refresh_device(loadbalancer['id'])

    def delete_loadbalancer(self, loadbalancer):
        LOG.info('delete_loadbalancer loadbalancer %s',loadbalancer['id'])
        self.undeploy_instance(loadbalancer['id'])

    def remove_orphans(self, known_loadbalancer_ids):
        if not os.path.exists(self.state_path):
            return

        orphans = (loadbalancer_id for loadbalancer_id in os.listdir(self.state_path)
                   if loadbalancer_id not in known_loadbalancer_ids)
        for loadbalancer_id in orphans:
            if self.exists_loadbalancer_instance_check(loadbalancer_id):
                self.undeploy_instance(loadbalancer_id, cleanup_namespace=True)

    def _refresh_device(self, loadbalancer_id):
        logical_config = self.plugin_rpc.get_logical_device(loadbalancer_id)
        if not logical_config:
            LOG.error('_refresh_device loadbalancer %s config error.',loadbalancer_id)
            return
        self.deploy_instance(logical_config)

    def check_loadbalancer_deployable(self, logical_config):
        if (not logical_config or
                    'status' not in logical_config or
                    (logical_config['status'] not in
                     constants.ACTIVE_PENDING_STATUSES
                    and logical_config['status']!=constants.DEFERRED) or
                    not logical_config['admin_state_up'] or
                    'vip_port' not in logical_config or
                    len(logical_config['listeners']) <=0 ):
            LOG.info("check_loadbalancer_deployable loadbalancer %s"
                "not deployable.",logical_config)
            return False
        for listener in logical_config['listeners']:
            if listener['admin_state_up'] and listener['status']!=constants.PENDING_DELETE:
                LOG.info("check_loadbalancer_deployable loadbalancer %s"
                    "not deployable for listener state/status.",logical_config)
                return True
        return False

    @n_utils.synchronized('haproxy-driver')
    def deploy_instance(self, logical_config):

        if self.exists_loadbalancer_instance_check(logical_config['id']):
            self.update_loadbalancer_instance(logical_config)
        else:
            # do actual deploy loadbalancer instance
            # only if the status is OK,listener(s) exist(s)
            if not self.check_loadbalancer_deployable(logical_config):
                return
            self.create_loadbalancer_instance(logical_config)

    @n_utils.synchronized('haproxy-driver')
    def undeploy_instance(self, loadbalancer_id, cleanup_namespace=True):
        namespace = get_ns_name(loadbalancer_id)
        ns = ip_lib.IPWrapper(self.root_helper, namespace)
        pid_path = self._get_state_file_path(loadbalancer_id, 'pid')

        # kill the process
        kill_pids_in_file(self.root_helper, pid_path)

        # unplug the ports
        if loadbalancer_id in self.loadbalancer_to_port_id:
            self.clean_device(self.loadbalancer_to_port_id[loadbalancer_id])
            self._unplug_instance_port(namespace, self.loadbalancer_to_port_id[loadbalancer_id])

        # delete all devices from namespace;
        # used when deleting orphans and port_id is not known for loadbalancer_id
        if cleanup_namespace:
            for device in ns.get_devices(exclude_loopback=True):
                self.vif_driver.unplug(device.name, namespace=namespace)

        # remove the configuration directory
        conf_dir = os.path.dirname(self._get_state_file_path(loadbalancer_id, ''))
        if os.path.isdir(conf_dir):
            shutil.rmtree(conf_dir)
        ns.garbage_collect_namespace()

    def _get_state_file_path(self, loadbalancer_id, kind, ensure_state_dir=True):
        """Returns the file name for a given kind of config file."""
        confs_dir = os.path.abspath(os.path.normpath(self.state_path))
        conf_dir = os.path.join(confs_dir, loadbalancer_id)
        if ensure_state_dir:
            if not os.path.isdir(conf_dir):
                os.makedirs(conf_dir, 0o755)
        return os.path.join(conf_dir, kind)

    def exists_loadbalancer_instance_check(self, loadbalancer_id):
        namespace = get_ns_name(loadbalancer_id)
        root_ns = ip_lib.IPWrapper(self.root_helper)

        socket_path = self._get_state_file_path(loadbalancer_id, 'sock', False)
        if root_ns.netns.exists(namespace) and os.path.exists(socket_path):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(socket_path)
                return True
            except socket.error:
                pass
        return False

    def create_loadbalancer_instance(self, logical_config):
        loadbalancer_id = logical_config['id']
        namespace = get_ns_name(loadbalancer_id)

        self._plug_instance_port(namespace, logical_config['vip_port'])
        self._spawn_instance(logical_config)

    def update_loadbalancer_instance(self, logical_config):

        loadbalancer_id = logical_config['id']
        devices_added = set()
        devices_added.add(logical_config['vip_port_id'])

        pid_path = self._get_state_file_path(loadbalancer_id, 'pid')

        extra_args = ['-sf']
        extra_args.extend(p.strip() for p in open(pid_path, 'r'))
        self._spawn_instance(logical_config, extra_args)
        self.setup_port_filters(devices_added)

    def _plug_instance_port(self, namespace, port, reuse_existing=True):

        #RPC to update db for vip information
        self.plugin_rpc.plug_vip_port(port['id'])

        devices_added = set()
        devices_added.add(port['id'])
        interface_name = self.vif_driver.get_device_name(Wrap(port))

        if ip_lib.device_exists(interface_name, self.root_helper, namespace):
            if not reuse_existing:
                raise exceptions.PreexistingDeviceFailure(
                    dev_name=interface_name
                )
        else:
            self.vif_driver.plug(
                port['network_id'],
                port['id'],
                interface_name,
                port['mac_address'],
                namespace=namespace
            )
        LOG.debug(_(' _plug_instance_port for %(port)s fixed_ips %(fixed_ips)s '),
                        {'port': port, 'fixed_ips': port['fixed_ips']})
        cidrs = [
            '%s/%s' % (ip['ip_address'],
                       netaddr.IPNetwork(ip['subnet']['cidr']).prefixlen)
            for ip in port['fixed_ips']
        ]
        self.vif_driver.init_l3(interface_name, cidrs, namespace=namespace)

        gw_ip = port['fixed_ips'][0]['subnet'].get('gateway_ip')

        if not gw_ip:
            host_routes = port['fixed_ips'][0]['subnet'].get('host_routes', [])
            for host_route in host_routes:
                if host_route['destination'] == "0.0.0.0/0":
                    gw_ip = host_route['nexthop']
                    break

        if gw_ip:
            cmd = ['route', 'add', 'default', 'gw', gw_ip]
            ip_wrapper = ip_lib.IPWrapper(self.root_helper,
                                          namespace=namespace)
            ip_wrapper.netns.execute(cmd, check_exit_code=False)
            # When delete and re-add the same vip, we need to
            # send gratuitous ARP to flush the ARP cache in the Router.
            gratuitous_arp = self.conf.haproxy.send_gratuitous_arp
            if gratuitous_arp > 0:
                for ip in port['fixed_ips']:
                    cmd_arping = ['arping', '-U',
                                  '-I', interface_name,
                                  '-c', gratuitous_arp,
                                  ip['ip_address']]
                    ip_wrapper.netns.execute(cmd_arping, check_exit_code=False)
        self.setup_port_filters(devices_added)

    def _spawn_instance(self, logical_config, extra_cmd_args=()):
        loadbalancer_id = logical_config['id']
        namespace = get_ns_name(loadbalancer_id)

        conf_path = self._get_state_file_path(loadbalancer_id, 'conf')
        pid_path = self._get_state_file_path(loadbalancer_id, 'pid')
        sock_path = self._get_state_file_path(loadbalancer_id, 'sock')
        user_group = self.conf.haproxy.user_group

        haproxycfg.save_config(conf_path, logical_config, sock_path, user_group)
        cmd = ['haproxy', '-f', conf_path, '-p', pid_path]
        cmd.extend(extra_cmd_args)

        ns = ip_lib.IPWrapper(self.root_helper, namespace)
        ns.netns.execute(cmd)

        # remember the loadbalancer<-->port mapping
        self.loadbalancer_to_port_id[loadbalancer_id] = logical_config['vip_port_id']

    def _unplug_instance_port(self, namespace, port_id):
        port_stub = {'id': port_id}
        self.plugin_rpc.unplug_vip_port(port_id)
        interface_name = self.vif_driver.get_device_name(Wrap(port_stub))
        self.vif_driver.unplug(interface_name, namespace=namespace)

    def get_stats(self, loadbalancer_id):
        socket_path = self._get_state_file_path(loadbalancer_id, 'sock', False)
        TYPE_FRONTEND_REQUEST = 1
        TYPE_BACKEND_REQUEST = 2
        TYPE_SERVER_REQUEST = 4
        loadbalancer_stats= {}
        if os.path.exists(socket_path):
            parsed_stats = self._get_stats_from_socket(
                socket_path,
                entity_type=TYPE_FRONTEND_REQUEST | TYPE_BACKEND_REQUEST | TYPE_SERVER_REQUEST )
            loadbalancer_stats_frontends= self._get_frontend_stats(parsed_stats)
            loadbalancer_stats_backends = self._get_backend_stats(parsed_stats)
            loadbalancer_stats['frontends_stats'] = loadbalancer_stats_frontends
            loadbalancer_stats['backends_stats'] =  loadbalancer_stats_backends
            loadbalancer_stats['members_stats'] =   self._get_servers_stats(parsed_stats)
            loadbalancer_stats['members'] = self._get_servers_stats_for_plugin(parsed_stats)
            # we compute the statistics for loadbalancer itself
            for frontend in loadbalancer_stats_frontends:
                frontend_id = frontend['id']
                for k,v in haproxycfg.STATS_MAP.items():
                    if k == 'id':
                        continue
                    LOG.debug(_('Stats %(key)s for frontend %(frontend)s is %(stats)s'),
                      {
                       'key':k, 'frontend':frontend_id,
                       'stats':frontend[k]
                      }
                    )
                    if frontend[k]!='':
                        loadbalancer_stats[k] = loadbalancer_stats.get(k,'0')
                        loadbalancer_stats[k]=  str(int(frontend[k])
                            +int(loadbalancer_stats[k]))
            loadbalancer_stats['id'] = loadbalancer_id
            return loadbalancer_stats
        else:
            LOG.error(_('Stats socket not found for loadbalancer %s'), loadbalancer_id)
            return {}

    def _get_frontend_stats(self, parsed_stats):
        TYPE_FRONTEND_RESPONSE = '0'
        unified_stats = []
        for stats in parsed_stats:
            if stats.get('type') == TYPE_FRONTEND_RESPONSE:
                result_stats = dict((k, stats.get(v, ''))
                                     for k, v in haproxycfg.STATS_MAP.items())
                result_stats['id'] = stats['pxname']
                unified_stats.append(result_stats)
        return unified_stats

    def _get_backend_stats(self, parsed_stats):
        TYPE_BACKEND_RESPONSE = '1'
        unified_stats = []
        for stats in parsed_stats:
            if stats.get('type') == TYPE_BACKEND_RESPONSE:
                result_stats = dict((k, stats.get(v, ''))
                                     for k, v in haproxycfg.STATS_MAP.items())
                result_stats['id'] = stats['pxname']
                unified_stats.append(result_stats)
        return unified_stats

    def _get_servers_stats(self, parsed_stats):
        TYPE_SERVER_RESPONSE = '2'
        result = []
        for stats in parsed_stats:
            if stats.get('type') == TYPE_SERVER_RESPONSE:
                res = dict((k, stats.get(v, ''))
                                     for k, v in haproxycfg.STATS_MAP.items())
                res['id'] = stats['svname']
                result.append(res)
        return result

    def _get_servers_stats_for_plugin(self, parsed_stats):
        TYPE_SERVER_RESPONSE = '2'
        res = {}
        for stats in parsed_stats:
            if stats.get('type') == TYPE_SERVER_RESPONSE:
                res[stats['svname']] = {
                    lb_const.STATS_STATUS: (constants.INACTIVE
                                            if stats['status'] == 'DOWN'
                                            else constants.ACTIVE),
                    lb_const.STATS_HEALTH: stats['check_status'],
                    lb_const.STATS_FAILED_CHECKS: stats['chkfail']
                }
        return res

    def _get_stats_from_socket(self, socket_path, entity_type):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(socket_path)
            s.send('show stat -1 %s -1\n' % entity_type)
            raw_stats = ''
            chunk_size = 1024
            while True:
                chunk = s.recv(chunk_size)
                raw_stats += chunk
                if len(chunk) < chunk_size:
                    break

            return self._parse_stats(raw_stats)
        except socket.error as e:
            LOG.warn(_('Error while connecting to stats socket: %s'), e)
            return {}

    def _parse_stats(self, raw_stats):
        stat_lines = raw_stats.splitlines()
        if len(stat_lines) < 2:
            return []
        stat_names = [name.strip('# ') for name in stat_lines[0].split(',')]
        res_stats = []
        for raw_values in stat_lines[1:]:
            if not raw_values:
                continue
            stat_values = [value.strip() for value in raw_values.split(',')]
            res_stats.append(dict(zip(stat_names, stat_values)))

        return res_stats

    def create_pool_health_monitor(self, health_monitor, pool_id):
        pass

    def update_pool_health_monitor(self, old_health_monitor, health_monitor,
                                   pool_id):
        pass

    def delete_pool_health_monitor(self, health_monitor, pool_id):
        pass


# NOTE For compliance with interface.py which expects objects
class Wrap(object):
    """A light attribute wrapper for compatibility with the interface lib."""
    def __init__(self, d):
        self.__dict__.update(d)

    def __getitem__(self, key):
        return self.__dict__[key]

def get_ns_name(namespace_id):
    return NS_PREFIX + namespace_id


def kill_pids_in_file(root_helper, pid_path):
    if os.path.exists(pid_path):
        with open(pid_path, 'r') as pids:
            for pid in pids:
                pid = pid.strip()
                try:
                    utils.execute(['kill', '-9', pid], root_helper)
                except RuntimeError:
                    LOG.exception(
                        _('Unable to kill haproxy process: %s'),
                        pid
                    )


