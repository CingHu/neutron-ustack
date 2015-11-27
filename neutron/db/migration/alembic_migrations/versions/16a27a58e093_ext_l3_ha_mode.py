# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright 2014 OpenStack Foundation
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

"""ext_l3_ha_mode

Revision ID: 16a27a58e093
Revises: 4117d142767a
Create Date: 2014-02-01 10:24:12.412733

"""

# revision identifiers, used by Alembic.
revision = '16a27a58e093'
down_revision = '4117d142767a'

# Change to ['*'] if this migration applies to all plugins

migration_for_plugins = [
    'neutron.plugins.hyperv.hyperv_neutron_plugin.HyperVNeutronPlugin',
    'neutron.plugins.linuxbridge.lb_neutron_plugin.LinuxBridgePluginV2',
    'neutron.plugins.metaplugin.meta_neutron_plugin.MetaPluginV2',
    'neutron.plugins.ml2.plugin.Ml2Plugin',
    'neutron.plugins.nec.nec_plugin.NECPluginV2',
    'neutron.plugins.openvswitch.ovs_neutron_plugin.OVSNeutronPluginV2',
    'neutron.plugins.ryu.ryu_neutron_plugin.RyuNeutronPluginV2',
    'neutron.plugins.mlnx.mlnx_plugin.MellanoxEswitchPlugin',
    'neutron.services.l3_router.l3_router_plugin.L3RouterPlugin'
]

from alembic import op
import sqlalchemy as sa

from neutron.db import migration


def upgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return

    op.add_column('routers', sa.Column('ha_vr_id', sa.Integer())),

    op.create_table('ha_router_agent_port_bindings',
                    sa.Column('port_id', sa.String(length=36),
                              nullable=False),
                    sa.Column('router_id', sa.String(length=36),
                              nullable=False),
                    sa.Column('l3_agent_id', sa.String(length=36),
                              nullable=True),
                    sa.Column('priority', sa.Integer, default=50),
                    sa.PrimaryKeyConstraint('port_id'),
                    sa.ForeignKeyConstraint(['port_id'], ['ports.id'],
                                            ondelete='CASCADE'),
                    sa.ForeignKeyConstraint(['router_id'], ['routers.id'],
                                            ondelete='CASCADE'),
                    sa.ForeignKeyConstraint(['l3_agent_id'], ['agents.id']))

    op.create_table('ha_router_networks',
                    sa.Column('tenant_id', sa.String(length=255),
                              nullable=True),
                    sa.Column('id', sa.String(length=36), nullable=False),
                    sa.Column('network_id', sa.String(length=36),
                              nullable=False,
                              unique=True),
                    sa.ForeignKeyConstraint(['network_id'], ['networks.id'],
                                            ondelete='CASCADE'))


def downgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return

    op.drop_table('ha_router_networks')
    op.drop_table('ha_router_agent_port_bindings')
    op.drop_column('routers', 'ha_vr_id')
