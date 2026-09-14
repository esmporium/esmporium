"""
Fixtures for the live search integration tests

The `engine`/`database_path` fixtures come from `tests/integration/conftest.py`
(file-backed SQLite, unmigrated). Here we add the piece the health-tracking
assertions need: a migrated database, an observer that records into it, and a way
to read the recorded rows back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlmodel import Session, col, select

from esmporium.db import SearchAPICallRecord, record_search_api_calls
from esmporium.db.migrate import upgrade_to_head

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    from typing import NoReturn

    from sqlalchemy import Engine

    from esmporium.search.health import SearchAPICallObserver

# The observer to record with, plus a function that gives back the rows recorded.
Recorded = tuple["SearchAPICallObserver", "Callable[[], list[SearchAPICallRecord]]"]


@pytest.fixture
def recorded(engine: Engine) -> Iterator[Recorded]:
    """
    Get an observer that records search-API calls, and a reader for the rows

    Yields a `(observer, read_calls)` pair: pass `observer` to `search` or
    `check_query_values`, then call `read_calls()` to get the recorded
    [SearchAPICallRecord][esmporium.db.schema.SearchAPICallRecord] rows back.
    """
    upgrade_to_head(engine)

    observer = record_search_api_calls(engine)

    def read_calls() -> list[SearchAPICallRecord]:
        # A fresh session so we read what the observer committed, not a stale
        # identity-map view.
        with Session(engine) as reader:
            return list(
                reader.exec(
                    select(SearchAPICallRecord).order_by(col(SearchAPICallRecord.id))
                )
            )

    yield observer, read_calls


@pytest.fixture
def skip_or_fail() -> Callable[..., NoReturn]:
    """
    Get a function which turns every endpoint failing into a skip or a failure

    Call it with the failures carried by the error saying nobody answered
    (e.g. `NoAPIAnsweredError.failures`),
    the failure type meaning "the endpoint did not answer",
    and the reason to skip with.

    An endpoint which did not answer is down or unwell,
    which says nothing about the behaviour under test, so if that is every failure,
    the test skips.
    Any other failure fails the test.
    The one we know of is an endpoint answering with something we could not read,
    which is exactly the change in response shape the live tests exist to notice.
    Failing on anything but "did not answer" (rather than on "could not read" alone)
    means a kind of failure we add later fails loudly instead of quietly skipping.
    """

    def check(
        failures: Iterable[Exception], *, did_not_answer: type[Exception], reason: str
    ) -> NoReturn:
        other_failures = [
            failure for failure in failures if not isinstance(failure, did_not_answer)
        ]
        if other_failures:
            pytest.fail(
                "\n".join(str(failure) for failure in other_failures),
                pytrace=False,
            )

        pytest.skip(reason)

    return check
