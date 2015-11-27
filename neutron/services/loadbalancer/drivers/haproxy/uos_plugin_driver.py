#
# Copyright 2014 Openstack Foundation.
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

import os
import shutil
import socket
import uuid
import abc
import six
import netaddr
from oslo.config import cfg

from neutron.common import exceptions as qexception
from neutron.db import agents_db
from neutron.common import constants as q_const
from neutron.common import topics
from neutron.agent.common import config
from neutron.agent.linux import interface
from neutron.agent.linux import ip_lib
from neutron.common import exceptions
from neutron.common import rpc as n_rpc
from neutron.common import utils as n_utils
from neutron import context
from neutron.extensions import loadbalancerv2
from neutron.extensions import portbindings
from neutron.openstack.common import excutils
from neutron.openstack.common import importutils
from neutron.openstack.common import log as logging
from neutron.openstack.common import service
from neutron.plugins.common import constants
from neutron.services.loadbalancer.agent import agent as lb_agent
from neutron.services.loadbalancer import constants as lb_const
from neutron.services.loadbalancer.drivers import driver_base
from neutron.services.loadbalancer.drivers.haproxy import namespace_driver
from neutron.services.loadbalancer.drivers.common import agent_driver_base
from neutron.extensions import lbaas_agentscheduler
from neutron.db.loadbalancer import models as loadbalancer_dbv2
from neutron.common import exceptions as n_exc

LOG = logging.getLogger(__name__)

DRIVER_NAME = 'haproxy_ns'

class OriginalStatusError(qexception.Conflict):
    message = _("Original status not right for loadbalancer %(loadbalancer_id)s.")

class DriverNotSpecified(n_exc.NeutronException):
    message = _("Device driver for lbaas agent should be specified "
                "in plugin driver.")

class LoadBalancerAgentApi(n_rpc.RpcProxy):
    """Plugin-Agent Arch APIs Used to Notify Agent."""

    BASE_RPC_API_VERSION = '2.0'

    def __init__(self, topic):
        super(LoadBalancerAgentApi, self).__init__(
            topic, default_version=self.BASE_RPC_API_VERSION)

    def _cast(self, context, method_name, method_args, host, version=None):
        return self.cast(
            context,
            self.make_msg(method_name, **method_args),
            topic='%s.%s' % (self.topic, host),
            version=version
        )

    def create_loadbalancer(self, context, loadbalancer, host, driver):
        loadbalancer['driver_name'] = driver
        LOG.debug(_(" Notify agent '%(agent)s' to"
                   " create loadbalancer '%(loadbalancer)s'") %
                   {'loadbalancer': loadbalancer, 'agent': host})
        return self._cast(context, 'create_loadbalancer',
                {'loadbalancer': loadbalancer}, host)

    def update_loadbalancer(self, context,loadbalancer, host, driver):
        loadbalancer['driver_name'] = driver
        LOG.info(_(" Notify agent '%(agent)s' to"
                   " update loadbalancer '%(loadbalancer)s'") %
                   {'loadbalancer': loadbalancer, 'agent': host})
        return self._cast(context, 'update_loadbalancer',
                {'loadbalancer': loadbalancer}, host)

    def delete_loadbalancer(self, context, loadbalancer, host):
        LOG.debug(_(" Notify agent '%(agent)s' to"
                   " delete loadbalancer '%(loadbalancer)s'") %
                   {'loadbalancer': loadbalancer, 'agent': host})
        return self._cast(context, 'delete_loadbalancer',
               {'loadbalancer': loadbalancer}, host)

    def agent_updated(self, context, admin_state_up, host):
        return self._cast(context, 'agent_updated',
                          {'payload': {'admin_state_up': admin_state_up}},
                          host)

class LoadBalancerCallbacks(n_rpc.RpcCallback):
    """Plugin-Agent Arch APIs Used to Callback Plugin."""

    RPC_API_VERSION = '2.0'

    def __init__(self, plugin):
        super(LoadBalancerCallbacks, self).__init__()
        self.plugin = plugin

    def get_ready_devices(self, context, host=None):
        with context.session.begin(subtransactions=True):
            agents = self.plugin.get_lbaas_agents(context,
                                                  filters={'host': [host]})
            if not agents:
                return []
            elif len(agents) > 1:
                LOG.warning(_('Multiple lbaas agents found on host %s'), host)
            loadbalancers = self.plugin.list_loadbalancers_on_lbaas_agent(context,
                                                          agents[0].id)
            loadbalancer_ids = [loadbalancer['id'] for loadbalancer in loadbalancers['loadbalancers']]

            qry = context.session.query(loadbalancer_dbv2.LoadBalancer.id)
            qry = qry.filter(loadbalancer_dbv2.LoadBalancer.id.in_(loadbalancer_ids))
            qry = qry.filter(
                loadbalancer_dbv2.LoadBalancer.status.in_(
                    constants.ACTIVE_PENDING_STATUSES))
            up = True  # makes pep8 and sqlalchemy happy
            qry = qry.filter(loadbalancer_dbv2.LoadBalancer.admin_state_up == up)
            return [id for id, in qry]

    def get_logical_device(self, context, loadbalancer_id=None):

        loadbalancer_ob = self.plugin.get_loadbalancer_instance(context, loadbalancer_id);
        retval=loadbalancer_ob.to_dict()

        retval['driver'] = self.plugin.get_driver_for_provider(retval['provider']['provider_name'])

        if loadbalancer_ob.vip_port is None:
            LOG.error(_(' get_logical_device for %(loadbalancer_id)s vip port None'),
                        {'loadbalancer_id': loadbalancer_id})
            return

        port_data = {
            'id': loadbalancer_ob.vip_port.id,
            'tenant_id': loadbalancer_ob.vip_port.tenant_id,
            'name': 'vip-' + loadbalancer_ob.vip_port.id,
            'network_id': loadbalancer_ob.vip_port.network_id,
            'mac_address': loadbalancer_ob.vip_port.mac_address,
            'admin_state_up': loadbalancer_ob.vip_port.admin_state_up,
            'device_id': loadbalancer_ob.vip_port.device_id,
            'device_owner': loadbalancer_ob.vip_port.device_owner,
            'fixed_ips' : [{'subnet': self.plugin._core_plugin.get_subnet(
                                    context,
                                    ip.subnet_id,
                             ),
                            'subnet_id': ip.subnet_id,
                            'ip_address': ip.ip_address}
                             for ip in loadbalancer_ob.vip_port.fixed_ips]
        }
        retval['vip_port'] = port_data
        LOG.debug(_(' get_logical_device for %(loadbalancer_id)s retval %(retval)s\n '),
                        {'loadbalancer_id': loadbalancer_id, 'retval': retval})
        return retval

    def plug_vip_port(self, context, port_id=None, host=None):
        if not port_id:
            LOG.error(_(' plug_vip_port but not port_id'))
            return

        try:
            port = self.plugin._core_plugin.get_port(
                context,
                port_id
            )
        except n_exc.PortNotFound:
            msg = _('Unable to find port %s to plug.')
            LOG.error(msg, port_id)
            return

        port['admin_state_up'] = True
        #port['device_owner'] = 'neutron:' + constants.LOADBALANCER
        #port['device_id'] = str(uuid.uuid5(uuid.NAMESPACE_DNS, str(host)))
        port[portbindings.HOST_ID] = host
        self.plugin._core_plugin.update_port(
            context,
            port_id,
            {'port': port}
        )

    def unplug_vip_port(self, context, port_id=None, host=None):
        if not port_id:
            LOG.error(_(' unplug_vip_port but not port_id'))
            return

        try:
            port = self.plugin._core_plugin.get_port(
                context,
                port_id
            )
        except n_exc.PortNotFound:
            msg = _('Unable to find port %s to unplug.  This can occur when '
                    'the Vip has been deleted first.')
            LOG.info(msg, port_id)
            return

        port['admin_state_up'] = False

        try:
            self.plugin._core_plugin.update_port(
                context,
                port_id,
                {'port': port}
            )

        except n_exc.PortNotFound:
            msg = _('Unable to find port %s to unplug.  This can occur when '
                    'the Vip has been deleted first.')
            LOG.info(msg, port_id)

    def update_status(self, context, obj_type, obj_id, status):
        model_mapping = {
            'loadbalancer': loadbalancer_dbv2.LoadBalancer,
            'listener': loadbalancer_dbv2.Listener,
        }
        if obj_type not in model_mapping:
            raise n_exc.Invalid(_('Unknown object type: %s') % obj_type)
        try:
            if obj_type == 'loadbalancer':
                lb = self.plugin.db.get_loadbalancer(context, obj_id)
                self.plugin.db.update_status(context, model_mapping[obj_type],
                                  obj_id, status)
                LOG.debug(_('update status: %(obj_type)s %(obj_id)s %(status)s '),
                    {'obj_type': obj_type, 'obj_id': obj_id, 'status':status })
                if(lb and lb.status != status):
                    LOG.info(_('update status: %(obj_type)s %(obj_id)s %(status)s notified'),
                        {'obj_type': obj_type, 'obj_id': obj_id, 'status':status })
                    notifier = n_rpc.get_notifier('loadbalancer')
                    notifier.info(context, 'loadbalancer.update.end',lb.to_dict())
            else :
                LOG.warning(_('Cannot update status: %(obj_type)s %(obj_id)s '
                          'the object type not supported'),
                        {'obj_type': obj_type, 'obj_id': obj_id})
                pass
        except n_exc.NotFound:
            # update_status may come from agent on an object which was
            # already deleted from db with other request
           LOG.warning(_('Cannot update status: %(obj_type)s %(obj_id)s '
                          'not found in the DB, it was probably deleted '
                          'concurrently'),
                        {'obj_type': obj_type, 'obj_id': obj_id})

    def loadbalancer_deployed(self, context, loadbalancer_id=None):
        """Agent confirmation hook that a loadbalancer has been destroyed.

        This method exists for subclasses to change the deletion
        behavior.
        """
        pass

    def loadbalancer_destroyed(self, context, loadbalancer_id=None):
        """Agent confirmation hook that a loadbalancer has been destroyed.

        This method exists for subclasses to change the deletion
        behavior.
        """
        pass

    def update_loadbalancer_stats(self, context, loadbalancer_id=None,
                                     stats=None, host=None):
        self.plugin.stats(context, loadbalancer_id, stats)

@six.add_metaclass(abc.ABCMeta)
class LoadBalancerAbstractDriver(object):

    """Abstract lbaas driver APIs used to driver the agent.
    """

    @abc.abstractmethod
    def create_loadbalancer_instance(self, context, loadbalancer):
        """A real driver would invoke a call to his backend
        and set the Loadbalancer status to ACTIVE/ERROR according
        to the backend call result such as
        self.plugin.update_status(context, LoadBalancer, loadbalancer["id"],
                                  constants.ACTIVE)
        """
        pass

    @abc.abstractmethod
    def update_loadbalancer_instance(self, context, loadbalancer):
        """A real driver would invoke a call to his backend
        and update the Loadbalancer status to ACTIVE/ERROR according
        to the backend call result such as
        self.plugin.update_status(context, LoadBalancer, loadbalancer["id"],
                                  constants.ACTIVE)
        """
        pass

    @abc.abstractmethod
    def delete_loadbalancer_instance(self, context, loadbalancer, remove_id=True):
        """A real driver would invoke a call to his backend
        and try to delete the LoadBalancer.
        if the deletion was successful, delete the record from the database.
        if the deletion has failed, set the LoadBalancer status to ERROR.
        """
        pass

class AgentDriverBase(LoadBalancerAbstractDriver):

    # name of device driver that should be used by the agent;
    # vendor specific plugin drivers must override it;
    device_driver = DRIVER_NAME

    def _set_callbacks_on_plugin(self):
        # other agent based plugin driver might already set callbacks on plugin
        if hasattr(self.plugin, 'agent_callbacks'):
            return

        self.plugin.agent_endpoints = [
            LoadBalancerCallbacks(self.plugin),
            agents_db.AgentExtRpcCallback(self.plugin)
        ]
        self.plugin.conn = n_rpc.create_connection(new=True)
        self.plugin.conn.create_consumer(
            topics.LOADBALANCER_PLUGIN,
            self.plugin.agent_endpoints,
            fanout=False)
        self.plugin.conn.consume_in_threads()

    def __init__(self, plugin):
        if not self.device_driver:
            raise DriverNotSpecified()

        # Setup notifier
        self.agent_rpc = LoadBalancerAgentApi(topics.LOADBALANCER_AGENT)
        self.plugin.agent_notifiers.update(
            {q_const.AGENT_TYPE_LOADBALANCER: self.agent_rpc})

        # Setup callbacks
        self.plugin = plugin
        self._set_callbacks_on_plugin()

        # Setup scheduler
        self.loadbalancer_scheduler = importutils.import_object(
            cfg.CONF.loadbalancer_pool_scheduler_driver)
        self.admin_ctx = context.get_admin_context()
        # Used to manager loadbalancer
        #self.deployed_loadbalancer_ids = set()

    def exists(self,context,loadbalancer):
        if loadbalancer.id in self.deployed_loadbalancer_ids:
            return loadbalancer.id

    def reschedule_loadbalancer_instance(self, context, loadbalancer):
        agent = self.loadbalancer_scheduler.reschedule_loadbalancer_instance(
                                     self.plugin, context,loadbalancer, self.device_driver)
        self.agent_rpc.agent_updated(context, True,
                                   agent['agent']['host'])

    def create_loadbalancer_instance(self, context, loadbalancer):
        agent = self.loadbalancer_scheduler.schedule(self.plugin, context,
                                             loadbalancer, self.device_driver)
        if not agent:
            LOG.warning(_(" LoadBalancer '%(loadbalancer)s' can not be created"
                      " because no eligible lbaas-agent can be found. ") %
                     {'loadbalancer': loadbalancer.id})
            raise lbaas_agentscheduler.NoEligibleLbaasAgent(loadbalancer_id=loadbalancer.id)

        #LOG.info("create_loadbalancer_instance deployed_loadbalancer_ids
        #             add %s", loadbalancer.id)
        #self.deployed_loadbalancer_ids.add(loadbalancer.id)
        self.agent_rpc.create_loadbalancer(context, loadbalancer.to_dict(),
                                   agent['agent']['host'],self.device_driver)

    def update_loadbalancer_instance(self, context,loadbalancer):

        agent = self.plugin.get_lbaas_agent_hosting_loadbalancer(context, loadbalancer.id)
        if agent:
            if (loadbalancer.status == constants.ACTIVE
                  or loadbalancer.status == constants.PENDING_UPDATE ):
                self.agent_rpc.update_loadbalancer(context, loadbalancer.to_dict(),
                                                      agent['agent']['host'], self.device_driver)
            else:
                LOG.warning(_(" LoadBalancer '%(loadbalancer)s' in agent: %(agent)s"
                    " status not ACTIVE/PENDING_UPDATE this may happen after lbaas-agent"
                    " error during spawning one lbaas instance.") %
                   {'loadbalancer': loadbalancer.id,
                    'agent': agent})
                raise OriginalStatusError(loadbalancer_id=loadbalancer.id)
        else:
            if (loadbalancer.status == constants.PENDING_UPDATE):
                LOG.warning(_(" LoadBalancer '%(loadbalancer)s' has not be deployed"
                            " by lbaas-agent this may happen after lbaas-agent "
                            "scheduler can not find eligible one. ") %
                   {'loadbalancer': loadbalancer.id})
                raise OriginalStatusError(loadbalancer_id=loadbalancer.id)
            else:
                LOG.error(_(" LoadBalancer '%(loadbalancer)s' has not be deployed"
                             " by lbaas-agent when loadbalancer-refresh happens. ") %
                   {'loadbalancer': loadbalancer.id})
                raise OriginalStatusError(loadbalancer_id=loadbalancer.id)

    def check_delete_loadbalancer(self, context ,loadbalancer):
        if len(loadbalancer.listeners) <=0:
            return True
        for listener in loadbalancer.listeners:
            if listener.admin_state_up and listener!= constants.PENDING_DELETE:
                return False
        return True

    def delete_loadbalancer_instance(self, context, loadbalancer, remove_id_flag):

        agent = self.plugin.get_lbaas_agent_hosting_loadbalancer(context, loadbalancer.id)

        if agent:
            if ( (loadbalancer.status == constants.ACTIVE or
                   loadbalancer.status == constants.ERROR) and remove_id_flag):
                # this means called by listener's delete function
                if self.check_delete_loadbalancer(context, loadbalancer):
                    #self.deployed_loadbalancer_ids.remove(loadbalancer.id)
                    self.agent_rpc.delete_loadbalancer(context, loadbalancer.to_dict(),
                                                           agent['agent']['host'])

            elif ( loadbalancer.status == constants.ERROR or
                     loadbalancer.status == constants.ACTIVE):
                # this means called by loadbalancer's delete
                LOG.debug(_(" LoadBalancer '%(loadbalancer_id_tmp)s' has not been deployed"
                             " although agent exists. ") %
                            {"loadbalancer_id_tmp": loadbalancer.id} )
            else :
                #this should never happen.
                LOG.error(_(" LoadBalancer '%(loadbalancer_id_tmp)s' not ACTIVE/ERROR"
                             " but agent exists when delete instance. ") %
                            {"loadbalancer_id_tmp": loadbalancer.id} )
        else:
            # loadbalancer has not been deployed.
            # if loadbalancer.listeners exist(s) the original status must be ERROR.
            # if loadbalancer.listeners not exists the original status must be ACTIVE.
            LOG.debug(_(" LoadBalancer '%(loadbalancer_id_tmp)s' has not been deployed "
                         " and agent not exists. ") %
                          {"loadbalancer_id_tmp": loadbalancer.id} )

class HaproxyNSDriver(AgentDriverBase,driver_base.LoadBalancerBaseDriver):

    def __init__(self, plugin):
        device_driver = DRIVER_NAME
        self.load_balancer = LoadBalancerManager(self)
        self.listener = ListenerManager(self)
        self.pool = PoolManager(self)
        self.member = MemberManager(self)
        self.l7policy = L7PolicyManager(self)
        self.l7rule = L7RuleManager(self)

        driver_base.LoadBalancerBaseDriver.__init__(self, plugin)
        AgentDriverBase.__init__(self,plugin)

class LoadBalancerManager(driver_base.BaseLoadBalancerManager):

    def deployable(self, loadbalancer):
        if not loadbalancer:
            return False
        if not loadbalancer.admin_state_up:
            return False
        acceptable_listeners = [
            listener for listener in loadbalancer.listeners
            if (listener.status != constants.PENDING_DELETE and
                listener.admin_state_up)]
        return (acceptable_listeners and
                loadbalancer.status != constants.PENDING_DELETE)

    def reschedule(self, context, loadbalancer):
        LOG.info(_("LoadBalancer '%(loadbalancer)s' rescheduled called in driver. ") %
                       {'loadbalancer': loadbalancer.id})
        agent = self.driver.reschedule_loadbalancer_instance(context,
                                             loadbalancer)
        self.refresh(context, loadbalancer)

    def refresh(self, context, loadbalancer):
        super(LoadBalancerManager, self).refresh(context, loadbalancer)
        # call the RPC to make the lbaas-agent spawn one loadbalancer instance
        # or update the spawned loadbalancer instance
        agent = self.driver.plugin.get_lbaas_agent_hosting_loadbalancer(context, loadbalancer.id)
        if agent:
        #if self.driver.exists(context,loadbalancer):
            LOG.info(_("LoadBalancer '%(loadbalancer)s' exists when refresh, so update it. ") %
                       {'loadbalancer': loadbalancer.id})
            #if not self.deployable(loadbalancer):
            #    LOG.debug(_("LoadBalancer '%(loadbalancer)s' became not deployable when refresh exist one. ") %
            #           {'loadbalancer': loadbalancer.id})
            #    self.driver.load_balancer.delete_instance(context,loadbalancer,True)
            #    self.active(context, loadbalancer.id)
            #    return
            try:
                self.driver.update_loadbalancer_instance(context,loadbalancer)
            except OriginalStatusError:
                # original loadbalancer status is error, so set status to ERROR
                msg = _('Unable to deploy loadbalancer %s because the original status is error. ')
                LOG.error(msg, loadbalancer.id)
                self.failed(context, loadbalancer.id)
                return

        else:
            LOG.info(_("LoadBalancer '%(loadbalancer)s' not exists when refresh, so create it. ") %
                       {'loadbalancer': loadbalancer.id})
            # if not deployable, need no more operation, just set it to ACTIVE.
            if not self.deployable(loadbalancer):
                LOG.debug(_("LoadBalancer '%(loadbalancer)s' still not deployable when refresh. ") %
                       {'loadbalancer': loadbalancer.id})
                self.active(context, loadbalancer.id)
                return

            try:
                self.driver.create_loadbalancer_instance(context, loadbalancer)
            except lbaas_agentscheduler.NoEligibleLbaasAgent:
                # loadbalancer can not be deployed for no eligible lbaas-agent, then set status to ERROR
                msg = _('Unable to deploy loadbalancer %s because no eligible lbaas-agent can be found. ')
                LOG.error(msg, loadbalancer.id)
                self.failed(context, loadbalancer.id)
                return

    def update(self, context, old_loadbalancer, loadbalancer):
        super(LoadBalancerManager, self).update(context, old_loadbalancer,
                                                loadbalancer)
        # update only happened when the admin_state_up/name/description of loadbalancer is being changed.
        # the original status should not be PENDING_* because check has been applied in the DB level.
        # if the original status is ERROR/ACTIVE :
        self.refresh(context,loadbalancer)

    def create(self, context, loadbalancer):
        super(LoadBalancerManager, self).create(context, loadbalancer)

        # loadbalancer has no listeners then no operation will be applied, so just set it ACTIVE
        if not self.deployable(loadbalancer) :
            LOG.debug(_("LoadBalancer '%(loadbalancer)s' not deployable when create. ") %
                       {'loadbalancer': loadbalancer.id})

            self.active(context, loadbalancer.id)
            return

        # loadbalancer has listener(s) then notify the lbaas-agent to create loadbalancer instance
        try:
            self.driver.create_loadbalancer_instance(context, loadbalancer)
        except lbaas_agentscheduler.NoEligibleLbaasAgent:
            # loadbalancer can not be deployed for no eligible lbaas-agent, then set status to ERROR
            msg = _('Unable to deploy loadbalancer %s because no eligible lbaas-agent can be found. ')
            LOG.error(msg, loadbalancer.id)
            self.failed(context, loadbalancer.id)
            return

    def delete(self, context, loadbalancer):
        self.delete_instance(context, loadbalancer)
        for listener in loadbalancer.listeners:
            if listener.default_pool:
                self.driver.plugin.defer_pool(context, listener.default_pool)
            self.driver.plugin.defer_l7policies(context, listener.l7_policies)
        self.db_delete(context, loadbalancer.id)

    def delete_instance(self, context, loadbalancer, remove_id=False):
        super(LoadBalancerManager, self).delete(context, loadbalancer)
        self.driver.delete_loadbalancer_instance(context, loadbalancer,remove_id)

class ListenerManager(driver_base.BaseListenerManager):

    def _remove_listener(self, loadbalancer, listener_id):
        index_to_remove = None
        for index, listener in enumerate(loadbalancer.listeners):
            if listener.id == listener_id:
                index_to_remove = index
        loadbalancer.listeners.pop(index_to_remove)

    def check_delete_loadbalancer(self, context ,loadbalancer):
        if len(loadbalancer.listeners) <=0:
            return True
        for listener in loadbalancer.listeners:
            if listener.admin_state_up and listener!= constants.PENDING_DELETE:
                return False
        return True

    def update(self, context, old_listener, new_listener):
        super(ListenerManager, self).update(context,old_listener, new_listener)
        if new_listener.attached_to_loadbalancer():
            self.driver.plugin.activate_linked_entities(context, new_listener)

        if  not self.check_delete_loadbalancer(context, new_listener.loadbalancer): 
            LOG.info("ListenerManager not delete,only update for listener %s",new_listener.id)
            self.driver.load_balancer.refresh(context, new_listener.loadbalancer)
        elif (not new_listener.admin_state_up and old_listener.admin_state_up):
            LOG.info("ListenerManager delete because update for listener %s",new_listener.id)
            self.driver.load_balancer.delete_instance(context,new_listener.loadbalancer,True)

        if not new_listener.default_pool and old_listener.default_pool:
            # if listener's pool has been detached then defer the pool
            # and its children
            self.driver.plugin.defer_pool(context, old_listener.default_pool)

    def create(self, context, listener):
        super(ListenerManager, self).create(context, listener)
        self.driver.load_balancer.refresh(context, listener.loadbalancer)
        self.driver.plugin.activate_linked_entities(context, listener)

    def delete(self, context, obj):
        super(ListenerManager, self).delete(context, obj)
        self.db_delete(context, obj.id)
        loadbalancer = obj.loadbalancer
        self._remove_listener(loadbalancer, obj.id)
        if not self.check_delete_loadbalancer(context, loadbalancer):
            LOG.info("ListenerManager not delete only delete listener %s",obj.id)
            self.driver.load_balancer.refresh(context, loadbalancer)
        elif obj.admin_state_up:
            LOG.info("ListenerManager delete for listener %s delete",obj.id)
            # delete instance because haproxy will throw error if listener is
            # missing in frontend
            self.driver.load_balancer.delete_instance(context,loadbalancer,True)
        if obj.default_pool:
            self.driver.plugin.defer_pool(context, obj.default_pool)
        self.driver.plugin.defer_l7policies(context, obj.l7_policies)

class PoolManager(driver_base.BasePoolManager):

    def update(self, context, obj_old, obj):
        super(PoolManager, self).update(context, obj_old, obj)
        if obj.l7policy:
            self.driver.load_balancer.refresh(context, obj.l7policy.listener.loadbalancer)
        elif obj.listener:
            self.driver.load_balancer.refresh(context, obj.listener.loadbalancer)
        LOG.debug("PoolManager update %s",obj.id)
        self.driver.plugin.activate_linked_entities(context, obj)

    def create(self, context, obj):
        super(PoolManager, self).create(context, obj)
        #ToDo:call the LoadBalancerManager to refresh also not necessary.
        #self.driver.load_balancer.refresh(context, obj.listener.loadbalancer)
        # This shouldn't be called since a pool cannot be created and linked
        # to a loadbalancer at the same time
        #self.driver.plugin.activate_linked_entities(context, obj)

    def delete(self, context, obj):
        super(PoolManager, self).delete(context, obj)
        self.db_delete(context, obj.id)
        if obj.l7policy:
            loadbalancer = obj.l7policy.listener.loadbalancer
            obj.l7policy.redirect_pool = None
        else:
            loadbalancer = obj.listener.loadbalancer
            obj.listener.default_pool = None
        self.driver.load_balancer.refresh(context, loadbalancer)


class MemberManager(driver_base.BaseMemberManager):

    def _remove_member(self, pool, member_id):
        index_to_remove = None
        for index, member in enumerate(pool.members):
            if member.id == member_id:
                index_to_remove = index
        pool.members.pop(index_to_remove)

    def update(self, context, obj_old, obj):
        super(MemberManager, self).update(context, obj_old, obj)
        self.driver.load_balancer.refresh(context,
                                          obj.pool.listener.loadbalancer)
        self.driver.plugin.activate_linked_entities(context, obj)

    def create(self, context, obj):
        super(MemberManager, self).create(context, obj)
        self.driver.load_balancer.refresh(context,
                                          obj.pool.listener.loadbalancer)
        self.driver.plugin.activate_linked_entities(context, obj)

    def delete(self, context, obj):
        super(MemberManager, self).delete(context, obj)
        loadbalancer = obj.pool.listener.loadbalancer
        self._remove_member(obj.pool, obj.id)
        self.driver.load_balancer.refresh(context, loadbalancer)
        self.db_delete(context, obj.id)

class L7PolicyManager(driver_base.BaseL7PolicyManager):
    def _remove_policy_from_listener(self, listener, policy_id):
        index_to_remove = None
        for index, policy in enumerate(listener.l7_policies):
            if policy.id == policy_id:
                index_to_remove = index
        listener.l7_policies.pop(index_to_remove)

    def create(self, context, l7policy):
        super(L7PolicyManager, self).create(context,l7policy)
        self.driver.load_balancer.refresh(context,
                                          l7policy.listener.loadbalancer)
        self.driver.plugin.activate_linked_entities(context, l7policy)

    def update(self, context, old_l7policy, l7policy):
        super(L7PolicyManager, self).update(context,old_l7policy,l7policy)
        self.driver.load_balancer.refresh(context,
                                          l7policy.listener.loadbalancer)
        if (old_l7policy.redirect_pool!=None and
           l7policy.redirect_pool!=old_l7policy.redirect_pool):
            self.driver.plugin.defer_pool(context, old_l7policy.redirect_pool)

        self.driver.plugin.activate_linked_entities(context, l7policy)

    def delete(self, context, l7policy):
        super(L7PolicyManager, self).delete(context,l7policy)
        self._remove_policy_from_listener(l7policy.listener,l7policy.id)
        if l7policy.redirect_pool:
            self.driver.plugin.defer_pool(context, l7policy.redirect_pool)
        self.driver.load_balancer.refresh(context,
                                          l7policy.listener.loadbalancer)
        self.db_delete(context, l7policy.id)


class L7RuleManager(driver_base.BaseL7RuleManager):

    def _remove_listener(self, policy, rule_id):
        index_to_remove = None
        for index, rule in enumerate(policy.rules):
            if rule.id == rule_id:
                index_to_remove = index
        policy.rules.pop(index_to_remove)

    def create(self, context, l7rule):
        super(L7RuleManager, self).create(context,l7rule)
        self.driver.load_balancer.refresh(context,
                                          l7rule.l7policy.listener.loadbalancer)
        self.driver.plugin.activate_linked_entities(context, l7rule)

    def update(self, context, old_l7rule, l7rule):
        super(L7RuleManager, self).update(context,old_l7rule,l7rule)
        self.driver.load_balancer.refresh(context,
                                          l7rule.l7policy.listener.loadbalancer)
        self.driver.plugin.activate_linked_entities(context, l7rule)

    def delete(self, context, l7rule):
        super(L7RuleManager, self).delete(context,l7rule)
        self._remove_listener(l7rule.l7policy,l7rule.id)
        self.driver.load_balancer.refresh(context,
                                          l7rule.l7policy.listener.loadbalancer)
        self.db_delete(context, l7rule.id,l7rule.l7policy.id)

