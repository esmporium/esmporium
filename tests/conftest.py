"""
Re-useable fixtures etc. for tests

See https://docs.pytest.org/en/7.1.x/reference/fixtures.html#conftest-py-sharing-fixtures-across-multiple-files
"""

import pandas as pd
import pytest

from esmporium.db import DATASET_FACET_COLUMNS, Dataset


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
