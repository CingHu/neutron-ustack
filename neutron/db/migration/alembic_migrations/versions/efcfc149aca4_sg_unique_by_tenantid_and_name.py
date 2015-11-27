# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
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

"""Add a unique constraint on (tenant_id, name) columns for sg

Revision ID: efcfc149aca4
Revises: 51fbcd090f3e
Create Date: 2014-04-27 18:35:28.148680

"""

revision = 'efcfc149aca4'
down_revision = '51fbcd090f3e'

migration_for_plugins = [
    '*'
]

from alembic import op

from neutron.db import migration


TABLE_NAME = 'securitygroups'
UC_NAME = 'uniq_sg0tenant_id0name'


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
