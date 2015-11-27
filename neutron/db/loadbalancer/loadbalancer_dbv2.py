#
# Copyright 2014 OpenStack Foundation.  All rights reserved
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

import contextlib
import sys

from oslo.db import exception
from sqlalchemy import orm
from sqlalchemy.orm import exc

from neutron.api.v2 import attributes
from neutron.db import common_db_mixin as base_db
from neutron.db.loadbalancer import models
from neutron.extensions import loadbalancerv2
from neutron import manager
from neutron.openstack.common import excutils
from neutron.openstack.common import log as logging
from neutron.openstack.common import lockutils
from neutron.openstack.common import timeutils
from neutron.openstack.common import uuidutils
from neutron.plugins.common import constants
from neutron.common import constants as device_constants
from neutron.extensions import securitygroup as ext_sg
from neutron.services.loadbalancer import constants as lb_const
from neutron.services.loadbalancer import data_models

LOG = logging.getLogger(__name__)


class LoadBalancerPluginDbv2(base_db.CommonDbMixin):
    """Wraps loadbalancer with SQLAlchemy models.

    A class that wraps the implementation of the Neutron loadbalancer
    plugin database access interface using SQLAlchemy models.
    """

    @property
    def _core_plugin(self):
        return manager.NeutronManager.get_plugin()

    def _get_resource(self, context, model, id, for_update=False):
        resource = None
        try:
            if for_update:
                query = self._model_query(context, model).filter(
                    model.id == id).with_lockmode('update')
                resource = query.one()
            else:
                resource = self._get_by_id(context, model, id)
        except exc.NoResultFound:
            with excutils.save_and_reraise_exception(reraise=False) as ctx:
                if issubclass(model, (models.LoadBalancer, models.Listener,
                                      models.PoolV2, models.MemberV2,
                                      models.HealthMonitorV2,
                                      models.LoadBalancerStatistics,
                                      models.SessionPersistenceV2,
                                      models.L7Policy,
                                      models.L7Rule)):
                    raise loadbalancerv2.EntityNotFound(name=model.NAME, id=id)
                ctx.reraise = True
        return resource

    def _resource_exists(self, context, model, id):
        try:
            self._get_by_id(context, model, id)
        except exc.NoResultFound:
            return False
        return True

    def _get_resources(self, context, model, filters=None):
        query = self._get_collection_query(context, model,
                                           filters=filters)
        return [model_instance for model_instance in query]

    def _create_port_for_load_balancer(self, context, lb_db,
                                     ip_address, securitygroup_id):

        network = self._core_plugin.get_network(context, lb_db.vip_network_id)

        fixed_ip = {}
        append_fixed_ip = False
        # resolve subnet and create port
        if lb_db.vip_subnet_id:
            subnet = self._core_plugin.get_subnet(context, lb_db.vip_subnet_id)
            if subnet['network_id']!=lb_db.vip_network_id:
                raise loadbalancerv2.NetworkSubnetIDMismatch(
                    subnet_id = lb_db.vip_subnet_id,
                    network_id = lb_db.vip_network_id)
            fixed_ip['subnet_id'] = subnet['id']
            append_fixed_ip = True

        if ip_address and ip_address != attributes.ATTR_NOT_SPECIFIED:
            fixed_ip['ip_address'] = ip_address

        port_data = {
            'tenant_id': lb_db.tenant_id,
            'name': 'loadbalancer-' + lb_db.id,
            'network_id': network['id'],
            'mac_address': attributes.ATTR_NOT_SPECIFIED,
            'admin_state_up': False,
            'device_id': lb_db.id,
            'device_owner': device_constants.DEVICE_OWNER_LOADBALANCER,
            'fixed_ips': [fixed_ip],
            'security_groups':[securitygroup_id]
        }

        if not append_fixed_ip:
            port_data.pop('fixed_ips', None)
            port_data['fixed_ips'] = attributes.ATTR_NOT_SPECIFIED

        port = self._core_plugin.create_port(context, {'port': port_data})
        lb_db.vip_port_id = port['id']

        # For we only support one fixed ip in lbaas port
        fixed_ip = port['fixed_ips'][0]
        lb_db.vip_address = fixed_ip['ip_address']
        # Only the subnet is specified set the vip_subnet_id
        if append_fixed_ip:
            lb_db.vip_subnet_id = fixed_ip['subnet_id']
        lb_db.securitygroup_id = securitygroup_id
        # explicitly sync session with db
        context.session.flush()

    def _update_port_for_load_balancer(self, context, lb_db, securitygroup_id):
        LOG.info('_update_port_for_load_balancer sg is %s',securitygroup_id)
        port_data = {
            'tenant_id': lb_db.tenant_id,
            'security_groups':[securitygroup_id],
            'device_id': lb_db.id,
            'device_owner': device_constants.DEVICE_OWNER_LOADBALANCER
        }
        port = self._core_plugin.update_port(context,lb_db.vip_port_id, {'port': port_data})
        lb_db.securitygroup_id = securitygroup_id
        context.session.flush()

    def _create_loadbalancer_stats(self, context, loadbalancer_id, data=None):
        # This is internal method to add load balancer statistics.  It won't
        # be exposed to API
        data = data or {}
        stats_db = models.LoadBalancerStatistics(
            loadbalancer_id=loadbalancer_id,
            bytes_in=data.get(lb_const.STATS_IN_BYTES, 0),
            bytes_out=data.get(lb_const.STATS_OUT_BYTES, 0),
            active_connections=data.get(lb_const.STATS_ACTIVE_CONNECTIONS, 0),
            total_connections=data.get(lb_const.STATS_TOTAL_CONNECTIONS, 0)
        )
        return stats_db

    def _delete_loadbalancer_stats(self, context, loadbalancer_id):
        # This is internal method to delete pool statistics. It won't
        # be exposed to API
        with context.session.begin(subtransactions=True):
            stats_qry = context.session.query(models.LoadBalancerStatistics)
            try:
                stats = stats_qry.filter_by(
                    loadbalancer_id=loadbalancer_id).one()
            except exc.NoResultFound:
                raise loadbalancerv2.EntityNotFound(
                    name=models.LoadBalancerStatistics.NAME,
                    id=loadbalancer_id)
            context.session.delete(stats)

    def _load_id_and_tenant_id(self, context, model_dict):
        model_dict['id'] = uuidutils.generate_uuid()
        model_dict['tenant_id'] = self._get_tenant_id_for_create(
            context, model_dict)

    def assert_modification_allowed(self, obj):
        status = getattr(obj, 'status', None)
        LOG.debug(' assert_modification_allowed the status is %s',status)
        if status in [constants.PENDING_DELETE, constants.PENDING_UPDATE,
                      constants.PENDING_CREATE]:
            id = getattr(obj, 'id', None)
            raise loadbalancerv2.StateInvalid(id=id, state=status)

    def test_and_set_status(self, context, model, id, status):
        with context.session.begin(subtransactions=True):
            model_db = self._get_resource(context, model, id, for_update=True)
            self.assert_modification_allowed(model_db)
            if model_db.status != status:
                model_db.status = status

    def update_status(self, context, model, id, status):
        LOG.debug(_("update_status for_%(model)s id %(id)s %(status)s"),
                    {'model':model, 'id':id, 'status':status } )
        with context.session.begin(subtransactions=True):
            if issubclass(model, models.LoadBalancer):
                try:
                    model_db = (self._model_query(context, model).
                                filter(model.id == id).
                                options(orm.noload('vip_port')).
                                one())
                except exc.NoResultFound:
                    raise loadbalancerv2.EntityNotFound(
                        name=models.LoadBalancer.NAME, id=id)
            else:
                model_db = self._get_resource(context, model, id)
            if model_db.status != status:
                model_db.status = status

    def create_loadbalancer(self, context, loadbalancer):
        with context.session.begin(subtransactions=True):
            self._load_id_and_tenant_id(context, loadbalancer)
            vip_address = loadbalancer.pop('vip_address')
            securitygroup_id = loadbalancer.get('securitygroup_id')
            loadbalancer['status'] = constants.PENDING_CREATE
            loadbalancer['created_at'] = timeutils.utcnow()
            lb_db = models.LoadBalancer(**loadbalancer)
            context.session.add(lb_db)
            context.session.flush()
            lb_db.stats = self._create_loadbalancer_stats(
                context, lb_db.id)
            context.session.add(lb_db)

        # create port outside of lb create transaction since it can sometimes
        # cause lock wait timeouts
        try:
            self._create_port_for_load_balancer(context, lb_db,
                                        vip_address, securitygroup_id)
        except ext_sg.SecurityGroupNotFound:
            LOG.error('_create_port_for_load_balancer %s securitygroup',lb_db.id)
            with excutils.save_and_reraise_exception():
                context.session.delete(lb_db)
                context.session.flush()
                raise loadbalancerv2.SecurityGroupNotFound(id=lb_db.id) 
        except Exception:
            LOG.error('_create_port_for_load_balancer %s',lb_db.id)
            with excutils.save_and_reraise_exception():
                context.session.delete(lb_db)
                context.session.flush()
        return data_models.LoadBalancer.from_sqlalchemy_model(lb_db)

    def update_loadbalancer(self, context, id, loadbalancer):
        with context.session.begin(subtransactions=True):
            securitygroup_id = loadbalancer.pop('securitygroup_id', None)
            lb_db = self._get_resource(context, models.LoadBalancer, id)
            lb_db.update(loadbalancer)
        context.session.refresh(lb_db)
        if securitygroup_id:
            LOG.debug('update_loadbalancer sg is %s',securitygroup_id)
            try:
                self._update_port_for_load_balancer(context, lb_db, securitygroup_id)
            except Exception:
                LOG.error('_udpate_port_for_load_balancer %s',lb_db.id)
                raise loadbalancerv2.UpdateSecurityGroupFailed(id=lb_db.id)
        LOG.debug('update_loadbalancer lb_db sg is %s',lb_db.securitygroup_id)
        return data_models.LoadBalancer.from_sqlalchemy_model(lb_db)

    def delete_loadbalancer(self, context, id):
        with context.session.begin(subtransactions=True):
            lb_db = self._get_resource(context, models.LoadBalancer, id)
            context.session.delete(lb_db)
        if lb_db.vip_port:
            self._core_plugin.delete_port(context, lb_db.vip_port_id)

    def get_loadbalancers(self, context, filters=None):
        lb_dbs = self._get_resources(context, models.LoadBalancer,
                                     filters=filters)
        return [data_models.LoadBalancer.from_sqlalchemy_model(lb_db)
                for lb_db in lb_dbs]

    def get_loadbalancer(self, context, id):
        lb_db = self._get_resource(context, models.LoadBalancer, id)
        return data_models.LoadBalancer.from_sqlalchemy_model(lb_db)

    def create_listener(self, context, listener):

        try:
            with contextlib.nested(lockutils.lock('db-access'),
                               context.session.begin(subtransactions=True)):
                self._load_id_and_tenant_id(context, listener)
                listener['status'] = constants.PENDING_CREATE
                # Check for unspecified loadbalancer_id and listener_id and
                # set to None
                for id in ['loadbalancer_id', 'default_pool_id']:
                    if listener.get(id) == attributes.ATTR_NOT_SPECIFIED:
                        listener[id] = None
                pool_id = listener.get('default_pool_id')
                lb_id = listener.get('loadbalancer_id')
                if lb_id:
                    if not self._resource_exists(context, models.LoadBalancer,
                                                 lb_id):
                        raise loadbalancerv2.EntityNotFound(
                            name=models.LoadBalancer.NAME, id=lb_id)
                loadbalancer_db = self._get_resource(context, models.LoadBalancer, lb_id)
                if pool_id:
                    if not self._resource_exists(context, models.PoolV2,
                                                 pool_id):
                        raise loadbalancerv2.EntityNotFound(
                            name=models.PoolV2.NAME, id=pool_id)
                    pool = self._get_resource(context, models.PoolV2, pool_id)

                    if pool.subnet_id:
                        if loadbalancer_db.vip_subnet_id != pool.subnet_id:
                            raise loadbalancerv2.LoadBalancerPoolSubnetMismatch()
                    else:
                        if loadbalancer_db.subnet_id:
                            raise loadbalancerv2.LoadBalancerPoolSubnetMismatch()
                        elif loadbalancer_db.network_id!=pool.network_id:
                            raise loadbalancerv2.LoadBalancerPoolNetworkMismatch()

                    if ((pool.protocol, listener.get('protocol'))
                        not in lb_const.LISTENER_POOL_COMPATIBLE_PROTOCOLS):
                        raise loadbalancerv2.ListenerPoolProtocolMismatch(
                            listener_proto=listener['protocol'],
                            pool_proto=pool.protocol)
                    filters = {'default_pool_id': [pool_id]}
                    listenerpools = self._get_resources(context,
                                                        models.Listener,
                                                        filters=filters)
                    if listenerpools:
                        raise loadbalancerv2.EntityInUse(
                            entity_using=models.Listener.NAME,
                            id=listenerpools[0].id,
                            entity_in_use=models.PoolV2.NAME)
                    filters = {'redirect_pool_id': [pool_id]}
                    l7policypools = self._get_resources(context,
                                                    models.L7Policy,
                                                    filters=filters)
                    if l7policypools:
                        raise loadbalancerv2.EntityInUse(
                            entity_using=models.L7Policy.NAME,
                            id=l7policypools[0].id,
                            entity_in_use=models.PoolV2.NAME)

                listener['created_at'] = timeutils.utcnow()
                listener_db_entry = models.Listener(**listener)

                context.session.add(listener_db_entry)
        except exception.DBDuplicateEntry:
            raise loadbalancerv2.LoadBalancerListenerProtocolPortExists(
                lb_id=listener['loadbalancer_id'],
                protocol_port=listener['protocol_port'])
        return data_models.Listener.from_sqlalchemy_model(listener_db_entry)

    def update_listener(self, context, id, listener):

        with contextlib.nested(lockutils.lock('db-access'),
                               context.session.begin(subtransactions=True)):
            listener_db = self._get_resource(context, models.Listener, id)
            admin_enable = listener.get('admin_state_up')
            if (admin_enable is not None and admin_enable==False
               and listener_db.admin_state_up==True):
                filters = {'loadbalancer_id': [listener_db.loadbalancer_id],
                           'admin_state_up': [True]}
                up_listeners = self._get_resources(context,
                                                    models.Listener,
                                                    filters=filters)
                if len(up_listeners)<=1 :
                    raise loadbalancerv2.OneListenerAdminStateUpAtLeast(
                        lb_id=listener_db.loadbalancer_id)

            pool_id = listener.get('default_pool_id')
            lb_id = listener.get('loadbalancer_id')

            # Do not allow changing loadbalancer ids
            if listener_db.loadbalancer_id and lb_id:
                raise loadbalancerv2.AttributeIDImmutable(
                    attribute='loadbalancer_id')
            # Do not allow changing pool ids
            #if listener_db.default_pool_id and pool_id:
            #    raise loadbalancerv2.AttributeIDImmutable(
            #        attribute='default_pool_id')
            if lb_id:
                if not self._resource_exists(context, models.LoadBalancer,
                                             lb_id):
                    raise loadbalancerv2.EntityNotFound(
                        name=models.LoadBalancer.NAME, id=lb_id)
            loadbalancer_db = listener_db.loadbalancer
            if pool_id:
                if not self._resource_exists(context, models.PoolV2, pool_id):
                    raise loadbalancerv2.EntityNotFound(
                        name=models.PoolV2.NAME, id=pool_id)
                pool = self._get_resource(context, models.PoolV2, pool_id)
                if pool.subnet_id:
                    if loadbalancer_db.vip_subnet_id != pool.subnet_id:
                        raise loadbalancerv2.LoadBalancerPoolSubnetMismatch()
                else:
                    if loadbalancer_db.vip_subnet_id:
                        raise loadbalancerv2.LoadBalancerPoolSubnetMismatch()
                    elif loadbalancer_db.vip_network_id!=pool.network_id:
                        raise loadbalancerv2.LoadBalancerPoolNetworkMismatch()

                protocol = listener.get('protocol') or listener_db.protocol
                if pool.protocol != protocol:
                    raise loadbalancerv2.ListenerPoolProtocolMismatch(
                        listener_proto=protocol,
                        pool_proto=pool.protocol)
                filters = {'default_pool_id': [pool_id]}
                listenerpools = self._get_resources(context,
                                                    models.Listener,
                                                    filters=filters)
                if listenerpools:
                    if listenerpools[0].id!=id:
                        raise loadbalancerv2.EntityInUse(
                            entity_using=models.Listener.NAME,
                            id=listenerpools[0].id,
                            entity_in_use=models.PoolV2.NAME)

                filters = {'redirect_pool_id': [pool_id]}
                l7policypools = self._get_resources(context,
                                                    models.L7Policy,
                                                    filters=filters)
                if l7policypools:
                    raise loadbalancerv2.EntityInUse(
                        entity_using=models.L7Policy.NAME,
                        id=l7policypools[0].id,
                        entity_in_use=models.PoolV2.NAME)

                if (listener_db.default_pool_id and
                       listener_db.default_pool_id != pool_id):
                    self.update_status(context, models.PoolV2,
                        listener_db.default_pool_id, constants.DEFERRED)
            else:
                #Only if the default_pool_id exists and set to None
                if 'default_pool_id' in listener:
                    listener['default_pool_id'] =  None
                    listener['default_pool'] = None
            listener_db.update(listener)
        context.session.refresh(listener_db)
        return data_models.Listener.from_sqlalchemy_model(listener_db)

    def delete_listener(self, context, id):
        with contextlib.nested(lockutils.lock('db-access'),
                               context.session.begin(subtransactions=True)):
            listener_db_entry = self._get_resource(context, models.Listener, id)
            #if listener_db_entry.admin_state_up:
            #    filters = {'loadbalancer_id': [listener_db_entry.loadbalancer_id],
            #               'admin_state_up': [True]}
            #    all_filters = {'loadbalancer_id': [listener_db_entry.loadbalancer_id]}
            #    all_listeners = self._get_resources(context,
            #                                        models.Listener,
            #                                        filters=all_filters)
            #    if len(all_listeners)>1:
            #        up_listeners = self._get_resources(context,
            #                                            models.Listener,
            #                                            filters=filters)
            #        if len(up_listeners)<=1:
            #            raise loadbalancerv2.OneListenerAdminStateUpAtLeast(
            #                lb_id=listener_db_entry.loadbalancer_id)
            context.session.delete(listener_db_entry)

    def get_listeners(self, context, filters=None):
        listener_dbs = self._get_resources(context, models.Listener,
                                           filters=filters)
        return [data_models.Listener.from_sqlalchemy_model(listener_db)
                for listener_db in listener_dbs]

    def get_listener(self, context, id):
        listener_db = self._get_resource(context, models.Listener, id)
        return data_models.Listener.from_sqlalchemy_model(listener_db)

    def _create_session_persistence_db(self, session_info, pool_id):
        session_info['pool_id'] = pool_id
        return models.SessionPersistenceV2(**session_info)

    def _update_pool_session_persistence(self, context, pool_id, info):
        LOG.info('_update_pool_session_persistence info %s',info)
        pool = self._get_resource(context, models.PoolV2, pool_id)
        with context.session.begin(subtransactions=True):
            # Update sessionPersistence table
            sess_qry = context.session.query(models.SessionPersistenceV2)
            sesspersist_db = sess_qry.filter_by(pool_id=pool_id).first()

            # Insert a None cookie_info if it is not present to overwrite an
            # an existing value in the database.
            if 'cookie_name' not in info:
                info['cookie_name'] = None

            if sesspersist_db:
                sesspersist_db.update(info)
            else:
                info['pool_id'] = pool_id
                sesspersist_db = models.SessionPersistenceV2(**info)
                context.session.add(sesspersist_db)
                # Update pool table
                pool.session_persistence = sesspersist_db
            context.session.add(pool)

    def _delete_session_persistence(self, context, pool_id):
        LOG.info('_delete_session_persistence for pool_id %s start',pool_id)
        with context.session.begin(subtransactions=True):
            sess_qry = context.session.query(models.SessionPersistenceV2)
            sess_qry.filter_by(pool_id=pool_id).delete()
        LOG.info('_delete_session_persistence for pool_id %s end',pool_id)

    def _create_healthmonitor_db(self, healthmonitor_info, pool_id):
        healthmonitor_info['id'] = pool_id
        return models.HealthMonitorV2(**healthmonitor_info)

    def _update_pool_healthmonitor(self, context, pool_id, info):
        pool = self._get_resource(context, models.PoolV2, pool_id)
        with context.session.begin(subtransactions=True):
            # Update healthMonitor table
            sess_qry = context.session.query(models.HealthMonitorV2)
            healthmonitor_db = sess_qry.filter_by(id=pool_id).first()

            if healthmonitor_db:
                healthmonitor_db.update(info)
            else:
                info['id'] = pool_id
                healthmonitor_db = models.HealthMonitorV2(**info)
                context.session.add(healthmonitor_db)
                # Update pool table
                pool.healthmonitor = healthmonitor_db
            context.session.add(pool)

    def _delete_healthmonitor(self, context, pool_id):
        with context.session.begin(subtransactions=True):
            sess_qry = context.session.query(models.HealthMonitorV2)
            sess_qry.filter_by(id=pool_id).delete()

    def create_pool(self, context, pool):
        network = self._core_plugin.get_network(context, pool['network_id'])
        subnet_id = pool.get('subnet_id', None)
        if subnet_id:
            subnet = self._core_plugin.get_subnet(context, subnet_id)

            if subnet['network_id']!=pool['network_id']:
                raise loadbalancerv2.NetworkSubnetIDMismatch(
                    subnet_id = subnet_id,
                    network_id = pool['network_id'])

        with context.session.begin(subtransactions=True):
            self._load_id_and_tenant_id(context, pool)
            pool['status'] = constants.PENDING_CREATE

            session_info = pool.pop('session_persistence')
            healthmonitor_info = pool.pop('healthmonitor')

            pool['created_at'] = timeutils.utcnow()
            pool_db = models.PoolV2(**pool)
            if session_info:
                LOG.debug('_create_pool session_info %s',session_info)
                s_p = self._create_session_persistence_db(session_info,
                                                          pool_db.id)
                pool_db.session_persistence = s_p
            if healthmonitor_info:
                health_monitor = self._create_healthmonitor_db(healthmonitor_info,
                                                          pool_db.id)
                pool_db.healthmonitor = health_monitor
            LOG.debug('_create_pool pool_db %s', pool_db)

            context.session.add(pool_db)
            context.session.flush()
        return data_models.Pool.from_sqlalchemy_model(pool_db)

    def update_pool(self, context, id, pool):
        with context.session.begin(subtransactions=True):
            pool_db = self._get_resource(context, models.PoolV2, id)

            sp = pool.pop('session_persistence', None)
            hm_info = pool.pop('healthmonitor', None)
            if sp:
                self._update_pool_session_persistence(context, id, sp)
            else:
                LOG.info('_update_pool %s delete session',id)
                pool['session_persistence'] = None
            if hm_info:
                self._update_pool_healthmonitor(context, id, hm_info)
            else:
                pool['healthmonitor'] = None
            pool_db.update(pool)
        context.session.refresh(pool_db)
        return data_models.Pool.from_sqlalchemy_model(pool_db)

    def delete_pool(self, context, id):
        with context.session.begin(subtransactions=True):
            pool_db = self._get_resource(context, models.PoolV2, id)
            context.session.delete(pool_db)

    def get_pools(self, context, filters=None):
        pool_dbs = self._get_resources(context, models.PoolV2, filters=filters)
        return [data_models.Pool.from_sqlalchemy_model(pool_db)
                for pool_db in pool_dbs]

    def get_pool(self, context, id):
        pool_db = self._get_resource(context, models.PoolV2, id)
        return data_models.Pool.from_sqlalchemy_model(pool_db)

    def create_pool_member(self, context, member, pool_id):
        subnet_db = self._core_plugin.get_subnet(context, member['subnet_id'])
        try:
            with context.session.begin(subtransactions=True):
                pool_dbs = self._get_resource(context, models.PoolV2, pool_id)
                if pool_dbs.subnet_id:
                    if pool_dbs.subnet_id!=subnet_db['id']:
                        raise loadbalancerv2.PoolMemberSubnetIDMismatch(
                            pool_subnet_id = pool_dbs.subnet_id,
                            member_subnet_id = subnet_db['id'])
                else:
                    if pool_dbs.network_id!=subnet_db['network_id']:
                        raise loadbalancerv2.PoolMemberNetworkIDMismatch(
                            pool_network_id = pool_dbs.network_id,
                            member_network_id = subnet_db['network_id'])

                if not self._resource_exists(context, models.PoolV2, pool_id):
                    raise loadbalancerv2.EntityNotFound(
                        name=models.PoolV2.NAME, id=pool_id)
                self._load_id_and_tenant_id(context, member)
                member['pool_id'] = pool_id
                member['status'] = constants.PENDING_CREATE
                member_db = models.MemberV2(**member)
                context.session.add(member_db)
        except exception.DBDuplicateEntry:
            raise loadbalancerv2.MemberExists(address=member['address'],
                                              port=member['protocol_port'],
                                              pool=pool_id)
        return data_models.Member.from_sqlalchemy_model(member_db)

    def update_pool_member(self, context, id, member, pool_id):
        with context.session.begin(subtransactions=True):
            if not self._resource_exists(context, models.PoolV2, pool_id):
                raise loadbalancerv2.MemberNotFoundForPool(pool_id=pool_id,
                                                           member_id=id)
            member_db = self._get_resource(context, models.MemberV2, id)
            member_db.update(member)
        context.session.refresh(member_db)
        return data_models.Member.from_sqlalchemy_model(member_db)

    def delete_pool_member(self, context, id, pool_id):
        with context.session.begin(subtransactions=True):
            if not self._resource_exists(context, models.PoolV2, pool_id):
                raise loadbalancerv2.MemberNotFoundForPool(pool_id=pool_id,
                                                           member_id=id)
            member_db = self._get_resource(context, models.MemberV2, id)
            context.session.delete(member_db)

    def get_pool_members(self, context, pool_id, filters=None):
        if filters:
            filters.update(filters)
        else:
            filters = {'pool_id': [pool_id]}
        member_dbs = self._get_resources(context, models.MemberV2,
                                         filters=filters)
        return [data_models.Member.from_sqlalchemy_model(member_db)
                for member_db in member_dbs]

    def get_pool_member(self, context, id, pool_id, filters=None):
        member_db = self._get_resource(context, models.MemberV2, id)
        if member_db.pool_id != pool_id:
            raise loadbalancerv2.MemberNotFoundForPool(member_id=id,
                                                       pool_id=pool_id)
        return data_models.Member.from_sqlalchemy_model(member_db)

    def delete_member(self, context, id):
        with context.session.begin(subtransactions=True):
            member_db = self._get_resource(context, models.MemberV2, id)
            context.session.delete(member_db)

    def get_member_status_info(self, context, id):
        member_db = self._get_resource(context, models.MemberV2, id)
        if member_db is not None:
            return member_db.status

    def update_loadbalancer_stats(self, context, loadbalancer_id, stats_data):
        stats_data = stats_data or {}
        with context.session.begin(subtransactions=True):
            lb_db = self._get_resource(context, models.LoadBalancer,
                                       loadbalancer_id)
            self.assert_modification_allowed(lb_db)
            lb_db.stats = self._create_loadbalancer_stats(context,
                                                          loadbalancer_id,
                                                          data=stats_data)

    def stats(self, context, loadbalancer_id):
        with context.session.begin(subtransactions=True):
            loadbalancer = self._get_resource(context, models.LoadBalancer,
                                              loadbalancer_id)
        return data_models.LoadBalancerStatistics.from_sqlalchemy_model(
            loadbalancer.stats)

    def create_l7policy(self, context, l7policy):
        if l7policy['redirect_pool_id'] == attributes.ATTR_NOT_SPECIFIED:
            l7policy['redirect_pool_id'] = None

        if l7policy['redirect_url'] == attributes.ATTR_NOT_SPECIFIED:
            l7policy['redirect_url'] = None
        if l7policy['redirect_url_code'] == attributes.ATTR_NOT_SPECIFIED:
            l7policy['redirect_url_code'] = None
        if l7policy['redirect_url_drop_query'] == attributes.ATTR_NOT_SPECIFIED:
            l7policy['redirect_url_drop_query'] = None

        if (l7policy['action'] != lb_const.L7_POLICY_ACTION_REDIRECT_TO_POOL
               and l7policy['redirect_pool_id'] is not None ):
            raise loadbalancerv2.L7PolicyActionNotCorresponding()
 
        if (l7policy['action'] != lb_const.L7_POLICY_ACTION_REDIRECT_TO_URL
               and (l7policy['redirect_url'] is not None or 
                    l7policy['redirect_url_code'] is not None or 
                    l7policy['redirect_url_drop_query'] is not None)):
            raise loadbalancerv2.L7PolicyActionNotCorresponding()
        
        if (l7policy['action'] == lb_const.L7_POLICY_ACTION_REDIRECT_TO_URL
            and ( (l7policy['redirect_url'] is None) or (l7policy['redirect_url_code'] is None)
                  or (l7policy['redirect_url_drop_query'] is None))):
            raise loadbalancerv2.L7PolicyRedirectUrlMissing()

        if l7policy['action'] == lb_const.L7_POLICY_ACTION_REDIRECT_TO_POOL:
            if not l7policy['redirect_pool_id']:
                raise loadbalancerv2.L7PolicyRedirectPoolIdMissing()
            if not self._resource_exists(
                context, models.PoolV2, l7policy['redirect_pool_id']):
                raise loadbalancerv2.EntityNotFound(
                    name=models.PoolV2.NAME, id=l7policy['redirect_pool_id'])
            pool_db = self._get_by_id(context, models.PoolV2, l7policy['redirect_pool_id'])

            if pool_db.listener:
                raise loadbalancerv2.EntityInUse(
                        entity_using=models.Listener.NAME,
                        id=pool_db.listener.id,
                        entity_in_use=models.PoolV2.NAME)

            if pool_db.l7policy:
                raise loadbalancerv2.EntityInUse(
                        entity_using=models.L7Policy.NAME,
                        id=pool_db.l7policy.id,
                        entity_in_use=models.PoolV2.NAME)
            if pool_db.protocol != lb_const.PROTOCOL_HTTP:
                raise loadbalancerv2.PoolProtocolMismatchForL7Policy()
 
        with context.session.begin(subtransactions=True):
            listener_id = l7policy.get('listener_id')
            listener_db = self._get_resource(
                context, models.Listener, listener_id)
            if not listener_db:
                raise loadbalancerv2.EntityNotFound(
                    name=models.Listener.NAME, id=listener_id)
            #Not allow user config l7policy for TCP
            if listener_db.protocol != lb_const.PROTOCOL_HTTP:
                raise loadbalancerv2.ListenerProtocolMismatchForL7Policy()

            if l7policy['action'] == lb_const.L7_POLICY_ACTION_REDIRECT_TO_POOL:
                if listener_db.loadbalancer.vip_network_id!=pool_db.network_id:
                    raise loadbalancerv2.LoadBalancerPoolNetworkMismatch()
                if listener_db.loadbalancer.vip_subnet_id!=pool_db.subnet_id:
                    raise loadbalancerv2.LoadBalancerPoolSubnetMismatch()

            self._load_id_and_tenant_id(context, l7policy)

            l7policy['status'] = constants.PENDING_CREATE
            if l7policy['position'] < 0:
                l7policy['position'] = sys.maxint

            l7policy['created_at'] = timeutils.utcnow()
            l7policy_db = models.L7Policy(**l7policy)
            listener_db.l7_policies.insert(l7policy['position'], l7policy_db)
        return data_models.L7Policy.from_sqlalchemy_model(l7policy_db)

    def update_l7policy(self, context, id, l7policy):
        l7policy['action'] = l7policy.get('action',attributes.ATTR_NOT_SPECIFIED)
        if l7policy['action'] == attributes.ATTR_NOT_SPECIFIED:
            l7policy_db = self._get_resource(context, models.L7Policy, id)
            l7policy['action'] = l7policy_db.action
        l7policy['redirect_pool_id'] = l7policy.get('redirect_pool_id',attributes.ATTR_NOT_SPECIFIED)
        l7policy['redirect_url'] = l7policy.get('redirect_url',attributes.ATTR_NOT_SPECIFIED)
        l7policy['redirect_url_code'] = l7policy.get('redirect_url_code',attributes.ATTR_NOT_SPECIFIED)
        l7policy['redirect_url_drop_query'] = l7policy.get('redirect_url_drop_query',attributes.ATTR_NOT_SPECIFIED)
        if l7policy['redirect_pool_id'] == attributes.ATTR_NOT_SPECIFIED:
            l7policy['redirect_pool_id'] = None
        if l7policy['redirect_url'] == attributes.ATTR_NOT_SPECIFIED:
            l7policy['redirect_url'] = None
        if l7policy['redirect_url_code'] == attributes.ATTR_NOT_SPECIFIED:
            l7policy['redirect_url_code'] = None
        if l7policy['redirect_url_drop_query'] == attributes.ATTR_NOT_SPECIFIED:
            l7policy['redirect_url_drop_query'] = None
 
        if (l7policy['action'] != lb_const.L7_POLICY_ACTION_REDIRECT_TO_URL
               and (l7policy['redirect_url'] is not None or 
                    l7policy['redirect_url_code'] is not None or 
                    l7policy['redirect_url_drop_query'] is not None)):
            LOG.debug('Not L7_POLICY_ACTION_REDIRECT_TO_URL L7PolicyActionNotCorresponding')
            raise loadbalancerv2.L7PolicyActionNotCorresponding()
       
        if (l7policy['action'] == lb_const.L7_POLICY_ACTION_REDIRECT_TO_URL
            and (l7policy['redirect_url'] is None or (l7policy['redirect_url_code'] is None)
                  or (l7policy['redirect_url_drop_query'] is None) )):
            LOG.debug('L7_POLICY_ACTION_REDIRECT_TO_URL L7PolicyRedirectUrlMissing')
            raise loadbalancerv2.L7PolicyRedirectUrlMissing()

        if (l7policy['action'] != lb_const.L7_POLICY_ACTION_REDIRECT_TO_POOL
                and l7policy['redirect_pool_id'] is not None):
            LOG.debug('not L7_POLICY_ACTION_REDIRECT_TO_POOL L7PolicyActionNotCorresponding')
            raise loadbalancerv2.L7PolicyActionNotCorresponding()

        if l7policy['action'] == lb_const.L7_POLICY_ACTION_REDIRECT_TO_POOL:
            if not l7policy['redirect_pool_id']:
                raise loadbalancerv2.L7PolicyRedirectPoolIdMissing()
            if not self._resource_exists(
                context, models.PoolV2, l7policy['redirect_pool_id']):
                raise loadbalancerv2.EntityNotFound(
                    name=models.PoolV2.NAME, id=l7policy['redirect_pool_id'])

            l7policy_db = self._get_resource(context, models.L7Policy, id)
            pool_db = self._get_by_id(context, models.PoolV2,
                          l7policy['redirect_pool_id'])

            if l7policy_db.listener.loadbalancer.vip_network_id!=pool_db.network_id:
                raise loadbalancerv2.LoadBalancerPoolNetworkMismatch()
            if l7policy_db.listener.loadbalancer.vip_subnet_id!=pool_db.subnet_id:
                raise loadbalancerv2.LoadBalancerPoolSubnetMismatch()

            if pool_db.listener:
                raise loadbalancerv2.EntityInUse(
                        entity_using=models.Listener.NAME,
                        id=pool_db.listener.id,
                        entity_in_use=models.PoolV2.NAME)

            if pool_db.l7policy:
                if pool_db.l7policy.id != id:
                    raise loadbalancerv2.EntityInUse(
                            entity_using=models.L7Policy.NAME,
                            id=pool_db.l7policy.id,
                            entity_in_use=models.PoolV2.NAME)
            if pool_db.protocol != lb_const.PROTOCOL_HTTP:
                raise loadbalancerv2.PoolProtocolMismatchForL7Policy()
        if (l7policy['action']== attributes.ATTR_NOT_SPECIFIED):
            l7policy.pop('action')
            l7policy.pop('redirect_pool_id')
            l7policy.pop('redirect_url')

        with context.session.begin(subtransactions=True):
            l7policy_db = self._get_resource(context, models.L7Policy, id)
            l7polcicy_position = l7policy.get('position',sys.maxint)
            if l7polcicy_position == sys.maxint:
                l7policy['position'] = l7policy_db.position
            if l7polcicy_position < 0:
                l7policy['position'] = sys.maxint

            listener_id = l7policy_db.listener_id
            listener_db = self._get_resource(
                context, models.Listener, listener_id)
            l7policy_db = listener_db.l7_policies.pop(l7policy_db.position)
            l7policy_db.update(l7policy)
            listener_db.l7_policies.insert(l7policy['position'], l7policy_db)

        context.session.refresh(l7policy_db)
        return data_models.L7Policy.from_sqlalchemy_model(l7policy_db)

    def delete_l7policy(self, context, id):
        with context.session.begin(subtransactions=True):
            l7policy_db = self._get_resource(context, models.L7Policy, id)
            listener_id = l7policy_db.listener_id
            listener_db = self._get_resource(
                context, models.Listener, listener_id)
            listener_db.l7_policies.remove(l7policy_db)

    def get_l7policy(self, context, id, fields=None):
        l7policy_db = self._get_resource(context, models.L7Policy, id)
        return data_models.L7Policy.from_sqlalchemy_model(l7policy_db)

    def get_l7policies(self, context, filters=None):
        l7policy_dbs = self._get_resources(context, models.L7Policy,
                                           filters=filters)
        return [data_models.L7Policy.from_sqlalchemy_model(l7policy_db)
                for l7policy_db in l7policy_dbs]

    def create_l7policy_rule(self, context, rule, l7policy_id):
        with context.session.begin(subtransactions=True):
            if not self._resource_exists(context, models.L7Policy,
                                         l7policy_id):
                raise loadbalancerv2.EntityNotFound(
                    name=models.L7Policy.NAME, id=l7policy_id)
            self._load_id_and_tenant_id(context, rule)
            rule['l7policy_id'] = l7policy_id
            rule['status'] = constants.PENDING_CREATE
            rule_db = models.L7Rule(**rule)
            context.session.add(rule_db)
        return data_models.L7Rule.from_sqlalchemy_model(rule_db)

    def update_l7policy_rule(self, context, id, rule, l7policy_id):
        with context.session.begin(subtransactions=True):
            if not self._resource_exists(context, models.L7Policy,
                                         l7policy_id):
                raise loadbalancerv2.RuleNotFoundForL7Policy(
                    l7policy_id=l7policy_id, rule_id=id)

            rule_db = self._get_resource(context, models.L7Rule, id)
            rule_db.update(rule)
        context.session.refresh(rule_db)
        return data_models.L7Rule.from_sqlalchemy_model(rule_db)

    def delete_l7policy_rule(self, context, id, l7policy_id):
        with context.session.begin(subtransactions=True):
            if not self._resource_exists(context, models.L7Policy,
                                         l7policy_id):
                raise loadbalancerv2.RuleNotFoundForL7Policy(
                    l7policy_id=l7policy_id, rule_id=id)
            rule_db_entry = self._get_resource(context, models.L7Rule, id)
            context.session.delete(rule_db_entry)

    def get_l7policy_rule(self, context, id, l7policy_id):
        rule_db = self._get_resource(context, models.L7Rule, id)
        if rule_db.l7policy_id != l7policy_id:
            raise loadbalancerv2.RuleNotFoundForL7Policy(
                l7policy_id=l7policy_id, rule_id=id)
        return data_models.L7Rule.from_sqlalchemy_model(rule_db)

    def get_l7policy_rules(self, context, l7policy_id, filters=None):
        if filters:
            filters.update(filters)
        else:
            filters = {'l7policy_id': [l7policy_id]}
        rule_dbs = self._get_resources(context, models.L7Rule,
                                       filters=filters)
        return [data_models.L7Rule.from_sqlalchemy_model(rule_db)
                for rule_db in rule_dbs]
