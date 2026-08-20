"""
Test that a database written by the previous release can still be opened

`tests/integration/test_migrations.py` checks that our migrations and our models
agree, starting from an empty database.
That is not the case that breaks users.
The case that breaks users is the database they already have:
one that was created by the version of esmporium they installed last time,
with their data in it.
A migration that adds a non-nullable column without a server default,
or tightens a constraint that existing rows don't satisfy,
passes every other test we have and then fails on their machine.

So this test does the only thing that actually checks that:
it installs the previous release, creates a database with it, puts a row in it,
and then migrates that database with the code in the working tree.
Installing the release, rather than keeping a database file in the repo,
means we test a database built by the code we actually shipped,
and means there is nothing to regenerate and commit each time we release.
The cost is that this test needs `uv` on the path and network access.

Note that this is not active yet, as we don't have a release to compare against.
It is a placeholder so we remember to check this once we start doing releases.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlmodel import Session

from esmporium.db import Dataset, migrate

# Installing the previous release means going to PyPI.
# That is ordinary network access rather than an API we care about in its own
# right, so it gets the generic marker.
pytestmark = pytest.mark.network

FIRST_RELEASE_WITH_A_DATABASE: str | None = None
"""
First released version of esmporium that ships a database

Databases only exist in the wild once we release something that creates one,
and at the time of writing we never have,
which is why this is `None` and why this test skips.

Set this to the version that first ships the database (e.g. `"0.2.0"`)
and this test starts doing its job.
It is then a fact about our history, so it never needs updating again:
which release we test against is worked out from our tags, see `get_releases`.
"""

DATASET_ID = "dataset-created-by-previous-release"
"""ID of the dataset we write with the previous release and look for afterwards"""

CREATE_DATABASE_SCRIPT = (
    Path(__file__).parent / "create_database_with_previous_release.py"
)
"""Script that creates the database we then migrate"""

REPO_ROOT = Path(__file__).parents[2]
"""Root of the repository, so git commands don't depend on where pytest was run from"""

REMOTE = "origin"
"""
Remote to read our released versions from

We ask the remote rather than using `git tag`,
because local tags are whatever the developer happened to have fetched,
which can silently be nothing at all.
"""


def get_releases() -> list[str]:
    """
    Get every released version of esmporium, oldest first

    The releases are our `v`-prefixed tags on `REMOTE`,
    sorted by version rather than alphabetically,
    so the most recent release is the last element.

    Returns
    -------
    :
        The released versions, without their `v` prefix.
    """
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "ls-remote",
            "--tags",
            # Drop the dereferenced (`^{}`) entries that annotated tags also produce.
            "--refs",
            "--sort=v:refname",
            REMOTE,
            "v*",
        ],
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
        text=True,
    )

    if result.returncode:
        # Deliberately a failure rather than a skip.
        # This test is the only thing standing between a migration
        # and a user's database, so it should shout when it can't run,
        # even though that means an offline `make test` gets a red line
        # (if that becomes annoying, this is the place to change it).
        msg = (
            f"Could not read the tags on {REMOTE}, "
            "so could not work out which release to test against "
            "(this test needs network access).\n"
            f"stderr:\n{result.stderr}"
        )
        pytest.fail(msg)

    return [
        line.split("refs/tags/v")[-1] for line in result.stdout.splitlines() if line
    ]


def create_database_with_previous_release(version: str, database_path: Path) -> None:
    """
    Create a database using a released version of esmporium

    Parameters
    ----------
    version
        Version of esmporium to create the database with

    database_path
        Path of the database to create
    """
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "uv",
            "run",
            # Ignore the project we're sitting in,
            # otherwise uv gives us the working tree rather than the release.
            "--no-project",
            "--with",
            f"esmporium=={version}",
            "python",
            str(CREATE_DATABASE_SCRIPT),
            str(database_path),
            DATASET_ID,
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    if result.returncode:
        msg = (
            f"Could not create a database with esmporium=={version}.\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
        pytest.fail(msg)


@pytest.mark.skipif(
    FIRST_RELEASE_WITH_A_DATABASE is None,
    reason=(
        "No release of esmporium ships a database yet, "
        "see FIRST_RELEASE_WITH_A_DATABASE"
    ),
)
def test_database_from_previous_release_can_be_migrated(
    database_path, engine, get_pending_changes
):
    """
    Test that we can migrate a database written by the previous release

    If this fails, shipping the working tree as-is would leave anyone
    who already uses esmporium with a database they can no longer open,
    which is the one failure mode our migrations exist to prevent.
    The fix is in the migration, not in this test:
    a migration that has to cope with existing rows has to say what to do with them
    (see the notes on data migrations in `docs/development.md`).

    Before this starts running for real, i.e. before
    `FIRST_RELEASE_WITH_A_DATABASE` is set, work out how to install the previous
    release without `uv`.

    `create_database_with_previous_release` shells out to `uv run --with`,
    which is by far the neatest way to get a throwaway environment holding a
    different version of ourselves. The catch is CI: the `tests-without-extras`
    job builds its environment with `setup-python` and pip, deliberately, to
    mirror what a plain PyPI install gives someone, and there is no `uv` on the
    path there. Today that does not matter, because the skip above means this
    test never runs. On the day it does, that job would go red for a reason
    that has nothing to do with what it is meant to be checking.

    Adding `uv` to that job would fix the symptom and spoil the job: the whole
    point of it is that it does not have our tooling. So the change belongs
    here. Options, roughly in order of how much we like them:

    - build the environment with `pip install --target` into a temporary
      directory and run the script with that on `PYTHONPATH`;
      pip is there in every environment which can run pytest at all
    - keep `uv` when it is on the path and fall back to pip when it is not,
      which works everywhere but means two code paths to keep honest
    - skip when `uv` is missing, which keeps this simple and quietly gives up
      exactly the coverage the job was added for

    Whichever we pick, `--run-network` can stay on `tests-without-extras`
    (see `.github/workflows/ci.yaml`) and that job can stay as it is.
    """
    releases = get_releases()
    if FIRST_RELEASE_WITH_A_DATABASE not in releases:
        # The version that first ships a database has been decided
        # but hasn't been released yet, which is the state a release branch is in
        # between bumping the version and publishing it.
        # There is nothing to test against until the tag exists,
        # and this clears itself as soon as it does.
        pytest.skip(
            f"esmporium {FIRST_RELEASE_WITH_A_DATABASE}, "
            "the first release with a database, hasn't been released yet"
        )

    # The releases are sorted oldest first, so the previous release is the last one.
    previous_release = releases[-1]

    create_database_with_previous_release(previous_release, database_path)

    # The previous release is expected to stamp the database it creates.
    # If it didn't, the rest of this test would be checking nothing at all,
    # because migrating from "no revision" is just migrating from scratch.
    assert migrate.get_current_revision(engine) is not None

    migrate.upgrade_to_head(engine)

    assert migrate.get_current_revision(engine) == migrate.get_head_revision()
    assert get_pending_changes(engine) == []

    # The migration has to bring the data with it.
    # A migration that drops the table and re-creates it
    # would pass every check above and lose everything the user had.
    with Session(engine) as session:
        assert session.get(Dataset, DATASET_ID) is not None
