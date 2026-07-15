"""The single effect yielded by HyperTable write plans."""

from __future__ import annotations

from collections.abc import Generator, Mapping
from dataclasses import dataclass
from typing import Any

_Predicate = tuple[tuple[str, str, Any], ...]


@dataclass(frozen=True, slots=True)
class RunGraph:
    """Execute a graph with inputs through the table's configured runner."""

    graph: Any
    inputs: Mapping[str, Any]

    def input_values(self) -> dict[str, Any]:
        return dict(self.inputs)


WriteOperation = Generator[RunGraph, Any, Any]
