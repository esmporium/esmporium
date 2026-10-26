"""
Tests of reading a checksum out of what a search API published

The interesting values here are real, taken from the recorded responses in
`tests/test-data/search`, including the malformed one: upstream data is the thing
this code exists to cope with, so invented values alone would not prove much.
"""

from __future__ import annotations

import logging

import pytest

from esmporium.search import decode_multihash_checksum

# A real `file:checksum` from a CMIP6 asset on CEDA (east STAC).
REAL_SHA256_MULTIHASH = (
    "12200f63e0dba20128138896947da92f6a36852db8f929954ff02db08dec520a2213"
)

# A real CMIP6Plus value, and an upstream bug: 72 hex characters where a sha2-256
# multihash is 68, because the `1220` prefix is there twice.
REAL_DOUBLE_PREFIXED = (
    "1220122035c876055e5d4508209fd8961a446e1bfa9a762adadcc93b23b2b54451d6ae05"
)


def test_a_real_sha256_multihash_decodes():
    """The `1220` prefix comes off and the algorithm is named the way Solr names it"""
    decoded = decode_multihash_checksum(REAL_SHA256_MULTIHASH)

    assert decoded.checksum_type == "SHA256"
    assert decoded.value == REAL_SHA256_MULTIHASH.removeprefix("1220")
    # 32 bytes, so 64 hex characters -- the point of checking the length is that a
    # digest silently losing or keeping two characters is otherwise invisible.
    assert len(decoded.value) == 64


def test_a_decoded_digest_matches_the_bare_digest_solr_would_have_published():
    """
    Decoding is what lets the two APIs be compared at all

    Solr publishes a bare digest plus `checksum_type`; STAC publishes a multihash and
    no type. After decoding, the same file described by both reads identically, which
    is what the CMIP6 cross-API integration test leans on.
    """
    as_solr_would_publish = (
        "0f63e0dba20128138896947da92f6a36852db8f929954ff02db08dec520a2213"
    )

    decoded = decode_multihash_checksum(REAL_SHA256_MULTIHASH)

    assert (decoded.value, decoded.checksum_type) == (as_solr_would_publish, "SHA256")


def test_the_real_double_prefixed_value_is_recorded_verbatim():
    """
    A live CMIP6Plus asset carries the prefix twice, and we do not guess

    Which 64 of those 68 digest characters are the real digest is a question about
    someone else's data, so the value is stored exactly as published and the
    algorithm is left unstated rather than asserted wrongly.
    """
    decoded = decode_multihash_checksum(REAL_DOUBLE_PREFIXED)

    assert decoded.value == REAL_DOUBLE_PREFIXED
    assert decoded.checksum_type is None


@pytest.mark.parametrize(
    "published",
    [
        pytest.param("", id="empty"),
        pytest.param("zz20" + "ab" * 32, id="not-hex"),
        pytest.param("1220" + "ab" * 32 + "a", id="odd-number-of-hex-characters"),
        pytest.param("1220", id="prefix-with-no-digest"),
        pytest.param("9920" + "ab" * 32, id="unrecognised-multihash-code"),
        pytest.param("1240" + "ab" * 32, id="declares-a-length-sha256-is-not"),
        pytest.param("1220" + "ab" * 30, id="declares-more-digest-than-it-carries"),
    ],
)
def test_anything_unreadable_comes_back_verbatim_and_unnamed(published):
    """
    Every way of being unreadable is recorded, never repaired and never raised

    One publisher's bad asset must not take a whole search down, so there is
    deliberately no exception here -- the caller gets a value it can store and a
    `None` that says we could not name it.
    """
    decoded = decode_multihash_checksum(published)

    assert decoded.value == published
    assert decoded.checksum_type is None


def test_an_unreadable_value_says_why_at_debug_level(caplog):
    """
    The log line carries the reason, not just the fact

    "We could not read this" is much less use when debugging a publisher's data than
    "it declares 32 digest bytes but carries 30".
    """
    with caplog.at_level(logging.DEBUG, logger="esmporium.search.checksums"):
        decode_multihash_checksum("1220" + "ab" * 30)

    assert "declares 32 digest bytes but carries 30" in caplog.text
