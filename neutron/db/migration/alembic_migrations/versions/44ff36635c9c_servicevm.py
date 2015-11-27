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

"""Add serviceVM db

Revision ID: 44ff36635c9c
Revises: 522615534122
Create Date: 2015-05-26 06:13:41.240283

"""

# revision identifiers, used by Alembic.
revision = '44ff36635c9c'
down_revision = '522615534122'

from alembic import op
import sqlalchemy as sa

from neutron.db import migration
from neutron.common import core as sql

migration_for_plugins = [
             '*'
]
 

def upgrade(active_plugins=None, options=None):
    op.create_table(
        'devicetemplates',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=255), nullable=True),
        sa.Column('name', sa.String(length=255), nullable=True),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.Column('infra_driver', sa.String(length=255), nullable=True),
        sa.Column('mgmt_driver', sa.String(length=255), nullable=True),
        sa.Column('device_driver', sa.String(length=255), nullable=True),
        sa.Column('shared', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'devicetemplateattributes',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('template_id', sa.String(length=36), nullable=False),
        sa.Column('key', sa.String(length=255), nullable=False),
        #sa.Column('value', sa.String(length=4096), nullable=True),
        sa.Column('value', sql.JsonCom(), nullable=True),
        sa.ForeignKeyConstraint(['template_id'], ['devicetemplates.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'servicetypes',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('template_id', sa.String(length=36), nullable=False),
        sa.Column('servicetype', sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(['template_id'], ['devicetemplates.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'devices',
        sa.Column('id', sa.String(length=255), nullable=False),
        sa.Column('tenant_id', sa.String(length=255), nullable=True),
        sa.Column('name', sa.String(length=255), nullable=True),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.Column('template_id', sa.String(length=36), nullable=True),
        sa.Column('instance_id', sa.String(length=255), nullable=True),
        sa.Column('mgmt_url', sql.JsonCom(), nullable=True),
        sa.Column('status', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['template_id'], ['devicetemplates.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'deviceattributes',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('device_id', sa.String(length=255)),
        sa.Column('key', sa.String(length=255), nullable=False),
        #sa.Column('value', sa.String(length=4096), nullable=True),
        sa.Column('value', sql.JsonCom(), nullable=True),
        sa.ForeignKeyConstraint(['device_id'], ['devices.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'serviceinstances',
        sa.Column('id', sa.String(length=255), nullable=False),
        sa.Column('tenant_id', sa.String(length=255), nullable=True),
        sa.Column('service_type_id', sa.String(length=255), nullable=True),
        sa.Column('servicetype', sa.String(length=255), nullable=False),
        sa.Column('service_table_id', sa.String(length=255), nullable=True),
        sa.Column('name', sa.String(length=255), nullable=True),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.Column('mgmt_url', sql.JsonCom(), nullable=True),
        sa.Column('mgmt_driver', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=255), nullable=True),
        sa.Column('managed_by_user', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['service_type_id'], ['servicetypes.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'serviceinstanceattributes',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('service_instance_id', sa.String(length=255)),
        sa.Column('key', sa.String(length=255), nullable=False),
        #sa.Column('value', sa.String(length=4096), nullable=True),
        sa.Column('value', sql.JsonCom(), nullable=True),
        sa.ForeignKeyConstraint(['service_instance_id'], 
                           ['serviceinstances.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'servicedevicebindings',
        sa.Column('service_instance_id', sa.String(length=255)),
        sa.Column('device_id', sa.String(length=255)),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['service_instance_id'], 
                           ['serviceinstances.id'], ),
        sa.ForeignKeyConstraint(['device_id'], ['devices.id'], ),
        sa.PrimaryKeyConstraint('service_instance_id'),
    )




def downgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return

    op.drop_table('servicedevicebindings')
    op.drop_table('serviceinstanceattributes')
    op.drop_table('serviceinstances')
    op.drop_table('deviceattributes')
    op.drop_table('devices')
    op.drop_table('servicetypes')
    op.drop_table('devicetemplateattributes')
    op.drop_table('devicetemplates')
