"""
Remove the migrations that have been added since a given git ref

This is the first half of squashing migrations
(the second half is regenerating a single migration in their place,
see the `migration-squash-*` targets in the `Makefile`).
It only ever removes migrations that are absent from `ref`,
i.e. migrations that no-one outside this branch has ever had to apply.

The migrations that are kept must be untouched since `ref`,
because they are the ones that users' databases may already have applied.
If they have been edited (or deleted), we bail out rather than guess.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import NoReturn

import typer

VERSIONS_DIR = Path("src") / "esmporium" / "db" / "migrations" / "versions"


def git(*args: str) -> str:
    """
    Run a git command and capture its output

    Parameters
    ----------
    args
        Arguments to pass to git

    Returns
    -------
    :
        The command's standard output, stripped of surrounding whitespace
    """
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


def migrations_at(ref: str) -> set[Path]:
    """
    Get the migrations that exist at a given git ref

    Parameters
    ----------
    ref
        Git ref of interest

    Returns
    -------
    :
        Paths of the migration scripts that exist at `ref`
    """
    listing = git("ls-tree", "-r", "--name-only", ref, "--", str(VERSIONS_DIR))

    return {Path(line) for line in listing.splitlines() if line.endswith(".py")}


def exit_with_error(msg: str) -> NoReturn:
    """
    Print an error message, then exit with a non-zero exit code

    Parameters
    ----------
    msg
        Message to print
    """
    typer.echo(msg, err=True)

    raise typer.Exit(code=1)


def main(ref: str) -> None:
    """
    Remove the migrations that have been added since `ref`

    Parameters
    ----------
    ref
        Git ref to squash relative to,
        e.g. `main` or the tag of the last release
    """
    try:
        ref_commit = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except subprocess.CalledProcessError:
        exit_with_error(f"Not a git ref: {ref}")

    # Everything we remove has to be recoverable with `git restore`,
    # so refuse to run if there is anything in `versions/`
    # that is not committed (including migrations you have just generated).
    dirty = git("status", "--porcelain", "--", str(VERSIONS_DIR))
    if dirty:
        exit_with_error(
            f"There are uncommitted changes in {VERSIONS_DIR}:\n"
            f"{dirty}\n"
            "Commit or stash them first, "
            "so that squashing can be undone with `git restore`."
        )

    at_ref = migrations_at(ref_commit)
    at_head = migrations_at("HEAD")

    deleted_since_ref = at_ref - at_head
    if deleted_since_ref:
        exit_with_error(
            f"These migrations exist at {ref} but not at HEAD:\n"
            + "\n".join(f"  {p}" for p in sorted(deleted_since_ref))
            + f"\nSquashing assumes everything released at {ref} is still here."
        )

    changed_since_ref = {
        Path(line)
        for line in git(
            "diff", "--name-only", ref_commit, "HEAD", "--", str(VERSIONS_DIR)
        ).splitlines()
    }
    edited = changed_since_ref & at_ref
    if edited:
        exit_with_error(
            f"These migrations already existed at {ref} but have been edited since:\n"
            + "\n".join(f"  {p}" for p in sorted(edited))
            + "\nA database may already have applied them as they were, "
            "so squashing would bake in an edit that those databases never saw.\n"
            "Sort that out by hand before squashing."
        )

    to_remove = at_head - at_ref
    if not to_remove:
        exit_with_error(f"No migrations have been added since {ref}, nothing to do")

    for migration in sorted(to_remove):
        migration.unlink()
        typer.echo(f"Removed {migration}")

    typer.echo(
        f"Removed {len(to_remove)} migration(s) added since {ref}. "
        f"To undo: `git restore {VERSIONS_DIR}` "
        "(and delete the squashed migration, if one was generated)."
    )


if __name__ == "__main__":
    typer.run(main)
