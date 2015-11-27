# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2012 Nicira Networks, Inc.
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
#
# @author: cing, UnitedStack, Inc
#
import abc

from neutron import quota
from neutron.api import extensions
from neutron.api.v2 import attributes as attr
from neutron.api.v2 import resource_helper
from neutron.common import exceptions as qexception
from neutron.plugins.common import constants


UOS_SERVICE_PROVIDER = 'uos:service_provider'
UOS_NAME = 'uos:name'
UOS_REGISTERNO = 'uos:registerno'
UOS_PORT_DEVICE_NAME = 'uos:port_device_name'
UOS_PORT_DEVICE_OWNER = 'uos:port_device_owner'
UOS_PORT_DEVICE_ID = 'uos:port_device_id'
UOS_RATE_LIMIT = 'rate_limit'
RESOURCE_ATTRIBUTE_MAP = {
    'floatingipsets': {
        'id': {'allow_post': False, 'allow_put': False,
               'validate': {'type:uuid': None},
               'is_visible': True,
               'primary_key': True},
        'floatingipset_address': {'allow_post': False, 'allow_put': False,
                                'convert_to': attr._validate_dict_or_none,
                                'is_visible': True, 'required_by_policy': True,
                                'enforce_policy': True, 'default': list()},
        'floatingipset_subnet_id': {'allow_post': True, 'allow_put': False,
                               'convert_to': attr.convert_to_list,
                               'validate': {'type:uuid_list': None},
                               'is_visible': True,
                               'default': None},
        'floatingipset_network_id': {'allow_post': True, 'allow_put': False,
                                'validate': {'type:uuid': None},
                                'is_visible': True},
        'router_id': {'allow_post': False, 'allow_put': False,
                      'validate': {'type:uuid_or_none': None},
                      'is_visible': True, 'default': None},
        'port_id': {'allow_post': True, 'allow_put': True,
                    'validate': {'type:uuid_or_none': None},
                    'is_visible': True, 'default': None,
                    'required_by_policy': True},
        'fixed_ip_address': {'allow_post': True, 'allow_put': True,
                             'validate': {'type:ip_address_or_none': None},
                             'is_visible': True, 'default': None},
        'tenant_id': {'allow_post': True, 'allow_put': False,
                      'required_by_policy': True,
                      'validate': {'type:string': None},
                      'is_visible': True},
        'status': {'allow_post': False, 'allow_put': False,
                   'is_visible': True},
        UOS_NAME: {'allow_post': True, 'allow_put': False,
                   'validate': {'type:string': None},
                   'is_visible': True, 'default': ''},
        UOS_REGISTERNO: {'allow_post': True, 'allow_put': False,
                         'validate': {'type:string': None},
                         'is_visible': True, 'default': ''},
        UOS_SERVICE_PROVIDER: {'allow_post': True, 'allow_put': False,
                         'convert_to': attr.convert_to_list,
                         'is_visible': True, 'default': ''},
        UOS_PORT_DEVICE_NAME: {'allow_post': False, 'allow_put': False,
                               'validate': {'type:string': None},
                               'is_visible': True, 'default': ''},
        UOS_PORT_DEVICE_OWNER: {'allow_post': False, 'allow_put': False,
                                'validate': {'type:string': None},
                                'is_visible': True, 'default': ''},
        UOS_PORT_DEVICE_ID: {'allow_post': False, 'allow_put': False,
                             'validate': {'type:string': None},
                             'is_visible': True, 'default': ''},
        UOS_RATE_LIMIT: {'allow_post': True, 'allow_put': False,
                     'convert_to': attr.convert_to_int,
                     'validate': {'type:fip_rate_limit': None},
                     'is_visible': True, 'default': 1024}

    }
}

class ServiceProviderNotExist(qexception.BadRequest):
    message = _("the service provider %(service_provider)s is not exists")

class InputServieProviderNull(qexception.BadRequest):
    message = _("the service provider could not be found")

class FloatingipsLenTooLong(qexception.BadRequest):
    message = _("In the floatingipset, the num of floatingip must be only one")

class FloatingIPSetNotFound(qexception.NotFound):
    message = _("Floating IP Set %(floatingipset_id)s could not be found")

class Uosfloatingipset(extensions.ExtensionDescriptor):

    @classmethod
    def get_name(cls):
        return "UnitedStack Floatingipset"

    @classmethod
    def get_alias(cls):
        return "uos_floatingipsets"

    @classmethod
    def get_description(cls):
        return ("Return related resources")

    @classmethod
    def get_namespace(cls):
        return "http://docs.openstack.org/ext/neutron/uos/api/v1.0"

    @classmethod
    def get_updated(cls):
        return "2013-12-25T10:00:00-00:00"

    @classmethod
    def get_resources(cls):
        """Returns uos floatingipset Resources."""
        return []

    @classmethod
    def get_resources(cls):
        """Returns floatingipset Resources."""
        plural_mappings = resource_helper.build_plural_mappings(
            {}, RESOURCE_ATTRIBUTE_MAP)
        attr.PLURALS.update(plural_mappings)
        #quota.QUOTAS.register_resource_by_name('floatingset')
        return resource_helper.build_resource_info(plural_mappings,
                                                   RESOURCE_ATTRIBUTE_MAP,
                                                   constants.L3_ROUTER_NAT,
                                                   register_quota=True)

    def update_attributes_map(self, attributes):
        super(Uosfloatingipset, self).update_attributes_map(
            attributes, extension_attrs_map=RESOURCE_ATTRIBUTE_MAP)

    def get_extended_resources(self, version):
        if version == "2.0":
            return RESOURCE_ATTRIBUTE_MAP
        else:
            return {}

class FloatingipsetBase(object):

    @abc.abstractmethod
    def create_floatingipset(self, context, floatingipset):
        pass

    @abc.abstractmethod
    def update_floatingipset(self, context, id, floatingipset):
        pass

    @abc.abstractmethod
    def get_floatingipset(self, context, id, fields=None):
        pass

    @abc.abstractmethod
    def delete_floatingipset(self, context, id):
        pass

    @abc.abstractmethod
    def get_floatingipsets(self, context, filters=None, fields=None,
                        sorts=None, limit=None, marker=None,
                        page_reverse=False):
        pass

    def get_floatingipsets_count(self, context, filters=None):
        pass

