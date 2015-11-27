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

"""add column table for lbaas

Revision ID: 30bc2ee7f736
Revises: f70aef653b1
Create Date: 2014-12-05 04:59:28.061959

"""

# revision identifiers, used by Alembic.
revision = '30bc2ee7f736'
down_revision = 'f70aef653b1'

from alembic import op
import sqlalchemy as sa

migration_for_plugins = [
    '*',
]

from neutron.db import migration

def upgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return

    op.add_column('lbaas_loadbalancers', sa.Column(u'created_at', sa.DateTime(), nullable=True))
    op.add_column('lbaas_listeners', sa.Column(u'created_at', sa.DateTime(), nullable=True))
    op.add_column('lbaas_pools', sa.Column(u'created_at', sa.DateTime(), nullable=True))
    op.add_column('lbaas_l7policies', sa.Column(u'created_at', sa.DateTime(), nullable=True))

def downgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return

    op.drop_column('lbaas_loadbalancers', 'created_at')
    op.drop_column('lbaas_listeners', 'created_at')
    op.drop_column('lbaas_pools', 'created_at')
    op.drop_column('lbaas_l7policies', 'created_at')
