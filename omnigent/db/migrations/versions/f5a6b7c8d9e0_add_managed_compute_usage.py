"""Add per-generation managed sandbox compute usage for metering and enforcement."""

import sqlalchemy as sa
from alembic import op

revision = "f5a6b7c8d9e0"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_compute_usage",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("host_id", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("sandbox_id", sa.String(256), nullable=False),
        sa.Column("funding", sa.String(16), nullable=False),
        sa.Column("started_at", sa.Integer(), nullable=False),
        sa.Column("last_active_at", sa.Integer(), nullable=False),
        sa.Column("deadline_at", sa.Integer(), nullable=True),
        sa.Column("ended_at", sa.Integer(), nullable=True),
        sa.Column("end_reason", sa.String(16), nullable=True),
        sa.Column("trial_charge_id", sa.String(64), nullable=True),
        sa.Column("billed_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.CheckConstraint(
            "funding IN ('deployment', 'trial', 'billing')",
            name="ck_managed_compute_usage_funding",
        ),
    )
    op.create_index(
        "ix_managed_compute_usage_open", "managed_compute_usage", ["ended_at", "workspace_id"]
    )
    op.create_index(
        "ix_managed_compute_usage_user",
        "managed_compute_usage",
        ["workspace_id", "user_id", "started_at"],
    )
    op.create_index(
        "ix_managed_compute_usage_sandbox",
        "managed_compute_usage",
        ["workspace_id", "provider", "sandbox_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_managed_compute_usage_sandbox", table_name="managed_compute_usage")
    op.drop_index("ix_managed_compute_usage_user", table_name="managed_compute_usage")
    op.drop_index("ix_managed_compute_usage_open", table_name="managed_compute_usage")
    op.drop_table("managed_compute_usage")
