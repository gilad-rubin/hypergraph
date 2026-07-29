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


@dataclass(frozen=True, slots=True)
class RunOperations:
    """Drive independent child-page write plans under one bounded window."""

    operations: tuple[WriteOperation, ...]
    max_concurrency: int


WriteAction = RunGraph | RunOperations
WriteOperation = Generator[WriteAction, Any, Any]
