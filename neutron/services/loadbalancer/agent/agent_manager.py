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

from oslo.config import cfg

from neutron.agent import rpc as agent_rpc
from neutron.api.rpc.agentnotifiers import helo_rpc_agent_api
from neutron.common import constants as n_const
from neutron.common import exceptions as n_exc
from neutron.common import rpc as n_rpc
from neutron.common import topics
from neutron import context
from neutron.openstack.common import importutils
from neutron.openstack.common import log as logging
from neutron.openstack.common import loopingcall
from neutron.openstack.common import periodic_task
from neutron.plugins.common import constants
from neutron.services.loadbalancer.agent import agent_api

LOG = logging.getLogger(__name__)

OPTS = [
    cfg.MultiStrOpt(
        'device_driver',
        default=['neutron.services.loadbalancer.drivers'
                 '.haproxy.uos_agent_driver.HaproxyNSDriver'],
        help=_('Drivers used to manage loadbalancing devices'),
    ),
    cfg.BoolOpt(
        'statistic_notification_enable',
        default=False,
        help=_('Whether notify for statistic information'),
    ),
    cfg.BoolOpt(
        'statistic_notification_enable_for_neutron',
        default=True,
        help=_('Whether notify neutron for statistic information'),
    ),
    cfg.IntOpt(
        'collect_periodic_interval',
        default=60,
        help=_('Seconds between periodic task runs for statistic')
    )
]


class DeviceNotFoundOnAgent(n_exc.NotFound):
    msg = _('Unknown device with loadbalancer_id %(loadbalancer_id)s')

class LoadBalancerNotFoundOnAgent(n_exc.NotFound):
    msg = _('Unknown loadbalancer with loadbalancer_id %(loadbalancer_id)s')

class LbaasAgentManager(n_rpc.RpcCallback, periodic_task.PeriodicTasks,
                           helo_rpc_agent_api.HeloRpcCallbackMixin):

    RPC_API_VERSION = '2.0'
    # history
    #   1.0 Initial version
    #   1.1 Support agent_updated call
    #   2.0 Generic API for agent based drivers
    #       - modify/reload/destroy_pool methods were removed;
    #       - added methods to handle create/update/delete for every lbaas
    #       object individually;

    def __init__(self, conf):
        super(LbaasAgentManager, self).__init__()
        self.conf = conf
        self.context = context.get_admin_context_without_session()
        self.plugin_rpc = agent_api.LbaasAgentApi(
            topics.LOADBALANCER_PLUGIN,
            self.context,
            self.conf.host
        )
        self._load_drivers()

        self.agent_state = {
            'binary': 'neutron-lbaas-agent',
            'host': conf.host,
            'topic': topics.LOADBALANCER_AGENT,
            'configurations': {'device_drivers': self.device_drivers.keys()},
            'agent_type': n_const.AGENT_TYPE_LOADBALANCER,
            'start_flag': True}
        self.admin_state_up = True

        self._setup_state_rpc()
        self.needs_resync = False

        # loadbalancer_id->device_driver_name mapping used to store known instances
        self.instance_mapping = {}

        # loadbalancer_id->tenant_id mapping used to store known instances
        self.label_tenant_id = {}
        collect_statistic = loopingcall.FixedIntervalLoopingCall(
                self.collect_stats_for_monitor)
        collect_statistic.start(interval=cfg.CONF.collect_periodic_interval)

    def _load_drivers(self):
        self.device_drivers = {}
        for driver in self.conf.device_driver:
            try:
                driver_inst = importutils.import_object(
                    driver,
                    self.conf,
                    self.plugin_rpc,
                    self.context
                )
            except ImportError:
                msg = _('Error importing loadbalancer device driver: %s')
                raise SystemExit(msg % driver)

            driver_name = driver_inst.get_name()
            if driver_name not in self.device_drivers:
                self.device_drivers[driver_name] = driver_inst
            else:
                msg = _('Multiple device drivers with the same name found: %s')
                raise SystemExit(msg % driver_name)

    def _setup_state_rpc(self):
        self.state_rpc = agent_rpc.PluginReportStateAPI(
            topics.LOADBALANCER_PLUGIN)
        report_interval = self.conf.AGENT.report_interval
        if report_interval:
            heartbeat = loopingcall.FixedIntervalLoopingCall(
                self._report_state)
            heartbeat.start(interval=report_interval)

    def _report_state(self):
        try:
            instance_count = len(self.instance_mapping)
            self.agent_state['configurations']['instances'] = instance_count
            self.state_rpc.report_state(self.context,
                                        self.agent_state)
            self.agent_state.pop('start_flag', None)
        except Exception:
            LOG.exception(_("Failed reporting state!"))

    def initialize_service_hook(self, started_by):
        self.sync_state()

    @periodic_task.periodic_task
    def periodic_resync(self, context):
        if self.needs_resync:
            self.needs_resync = False
            self.sync_state()

    def collect_stats_for_monitor(self):
        for loadbalancer_id, driver_name in self.instance_mapping.items():
            driver = self.device_drivers[driver_name]
            tenant_id = self.label_tenant_id.get(loadbalancer_id, None)
            try:
                stats = driver.get_stats(loadbalancer_id)
                if stats and self.conf.statistic_notification_enable: 
                    stats.pop('members', None)
                    stats['tenant_id'] = tenant_id
                    notifier = n_rpc.get_notifier('loadbalancer')
                    notifier.info(self.context, 'loadbalancer.meter', stats)
                    LOG.info(_('collect_stats_for_monitor'
                             'loadbalancer %(id)s stats %(stats)s .'),
                             {'id':loadbalancer_id,'stats':stats})
            except Exception:
                LOG.exception(_('Error updating statistics on loadbalancer %s'),
                              loadbalancer_id)


    @periodic_task.periodic_task(spacing=6)
    def collect_stats(self, context):
        for loadbalancer_id, driver_name in self.instance_mapping.items():
            driver = self.device_drivers[driver_name]
            try:
                stats = driver.get_stats(loadbalancer_id)
                if stats and self.conf.statistic_notification_enable_for_neutron:
                    stats.pop('id', None)
                    stats.pop('frontends_stats', None)
                    stats.pop('backends_stats', None)
                    stats.pop('members_stats', None)
                    self.plugin_rpc.update_loadbalancer_stats(loadbalancer_id, stats)
                    LOG.info(_('collect_stats loadbalancer %(id)s'
                              ' update stats %(stats)s .'),
                             {'id':loadbalancer_id,'stats':stats})
            except Exception:
                LOG.exception(_('Error updating statistics on loadbalancer %s'),
                              loadbalancer_id)

    def sync_state(self):
        known_instances = set(self.instance_mapping.keys())
        try:
            ready_instances = set(self.plugin_rpc.get_ready_devices())

            for deleted_id in known_instances - ready_instances:
                self._destroy_loadbalancer(deleted_id)

            for loadbalancer_id in ready_instances:
                self._reload_loadbalancer(loadbalancer_id)

        except Exception:
            LOG.exception(_('Unable to retrieve ready devices'))
            self.needs_resync = True

        self.remove_orphans()

    def _get_driver(self, loadbalancer_id):
        if loadbalancer_id not in self.instance_mapping:
            LOG.error(_('No loadbalancer id on agent: %s.'), loadbalancer_id)
            raise DeviceNotFoundOnAgent(loadbalancer_id=loadbalancer_id)

        driver_name = self.instance_mapping[loadbalancer_id]
        return self.device_drivers[driver_name]

    def _reload_loadbalancer(self, loadbalancer_id):
        try:
            logical_config = self.plugin_rpc.get_logical_device(loadbalancer_id)
            driver_name = logical_config['driver']
            tenant_id = logical_config.get('tenant_id', None)
            if driver_name not in self.device_drivers:
                LOG.error(_('No device driver '
                            'on agent: %s.'), driver_name)
                self.plugin_rpc.update_status(
                    'loadbalancer', loadbalancer_id, constants.ERROR)
                return

            self.device_drivers[driver_name].deploy_instance(logical_config)
            self.instance_mapping[loadbalancer_id] = driver_name
            self.label_tenant_id[loadbalancer_id] = tenant_id
            self.plugin_rpc.loadbalancer_deployed(loadbalancer_id)
        except Exception:
            LOG.exception(_('Unable to deploy instance for loadbalancer: %s'), loadbalancer_id)
            self.needs_resync = True

    def _destroy_loadbalancer(self, loadbalancer_id):
        try:
            driver = self._get_driver(loadbalancer_id)
        except Exception:
            self._handle_failed_driver_call('destroy', 'loadbalancer', loadbalancer['id'],
                                            driver.get_name())
            return
        try:
            driver.undeploy_instance(loadbalancer_id)
            del self.instance_mapping[loadbalancer_id]
            del self.label_tenant_id[loadbalancer_id]
            self.plugin_rpc.loadbalancer_destroyed(loadbalancer_id)
        except Exception:
            LOG.exception(_('Unable to destroy device for loadbalancer: %s'), loadbalancer_id)
            self.needs_resync = True

    def remove_orphans(self):
        for driver_name in self.device_drivers:
            loadbalancer_ids = [loadbalancer_id for loadbalancer_id in self.instance_mapping
                        if self.instance_mapping[loadbalancer_id] == driver_name]
            try:
                self.device_drivers[driver_name].remove_orphans(loadbalancer_ids)
            except NotImplementedError:
                pass  # Not all drivers will support this

    def _handle_failed_driver_call(self, operation, obj_type, obj_id, driver):
        LOG.exception(_('%(operation)s %(obj)s %(id)s failed on device driver '
                        '%(driver)s'),
                      {'operation': operation.capitalize(), 'obj': obj_type,
                       'id': obj_id, 'driver': driver})
        self.plugin_rpc.update_status(obj_type, obj_id, constants.ERROR)

    def create_loadbalancer(self, context, loadbalancer):

        driver_name = loadbalancer['driver_name']
        tenant_id = loadbalancer.get('tenant_id', None)
        if driver_name not in self.device_drivers:
            LOG.error(_('No device driver on agent: %s.'), driver_name)
            self.plugin_rpc.update_status('loadbalancer', loadbalancer['id'], constants.ERROR)
            return

        driver = self.device_drivers[driver_name]
        try:
            driver.create_loadbalancer(loadbalancer)
        except Exception:
            LOG.error(_('loadbalancer driver is  %s.'), driver_name)
            self._handle_failed_driver_call('create', 'loadbalancer', loadbalancer['id'], driver_name)
        else:
            self.instance_mapping[loadbalancer['id']] = driver_name
            self.label_tenant_id[loadbalancer['id']] = tenant_id
            self.plugin_rpc.update_status('loadbalancer', loadbalancer['id'], constants.ACTIVE)

    def update_loadbalancer(self, context, loadbalancer):
        try:
            driver = self._get_driver(loadbalancer['id'])
        except Exception:
            LOG.info(_('create loadbalancer when update_loadbalancer because'
                       ' loadbalancer %s not found.'), loadbalancer['id'])
            self.create_loadbalancer(context, loadbalancer)
            return
        try:
            driver.update_loadbalancer(loadbalancer)
        except Exception:
            self._handle_failed_driver_call('update', 'loadbalancer', loadbalancer['id'],
                                            driver.get_name())
        else:
            self.plugin_rpc.update_status('loadbalancer', loadbalancer['id'], constants.ACTIVE)

    def delete_loadbalancer(self, context, loadbalancer):
        try:
            driver = self._get_driver(loadbalancer['id'])
        except Exception:
            self._handle_failed_driver_call('delete', 'loadbalancer', loadbalancer['id'],
                                            'None')
            return
        driver.delete_loadbalancer(loadbalancer)
        del self.instance_mapping[loadbalancer['id']]

    def create_vip(self, context, vip):
        driver = self._get_driver(vip['pool_id'])
        try:
            driver.create_vip(vip)
        except Exception:
            self._handle_failed_driver_call('create', 'vip', vip['id'],
                                            driver.get_name())
        else:
            self.plugin_rpc.update_status('vip', vip['id'], constants.ACTIVE)

    def update_vip(self, context, old_vip, vip):
        driver = self._get_driver(vip['pool_id'])
        try:
            driver.update_vip(old_vip, vip)
        except Exception:
            self._handle_failed_driver_call('update', 'vip', vip['id'],
                                            driver.get_name())
        else:
            self.plugin_rpc.update_status('vip', vip['id'], constants.ACTIVE)

    def delete_vip(self, context, vip):
        driver = self._get_driver(vip['pool_id'])
        driver.delete_vip(vip)

    def create_pool(self, context, pool, driver_name):
        if driver_name not in self.device_drivers:
            LOG.error(_('No device driver on agent: %s.'), driver_name)
            self.plugin_rpc.update_status('pool', pool['id'], constants.ERROR)
            return

        driver = self.device_drivers[driver_name]
        try:
            driver.create_pool(pool)
        except Exception:
            self._handle_failed_driver_call('create', 'pool', pool['id'],
                                            driver.get_name())
        else:
            self.instance_mapping[pool['id']] = driver_name
            self.plugin_rpc.update_status('pool', pool['id'], constants.ACTIVE)

    def update_pool(self, context, old_pool, pool):
        driver = self._get_driver(pool['id'])
        try:
            driver.update_pool(old_pool, pool)
        except Exception:
            self._handle_failed_driver_call('update', 'pool', pool['id'],
                                            driver.get_name())
        else:
            self.plugin_rpc.update_status('pool', pool['id'], constants.ACTIVE)

    def delete_pool(self, context, pool):
        driver = self._get_driver(pool['id'])
        driver.delete_pool(pool)
        del self.instance_mapping[pool['id']]

    def create_member(self, context, member):
        driver = self._get_driver(member['pool_id'])
        try:
            driver.create_member(member)
        except Exception:
            self._handle_failed_driver_call('create', 'member', member['id'],
                                            driver.get_name())
        else:
            self.plugin_rpc.update_status('member', member['id'],
                                          constants.ACTIVE)

    def update_member(self, context, old_member, member):
        driver = self._get_driver(member['pool_id'])
        try:
            driver.update_member(old_member, member)
        except Exception:
            self._handle_failed_driver_call('update', 'member', member['id'],
                                            driver.get_name())
        else:
            self.plugin_rpc.update_status('member', member['id'],
                                          constants.ACTIVE)

    def delete_member(self, context, member):
        driver = self._get_driver(member['pool_id'])
        driver.delete_member(member)

    def create_pool_health_monitor(self, context, health_monitor, pool_id):
        driver = self._get_driver(pool_id)
        assoc_id = {'pool_id': pool_id, 'monitor_id': health_monitor['id']}
        try:
            driver.create_pool_health_monitor(health_monitor, pool_id)
        except Exception:
            self._handle_failed_driver_call(
                'create', 'health_monitor', assoc_id, driver.get_name())
        else:
            self.plugin_rpc.update_status(
                'health_monitor', assoc_id, constants.ACTIVE)

    def update_pool_health_monitor(self, context, old_health_monitor,
                                   health_monitor, pool_id):
        driver = self._get_driver(pool_id)
        assoc_id = {'pool_id': pool_id, 'monitor_id': health_monitor['id']}
        try:
            driver.update_pool_health_monitor(old_health_monitor,
                                              health_monitor,
                                              pool_id)
        except Exception:
            self._handle_failed_driver_call(
                'update', 'health_monitor', assoc_id, driver.get_name())
        else:
            self.plugin_rpc.update_status(
                'health_monitor', assoc_id, constants.ACTIVE)

    def delete_pool_health_monitor(self, context, health_monitor, pool_id):
        driver = self._get_driver(pool_id)
        driver.delete_pool_health_monitor(health_monitor, pool_id)

    def agent_updated(self, context, payload):
        """Handle the agent_updated notification event."""
        if payload['admin_state_up'] != self.admin_state_up:
            self.admin_state_up = payload['admin_state_up']
            if self.admin_state_up:
                self.needs_resync = True
            else:
                for loadbalancer_id in self.instance_mapping.keys():
                    LOG.info(_("Destroying loadbalancer %s due to agent disabling"),
                             loadbalancer_id)
                    self._destroy_loadbalancer(loadbalancer_id)
            LOG.info(_("Agent_updated by server side %s!"), payload)
