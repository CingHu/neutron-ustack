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

"""Add device bindings and add power_state to devices

Revision ID: 2a05acc72b73
Revises: 44ff36635c9c
Create Date: 2015-08-13 02:37:06.692494

"""

# revision identifiers, used by Alembic.
revision = '2a05acc72b73'
down_revision = '44ff36635c9c'

from alembic import op
import sqlalchemy as sa

from neutron.db import migration

migration_for_plugins = [
    '*'
]

def upgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return

    ### commands for create and add new column
    op.create_table(
        'deviceagentbindings',
        sa.Column('device_id', sa.String(length=36), nullable=False),
        sa.Column('servicevm_agent_id', sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(['servicevm_agent_id'], ['agents.id'],
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['device_id'], ['devices.id'],
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('device_id', 'servicevm_agent_id')
    )

    op.add_column('devices',
                  sa.Column('power_state', sa.String(length=36), nullable=True))
    ## end Alembic commands


def downgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return

    ### commands for delete table and column
    op.drop_table('deviceagentbindings')
    op.drop_column('devices', 'power_state')
    ### end Alembic commands
