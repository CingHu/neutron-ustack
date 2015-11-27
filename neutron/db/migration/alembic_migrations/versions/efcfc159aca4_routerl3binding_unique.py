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

"""Add a unique constraint on (router_id, l3_agent_id)
   columns to router l3 agent binding

Revision ID: efcfc159aca4
Revises: efcfc149aca4
Create Date: 2014-04-27 18:35:28.148680

"""

revision = 'efcfc159aca4'
down_revision = 'efcfc149aca4'

migration_for_plugins = [
    '*'
]

from alembic import op
from sqlalchemy import exc

from neutron.db import migration


TABLE_NAME = 'routerl3agentbindings'
UC_NAME = 'uniq_routerl3agentbind0router_id0l3_agent_id'


def upgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return
    #NOTE(gongysh) since router_id is a foreign key, add
    # this new index will replace the foreign key index,
    # if we want to drop the constraint, it will raise an error.
    # for us to replay the downgrade and upgrade, we have to
    # catch the duplicate key the second time we upgrade the DB
    try:
        op.create_unique_constraint(
            name=UC_NAME,
            source=TABLE_NAME,
            local_cols=['router_id', 'l3_agent_id']
        )
    except exc.OperationalError as e:
        if 1061 == e.orig.args[0]:
            pass
        else:
            raise


def downgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return
    #NOTE(gongysh) we will not be able to drop the constraint since we
    # replace the foreign key router_id with the new combined constraint
    try:
        op.drop_constraint(
            name=UC_NAME,
            table_name=TABLE_NAME,
            type_='unique'
        )
    except exc.OperationalError:
        pass
