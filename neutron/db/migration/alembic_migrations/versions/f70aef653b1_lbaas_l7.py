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

"""lbaas l7

Revision ID: f70aef653b1
Revises: b30ae52b12b
Create Date: 2014-07-04 10:50:15.606420

"""

# revision identifiers, used by Alembic.
revision = 'f70aef653b1'
down_revision = 'b30ae52b12b'

# Change to ['*'] if this migration applies to all plugins

migration_for_plugins = [
    '*',
]

from alembic import op
import sqlalchemy as sa


from neutron.db import migration

l7rule_type = sa.Enum("HOST_NAME", "PATH", "FILE_TYPE", "HEADER", "COOKIE",
                      name="l7rule_typesv2")
l7rule_compare_type = sa.Enum("REGEX", "STARTS_WITH", "ENDS_WITH", "CONTAINS",
                              "EQUALS_TO", "GREATER_THAN", "LESS_THAN",
                              name="l7rule_compare_typesv2")
l7policy_action_type = sa.Enum("REJECT", "REDIRECT_TO_URL", "REDIRECT_TO_POOL",
                               name="l7policy_action_typesv2")


def upgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return

    op.create_table(
        u'lbaas_l7policies',
        sa.Column(u'tenant_id', sa.String(255), nullable=True),
        sa.Column(u'id', sa.String(36), nullable=False),
        sa.Column(u'name', sa.String(255), nullable=True),
        sa.Column(u'description', sa.String(255), nullable=True),
        sa.Column(u'listener_id', sa.String(36), nullable=False),
        sa.Column(u'action', l7policy_action_type, nullable=False),
        sa.Column(u'redirect_pool_id', sa.String(36), nullable=True),
        sa.Column(u'redirect_url', sa.String(255), nullable=True),
        sa.Column(u'position', sa.Integer),
        sa.Column(u'status', sa.String(16), nullable=False),
        sa.Column(u'admin_state_up', sa.Boolean(), nullable=False),
        sa.Column(u'redirect_url_drop_query', sa.Boolean(), nullable=True),
        sa.Column(u'redirect_url_code', sa.Integer, nullable=True),
        sa.PrimaryKeyConstraint(u'id'),
        sa.ForeignKeyConstraint([u'listener_id'],
                                [u'lbaas_listeners.id']),
        sa.ForeignKeyConstraint([u'redirect_pool_id'],
                                [u'lbaas_pools.id'])
    )

    op.create_table(
        u'lbaas_l7rules',
        sa.Column(u'tenant_id', sa.String(255), nullable=True),
        sa.Column(u'id', sa.String(36), nullable=False),
        sa.Column(u'l7policy_id', sa.String(36), nullable=False),
        sa.Column(u'type', l7rule_type, nullable=False),
        sa.Column(u'compare_type', l7rule_compare_type, nullable=False),
        sa.Column(u'key', sa.String(255), nullable=False),
        sa.Column(u'value', sa.String(255), nullable=True),
        sa.Column(u'status', sa.String(16), nullable=False),
        sa.Column(u'admin_state_up', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint(u'id'),
        sa.ForeignKeyConstraint([u'l7policy_id'],
                                [u'lbaas_l7policies.id'])
    )


def downgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return
    op.drop_table(u'lbaas_l7rules')
    l7rule_type.drop(op.get_bind(), checkfirst=False)
    l7rule_compare_type.drop(op.get_bind(), checkfirst=False)
    op.drop_table(u'lbaas_l7policies')
    l7policy_action_type.drop(op.get_bind(), checkfirst=False)
