"""Email verification for open signup.

Adds the canonical ``email_key`` and ``email_verified_at`` to users and
admits the ``verify`` account-token kind (code 3) used by verification links.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("email_key", sa.String(254), nullable=True))
        batch_op.add_column(sa.Column("email_verified_at", sa.Integer(), nullable=True))
        batch_op.create_index("ix_users_email_key", ["workspace_id", "email_key"])
    with op.batch_alter_table("account_tokens") as batch_op:
        batch_op.drop_constraint("ck_account_tokens_kind", type_="check")
        batch_op.create_check_constraint("ck_account_tokens_kind", "kind IN (1, 2, 3)")


def downgrade() -> None:
    with op.batch_alter_table("account_tokens") as batch_op:
        batch_op.drop_constraint("ck_account_tokens_kind", type_="check")
        batch_op.create_check_constraint("ck_account_tokens_kind", "kind IN (1, 2)")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_index("ix_users_email_key")
        batch_op.drop_column("email_verified_at")
        batch_op.drop_column("email_key")
