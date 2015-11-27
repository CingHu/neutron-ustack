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

"""empty message

Revision ID: 2ecd9645aad4
Revises: 204d17871a7f
Create Date: 2015-04-17 06:23:43.723135

"""

# revision identifiers, used by Alembic.
revision = '2ecd9645aad4'
down_revision = '204d17871a7f'

from alembic import op
import sqlalchemy as sa

migration_for_plugins = [
    '*',
]

from neutron.db import migration

def upgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return
    op.add_column('lbaas_listeners', sa.Column(u'keep_alive', sa.Boolean(), nullable=False))


def downgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return
    op.drop_column('lbaas_listeners', 'keep_alive')
