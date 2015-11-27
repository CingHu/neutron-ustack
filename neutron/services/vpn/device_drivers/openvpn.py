# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright 2013, Nachi Ueno, NTT I3, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software

#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
import abc
import os
import time
import shutil
import sys

import jinja2
from oslo.config import cfg
from oslo import messaging
import six

from neutron.agent.linux import ip_lib
from neutron.agent.linux import utils
from neutron.agent import l3_agent
from neutron.api.rpc.agentnotifiers import helo_rpc_agent_api
from neutron.common import rpc as q_rpc
from neutron import context
from neutron.openstack.common import jsonutils
from neutron.openstack.common import log as logging
from neutron.openstack.common import lockutils
from neutron.plugins.common import constants
from neutron.services.vpn.common import topics
from neutron.services.vpn.common import constants as vpn_constants
from neutron.services.vpn import device_drivers
from neutron.services.vpn import ca


reload(sys)
sys.setdefaultencoding('utf-8')

LOG = logging.getLogger('openvpn_agent')

TEMPLATE_PATH = os.path.dirname(__file__)
NS_PREFIX = l3_agent.NS_PREFIX

openvpn_opts = [
    cfg.StrOpt('openvpn_ca_file', default='$state_path/openvpn/ca/ca.crt',
                help=_('ca absolute path for server and client')),

    cfg.StrOpt('openvpn_dh_file', default='$state_path/openvpn/ca/dh.pem',
                help=_('ca absolute path for server and client')),

    cfg.StrOpt('openvpn_ca_file', default='$state_path/openvpn/ca/ca.crt',
                help=_('ca absolute path for server and client')),

    cfg.StrOpt(
        'config_base_dir',
        default='$state_path/openvpn',
        help=_('Location to store pptp server config files')),

    cfg.StrOpt(
        'openvpn_config_template',
        default=os.path.join(
            TEMPLATE_PATH,
            'template/openvpn/openvpn.conf.template'),
        help=_('Template file for openvpn configuration')),
]

cfg.CONF.register_opts(openvpn_opts, 'openvpn')

default = {'ca_file':cfg.CONF.openvpn.openvpn_ca_file,
           'server_dh':cfg.CONF.openvpn.openvpn_dh_file,
           'max_client':vpn_constants.MAX_CLIENT,
           'heatbeat_interval':vpn_constants.HEATBEAT_INTERVAL,
           'reconnection_time':vpn_constants.RECONNECTION_TIME,
           'client_to_client':vpn_constants.CLIENT_TO_CLIENT,
           'log_level':vpn_constants.LOG_LEVEL,
           'aes_256_cbc':vpn_constants.AES_256_CBC}

JINJA_ENV = None


def _get_template(template_file):
    global JINJA_ENV
    if not JINJA_ENV:
        templateLoader = jinja2.FileSystemLoader(searchpath="/")
        JINJA_ENV = jinja2.Environment(loader=templateLoader)
    return JINJA_ENV.get_template(template_file)

def get_pid_file(id):
    config_dir = os.path.join(
            cfg.CONF.openvpn.config_base_dir, id)
    return os.path.join(
                config_dir, 'var', 'run', 'openvpn.pid')

@six.add_metaclass(abc.ABCMeta)
class BaseProcess():
    """Swan Family Process Manager

    This class manages start/restart/stop openvpn process.
    This class create/delete config template
    """

    binary = "openvpn"
    CONFIG_DIRS = [
        'var/run',
        'log',
        'etc',
        'etc/openvpn',
    ]

    def __init__(self, conf, root_helper, process_id,
                 openvpn_service, namespace):
        self.conf = conf
        self.id = process_id
        self.root_helper = root_helper
        self.openvpn_service = openvpn_service
        self.namespace = namespace
        self.config_dir = os.path.join(
            cfg.CONF.openvpn.config_base_dir, self.id)
        self.etc_dir = os.path.join(self.config_dir, 'etc', 'openvpn')


    @abc.abstractmethod
    def ensure_configs(self):
        pass


    def ensure_config_file(self, kind, template, openvpn_service, defaults=None):
        """Update config file,  based on current settings for service."""
        config_str = self._gen_config_content(template, openvpn_service, defaults=defaults)
        config_file_name = self._get_config_filename(kind)
        LOG.info(_("openvpn config file:%s" % config_str))
        utils.replace_file(config_file_name, unicode(config_str), mode=0o600)

    def remove_config(self):
        """Remove whole config file."""
        shutil.rmtree(self.config_dir, ignore_errors=True)

    def _get_config_filename(self, kind):
        config_dir = self.etc_dir
        return os.path.join(config_dir, kind)

    def _ensure_dir(self, dir_path):
        if not os.path.isdir(dir_path):
            os.makedirs(dir_path, 0o755)

    def ensure_config_dir(self, openvpn_service):
        """Create config directory if it does not exist."""
        self._ensure_dir(self.config_dir)
        for subdir in self.CONFIG_DIRS:
            dir_path = os.path.join(self.config_dir, subdir)
            self._ensure_dir(dir_path)

    def _gen_config_content(self, template_file, openvpn_service, defaults):
        template = _get_template(template_file)
        return template.render(
            {'openvpn_service': openvpn_service,
             'defaults': defaults})

    @property
    def status(self):
        if self.active:
            return constants.ACTIVE
        return constants.DOWN

    @abc.abstractmethod
    def active(self):
        """Check if the process is active or not."""
        pass

    def update(self):
        """Update Status based on openvpn_service configuration."""
        if self.openvpn_service and not self.openvpn_service['admin_state_up']:
            self.disable()
        else:
            self.enable()

    def enable(self):
        """Enabling the process."""
        try:
            self.ensure_configs()
            if self.active:
                self.restart()
            else:
                self.start()
        except RuntimeError:
            LOG.exception(
                _("Failed to enable vpn process on router %s"),
                self.id)

    def disable(self):
        """Disabling the process."""
        try:
            if self.active:
                self.stop()
            self.remove_config()
        except RuntimeError:
            LOG.exception(
                _("Failed to disable vpn process on router %s"),
                self.id)

    @abc.abstractmethod
    def restart(self):
        """Restart process."""

    def reload(self):
        """Reload config for process"""
        if self.openvpn_service and not self.openvpn_service['admin_state_up']:
            LOG.debug("openvpn realod, stop")
            self.stop()
        else:
            if not self.active:
                LOG.debug("openvpn realod, start")
                self.start()

    @abc.abstractmethod
    def start(self):
        """Start process."""

    @abc.abstractmethod
    def stop(self):
        """Stop process."""


class OpenVPNProcess(BaseProcess):
    """OpenSwan Process manager class.

    This process class uses three commands
    openvpn --config openvpn.conf --writepid /var/run/openvpn.pid --log-append log/openvpn.log --daemon openvpn
    """
    def __init__(self, conf, root_helper, process_id,
                 openvpn_service, namespace):
        super(OpenVPNProcess, self).__init__(
            conf, root_helper, process_id,
            openvpn_service, namespace)
        self.config_file = os.path.join(
            self.etc_dir, 'openvpn.conf')
        self.pid_file = os.path.join(
            self.config_dir, 'var', 'run', 'openvpn.pid')
        self.log_file = os.path.join(
            self.config_dir, 'log', 'openvpn.log')

    def _execute(self, cmd, check_exit_code=True):
        """Execute command on namespace."""
        ip_wrapper = ip_lib.IPWrapper(self.root_helper, self.namespace)
#         ip_wrapper = ip_lib.IPWrapper('sudo', self.namespace)
        return ip_wrapper.netns.execute(
            cmd,
            check_exit_code=check_exit_code)

    def ensure_configs(self):
        """Generate config files which are needed for openvpn.

        If there is no directory, this function will create
        dirs.
        """
        #self.openvpn_service['openvpn_file'] = self.openvpn_file
        self.ensure_config_dir(self.openvpn_service)
        self.ensure_config_file(
            'openvpn.conf',
            self.conf.openvpn.openvpn_config_template,
            self.openvpn_service,
            defaults = default)

    def restart(self):
        """Restart the process."""
        self.stop()
        time.sleep(0.1)
        self.start()
        return

    def start(self):
        """Start the process.

        Note: if there is not namespace yet,
        just do nothing, and wait next event.
        """
        if not self.namespace:
            LOG.warn("start openvpn process, It have not namespace")
            return

        LOG.info("start openvpn process, config file:%s" % self.config_file)
        #start openvpn --config  openvpn.conf --writepid file  --log-append file --daemon
        self._execute([self.binary,
                      '--config', self.config_file,
                      '--writepid', self.pid_file,
                      '--log-append', self.log_file,
                      '--daemon', 'openvpn'
                    ])

    @property
    def pid(self):
        """Last known pid for this external process spawned for this uuid."""
        file_name = self.pid_file
        msg = _('Error while reading %s')

        try:
            with open(file_name, 'r') as f:
                return int(f.read())
        except IOError:
            msg = _('Unable to access %s')
        except ValueError:
            msg = _('Unable to convert value in %s')
        LOG.debug(msg, file_name)
        return None

    def stop(self):
        pid = self.pid

        if self.active:
            cmd = ['kill', '-9', pid]
            utils.execute(cmd, self.root_helper)
        elif pid:
            LOG.debug(_('Process for %(uuid)s pid %(pid)d is stale, ignoring '
                        'command'), {'uuid': self.id, 'pid': pid})
        else:
            LOG.debug(_('No process started for %s'), self.uuid)

    @property
    def active(self):
        if not self.namespace:
            return False
        pid = self.pid
        if pid is None:
            return False

        cmdline = '/proc/%s/cmdline' % pid
        try:
            with open(cmdline, "r") as f:
                return self.id in f.readline()
        except IOError:
            return False

class OpenVPNDriverApi(q_rpc.RpcProxy):
    """OpenVPNDriver RPC api."""
    OPENVPN_PLUGIN_VERSION = '1.0'

    def get_vpn_services_on_host(self, context, routers, host):
        """Get list of openvpn_services.

        The openvpn_services including related openvpn_connection,
        and users on this host
        """
        return self.call(context,
                         self.make_msg('get_vpn_services_on_host',
                                       host=host,
                                       routers=routers),
                         version=self.OPENVPN_PLUGIN_VERSION,
                         topic=self.topic)

    def update_status(self, context, status):
        """Update local status.

        This method call updates status attribute of
        VPNServices.
        """
        return self.cast(context,
                         self.make_msg('update_status',
                                       status=status),
                         version=self.OPENVPN_PLUGIN_VERSION,
                         topic=self.topic)


@six.add_metaclass(abc.ABCMeta)
class OpenVPNBaseDriver(device_drivers.DeviceDriver,
                      helo_rpc_agent_api.HeloRpcCallbackMixin):
    """VPN Device Driver for openvpn.

    This class is designed for use with L3-agent now.
    However this driver will be used with another agent in future.
    so the use of "Router" is kept minimul now.
    Insted of router_id,  we are using process_id in this code.
    """

    # history
    #   1.0 Initial version

    RPC_API_VERSION = '1.0'

    def __init__(self, agent, host):
        self.agent = agent
        self.conf = self.agent.conf
        self.root_helper = self.agent.root_helper
        self.context = context.get_admin_context_without_session()
        self.host = host
        self.processes = {}
        self.process_status_cache = {}

        self.updated_openvpn_services = set()
        self.added_openvpn_services = set()
        self.deleted_openvpn_services = set()
        self.restore_openvpn_services = list()

        self.conn = q_rpc.create_connection(new=True)
        self.topic = topics.OPENVPN_AGENT_TOPIC
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
        self.agent_rpc = OpenVPNDriverApi(topics.OPENVPN_DRIVER_TOPIC, '1.0')
        self.conn.consume_in_threads()

    def create_rpc_dispatcher(self):
        # return q_rpc.PluginRpcDispatcher([self])
        return [self]

    @lockutils.synchronized('vpn-agent', 'neutron-')
    def vpnservice_updated(self, context, router_id):
        self.updated_openvpn_services.add(router_id)

    @lockutils.synchronized('vpn-agent', 'neutron-')
    def vpnservice_created(self, context, router_id):
        self.added_openvpn_services.add(router_id)

    @lockutils.synchronized('vpn-agent', 'neutron-')
    def vpnservice_deleted(self, context, router_id):
        self.deleted_openvpn_services.add(router_id)

    @abc.abstractmethod
    def create_process(self, process_id, openvpn_service, namespace):
        pass

    def kill_dead_process(self, router_id):

        def pid(router_id):
            """Last known pid for this external process spawned for this uuid."""
            file_name = get_pid_file(router_id)
            msg = _('Error while reading %s')
            try:
                with open(file_name, 'r') as f:
                    return int(f.read())
            except IOError:
                msg = _('Unable to access %s')
            except ValueError:
                msg = _('Unable to convert value in %s')
            LOG.debug(msg, file_name)
            return None

        def _remove_config():
            config_dir = os.path.join(
            cfg.CONF.openvpn.config_base_dir, router_id)
            shutil.rmtree(config_dir, ignore_errors=True)

        def kill(id):
            try:
                cmd = ['kill', '-9', id]
                utils.execute(cmd, self.root_helper)
                _remove_config()
                LOG.debug('kill process of openvpn success, router_id' % router_id)
            except:
                LOG.error('kill process failed')

        process_id = pid(router_id)
        if process_id is None:
            return
        kill(process_id)

    def gen_server_ca(self, openvpn_service, is_added=False):
        pass

    def ensure_process(self, process_id, openvpn_service):
        """Ensuring process.

        If the process doesn't exist, it will create process
        and store it in self.processs
        """
        process = self.processes.get(process_id)
        openvpn_cons=openvpn_service
        router_id = openvpn_service['router_id']
        if not ca.file_is_exists(openvpn_service['id']):
            openvpn_cons = self.gen_server_ca(openvpn_service, is_added=True)
        else:
            openvpn_cons = self.gen_server_ca(openvpn_service, is_added=False)

        if not process or not process.namespace:
            namespace = self.agent.get_namespace(process_id)
            process = self.create_process(
                process_id,
                openvpn_cons,
                namespace)
            self.processes[process_id] = process

        if openvpn_cons:
            process.openvpn_service = openvpn_cons
        return process

    @lockutils.synchronized('vpn-agent', 'neutron-')
    def get_openvpn_info(self):
        added_routerids = list(self.added_openvpn_services)
        updated_routerids = list(self.updated_openvpn_services)
        deleted_ids = list(self.deleted_openvpn_services)

        self.updated_openvpn_services.clear()
        self.added_openvpn_services.clear()
        self.deleted_openvpn_services.clear()

        return [added_routerids, updated_routerids, deleted_ids]

    @lockutils.synchronized('vpn-agent', 'neutron-')
    def set_openvpn_updated(self, routerids):
        [self.updated_openvpn_services.add(router) for router in routerids]

    def restore_openvpn(self):
        routerids = self.agent.router_info.keys()
        if not routerids:
            return

        restore_vpn_router_ids = list(set(routerids) -\
                                    set(self.restore_openvpn_services))
        if restore_vpn_router_ids:
            self.set_openvpn_updated(restore_vpn_router_ids)
            self.restore_openvpn_services += restore_vpn_router_ids

    def _get_local_openvpn_process(self):
        local_process_ids = []
        namespaces = self.agent._list_namespaces()
        for ns in namespaces:
            if not ns.startswith(NS_PREFIX):
                continue
            router_id = ns[len(NS_PREFIX):]
            local_process_ids.append(router_id)

        return local_process_ids


    def _sync_vpnservices(self):
        openvpn_services = []
        try:
            openvpn_services = self.agent_rpc.get_vpn_services_on_host(
                self.context, [], self.host)
        except Exception:
            LOG.exception(_("Failed synchronizing vpn service"))
            return

        routerids = [openvpn['router_id'] for openvpn in openvpn_services] 

        local_process_ids= self._get_local_openvpn_process()
        remove_ids = list(set(local_process_ids)-set(routerids))
        LOG.info('disable openvpn sevice, routerids:%s' % remove_ids)
        for id in remove_ids:
            self.kill_dead_process(id)

    def _rpc_loop_nosync(self):
        # _rpc_loop and _sync_routers_task will not be
        # executed in the same time because of lock.
        # so we can clear the value of updated_routers
        # and removed_routers

        self.restore_openvpn()

        [added_routerids, updated_routerids,deleted_ids] = \
                              self.get_openvpn_info()

        if added_routerids or updated_routerids or deleted_ids:
            LOG.info("added_routerids %s", str(added_routerids))
            LOG.info("updated_routerids %s", str(updated_routerids))
            LOG.info("deleted_routerids %s", str(deleted_ids))
            LOG.info("processes keys  %s", str(self.processes.keys()))

        for router_id in deleted_ids:
            if router_id in added_routerids:
                added_routerids.remove(router_id)

        if deleted_ids:
            self.deleted_rpc_loop_nosync_service(deleted_ids)

        if added_routerids:
            self._process_router_openvpn_services(added_routerids)

        if updated_routerids:
            self._process_router_openvpn_services(updated_routerids)

    def deleted_rpc_loop_nosync_service(self, deleted_ids):
        #process delete operation
        for router_id in deleted_ids:
            if router_id in self.processes:
                try:
                    process = self.processes[router_id]
                    process.disable()
                    del self.processes[router_id]
                    self._update_nat(process.openvpn_service, addflag=False)
                except Exception:
                    LOG.exception(_("Failed to delete openvpn for router %s"),
                                  router_id)

    def _sync_vpnservices_task_nosync(self, context=None):
        LOG.debug(_("Starting _sync_vpnservices_task_nosync - fullsync:%s"),
                  self.agent.fullsync)
        if self.agent.fullsync:
            # the l3 agent will sync
            LOG.info(_("_sync_vpnservices_task_nosync"
                        " quit due to router sync needs"))
            return
        self.processes.clear()
        self.added_openvpn_services.clear()
        self.updated_openvpn_services.clear()
        self.deleted_openvpn_services.clear()
        routerids = self.agent.router_info.keys()
        if not routerids:
            return

        self._process_router_openvpn_services(routerids, fullrouters=True)
        LOG.debug(_("_sync_vpnservices_task_nosync"
                    "successfully completed"))

    def _process_router_openvpn_services(self, routerids, fullrouters=False):
        openvpn_services = []
        try:
            openvpn_services = self.agent_rpc.get_vpn_services_on_host(
                self.context, routerids, self.host)
        except Exception:
            LOG.exception(_("Failed synchronizing vpn service"))
            self.agent.fullsync = True
            return

        if LOG.logger.isEnabledFor(10):
            LOG.debug(_('Processing :%s'),
                      jsonutils.dumps(openvpn_services, indent=5))

        vpn_statuses = []
        routers_with_vpn = set()
        for openvpn_service in openvpn_services:
            routers_with_vpn.add(openvpn_service['router_id'])
            try:
                process = self.ensure_process(openvpn_service['router_id'],
                                              openvpn_service)
                process.update()

                oldstatus = openvpn_service['status']
                newstatus = process.status
                self._update_nat(process.openvpn_service,
                                 addflag=newstatus == constants.ACTIVE)
                if oldstatus != newstatus:
                    vpn_statuses.append({'id': openvpn_service['id'],
                                         'tenant_id': openvpn_service['tenant_id'],
                                         'status': newstatus})
            except Exception:
                LOG.exception(_("Failed processing vpn service %s"),
                              openvpn_service)
                vpn_statuses.append({'id': openvpn_service['id'],
                                     'tenant_id': openvpn_service['tenant_id'],
                                     'status': constants.ERROR})
        try:
            self.report_vpn_status(vpn_statuses)
        except Exception:
            LOG.exception(_("Failed to report vpn_status %s"),
                          vpn_statuses)

        #only l3 agent may delete namespace of router
        if fullrouters:
            routers_without_vpn = set(routerids) - routers_with_vpn
            for routerid in routers_without_vpn:
                self._disable_service(
                    self.agent.get_namespace(routerid))

    def _get_ex_gw_ip(self, openvpn_service, ri):

        ex_gw_port = self.agent._get_ex_gw_port(ri)
        if not ex_gw_port:
            return openvpn_service

        ex_gw_ip = ex_gw_port['fixed_ips'][0]['ip_address']

        openvpn_service['ex_gw_ip'] = ex_gw_ip

        return openvpn_service

    def _update_nat(self, openvpn_service, addflag=True):
        """Setting up nat rule in iptables.

        We need to setup nat rule for vpn packet.
        :param openvpn_service: openvpn_services
        excample: iptables -t nat -A POSTROUTING -s 10.8.0.0/24 -o eth0 -j MASQUERADE
                  iptables -t nat -A POSTROUTING -s 10.8.0.0/24 -o DEVICE -j SNAT --to-source LOCAL_IP_ADDRESS
        """
        router_id = openvpn_service['router_id']

        ri = self.agent.router_info.get(router_id)
        if not ri:
            return

        old_gw_ip = openvpn_service.get('ex_gw_ip', None)
        openvpn_cons = self._get_ex_gw_ip(openvpn_service, ri)
        local_cidr = openvpn_service['peer_cidr']

        try:
            if addflag:
                rule = self.agent.internal_network_nat_rules(openvpn_cons.get('ex_gw_ip',None), local_cidr)[0]
                ri.iptables_manager.ipv4['nat'].add_rule(*rule)
            else:
                rule = self.agent.internal_network_nat_rules(old_gw_ip, local_cidr)[0]
                ri.iptables_manager.ipv4['nat'].remove_rule(*rule)
        except KeyError:
            return

        ri.iptables_manager.apply()

    def _disable_service(self, namespace):
        """Destroy the openvpn in router's namespace."""
        pm = OpenVPNProcess(self.conf, self.root_helper,
                         namespace[len('qrouter-'):],
                         None, namespace)
        pm.disable()

    def _destroy_router_namespace(self, namespace):
        pass

    def report_vpn_status(self, statuses):
        self.agent_rpc.update_status(self.context, statuses)

    def sync(self, context, routers):
        routerids = []
        for router in routers:
            namespace = self.agent.get_namespace(router['id'])
            if namespace is None:
                LOG.warn("namespace is NULL, id:%s", router['id'])
                continue

            routerids.append(router['id'])

        self.set_openvpn_updated(routerids)

    def create_router(self, process_id):
        pass

    def destroy_router(self, process_id):
        pass


class OpenVPNDriver(OpenVPNBaseDriver):

    def gen_server_ca(self, openvpn_service, is_added=False):
        self.defaults = {'ca_file':cfg.CONF.openvpn.openvpn_ca_file,
                        'server_dh':cfg.CONF.openvpn.openvpn_dh_file,
                        'max_client':vpn_constants.MAX_CLIENT,
                        'heatbeat_interval':vpn_constants.HEATBEAT_INTERVAL,
                        'reconnection_time':vpn_constants.RECONNECTION_TIME,
                        'client_to_client':vpn_constants.CLIENT_TO_CLIENT,
                        'log_level':vpn_constants.LOG_LEVEL,
                        'aes_256_cbc':vpn_constants.AES_256_CBC}
        if is_added:
            LOG.debug("generate server ca and key")
            ca.OpenVPNDBDrv().generate_server_ca(openvpn_service)

        prefix = ca.OpenVPNDBDrv().get_file_name(openvpn_service['id'])
        ca_info = {
                   'server_ca':prefix+'.crt',
                   'server_key':prefix+'.key',
                   'ta_key':prefix+'ta.key',
                  }
        openvpn_service.update(ca_info)
        return openvpn_service

    def create_process(self, process_id, openvpn_service, namespace):
        return OpenVPNProcess(
            self.conf,
            self.root_helper,
            process_id,
            openvpn_service,
            namespace)
