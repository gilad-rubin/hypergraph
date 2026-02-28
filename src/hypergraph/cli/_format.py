"""Formatting utilities for CLI output.

Handles human-readable tables, value truncation, progressive disclosure,
and JSON envelope wrapping.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

# JSON envelope version — bump on breaking changes to JSON structure
SCHEMA_VERSION = 2

# Default limits
DEFAULT_LIMIT = 20
MAX_LINES = 100


def json_envelope(command: str, data: Any) -> dict[str, Any]:
    """Wrap data in the standard JSON output envelope."""
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }


def print_json(command: str, data: Any, output: str | None = None) -> None:
    """Print JSON envelope to stdout or write to file."""
    envelope = json_envelope(command, data)
    text = json.dumps(envelope, indent=2, default=str)

    if output:
        with open(output, "w") as f:
            f.write(text)
        size_kb = len(text.encode()) / 1024
        print(f"Wrote {command} output to {output} ({size_kb:.1f}KB)")
    else:
        print(text)


def format_duration(ms: float | None) -> str:
    """Format milliseconds into human-readable duration."""
    if ms is None or ms == 0:
        return "—"
    if ms < 1000:
        return f"{ms:.0f}ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    remaining = seconds % 60
    return f"{minutes}m{remaining:04.1f}s"


def format_datetime(dt: datetime | str | None) -> str:
    """Format datetime for display."""
    if dt is None:
        return "—"
    if isinstance(dt, str):
        return dt[:19]  # Trim microseconds
    return dt.strftime("%Y-%m-%d %H:%M")


def format_status(status: str) -> str:
    """Format status string — uppercase for failures, lowercase for others."""
    if status in ("failed", "FAILED"):
        return "FAILED"
    return status


def describe_value(value: Any) -> tuple[str, str]:
    """Describe a value's type and size for progressive disclosure.

    Returns (type_str, size_str).
    """
    if value is None:
        return "—", "—"
    if isinstance(value, list):
        return "list", f"{len(value)} items"
    if isinstance(value, dict):
        return "dict", f"{len(value)} keys"
    if isinstance(value, str):
        size = len(value.encode("utf-8"))
        if size < 1024:
            return "str", f"{size}B"
        return "str", f"{size / 1024:.1f}KB"
    if isinstance(value, (int, float)):
        return type(value).__name__, str(value)
    if isinstance(value, bool):
        return "bool", str(value)
    return type(value).__name__, "—"


def truncate_value(value: Any, max_chars: int = 200) -> str:
    """Truncate a value for display."""
    text = json.dumps(value, default=str) if not isinstance(value, str) else value
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def print_table(headers: list[str], rows: list[list[str]], indent: int = 2) -> list[str]:
    """Format a table with aligned columns.

    Returns list of lines (does not print).
    """
    if not rows:
        return []

    # Calculate column widths
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))

    prefix = " " * indent
    lines = []

    # Header
    header_line = prefix + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    lines.append(header_line)

    # Separator
    sep_line = prefix + "  ".join("─" * w for w in widths)
    lines.append(sep_line)

    # Rows
    for row in rows:
        cells = []
        for i, cell in enumerate(row):
            if i < len(widths):
                # Right-align numeric-looking columns (Step, Duration, Steps)
                if headers[i] in ("Step", "Duration", "Steps"):
                    cells.append(cell.rjust(widths[i]))
                else:
                    cells.append(cell.ljust(widths[i]))
        lines.append(prefix + "  ".join(cells))

    return lines


def print_lines(lines: list[str], max_lines: int = MAX_LINES) -> None:
    """Print lines with truncation warning if too many."""
    if len(lines) <= max_lines:
        for line in lines:
            print(line)
    else:
        for line in lines[:max_lines]:
            print(line)
        remaining = len(lines) - max_lines
        print(f"\n  # ... {remaining} more lines (use --limit or --all to control)")


def print_ctas(ctas: list[str]) -> None:
    """Print context-aware next-step suggestions after command output."""
    print()
    for cta in ctas:
        print(f"  → {cta}")


_SINCE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_since(since_str: str) -> datetime:
    """Parse a human-friendly time delta into a UTC datetime.

    Supports: 30s, 5m, 1h, 7d, 2w.
    """
    import re
    from datetime import timedelta

    match = re.fullmatch(r"(\d+)([smhdw])", since_str.strip())
    if not match:
        raise ValueError(f"Invalid --since value: '{since_str}'. Use e.g. 30s, 5m, 1h, 7d, 2w.")

    amount = int(match.group(1))
    unit = match.group(2)
    seconds = amount * _SINCE_UNITS[unit]
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)
