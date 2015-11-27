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
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
import abc
import os
import shutil

import jinja2
from oslo.config import cfg
from oslo import messaging
import six

from neutron.agent.linux import ip_lib
from neutron.agent.linux import utils
from neutron.api.rpc.agentnotifiers import helo_rpc_agent_api
from neutron.common import rpc as q_rpc
from neutron import context
from neutron.openstack.common import jsonutils
from neutron.openstack.common import lockutils
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants
from neutron.services.vpn.common import topics
from neutron.services.vpn import device_drivers


LOG = logging.getLogger('pptp_agent')
TEMPLATE_PATH = os.path.dirname(__file__)

pptp_opts = [
    cfg.StrOpt(
        'config_base_dir',
        default='$state_path/pptp',
        help=_('Location to store pptp server config files')),
    cfg.IntOpt('pptp_status_check_interval',
               default=60,
               help=_("Interval for checking pptpd status")),
    cfg.StrOpt(
        'pptpd_config_template',
        default=os.path.join(
            TEMPLATE_PATH,
            'template/pptp/pptpd.conf.template'),
        help=_('Template file for pptpd configuration')),
    cfg.StrOpt(
        'pptpd_ppp_template',
        default=os.path.join(
            TEMPLATE_PATH,
            'template/pptp/options.pptp.template'),
        help=_('Template file for pptp ppp options configuration')),
    cfg.StrOpt(
        'pptpd_chap_template',
        default=os.path.join(
            TEMPLATE_PATH,
            'template/pptp/chap-secrets.template'),
        help=_('Template file for ppp chap configuration')),
]
cfg.CONF.register_opts(pptp_opts, 'pptpd')


JINJA_ENV = None


def _get_template(template_file):
    global JINJA_ENV
    if not JINJA_ENV:
        templateLoader = jinja2.FileSystemLoader(searchpath="/")
        JINJA_ENV = jinja2.Environment(loader=templateLoader)
    return JINJA_ENV.get_template(template_file)


@six.add_metaclass(abc.ABCMeta)
class BaseProcess():
    """Swan Family Process Manager

    This class manages start/restart/stop pptpd process.
    This class create/delete config template
    """

    binary = "pptpd"
    CONFIG_DIRS = [
        'var/run',
        'log',
        'etc',
        'etc/pptpd',
    ]

    def __init__(self, conf, root_helper, process_id,
                 vpnservice, namespace):
        self.conf = conf
        self.id = process_id
        self.root_helper = root_helper
        self.vpnservice = vpnservice
        self.namespace = namespace
        self.config_dir = os.path.join(
            cfg.CONF.pptpd.config_base_dir, self.id)
        self.etc_dir = os.path.join(self.config_dir, 'etc', 'pptpd')

    @abc.abstractmethod
    def ensure_configs(self):
        pass

    def ensure_config_file(self, kind, template, vpnservice):
        """Update config file,  based on current settings for service."""
        config_str = self._gen_config_content(template, vpnservice)
        config_file_name = self._get_config_filename(kind)
        utils.replace_file(config_file_name, config_str, mode=0o600)

    def remove_config(self):
        """Remove whole config file."""
        shutil.rmtree(self.config_dir, ignore_errors=True)

    def _get_config_filename(self, kind):
        config_dir = self.etc_dir
        return os.path.join(config_dir, kind)

    def _ensure_dir(self, dir_path):
        if not os.path.isdir(dir_path):
            os.makedirs(dir_path, 0o755)

    def ensure_config_dir(self, vpnservice):
        """Create config directory if it does not exist."""
        self._ensure_dir(self.config_dir)
        for subdir in self.CONFIG_DIRS:
            dir_path = os.path.join(self.config_dir, subdir)
            self._ensure_dir(dir_path)

    def _gen_config_content(self, template_file, vpnservice):
        template = _get_template(template_file)
        return template.render(
            {'vpnservice': vpnservice})

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
        """Update Status based on vpnservice configuration."""
        if self.vpnservice and not self.vpnservice['admin_state_up']:
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

    @abc.abstractmethod
    def start(self):
        """Start process."""

    @abc.abstractmethod
    def stop(self):
        """Stop process."""


class PppdProcess(BaseProcess):
    """OpenSwan Process manager class.

    This process class uses three commands
    pptpd -c pptpd.conf -o options.pptp -p pid
    """
    def __init__(self, conf, root_helper, process_id,
                 vpnservice, namespace):
        super(PppdProcess, self).__init__(
            conf, root_helper, process_id,
            vpnservice, namespace)
        self.ppp_file = os.path.join(
            self.etc_dir, 'options.pptp')
        self.config_file = os.path.join(
            self.etc_dir, 'pptpd.conf')
        self.chap_secrets_file = os.path.join(
            self.etc_dir, 'chap-secrets')
        self.pid_file = os.path.join(
            self.config_dir, 'var', 'run', 'pptpd.pid')

    def _execute(self, cmd, check_exit_code=True):
        """Execute command on namespace."""
        ip_wrapper = ip_lib.IPWrapper(self.root_helper, self.namespace)
#         ip_wrapper = ip_lib.IPWrapper('sudo', self.namespace)
        return ip_wrapper.netns.execute(
            cmd,
            check_exit_code=check_exit_code)

    def process_users(self, tenant_users):
        self.vpnservice['_users'] = tenant_users
        self.ensure_config_file(
            'chap-secrets',
            self.conf.pptpd.pptpd_chap_template,
            self.vpnservice)

    def ensure_configs(self):
        """Generate config files which are needed for pptpd.

        If there is no directory, this function will create
        dirs.
        """
        self.vpnservice['pppoptionsfile'] = self.ppp_file
        self.vpnservice['pidfile'] = self.pid_file
        self.vpnservice['chap_secrets'] = self.chap_secrets_file
        self.ensure_config_dir(self.vpnservice)
        self.ensure_config_file(
            'pptpd.conf',
            self.conf.pptpd.pptpd_config_template,
            self.vpnservice)
        self.ensure_config_file(
            'chap-secrets',
            self.conf.pptpd.pptpd_chap_template,
            self.vpnservice)
        self.ensure_config_file(
            'options.pptp',
            self.conf.pptpd.pptpd_ppp_template,
            self.vpnservice)

    def restart(self):
        """Restart the process."""
        self.stop()
        self.start()
        return

    def start(self):
        """Start the process.

        Note: if there is not namespace yet,
        just do nothing, and wait next event.
        """
        if not self.namespace:
            return
        #start pptpd -c pptpd.conf
        self._execute([self.binary,
                       '-c', self.config_file,
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


class PptpVpnDriverApi(q_rpc.RpcProxy):
    """PptpVpnDriver RPC api."""
    PPTP_PLUGIN_VERSION = '1.0'

    def get_vpn_services_on_host(self, context, routers, host):
        """Get list of vpnservices.

        The vpnservices including related pptp_connection,
        and users on this host
        """
        return self.call(context,
                         self.make_msg('get_vpn_services_on_host',
                                       host=host,
                                       routers=routers),
                         version=self.PPTP_PLUGIN_VERSION,
                         topic=self.topic)

    def get_vpn_users(self, context, tenant_ids):
        """Get list of vpnusers.

        The vpnusers including related pptp_connection,
        and users on this host
        """
        return self.call(context,
                         self.make_msg('get_vpn_users',
                                       tenant_ids=tenant_ids),
                         version=self.PPTP_PLUGIN_VERSION,
                         topic=self.topic)

    def update_status(self, context, status):
        """Update local status.

        This method call updates status attribute of
        VPNServices.
        """
        return self.cast(context,
                         self.make_msg('update_status',
                                       status=status),
                         version=self.PPTP_PLUGIN_VERSION,
                         topic=self.topic)


@six.add_metaclass(abc.ABCMeta)
class PptpdBaseDriver(device_drivers.DeviceDriver,
                      helo_rpc_agent_api.HeloRpcCallbackMixin):
    """VPN Device Driver for pptpd.

    This class is designed for use with L3-agent now.
    However this driver will be used with another agent in future.
    so the use of "Router" is kept minimul now.
    Insted of router_id,  we are using process_id in this code.
    """

    # history
    #   1.0 Initial version

    RPC_API_VERSION = '1.0'
    target = messaging.Target(version=RPC_API_VERSION)

    def __init__(self, agent, host):
        self.agent = agent
        self.conf = self.agent.conf
        self.root_helper = self.agent.root_helper
        self.context = context.get_admin_context_without_session()
        self.host = host
        self.processes = {}
        self.process_status_cache = {}

        self.updated_vpnservices = set()
        self.deleted_vpnservices = set()
        self.updated_vpnusers = set()

        self.conn = q_rpc.create_connection(new=True)
        self.topic = topics.PPTP_AGENT_TOPIC
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
        self.agent_rpc = PptpVpnDriverApi(topics.PPTP_DRIVER_TOPIC, '1.0')
        self.conn.consume_in_threads()

    def create_rpc_dispatcher(self):
        # return q_rpc.PluginRpcDispatcher([self])
        return [self]

    def vpnservice_updated(self, context, router_id):
        self.updated_vpnservices.add(router_id)

    def vpnservice_created(self, context, router_id):
        self.updated_vpnservices.add(router_id)

    def vpnservice_deleted(self, context, router_id):
        self.deleted_vpnservices.add(router_id)

    def vpnuser_updated(self, context, tenant_id):
        self.updated_vpnusers.add(tenant_id)

    @abc.abstractmethod
    def create_process(self, process_id, vpnservice, namespace):
        pass

    def ensure_process(self, process_id, vpnservice=None):
        """Ensuring process.

        If the process doesn't exist, it will create process
        and store it in self.processs
        """
        process = self.processes.get(process_id)
        if not process or not process.namespace:
            namespace = self.agent.get_namespace(process_id)
            process = self.create_process(
                process_id,
                vpnservice,
                namespace)
            self.processes[process_id] = process
        if vpnservice:
            process.vpnservice = vpnservice
        return process

    def _rpc_loop_nosync(self):
        # _rpc_loop and _sync_routers_task will not be
        # executed in the same time because of lock.
        # so we can clear the value of updated_routers
        # and removed_routers
        updated_routerids = list(self.updated_vpnservices)
        deleted_ids = list(self.deleted_vpnservices)
        self.updated_vpnservices.clear()
        self.deleted_vpnservices.clear()
        updated_vpnusers_tenants = list(self.updated_vpnusers)
        self.updated_vpnusers.clear()
        LOG.debug("updated_routerids %s", str(updated_routerids))
        LOG.debug("deleted_routerids %s", str(deleted_ids))
        LOG.debug("updated_vpnusers_tenants %s", str(updated_vpnusers_tenants))
        LOG.debug("processes keys 1 %s", str(self.processes.keys()))
        for router_id in deleted_ids:
            if router_id in updated_routerids:
                updated_routerids.remove(router_id)
            if router_id in self.processes:
                try:
                    process = self.processes[router_id]
                    process.disable()
                    del self.processes[router_id]
                    self._update_nat(process.vpnservice, addflag=False)
                except Exception:
                    LOG.exception(_("Failed to delete pptp for router %s"),
                                  router_id)
        if updated_routerids:
            self._process_router_vpnservices(updated_routerids)
        if updated_vpnusers_tenants:
            stale_proceses = (set(self.processes.keys()) -
                              set(updated_routerids))
            LOG.debug("processes keys 2 %s", str(self.processes.keys()))
            if stale_proceses:
                try:
                    users = self.agent_rpc.get_vpn_users(
                        self.context, updated_vpnusers_tenants)
                except Exception:
                    LOG.exception(_("Failed synchronizing vpn users"))
                    self.agent.fullsync = True
                    return
                tenant_users_dict = {}
                for user in users:
                    tenant_users = tenant_users_dict.get(user['tenant_id'], [])
                    tenant_users.append(user)
                    tenant_users_dict[user['tenant_id']] = tenant_users
                processes = [self.processes[router_id] for
                             router_id in self.processes
                             if router_id in stale_proceses]
                for process in processes:
                    tenant_users = tenant_users_dict.get(
                        process.vpnservice['tenant_id'])
                    if tenant_users:
                        LOG.debug(_("Process vpn users for router %s"),
                                  process.id)
                        try:
                            process.process_users(tenant_users)
                        except Exception:
                            LOG.exception(
                                 _('Failed to process vpn users'
                                 ' for router %s '), process.id)

    def _sync_vpnservices_task_nosync(self, context):
        LOG.debug(_("Starting _sync_vpnservices_task_nosync - fullsync:%s"),
                  self.agent.fullsync)
        if self.agent.fullsync:
            # the l3 agent will sync
            LOG.debug(_("_sync_vpnservices_task_nosync"
                        "quit due to router sync needs"))
            return
        self.processes.clear()
        self.updated_vpnservices.clear()
        self.deleted_vpnservices.clear()
        routerids = self.agent.router_info.keys()
        if not routerids:
            return
        self._process_router_vpnservices(routerids, fullrouters=True)
        LOG.debug(_("_sync_vpnservices_task_nosync "
                    "successfully completed"))

    @lockutils.synchronized('vpn-service')
    def _process_router_vpnservices(self, routerids, fullrouters=False):
        vpnservices = []
        try:
            vpnservices = self.agent_rpc.get_vpn_services_on_host(
                self.context, routerids, self.host)
        except Exception:
            LOG.exception(_("Failed synchronizing vpn service"))
            self.agent.fullsync = True
            return
        if LOG.logger.isEnabledFor(10):
            LOG.debug(_('Processing :%s'),
                      jsonutils.dumps(vpnservices, indent=5))
        vpn_statuses = []
        routers_with_vpn = set()
        for vpnservice in vpnservices:
            routers_with_vpn.add(vpnservice['router_id'])
            try:
                process = self.ensure_process(vpnservice['router_id'],
                                              vpnservice=vpnservice)
                process.update()
                oldstatus = vpnservice['status']
                newstatus = process.status
                self._update_nat(process.vpnservice,
                                 addflag=newstatus == constants.ACTIVE)
                if oldstatus != newstatus:
                    vpn_statuses.append({'id': vpnservice['id'],
                                         'tenant_id': vpnservice['tenant_id'],
                                         'status': newstatus})
            except Exception:
                LOG.exception(_("Failed processing vpn service %s"),
                              vpnservice)
                vpn_statuses.append({'id': vpnservice['id'],
                                     'tenant_id': vpnservice['tenant_id'],
                                     'status': constants.ERROR})
        try:
            self.report_vpn_status(vpn_statuses)
        except Exception:
            LOG.exception(_("Failed to report vpn_status %s"),
                          vpn_statuses)
        if fullrouters:
            routers_without_vpn = set(routerids) - routers_with_vpn
            for routerid in routers_without_vpn:
                self._destroy_router_namespace(
                    self.agent.get_namespace(routerid))

    def _update_nat(self, vpnservice, addflag=True):
        """Setting up nat rule in iptables.

        We need to setup nat rule for vpn packet.
        :param vpnservice: vpnservices
        """
        local_cidr = vpnservice['vpn_cidr']
        router_id = vpnservice['router_id']
        ri = self.agent.router_info.get(router_id)
        if not ri:
            return
        ex_gw_port = self.agent._get_ex_gw_port(ri)
        if not ex_gw_port:
            return
        ex_gw_ip = ex_gw_port['fixed_ips'][0]['ip_address']
        rule = self.agent.internal_network_nat_rules(ex_gw_ip, local_cidr)[0]
        if addflag:
            ri.iptables_manager.ipv4['nat'].add_rule(*rule)
        else:
            try:
                ri.iptables_manager.ipv4['nat'].remove_rule(*rule)
            except KeyError:
                return

        ri.iptables_manager.apply()

    def _destroy_router_namespace(self, namespace):
        """Destroy the pptpd in router's namespace."""
        pm = PppdProcess(self.conf, self.root_helper,
                         namespace[len('qrouter-'):],
                         None, namespace)
        pm.disable()

    def report_vpn_status(self, statuses):
        self.agent_rpc.update_status(self.context, statuses)

    def _sync_vpnservices(self):
        pass

    def sync(self, context, processes):
        pass

    def create_router(self, process_id):
        pass

    def destroy_router(self, process_id):
        pass


class PptpdDriver(PptpdBaseDriver):
    def create_process(self, process_id, vpnservice, namespace):
        return PppdProcess(
            self.conf,
            self.root_helper,
            process_id,
            vpnservice,
            namespace)
