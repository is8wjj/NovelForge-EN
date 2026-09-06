"""budget dispatch and repair reservations

Revision ID: 0006_budget_dispatch
Revises: 0005_budget_ledger
Create Date: 2026-09-06 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006_budget_dispatch'
down_revision: Union[str, None] = '0005_budget_ledger'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('autonomousnoveljob', schema=None) as batch_op:
        batch_op.add_column(sa.Column('reserved_repair_calls', sa.Integer(), server_default='0', nullable=False))

    with op.batch_alter_table('budgetreservation', schema=None) as batch_op:
        batch_op.add_column(sa.Column('dispatched_at', sa.DateTime(), nullable=True))

    # Conservative treatment for pre-existing open rows:
    # If dispatch status cannot be known, treat open rows as dispatched so recovery never
    # restores capacity that might have been dispatched.
    op.execute("UPDATE budgetreservation SET status = 'dispatched' WHERE status = 'open'")


def downgrade() -> None:
    with op.batch_alter_table('budgetreservation', schema=None) as batch_op:
        batch_op.drop_column('dispatched_at')

    with op.batch_alter_table('autonomousnoveljob', schema=None) as batch_op:
        batch_op.drop_column('reserved_repair_calls')
