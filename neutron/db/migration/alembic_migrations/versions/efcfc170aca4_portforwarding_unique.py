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

"""Add a unique constraint on portforwarding rule

Revision ID: efcfc170aca4
Revises: efcfc169aca4
Create Date: 2014-04-27 18:35:28.148680

"""

revision = 'efcfc170aca4'
down_revision = 'efcfc169aca4'

migration_for_plugins = [
    '*'
]

from alembic import op
from sqlalchemy import exc

from neutron.db import migration


TABLE_NAME = 'portforwardingrules'
UC_NAME_1 = 'uniq_pf_rule_10router_id0protocol0outside_port'
UC_NAME_2 = 'uniq_pf_rule_20router_id0protocol0inside_addr0inside_port'


def upgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return
    try:
        op.create_unique_constraint(
            name=UC_NAME_1,
            source=TABLE_NAME,
            local_cols=['router_id',
                        'protocol',
                        'outside_port', ]
        )
    except exc.OperationalError as e:
        if 1061 == e.orig.args[0]:
            pass
        else:
            raise
    op.create_unique_constraint(
        name=UC_NAME_2,
        source=TABLE_NAME,
        local_cols=['router_id',
                    'protocol',
                    'inside_addr',
                    'inside_port', ]
    )
    try:
        op.drop_constraint(
            name='outside_port',
            table_name=TABLE_NAME,
            type_='unique'
        )
    except exc.OperationalError:
        pass


def downgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return
    op.drop_constraint(
        name=UC_NAME_2,
        table_name=TABLE_NAME,
        type_='unique'
    )
    try:
        op.drop_constraint(
            name=UC_NAME_1,
            table_name=TABLE_NAME,
            type_='unique'
        )
    except exc.OperationalError:
        pass
