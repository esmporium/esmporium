"""Unit tests for the recorder seam."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from esmporium.esgf.search import (
    FakeRecorder,
    NullRecorder,
    SearchApiCallStat,
    SearchAPIGeneration,
)


def _stat(**overrides) -> SearchApiCallStat:
    kwargs = {
        "host": "discovery.west.esgf.io",
        "generation": SearchAPIGeneration.ESGF_NG_WEST,
        "project": "CMIP6",
        "timestamp": datetime.now(timezone.utc),
        "ok": True,
        "elapsed_seconds": 0.5,
    }
    kwargs.update(overrides)
    return SearchApiCallStat(**kwargs)


def test_fake_recorder_collects_stats_in_order():
    recorder = FakeRecorder()
    assert recorder.stats == []
    first, second = _stat(project="CMIP6"), _stat(project="CMIP7")
    recorder.record(first)
    recorder.record(second)
    assert recorder.stats == [first, second]


def test_null_recorder_is_a_no_op():
    recorder = NullRecorder()
    # Must accept a stat and simply do nothing, without error.
    assert recorder.record(_stat()) is None


def test_stat_is_immutable():
    stat = _stat()
    with pytest.raises(ValidationError):
        stat.ok = False


def test_stat_optional_fields_default_to_none():
    stat = _stat()
    assert stat.status_code is None
    assert stat.num_matched is None
    assert stat.error is None
