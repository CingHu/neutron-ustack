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

from eventlet import greenthread

from oslo.config import cfg
from oslo.db import exception as db_exc
import sqlalchemy as sa
from sqlalchemy.orm import exc
from sqlalchemy import sql

from neutron.common import rpc as n_rpc
from neutron.db import model_base
from neutron.db import models_v2
from neutron.db import db_base_plugin_v2
from neutron.extensions import tenant as ext_tenant
from neutron import manager
from neutron.openstack.common import excutils
from neutron.openstack.common import jsonutils
from neutron.openstack.common import log as logging
from neutron.openstack.common import timeutils
from neutron.plugins.common import constants

LOG = logging.getLogger(__name__)


class TenantDbMixin(ext_tenant.TenantPluginBase):
    """Mixin class to add tenant extension to db_base_plugin_v2."""

    def _get_tenant(self, context, id):
        pass

    def _get_all_tenant(self, context):
        res = db_base_plugin_v2.NeutronDbPluginV2().get_ports(context)
        tenants = [r['tenant_id'] for r in res if r['tenant_id']]
        return list(set(tenants))

    def get_l3_plugin(self):

        return manager.NeutronManager.get_service_plugins().get(
                constants.L3_ROUTER_NAT)

    def get_tenants(self, context, filters=None, fields=None, sorts=None,
                     limit=None, marker=None, page_reverse=False):
        tenants = self._get_all_tenant(context)
        result_t = []
        l3_plugin = self.get_l3_plugin()
        if 'id' in filters:
           tenants = filters.pop('id')
        for tenant in tenants:
            counter = {}
            counter['id'] = tenant
            for resource in ['ports', 'networks', 'floatingips', 'subnets', 'routers']:
                method = "get_" + resource
                filters['tenant_id'] = [tenant]
                if resource in ['ports', 'networks','subnets']:
                    method_r = getattr(db_base_plugin_v2.NeutronDbPluginV2(), method)
                else:
                    method_r = getattr(l3_plugin, method)

                result = method_r(context,filters=filters,fields=fields, sorts=sorts,
                             limit=limit, marker=marker, page_reverse=page_reverse)
                r_counter = resource + '_count'
                counter.update({r_counter:len(result)})

            result_t.append(counter)

        return result_t
