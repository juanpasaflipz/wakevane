"""Record explicit ownership of packaged EGM templates and run snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "e3f4a5b6c7d8"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("managed_by", sa.String(32), nullable=True))


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM agents WHERE managed_by IS NOT NULL LIMIT 1"))
        .first()
    ):
        raise RuntimeError("Retain agent management while protected EGM rows exist")
    op.drop_column("agents", "managed_by")
