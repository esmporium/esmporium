"""
Unit tests for recording search API health into the database
"""

from __future__ import annotations

from sqlmodel import Session, col, select

from esmporium.db import SearchAPICallRecord, record_search_api_calls
from esmporium.search.health import SearchAPICall


def make_call(  # noqa: PLR0913 - a factory mirroring every field of the record
    *,
    host: str = "esgf.example.org",
    http_method: str = "GET",
    url: str = "https://esgf.example.org/esg-search/search?x=1",
    request_body: str | None = None,
    response_code: int | None = 200,
    success: bool = True,
    error: Exception | None = None,
    num_results: int | None = 7,
    response_time_seconds: float = 0.25,
    attempt_number: int = 1,
) -> SearchAPICall:
    """Build a SearchAPICall with sensible defaults, overriding as needed."""
    return SearchAPICall(
        host=host,
        http_method=http_method,
        url=url,
        request_body=request_body,
        response_code=response_code,
        success=success,
        error=error,
        num_results=num_results,
        response_time_seconds=response_time_seconds,
        attempt_number=attempt_number,
    )


def test_from_call_copies_the_scalar_fields():
    call = make_call(
        host="h",
        http_method="POST",
        url="u",
        request_body='{"q": 1}',
        response_code=503,
        success=False,
        num_results=None,
        response_time_seconds=1.5,
        attempt_number=3,
        error=ValueError("x"),
    )

    row = SearchAPICallRecord.from_call(call)

    assert row.host == "h"
    assert row.http_method == "POST"
    assert row.url == "u"
    assert row.request_body == '{"q": 1}'
    assert row.response_code == 503
    assert row.success is False
    assert row.num_results is None
    assert row.response_time_seconds == 1.5
    assert row.attempt_number == 3


def test_from_call_translates_the_error_to_type_and_message():
    assert SearchAPICallRecord.from_call(make_call(error=None)).error is None

    row = SearchAPICallRecord.from_call(
        make_call(success=False, error=ValueError("boom"))
    )
    assert row.error == "ValueError: boom"


def test_record_persists_a_row(engine):
    observer = record_search_api_calls(engine)

    observer(make_call(host="node-a", num_results=42))

    with Session(engine) as session:
        rows = session.exec(select(SearchAPICallRecord)).all()

    (row,) = rows
    assert row.host == "node-a"
    assert row.num_results == 42
    assert row.success is True
    assert row.error is None
    assert row.created_at is not None


def test_record_stores_the_error_string(engine):
    observer = record_search_api_calls(engine)

    observer(
        make_call(
            success=False,
            response_code=500,
            num_results=None,
            error=ValueError("bad json"),
        )
    )

    with Session(engine) as session:
        (row,) = session.exec(select(SearchAPICallRecord)).all()

    assert row.success is False
    assert row.error == "ValueError: bad json"


def test_record_is_append_only(engine):
    observer = record_search_api_calls(engine)

    observer(make_call(host="a", attempt_number=1))
    observer(make_call(host="b", attempt_number=2))

    with Session(engine) as session:
        rows = session.exec(
            select(SearchAPICallRecord).order_by(col(SearchAPICallRecord.id))
        ).all()

    assert [row.host for row in rows] == ["a", "b"]
    assert [row.attempt_number for row in rows] == [1, 2]
