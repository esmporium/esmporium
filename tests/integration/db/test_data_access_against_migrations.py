"""
Files and data-access routes against a migrated database

The unit tests in `tests/unit/test_schema.py` build the schema with `create_all`,
which is what our models say it should be. This checks the two new tables against
what the *migrations* actually produce, which is what a real database has.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from esmporium.datasets import DATASET_FACET_COLUMNS, AccessService, ContainerFormat
from esmporium.db.migrate import upgrade_to_head
from esmporium.db.schema import (
    DataAccess,
    DataNode,
    Dataset,
    DatasetVersion,
    File,
)


def _a_version(session: Session) -> int:
    """Store a dataset and one version of it, and give back the version's id."""
    facets = dict.fromkeys(DATASET_FACET_COLUMNS, "placeholder")
    facets.update(project="CMIP6", variable="tas", grid_label="gn")

    dataset = Dataset(id_project_specific="a-dataset", **facets)
    session.add(dataset)
    session.flush()

    version = DatasetVersion(
        dataset_id=dataset.id,
        version="20200101",
        is_latest=True,
        retracted=False,
    )
    session.add(version)
    session.flush()

    return version.id


def test_a_file_and_both_kinds_of_route_store_in_a_migrated_database(engine):
    """
    A file, a route to it, and a whole-version route all round-trip

    The whole-version route is the interesting one: it has a NULL `file_id`, which
    is the discriminator the model rests on, and a migration that made the column
    NOT NULL would be caught only here.
    """
    upgrade_to_head(engine)

    with Session(engine) as session:
        version_id = _a_version(session)
        node = DataNode(data_node="ceda.ac.uk")
        session.add(node)
        session.flush()

        stored_file = File(
            dataset_version_id=version_id,
            filename="tas_Amon_185001-194912.nc",
            size=3_000_000_000,
            checksum="bacddda94d64f75f9832b163929d0c099c6c320805df204a97f83fb15fb76884",
            checksum_type="SHA256",
            tracking_id="hdl:21.14100/84ce33af-1a76-4f66-b9eb-c222df3da596",
        )
        session.add(stored_file)
        session.flush()

        session.add(
            DataAccess(
                dataset_version_id=version_id,
                file_id=stored_file.id,
                data_node_id=node.id,
                service=AccessService.HTTPS,
                container_format=ContainerFormat.NETCDF,
                url="https://dap.ceda.ac.uk/.../tas_Amon_185001-194912.nc",
                raw_json="{}",
            )
        )
        session.add(
            DataAccess(
                dataset_version_id=version_id,
                file_id=None,
                data_node_id=node.id,
                service=AccessService.HTTPS,
                container_format=ContainerFormat.KERCHUNK,
                url="https://esgf-node.ornl.gov/.../tas.kerchunk.parq",
                raw_json="{}",
            )
        )
        session.commit()

        read_back = session.exec(select(File)).one()
        # BIGINT survived the migration, not just the model.
        assert read_back.size == 3_000_000_000
        assert read_back.checksum_type == "SHA256"

        routes = {
            route.container_format: route
            for route in session.exec(select(DataAccess)).all()
        }
        assert routes[ContainerFormat.NETCDF].file_id == read_back.id
        assert routes[ContainerFormat.KERCHUNK].file_id is None


def test_the_route_identity_holds_in_a_migrated_database(engine):
    """
    Re-recording the same whole-version route is refused, not duplicated

    This is the case a naive identity would have got wrong: `file_id` is NULL on both
    rows, and SQLite counts NULLs as distinct, so an identity mentioning `file_id`
    would have let the same route in twice on every ingest.
    """
    upgrade_to_head(engine)

    with Session(engine) as session:
        version_id = _a_version(session)
        node = DataNode(data_node="ceda.ac.uk")
        session.add(node)
        session.flush()

        def kerchunk_route() -> DataAccess:
            return DataAccess(
                dataset_version_id=version_id,
                file_id=None,
                data_node_id=node.id,
                service=AccessService.HTTPS,
                container_format=ContainerFormat.KERCHUNK,
                url="https://esgf-node.ornl.gov/.../tas.kerchunk.parq",
                raw_json="{}",
            )

        session.add(kerchunk_route())
        session.commit()

        session.add(kerchunk_route())
        with pytest.raises(IntegrityError):
            session.commit()
