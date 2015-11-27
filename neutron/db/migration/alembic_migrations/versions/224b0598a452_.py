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

"""Add attributes to ports and floatingips by servicevm

Revision ID: 224b0598a452
Revises: 464d77f5b4d9
Create Date: 2015-09-18 04:30:02.213043

"""

# revision identifiers, used by Alembic.
revision = '224b0598a452'
down_revision = '464d77f5b4d9'

from alembic import op
import sqlalchemy as sa

from neutron.common import core as sql
from neutron.db import migration

migration_for_plugins = [
    '*'
]


def upgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return
    op.add_column('ports', sa.Column('servicevm_device', sa.String(255),
                                     nullable=True, default='none'))
    op.add_column('ports', sa.Column('servicevm_type', sa.String(255),
                                     nullable=True, default='none'))
    op.add_column('ports', sa.Column('service_instance_id', sa.String(255),
                                     nullable=True, default='none'))
    op.add_column('floatingips', sa.Column('service_instance_id', sa.String(255),
                                     nullable=True, default='none'))
    op.add_column('devices', sa.Column('auth', sql.JsonCom(), nullable=True))
    #op.create_foreign_key(
    #    'ports_ibfk_2',
    #    source='ports',
    #    referent='devices',
    #    local_cols=['servicevm_device'],
    #    remote_cols=['id'],
    #)
    #op.create_foreign_key(
    #    'floatingips_ibfk_5',
    #    source='floatingips',
    #    referent='serviceinstances',
    #    local_cols=['service_instance_id'],
    #    remote_cols=['id'],
    #)


def downgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return
    #op.drop_constraint(
    #    name='ports_ibfk_2',
    #    table_name='ports',
    #    type_='foreignkey'
    #)
    op.drop_constraint(
        name='floatingips_ibfk_5',
        table_name='floatingips',
        type_='foreignkey'
    )
    op.drop_column('ports', 'servicevm_device')
    op.drop_column('ports', 'servicevm_type')
    op.drop_column('ports', 'service_instance_id')
    op.drop_column('devices', 'auth')
    op.drop_column('floatingips', 'service_instance_id')
