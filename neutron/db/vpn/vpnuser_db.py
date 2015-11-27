#
#    (c) Copyright 2014 Hewlett-Packard Development Company, L.P.
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
# @author: yong sheng gong.

import sqlalchemy as sa

from neutron.db import common_db_mixin
from neutron.db import model_base
from neutron.db import models_v2
from neutron.extensions import vpnuser as vpnuser_ext
from neutron.openstack.common import log as logging
from neutron.openstack.common import timeutils
from neutron.openstack.common import uuidutils
from oslo.db import exception as db_exc


LOG = logging.getLogger(__name__)


class VPNuser(model_base.BASEV2, models_v2.HasId,
              models_v2.HasTenant,
              models_v2.TimestampMixin):
    """Represents a VPN user Object."""
    name = sa.Column(sa.String(255))
    password = sa.Column(sa.String(255))
    description = sa.Column(sa.String(255))
    admin_state_up = sa.Column(sa.Boolean(), nullable=False)
    __table_args__ = (
        sa.UniqueConstraint('tenant_id', 'name',
                            name='uniq_vpnusername0tenant_id0name'),
    )


class VPNUserNDbMixin(vpnuser_ext.VPNUserPluginBase,
                      common_db_mixin.CommonDbMixin):
    """PPTPD VPN plugin database class using SQLAlchemy models."""
    vpnuser_notifier = None

    def _make_vpnuser_dict(self, vpnuser, fields=None):

        res = {'id': vpnuser['id'],
               'tenant_id': vpnuser['tenant_id'],
               'name': vpnuser['name'],
               'password': vpnuser['password'],
               'description': vpnuser['description'],
               'admin_state_up': vpnuser['admin_state_up'],
               'created_at': vpnuser['created_at'],
               }

        return self._fields(res, fields)

    def create_vpnuser(self, context, vpnuser):
        vpnuser = vpnuser['vpnuser']
        tenant_id = self._get_tenant_id_for_create(context, vpnuser)
        try:
            with context.session.begin(subtransactions=True):
                vpnuser_db = VPNuser(
                    id=uuidutils.generate_uuid(),
                    tenant_id=tenant_id,
                    name=vpnuser['name'],
                    description=vpnuser['description'],
                    admin_state_up=vpnuser['admin_state_up'],
                    password=vpnuser['password'],
                    created_at=timeutils.utcnow(),
                )
                context.session.add(vpnuser_db)
        except db_exc.DBDuplicateEntry:
            raise vpnuser_ext.DuplicateVPNUsername(name=vpnuser['name'])
        if self.vpnuser_notifier:
            self.vpnuser_notifier.notify_user_change(context)
        return self._make_vpnuser_dict(vpnuser_db)

    def update_vpnuser(self, context, vpnuser_id, vpnuser):
        vpnuser = vpnuser['vpnuser']
        vpnuser.pop('created_at', None)
        with context.session.begin(subtransactions=True):
            vpnusern_db = self._get_by_id(
                context,
                VPNuser,
                vpnuser_id)
            vpnusern_db.update(vpnuser)
        result = self._make_vpnuser_dict(vpnusern_db)
        if self.vpnuser_notifier:
            self.vpnuser_notifier.notify_user_change(context)
        return result

    def delete_vpnuser(self, context, vpnuser_id):
        with context.session.begin(subtransactions=True):
            vpnusern_db = self._get_by_id(
                context,
                VPNuser,
                vpnuser_id)
            context.session.delete(vpnusern_db)
        if self.vpnuser_notifier:
            self.vpnuser_notifier.notify_user_change(context)

    def get_vpnuser(self, context, vpnuser_id, fields=None):
        vpnusern_db = self._get_by_id(context, VPNuser, vpnuser_id)
        return self._make_vpnuser_dict(
            vpnusern_db, fields)

    def get_vpnusers(self, context, filters=None, fields=None):
        return self._get_collection(context, VPNuser,
                                    self._make_vpnuser_dict,
                                    filters=filters, fields=fields)
