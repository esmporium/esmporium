"""
Recording search API health into the database
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from esmporium.db.schema import SearchAPICallRecord

if TYPE_CHECKING:
    from sqlmodel import Session

    from esmporium.search.health import SearchAPICall, SearchAPICallObserver


def record_search_api_calls(session: Session) -> SearchAPICallObserver:
    """
    Build an observer which records each search API call record into the database

    Each call is committed on its own, so a run that is killed part way through
    keeps the records of the calls it had already made.

    Parameters
    ----------
    session
        The database session to record into.
        The caller owns it (opening and closing it), the same way the search
        functions let the caller own the HTTP client.

    Returns
    -------
    :
        An observer which writes one row per call it is told about
    """

    def observer(call: SearchAPICall) -> None:
        session.add(SearchAPICallRecord.from_call(call))
        session.commit()

    return observer
