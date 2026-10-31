"""Add service-funded trial grants, charge ledger and audit events."""

import sqlalchemy as sa
from alembic import op

revision = "e4f5a6b7c8d9"
down_revision = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None


def _workspace_id() -> sa.Column:
    return sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0")


def upgrade() -> None:
    op.create_table(
        "funded_trial_grants",
        _workspace_id(),
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("capabilities", sa.String(256), nullable=False),
        sa.Column("allowance_micros", sa.BigInteger(), nullable=False),
        sa.Column("reserved_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("spent_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("max_concurrent_roots", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.CheckConstraint("allowance_micros >= 0", name="ck_funded_trial_grants_allowance"),
        sa.CheckConstraint("reserved_micros >= 0", name="ck_funded_trial_grants_reserved"),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_funded_trial_grants_user"),
    )
    op.create_table(
        "funded_trial_charges",
        _workspace_id(),
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("grant_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("root_session_id", sa.String(128), nullable=True),
        sa.Column("session_id", sa.String(128), nullable=True),
        sa.Column("reserved_micros", sa.BigInteger(), nullable=False),
        sa.Column("settled_micros", sa.BigInteger(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("settled_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.CheckConstraint(
            "kind IN ('inference', 'compute', 'storage')", name="ck_funded_trial_charges_kind"
        ),
        sa.CheckConstraint(
            "status IN ('reserved', 'settled', 'released')",
            name="ck_funded_trial_charges_status",
        ),
        sa.UniqueConstraint(
            "workspace_id", "grant_id", "idempotency_key", name="uq_funded_trial_charges_key"
        ),
    )
    op.create_index(
        "ix_funded_trial_charges_open",
        "funded_trial_charges",
        ["workspace_id", "grant_id", "status"],
    )
    op.create_table(
        "funded_trial_events",
        _workspace_id(),
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("grant_id", sa.String(64), nullable=True),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("actor_id", sa.String(128), nullable=True),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_funded_trial_events_user",
        "funded_trial_events",
        ["workspace_id", "user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_funded_trial_events_user", table_name="funded_trial_events")
    op.drop_table("funded_trial_events")
    op.drop_index("ix_funded_trial_charges_open", table_name="funded_trial_charges")
    op.drop_table("funded_trial_charges")
    op.drop_table("funded_trial_grants")
