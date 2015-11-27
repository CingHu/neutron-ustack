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

Revision ID: 566055906098
Revises: e28dc49e9e4
Create Date: 2015-01-30 04:53:20.101937

"""

# revision identifiers, used by Alembic.
revision = '566055906098'
down_revision = 'e28dc49e9e4'

from alembic import op
import sqlalchemy as sa

migration_for_plugins = [
    '*',
]

from neutron.db import migration

def upgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return
    op.add_column('subnets', sa.Column(u'uos_service_provider', sa.String(255), nullable=True))
    op.add_column('floatingips', sa.Column(u'uos_service_provider', sa.String(255), nullable=True))


def downgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return
    op.drop_column('subnets', 'uos_service_provider')
    op.drop_column('floatingips', 'uos_service_provider')
