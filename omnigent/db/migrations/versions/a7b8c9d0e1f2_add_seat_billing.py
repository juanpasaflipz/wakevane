"""Add seat billing: grant kinds and markup, charge reporting, billing accounts and events.

Existing ``funded_trial_grants`` rows become ``kind='trial'`` with
``billing_period_end=0`` and no markup, so the one-trial-per-account rule
is preserved by the widened unique constraint.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7b8c9d0e1f2"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("funded_trial_grants") as batch_op:
        batch_op.add_column(
            sa.Column("kind", sa.String(16), nullable=False, server_default="trial")
        )
        batch_op.add_column(
            sa.Column("billing_period_end", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("markup_percent", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.drop_constraint("uq_funded_trial_grants_user", type_="unique")
        batch_op.create_unique_constraint(
            "uq_funded_trial_grants_user_kind_period",
            ["workspace_id", "user_id", "kind", "billing_period_end"],
        )
        batch_op.create_check_constraint(
            "ck_funded_trial_grants_kind", "kind IN ('trial', 'period_credit', 'overage')"
        )
    with op.batch_alter_table("funded_trial_charges") as batch_op:
        batch_op.add_column(sa.Column("reported_at", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("report_ref", sa.String(128), nullable=True))
    op.create_table(
        "billing_accounts",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("stripe_customer_id", sa.String(128), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(128), nullable=True),
        sa.Column("plan", sa.String(32), nullable=False),
        sa.Column("seat_status", sa.String(16), nullable=False, server_default="none"),
        sa.Column("seats", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("period_start", sa.Integer(), nullable=True),
        sa.Column("period_end", sa.Integer(), nullable=True),
        sa.Column("overage_cap_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("overage_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "user_id"),
        sa.CheckConstraint(
            "seat_status IN ('none', 'active', 'past_due', 'canceled')",
            name="ck_billing_accounts_seat_status",
        ),
        sa.CheckConstraint("overage_cap_micros >= 0", name="ck_billing_accounts_overage_cap"),
    )
    op.create_index(
        "ix_billing_accounts_customer", "billing_accounts", ["workspace_id", "stripe_customer_id"]
    )
    op.create_table(
        "billing_events",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", sa.String(128), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("received_at", sa.Integer(), nullable=False),
        sa.Column("processed_at", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )


def downgrade() -> None:
    op.drop_table("billing_events")
    op.drop_index("ix_billing_accounts_customer", table_name="billing_accounts")
    op.drop_table("billing_accounts")
    with op.batch_alter_table("funded_trial_charges") as batch_op:
        batch_op.drop_column("report_ref")
        batch_op.drop_column("reported_at")
    with op.batch_alter_table("funded_trial_grants") as batch_op:
        batch_op.drop_constraint("ck_funded_trial_grants_kind", type_="check")
        batch_op.drop_constraint("uq_funded_trial_grants_user_kind_period", type_="unique")
        batch_op.create_unique_constraint(
            "uq_funded_trial_grants_user", ["workspace_id", "user_id"]
        )
        batch_op.drop_column("markup_percent")
        batch_op.drop_column("billing_period_end")
        batch_op.drop_column("kind")
