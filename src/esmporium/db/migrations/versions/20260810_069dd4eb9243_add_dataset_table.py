"""Add Dataset table

Revision ID: 069dd4eb9243
Revises:
Create Date: 2026-08-10 12:09:05.974686

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

revision: str = "069dd4eb9243"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this migration"""
    op.create_table(
        "dataset",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "id_project_specific", sqlmodel.sql.sqltypes.AutoString(), nullable=False
        ),
        sa.Column("project", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("model", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("institution", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("experiment", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("variant_label", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("variable", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "reporting_interval", sqlmodel.sql.sqltypes.AutoString(), nullable=False
        ),
        sa.Column("grid_label", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("processing_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset")),
        sa.UniqueConstraint(
            "id_project_specific", name=op.f("uq_dataset_id_project_specific")
        ),
    )


def downgrade() -> None:
    """Undo this migration"""
    op.drop_table("dataset")
