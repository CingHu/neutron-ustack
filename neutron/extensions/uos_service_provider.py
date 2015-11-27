# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2013 OpenStack Foundation.
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

from neutron.api import extensions
from neutron.api.v2 import attributes as attr


SERVICE_PROVIDER = 'uos:service_provider'
EXTENDED_ATTRIBUTES_2_0 = {
    'subnets': {SERVICE_PROVIDER: {'allow_post': True,
                              'allow_put': True,
                              'is_visible': True,
                              'default': '',
                              'validate': {'type:string': None},
                              'enforce_policy': True,
                              'required_by_policy': True}}}


class Uos_service_provider(extensions.ExtensionDescriptor):

    @classmethod
    def get_name(cls):
        return "Uos subnet service provider"

    @classmethod
    def get_alias(cls):
        return "uos-service-provider"

    @classmethod
    def get_description(cls):
        return _("Adds service provider attribute to subnet resource.")

    @classmethod
    def get_namespace(cls):
        return ("http://docs.openstack.org/ext/neutron/"
                + "uos_service_provider/api/v1.0")

    @classmethod
    def get_updated(cls):
        return "2015-02-02T10:00:00-00:00"

    def get_extended_resources(self, version):
        return EXTENDED_ATTRIBUTES_2_0
