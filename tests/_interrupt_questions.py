"""Minimal structural ask fakes shared by Hypergraph's interrupt tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class StringQuestion:
    answer_type: ClassVar[object] = str
    prompt: str
    options: tuple[str, ...] | None = None
    evidence: tuple[object, ...] = ()


@dataclass(frozen=True)
class BoolQuestion:
    answer_type: ClassVar[object] = bool
    prompt: str
    options: tuple[str, ...] | None = None
    evidence: tuple[object, ...] = ()
