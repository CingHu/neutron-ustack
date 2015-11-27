# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
#    (c) Copyright 2013 Hewlett-Packard Development Company, L.P.
#    All Rights Reserved.
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
# @author: Swaminathan Vasudevan, Hewlett-Packard.

import abc

from oslo.config import cfg
import six

from neutron.api import extensions
from neutron.api.v2 import attributes as attr
from neutron.api.v2 import base
from neutron.common import exceptions as nexception
from neutron import manager
from neutron.plugins.common import constants
from neutron import quota
from neutron.services import service_base


class DuplicateVPNUsername(nexception.InvalidInput):
    message = _("Duplicate vpn username: %(name)s")


class InvalidVPNUsername(nexception.InvalidInput):
    message = _("VPN username must be an non-empty string "
            "with numbers or English letters")


class InvalidVPNPassword(nexception.InvalidInput):
    message = _("Password must be an non-empty string "
                "with 8 to 12 ASCII characters without ' or \"")


def _validate_name(name, valid_values=None):
    if (attr.validators["type:not_empty_string_or_none"](name) or
            attr.validators["type:regex"](name, "^[A-Za-z0-9]+$")):
        raise InvalidVPNUsername()
    #NOTE(wangw) "^[A-Za-z0-9]+$" can check whether name is "",
    # but it doesn't support None. I think use builtin function is
    # better than catch a TypeError.


def _validate_password(password, valid_values=None):
    if (attr.validators["type:not_empty_string_or_none"](password) or
            attr.validators["type:ascii_string"](password) or
            any(c in password for c in ("'", '"')) or
            not 7 < len(password) < 13):
        raise InvalidVPNPassword()

attr.validators['type:vpnuser_name'] = _validate_name
attr.validators['type:vpnuser_pass'] = _validate_password

RESOURCE_ATTRIBUTE_MAP = {

    'vpnusers': {
        'id': {'allow_post': False, 'allow_put': False,
               'validate': {'type:uuid': None},
               'is_visible': True,
               'primary_key': True},
        'tenant_id': {'allow_post': True, 'allow_put': False,
                      'validate': {'type:string': None},
                      'required_by_policy': True,
                      'is_visible': True},
        'name': {'allow_post': True, 'allow_put': True,
                 'validate': {'type:vpnuser_name': None},
                 'is_visible': True, 'default': ''},
        'password': {'allow_post': True, 'allow_put': True,
                     'validate': {'type:vpnuser_pass': None},
                     'is_visible': True, 'default': ''},
        'description': {'allow_post': True, 'allow_put': True,
                        'validate': {'type:string': None},
                        'is_visible': True, 'default': ''},
        'admin_state_up': {'allow_post': True, 'allow_put': True,
                           'default': True,
                           'convert_to': attr.convert_to_boolean,
                           'is_visible': True},
        'created_at': {'allow_post': False, 'allow_put': False,
                       'is_visible': True},
    },
}


class Vpnuser(extensions.ExtensionDescriptor):

    @classmethod
    def get_name(cls):
        return "VPN user service"

    @classmethod
    def get_alias(cls):
        return "vpn_user"

    @classmethod
    def get_description(cls):
        return "Extension for VPN User service"

    @classmethod
    def get_namespace(cls):
        return "https://wiki.openstack.org/Neutron/VPNaaS/user"

    @classmethod
    def get_updated(cls):
        return "2014-02-23T10:00:00-00:00"

    @classmethod
    def get_resources(cls):
        plural_mapping = {
            'vpnusers': 'vpnuser',
        }
        my_plurals = []
        for plural in RESOURCE_ATTRIBUTE_MAP:
            singular = plural_mapping.get(plural, plural[:-1])
            my_plurals.append((plural, singular))
        attr.PLURALS.update(dict(my_plurals))
        resources = []
        plugin = manager.NeutronManager.get_service_plugins()[
            constants.VPN]
        for collection_name in RESOURCE_ATTRIBUTE_MAP:
            resource_name = plural_mapping.get(
                collection_name, collection_name[:-1])
            params = RESOURCE_ATTRIBUTE_MAP[collection_name]
            collection_name = collection_name.replace('_', '-')

            quota.QUOTAS.register_resource_by_name(resource_name)
            controller = base.create_resource(
                collection_name, resource_name, plugin, params,
                allow_pagination=cfg.CONF.allow_pagination,
                allow_sorting=cfg.CONF.allow_sorting)

            resource = extensions.ResourceExtension(
                collection_name,
                controller,
                path_prefix=constants.COMMON_PREFIXES[constants.VPN],
                attr_map=params)
            resources.append(resource)
        return resources

    @classmethod
    def get_plugin_interface(cls):
        return VPNUserPluginBase

    def update_attributes_map(self, attributes):
        super(Vpnuser, self).update_attributes_map(
            attributes, extension_attrs_map=RESOURCE_ATTRIBUTE_MAP)

    def get_extended_resources(self, version):
        if version == "2.0":
            return RESOURCE_ATTRIBUTE_MAP
        else:
            return {}


@six.add_metaclass(abc.ABCMeta)
class VPNUserPluginBase(service_base.ServicePluginBase):

    def get_plugin_name(self):
        return constants.VPN

    def get_plugin_type(self):
        return constants.VPN

    def get_plugin_description(self):
        return 'VPN User service plugin'

    @abc.abstractmethod
    def get_vpnusers(self, context, filters=None, fields=None):
        pass

    @abc.abstractmethod
    def get_vpnuser(self, context, vpnuser_id, fields=None):
        pass

    @abc.abstractmethod
    def create_vpnuser(self, context, vpnuser):
        pass

    @abc.abstractmethod
    def update_vpnuser(self, context, vpnuser_id, vpnuser):
        pass

    @abc.abstractmethod
    def delete_vpnuser(self, context, vpnuser_id):
        pass
