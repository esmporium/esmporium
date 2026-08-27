"""
Recording search API health into the database

The search layer emits a plain [SearchApiCall][esmporium.search.health.SearchApiCall]
for each call it makes and hands it to an observer, without knowing what the observer
does with it (see [esmporium.search.health][]).
This is the observer which records that call as a row:
it is the one place the search-health fact becomes a
[SearchApiCallRecord][esmporium.db.schema.SearchApiCallRecord] in the database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from esmporium.db.schema import SearchApiCallRecord

if TYPE_CHECKING:
    from sqlmodel import Session

    from esmporium.search.health import SearchApiCall, SearchApiCallObserver


def record_search_api_calls(session: Session) -> SearchApiCallObserver:
    """
    Build an observer which records each search API call into the database

    Pass the result as the `observer` of
    [search][esmporium.search.search.search] or
    [check_query_values][esmporium.search.check_query_values.check_query_values]
    (or compose it with others via
    [fan_out][esmporium.search.health.fan_out]).

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

    def observer(call: SearchApiCall) -> None:
        session.add(SearchApiCallRecord.from_call(call))
        session.commit()

    return observer
