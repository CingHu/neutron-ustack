# Copyright 2013 OpenStack Foundation
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

"""Add a unique constraint on (tenant_id, name) for vpnuser.

Revision ID: efcfc175aca4
Revises: efcfc174aca4
Create Date: 2014-08-22 13:30:28.148680

"""

revision = 'efcfc175aca4'
down_revision = 'efcfc174aca4'

migration_for_plugins = [
    '*'
]

from alembic import op

from neutron.db import migration


TABLE_NAME = 'vpnusers'
UC_NAME = 'uniq_vpnusername0tenant_id0name'


def upgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return

    op.create_unique_constraint(
        name=UC_NAME,
        source=TABLE_NAME,
        local_cols=['tenant_id', 'name']
    )


def downgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return

    op.drop_constraint(
        name=UC_NAME,
        table_name=TABLE_NAME,
        type_='unique'
    )
