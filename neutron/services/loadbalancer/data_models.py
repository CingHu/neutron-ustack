# Copyright (c) 2014 OpenStack Foundation.
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

import sys

from sqlalchemy.ext import orderinglist
from sqlalchemy.orm import collections

from neutron.db.loadbalancer import models
from neutron.db import model_base
from neutron.db import models_v2
from neutron.db import servicetype_db
from neutron.openstack.common import timeutils
from neutron.api.v2 import attributes
from neutron.services.loadbalancer import constants

def get_class_from_sa_class(sa_class):
    current_module = sys.modules[__name__]
    for attr_name in current_module.__dict__:
        data_class = getattr(current_module, attr_name)
        if (hasattr(data_class, '_SA_MODEL') and
                data_class._SA_MODEL == sa_class):
            return data_class


class BaseDataModel(object):

    _SA_MODEL = None

    # NOTE(brandon-logan) This does not discover dicts for relationship
    # attributes.
    def to_dict(self):
        ret = {}
        for attr in self.__dict__:
            if (attr.startswith('_') or
                    isinstance(getattr(self, attr), BaseDataModel)):
                continue
            ret[attr] = self.__dict__[attr]
        return ret

    @classmethod
    def from_sqlalchemy_model(cls, sa_model, calling_class=None):
        instance = cls()
        # Automatically set instance vars to sa model column values
        for column in sa_model.__table__.columns:
            setattr(instance, column.name, getattr(sa_model, column.name))
        # Automaticaly set instance vars to sa model relationships
        attr_names = [attr_name for attr_name in dir(sa_model)
                      if not attr_name.startswith('_')]
        for attr_name in attr_names:
            attr = getattr(sa_model, attr_name)
            # Handles M:1 or 1:1 relationships
            if isinstance(attr, model_base.BASEV2):
                if hasattr(instance, attr_name):
                    data_class = get_class_from_sa_class(attr.__class__)
                    if calling_class != data_class and data_class:
                        setattr(instance, attr_name,
                                data_class.from_sqlalchemy_model(
                                    attr, calling_class=cls))
            # Handles 1:M or M:M relationships
            elif (isinstance(attr, collections.InstrumentedList) or
                 isinstance(attr, orderinglist.OrderingList)):
                for item in attr:
                    if hasattr(instance, attr_name):
                        data_class = get_class_from_sa_class(item.__class__)
                        attr_list = getattr(instance, attr_name) or []
                        attr_list.append(data_class.from_sqlalchemy_model(
                            item, calling_class=cls))
                        setattr(instance, attr_name, attr_list)
        return instance


# NOTE(brandon-logan) IPAllocation, Port, and ProviderResourceAssociation are
# defined here because there aren't any data_models defined in core neutron
# or neutron services.  Instead of jumping through the hoops to create those
# I've just defined them here.  If ever data_models or similar are defined
# in those packages, those should be used instead of these.
class IPAllocation(BaseDataModel):

    _SA_MODEL = models_v2.IPAllocation

    def __init__(self, port_id=None, ip_address=None, subnet_id=None,
                 network_id=None):
        self.port_id = port_id
        self.ip_address = ip_address
        self.subnet_id = subnet_id
        self.network_id = network_id


class Port(BaseDataModel):

    _SA_MODEL = models_v2.Port

    def __init__(self, id=None, tenant_id=None, name=None, network_id=None,
                 mac_address=None, admin_state_up=None, status=None,
                 device_id=None, device_owner=None, fixed_ips=None):
        self.id = id
        self.tenant_id = tenant_id
        self.name = name
        self.network_id = network_id
        self.mac_address = mac_address
        self.admin_state_up = admin_state_up
        self.status = status
        self.device_id = device_id
        self.device_owner = device_owner
        self.fixed_ips = fixed_ips or []


class ProviderResourceAssociation(BaseDataModel):

    _SA_MODEL = servicetype_db.ProviderResourceAssociation

    def __init__(self, provider_name=None, resource_id=None):
        self.provider_name = provider_name
        self.resource_id = resource_id


class SessionPersistence(BaseDataModel):

    _SA_MODEL = models.SessionPersistenceV2

    def __init__(self, pool_id=None, type=None, cookie_name=None,
                 pool=None):
        self.pool_id = pool_id
        self.type = type
        self.cookie_name = cookie_name
        self.pool = pool

    def to_dict(self):
        ret_dict = super(SessionPersistence, self).to_dict()
        #ret_dict.pop('pool_id', None)
        ret_dict.pop('pool', None)
        if self.type != constants.SESSION_PERSISTENCE_APP_COOKIE:
            ret_dict.pop('cookie_name', None)
        return ret_dict


class LoadBalancerStatistics(BaseDataModel):

    _SA_MODEL = models.LoadBalancerStatistics

    def __init__(self, loadbalancer_id=None, bytes_in=None, bytes_out=None,
                 active_connections=None, total_connections=None,
                 loadbalancer=None):
        self.loadbalancer_id = loadbalancer_id
        self.bytes_in = bytes_in
        self.bytes_out = bytes_out
        self.active_connections = active_connections
        self.total_connections = total_connections
        self.loadbalancer = loadbalancer

    def to_dict(self):
        ret = super(LoadBalancerStatistics, self).to_dict()
        ret.pop('loadbalancer_id', None)
        return ret


class HealthMonitor(BaseDataModel):

    _SA_MODEL = models.HealthMonitorV2

    def __init__(self, id=None, type=None, delay=None, timeout=None,
                 max_retries=None, http_method=None, url_path=None,
                 expected_codes=None):
        self.id = id
        self.type = type
        self.delay = delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.http_method = http_method
        self.url_path = url_path
        self.expected_codes = expected_codes

    def to_dict(self):
        ret_dict = super(HealthMonitor, self).to_dict()
        if self.type == constants.PROTOCOL_TCP:
            ret_dict.pop('http_method', None)
            ret_dict.pop('url_path', None)
            ret_dict.pop('expected_codes', None)
        return ret_dict


class Pool(BaseDataModel):

    _SA_MODEL = models.PoolV2

    def __init__(self, id=None, tenant_id=None,
                 created_at=None, name=None, description=None,
                 healthmonitor_id=None, protocol=None, lb_algorithm=None,
                 admin_state_up=None, status=None, members=None,
                 healthmonitor=None, session_persistence=None,
                 listener=None,l7policy=None,subnet_id=None, network_id=None):
        self.id = id
        self.tenant_id = tenant_id
        self.created_at = created_at
        self.name = name
        self.description = description
        self.healthmonitor_id = healthmonitor_id
        self.protocol = protocol
        self.lb_algorithm = lb_algorithm
        self.admin_state_up = admin_state_up
        self.status = status
        self.members = members or []
        self.healthmonitor = healthmonitor
        self.session_persistence = session_persistence
        self.listener = listener
        self.l7policy = l7policy
        self.subnet_id = subnet_id
        self.network_id = network_id

    def attached_to_loadbalancer(self):
        return bool((self.listener and self.listener.loadbalancer)
                     or self.l7policy)

    def to_dict(self):
        ret_dict = super(Pool, self).to_dict()
        if self.healthmonitor and self.healthmonitor!=None:
            ret_dict['healthmonitor'] = self.healthmonitor.to_dict()
        else:
            ret_dict.pop('healthmonitor', None)
        ret_dict['members'] = [member.to_dict() for member in self.members]
        if self.session_persistence:
            ret_dict['session_persistence'] = self.session_persistence.to_dict()
        else:
            ret_dict.pop('session_persistence', None)
        ret_dict['created_at'] = timeutils.strtime(ret_dict['created_at'])
        if not self.subnet_id:
            ret_dict.pop('subnet_id', None)

        ret_dict.pop('l7policy', None)

        return ret_dict


class Member(BaseDataModel):

    _SA_MODEL = models.MemberV2

    def __init__(self, id=None, tenant_id=None, pool_id=None, address=None,
                 protocol_port=None, weight=None, admin_state_up=None,
                 subnet_id=None, status=None, pool=None, instance_id=None):
        self.id = id
        self.tenant_id = tenant_id
        self.pool_id = pool_id
        self.address = address
        self.protocol_port = protocol_port
        self.weight = weight
        self.admin_state_up = admin_state_up
        self.subnet_id = subnet_id
        self.status = status
        self.pool = pool
        self.instance_id = instance_id

    def attached_to_loadbalancer(self):
        return bool(self.pool and self.pool.listener and
                    self.pool.listener.loadbalancer)


class L7Rule(BaseDataModel):

    _SA_MODEL = models.L7Rule

    def __init__(self, id=None, tenant_id=None,
                 l7policy_id=None, type=None, compare_type=None,
                 key=None, value=None, status=None,
                 admin_state_up=None, policy=None):
        self.id = id
        self.tenant_id = tenant_id
        self.l7policy_id = l7policy_id
        self.type = type
        self.compare_type = compare_type
        self.key = key
        self.value = value
        self.status = status
        self.admin_state_up = admin_state_up
        self.l7policy = policy

    def attached_to_loadbalancer(self):
        return bool(self.l7policy.listener.loadbalancer)

    def to_dict(self):
        ret_dict = super(L7Rule, self).to_dict()
        ret_dict.pop('value', None)
        ret_dict.pop('l7policy', None)
        return ret_dict



class L7Policy(BaseDataModel):

    _SA_MODEL = models.L7Policy

    def __init__(self, id=None, tenant_id=None,created_at=None,
                 name=None, description=None,
                 listener_id=None, action=None, redirect_pool_id=None,
                 redirect_pool=None, redirect_url=None,position=None, status=None,
                 admin_state_up=None, listener=None, rules=None, pool=None,
                 redirect_url_code=None,redirect_url_drop_query=None):
        self.id = id
        self.tenant_id = tenant_id
        self.created_at = created_at
        self.name = name
        self.description = description
        self.listener_id = listener_id
        self.action = action
        self.redirect_pool_id = redirect_pool_id
        self.redirect_url = redirect_url
        self.position = position
        self.status = status
        self.admin_state_up = admin_state_up
        self.listener = listener
        self.rules = rules or []
        self.redirect_pool = redirect_pool
        self.redirect_url_code = redirect_url_code
        self.redirect_url_drop_query = redirect_url_drop_query
    def attached_to_loadbalancer(self):
        return bool(self.listener.loadbalancer)

    def attached_to_pool(self):
        return bool(self.redirect_pool)

    def to_dict(self):
        ret_dict = super(L7Policy, self).to_dict()
        ret_dict['rules'] = [rule.to_dict() for rule in self.rules]
        if self.redirect_pool and (self.redirect_pool.listener is None):
            ret_dict['pool'] = self.redirect_pool.to_dict()
        else:
            ret_dict.pop('pool', None)
        if self.action == constants.L7_POLICY_ACTION_REJECT:
            ret_dict.pop('redirect_pool', None)
            ret_dict.pop('redirect_pool_id', None)
            ret_dict.pop('redirect_url', None)
            ret_dict.pop('redirect_url_code', None)
            ret_dict.pop('redirect_url_drop_query', None)
        if self.action == constants.L7_POLICY_ACTION_REDIRECT_TO_URL:
            ret_dict.pop('redirect_pool', None)
            ret_dict.pop('redirect_pool_id', None)
        if self.action == constants.L7_POLICY_ACTION_REDIRECT_TO_POOL:
            ret_dict.pop('redirect_url', None)
            ret_dict.pop('redirect_url_code', None)
            ret_dict.pop('redirect_url_drop_query', None)

        ret_dict['created_at'] = timeutils.strtime(ret_dict['created_at'])
        return ret_dict


class Listener(BaseDataModel):

    _SA_MODEL = models.Listener

    def __init__(self, id=None, tenant_id=None,
                 created_at=None, name=None, description=None,
                 default_pool_id=None, loadbalancer_id=None, protocol=None,
                 default_tls_container=None,sni_containers = None,
                 protocol_port=None, connection_limit=None, keep_alive=None,
                 admin_state_up=None, status=None, default_pool=None,
                 loadbalancer=None, l7_policies=None,default_tls_container_id=None,sni_container_ids=None):
        self.id = id
        self.tenant_id = tenant_id
        self.created_at = created_at
        self.name = name
        self.description = description
        self.default_pool_id = default_pool_id
        self.loadbalancer_id = loadbalancer_id
        self.protocol = protocol
        self.default_tls_container_id = default_tls_container_id
        self.default_tls_container = default_tls_container
        self.sni_containers = sni_containers or []
        self.protocol_port = protocol_port
        self.connection_limit = connection_limit
        self.admin_state_up = admin_state_up
        self.keep_alive = keep_alive
        self.status = status
        self.default_pool = default_pool
        self.loadbalancer = loadbalancer
        self.l7_policies = l7_policies or []
        self.sni_container_ids = sni_container_ids or []

    def attached_to_loadbalancer(self):
        return bool(self.loadbalancer)

    def to_dict(self):
        ret_dict = super(Listener, self).to_dict()
        if self.default_pool :
           ret_dict['default_pool'] = self.default_pool.to_dict()
        ret_dict['l7policy_ids'] = []
        ret_dict['l7_policies'] = []
        for l7_policy in self.l7_policies:
            ret_dict['l7policy_ids'].append(l7_policy.id)
            ret_dict['l7_policies'].append(l7_policy.to_dict())
        #ret_dict['sni_container_ids'] = [container.tls_container_id
        #    for container in self.sni_containers]
        #if self.default_tls_container:
        #    ret_dict['default_tls_container_id'] = self.default_tls_container.tls_container_id
        #else:
        #    ret_dict['default_tls_container_id'] = None
        #ret_dict['sni_containers'] = []
        #for sni in self.sni_containers:
        #    ret_dict['sni_containers'].append(sni.to_dict())
        ret_dict['created_at'] = timeutils.strtime(ret_dict['created_at'])
        return ret_dict


class TLSCertificate(BaseDataModel):
    _SA_MODEL = models.TLSCertificate
    def __init__(self, id=None, tenant_id=None, name=None, description=None,
                 certificate_contenet=None, private_key=None,
                 status=None,listener=None):
        self.id = id
        self.tenant_id = tenant_id
        self.name = name
        self.description = description
        self.certificate_contenet = certificate_contenet
        self.private_key = private_key
        self.status = status

    def to_dict(self):
        ret_dict = super(TLSCertificate, self).to_dict()
        return ret_dict


class LoadBalancer(BaseDataModel):

    _SA_MODEL = models.LoadBalancer

    def __init__(self, id=None, tenant_id=None,
                 created_at=None, name=None, description=None,
                 vip_subnet_id=None,vip_network_id=None,
                 vip_port_id=None, vip_address=None,
                 status=None, admin_state_up=None, vip_port=None,
                 stats=None, provider=None, listeners=None,
                 securitygroup_id=None):
        self.id = id
        self.tenant_id = tenant_id
        self.created_at = created_at
        self.name = name
        self.description = description
        self.vip_network_id = vip_network_id
        self.vip_subnet_id = vip_subnet_id
        self.vip_port_id = vip_port_id
        self.vip_address = vip_address
        self.status = status
        self.admin_state_up = admin_state_up
        self.vip_port = vip_port
        self.stats = stats
        self.provider = provider
        self.listeners = listeners or []
        self.securitygroup_id = securitygroup_id
    def to_dict(self):
        ret_dict = super(LoadBalancer, self).to_dict()
        if self.provider:
            ret_dict['provider'] = self.provider.to_dict()
        else:
            ret_dict['provider'] = None
        ret_dict['listeners'] = [listener.to_dict()
            for listener in self.listeners]
        ret_dict['listener_ids'] = [listener.id
            for listener in self.listeners]
        ret_dict['created_at'] = timeutils.strtime(ret_dict['created_at'])
        if not self.vip_subnet_id:
            ret_dict.pop('vip_subnet_id', None)
        return ret_dict
