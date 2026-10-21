"""
Re-useable fixtures etc. for tests

See https://docs.pytest.org/en/7.1.x/reference/fixtures.html#conftest-py-sharing-fixtures-across-multiple-files
"""

import datetime
import types
import typing
from dataclasses import dataclass

import pandas as pd
import pytest

from esmporium.datasets import DATASET_FACET_COLUMNS
from esmporium.db import (
    ChangeRecord,
    Dataset,
    DatasetChangeInfo,
    RunHealth,
    SearchRunLeafInfo,
    SearchSummary,
    VersionRecord,
)
from esmporium.requirements import VersionFilter

OPT_IN_MARKERS: dict[str, str] = {
    "network": "a third-party service over the network",
    "hits_esgf_search_api": "the live ESGF search APIs",
}
"""
Markers whose tests only run when they are explicitly asked for

These tests depend on somebody else's server being up and unchanged,
so they are slow, they fail for reasons which have nothing to do with a change,
and a red run does not mean what a red run usually means.
Opting in keeps `pytest tests` about this repository,
and keeps the third-party checks somewhere they can be read as what they are.

`network` is the generic one, for network access which is not worth naming.
Anything we hit often enough to want to run, skip or debug on its own
gets its own marker instead, which is why the ESGF search API has one.

Give a test exactly one of these, the most specific one which applies.
Two would mean it only runs when both flags are given,
which is not what carrying two labels looks like it should mean.

Each entry is a marker name and what its tests talk to.
Adding another means adding a line here
and a line under `markers` in `pyproject.toml`.
"""


def opt_in_flag(marker: str) -> str:
    """
    Get the command line flag which turns a marker's tests on

    Parameters
    ----------
    marker
        The marker to get the flag for

    Returns
    -------
    :
        The flag, e.g. `--run-hits-esgf-search-api`
    """
    return f"--run-{marker.replace('_', '-')}"


def pytest_addoption(parser):
    """Add a flag for each opt-in marker"""
    for marker, talks_to in OPT_IN_MARKERS.items():
        parser.addoption(
            opt_in_flag(marker),
            action="store_true",
            default=False,
            help=f"Also run the tests which talk to {talks_to}",
        )


def pytest_collection_modifyitems(config, items):
    """Skip the tests of any opt-in marker which was not asked for"""
    for marker, talks_to in OPT_IN_MARKERS.items():
        flag = opt_in_flag(marker)
        if config.getoption(flag):
            continue

        skip = pytest.mark.skip(reason=f"Needs {flag}: this test talks to {talks_to}")
        for item in items:
            if marker in item.keywords:
                item.add_marker(skip)


def get_facet_value(column):
    """
    Get a placeholder value for a facet, of the right type for its column

    The type comes from the model's own declaration of the field,
    so this doesn't have to be kept in step with the model by hand.
    We read it off `model_fields` rather than off the table,
    because the columns SQLModel builds for strings are `AutoString`,
    a `TypeDecorator` that doesn't implement `python_type`,
    so the table can't tell us what a string column holds.

    What can't be derived is what a *value* of a given type should be,
    hence the hard-coded values below.
    Adding a facet of a type we already handle needs no change here.

    Parameters
    ----------
    column
        Name of the facet column to build a value for

    Returns
    -------
    :
        A value that the column will accept.

    Raises
    ------
    NotImplementedError
        We don't know how to make a value of this column's type yet.
    """
    facet_type = Dataset.model_fields[column].annotation

    # Unwrap an optional annotation (e.g. `grid_label` is `str | None`) down to its
    # underlying type, so a nullable facet is treated like its non-null counterpart.
    if isinstance(facet_type, types.UnionType) or (
        typing.get_origin(facet_type) is typing.Union
    ):
        non_none = [arg for arg in typing.get_args(facet_type) if arg is not type(None)]
        if len(non_none) == 1:
            facet_type = non_none[0]

    if facet_type is str:
        return f"{column}-value"

    if facet_type is bool:
        return False

    msg = (
        f"No placeholder value defined for {column!r}, "
        f"which is a {facet_type.__name__}. "
        "Add a branch for this type."
    )
    raise NotImplementedError(msg)


@pytest.fixture
def get_dataset_kwargs():
    """
    Get a factory for the keyword arguments of a dataset with every facet filled in

    The facet values are built from their column names and types,
    so adding a facet to the model doesn't mean editing every test that stores one.
    Tests that care about a particular value pass it by name.
    """

    def factory(label, **facets):
        # `id` is a surrogate the database assigns, so it is not set here. The
        # `label` seeds `id_project_specific`, which (with `variable`) is the row's
        # natural identity, so distinct labels give distinct datasets.
        return {
            "id_project_specific": f"{label}_ps",
            **{column: get_facet_value(column) for column in DATASET_FACET_COLUMNS},
            **facets,
        }

    return factory


@pytest.fixture
def reads_everything():
    """
    Get a wrapper which makes a catalogue ignore `needs_facets`

    What "the same answer with every stored document read" is measured against, and
    the stand-in for a catalogue which chooses not to implement the optimisation at
    all -- which is allowed: `needs_facets` says what will be read, not what must be
    skipped.

    It takes `needs_facets` so that it still fits
    [Catalogue][esmporium.requirements.Catalogue], and throws it away.

    Lives here rather than beside either set of tests because both the solver tests
    and the database ones need it, and two copies would be free to drift.
    """

    class ReadsEverything:
        """A catalogue which forwards every lookup as though nothing had been named"""

        def __init__(self, catalogue):
            self.catalogue = catalogue

        def find(
            self, query, *, versions=VersionFilter.NOT_RETRACTED, needs_facets=None
        ):
            """Find every dataset which matches a query, reading every document"""
            return self.catalogue.find(query, versions=versions)

    return ReadsEverything


REALISTIC_FACETS: dict[str, str] = {
    "project": "CMIP7",
    "institution": "CSIRO",
    "model": "ACCESS-ESM1-6",
    "experiment": "historical",
    "variant_label": "r1i1p1f1",
    "variable": "tas",
    "reporting_interval": "mon",
    "grid_label": "gn",
    "processing_id": "Amon",
}
"""
A plausible set of dataset facets, for the scenes whose facets get rendered

Spelled out rather than derived from the column names, so that a rendered table reads
like a real one. Every key of
[DATASET_FACET_COLUMNS][esmporium.datasets.DATASET_FACET_COLUMNS] is present, which
`test_realistic_facets_covers_every_column` pins.
"""


@dataclass(frozen=True)
class SummaryScenes:
    """The vocabulary the `summary_scenes` fixture hands a test"""

    version: typing.Callable[..., VersionRecord]
    """Build one dataset version, as a summary carries it"""

    change: typing.Callable[..., ChangeRecord]
    """Build one change, as a summary carries it"""

    leaf: typing.Callable[..., SearchRunLeafInfo]
    """Build one sub-search's outcome, as a run's health carries it"""

    run: typing.Callable[..., RunHealth]
    """Build one run's health"""

    summary: typing.Callable[..., SearchSummary]
    """Build a whole summary, with the parts a test doesn't care about filled in"""

    recorded: typing.Callable[..., DatasetChangeInfo]
    """Build one *stored* change, i.e. what `collapse_changes` is given"""


@pytest.fixture
def summary_scenes():
    """
    Get builders for the pieces of a search summary

    Lives here rather than beside either set of tests because the unit tests and the
    rendering regression tests both need them, and two copies would be free to drift.

    Every builder fills in the parts a test is not making a point about, so that a
    test reads as the one thing it is checking.
    """

    def version(  # noqa: PLR0913 - one argument per thing a scene varies
        model="ACCESS-ESM1-6",
        experiment="historical",
        variant_label="r1i1p1f1",
        dataset_version_id=101,
        *,
        version_string="20261018",
        retracted=False,
        is_latest=True,
        data_nodes=("esgf.nci.org.au",),
        seen_in_run_id=7,
        **facets,
    ):
        """
        Build one dataset version, as a summary carries it

        Real CMIP values rather than `get_facet_value` placeholders, because these
        facets are rendered: a checked-in regression file full of
        `reporting_interval-value` is one nobody can review by eye, which is the
        whole point of having it. Pass any facet by name to vary it.
        """
        return VersionRecord(
            dataset_version_id=dataset_version_id,
            dataset_id=dataset_version_id * 10,
            id_project_specific=f"CMIP7.{model}.{experiment}.{variant_label}",
            facets={
                **REALISTIC_FACETS,
                "model": model,
                "experiment": experiment,
                "variant_label": variant_label,
                **facets,
            },
            version=version_string,
            is_latest=is_latest,
            retracted=retracted,
            data_nodes=data_nodes,
            seen_in_run_id=seen_in_run_id,
        )

    def change(change_type, of=None, *, detail=None, first_run_id=7, last_run_id=7):
        """Build one change, as a summary carries it."""
        return ChangeRecord(
            change_type=change_type,
            version=of if of is not None else version(),
            detail=detail,
            first_run_id=first_run_id,
            last_run_id=last_run_id,
        )

    def leaf(
        role="historical.tas",
        *,
        answered=True,
        n_matches=209,
        n_documents=209,
        failure=None,
    ):
        """Build one sub-search's outcome, as a run's health carries it."""
        return SearchRunLeafInfo(
            roles=(role,),
            project="CMIP7",
            answered=answered,
            host="search.east.esgf.io" if answered else None,
            n_matches=n_matches,
            n_documents=n_documents,
            failure=failure,
        )

    def run(run_id=7, *, complete=True, leaves=None):
        """Build one run's health."""
        return RunHealth(
            run_id=run_id,
            started_at=datetime.datetime(
                2026, 10, 20, 6, 2, tzinfo=datetime.timezone.utc
            ),
            finished_at=datetime.datetime(
                2026, 10, 20, 6, 4, tzinfo=datetime.timezone.utc
            ),
            complete=complete,
            leaves=(leaf(),) if leaves is None else leaves,
        )

    def summary(  # noqa: PLR0913 - one argument per field a scene fills in
        *,
        health=None,
        changes=(),
        net_unchanged=0,
        current=(),
        window_runs=(7,),
        baseline_run_id=6,
    ):
        """Build a whole summary, with the boring parts filled in."""
        return SearchSummary(
            requirement_name="pattern-effect",
            stored_requirement_id=1,
            run=health if health is not None else run(),
            baseline_run_id=baseline_run_id,
            window_runs=window_runs,
            changes=changes,
            net_unchanged=net_unchanged,
            current=current,
        )

    def recorded(
        change_type, dataset_version_id=417, *, detail=None, baseline_run_id=2
    ):
        """Build one stored change, i.e. what `collapse_changes` is given."""
        return DatasetChangeInfo(
            dataset_version_id=dataset_version_id,
            change_type=change_type,
            baseline_run_id=baseline_run_id,
            detail=detail,
        )

    return SummaryScenes(
        version=version,
        change=change,
        leaf=leaf,
        run=run,
        summary=summary,
        recorded=recorded,
    )


@pytest.fixture(scope="session", autouse=True)
def pandas_terminal_width():
    # Set pandas terminal width so that doctests don't depend on terminal width.

    # We set the display width to 120 because examples should be short,
    # anything more than this is too wide to read in the source.
    pd.set_option("display.width", 120)

    # Display as many columns as you want (i.e. let the display width do the
    # truncation)
    pd.set_option("display.max_columns", 1000)
