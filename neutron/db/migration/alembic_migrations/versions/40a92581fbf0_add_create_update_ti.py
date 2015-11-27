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

"""add create time

Revision ID: 40a92581fbf0
Revises: 27f0ae223a5b
Create Date: 2014-01-09 13:29:50.817938

"""

# revision identifiers, used by Alembic.
revision = '40a92581fbf0'
down_revision = '27f0ae223a5b'

# Change to ['*'] if this migration applies to all plugins

migration_for_plugins = [
    'neutron.plugins.ml2.plugin.Ml2Plugin'
]

from alembic import op
import sqlalchemy as sa

from neutron.db import migration


def upgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return

    ### commands auto generated by Alembic - please adjust! ###
    op.add_column('floatingips', sa.Column('created_at',
                                           sa.DateTime(), nullable=True))
    op.add_column('networks', sa.Column('created_at',
                                        sa.DateTime(), nullable=True))
    op.add_column('ports', sa.Column('created_at',
                                     sa.DateTime(), nullable=True))
    op.add_column('routers', sa.Column('created_at',
                                       sa.DateTime(), nullable=True))
    op.add_column('subnets', sa.Column('created_at',
                                       sa.DateTime(), nullable=True))
    op.add_column('securitygroups', sa.Column('created_at',
                                              sa.DateTime(), nullable=True))
    ### end Alembic commands ###


def downgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return

    ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('routers', 'created_at')
    op.drop_column('ports', 'created_at')
    op.drop_column('networks', 'created_at')
    op.drop_column('floatingips', 'created_at')
    op.drop_column('subnets', 'created_at')
    op.drop_column('securitygroups', 'created_at')
    ### end Alembic commands ###