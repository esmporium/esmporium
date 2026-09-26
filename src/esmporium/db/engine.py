"""
Engine helpers

Small helpers for preparing a database engine, kept apart from the schema and the
writing logic because configuring the engine is a separate concern from either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import event

if TYPE_CHECKING:
    from sqlalchemy import Connection, Engine


def configure_sqlite_for_concurrency(
    engine: Engine, *, busy_timeout_ms: int = 300_000
) -> Engine:
    """
    Make an SQLite engine's transactions real, and safe to run from several connections

    For many functions
    (e.g. [`ingest_parsed_documents`][esmporium.db.ingest_parsed_documents] and
    [`save_dataset`][esmporium.db.save_dataset]),
    every SQLite engine that is passed must be configured with this,
    whether the search runs serially or in parallel.

    This setup fixes two things.

    **Transactions**
    By default, Python's `sqlite3` driver decides for itself when to send `BEGIN`,
    and never sends one before a `SAVEPOINT`.
    A savepoint opened outside a transaction then becomes the transaction,
    so releasing it commits.
    Saving results uses savepoints,
    so without this setup rows would be committed one by one as they are written,
    and a sub-query that fails partway would leave half its results behind.
    This applies
    [SQLAlchemy's documented fix](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#serializable-isolation-savepoints-transactional-ddl):
    it turns off the driver's own transaction handling
    and has SQLAlchemy send the `BEGIN` itself.

    That `BEGIN` is `BEGIN IMMEDIATE`, which takes SQLite's single write lock
    when the transaction starts rather than at its first write.
    Parallel search (e.g. via [`esmporium.search.search`][] with ``max_workers > 1``)
    gives each worker its own session, and so its own connection.
    With `BEGIN IMMEDIATE` their transactions take turns:
    a worker only starts once the previous one has committed,
    so it sees every row committed before it
    and never races another worker to insert the same row.
    A plain (deferred) `BEGIN`
    would instead fix a worker's view of the database at its first read,
    and SQLite would refuse its later write if another worker had committed in between.
    Read-only sessions on engines configured like this take the write lock too,
    but each only holds it briefly.

    **Waiting for the lock.** Two pragmas are set on every new connection:

    - ``journal_mode=WAL``: readers outside a transaction are not blocked by the writer
      (and vice versa).
    - ``busy_timeout``: a connection that finds the write lock held
      waits up to this long for it
      instead of failing straight away with "database is locked".

    Call this straight after creating the engine, before it opens any connection:
    connections that already exist do not get the pragmas.
    It is SQLite-specific: do not call it on an engine for another backend.

    Parameters
    ----------
    engine
        The SQLite engine to configure. Configured in place.

    busy_timeout_ms
        How long, in milliseconds, a connection waits for a held lock before giving up.

        The default is five minutes.
        A worker holds the lock while it saves a whole page of results,
        which can take tens of seconds for a large page,
        and a waiting worker may have to wait for several others in turn.

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

        # Stop the driver sending BEGIN/COMMIT itself;
        # `_begin_immediate` sends the BEGIN instead.
        dbapi_conn.isolation_level = None

    event.listen(engine, "begin", _begin_immediate)

    return engine


def _begin_immediate(conn: Connection) -> None:
    """Start each transaction explicitly, taking the write lock straight away"""
    conn.exec_driver_sql("BEGIN IMMEDIATE")


def is_sqlite_configured_for_concurrency(engine: Engine) -> bool:
    """
    Check whether [configure_sqlite_for_concurrency][(m).] has been applied to `engine`

    Parameters
    ----------
    engine
        The engine to check

    Returns
    -------
    :
        `True` if `engine` has been configured
    """
    return event.contains(engine, "begin", _begin_immediate)
