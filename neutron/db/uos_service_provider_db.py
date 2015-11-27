# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2015 Uos Networks, Inc.  All rights reserved.
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
# @author: Salvatore Orlando, Nicira, Inc
#

import sqlalchemy as sa

from neutron.api.v2 import attributes
from neutron.db import db_base_plugin_v2
from neutron.db import models_v2
from neutron.openstack.common import log as logging
from neutron.extensions import uos_service_provider

LOG = logging.getLogger(__name__)

setattr(models_v2.Subnet, 'uos_service_provider',
        sa.Column(sa.String(255), nullable=True))


class Uos_subnet_service_provider_db_mixin(object):
    """Mixin class to add service_provider."""

    # Register dict extend functions for subnets
    db_base_plugin_v2.NeutronDbPluginV2.register_dict_extend_funcs(
        attributes.SUBNETS, ['_extend_subnet_dict_service_provider'])

    def _extend_subnet_dict_service_provider(self, subnet_res, subnet_db):
        subnet_res[uos_service_provider.SERVICE_PROVIDER] = subnet_db.uos_service_provider

    def _process_uos_service_provider_create(self, context, subnet_data, subnetdb):
        if uos_service_provider.SERVICE_PROVIDER in subnet_data:
            subnetdb.uos_service_provider = subnet_data[uos_service_provider.SERVICE_PROVIDER]

    def _process_uos_service_provider_update(self, context, subnet_data, subnetdb):
        if uos_service_provider.SERVICE_PROVIDER in subnet_data:
            subnetdb.uos_service_provider = subnet_data[uos_service_provider.SERVICE_PROVIDER]
