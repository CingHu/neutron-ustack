# Copyright (C) 2014 eNovance SAS <licensing@enovance.com>
#
# Author: Sylvain Afchain <sylvain.afchain@enovance.com>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from oslo.config import cfg
import sqlalchemy as sa
from sqlalchemy import orm

from neutron.db import agents_db
from neutron.db import l3_db
from neutron.db import model_base
from neutron.db import models_v2
from neutron.openstack.common import log as logging


VR_ID_RANGE = set(range(1, 255))

LOG = logging.getLogger(__name__)

L3_HA_OPTS = [
    cfg.BoolOpt('l3_ha',
                default=False,
                help=_('Enable the HA mode of virtual routers')),
    cfg.IntOpt('l3_agents_per_router',
               default=2,
               help=_('number of agents on which a router will be '
                      'scheduled.')),
    cfg.StrOpt('l3_ha_net_cidr',
               default='169.254.0.0/16',
               help=_('Network address used for the l3 ha admin network.')),
]
cfg.CONF.register_opts(L3_HA_OPTS)

# Modify the Router Data Model adding the virtual router id
setattr(l3_db.Router, 'ha_vr_id',
        sa.Column(sa.Integer, nullable=True))


class L3HARouterAgentPortBinding(model_base.BASEV2):
    """Represent agent binding state of a ha router port.

    A HA Router has one HA port per agent on which it is spawned,
    This binding table stores which port is used for a HA router by an
    l3 agent.
    """

    __tablename__ = 'ha_router_agent_port_bindings'

    port_id = sa.Column(sa.String(36), sa.ForeignKey('ports.id',
                                                     ondelete='CASCADE'),
                        nullable=False, primary_key=True)
    port = orm.relationship(models_v2.Port)

    router_id = sa.Column(sa.String(36), sa.ForeignKey('routers.id',
                                                       ondelete='CASCADE'),
                          nullable=False)

    l3_agent_id = sa.Column(sa.String(36),
                            sa.ForeignKey("agents.id",
                                          ondelete='CASCADE'))
    agent = orm.relationship(agents_db.Agent)

    priority = sa.Column(sa.Integer, default=50)
    __table_args__ = (
        sa.UniqueConstraint('router_id', 'l3_agent_id',
                name='uniq_h3portl3agentbind0router_id0l3_agent_id'),)


class L3HARouterNetwork(model_base.BASEV2, models_v2.HasId,
                        models_v2.HasTenant):
    """Host HA Network for a tenant.

    One HA Network is used per tenant, all HA Router port are created
    on this type of network.
    """

    __tablename__ = 'ha_router_networks'

    network_id = sa.Column(sa.String(36),
                           sa.ForeignKey('networks.id', ondelete="CASCADE"),
                           nullable=False, unique=True)
    __table_args__ = (
        sa.UniqueConstraint('tenant_id',
                            name='uniq_l3hanet0tenant_id'),)
