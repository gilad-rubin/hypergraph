"""Shared browser-wire safety contracts."""

from __future__ import annotations

import pytest

from hypergraph.runners._shared._wire import sandboxed_child_document, script_safe_json

_DEFAULT_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; "
    "object-src 'none'; base-uri 'none'; form-action 'none'"
)


def test_script_safe_json_rejects_non_finite_numbers() -> None:
    with pytest.raises(ValueError, match="Out of range float values are not JSON compliant"):
        script_safe_json({"measurement": float("nan")})


def test_script_safe_json_escapes_inline_script_boundaries_and_separators() -> None:
    assert script_safe_json({"marker": "</script>&\u2028\u2029", "label": "caf\u00e9"}) == (
        '{"marker":"\\u003c/script\\u003e\\u0026\\u2028\\u2029","label":"caf\\u00e9"}'
    )


def test_sandboxed_child_document_wraps_body_with_default_csp() -> None:
    document = sandboxed_child_document("<main>Saved inspection</main>")

    assert document == (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" '
        f'content="{_DEFAULT_CSP}">'
        '</head><body style="margin:0"><main>Saved inspection</main></body></html>'
    )
