"""Base runner abstract class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal

from hypergraph.runners._shared.helpers import _UNSET_SELECT
from hypergraph.runners._shared.types import RunnerCapabilities, RunResult

if TYPE_CHECKING:
    from hypergraph.events.processor import EventProcessor
    from hypergraph.graph import Graph


class BaseRunner(ABC):
    """Abstract base class for all runners.

    Runners are responsible for executing graphs. Different runners provide
    different execution strategies (sync vs async, with or without caching, etc.)

    All runners must implement:
    - capabilities property: declares what features the runner supports
    - run(): execute a graph once
    - map(): execute a graph multiple times with different inputs
    """

    @property
    @abstractmethod
    def capabilities(self) -> RunnerCapabilities:
        """Declare what this runner supports.

        Returns:
            RunnerCapabilities describing runner features
        """
        ...

    @abstractmethod
    def run(
        self,
        graph: Graph,
        values: dict[str, Any] | None = None,
        *,
        select: str | list[str] = _UNSET_SELECT,
        on_missing: Literal["ignore", "warn", "error"] = "ignore",
        entrypoint: str | None = None,
        max_iterations: int | None = None,
        event_processors: list[EventProcessor] | None = None,
        **input_values: Any,
    ) -> RunResult:
        """Execute a graph.

        Args:
            graph: The graph to execute
            values: Optional input values dict
            select: Which outputs to return. "**" (default) = all outputs.
            on_missing: How to handle missing selected outputs.
            entrypoint: Optional explicit cycle entry point node name.
            max_iterations: Max iterations for cyclic graphs (None = default)
            event_processors: Optional list of event processors to receive execution events
            **input_values: Input values shorthand (merged with values)

        Returns:
            RunResult with output values and status
        """
        ...

    @abstractmethod
    def map(
        self,
        graph: Graph,
        values: dict[str, Any] | None = None,
        *,
        map_over: str | list[str],
        map_mode: Literal["zip", "product"] = "zip",
        clone: bool | list[str] = False,
        select: str | list[str] = _UNSET_SELECT,
        on_missing: Literal["ignore", "warn", "error"] = "ignore",
        event_processors: list[EventProcessor] | None = None,
        **input_values: Any,
    ) -> list[RunResult]:
        """Execute a graph multiple times with different inputs.

        Args:
            graph: The graph to execute
            values: Optional input values dict (some should be lists for map_over)
            map_over: Parameter name(s) to iterate over
            map_mode: "zip" for parallel iteration, "product" for cartesian
            clone: Deep-copy broadcast values per iteration.
                False (default) = share by reference.
                True = deep-copy all broadcast values.
                list[str] = deep-copy only named params.
            select: Which outputs to return. "**" (default) = all outputs.
            on_missing: How to handle missing selected outputs.
            event_processors: Optional list of event processors to receive execution events
            **input_values: Input values shorthand (merged with values)

        Returns:
            List of RunResult, one per iteration
        """
        ...
