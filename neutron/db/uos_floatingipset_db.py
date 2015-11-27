# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
#    (c) Copyright 2014 UnitedStack Inc.
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
# @author: cing, UnitedStack Inc.

import copy

from sqlalchemy import orm
import sqlalchemy as sa
from sqlalchemy.orm import exc

from neutron.common import constants as l3_constants
from neutron.common import core as sql
from neutron.common import rpc as n_rpc
from neutron.common import uos_constants as uos_l3_constants
from neutron.common import utils as common_utils
from neutron import context as n_context
from neutron.db import db_base_plugin_v2
from neutron.db import model_base
from neutron.db import models_v2
from neutron.db import l3_db
from neutron.db import uos_floatingip_db
from neutron.extensions import uosfloatingipset
from neutron.extensions import uosfloatingip
from neutron.openstack.common import log as logging
from neutron.openstack.common import uuidutils


LOG = logging.getLogger('uos')
DEVICE_OWNER_ROUTER_GW = l3_constants.DEVICE_OWNER_ROUTER_GW
CORE_FLOATINGIP_ATTRS = ['tenant_id', 'id', 'floating_ip_address', 'floating_network_id',
                         'floating_subnet_id', 'floating_port_id', 'fixed_port_id',
                         'fixed_ip_address', 'router_id', 'status', 'uos_service_provider',
                         'uos_name', 'uos_registerno', 'rate_limit', 'created_at']

class FloatingIPSet(model_base.BASEV2, models_v2.HasId):
    """Represents a floating IP set
    """

    uos_name = sa.Column(sa.String(255), nullable=True)
    floatingips = orm.relationship(l3_db.FloatingIP, lazy='joined',
                               cascade="all, delete-orphan")


class UosFloatingIPSetMixin(uos_floatingip_db.UosFloatingIPMixin):

    @common_utils.exception_logger()
    def associate_floatingipset_router(self, context, floatingipset_id, router_id):
        with context.session.begin(subtransactions=True):
            floatingipset_db = self.get_floatingipset_fips(context,
                                                     floatingipset_id)
            floatingips = floatingipset_db['floatingips']
            if len(floatingips) != 1:
               raise uosfloatingipset.FloatingipsLenTooLong()

            return self.associate_floatingip_router(context, floatingips[0]['id'], router_id)

    def _get_fipset(self, context, id):
        try:
            floatingipset = self._get_by_id(context, FloatingIPSet, id)
        except exc.NoResultFound:
            raise uosfloatingipset.FloatingIPSetNotFound(floatingipset_id=id)
        return floatingipset

    def _make_fipset_fip_dict(self, fip_db):
        return dict((key, fip_db[key]) for key in CORE_FLOATINGIP_ATTRS)

    def _make_floatingipset_dict(self, floatingipset, fields=None):
        res = {'id': floatingipset['id'],
               'uos:name': floatingipset['uos_name'],
               'floatingips':
               [self._make_fipset_fip_dict(fip_db) for fip_db in floatingipset['floatingips']]}
        return self._fields(res, fields)

    def get_floatingipset_fips(self, context, id):
        floatingipset_db = self._get_fipset(context, id)
        return self._make_floatingipset_dict(floatingipset_db)

    def _format_floatingipset(self, floatingipset_db, fields=None):
        floatingipset = self._make_floatingipset_dict(floatingipset_db)
        floatingipset_addr = {}
        fipset = {}
        service_provider_list = list()
        subnet_id_list = list()
        for floatingip in floatingipset['floatingips']:
            service_provider = floatingip.get('uos_service_provider')
            floatingipset_addr[service_provider] = [floatingip['floating_ip_address']]
            service_provider_list.append(service_provider)
            subnet_id_list.append(floatingip.get('floating_subnet_id'))
            fipset = {'router_id':floatingip['router_id'],
                      'status':floatingip['status'],
                      'port_id':floatingip['fixed_port_id'],
                      'created_at':floatingip['created_at'],
                      'rate_limit':floatingip['rate_limit'],
                      'uos:registerno':floatingip['uos_registerno'],
                      'fixed_ip_address':floatingip['fixed_ip_address'],
                      'tenant_id':floatingip['tenant_id'],
                      'floatingipset_network_id':floatingip['floating_network_id']
                      }

        #NOTE product name is required by bill project
        fipset[uosfloatingip.UOS_SERVICE_PROVIDER] = service_provider_list
        fipset['floatingipset_subnet_id'] = subnet_id_list
        fipset['floatingipset_address'] = floatingipset_addr
        fipset['uos:name'] = floatingipset['uos:name']
        fipset['id'] = floatingipset['id']
        return fipset

    def _get_floatingipset(self, context, id, fields=None):
        floatingipset_db = self._get_fipset(context, id)
        return self._format_floatingipset(floatingipset_db,
                                          fields=fields)

    def get_floatingipset(self, context, id, fields=None):
        return self._get_floatingipset(context, id, fields=fields)

    def get_floatingipsets(self, context, filters=None, fields=None,
                        sorts=None, limit=None, marker=None,
                        page_reverse=False):
        marker_obj = self._get_marker_obj(context, 'floatingipset', limit,
                                          marker)
        floatingipsets = self._get_collection(context, FloatingIPSet,
                                             self._format_floatingipset,
                                             filters=filters, fields=fields,
                                             sorts=sorts,
                                             limit=limit,
                                             marker_obj=marker_obj,
                                             page_reverse=page_reverse)
        return floatingipsets

    def get_floatingipsets_count(self, context, filters=None):
        return self._get_collection_count(context, FloatingIPSet,
                                          filters=filters)

    def _check_exist_service_provider(self, context, fipset):
        network_id = fipset.get('floatingipset_network_id')
        if not fipset[uosfloatingip.UOS_SERVICE_PROVIDER]:
            raise uosfloatingipset.InputServieProviderNull()

        for service_provider in fipset.get(uosfloatingip.UOS_SERVICE_PROVIDER, []):
            filter = {'network_id': [network_id],
                      'uos_service_provider':[service_provider]}
            ctx_admin = n_context.get_admin_context()
            subnets = self._core_plugin.get_subnets(ctx_admin, filters=filter)
            if not subnets:
               raise uosfloatingipset.ServiceProviderNotExist(
                       service_provider=service_provider)

    def create_floatingipset(self, context, floatingipset):
        floatingip = {}
        fip = {}
        fipset = floatingipset['floatingipset']

        # check service provider exist
        self._check_exist_service_provider(context, fipset)

        fipset_id=uuidutils.generate_uuid()
        with context.session.begin(subtransactions=True):
            floatingipset_db = FloatingIPSet(id=fipset_id,
                                             uos_name=fipset.get('uos:name'))
            common_utils.make_default_name(floatingipset_db,
                   uos_l3_constants.UOS_PRE_FIPSET, name='uos_name')
            context.session.add(floatingipset_db)

        for service_provider in fipset.get(uosfloatingip.UOS_SERVICE_PROVIDER, []):
            fip['rate_limit'] = fipset.get('rate_limit')
            fip['tenant_id'] = fipset.get('tenant_id')
            fip[uosfloatingip.UOS_NAME] = fipset.get(uosfloatingip.UOS_NAME)
            fip['floating_network_id'] = fipset.get('floatingipset_network_id')
            fip[uosfloatingip.UOS_SERVICE_PROVIDER] = service_provider
            fip['floatingipset_id'] = fipset_id
            floatingip['floatingip'] = fip
            f = self.create_floatingip(context, floatingip)

        return self._get_floatingipset(context, fipset_id)

    def update_floatingipset(self, context, id, floatingipset):
        fipset = self.get_floatingipset_fips(context, id)
        floatingip = {'floatingip':floatingipset['floatingipset']}
        for fip in fipset['floatingips']:
            self.update_floatingip(context, fip['id'], floatingip)
        return self._get_floatingipset(context, id)

    def delete_floatingipset(self, context, id):
        floatingipset = self._get_fipset(context, id)
        with context.session.begin(subtransactions=True):
            context.session.delete(floatingipset)
