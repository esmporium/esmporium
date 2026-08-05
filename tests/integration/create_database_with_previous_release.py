"""
Create a database, with a dataset in it, using the installed version of esmporium

This is a helper for `test_migrations_from_previous_release.py`,
which runs it in a separate environment that has a *released* version of esmporium
installed rather than the working tree (see that module for why).

It follows that this can only use API that the release in question had.
If a release changes the API used here, this has to change with it
and, if that change was a breaking one, that is worth knowing in itself.

It is a script rather than something the test imports and calls
because the code it needs to run lives in a different environment,
so the only way to reach it is to start another interpreter.
"""

from __future__ import annotations

import argparse

from sqlmodel import Session, create_engine

from esmporium.db import DATASET_FACET_COLUMNS, Dataset, migrate


def main(database_path: str, dataset_id: str) -> None:
    """
    Create a database and write a dataset into it

    Parameters
    ----------
    database_path
        Path of the database to create

    dataset_id
        ID of the dataset to write into the database
    """
    engine = create_engine(f"sqlite:///{database_path}")
    migrate.upgrade_to_head(engine)

    # Write data into the database
    with Session(engine) as session:
        session.add(
            Dataset(
                id=dataset_id,
                **{column: f"{column}-value" for column in DATASET_FACET_COLUMNS},
            )
        )
        session.commit()


def parse_args() -> argparse.Namespace:
    """
    Parse the command-line arguments

    Returns
    -------
    :
        The parsed arguments.
    """
    # argparse rather than anything nicer,
    # because this runs in an environment that has esmporium and its dependencies,
    # so the standard library is all we can count on.
    parser = argparse.ArgumentParser(
        description=(
            "Create a database, with a dataset in it, "
            "using the installed version of esmporium"
        )
    )
    parser.add_argument("database_path", help="Path of the database to create")
    parser.add_argument(
        "dataset_id", help="ID of the dataset to write into the database"
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(args.database_path, args.dataset_id)
