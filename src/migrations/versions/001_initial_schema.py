"""Initial schema: Create all tables for MVP.

This is the only migration file - represents the current final schema for MVP.
Since we're not in production and backward compatibility is not required,
this is a fresh start reflecting all current models in src/models/.

Revision ID: 001_initial_schema
Revises:
Create Date: 2025-11-12 13:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create users table
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_id", sa.Integer(), nullable=True),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("is_investor", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("is_administrator", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("is_owner", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("is_staff", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("is_stakeholder", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("is_tenant", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column(
            "representative_id",
            sa.Integer(),
            nullable=True,
            comment="ID of the User who represents this user via Telegram (e.g., owner representing property manager)",
        ),
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
        sa.ForeignKeyConstraint(["representative_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sa.Index("ix_users_is_active", "is_active"),
        sa.Index("ix_users_representative_id", "representative_id"),
        sa.Index("ix_users_telegram_id", "telegram_id"),
        sa.Index("ix_users_username", "username"),
        sa.Index("ix_users_is_tenant", "is_tenant"),
    )

    # Create properties table
    op.create_table(
        "properties",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("property_name", sa.String(length=255), nullable=False),
        sa.Column("type", sa.String(length=100), nullable=True),
        sa.Column("share_weight", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column("is_ready", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("is_for_tenant", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column(
            "is_conservation",
            sa.Boolean(),
            nullable=False,
            server_default="1",
            comment="Whether property participates in conservation bills calculation",
        ),
        sa.Column("photo_link", sa.String(length=500), nullable=True),
        sa.Column("sale_price", sa.Numeric(precision=15, scale=2), nullable=True),
        sa.Column(
            "main_property_id",
            sa.Integer(),
            nullable=True,
            comment="ID of main property if this is an additional property",
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
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
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["main_property_id"],
            ["properties.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_properties_owner_id", "owner_id"),
        sa.Index("ix_properties_main_property_id", "main_property_id"),
        sa.Index("ix_properties_is_active", "is_active"),
        sa.Index("ix_properties_is_conservation", "is_conservation"),
    )

    # Create access_requests table
    op.create_table(
        "access_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_telegram_id", sa.Integer(), nullable=False, index=True),
        sa.Column("user_telegram_username", sa.String(length=255), nullable=True),
        sa.Column("request_message", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "approved", "rejected", native_enum=False),
            nullable=False,
            server_default="pending",
            index=True,
        ),
        sa.Column("admin_telegram_id", sa.Integer(), nullable=True, index=True),
        sa.Column("admin_response", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["admin_telegram_id"],
            ["users.telegram_id"],
        ),
        sa.Index("idx_user_status", "user_telegram_id", "status"),
        sa.Index("idx_status", "status"),
    )

    # Create accounts table (polymorphic: owner, staff, and organization accounts)
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "account_type",
            sa.String(length=50),
            nullable=False,
            server_default="organization",
            comment="Account type: 'owner', 'staff', or 'organization'",
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            nullable=True,
            comment="FK to User if account_type='owner' or 'staff' (1:1 relationship)",
        ),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("idx_account_name", "name"),
        sa.Index("idx_account_type", "account_type"),
        sa.Index("idx_account_user", "user_id", "account_type", unique=True),
    )

    # Create transactions table (unified account-to-account transactions)
    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("from_account_id", sa.Integer(), nullable=False),
        sa.Column("to_account_id", sa.Integer(), nullable=False),
        sa.Column(
            "service_period_id",
            sa.Integer(),
            nullable=True,
            comment="Associated service period",
        ),
        sa.Column(
            "amount",
            sa.Numeric(precision=10, scale=2),
            nullable=False,
            comment="Transaction amount in rubles",
        ),
        sa.Column(
            "transaction_date",
            sa.Date(),
            nullable=False,
            comment="Date of transaction",
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "budget_item_id",
            sa.Integer(),
            nullable=True,
            comment="Optional reference to budget item for expense categorization",
        ),
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
        sa.ForeignKeyConstraint(["from_account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["to_account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["service_period_id"], ["service_periods.id"]),
        sa.ForeignKeyConstraint(["budget_item_id"], ["budget_items.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("idx_transaction_from_account", "from_account_id"),
        sa.Index("idx_transaction_to_account", "to_account_id"),
        sa.Index("idx_transaction_from_to", "from_account_id", "to_account_id"),
        sa.Index("idx_transaction_period", "service_period_id"),
        sa.Index("idx_transaction_date", "transaction_date"),
        sa.Index("idx_transaction_budget_item", "budget_item_id"),
    )

    # Create service_periods table
    op.create_table(
        "service_periods",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False, unique=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("open", "closed", native_enum=False),
            nullable=False,
            server_default="open",
        ),
        sa.Column(
            "period_months",
            sa.Integer(),
            nullable=False,
            server_default="1",
            comment="Number of months in this period (1-12)",
        ),
        sa.Column(
            "year_budget",
            sa.Numeric(precision=10, scale=2),
            nullable=True,
            comment="Annual budget for MAIN bills (all non-conservation properties)",
        ),
        sa.Column(
            "conservation_year_budget",
            sa.Numeric(precision=10, scale=2),
            nullable=True,
            comment="Annual budget for CONSERVATION bills (conservation-flagged properties)",
        ),
        sa.Column(
            "electricity_start",
            sa.Numeric(precision=10, scale=2),
            nullable=True,
            comment="Starting electricity meter reading (kWh)",
        ),
        sa.Column(
            "electricity_end",
            sa.Numeric(precision=10, scale=2),
            nullable=True,
            comment="Ending electricity meter reading (kWh)",
        ),
        sa.Column(
            "electricity_multiplier",
            sa.Numeric(precision=10, scale=2),
            nullable=True,
            comment="Electricity consumption multiplier for calculation",
        ),
        sa.Column(
            "electricity_rate",
            sa.Numeric(precision=10, scale=2),
            nullable=True,
            comment="Electricity rate per kWh",
        ),
        sa.Column(
            "electricity_losses",
            sa.Numeric(precision=10, scale=2),
            nullable=True,
            comment="Electricity transmission losses ratio",
        ),
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
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_service_periods_name", "name", unique=True),
    )  # Create budget_items table
    op.create_table(
        "budget_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("expense_type", sa.String(length=255), nullable=False),
        sa.Column(
            "allocation_strategy",
            sa.Enum("proportional", "fixed_fee", "usage_based", "none", native_enum=False),
            nullable=False,
            server_default="none",
        ),
        sa.Column("year_budget", sa.Numeric(precision=10, scale=2), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
    )

    # Create electricity_readings table (polymorphic: user or property readings)
    op.create_table(
        "electricity_readings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "user_id",
            sa.Integer(),
            nullable=True,
            comment="FK to User for user-level readings (polymorphic with property_id)",
        ),
        sa.Column(
            "property_id",
            sa.Integer(),
            nullable=True,
            comment="FK to Property for property-level readings (polymorphic with user_id)",
        ),
        sa.Column(
            "reading_value",
            sa.Numeric(precision=10, scale=2),
            nullable=False,
            comment="Meter reading value",
        ),
        sa.Column(
            "reading_date",
            sa.Date(),
            nullable=False,
            comment="Date of the meter reading",
        ),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["property_id"], ["properties.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("idx_reading_user_date", "user_id", "reading_date"),
        sa.Index("idx_reading_property_date", "property_id", "reading_date"),
    )

    # Create bills table (unified: electricity, shared_electricity, conservation, main)
    op.create_table(
        "bills",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "service_period_id",
            sa.Integer(),
            nullable=False,
            comment="FK to ServicePeriod",
        ),
        sa.Column(
            "account_id",
            sa.Integer(),
            nullable=True,
            comment="FK to Account (user or organization account)",
        ),
        sa.Column(
            "property_id",
            sa.Integer(),
            nullable=True,
            comment="FK to Property for property-level bills",
        ),
        sa.Column(
            "bill_type",
            sa.String(length=50),
            nullable=False,
            comment="Type of bill: electricity, shared_electricity, conservation, or main",
        ),
        sa.Column(
            "bill_amount",
            sa.Numeric(precision=10, scale=2),
            nullable=False,
            comment="Bill amount in rubles",
        ),
        sa.Column(
            "comment",
            sa.String(length=500),
            nullable=True,
            comment="Optional comment (e.g., reason if property not found)",
        ),
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
        sa.ForeignKeyConstraint(["service_period_id"], ["service_periods.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["property_id"], ["properties.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("idx_bill_period_account", "service_period_id", "account_id"),
        sa.Index("idx_bill_period_property", "service_period_id", "property_id"),
        sa.Index("idx_bill_type", "bill_type"),
        sa.Index("idx_bill_period_type", "service_period_id", "bill_type"),
    )

    # Create audit_logs table (minimal audit logging for key entities)
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("changes", sa.JSON(), nullable=True),
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
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    # For MVP with no backward compatibility requirement, downgrade is not implemented.
    # To reset: use `make db-reset` to drop and recreate schema fresh.
    pass
