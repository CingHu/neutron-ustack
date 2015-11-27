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

Revision ID: 39ef5458c0fd
Revises: 26dc186e747d
Create Date: 2015-09-18 03:23:13.224430

"""

# revision identifiers, used by Alembic.
revision = '39ef5458c0fd'
down_revision = '26dc186e747d'

from alembic import op
import sqlalchemy as sa

from neutron.db import migration

migration_for_plugins = [
    '*'
]


def upgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return
    #op.add_column('floatingips', sa.Column('servicevm_device', sa.String(255),
    #                                 nullable=False, default='none'))


def downgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return
    #op.drop_column('ports', 'servicevm_device')
