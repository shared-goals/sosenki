"""add unique budget bill indexes

Revision ID: 4d1d3f9bc2a1
Revises: 2f3b8c2a1c10
Create Date: 2026-06-30 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "4d1d3f9bc2a1"
down_revision = "2f3b8c2a1c10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_bill_main_period_account",
        "bills",
        ["service_period_id", "account_id"],
        unique=True,
        sqlite_where=sa.text("bill_type = 'main'"),
        postgresql_where=sa.text("bill_type = 'main'"),
    )
    op.create_index(
        "uq_bill_conservation_period_account",
        "bills",
        ["service_period_id", "account_id"],
        unique=True,
        sqlite_where=sa.text("bill_type = 'conservation'"),
        postgresql_where=sa.text("bill_type = 'conservation'"),
    )


def downgrade() -> None:
    op.drop_index("uq_bill_conservation_period_account", table_name="bills")
    op.drop_index("uq_bill_main_period_account", table_name="bills")
