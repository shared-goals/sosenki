"""add auction bids

Revision ID: 2f3b8c2a1c10
Revises: c5aabb9221f4
Create Date: 2026-01-15 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "2f3b8c2a1c10"
down_revision = "c5aabb9221f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Convert sale_price from Numeric(15,2) to Integer
    # SQLite doesn't support ALTER COLUMN TYPE, so use batch mode to recreate table
    with op.batch_alter_table("properties", schema=None) as batch_op:
        batch_op.alter_column(
            "sale_price",
            type_=sa.Integer(),
            existing_type=sa.Numeric(precision=15, scale=2),
            existing_nullable=True,
            comment="Selling price of the property (integer price value)",
        )

    # Create auction_bids table
    op.create_table(
        "auction_bids",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("property_id", sa.Integer(), nullable=False),
        sa.Column("investor_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(["property_id"], ["properties.id"]),
        sa.ForeignKeyConstraint(["investor_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        "idx_auction_bid_property_status",
        "auction_bids",
        ["property_id", "status"],
        unique=False,
    )
    op.create_index(
        "idx_auction_bid_investor_status",
        "auction_bids",
        ["investor_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_auction_bid_investor_status", table_name="auction_bids")
    op.drop_index("idx_auction_bid_property_status", table_name="auction_bids")
    op.drop_table("auction_bids")

    # Revert sale_price from Integer back to Numeric
    with op.batch_alter_table("properties", schema=None) as batch_op:
        batch_op.alter_column(
            "sale_price",
            type_=sa.Numeric(precision=15, scale=2),
            existing_type=sa.Integer(),
            existing_nullable=True,
        )
