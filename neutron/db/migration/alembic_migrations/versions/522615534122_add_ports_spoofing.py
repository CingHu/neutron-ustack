"""empty message

Revision ID: 522615534122
Revises: 204d17871a7f
Create Date: 2015-05-11 01:44:00.558820

"""

# revision identifiers, used by Alembic.
revision = '522615534122'
down_revision = '2ecd9645aad4'

from alembic import op
import sqlalchemy as sa

from neutron.db import migration

migration_for_plugins = [
    '*'
]


def upgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return
    op.add_column('ports', sa.Column('disable_anti_spoofing', sa.Boolean(),
                                     nullable=False, default=False))

    op.add_column('networks', sa.Column('unmanaged_network', sa.Boolean(),
                                      nullable=False, default=False))


def downgrade(active_plugins=None, options=None):
    if not migration.should_run(active_plugins, migration_for_plugins):
        return
    op.drop_column('ports', 'disable_anti_spoofing')

    op.drop_column('networks', 'unmanaged_network')
