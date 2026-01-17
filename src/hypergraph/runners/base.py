"""Base runner abstract class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal

from hypergraph.runners._shared.types import RunnerCapabilities, RunResult

if TYPE_CHECKING:
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
        graph: "Graph",
        values: dict[str, Any],
        *,
        select: list[str] | None = None,
        max_iterations: int | None = None,
        **kwargs: Any,
    ) -> RunResult:
        """Execute a graph.

        Args:
            graph: The graph to execute
            values: Input values
            select: Optional list of outputs to return (None = all)
            max_iterations: Max iterations for cyclic graphs (None = default)
            **kwargs: Runner-specific options

        Returns:
            RunResult with output values and status
        """
        ...

    @abstractmethod
    def map(
        self,
        graph: "Graph",
        values: dict[str, Any],
        *,
        map_over: str | list[str],
        map_mode: Literal["zip", "product"] = "zip",
        select: list[str] | None = None,
        **kwargs: Any,
    ) -> list[RunResult]:
        """Execute a graph multiple times with different inputs.

        Args:
            graph: The graph to execute
            values: Input values (some should be lists for map_over)
            map_over: Parameter name(s) to iterate over
            map_mode: "zip" for parallel iteration, "product" for cartesian
            select: Optional list of outputs to return
            **kwargs: Runner-specific options

        Returns:
            List of RunResult, one per iteration
        """
        ...
