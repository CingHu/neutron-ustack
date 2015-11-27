# Copyright 2015 OpenStack Foundation
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

"""Implentment Tunnel as a Service

Revision ID: c4a558e0931
Revises: 566055906098
Create Date: 2015-02-17 04:23:14.301898

"""

# revision identifiers, used by Alembic.
revision = 'c4a558e0931'
down_revision = '566055906098'

from alembic import op
import sqlalchemy as sa


# Change to ['*'] if this migration applies to all plugins

migration_for_plugins = [
    'neutron.services.tunnel.plugin.TunnelDriverPlugin',
]

from neutron.db import migration


def upgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return

    op.create_table(
        'tunnels',
        sa.Column('tenant_id', sa.String(length=36), nullable=True),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=True),
        sa.Column('router_id', sa.String(length=36), nullable=True),
        sa.Column('mode', sa.String(length=255), nullable=True),
        sa.Column('type', sa.Integer(), nullable=True),
        sa.Column('local_subnet', sa.String(length=36), nullable=True),
        sa.Column('admin_state_up', sa.String(length=255), nullable=False),
        sa.Column('status', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['router_id'], ['routers.id']),
        mysql_charset='utf8',
    )



    op.create_table(
        'tunnelconnections',
        sa.Column('tenant_id', sa.String(length=36), nullable=True),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=255), nullable=False),
        sa.Column('tunnel_id', sa.String(length=36), nullable=True),
        sa.Column('remote_ip', sa.String(255), nullable=False),
        sa.Column('key', sa.String(length=255), nullable=True),
        sa.Column('key_type', sa.Integer(), nullable=True),
        sa.Column('checksum', sa.Integer, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['tunnel_id'], ['tunnels.id']),
        mysql_charset='utf8',
    )

    op.create_table(
        'targetnetworks',
        sa.Column('tenant_id', sa.String(length=36), nullable=True),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('network_cidr', sa.String(length=255), nullable=True),
        sa.Column('tunnel_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['tunnel_id'], ['tunnels.id']),
        mysql_charset='utf8',
    )


def downgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return

    op.drop_table('tunnelconnections')
    op.drop_table('targetnetworks')
    op.drop_table('tunnels')
