"""Fingerprint stability for ``bytes`` source values (one-HyperTable KB).

A KB corpus row's ``content`` column is raw ``bytes`` (``pa.large_binary``).
The row fingerprint must be deterministic and byte-sensitive over that value,
and must never attempt to decode the bytes as text — real PDFs contain invalid
UTF-8 sequences.
"""

from __future__ import annotations

from hypergraph.materialization._fingerprint import _fingerprint, compute_row_fingerprint

# Deliberately invalid UTF-8 (0x80, 0xff) plus an embedded NUL and the full byte range.
_PDF_LIKE = bytes([0x25, 0x50, 0x44, 0x46, 0x00, 0x80, 0xFF, 0xFE]) + bytes(range(256))


def test_bytes_fingerprint_is_deterministic() -> None:
    inputs = {"content": _PDF_LIKE, "filename": "a.pdf"}
    assert _fingerprint(inputs, ["n"], {}) == _fingerprint(dict(inputs), ["n"], {}), "the same bytes must fingerprint identically across calls"


def test_bytes_fingerprint_is_byte_sensitive() -> None:
    base = {"content": _PDF_LIKE, "filename": "a.pdf"}
    flipped = {"content": _PDF_LIKE[:-1] + bytes([(_PDF_LIKE[-1] + 1) % 256]), "filename": "a.pdf"}
    assert _fingerprint(base, ["n"], {}) != _fingerprint(flipped, ["n"], {}), (
        "flipping a single byte of a large_binary value must change the fingerprint"
    )


def test_bytes_fingerprint_does_not_decode() -> None:
    # If the fingerprint tried to str-decode the bytes, invalid UTF-8 would raise.
    fp = _fingerprint({"content": bytes([0x80, 0xC0, 0xFF])}, [], {})
    assert isinstance(fp, str) and len(fp) == 64, "fingerprint must hash invalid-UTF-8 bytes without decoding them"


def test_row_fingerprint_over_bytes_input() -> None:
    class _NoGraph:
        def iter_nodes(self):  # pragma: no cover - trivial
            return iter(())

    fp1 = compute_row_fingerprint(_NoGraph(), {}, {"content": _PDF_LIKE})
    fp2 = compute_row_fingerprint(_NoGraph(), {}, {"content": _PDF_LIKE})
    fp3 = compute_row_fingerprint(_NoGraph(), {}, {"content": _PDF_LIKE + b"!"})
    assert fp1 == fp2, "compute_row_fingerprint must be stable for the same bytes"
    assert fp1 != fp3, "compute_row_fingerprint must react to a byte change in a source column"
