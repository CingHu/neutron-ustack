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

Revision ID: 464d77f5b4d9
Revises: 39ef5458c0fd
Create Date: 2015-09-18 04:00:57.637891

"""

# revision identifiers, used by Alembic.
revision = '464d77f5b4d9'
down_revision = '39ef5458c0fd'

from alembic import op
import sqlalchemy as sa

from neutron.db import migration

migration_for_plugins = [
    '*'
]


def upgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return
    #op.add_column('ports', sa.Column('servicevm_device', sa.String(255),
    #                                 nullable=True, default='none'))
    #op.add_column('ports', sa.Column('servicevm_type', sa.String(255),
    #                                 nullable=True, default='none'))
    #op.add_column('floatingips', sa.Column('servicevm_device', sa.String(255),
    #                                 nullable=True, default='none'))


def downgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return
    #op.drop_column('ports', 'servicevm_device')
    #op.drop_column('ports', 'servicevm_type')
    #op.drop_column('floatingips', 'servicevm_device')
