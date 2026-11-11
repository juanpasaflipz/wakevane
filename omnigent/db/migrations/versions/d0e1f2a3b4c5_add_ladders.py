"""Add the ladders and ladder_attempts tables.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4

A ladder works one kind of repository event with an ordered list of agents,
cheapest first; ``ladder_attempts`` records each session it started and how
the repository's checks judged it. Both are new tables at the current schema
state: ``workspace_id`` leads the primary key and there are no foreign keys
(schema Rule R032).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "d0e1f2a3b4c5"
down_revision: str | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``ladders`` and ``ladder_attempts`` with their indexes."""
    op.create_table(
        "ladders",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("account_generation", sa.String(32), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("repo", sa.String(256), nullable=False),
        sa.Column("trigger_kind", sa.String(128), nullable=False),
        sa.Column("trigger_match", sa.Text(), nullable=False),
        sa.Column("prompt_template", sa.Text(), nullable=False),
        sa.Column("rungs", sa.Text(), nullable=False),
        sa.Column("verify_kind", sa.String(128), nullable=False),
        sa.Column("verify_name", sa.String(256), nullable=True),
        sa.Column("execution_target", sa.String(32), nullable=False),
        sa.Column("host_id", Uuid16(), nullable=True),
        sa.Column("workspace", sa.String(2048), nullable=True),
        sa.Column("attempt_timeout_minutes", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("state IN ('active', 'paused', 'deleted')", name="ck_ladders_state"),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index("ix_ladders_active_lookup", "ladders", ["state", "trigger_kind"])
    op.create_index(
        "ix_ladders_user_scope", "ladders", ["workspace_id", "user_id", "created_at", "id"]
    )
    op.create_table(
        "ladder_attempts",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("ladder_id", Uuid16(), nullable=False),
        sa.Column("task_id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("repo", sa.String(256), nullable=False),
        sa.Column("task_key", sa.String(256), nullable=False),
        sa.Column("rung_index", sa.Integer(), nullable=False),
        sa.Column("agent_id", Uuid16(), nullable=False),
        sa.Column("model_override", sa.String(128), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("conversation_id", Uuid16(), nullable=True),
        sa.Column("pull_request", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("detail", sa.String(1024), nullable=True),
        sa.Column("event_fields", sa.Text(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("started_at", sa.BigInteger(), nullable=False),
        sa.Column("finished_at", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'passed', 'failed', 'timed_out', 'launch_failed')",
            name="ck_ladder_attempts_status",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index("ix_ladder_attempts_status", "ladder_attempts", ["status", "started_at"])
    op.create_index(
        "ix_ladder_attempts_ladder",
        "ladder_attempts",
        ["workspace_id", "ladder_id", "started_at", "id"],
    )
    op.create_index(
        "ix_ladder_attempts_user_scope",
        "ladder_attempts",
        ["workspace_id", "user_id", "started_at", "id"],
    )


def downgrade() -> None:
    """Drop ``ladder_attempts`` and ``ladders``."""
    op.drop_index("ix_ladder_attempts_user_scope", table_name="ladder_attempts")
    op.drop_index("ix_ladder_attempts_ladder", table_name="ladder_attempts")
    op.drop_index("ix_ladder_attempts_status", table_name="ladder_attempts")
    op.drop_table("ladder_attempts")
    op.drop_index("ix_ladders_user_scope", table_name="ladders")
    op.drop_index("ix_ladders_active_lookup", table_name="ladders")
    op.drop_table("ladders")
