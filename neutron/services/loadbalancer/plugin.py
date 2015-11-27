#
# Copyright 2013 Radware LTD.
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
# @author: Avishay Balderman, Radware
from neutron import manager
from neutron.api.v2 import attributes as attrs
from neutron.common import exceptions as n_exc
from neutron.common import rpc as n_rpc
from neutron import context as ncontext
from neutron.db.loadbalancer import loadbalancer_db as ldb
from neutron.db.loadbalancer import loadbalancer_dbv2 as ldbv2
from neutron.db.loadbalancer import models
from neutron.db import servicetype_db as st_db
from neutron.extensions import loadbalancer
from neutron.extensions import loadbalancerv2
from neutron.openstack.common import excutils
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants
from neutron.services.loadbalancer import agent_scheduler
from neutron.services.loadbalancer import data_models
from neutron.services import provider_configuration as pconf
from neutron.services import service_base
from neutron.db import common_db_mixin as base_db
from neutron.services.loadbalancer import constants as lb_const
from neutron.agent.linux import utils
import contextlib
import tempfile

LOG = logging.getLogger(__name__)

class LoadBalancerPluginv2(loadbalancerv2.LoadBalancerPluginBaseV2,
                           agent_scheduler.LbaasAgentSchedulerDbMixin,
                           base_db.CommonDbMixin):
    """Implementation of the Neutron Loadbalancer Service Plugin.

    This class manages the workflow of LBaaS request/response.
    Most DB related works are implemented in class
    loadbalancer_dbv2.LoadBalancerPluginDbv2.
    """
    supported_extension_aliases = ["lbaasv2",
                                   "lbaas_agent_scheduler",
                                   "service-type"]

    # lbaas agent notifiers to handle agent update operations;
    # can be updated by plugin drivers while loading;
    # will be extracted by neutron manager when loading service plugins;
    agent_notifiers = {}

    def __init__(self):
        """Initialization for the loadbalancer service plugin."""

        self.db = ldbv2.LoadBalancerPluginDbv2()
        self.service_type_manager = st_db.ServiceTypeManager.get_instance()
        self._load_drivers()
        self.start_periodic_agent_status_check()
        #self.deploy_existing_instances();

    @property
    def _core_plugin(self):
        return manager.NeutronManager.get_plugin()

    def _load_drivers(self):
        """Loads plugin-drivers specified in configuration."""
        self.drivers, self.default_provider = service_base.load_drivers(
            constants.LOADBALANCERV2, self)

        # we're at the point when extensions are not loaded yet
        # so prevent policy from being loaded
        self.ctx = ncontext.get_admin_context(load_admin_roles=False)
        # stop service in case provider was removed, but resources were not
        self._check_orphan_loadbalancer_associations(self.ctx, self.drivers.keys())

    def _check_orphan_loadbalancer_associations(self, context, provider_names):
        """Checks remaining associations between loadbalancers and providers.

        If admin has not undeployed resources with provider that was deleted
        from configuration, neutron service is stopped. Admin must delete
        resources prior to removing providers from configuration.
        """
        loadbalancers = self.db.get_loadbalancers(context)
        lost_providers = set(
            [loadbalancer.provider.provider_name
             for loadbalancer in loadbalancers
             if ((loadbalancer.provider is not None and loadbalancer.provider.provider_name not in provider_names)
             )])
        # resources are left without provider - stop the service
        if lost_providers:
            msg = _("Delete associated load balancers before "
                    "removing providers %s") % list(lost_providers)
            LOG.exception(msg)
            raise SystemExit(1)

    def reschedule_loadbalancer_instance(self, loadbalancer_id):
        loadbalancer = self.db.get_loadbalancer(self.ctx, loadbalancer_id)
        driver = self._get_driver_for_loadbalancer(
                self.ctx, loadbalancer_id)
        try:
            self._call_driver_operation(
                   self.ctx, driver.load_balancer.reschedule,
                   loadbalancer)
        except Exception as exc:
            LOG.exception(exc)
            LOG.error(_(" reschedule_loadbalancer_instance error"
                   " for loadbalancer '%(loadbalancer_id)s' ") %
                   {'loadbalancer_id': loadbalancer.id})
            pass

    def deploy_existing_instances(self):
        loadbalancers = self.db.get_loadbalancers(self.ctx)
        for loadbalancer in loadbalancers:
            try:
                driver = self.drivers[loadbalancer.provider.provider_name]
                self._call_driver_operation(
                     self.ctx, driver.load_balancer.create, loadbalancer)
            except:
                LOG.error(_(" Deploy error for loadbalancer '%(loadbalancer_id)s' ") %
                   {'loadbalancer_id': loadbalancer.id})
                # do not stop anything this is a minor error
                pass

    def _get_driver_for_provider(self, provider):
        try:
            return self.drivers[provider]
        except KeyError:
            # raise if not associated (should never be reached)
            raise n_exc.Invalid(_("Error retrieving driver for provider %s") %
                                provider)

    def get_driver_for_provider(self, provider):
        return self._get_driver_for_provider(provider).device_driver

    def _get_driver_for_loadbalancer(self, context, loadbalancer_id):
        loadbalancer = self.db.get_loadbalancer(context, loadbalancer_id)
        try:
            return self.drivers[loadbalancer.provider.provider_name]
        except KeyError:
            raise n_exc.Invalid(
                _("Error retrieving provider for load balancer. Possible "
                  "providers are %s.") % self.drivers.keys()
            )

    def _get_provider_name(self, entity):
        if ('provider' in entity and
                entity['provider'] != attrs.ATTR_NOT_SPECIFIED):
            provider_name = pconf.normalize_provider_name(entity['provider'])
            self.validate_provider(provider_name)
            return provider_name
        else:
            if not self.default_provider:
                raise pconf.DefaultServiceProviderNotFound(
                    service_type=constants.LOADBALANCERV2)
            return self.default_provider

    def _call_driver_operation(self, context, driver_method, db_entity,
                               old_db_entity=None):
        manager_method = "%s.%s" % (driver_method.__self__.__class__.__name__,
                                    driver_method.__name__)
        LOG.info(_("Calling driver operation %s") % manager_method)
        try:
            if old_db_entity:
                driver_method(context, old_db_entity, db_entity)
            else:
                driver_method(context, db_entity)
        except Exception:
            LOG.exception(_("There was an error in the driver"))
            self.db.update_status(context, db_entity.__class__._SA_MODEL,
                                  db_entity.id, constants.ERROR)
            raise loadbalancerv2.DriverError()

    def defer_listener(self, context, listener, cascade=True):
        self.db.update_status(context, models.Listener, listener.id,
                              constants.DEFERRED)
        if cascade and listener.default_pool:
            self.defer_pool(context, listener.default_pool, cascade=cascade)
        if cascade:
            self.defer_l7policies(context, listener.l7_policies)

    def defer_l7policies(self, context, l7policies):
        for l7policy in l7policies:
            if l7policy.redirect_pool:
                self.defer_pool(context,l7policy.redirect_pool)

    def defer_pool(self, context, pool, cascade=True):
        self.db.update_status(context, models.PoolV2, pool.id,
                              constants.DEFERRED)
        if cascade:
            self.defer_members(context, pool.members)

    def defer_members(self, context, members):
        for member in members:
            self.db.update_status(context, models.MemberV2,
                                  member.id, constants.DEFERRED)

    def defer_unlinked_entities(self, context, obj, old_obj=None):
        # if old_obj is None then this is delete else it is an update
        if isinstance(obj, models.Listener):
            # if listener.loadbalancer_id is set to None set listener status
            # to deferred
            deleted_listener = not old_obj
            unlinked_listener = (not obj.loadbalancer and old_obj and
                                 old_obj.loadbalancer)
            unlinked_pool = (bool(old_obj) and not obj.default_pool and
                             old_obj.default_pool)
            if unlinked_listener:
                self.db.update_status(context, models.Listener,
                                      old_obj.id, constants.DEFERRED)
            # if listener has been deleted OR if default_pool_id has been
            # updated to None, then set Pool and its children statuses to
            # DEFERRED
            if deleted_listener or unlinked_pool or unlinked_listener:
                if old_obj:
                    obj = old_obj
                if not obj.default_pool:
                    return
                self.db.update_status(context, models.PoolV2,
                                      obj.default_pool.id, constants.DEFERRED)
                for member in obj.default_pool.members:
                    self.db.update_status(context, models.MemberV2,
                                          member.id, constants.DEFERRED)
        elif isinstance(obj, models.PoolV2):
            pass

    def activate_linked_entities(self, context, obj):
        if isinstance(obj, data_models.LoadBalancer):
            self.db.update_status(context, models.LoadBalancer,
                                  obj.id, constants.ACTIVE)
            # only update loadbalancer's status because it's not able to
            # change any links to children
            return
        if isinstance(obj, data_models.Listener):
            self.db.update_status(context, models.Listener,
                                  obj.id, constants.ACTIVE)
            if obj.default_pool:
                self.activate_linked_entities(context, obj.default_pool)
        if isinstance(obj, data_models.Pool):
            self.db.update_status(context, models.PoolV2,
                                  obj.id, constants.ACTIVE)
            for member in obj.members:
                self.activate_linked_entities(context, member)
        if isinstance(obj, data_models.Member):
            # do not overwrite INACTVE status
            if obj.status != constants.INACTIVE:
                self.db.update_status(context, models.MemberV2, obj.id,
                                      constants.ACTIVE)
        if isinstance(obj, data_models.HealthMonitor):
            self.db.update_status(context, models.HealthMonitorV2, obj.id,
                                  constants.ACTIVE)
        if isinstance(obj, data_models.L7Policy):
            self.db.update_status(context, models.L7Policy, obj.id,
                               constants.ACTIVE)
            if obj.redirect_pool:
                self.activate_linked_entities(context, obj.redirect_pool)

        if isinstance(obj, data_models.L7Rule):
            self.db.update_status(context, models.L7Rule, obj.id,
                               constants.ACTIVE)

    def get_plugin_type(self):
        return constants.LOADBALANCERV2

    def get_plugin_description(self):
        return "Neutron LoadBalancer Service Plugin v2"

    def validate_provider(self, provider):
        if provider not in self.drivers:
            raise pconf.ServiceProviderNotFound(
                provider=provider, service_type=constants.LOADBALANCERV2)

    def create_loadbalancer(self, context, loadbalancer):
        loadbalancer = loadbalancer.get('loadbalancer')
        loadbalancer['admin_state_up'] = True
        provider_name = self._get_provider_name(loadbalancer)
        lb_db = self.db.create_loadbalancer(context, loadbalancer)
        self.service_type_manager.add_resource_association(
            context,
            constants.LOADBALANCERV2,
            provider_name, lb_db.id)
        driver = self.drivers[provider_name]
        self._call_driver_operation(
            context, driver.load_balancer.create, lb_db)
        return self.db.get_loadbalancer(context, lb_db.id).to_dict()

    def update_loadbalancer(self, context, id, loadbalancer):
        loadbalancer = loadbalancer.get('loadbalancer')
        old_lb = self.db.get_loadbalancer(context, id)
        self.db.test_and_set_status(context, models.LoadBalancer, id,
                                    constants.PENDING_UPDATE)
        try:
            updated_lb = self.db.update_loadbalancer(
                context, id, loadbalancer)
        except Exception as exc:
            self.db.update_status(context, models.LoadBalancer, id,
                                  old_lb.status)
            LOG.exception(exc)
            raise exc
        driver = self._get_driver_for_provider(old_lb.provider.provider_name)
        self._call_driver_operation(context,
                                    driver.load_balancer.update,
                                    updated_lb, old_db_entity=old_lb)
        return self.db.get_loadbalancer(context, updated_lb.id).to_dict()

    def delete_loadbalancer(self, context, id):
        old_lb = self.db.get_loadbalancer(context, id)
        #if old_lb.listeners:
        #    raise loadbalancerv2.EntityInUse(
        #        entity_using=models.Listener.NAME,
        #        id=old_lb.listeners[0].id,
        #        entity_in_use=models.LoadBalancer.NAME)
        self.db.test_and_set_status(context, models.LoadBalancer, id,
                                    constants.PENDING_DELETE)
        driver = self._get_driver_for_provider(old_lb.provider.provider_name)
        self._call_driver_operation(
            context, driver.load_balancer.delete, old_lb)

    def get_loadbalancer_instance(self, context, id):
        lb_db = self.db.get_loadbalancer(context, id)
        return lb_db

    def get_loadbalancer(self, context, id, fields=None):
        lb_db = self.db.get_loadbalancer(context, id)
        return self.db._fields(lb_db.to_dict(), fields)

    def get_loadbalancers(self, context, filters=None, fields=None):
        loadbalancers = self.db.get_loadbalancers(context, filters=filters)
        return [self.db._fields(lb.to_dict(), fields) for lb in loadbalancers]

    def create_listener(self, context, listener):
        listener = listener.get('listener')

        listener_db = self.db.create_listener(context, listener)

        if listener_db.attached_to_loadbalancer():
            driver = self._get_driver_for_loadbalancer(
                context, listener_db.loadbalancer_id)
            self._call_driver_operation(
                context, driver.listener.create, listener_db)
        else:
            # UOS : this will not reach forever.
            self.db.update_status(context, models.Listener, listener_db.id,
                                  constants.DEFERRED)

        return self.db.get_listener(context, listener_db.id).to_dict()

    def update_listener(self, context, id, listener):
        listener = listener.get('listener')
        old_listener = self.db.get_listener(context, id)
        self.db.test_and_set_status(context, models.Listener, id,
                                    constants.PENDING_UPDATE)

        try:
            listener_db = self.db.update_listener(context, id, listener)
        except Exception as exc:
            self.db.update_status(context, models.Listener, id,
                                  old_listener.status)
            raise exc

        if (listener_db.attached_to_loadbalancer() or
                old_listener.attached_to_loadbalancer()):
            if listener_db.attached_to_loadbalancer():
                driver = self._get_driver_for_loadbalancer(
                    context, listener_db.loadbalancer_id)
            else:
                driver = self._get_driver_for_loadbalancer(
                    context, old_listener.loadbalancer_id)
            self._call_driver_operation(
                context,
                driver.listener.update,
                listener_db,
                old_db_entity=old_listener)
        else:
            # UOS : this will not reach forever.
            self.db.update_status(context, models.Listener, id,
                                  constants.DEFERRED)

        return self.db.get_listener(context, listener_db.id).to_dict()

    def delete_listener(self, context, id):
        self.db.test_and_set_status(context, models.Listener, id,
                                    constants.PENDING_DELETE)
        listener_db = self.db.get_listener(context, id)

        if listener_db.attached_to_loadbalancer():
            driver = self._get_driver_for_loadbalancer(
                context, listener_db.loadbalancer_id)
            try:
                self._call_driver_operation(
                    context, driver.listener.delete, listener_db)
            except loadbalancerv2.OneListenerAdminStateUpAtLeast:
                with excutils.save_and_reraise_exception():
                    self.db.update_status(context, models.Listener,
                        id,constants.ACTIVE)
        else:
            # UOS : this will not reach forever.
            self.db.delete_listener(context, id)

    def get_listener(self, context, id, fields=None):
        listener_db = self.db.get_listener(context, id)
        return self.db._fields(listener_db.to_dict(), fields)

    def get_listeners(self, context, filters=None, fields=None):
        listeners = self.db.get_listeners(context, filters=filters)
        return [self.db._fields(listener.to_dict(), fields)
                for listener in listeners]

    def get_loadbalancer_lbaas_listeners(self, context,loadbalancer_id, filters=None, fields=None):
        if filters:
            filters.update(filters)
        else:
            filters = {'loadbalancer_id': [loadbalancer_id]}
        listeners = self.get_listeners(context, filters=filters)
        return listeners

    def _check_session_persistence_info(self, info):
        """Performs sanity check on session persistence info.

        :param info: Session persistence info
        """
        if info['type'] == lb_const.SESSION_PERSISTENCE_APP_COOKIE:
            if not info.get('cookie_name'):
                raise ValueError(_("'cookie_name' should be specified for %s"
                                   " session persistence.") % info['type'])
        else:
            if 'cookie_name' in info:
                raise ValueError(_("'cookie_name' is not allowed for %s"
                                   " session persistence") % info['type'])

    def _prepare_healthmonitor_info(self, info):

        if (info['type'] != lb_const.HEALTH_MONITOR_HTTP and
               info['type'] != lb_const.HEALTH_MONITOR_HTTPS):
           info.pop('http_method',None)
           info.pop('url_path',None)
           info.pop('expected_codes',None)
        else :
            if 'http_method' not in info:
                info['http_method'] = 'GET'
            if 'url_path' not in info:
                info['url_path'] = '/'
            if 'expected_codes' not in info:
                info['expected_codes'] = 200

    def create_pool(self, context, pool):
        pool = pool.get('pool')
        session_info = pool.get('session_persistence', None)
        if session_info:
            if pool['protocol'] != lb_const.PROTOCOL_HTTP:
                raise n_exc.Invalid(_("Can not specify session persistence for TCP protocol.")) 
            try:
                self._check_session_persistence_info(pool['session_persistence'])
            except ValueError:
                raise n_exc.Invalid(_("Error value for session persistence type."))
        healthmonitor_info = pool.get('healthmonitor', None)
        if healthmonitor_info:
           self._prepare_healthmonitor_info(pool['healthmonitor'])

        db_pool = self.db.create_pool(context, pool)
        # no need to call driver since on create it cannot be linked to a load
        # balancer, but will still update status to DEFERRED
        self.db.update_status(context, models.PoolV2, db_pool.id,
                              constants.DEFERRED)
        return self.db.get_pool(context, db_pool.id).to_dict()

    def update_pool(self, context, id, pool):
        pool = pool.get('pool')
        session_info = pool.get('session_persistence', None)
        if session_info:
            try:
                self._check_session_persistence_info(pool['session_persistence'])
            except ValueError:
                raise n_exc.Invalid(_("Error value for session persistence type."))
        healthmonitor_info = pool.get('healthmonitor', None)
        if healthmonitor_info:
           self._prepare_healthmonitor_info(pool['healthmonitor'])

        old_pool = self.db.get_pool(context, id)
        if (session_info and old_pool.protocol != lb_const.PROTOCOL_HTTP):
            raise n_exc.Invalid(_("Can not specify session persistence for TCP protocol.")) 
        self.db.test_and_set_status(context, models.PoolV2, id,
                                    constants.PENDING_UPDATE)
        try:
            updated_pool = self.db.update_pool(context, id, pool)
        except Exception as exc:
            self.db.update_status(context, models.PoolV2, id, old_pool.status)
            LOG.info('_update_pool exc: %s',exc)
            raise exc

        if (updated_pool.attached_to_loadbalancer()):
            if updated_pool.l7policy:
                loadbalancer_id = updated_pool.l7policy.listener.loadbalancer_id
            else:
                loadbalancer_id = updated_pool.listener.loadbalancer_id
            driver = self._get_driver_for_loadbalancer(
                    context, loadbalancer_id)
            self._call_driver_operation(context,
                                        driver.pool.update,
                                        updated_pool,
                                        old_db_entity=old_pool)
        elif (old_pool.attached_to_loadbalancer()):
            if old_pool.l7policy:
                loadbalancer_id = old_pool.l7policy.listener.loadbalancer_id
            else:
                loadbalancer_id = old_pool.listener.loadbalancer_id
            driver = self._get_driver_for_loadbalancer(
                    context, loadbalancer_id)
            self._call_driver_operation(context,
                                        driver.pool.update,
                                        updated_pool,
                                        old_db_entity=old_pool)
        else:
            self.db.update_status(context, models.PoolV2, id,
                                  constants.DEFERRED)

        return self.db.get_pool(context, updated_pool.id).to_dict()

    def delete_pool(self, context, id):
        self.db.test_and_set_status(context, models.PoolV2, id,
                                    constants.PENDING_DELETE)
        db_pool = self.db.get_pool(context, id)

        if db_pool.attached_to_loadbalancer():
            if db_pool.l7policy:
                loadbalancer_id = db_pool.l7policy.listener.loadbalancer_id
            else:
                loadbalancer_id = db_pool.listener.loadbalancer_id
            driver = self._get_driver_for_loadbalancer(
                context, loadbalancer_id)
            self._call_driver_operation(context, driver.pool.delete, db_pool)
        else:
            self.db.delete_pool(context, id)

    def get_pools(self, context, filters=None, fields=None):
        pools = self.db.get_pools(context, filters=filters)
        return [self.db._fields(pool.to_dict(), fields) for pool in pools]

    def get_pool(self, context, id, fields=None):
        pool_db = self.db.get_pool(context, id)
        return self.db._fields(pool_db.to_dict(), fields)

    def create_pool_member(self, context, member, pool_id):
        member = member.get('member')
        member_db = self.db.create_pool_member(context, member, pool_id)

        if member_db.attached_to_loadbalancer():
            driver = self._get_driver_for_loadbalancer(
                context, member_db.pool.listener.loadbalancer_id)
            self._call_driver_operation(context,
                                        driver.member.create,
                                        member_db)
        else:
            self.db.update_status(context, models.MemberV2, member_db.id,
                                  constants.DEFERRED)

        return self.db.get_pool_member(context, member_db.id,
                                       pool_id).to_dict()

    def update_pool_member(self, context, id, member, pool_id):
        member = member.get('member')
        old_member = self.db.get_pool_member(context, id, pool_id)
        self.db.test_and_set_status(context, models.MemberV2, id,
                                    constants.PENDING_UPDATE)
        try:
            updated_member = self.db.update_pool_member(context, id, member,
                                                        pool_id)
        except Exception as exc:
            self.db.update_status(context, models.MemberV2, id,
                                  old_member.status)
            raise exc
        # cannot unlink a member from a loadbalancer through an update
        # so no need to check if the old_member is attached
        if updated_member.attached_to_loadbalancer():
            driver = self._get_driver_for_loadbalancer(
                context, updated_member.pool.listener.loadbalancer_id)
            self._call_driver_operation(context,
                                        driver.member.update,
                                        updated_member,
                                        old_db_entity=old_member)
        else:
            self.db.update_status(context, models.MemberV2, id,
                                  constants.DEFERRED)

        return self.db.get_pool_member(context, updated_member.id,
                                       pool_id).to_dict()

    def delete_pool_member(self, context, id, pool_id):
        self.db.test_and_set_status(context, models.MemberV2, id,
                                    constants.PENDING_DELETE)
        db_member = self.db.get_pool_member(context, id, pool_id)

        if db_member.attached_to_loadbalancer():
            driver = self._get_driver_for_loadbalancer(
                context, db_member.pool.listener.loadbalancer_id)
            self._call_driver_operation(context,
                                        driver.member.delete,
                                        db_member)
        else:
            self.db.delete_pool_member(context, id, pool_id)

    def get_pool_members(self, context, pool_id, filters=None, fields=None):
        members = self.db.get_pool_members(context, pool_id, filters=filters)
        return [self.db._fields(member.to_dict(), fields)
                for member in members]

    def get_pool_member(self, context, id, pool_id, filters=None, fields=None):
        member = self.db.get_pool_member(context, id, pool_id, filters=filters)
        return member.to_dict()

    def create_l7policy(self, context, l7policy):
        l7policy = l7policy.get('l7policy')
        l7policy_db = self.db.create_l7policy(context, l7policy)

        if l7policy_db.attached_to_loadbalancer():
            driver = self._get_driver_for_loadbalancer(
                context, l7policy_db.listener.loadbalancer_id)
            self._call_driver_operation(context,
                                        driver.l7policy.create,
                                        l7policy_db)
        else:
            self.db.update_status(context, models.L7Policy, l7policy_db.id,
                                  constants.DEFERRED)

        return self.db.get_l7policy(context, l7policy_db.id).to_dict()

    def update_l7policy(self, context, id, l7policy):
        l7policy = l7policy.get('l7policy')
        old_l7policy_db = self.db.get_l7policy(context, id)
        self.db.test_and_set_status(context, models.L7Policy, id,
                                    constants.PENDING_UPDATE)
        try:
            updated_l7policy_db = self.db.update_l7policy(
                context, id, l7policy)
        except Exception as exc:
            self.db.update_status(context, models.L7Policy, id,
                               old_l7policy_db.status)
            raise exc

        if (updated_l7policy_db.attached_to_loadbalancer() or
                old_l7policy_db.attached_to_loadbalancer()):
            if updated_l7policy_db.attached_to_loadbalancer():
                driver = self._get_driver_for_loadbalancer(
                    context, updated_l7policy_db.listener.loadbalancer_id)
            else:
                driver = self._get_driver_for_loadbalancer(
                    context, old_l7policy_db.listener.loadbalancer_id)
            self._call_driver_operation(context,
                                        driver.l7policy.update,
                                        updated_l7policy_db,
                                        old_db_entity=old_l7policy_db)

        return self.db.get_l7policy(context, id).to_dict()

    def delete_l7policy(self, context, id):
        self.db.test_and_set_status(context, models.L7Policy, id,
                                    constants.PENDING_DELETE)
        l7policy_db = self.db.get_l7policy(context, id)

        if l7policy_db.attached_to_loadbalancer():
            driver = self._get_driver_for_loadbalancer(
                context, l7policy_db.listener.loadbalancer_id)
            self._call_driver_operation(context, driver.l7policy.delete,
                                        l7policy_db)
        else:
            self.db.delete_l7policy(context, id)

    def get_l7policies(self, context, filters=None, fields=None):
        l7policy_dbs = self.db.get_l7policies(context, filters=filters)
        return [self.db._fields(l7policy_db.to_dict(), fields)
                for l7policy_db in l7policy_dbs]

    def get_l7policy(self, context, id, fields=None):
        l7policy_db = self.db.get_l7policy(context, id)
        return self.db._fields(l7policy_db.to_dict(), fields)

    def get_listener_lbaas_l7policies(self, context,listener_id, filters=None, fields=None):
        if filters:
            filters.update(filters)
        else:
            filters = {'listener_id': [listener_id]}
        l7policy_dbs = self.get_l7policies(context, filters=filters)
        return l7policy_dbs

    def create_l7policy_rule(self, context, rule, l7policy_id):
        rule = rule.get('rule')
        rule_db = self.db.create_l7policy_rule(context, rule, l7policy_id)

        if rule_db.attached_to_loadbalancer():
            driver = self._get_driver_for_loadbalancer(
                context, rule_db.l7policy.listener.loadbalancer_id)
            self._call_driver_operation(context,
                                        driver.l7rule.create,
                                        rule_db)
        else:
            self.db.update_status(context, models.L7Rule, rule_db.id,
                                  constants.DEFERRED)

        return self.db.get_l7policy_rule(context, rule_db.id, l7policy_id).to_dict()

    def update_l7policy_rule(self, context, id, rule, l7policy_id):
        rule = rule.get('rule')
        old_rule_db = self.db.get_l7policy_rule(context, id, l7policy_id)
        self.db.test_and_set_status(context, models.L7Rule, id,
                                    constants.PENDING_UPDATE)
        try:
            upd_rule_db = self.db.update_l7policy_rule(
                context, id, rule, l7policy_id)
        except Exception as exc:
            self.update_status(context, models.L7Rule, id, old_rule_db.status)
            raise exc

        if (upd_rule_db.attached_to_loadbalancer() or
                old_rule_db.attached_to_loadbalancer()):
            if upd_rule_db.attached_to_loadbalancer():
                driver = self._get_driver_for_loadbalancer(
                    context, upd_rule_db.l7policy.listener.loadbalancer_id)
            else:
                driver = self._get_driver_for_loadbalancer(
                    context, old_rule_db.listener.loadbalancer_id)
            self._call_driver_operation(context,
                                        driver.l7rule.update,
                                        upd_rule_db,
                                        old_db_entity=old_rule_db)
        else:
            self.db.update_status(context, models.L7Rule, id,
                                  constants.DEFERRED)

        return self.db.get_l7policy_rule(context, id, l7policy_id).to_dict()

    def delete_l7policy_rule(self, context, id, l7policy_id):
        self.db.test_and_set_status(context, models.L7Rule, id,
                                    constants.PENDING_DELETE)
        rule_db = self.db.get_l7policy_rule(context, id, l7policy_id)

        if rule_db.attached_to_loadbalancer():
            driver = self._get_driver_for_loadbalancer(
                context, rule_db.l7policy.listener.loadbalancer_id)
            self._call_driver_operation(context, driver.l7rule.delete,
                                        rule_db)
        else:
            self.db.delete_l7policy_rule(context, id, l7policy_id)

    def get_l7policy_rules(self, context, l7policy_id,
                           filters=None, fields=None):
        rule_dbs = self.db.get_l7policy_rules(
            context, l7policy_id, filters=filters)
        return [self.db._fields(rule_db.to_dict(), fields)
                for rule_db in rule_dbs]

    def get_l7policy_rule(self, context, id, l7policy_id, fields=None):
        rule_db = self.db.get_l7policy_rule(context, id, l7policy_id)
        return self.db._fields(rule_db.to_dict(), fields)

    def _get_members(self, loadbalancer):
        for listener in loadbalancer.listeners:
            if listener.default_pool:
                for member in listener.default_pool.members:
                    yield member
            for l7policy in listener.l7_policies:
                if l7policy.redirect_pool:
                    for member in l7policy.redirect_pool.members:
                        yield member

    def _set_member_status(self, context, loadbalancer, members_stats):
        for member in self._get_members(loadbalancer):
            if member.id in members_stats:
                status = members_stats[member.id].get('status')
                old_status = self.db.get_member_status_info(context, member.id)
                if status and status == constants.ACTIVE:
                    self.db.update_status(
                        context, models.MemberV2, member.id,
                        constants.ACTIVE)
                else:
                    self.db.update_status(
                        context, models.MemberV2, member.id,
                        constants.INACTIVE)
                if old_status != status:
                    LOG.info(_('kiki_set_member_status: %(obj_id)s %(status)s notified'),
                        {'obj_id': member.id, 'status':status })
                    notifier = n_rpc.get_notifier('loadbalancer')
                    notifier.info(context, 'member.update.end', {'id':member.id})

    def stats(self, context, loadbalancer_id, stats_data=None):
        LOG.debug(_(" stats '%(loadbalancer_id)s' ,%(stat)s") %
                   {'loadbalancer_id': loadbalancer_id,"stat":stats_data})
        try:
            loadbalancer = self.db.get_loadbalancer(context, loadbalancer_id)
        except Exception:
            LOG.error("Exception when stats for loadbalancer %s", loadbalancer_id)
            return
        driver = self._get_driver_for_loadbalancer(context, loadbalancer_id)
        # if we get something from the driver -
        # update the db and return the value from db
        # else - return what we have in db
        if stats_data:
            self.db.update_loadbalancer_stats(context, loadbalancer_id,
                                              stats_data)
            if 'members' in stats_data:
                self._set_member_status(context, loadbalancer,
                                            stats_data['members'])

        db_stats = self.db.stats(context, loadbalancer_id)

        return {'stats': db_stats.to_dict()}

    # NOTE(brandon-logan): these need to be concrete methods because the
    # neutron request pipeline calls these methods before the plugin methods
    # are ever called
    def get_members(self, context, filters=None, fields=None):
        pass

    def get_member(self, context, id, fields=None):
        pass
