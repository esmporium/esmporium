"""
Re-useable fixtures etc. for tests

See https://docs.pytest.org/en/7.1.x/reference/fixtures.html#conftest-py-sharing-fixtures-across-multiple-files
"""

import pandas as pd
import pytest

from esmporium.db import DATASET_FACET_COLUMNS, Dataset

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

    def factory(dataset_id, **facets):
        return {
            "id": dataset_id,
            "id_project_specific": f"{dataset_id}_ps",
            **{column: get_facet_value(column) for column in DATASET_FACET_COLUMNS},
            **facets,
        }

    return factory


@pytest.fixture(scope="session", autouse=True)
def pandas_terminal_width():
    # Set pandas terminal width so that doctests don't depend on terminal width.

    # We set the display width to 120 because examples should be short,
    # anything more than this is too wide to read in the source.
    pd.set_option("display.width", 120)

    # Display as many columns as you want (i.e. let the display width do the
    # truncation)
    pd.set_option("display.max_columns", 1000)
