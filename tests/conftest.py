"""
Re-useable fixtures etc. for tests

See https://docs.pytest.org/en/7.1.x/reference/fixtures.html#conftest-py-sharing-fixtures-across-multiple-files
"""

import pandas as pd
import pytest

from esmporium.db import DATASET_FACET_COLUMNS


@pytest.fixture
def get_dataset_kwargs():
    """
    Get a factory for the keyword arguments of a dataset with every facet filled in

    The facet values are built from their column names,
    so adding a facet to the model doesn't mean editing every test that stores one.
    Tests that care about a particular value pass it by name.
    """

    def factory(dataset_id, **facets):
        return {
            "id": dataset_id,
            **{column: f"{column}-value" for column in DATASET_FACET_COLUMNS},
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
