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


RATE_LIMIT = 'uos:rate_limit'
EXTENDED_ATTRIBUTES_2_0 = {
    'networks': {RATE_LIMIT: {'allow_post': True,
                              'allow_put': True,
                              'default': 600,
                              'is_visible': True,
                              'convert_to': attr.convert_to_int,
                              'enforce_policy': True,
                              'required_by_policy': True}}}


class Uos_net_ratelimits(extensions.ExtensionDescriptor):

    @classmethod
    def get_name(cls):
        return "Uos net ratelimit"

    @classmethod
    def get_alias(cls):
        return "uos-net-ratelimit"

    @classmethod
    def get_description(cls):
        return _("Adds ratelimit attribute to network resource.")

    @classmethod
    def get_namespace(cls):
        return ("http://docs.openstack.org/ext/neutron/"
                + "uos_net_ratelimit/api/v1.0")

    @classmethod
    def get_updated(cls):
        return "2014-04-14T10:00:00-00:00"

    def get_extended_resources(self, version):
        if version == "2.0":
            return EXTENDED_ATTRIBUTES_2_0
        else:
            return {}
