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

"""empty message

Revision ID: e28dc49e9e4
Revises: 130e35166f9d
Create Date: 2014-12-24 19:11:43.368566

"""

# revision identifiers, used by Alembic.
revision = 'e28dc49e9e4'
down_revision = '130e35166f9d'

from alembic import op
import sqlalchemy as sa

migration_for_plugins = [
    '*',
]

from neutron.db import migration

def upgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return
    op.add_column('lbaas_pools', sa.Column(u'network_id', sa.String(36), nullable=False))
    op.add_column('lbaas_pools', sa.Column(u'subnet_id', sa.String(36), nullable=True))


def downgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return
    op.drop_column('lbaas_pools', 'network_id')
    op.drop_column('lbaas_pools', 'subnet_id')
