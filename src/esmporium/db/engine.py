"""
Engine helpers

Small helpers for preparing a database engine, kept apart from the schema and the
writing logic because configuring the engine is a separate concern from either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import event

if TYPE_CHECKING:
    from sqlalchemy import Engine


def configure_sqlite_for_concurrency(
    engine: Engine, *, busy_timeout_ms: int = 30_000
) -> Engine:
    """
    Make a SQLite engine safe to write to from several connections at once

    Parallel search (via [`esmporium.search.search`][] with ``max_workers > 1``) gives
    each worker its own session, and so its own connection, all writing to the one
    database. Default SQLite allows a single writer and fails a second one immediately
    with "database is locked". This sets two pragmas on every new connection to fix
    that:

    - ``journal_mode=WAL``: readers no longer block the writer (and vice versa), so the
      workers do not trip over each other's reads.
    - ``busy_timeout``: a worker that finds the database busy waits up to this long for
      the lock instead of failing straight away, which serialises the brief writes
      rather than losing them.

    Call this on the engine you hand to
    [`build_result_processor_factory`][esmporium.db.build_result_processor_factory]
    before running a parallel search against a shared SQLite database. It is
    SQLite-specific: do not call it on an engine for another backend.

    Parameters
    ----------
    engine
        The SQLite engine to configure. Configured in place.

    busy_timeout_ms
        How long, in milliseconds, a connection waits for a held lock before giving up.

    Returns
    -------
    :
        `engine`, so this can be used inline, e.g.
        ``configure_sqlite_for_concurrency(create_engine(url))``.
    """

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _rec):  # type: ignore[no-untyped-def]
        # Runs once per new DBAPI connection, so every worker's connection is set up.
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        finally:
            cursor.close()

    return engine
