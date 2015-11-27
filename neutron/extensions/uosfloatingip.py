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
# @author: Yong Sheng Gong, UnitedStack, Inc
#

from neutron.api import extensions
from neutron.common import exceptions as n_exc

UOS_SERVICE_PROVIDER = 'uos:service_provider'
UOS_NAME = 'uos:name'
UOS_REGISTERNO = 'uos:registerno'
UOS_PORT_DEVICE_NAME = 'uos:port_device_name'
UOS_PORT_DEVICE_OWNER = 'uos:port_device_owner'
UOS_PORT_DEVICE_ID = 'uos:port_device_id'
EXTENDED_ATTRIBUTES_2_0 = {
    'floatingips': {
        UOS_NAME: {'allow_post': True, 'allow_put': False,
                   'validate': {'type:string': None},
                   'is_visible': True, 'default': ''},
        # NOTE(gongysh) since we have no update floatingip API,
        # the allow_put is given as False, but we use a special API
        # to change the UOS_REGISTERNO
        UOS_REGISTERNO: {'allow_post': True, 'allow_put': False,
                         'validate': {'type:string': None},
                         'is_visible': True, 'default': ''},
        UOS_SERVICE_PROVIDER: {'allow_post': True, 'allow_put': False,
                         'validate': {'type:string': None},
                         'is_visible': True, 'default': ''},
        UOS_PORT_DEVICE_NAME: {'allow_post': False, 'allow_put': False,
                               'validate': {'type:string': None},
                               'is_visible': True, 'default': ''},
        UOS_PORT_DEVICE_OWNER: {'allow_post': False, 'allow_put': False,
                                'validate': {'type:string': None},
                                'is_visible': True, 'default': ''},
        UOS_PORT_DEVICE_ID: {'allow_post': False, 'allow_put': False,
                             'validate': {'type:string': None},
                             'is_visible': True, 'default': ''}}}


class GWPortForFloatingIPNotFound(n_exc.NotFound):
    message = _("Gateway port for router %(id)s not found.")


class Uosfloatingip(extensions.ExtensionDescriptor):

    @classmethod
    def get_name(cls):
        return "UnitedStack Floatingip ext"

    @classmethod
    def get_alias(cls):
        return "uos_floatingips"

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
        """Returns uos floatingip Resources."""
        return []

    def get_extended_resources(self, version):
        if version == "2.0":
            return EXTENDED_ATTRIBUTES_2_0
        else:
            return {}
