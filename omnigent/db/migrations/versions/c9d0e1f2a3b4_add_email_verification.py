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


_KIND_CHECK = "ck_account_tokens_kind"


def _replace_kind_check(expression: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "cockroachdb":
        with op.batch_alter_table("account_tokens") as batch_op:
            batch_op.drop_constraint(_KIND_CHECK, type_="check")
            batch_op.create_check_constraint(_KIND_CHECK, expression)
        return
    # CRDB frees a dropped constraint's name only at commit; the existence
    # check lets startup resume if it stops between the drop and the create.
    checks = sa.inspect(bind).get_check_constraints("account_tokens")
    if _KIND_CHECK in {check["name"] for check in checks}:
        with op.batch_alter_table("account_tokens") as batch_op:
            batch_op.drop_constraint(_KIND_CHECK, type_="check")
        bind.commit()
        bind.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
    with op.batch_alter_table("account_tokens") as batch_op:
        batch_op.create_check_constraint(_KIND_CHECK, expression)


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("users")}
    if "email_key" not in columns:
        with op.batch_alter_table("users") as batch_op:
            batch_op.add_column(sa.Column("email_key", sa.String(254), nullable=True))
            batch_op.add_column(sa.Column("email_verified_at", sa.Integer(), nullable=True))
            batch_op.create_index("ix_users_email_key", ["workspace_id", "email_key"])
    _replace_kind_check("kind IN (1, 2, 3)")


def downgrade() -> None:
    _replace_kind_check("kind IN (1, 2)")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_index("ix_users_email_key")
        batch_op.drop_column("email_verified_at")
        batch_op.drop_column("email_key")
