# Copyright (c) 2013 OpenStack Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import abc

from neutron.api import extensions
from neutron.api.v2 import attributes as attr
from neutron.api.v2 import base
from neutron.common import exceptions
from neutron import manager
from oslo.config import cfg


# Attribute Map
RESOURCE_NAME = 'tenant'
RESOURCE_ATTRIBUTE_MAP = {
    RESOURCE_NAME + 's': {
        'id': {'allow_post': False, 'allow_put': False,
               'validate': {'type:uuid': None},
               'is_visible': True},
        'ports_count': {'allow_post': False, 'allow_put': False,
                         'convert_to': attr.convert_to_int,
                         'is_visible': True},
        'networks_count': {'allow_post': False, 'allow_put': False,
                         'convert_to': attr.convert_to_int,
                         'is_visible': True},
        'floatingips_count': {'allow_post': False, 'allow_put': False,
                         'convert_to': attr.convert_to_int,
                         'is_visible': True},
        'subnets_count': {'allow_post': False, 'allow_put': False,
                         'convert_to': attr.convert_to_int,
                         'is_visible': True},
        'routers_count': {'allow_post': False, 'allow_put': False,
                         'convert_to': attr.convert_to_int,
                         'is_visible': True},
    },
}



class Tenant(object):
    """Tenant management extension."""

    @classmethod
    def get_name(cls):
        return "tenant"

    @classmethod
    def get_alias(cls):
        return "tenant"

    @classmethod
    def get_description(cls):
        return "The tenant management extension."

    @classmethod
    def get_namespace(cls):
        return "http://docs.openstack.org/ext/tenant/api/v2.0"

    @classmethod
    def get_updated(cls):
        return "2013-02-03T10:00:00-00:00"

    @classmethod
    def get_resources(cls):
        """Returns Ext Resources."""
        my_plurals = [(key, key[:-1]) for key in RESOURCE_ATTRIBUTE_MAP.keys()]
        attr.PLURALS.update(dict(my_plurals))
        plugin = manager.NeutronManager.get_plugin()
        params = RESOURCE_ATTRIBUTE_MAP.get(RESOURCE_NAME + 's')
        controller = base.create_resource(RESOURCE_NAME + 's',
                                          RESOURCE_NAME,
                                          plugin, params,
                                          allow_pagination=cfg.CONF.allow_pagination,
                                          )
        ex = extensions.ResourceExtension(RESOURCE_NAME + 's',
                                          controller)

        return [ex]

    def get_extended_resources(self, version):
        if version == "2.0":
            return RESOURCE_ATTRIBUTE_MAP
        else:
            return {}


class TenantPluginBase(object):
    """REST API to operate the tenant.

    All of method must be in an admin context.
    """

    @abc.abstractmethod
    def get_tenants(self, context, filters=None, fields=None):
        pass

    @abc.abstractmethod
    def get_tenant(self, context, id, fields=None):
        pass
