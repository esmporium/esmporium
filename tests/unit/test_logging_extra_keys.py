"""
A guard against a crash that only happens when logging is turned up

`logging` builds every record's own attributes -- `filename`, `module`, `name`,
`process` and friends -- and then refuses an `extra=` that would overwrite one,
raising `KeyError("Attempt to overwrite 'filename' in LogRecord")`. The refusal
happens when the record is *made*, so a line guarded by nothing is fine until someone
enables `DEBUG`, at which point it takes down whatever it was reporting on.

This repo logs a structured `extra` beside almost every message, so it has plenty of
chances to hit that. It did: `esmporium.downloads.run` passed `extra={"filename": ...}`
on both of its route-fallback lines, so a download that fell through to a second mirror
with `DEBUG` on would raise instead of trying it. Found while adding the same key in
`esmporium.headers`.

A static check rather than a per-call-site test, because the bug is in the *name* and
one test can cover every name in the package for ever.
"""

from __future__ import annotations

import logging
import pathlib
import re

import pytest

import esmporium

EXTRA_KEYS = re.compile(r"extra=\{(.*?)\}", re.DOTALL)
"""Finds the body of an `extra={...}` literal"""

QUOTED_KEY = re.compile(r'"([A-Za-z_][A-Za-z_0-9]*)":')
"""Finds each string key inside one"""


def reserved_log_record_attributes() -> frozenset[str]:
    """
    Get the attribute names `logging` puts on a record, which an `extra` may not reuse

    Read off a real record rather than hard-coded, so this keeps up with the standard
    library instead of pinning one version's list.

    Returns
    -------
    :
        Every name an `extra=` key would collide with
    """
    record = logging.LogRecord(
        name="probe",
        level=logging.DEBUG,
        pathname="p",
        lineno=1,
        msg="m",
        args=None,
        exc_info=None,
    )

    # `message` and `asctime` are not on the record yet; a formatter sets them, and
    # `logging` rejects them in an `extra` for that reason.
    return frozenset(vars(record)) | {"message", "asctime"}


def test_the_collision_really_does_raise():
    """
    Pin the behaviour this test exists because of, so the guard cannot go stale

    If a future `logging` stopped refusing, the test below would be guarding nothing
    and this would say so.
    """
    logger = logging.getLogger("esmporium.probe")
    logger.setLevel(logging.DEBUG)

    with pytest.raises(KeyError, match="Attempt to overwrite"):
        logger.debug("a file", extra={"filename": "tas.nc"})


def test_no_structured_log_key_collides_with_a_log_record_attribute():
    source_root = pathlib.Path(esmporium.__file__).parent
    reserved = reserved_log_record_attributes()

    offences: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        text = path.read_text()
        for block in EXTRA_KEYS.finditer(text):
            for key in QUOTED_KEY.findall(block.group(1)):
                if key in reserved:
                    line = text[: block.start()].count("\n") + 1
                    offences.append(
                        f"{path.relative_to(source_root)}:{line} passes "
                        f"extra={{{key!r}: ...}}"
                    )

    assert offences == [], (
        "These `extra=` keys collide with an attribute `logging` sets on every record, "
        "so the log call raises KeyError as soon as its level is enabled. Rename the "
        "key (e.g. `filename` -> `file_name`).\n  " + "\n  ".join(offences)
    )
