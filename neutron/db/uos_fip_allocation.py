# Copyright 2012 VMware, Inc.  All rights reserved.
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
# Author cinghu xining@unitedstack.com Unitedstack.Inc

import netaddr
from oslo.config import cfg
import sqlalchemy as sa
from sqlalchemy import orm
from sqlalchemy.orm import exc
from sqlalchemy.orm.properties import RelationshipProperty


from neutron import context as neutron_context
from neutron.common import core as sql
from neutron.db import model_base
from neutron.db import models_v2
from neutron.db import l3_db
from neutron.extensions import uosfloatingip
from neutron.common import exceptions as n_exc
from neutron.openstack.common import log as logging
from neutron.openstack.common import timeutils
from neutron.openstack.common import uuidutils
from neutron.plugins.common import constants
from neutron import manager


LOG = logging.getLogger(__name__)

class FloatingIpAllocation(model_base.BASEV2):
    """use this table for a new Algorithm of floatingip.
    """

    floating_ip_address = sa.Column(sa.String(64),
                             primary_key=True,nullable=False)
    last_tenant_id = sa.Column(sa.String(255), nullable=True)
    floating_subnet_id = sa.Column(sa.String(255), nullable=True)
    allocated = sa.Column(sa.Boolean, nullable=True)
    updated_at = sa.Column(sa.DateTime, nullable=False)

class Floatingip_Allocation_db_mixin(object):

    @property
    def _core_plugin(self):
        return manager.NeutronManager.get_plugin()

    def _make_floatingip_alloc_dict(self, fip_alloc, fields=None):
        res = {'last_tenant_id': fip_alloc['last_tenant_id'],
               'floating_ip_address': fip_alloc['floating_ip_address'],
               'floating_subnet_id': fip_alloc['floating_subnet_id'],
               'allocated': fip_alloc['allocated'],
               'updated_at': fip_alloc['updated_at']}
        return self._fields(res, fields)

    def clear_floatingip_allocation(self, subnet_id):
        admin_cxt = neutron_context.get_admin_context()
        fip_qry = admin_cxt.session.query(
                 FloatingIpAllocation).filter_by(
                 floating_subnet_id=subnet_id).delete()
        LOG.info("clear floatingip allocation table successfully")

    def _create_floatingip_allocation(self, context, floatingip):
        with context.session.begin(subtransactions=True):
            floatingip_alloc_db = FloatingIpAllocation(
                last_tenant_id=floatingip['last_tenant_id'],
                updated_at=timeutils.utcnow(),
                floating_ip_address=floatingip['floating_ip_address'],
                floating_subnet_id=floatingip['floating_subnet_id'],
                allocated=floatingip['allocated'])
            context.session.add(floatingip_alloc_db)

        LOG.info(_("floatingip allocation %s successful") % floatingip)

    def _update_floatingip_time_tenant(self, context, floatingip_alloc):
        floating_ip_address = floatingip_alloc['floating_ip_address']
        fip_qry = context.session.query(FloatingIpAllocation)
        fip = fip_qry.filter_by(floating_ip_address=floating_ip_address)
        fip.update({'updated_at':timeutils.utcnow(),
                    'last_tenant_id':floatingip_alloc['last_tenant_id'],
                    'floating_subnet_id':floatingip_alloc['floating_subnet_id'],
                    'allocated':floatingip_alloc['allocated']})

        LOG.info("update floatingip %s time and tenant_id successfully" % fip)

    def _get_floatingip_allocations(self, context, filters=None, fields=None,
                        sorts=None, limit=None, marker=None,
                        page_reverse=False):
        marker_obj = self._get_marker_obj(context, 'floatingipallocation', limit,
                                          marker)
        fip_allocs = self._get_collection(context, FloatingIpAllocation,
                                    self._make_floatingip_alloc_dict,
                                    filters=filters, fields=fields,
                                    sorts=sorts,
                                    limit=limit,
                                    marker_obj=marker_obj,
                                    page_reverse=page_reverse)
        return fip_allocs


    def _get_tenant_floatingip_noused(self, context, tenant_id, subnets):
        subnet_ids = [subnet['id'] for subnet in subnets]
        fip_allocs = None

        sorts = [('updated_at', False)]
        filters = {'allocated':[False],
                   'last_tenant_id':[tenant_id],
                   'floating_subnet_id':subnet_ids}

        fip_allocs = self._get_floatingip_allocations(context,
                            filters=filters, sorts=sorts)

        if fip_allocs:
            return fip_allocs[0]['floating_ip_address']

        return fip_allocs

    def _get_floatingip_range_count(self, context, subnets):
        """ 
        Get all floatingip of allocation pools in subnets
        """
        sum = 0
        for subnet in subnets:
            allocation_pools = subnet['allocation_pools']
            for ip_pool in allocation_pools:
                try:
                    start_ip = netaddr.IPAddress(ip_pool['start'])
                    end_ip = netaddr.IPAddress(ip_pool['end'])
                except netaddr.AddrFormatError:
                    LOG.info(_("Found invalid IP address in pool: "
                               "%(start)s - %(end)s:"),
                             {'start': ip_pool['start'],
                              'end': ip_pool['end']})
                    raise n_exc.InvalidAllocationPool(pool=ip_pool)
                num = len(netaddr.IPRange(ip_pool['start'],ip_pool['end']))
                sum+=num
        LOG.info('get floatingip range count : %s' % sum)
        return sum

    def _get_oldest_floaingip_address(self, context, subnets):
        subnet_ids = [subnet['id'] for subnet in subnets]
        sorts = [('updated_at', True)]
        filters = {'allocated': [False],
                   'floating_subnet_id':subnet_ids}
        floatingip_allocations = self._get_floatingip_allocations(
                                    context, filters=filters, sorts=sorts)
        if not floatingip_allocations:
            raise uosfloatingip.FloatingipNoAvaliable()

        floating_ip_address = floatingip_allocations[0].get(
                                'floating_ip_address', None)

        LOG.info('get oldest floating_ip_address : %s' % floating_ip_address)
        return floating_ip_address

    def _get_floatingip_count(self, context, subnets):
        """
        the num of no allocated floatingip from FloatingIpAllocation and the num of
        allocated floatingip from IPAllocation, the sum of the two numbers specified those
        floatingip address has been used
        """
        subnet_ids = [subnet['id'] for subnet in subnets]
        sum = 0
        with context.session.begin(subtransactions=True):
           no_allocated_fip=(self._model_query(context, FloatingIpAllocation).
                             filter(FloatingIpAllocation.allocated == False).
                             filter(FloatingIpAllocation.floating_subnet_id.in_(
                             subnet_ids))).count()
           allocated_fip=(self._model_query(context, models_v2.IPAllocation).
                             filter(models_v2.IPAllocation.subnet_id.in_(
                             subnet_ids))).count()
           sum = no_allocated_fip+allocated_fip

        LOG.info('get floatingip count : %s' % sum)
        return sum

    def _get_valid_subnets(self, context, floatingip):
        floatingip_service_provider = floatingip.get(uosfloatingip.UOS_SERVICE_PROVIDER)
        network_id = floatingip.get('floating_network_id')
        subnet_id = floatingip.get('floating_subnet_id', None)

        filter = {'network_id': [network_id]}
        subnets = self._core_plugin.get_subnets(context, filters=filter)

        LOG.info("get all subnets: %s" % subnets)

        shadow_subnet = cfg.CONF.unitedstack.external_shadow_subnet
        subnets_to_exclude = cfg.CONF.unitedstack.subnets_to_exclude

        valid_subnets  = []
        for subnet in subnets:
            subnet_service_provider = subnet[uosfloatingip.UOS_SERVICE_PROVIDER]
            if (shadow_subnet and shadow_subnet == subnet['name']) or (
                subnet['id'] in subnets_to_exclude) or (
                floatingip_service_provider and (
                subnet_service_provider!=floatingip_service_provider) or (
                subnet_id and subnet_id != subnet['id'])):
                continue
            else:
                valid_subnets.append(subnet)

        if not valid_subnets:
            raise uosfloatingip.NoAvaliableSubnet(network_id=network_id)

        LOG.info("get valid subnets: %s" % valid_subnets)
        return valid_subnets

    def allocate_floatingip_address(self, context, floatingip_dict):
        floatingip = floatingip_dict['floatingip'].copy()
        floating_ip_address = floatingip.get('floating_ip_address', None)
        floating_subnet_id = floatingip.get('floating_subnet_id', None)
        tenant_id = self._get_tenant_id_for_create(context, floatingip)
        admin_cxt = neutron_context.get_admin_context()

        if floating_ip_address:
            return floating_ip_address

        subnets = self._get_valid_subnets(admin_cxt, floatingip)

        floating_ip_address = self._get_tenant_floatingip_noused(context,
                                         tenant_id, subnets)

        LOG.info('get noused floatingip address %s for this tenant %s' % (
                    floating_ip_address, tenant_id))
        # There is a floating_ip_address in floatingip allocation table
        # for input tenant
        if floating_ip_address:
            return floating_ip_address
        else:
            fip_range_count = self._get_floatingip_range_count(
                                     admin_cxt, subnets)
            fip_alloc_count = self._get_floatingip_count(admin_cxt, subnets)
            # if fip_range_count greater than fip_alloc_count, it specified there is
            # no used floatingip, so we should get a floatingip from ipavailablepool table,
            # else we get a floating ip from floatingipallocations table
            if fip_range_count > fip_alloc_count:
                fip = self._core_plugin.generate_ip(admin_cxt, subnets)
                if not fip:
                    raise uosfloatingip.FloatingipNoAvaliable()
                else:
                    floating_ip_address = fip['ip_address']
            elif fip_range_count == fip_alloc_count:
                floating_ip_address = self._get_oldest_floaingip_address(context, subnets)
            else:
                LOG.warn('the num of floatingip allocation table exception')

        LOG.info('return floatigip ip address %s ' % floating_ip_address)
        return floating_ip_address

    def update_floatingip_allocation_record(self,
                  context, floatingip, allocated=False):
        floating_ip_address = floatingip['floating_ip_address']
        fip_alloc = { 'allocated':allocated,
                      'last_tenant_id':floatingip['tenant_id'],
                      'floating_ip_address':floatingip['floating_ip_address'],
                      'floating_subnet_id':floatingip['floating_subnet_id']
                    }
        filters = {'floating_ip_address':[floating_ip_address]}
        fip_allocs = self._get_floatingip_allocations(context, filters=filters)
        if not fip_allocs:
            self._create_floatingip_allocation(context, fip_alloc)
        else:
            self._update_floatingip_time_tenant(context, fip_alloc)
