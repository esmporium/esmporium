"""decouple version from bundle, dedup nodes

Revision ID: b35e5c5503c9
Revises: 2ecbccb2bbc1
Create Date: 2026-09-07 16:09:43.286962

This rework changes primary keys and foreign keys on the result-linking tables:
`DatasetVersionSpecific` gains a plain integer `id` primary key (dropping the string
`version_id = f"{id_project_specific}.v{version}"`) and a real `dataset_id` foreign key
so a version belongs to a single `Dataset`; `DatasetNodeInformation` is reduced to one
row per distinct data node; a new `DatasetVersionNodeLink` junction makes the version
<-> node relationship many-to-many; and the raw-doc link now points at the integer
`datasetversionspecific.id`. SQLite cannot ALTER primary/foreign keys in place, so
these tables are dropped and recreated rather than altered. That is safe here: at this
revision the tables hold only local/dev data, so no rows need preserving.
`datasetrawdoc` only loses its `search_host` column, which a batch alter handles.
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

revision: str = "b35e5c5503c9"
down_revision: str | None = "2ecbccb2bbc1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this migration"""
    # Drop the linking tables that reference the string version key (children first),
    # then drop the version table itself.
    op.drop_table("rawdocversionlink")
    op.drop_table("datasetnodeinformation")
    op.drop_table("datasetversionspecific")

    # No table references datasetrawdoc now, so recreating it (to drop search_host) is
    # safe.
    with op.batch_alter_table("datasetrawdoc", schema=None) as batch_op:
        batch_op.drop_column("search_host")

    # Recreate the version table with an integer id PK and a real dataset_id FK.
    op.create_table(
        "datasetversionspecific",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("version", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("is_latest", sa.Boolean(), nullable=False),
        sa.Column("retracted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["dataset.id"],
            name=op.f("fk_datasetversionspecific_dataset_id_dataset"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_datasetversionspecific")),
        sa.UniqueConstraint(
            "dataset_id",
            "version",
            name=op.f("uq_datasetversionspecific_dataset_id_version"),
        ),
    )
    with op.batch_alter_table("datasetversionspecific", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_datasetversionspecific_dataset_id"),
            ["dataset_id"],
            unique=False,
        )

    # One row per distinct data node.
    op.create_table(
        "datasetnodeinformation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("data_node", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_datasetnodeinformation")),
    )
    with op.batch_alter_table("datasetnodeinformation", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_datasetnodeinformation_data_node"),
            ["data_node"],
            unique=True,
        )

    # Many-to-many junction between versions and nodes.
    op.create_table(
        "datasetversionnodelink",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dataset_version_id", sa.Integer(), nullable=False),
        sa.Column("node_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_version_id"],
            ["datasetversionspecific.id"],
            name=op.f(
                "fk_datasetversionnodelink_dataset_version_id_datasetversionspecific"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["datasetnodeinformation.id"],
            name=op.f("fk_datasetversionnodelink_node_id_datasetnodeinformation"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_datasetversionnodelink")),
        sa.UniqueConstraint(
            "dataset_version_id",
            "node_id",
            name=op.f("uq_datasetversionnodelink_dataset_version_id_node_id"),
        ),
    )
    with op.batch_alter_table("datasetversionnodelink", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_datasetversionnodelink_dataset_version_id"),
            ["dataset_version_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_datasetversionnodelink_node_id"),
            ["node_id"],
            unique=False,
        )

    # Raw-doc link now points at the integer version id.
    op.create_table(
        "rawdocversionlink",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("raw_id", sa.Integer(), nullable=False),
        sa.Column("dataset_version_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["raw_id"],
            ["datasetrawdoc.id"],
            name=op.f("fk_rawdocversionlink_raw_id_datasetrawdoc"),
        ),
        sa.ForeignKeyConstraint(
            ["dataset_version_id"],
            ["datasetversionspecific.id"],
            name=op.f("fk_rawdocversionlink_dataset_version_id_datasetversionspecific"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rawdocversionlink")),
        sa.UniqueConstraint(
            "raw_id",
            "dataset_version_id",
            name=op.f("uq_rawdocversionlink_raw_id_dataset_version_id"),
        ),
    )
    with op.batch_alter_table("rawdocversionlink", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_rawdocversionlink_raw_id"), ["raw_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_rawdocversionlink_dataset_version_id"),
            ["dataset_version_id"],
            unique=False,
        )


def downgrade() -> None:
    """Undo this migration"""
    op.drop_table("rawdocversionlink")
    op.drop_table("datasetversionnodelink")
    op.drop_table("datasetnodeinformation")
    op.drop_table("datasetversionspecific")

    with op.batch_alter_table("datasetrawdoc", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "search_host",
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default="",
            )
        )

    # Version table keyed on the string version_id, grouped by id_project_specific.
    op.create_table(
        "datasetversionspecific",
        sa.Column("version_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "id_project_specific", sqlmodel.sql.sqltypes.AutoString(), nullable=False
        ),
        sa.Column("version", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("is_latest", sa.Boolean(), nullable=False),
        sa.Column("retracted", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("version_id", name=op.f("pk_datasetversionspecific")),
    )
    with op.batch_alter_table("datasetversionspecific", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_datasetversionspecific_id_project_specific"),
            ["id_project_specific"],
            unique=False,
        )

    # One node row per (version, data node), with index_node and replica.
    op.create_table(
        "datasetnodeinformation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("data_node", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("index_node", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("replica", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["datasetversionspecific.version_id"],
            name=op.f("fk_datasetnodeinformation_version_id_datasetversionspecific"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_datasetnodeinformation")),
        sa.UniqueConstraint(
            "version_id",
            "data_node",
            name=op.f("uq_datasetnodeinformation_version_id_data_node"),
        ),
    )
    with op.batch_alter_table("datasetnodeinformation", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_datasetnodeinformation_version_id"),
            ["version_id"],
            unique=False,
        )

    # Raw-doc link keyed on the string version_id.
    op.create_table(
        "rawdocversionlink",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("raw_id", sa.Integer(), nullable=False),
        sa.Column("version_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.ForeignKeyConstraint(
            ["raw_id"],
            ["datasetrawdoc.id"],
            name=op.f("fk_rawdocversionlink_raw_id_datasetrawdoc"),
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["datasetversionspecific.version_id"],
            name=op.f("fk_rawdocversionlink_version_id_datasetversionspecific"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rawdocversionlink")),
        sa.UniqueConstraint(
            "raw_id", "version_id", name=op.f("uq_rawdocversionlink_raw_id_version_id")
        ),
    )
    with op.batch_alter_table("rawdocversionlink", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_rawdocversionlink_raw_id"), ["raw_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_rawdocversionlink_version_id"),
            ["version_id"],
            unique=False,
        )
