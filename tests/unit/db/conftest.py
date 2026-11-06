"""Fixtures for the database-layer unit tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

from esmporium.db import (
    METADATA,
    Dataset,
    DatasetVersion,
    configure_sqlite_for_concurrency,
    flushed_id,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sqlalchemy import Engine


@pytest.fixture
def engine() -> Iterator[Engine]:
    """
    Get an in-memory SQLite engine with our schema created

    `StaticPool` keeps every connection pointed at the one in-memory database, so
    the observer's per-call sessions all see the same tables (a plain in-memory
    SQLite gives each connection its own, separate database). The schema is built
    with `create_all` rather than migrations: the migrations are covered by
    `tests/integration/test_migrations.py`, and here we only need the tables.
    """
    engine = configure_sqlite_for_concurrency(
        create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    )
    METADATA.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def a_dataset_version() -> Callable[..., int]:
    """
    Get a factory which stores one dataset and a version of it, and gives back its id

    The facets are whatever is needed to make a valid row, except the ones a test
    names. Tests about parents care which simulation a dataset belongs to and not
    about the rest, so the rest has a default.
    """

    def factory(  # noqa: PLR0913 - one argument per facet a test may want to set
        session: Session,
        experiment: str = "abrupt-4xCO2",
        variable: str = "tas",
        variant_label: str = "r1i1p1f1",
        model: str = "UKESM1-3-LL",
        project: str = "CMIP7",
        institution: str = "UKNCSP",
        reporting_interval: str = "mon",
        grid_label: str | None = "g126",
        processing_id: str = "tavg-h2m-hxy-u",
        version: str = "20261008",
    ) -> int:
        dataset = Dataset(
            id_project_specific=(
                f"{project}.{model}.{experiment}.{variant_label}.{variable}"
                f".{reporting_interval}.{grid_label}.{processing_id}"
            ),
            project=project,
            model=model,
            institution=institution,
            experiment=experiment,
            variant_label=variant_label,
            variable=variable,
            reporting_interval=reporting_interval,
            grid_label=grid_label,
            processing_id=processing_id,
        )
        session.add(dataset)
        session.flush()
        dataset_version = DatasetVersion(
            dataset_id=flushed_id(dataset),
            version=version,
            is_latest=True,
            retracted=False,
        )
        session.add(dataset_version)
        session.flush()

        return flushed_id(dataset_version)

    return factory
