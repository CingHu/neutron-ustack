# Copyright 2014 OpenStack Foundation.
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
import sys

from oslo.config import cfg
import six

from neutron.api import extensions
from neutron.api.v2 import attributes as attr
from neutron.api.v2 import base
from neutron.api.v2 import resource_helper
from neutron.common import exceptions as qexception
from neutron import manager
from neutron.plugins.common import constants
from neutron.services.loadbalancer import constants as lb_const
from neutron.services import service_base


# Loadbalancer Exceptions
# This exception is only for a workaround when having v1 and v2 lbaas extension
# and plugins enabled
class RequiredAttributeNotSpecified(qexception.BadRequest):
    message = _("Required attribute %(attr_name)s not specified")


class EntityNotFound(qexception.NotFound):
    message = _("%(name)s %(id)s could not be found")


class DelayOrTimeoutInvalid(qexception.BadRequest):
    message = _("Delay must be greater than or equal to timeout")


class EntityInUse(qexception.InUse):
    message = _("%(entity_using)s %(id)s is using this %(entity_in_use)s.")

class CertificateInvalid(qexception.BadRequest):
    message = _("the certificate and private-key must be specified at same time when create/update")

class LoadBalancerListenerProtocolPortExists(qexception.Conflict):
    message = _("Loadbalancer %(lb_id)s already has a listener with "
                "protocol port of %(protocol_port)s.")


class PoolProtocolMismatchForL7Policy(qexception.Conflict):
    message = _("Only the pool of which the protocol is HTTP "
                "can be used for l7policy redirect pool.")

class ListenerProtocolMismatchForL7Policy(qexception.Conflict):
    message = _("Only the listener of which the protocol is HTTP "
                "can be used for l7policy.")

class ListenerPoolProtocolMismatch(qexception.Conflict):
    message = _("Listener protocol %(listener_proto)s and pool protocol "
                "%(pool_proto)s are not compatible.")


class AttributeIDImmutable(qexception.NeutronException):
    message = _("Cannot change %(attribute)s if one already exists")


class UpdateSecurityGroupFailed(qexception.NeutronException):
    message = _("Failed to update the security group for loadbalancer %(id)s.")

class SecurityGroupNotFound(qexception.NotFound):
    message = _("Failed to find the security group for loadbalancer %(id)s.")

class StateInvalid(qexception.NeutronException):
    message = _("Invalid state %(state)s of loadbalancer resource %(id)s")


class MemberNotFoundForPool(qexception.NotFound):
    message = _("Member %(member_id)s could not be found in pool %(pool_id)s")


class MemberExists(qexception.Conflict):
    message = _("Member with address %(address)s and protocol port %(port)s "
                "already presents in pool %(pool)s.")


class MemberAddressTypeSubnetTypeMismatch(qexception.NeutronException):
    message = _("Member with address %(address)s and subnet %(subnet_id) "
                " have mismatched IP versions")


class DriverError(qexception.NeutronException):
    message = _("An error happened in the driver")


class LBConfigurationUnsupported(qexception.NeutronException):
    message = _("Load balancer %(load_balancer_id)s configuration is not"
                "supported by driver %(driver_name)s")


class L7PolicyRedirectPoolIdMissing(qexception.Conflict):
    message = _("Redirect pool id is missing for L7 Policy with"
                " pool redirect action")


class L7PolicyRedirectUrlMissing(qexception.Conflict):
    message = _("Redirect URL/URL_CODE/URL_DROP_QUERY flag is missing for L7 Policy with"
                " URL redirect action")

class L7PolicyActionNotCorresponding(qexception.Conflict):
    message = _("redirect_pool_id should only used for REDIRECT_TO_POOL action,"
                "and redirect_url/redirect_url_code/redirect_url_drop_query"
                " should only used for REDIRECT_TO_URL action")

class RuleNotFoundForL7Policy(qexception.NotFound):
    message = _("Rule %(rule_id)s could not be found in"
                " l7 policy %(l7policy_id)s")

class NetworkSubnetIDMismatch(qexception.Conflict):
    message = _("Specified the subnet %(subnet_id)s not in specified network %(network_id)s.")

class PoolMemberSubnetIDMismatch(qexception.Conflict):
    message = _("Specified the member subnet %(member_subnet_id)s not same with pool subnet %(pool_subnet_id)s.")

class PoolMemberNetworkIDMismatch(qexception.Conflict):
    message = _("Specified the member network %(member_network_id)s not same with pool network %(pool_network_id)s.")

class LoadBalancerPoolSubnetMismatch(qexception.Conflict):
    message = _("Specified the loadbalancer subnet not same with pool.")

class LoadBalancerPoolNetworkMismatch(qexception.Conflict):
    message = _("Specified the loadbalancer network not same with pool.")

class OneListenerAdminStateUpAtLeast(qexception.Conflict):
    message = _("Loadbalancer [%(lb_id)s] must have one enabled listener at least.")

class TLSDefaultContainerNotSpecified(qexception.BadRequest):
    message = _("Default TLS container must be specified for TERMINATED_HTTPS mode.")

class TLSDefaultContainerSpecified(qexception.BadRequest):
    message = _("Default TLS container/SNI container ids must only be specified for TERMINATED_HTTPS mode.")

class TLSContainerNotFound(qexception.NotFound):
    message = _("TLS container %(container_id)s could not be found")


class TLSContainerInvalid(qexception.NeutronException):
    message = _("TLS container %(container_id)s is invalid. %(reason)s")

RESOURCE_ATTRIBUTE_MAP = {
    'loadbalancers': {
        'id': {'allow_post': False, 'allow_put': False,
               'validate': {'type:uuid': None},
               'is_visible': True,
               'primary_key': True},
        'name': {'allow_post': True, 'allow_put': True,
                 'validate': {'type:string': None},
                 'default': '',
                 'is_visible': True},
        'tenant_id': {'allow_post': True, 'allow_put': False,
                      'validate': {'type:string': None},
                      'required_by_policy': True,
                      'is_visible': True},
        'description': {'allow_post': True, 'allow_put': True,
                        'validate': {'type:string': None},
                        'is_visible': True, 'default': ''},
        'vip_subnet_id': {'allow_post': True, 'allow_put': False,
                          'validate': {'type:uuid_or_none': None},
                          'default': None,
                          'is_visible': True},
        'vip_network_id': {'allow_post': True, 'allow_put': False,
                          'validate': {'type:uuid': None},
                          'is_visible': True},
        'vip_port_id': {'allow_post': False, 'allow_put': False,
                          'is_visible': True},
        'vip_address': {'allow_post': True, 'allow_put': False,
                        'default': attr.ATTR_NOT_SPECIFIED,
                        'validate': {'type:ip_address_or_none': None},
                        'is_visible': True},
        'securitygroup_id': {'allow_post': True, 'allow_put': True,
                        'validate': {'type:uuid': None},
                        'default': None,
                        'is_visible': True},
        'admin_state_up': {'allow_post': False, 'allow_put': False,
                           'default': True,
                           'convert_to': attr.convert_to_boolean,
                           'is_visible': True},
        'status': {'allow_post': False, 'allow_put': False,
                   'is_visible': True},
        'created_at': {'allow_post': False, 'allow_put': False,
                   'is_visible': True},
        'listener_ids': {'allow_post': False, 'allow_put': False,
                   'is_visible': True}
    },
    'listeners': {
        'id': {'allow_post': False, 'allow_put': False,
               'validate': {'type:uuid': None},
               'is_visible': True,
               'primary_key': True},
        'tenant_id': {'allow_post': True, 'allow_put': False,
                      'validate': {'type:string': None},
                      'required_by_policy': True,
                      'is_visible': True},
        'name': {'allow_post': True, 'allow_put': True,
                 'validate': {'type:string': None},
                 'default': '',
                 'is_visible': True},
        'description': {'allow_post': True, 'allow_put': True,
                        'validate': {'type:string': None},
                        'is_visible': True, 'default': ''},
        'loadbalancer_id': {'allow_post': True, 'allow_put': False,
                            'validate': {'type:uuid': None},
                            'is_visible': True},
        'protocol': {'allow_post': True, 'allow_put': False,
                     'validate': {'type:values':
                         lb_const.LISTENER_SUPPORTED_PROTOCOLS},
                     'is_visible': True},
        'protocol_port': {'allow_post': True, 'allow_put': False,
                          'validate': {'type:range': [1, 65535]},
                          'convert_to': attr.convert_to_int,
                          'is_visible': True},

        'default_pool_id': {'allow_post': True, 'allow_put': True,
                            'validate': {'type:uuid_or_none': None},
                            'default': attr.ATTR_NOT_SPECIFIED,
                            'is_visible': True},
        'connection_limit': {'allow_post': True, 'allow_put': True,
                             'default': 5000,
                             'validate':{'type:values':[5000, 10000, 20000, 40000]},
                             'convert_to': attr.convert_to_int,
                             'is_visible': True},
        'keep_alive': {'allow_post': True, 'allow_put': False,
                           'default': False,
                           'convert_to': attr.convert_to_boolean,
                           'is_visible': True},
        'admin_state_up': {'allow_post': True, 'allow_put': True,
                           'default': True,
                           'convert_to': attr.convert_to_boolean,
                           'is_visible': True},
        'status': {'allow_post': False, 'allow_put': False,
                   'is_visible': True},
        'l7policy_ids': {'allow_post': False, 'allow_put': False,
                   'is_visible': True},
        'created_at': {'allow_post': False, 'allow_put': False,
                   'is_visible': True}
    },
    'pools': {
        'id': {'allow_post': False, 'allow_put': False,
               'validate': {'type:uuid': None},
               'is_visible': True,
               'primary_key': True},
        'tenant_id': {'allow_post': True, 'allow_put': False,
                      'validate': {'type:string': None},
                      'required_by_policy': True,
                      'is_visible': True},
        'name': {'allow_post': True, 'allow_put': True,
                 'validate': {'type:string': None},
                 'is_visible': True, 'default': ''},
        'description': {'allow_post': True, 'allow_put': True,
                        'validate': {'type:string': None},
                        'is_visible': True, 'default': ''},
        'healthmonitor': {
            'allow_post': True, 'allow_put': True,
            'convert_to': attr.convert_none_to_empty_dict,
            'default': {},
            'validate': {
                'type:dict_or_empty': {
                    'type': {
                        'type:values': lb_const.SUPPORTED_HEALTH_MONITOR_TYPES,
                        'required': True
                    },
                    'delay': {'type:range': [2, 60],
                              'convert_to': attr.convert_to_int,
                              'required': True
                    },
                    'timeout': {
                        'type:range': [5, 300],
                        'convert_to': attr.convert_to_int,
                        'required': True
                    },
                    'max_retries': {
                            'type:range': [1, 10],
                            'convert_to': attr.convert_to_int,
                            'required': True
                    },
                    'http_method': {
                            'type:values': lb_const.SUPPORTED_HTTP_CHECK_METHOD,
                            'default': 'GET',
                            'required': False
                    },
                    'url_path': {
                         'type:ascii_string': None,
                         'default': '/',
                         'required': False
                    },
                    'expected_codes': {
                         'type:regex': '^(\d{3}(\s*,\s*\d{3})*)$|^(\d{3}-\d{3})$',
                         'default': '200',
                         'required': False
                      }
                    }},
                'is_visible': True},
        'protocol': {'allow_post': True, 'allow_put': False,
                     'validate': {
                         'type:values': lb_const.POOL_SUPPORTED_PROTOCOLS},
                     'is_visible': True},
        'lb_algorithm': {'allow_post': True, 'allow_put': True,
                         'validate': {
                             'type:values': lb_const.SUPPORTED_LB_ALGORITHMS},
                         'is_visible': True},
        'session_persistence': {
            'allow_post': True, 'allow_put': True,
            'convert_to': attr.convert_none_to_empty_dict,
            'default': {},
            'validate': {
                'type:dict_or_empty': {
                    'type': {
                        'type:values': lb_const.SUPPORTED_SP_TYPES,
                        'required': True},
                    'cookie_name': {'type:ascii_string': None,
                                    'required': False}}},
            'is_visible': True},
        'members': {'allow_post': False, 'allow_put': False,
                    'is_visible': True},
        'admin_state_up': {'allow_post': True, 'allow_put': True,
                           'default': True,
                           'convert_to': attr.convert_to_boolean,
                           'is_visible': True},
        'status': {'allow_post': False, 'allow_put': False,
                   'is_visible': True},
        'created_at': {'allow_post': False, 'allow_put': False,
                   'is_visible': True},
        'subnet_id': {'allow_post': True, 'allow_put': False,
                          'validate': {'type:uuid_or_none': None},
                          'default' : None,
                          'is_visible': True},
        'network_id': {'allow_post': True, 'allow_put': False,
                          'validate': {'type:uuid': None},
                          'is_visible': True},
    },
    'l7policies': {
        'id': {'allow_post': False, 'allow_put': False,
               'validate': {'type:uuid': None},
               'is_visible': True,
               'primary_key': True},
        'tenant_id': {'allow_post': True, 'allow_put': False,
                      'validate': {'type:string': None},
                      'required_by_policy': True,
                      'is_visible': True},
        'name': {'allow_post': True, 'allow_put': True,
                 'validate': {'type:string': None},
                 'default': '',
                 'is_visible': True},
        'description': {'allow_post': True, 'allow_put': True,
                        'validate': {'type:string': None},
                        'is_visible': True, 'default': ''},
        'listener_id': {'allow_post': True, 'allow_put': False,
                        'validate': {'type:uuid': None},
                        'is_visible': True},
        'action': {'allow_post': True, 'allow_put': True,
                   'validate': { 'type:values': lb_const.SUPPORTED_L7_POLICY_ACTIONS},
                   'is_visible': True},
        'redirect_pool_id': {'allow_post': True, 'allow_put': True,
                             'validate': {'type:uuid_or_none': None},
                             'default': attr.ATTR_NOT_SPECIFIED,
                             'is_visible': True},
        'redirect_url': {'allow_post': True, 'allow_put': True,
                         'validate': {'type:ascii_string_or_none': None},
                         'default': attr.ATTR_NOT_SPECIFIED,
                         'is_visible': True},
        'redirect_url_code': {'allow_post': True, 'allow_put': True,
                         'validate': {'type:values': [301,302,303,307,308]},
                         'convert_to': attr.convert_to_int,
                         'default': attr.ATTR_NOT_SPECIFIED,
                         'is_visible': True},
        'redirect_url_drop_query': {'allow_post': True, 'allow_put': True,
                         'convert_to': attr.convert_to_boolean,
                         'default': attr.ATTR_NOT_SPECIFIED,
                         'is_visible': True},
        'position': {'allow_post': True, 'allow_put': True,
                     'convert_to': attr.convert_to_int,
                     'default': sys.maxint,
                     'is_visible': True},
        'rules': {'allow_post': False, 'allow_put': False,
                  'is_visible': True},
        'admin_state_up': {'allow_post': True, 'allow_put': True,
                           'default': True,
                           'convert_to': attr.convert_to_boolean,
                           'is_visible': True},
        'status': {'allow_post': False, 'allow_put': False,
                   'is_visible': True},
        'created_at': {'allow_post': False, 'allow_put': False,
                   'is_visible': True}
    }
}

SUB_RESOURCE_ATTRIBUTE_MAP = {
     'lbaas_listeners': {
        'parent': {'collection_name': 'loadbalancers',
                   'member_name': 'loadbalancer'},
        'parameters': {
                  'id': {'allow_post': False, 'allow_put': False,
                        'validate': {'type:uuid': None},
                        'is_visible': True,
                        'primary_key': True},
                  'tenant_id': {'allow_post': False, 'allow_put': False,
                         'validate': {'type:string': None},
                         'required_by_policy': True,
                         'is_visible': True},
                 'name': {'allow_post': False, 'allow_put': False,
                          'validate': {'type:string': None},
                          'default': '',
                          'is_visible': True},
                'description': {'allow_post': False, 'allow_put': False,
                                'validate': {'type:string': None},
                                'is_visible': True, 'default': ''},
                'loadbalancer_id': {'allow_post': False, 'allow_put': False,
                            'validate': {'type:uuid': None},
                            'is_visible': True},
                'default_pool_id': {'allow_post': False, 'allow_put': False,
                            'validate': {'type:uuid_or_none': None},
                            'default': attr.ATTR_NOT_SPECIFIED,
                            'is_visible': True},
                'connection_limit': {'allow_post': False, 'allow_put': False,
                             'default': 5000,
                             'convert_to': attr.convert_to_int,
                             'is_visible': True},
                'protocol': {'allow_post': False, 'allow_put': False,
                             'validate': {'type:values':
                                 lb_const.LISTENER_SUPPORTED_PROTOCOLS},
                             'is_visible': True},
                'protocol_port': {'allow_post': False, 'allow_put': False,
                                  'validate': {'type:range': [0, 65535]},
                                  'convert_to': attr.convert_to_int,
                                  'is_visible': True},
                'keep_alive': {'allow_post': True, 'allow_put': False,
                           'default': False,
                           'convert_to': attr.convert_to_boolean,
                           'is_visible': True},
                'admin_state_up': {'allow_post': False, 'allow_put': False,
                                   'default': True,
                                   'convert_to': attr.convert_to_boolean,
                                   'is_visible': True},
                'status': {'allow_post': False, 'allow_put': False,
                           'is_visible': True},
                'l7policy_ids': {'allow_post': False, 'allow_put': False,
                           'is_visible': True},
                'created_at': {'allow_post': False, 'allow_put': False,
                           'is_visible': True}
        }
    },
    'lbaas_l7policies': {
        'parent': {'collection_name': 'listeners',
                   'member_name': 'listener'},
        'parameters': {
            'id': {'allow_post': False, 'allow_put': False,
                   'validate': {'type:uuid': None},
                   'is_visible': True,
                   'primary_key': True},
            'tenant_id': {'allow_post': False, 'allow_put': False,
                      'validate': {'type:string': None},
                      'required_by_policy': True,
                      'is_visible': True},
            'name': {'allow_post': False, 'allow_put': False,
                     'validate': {'type:string': None},
                    'default': '',
                    'is_visible': True},
            'description': {'allow_post': False, 'allow_put': False,
                        'validate': {'type:string': None},
                        'is_visible': True, 'default': ''},
            'listener_id': {'allow_post': False, 'allow_put': False,
                        'validate': {'type:uuid': None},
                        'is_visible': True},
            'action': {'allow_post': False, 'allow_put': False,
                      'validate': {
                       'type:values': lb_const.SUPPORTED_L7_POLICY_ACTIONS},
                   'is_visible': True},
            'redirect_pool_id': {'allow_post': False, 'allow_put': False,
                             'validate': {'type:uuid_or_none': None},
                             'default': attr.ATTR_NOT_SPECIFIED,
                             'is_visible': True},
            'position': {'allow_post': False, 'allow_put': False,
                        'convert_to': attr.convert_to_int,
                        'default': sys.maxint,
                        'is_visible': True},
            'rules': {'allow_post': False, 'allow_put': False,
                      'is_visible': True},
            'admin_state_up': {'allow_post': False, 'allow_put': False,
                              'default': True,
                              'convert_to': attr.convert_to_boolean,
                              'is_visible': True},
            'status': {'allow_post': False, 'allow_put': False,
                       'is_visible': True},
            'created_at': {'allow_post': False, 'allow_put': False,
                       'is_visible': True}
        }
    },
    'members': {
        'parent': {'collection_name': 'pools',
                   'member_name': 'pool'},
        'parameters': {
            'id': {'allow_post': False, 'allow_put': False,
                   'validate': {'type:uuid': None},
                   'is_visible': True,
                   'primary_key': True},
            'tenant_id': {'allow_post': True, 'allow_put': False,
                          'validate': {'type:string': None},
                          'required_by_policy': True,
                          'is_visible': True},
            'address': {'allow_post': True, 'allow_put': False,
                        'validate': {'type:ip_address': None},
                        'is_visible': True},
            'protocol_port': {'allow_post': True, 'allow_put': False,
                              'validate': {'type:range': [0, 65535]},
                              'convert_to': attr.convert_to_int,
                              'is_visible': True},
            'weight': {'allow_post': True, 'allow_put': True,
                       'default': 1,
                       'validate': {'type:range': [1, 100]},
                       'convert_to': attr.convert_to_int,
                       'is_visible': True},
            'admin_state_up': {'allow_post': True, 'allow_put': True,
                               'default': True,
                               'convert_to': attr.convert_to_boolean,
                               'is_visible': True},
            'status': {'allow_post': False, 'allow_put': False,
                       'is_visible': True},
            'subnet_id': {'allow_post': True, 'allow_put': False,
                          'validate': {'type:uuid': None},
                          'is_visible': True},
            'instance_id': {'allow_post': True, 'allow_put': False,
                          'validate': {'type:uuid': None},
                          'is_visible': True}
        }
    },
    'stats': {
        'parent': {'collection_name': 'loadbalancers',
                   'member_name': 'loadbalancer'},
        'parameters': {
            'id': {'allow_post': False, 'allow_put': False,
                   'validate': {'type:uuid': None},
                   'is_visible': True,
                   'primary_key': True},
        }
    },
    'rules': {
        'parent': {'collection_name': 'l7policies',
                   'member_name': 'l7policy'},
        'parameters': {
            'id': {'allow_post': False, 'allow_put': False,
                   'validate': {'type:uuid': None},
                   'is_visible': True,
                   'primary_key': True},
            'tenant_id': {'allow_post': True, 'allow_put': False,
                          'validate': {'type:string': None},
                          'required_by_policy': False,
                          'is_visible': True},
            'type': {'allow_post': True, 'allow_put': True,
                     'validate': {
                         'type:values': lb_const.SUPPORTED_L7_RULE_TYPES},
                     'is_visible': True},
            'compare_type': {'allow_post': True, 'allow_put': True,
                             'validate': {
                                 'type:values':
                                 lb_const.SUPPORTED_L7_RULE_COMPARE_TYPES},
                             'is_visible': True},
            'key': {'allow_post': True, 'allow_put': True,
                    'validate': {'type:ascii_string_regex': '^[^\*]((?!\*{2,}).)*$'},
                    'is_visible': True},
            #'value': {'allow_post': True, 'allow_put': True,
            #          'validate': {'type:string': None},
            #          'default': '',
            #          'is_visible': True},
            'admin_state_up': {'allow_post': True, 'allow_put': True,
                               'default': True,
                               'convert_to': attr.convert_to_boolean,
                               'is_visible': True},
            'status': {'allow_post': False, 'allow_put': False,
                       'is_visible': True}
        }
    }
}


lbaasv2_quota_opts = [
    cfg.IntOpt('quota_loadbalancer',
               default=3,
               help=_('Number of LoadBalancers allowed per tenant. '
                      'A negative value means unlimited.')),
    cfg.IntOpt('quota_listener',
               default=6,
               help=_('Number of Loadbalancer Listeners allowed per tenant. '
                      'A negative value means unlimited.')),
    cfg.IntOpt('quota_pool',
               default=6,
               help=_('Number of pools allowed per tenant. '
                      'A negative value means unlimited.')),
    cfg.IntOpt('quota_member',
               default=-1,
               help=_('Number of pool members allowed per tenant. '
                      'A negative value means unlimited.'))
]
cfg.CONF.register_opts(lbaasv2_quota_opts, 'QUOTAS')


class Loadbalancerv2(extensions.ExtensionDescriptor):

    @classmethod
    def get_name(cls):
        return "LoadBalancing service v2"

    @classmethod
    def get_alias(cls):
        return "lbaasv2"

    @classmethod
    def get_description(cls):
        return "Extension for LoadBalancing service v2"

    @classmethod
    def get_namespace(cls):
        return "http://wiki.openstack.org/neutron/LBaaS/API_2.0"

    @classmethod
    def get_updated(cls):
        return "2014-06-18T10:00:00-00:00"

    @classmethod
    def get_resources(cls):
        special_mappings = {'l7policies': 'l7policy'}
        plural_mappings = resource_helper.build_plural_mappings(
            special_mappings,RESOURCE_ATTRIBUTE_MAP)
        action_map = {'loadbalancer': {'stats': 'GET'}}
        plural_mappings['members'] = 'member'
        plural_mappings['rules'] = 'rule'
        attr.PLURALS.update(plural_mappings)
        resources = resource_helper.build_resource_info(
            plural_mappings,
            RESOURCE_ATTRIBUTE_MAP,
            constants.LOADBALANCERV2,
            action_map=action_map,
            register_quota=True)
        plugin = manager.NeutronManager.get_service_plugins()[
            constants.LOADBALANCERV2]
        for collection_name in SUB_RESOURCE_ATTRIBUTE_MAP:
            # Special handling needed for sub-resources with 'y' ending
            # (e.g. proxies -> proxy)
            resource_name = special_mappings.get(collection_name,
                                                 collection_name[:-1])
            parent = SUB_RESOURCE_ATTRIBUTE_MAP[collection_name].get('parent')
            params = SUB_RESOURCE_ATTRIBUTE_MAP[collection_name].get(
                'parameters')

            controller = base.create_resource(collection_name, resource_name,
                                              plugin, params,
                                              allow_bulk=True,
                                              parent=parent,
                                              allow_pagination=True,
                                              allow_sorting=True)

            resource = extensions.ResourceExtension(
                collection_name,
                controller, parent,
                path_prefix=constants.COMMON_PREFIXES[
                    constants.LOADBALANCERV2],
                attr_map=params)
            resources.append(resource)

        return resources

    @classmethod
    def get_plugin_interface(cls):
        return LoadBalancerPluginBaseV2

    def update_attributes_map(self, attributes, extension_attrs_map=None):
        super(Loadbalancerv2, self).update_attributes_map(
            attributes, extension_attrs_map=RESOURCE_ATTRIBUTE_MAP)

    def get_extended_resources(self, version):
        if version == "2.0":
            return RESOURCE_ATTRIBUTE_MAP
        else:
            return {}


@six.add_metaclass(abc.ABCMeta)
class LoadBalancerPluginBaseV2(service_base.ServicePluginBase):

    def get_plugin_name(self):
        return constants.LOADBALANCERV2

    def get_plugin_type(self):
        return constants.LOADBALANCERV2

    def get_plugin_description(self):
        return 'LoadBalancer service plugin v2'

    @abc.abstractmethod
    def get_loadbalancers(self, context, filters=None, fields=None):
        pass

    @abc.abstractmethod
    def get_loadbalancer(self, context, id, fields=None):
        pass

    @abc.abstractmethod
    def create_loadbalancer(self, context, loadbalancer):
        pass

    @abc.abstractmethod
    def update_loadbalancer(self, context, id, loadbalancer):
        pass

    @abc.abstractmethod
    def delete_loadbalancer(self, context, id):
        pass

    @abc.abstractmethod
    def create_listener(self, context, listener):
        pass

    @abc.abstractmethod
    def get_listener(self, context, id, fields=None):
        pass

    @abc.abstractmethod
    def get_listeners(self, context, filters=None, fields=None):
        pass

    @abc.abstractmethod
    def update_listener(self, context, id, listener):
        pass

    @abc.abstractmethod
    def delete_listener(self, context, id):
        pass

    @abc.abstractmethod
    def get_pools(self, context, filters=None, fields=None):
        pass

    @abc.abstractmethod
    def get_pool(self, context, id, fields=None):
        pass

    @abc.abstractmethod
    def create_pool(self, context, pool):
        pass

    @abc.abstractmethod
    def update_pool(self, context, id, pool):
        pass

    @abc.abstractmethod
    def delete_pool(self, context, id):
        pass

    @abc.abstractmethod
    def get_pool_members(self, context, pool_id,
                         filters=None,
                         fields=None):
        pass

    @abc.abstractmethod
    def get_pool_member(self, context, id, pool_id,
                        fields=None):
        pass

    @abc.abstractmethod
    def create_pool_member(self, context, member,
                           pool_id):
        pass

    @abc.abstractmethod
    def update_pool_member(self, context, member, id,
                           pool_id):
        pass

    @abc.abstractmethod
    def delete_pool_member(self, context, id, pool_id):
        pass

    @abc.abstractmethod
    def get_l7policies(self, context, filters=None, fields=None):
        pass

    @abc.abstractmethod
    def get_l7policy(self, context, id, fields=None):
        pass

    @abc.abstractmethod
    def create_l7policy(self, context, l7policy):
        pass

    @abc.abstractmethod
    def update_l7policy(self, context, id, l7policy):
        pass

    @abc.abstractmethod
    def delete_l7policy(self, context, id):
        pass

    @abc.abstractmethod
    def get_l7policy_rules(self, context, l7policy_id,
                           filters=None):
        pass

    @abc.abstractmethod
    def get_l7policy_rule(self, context, id, l7policy_id):
        pass

    @abc.abstractmethod
    def create_l7policy_rule(self, context, rule, l7policy_id):
        pass

    @abc.abstractmethod
    def update_l7policy_rule(self, context, id, rule, l7policy_id):
        pass

    @abc.abstractmethod
    def delete_l7policy_rule(self, context, id, l7policy_id):
        pass

    @abc.abstractmethod
    def get_loadbalancer_lbaas_listeners(self, context, loadbalancer_id,
                         filters=None,
                         fields=None):
        pass

    @abc.abstractmethod
    def stats(self, context, loadbalancer_id):
        pass
