"""Shared safety helpers for Python-to-browser wire content."""

from __future__ import annotations

import html
import json

_DEFAULT_CHILD_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; "
    "object-src 'none'; base-uri 'none'; form-action 'none'"
)


def script_safe_json(value: object) -> str:
    """Encode strict JSON for embedding inside an inline script element."""
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    )
    return encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def sandboxed_child_document(body: str, *, csp: str = _DEFAULT_CHILD_CSP) -> str:
    """Wrap trusted body markup in the isolated document used by sandboxed frames."""
    escaped_csp = html.escape(csp, quote=False).replace('"', "&quot;")
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" '
        f'content="{escaped_csp}">'
        '</head><body style="margin:0">'
        f"{body}"
        "</body></html>"
    )
