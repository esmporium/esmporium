"""add search api call record table

Revision ID: dbb859b30d89
Revises: 069dd4eb9243
Create Date: 2026-08-27 11:05:57.314733

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

# SQLModel's string columns autogenerate as `sqlmodel.sql.sqltypes.AutoString`,
# so migration scripts need this importable even though it looks unused.
# The submodule is imported explicitly (rather than just `import sqlmodel`)
# so that type checkers can see where `AutoString` comes from.
import sqlmodel.sql.sqltypes
from alembic import op

revision: str = "dbb859b30d89"
down_revision: str | None = "069dd4eb9243"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this migration"""
    op.create_table(
        "searchapicallrecord",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("host", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("http_method", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("url", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("request_body", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("num_results", sa.Integer(), nullable=True),
        sa.Column("response_time_seconds", sa.Float(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_searchapicallrecord")),
    )
    with op.batch_alter_table("searchapicallrecord", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_searchapicallrecord_created_at"),
            ["created_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_searchapicallrecord_host"), ["host"], unique=False
        )


def downgrade() -> None:
    """Undo this migration"""
    with op.batch_alter_table("searchapicallrecord", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_searchapicallrecord_host"))
        batch_op.drop_index(batch_op.f("ix_searchapicallrecord_created_at"))

    op.drop_table("searchapicallrecord")
