# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright 2013 UnitedStack, Inc.
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
# @author: Jianing Yang, UnitedStack, Inc

import netaddr
import sqlalchemy as sa
from sqlalchemy import orm

from neutron.common import utils
from neutron.db import db_base_plugin_v2
from neutron.db import l3_db
from neutron.db import model_base
from neutron.db import models_v2
from neutron.extensions import l3
from neutron.extensions import portforwardings
from neutron.openstack.common import excutils
from neutron.openstack.common import log as logging
from neutron.openstack.common import uuidutils
from oslo.config import cfg
from oslo.db import exception as db_exc

LOG = logging.getLogger(__name__)


class PortForwardingRule(model_base.BASEV2, models_v2.HasId):
    router_id = sa.Column(sa.String(36),
                          sa.ForeignKey('routers.id',
                                        ondelete="CASCADE"))

    router = orm.relationship(l3_db.Router,
                              backref=orm.backref("portforwarding_list",
                                                  lazy='joined',
                                                  cascade='delete'))
    # NOTE(jianingy): outside_port should be unique as we only have one
    #                 external ip address
    outside_port = sa.Column(sa.Integer())
    inside_addr = sa.Column(sa.String(15))
    inside_port = sa.Column(sa.Integer())
    # NOTE(jianingy): protocol can be either TCP or UDP
    protocol = sa.Column(sa.String(4))
    __table_args__ = (
        sa.schema.UniqueConstraint(
            'router_id',
            'protocol',
            'outside_port',
            name='uniq_pf_rule_10router_id0protocol0outside_port'),
        sa.schema.UniqueConstraint(
            'router_id',
            'protocol',
            'inside_addr',
            'inside_port',
            name='uniq_pf_rule_20router_id0protocol0inside_addr0inside_port'))


class PortForwardingDbMixin(l3_db.L3_NAT_db_mixin):
    """Mixin class to support nat rule configuration on router."""

    def _extend_router_dict_portforwarding(self, router_res, router_db):
        router_res['portforwardings'] = (
            PortForwardingDbMixin._make_extra_portfwd_list(
                router_db['portforwarding_list']))

    db_base_plugin_v2.NeutronDbPluginV2.register_dict_extend_funcs(
        l3.ROUTERS, ['_extend_router_dict_portforwarding'])

    def update_router(self, context, id, router):
        r = router['router']
        with context.session.begin(subtransactions=True):
            router_db = self._get_router(context, id)
            if 'portforwardings' in r:
                try:
                    self._validate_fwds(context, router_db,
                                        r['portforwardings'])
                    self._update_extra_portfwds(context, router_db,
                                                r['portforwardings'])
                    context.session.flush()
                except db_exc.DBDuplicateEntry as e:
                    with excutils.save_and_reraise_exception() as ctxt:
                        if e.columns == ['router_id',
                                         'protocol',
                                         'outside_port']:
                            ctxt.reraise = False
                            raise portforwardings.DuplicatedOutsidePort()
                        if e.columns == ['router_id',
                                         'protocol',
                                         'inside_addr',
                                         'inside_port']:
                            ctxt.reraise = False
                            raise portforwardings.DuplicatedInsidePort()

            portfwds = self._get_extra_portfwds_by_router_id(context, id)

        router_updated = super(PortForwardingDbMixin, self).update_router(
            context, id, router)
        router_updated['portforwardings'] = portfwds

        return router_updated

    def _validate_fwds(self, context, router, portfwds):
        query = context.session.query(models_v2.Network).join(models_v2.Port)
        networks = query.filter_by(device_id=router['id'])
        subnets = []
        for network in networks:
            subnets.extend(map(lambda x: x['cidr'], network.subnets))

        ip_addr, ip_net = netaddr.IPAddress, netaddr.IPNetwork
        for portfwd in portfwds:
            ip_str = portfwd['inside_addr']
            valid = any([ip_addr(ip_str) in ip_net(x) for x in subnets])
            if not valid:
                raise portforwardings.InvalidInsideAddress(inside_addr=ip_str)

    def get_portforwardings_count(self, context, filters, **kwargs):
        router_id = kwargs['router_id']
        count = context.session.query(PortForwardingRule).filter_by(router_id=router_id).count()
        return count

    def _update_extra_portfwds(self, context, router, portfwds):
        old_fwds = self._get_extra_portfwds_by_router_id(
            context, router['id'])
        added, removed = utils.diff_list_of_dict(old_fwds, portfwds)

        LOG.debug(_('Removed port forwarding rules are %s'), removed)
        for portfwd in removed:
            del_context = context.session.query(PortForwardingRule)
            del_context.filter_by(router_id=router['id'],
                                  outside_port=portfwd['outside_port'],
                                  inside_addr=portfwd['inside_addr'],
                                  inside_port=portfwd['inside_port'],
                                  protocol=portfwd['protocol']).delete()

        LOG.debug(_('Added port forwarding rules are %s'), added)
        for portfwd in added:
            router_fwds = PortForwardingRule(
                router_id=router['id'],
                outside_port=portfwd['outside_port'],
                inside_addr=portfwd['inside_addr'],
                inside_port=portfwd['inside_port'],
                protocol=portfwd['protocol'])

            context.session.add(router_fwds)

    @staticmethod
    def _make_extra_portfwd_list(portforwardings):
        return [{'outside_port': portfwd['outside_port'],
                 'inside_addr': portfwd['inside_addr'],
                 'inside_port': portfwd['inside_port'],
                 'protocol': portfwd['protocol'],
                 'id': portfwd['id']
                 }
                for portfwd in portforwardings]

    def _get_extra_portfwds_by_router_id(self, context, id):
        query = context.session.query(PortForwardingRule)
        query = query.filter_by(router_id=id)
        return self._make_extra_portfwd_list(query)

    def add_router_portforwarding(self, context, router_id, rule):
        try:
            router_db = self._get_router(context, router_id)
            self._validate_fwds(context, router_db, [rule])
            portforwarding_qry = context.session.query(PortForwardingRule)
            used = portforwarding_qry.filter_by(router_id=router_id).count()
            quota = cfg.CONF.quota_portforwarding
            if used >= quota:
                raise portforwardings.OverQuota(usage=used, quota=quota)
            router_fwd = PortForwardingRule(
                id=uuidutils.generate_uuid(),
                router_id=router_id,
                outside_port=rule['outside_port'],
                inside_addr=rule['inside_addr'],
                inside_port=rule['inside_port'],
                protocol=rule['protocol'])
            context.session.add(router_fwd)
            context.session.flush()
            rule['id'] = router_fwd.id
        except db_exc.DBDuplicateEntry as e:
            with excutils.save_and_reraise_exception() as ctxt:
                if e.columns == ['router_id',
                                 'protocol',
                                 'outside_port']:
                    ctxt.reraise = False
                    raise portforwardings.DuplicatedOutsidePort()
                if e.columns == ['router_id',
                                 'protocol',
                                 'inside_addr',
                                 'inside_port']:
                    ctxt.reraise = False
                    raise portforwardings.DuplicatedInsidePort()

        return rule

    def remove_router_portforwarding(self, context, router_id, rule):
        if 'id' in rule:
            LOG.debug(_('Removed port forwarding rules id =  %s'), rule['id'])
            del_context = context.session.query(PortForwardingRule)
            found = del_context.filter_by(id=rule['id'])
        else:
            LOG.debug(_('Removed port forwarding rules: %s'), rule)
            del_context = context.session.query(PortForwardingRule)
            found = del_context.filter_by(router_id=router_id,
                                          outside_port=rule['outside_port'],
                                          inside_addr=rule['inside_addr'],
                                          inside_port=rule['inside_port'],
                                          protocol=rule['protocol'])

        if found.count() > 0:
            data = dict(found.one())
            found.delete()
            return data
        else:
            raise portforwardings.RouterPortForwardingNotFound(
                router_id=router_id)
