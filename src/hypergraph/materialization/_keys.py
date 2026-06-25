"""Definition hashing for HyperTable derive functions."""

from __future__ import annotations

import hashlib
import inspect
from typing import Any


def compute_definition_hash(fn: Any) -> str:
    """Hash the source code of a derive function (falls back to repr)."""
    try:
        source = inspect.getsource(fn)
    except (OSError, TypeError):
        source = repr(fn)
    return hashlib.sha256(source.encode()).hexdigest()
