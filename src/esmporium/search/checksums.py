"""
Reading a file's checksum out of what a search API published

The two APIs disagree about how to say "this file's checksum is this, of this
algorithm", and the disagreement is not cosmetic:

- ESGF-1 Solr publishes a bare hex digest in `checksum` and names the algorithm
  separately in `checksum_type`, e.g. `SHA256`.
- ESGF-NG STAC has **no** `checksum_type` field at all. Its `file:checksum` is a
  hex-encoded [multihash](https://github.com/multiformats/multihash), in which the
  algorithm and the digest length are a prefix on the digest itself.

[decode_multihash_checksum][(m).decode_multihash_checksum] turns the second shape
into the first, so that a stored checksum means the same thing whichever API it came
from and the two can be compared.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MULTIHASH_ALGORITHMS: Mapping[str, tuple[str, int]] = {
    "11": ("SHA1", 20),
    "12": ("SHA256", 32),
    "13": ("SHA512", 64),
}
"""
The multihash codes we recognise, as `code -> (algorithm name, digest bytes)`

Keyed by the code's two hex characters, because that is how the value arrives. The
names are spelled the way Solr spells its own `checksum_type`, so a decoded STAC
value and a Solr value agree.

Only the SHA family is here: it is what ESGF publishes. An unrecognised code is not
an error, it just means we cannot name the algorithm -- see
[decode_multihash_checksum][(m).decode_multihash_checksum].
"""

_HEX_PER_BYTE = 2
_CODE_HEX = 2
_LENGTH_HEX = 2
_PREFIX_HEX = _CODE_HEX + _LENGTH_HEX


@dataclass(frozen=True)
class Checksum:
    """A file's checksum, and which algorithm it is of if we could work that out"""

    value: str
    """
    The bare hex digest

    Or, if we could not decode what was published, the published value verbatim.
    """

    checksum_type: str | None
    """
    The algorithm the digest is of, e.g. `SHA256`

    `None` means we could not tell, in which case `value` is exactly what the API
    published.
    """


def decode_multihash_checksum(published: str) -> Checksum:
    """
    Read an ESGF-NG `file:checksum` multihash into a digest and an algorithm name

    A multihash is `<algorithm code><digest length><digest>`, hex-encoded, so
    `1220...` is `0x12` (sha2-256) followed by `0x20` (a 32-byte digest) followed by
    64 hex characters of digest.

    Anything we cannot read with confidence comes back **verbatim**, with
    `checksum_type` of `None`, and is logged at `DEBUG`. We record what upstream
    published rather than repairing it, and one unreadable value must not take a
    whole search down: a live CMIP6Plus asset carries 72 hex characters where a
    sha2-256 multihash is 68, because the `1220` prefix is there twice. That is an
    upstream bug, not an algorithm we have not heard of, and guessing which 64 of
    those characters are the digest would be a guess about someone else's data.

    Parameters
    ----------
    published
        The value the API published, e.g. a STAC asset's `file:checksum`

    Returns
    -------
    :
        The digest and its algorithm, or `published` unchanged and `None`

    Examples
    --------
    >>> decoded = decode_multihash_checksum("1220" + "ab" * 32)
    >>> decoded.checksum_type
    'SHA256'
    >>> decoded.value == "ab" * 32
    True

    The prefix twice over is the real CMIP6Plus bug, and comes back untouched:

    >>> unreadable = decode_multihash_checksum("1220" + "1220" + "ab" * 32)
    >>> unreadable.checksum_type is None
    True
    >>> unreadable.value == "1220" + "1220" + "ab" * 32
    True
    """
    reason = _why_not_a_multihash(published)
    if reason is not None:
        logger.debug(
            "could not read %r as a multihash checksum: %s",
            published,
            reason,
            extra={"published_checksum": published, "reason": reason},
        )
        return Checksum(value=published, checksum_type=None)

    algorithm, _ = MULTIHASH_ALGORITHMS[published[:_CODE_HEX]]
    return Checksum(value=published[_PREFIX_HEX:], checksum_type=algorithm)


def _why_not_hex_bytes(published: str) -> str | None:
    """Say why `published` is not an even-length run of hex, or `None` if it is"""
    if not published:
        return "it is empty"

    if any(character not in "0123456789abcdefABCDEF" for character in published):
        return "it is not hex"

    if len(published) % _HEX_PER_BYTE:
        return f"it has an odd number of hex characters ({len(published)})"

    return None


def _why_not_a_multihash(published: str) -> str | None:
    """
    Say why `published` cannot be read as a multihash, or `None` if it can

    Split out so that the reason can go in the log line: "we could not read this" is
    much less use when debugging a publisher's data than "this declared 32 digest
    bytes and carried 36".
    """
    not_hex = _why_not_hex_bytes(published)
    if not_hex is not None:
        return not_hex

    if len(published) <= _PREFIX_HEX:
        return f"it is too short to hold a prefix and a digest ({len(published)})"

    code = published[:_CODE_HEX]
    if code not in MULTIHASH_ALGORITHMS:
        return f"{code!r} is not a multihash code we recognise"

    algorithm, digest_bytes = MULTIHASH_ALGORITHMS[code]
    declared_bytes = int(published[_CODE_HEX:_PREFIX_HEX], 16)
    if declared_bytes != digest_bytes:
        return (
            f"it says {algorithm} but declares {declared_bytes} digest bytes, "
            f"where {algorithm} is {digest_bytes}"
        )

    digest_hex = len(published) - _PREFIX_HEX
    if digest_hex != digest_bytes * _HEX_PER_BYTE:
        return (
            f"it declares {declared_bytes} digest bytes "
            f"but carries {digest_hex / _HEX_PER_BYTE:g}"
        )

    return None
