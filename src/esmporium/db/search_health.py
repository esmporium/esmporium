"""
Recording search API health into the database
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import Session

from esmporium.db.schema import SearchAPICallRecord

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from esmporium.search.health import SearchAPICall, SearchAPICallObserver


def record_search_api_calls(engine: Engine) -> SearchAPICallObserver:
    """
    Build an observer which records each search API call record into database

    A fresh session is opened per call, so the observer is safe to call from
    several threads at once. Each call is committed on its own, so a run killed
    part way through keeps the records of the calls it had already made.

    Parameters
    ----------
    engine
        The database engine to record into.
        The caller owns it

    Returns
    -------
    :
        An observer which writes one row per call it is told about
    """

    def observer(call: SearchAPICall) -> None:
        # thread safe to handle parallelism
        with Session(engine) as session:
            session.add(SearchAPICallRecord.from_call(call))
            session.commit()

    return observer
