"""Base runner abstract class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal

from hypergraph.runners._shared.outputs import SELECT_UNSET
from hypergraph.runners._shared.results import ErrorHandling, MapResult, RunResult
from hypergraph.runners._shared.state import RunnerCapabilities

if TYPE_CHECKING:
    from hypergraph.checkpointers.base import Checkpointer
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

    def with_checkpointer(self, checkpointer: Checkpointer) -> BaseRunner:
        """Return a shallow clone of this runner bound to ``checkpointer``.

        The supplied runner is never mutated: the clone gets its own
        workflow registry, so in-process duplicate-active protection stays
        per-instance, and its executor wiring is rebuilt against the clone
        (GraphNode executors capture their runner — sharing the original's
        executors would run nested child workflows against the wrong
        checkpointer and live registry). Used by ``serve()`` to bind
        Definition runners to a Run Home.

        Raises:
            TypeError: If this runner class has no checkpointer seam
                (e.g. ``DaftRunner``) and cannot serve durable runs.
        """
        import copy

        if "_checkpointer_instance" not in self.__dict__:
            raise TypeError(f"{type(self).__name__} does not support checkpointer binding; it cannot execute durable runs in a host worker.")
        new_runner = copy.copy(self)
        new_runner._checkpointer_instance = checkpointer
        if "_active_workflows" in new_runner.__dict__:
            from hypergraph.runners._shared.stop import _ActiveWorkflows

            new_runner._active_workflows = _ActiveWorkflows()
        build_executors = getattr(new_runner, "_build_executors", None)
        if build_executors is not None:
            new_runner._executors = build_executors()
        return new_runner

    def has_active_run(self, workflow_id: str) -> bool:
        """True while a run with this workflow_id is live (``stop()`` would land).

        Runners without a live registry return False.
        """
        active = self.__dict__.get("_active_workflows")
        if active is None:
            return False
        return active.has(workflow_id)

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
        select: str | list[str] = SELECT_UNSET,
        on_missing: Literal["ignore", "warn", "error"] = "ignore",
        entrypoint: str | None = None,
        max_iterations: int | None = None,
        error_handling: ErrorHandling = "raise",
        event_processors: list[EventProcessor] | None = None,
        show_progress: bool | None = None,
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
            error_handling: How to handle node execution errors.
                "raise" (default) re-raises the original exception.
                "continue" returns RunResult with status=FAILED and partial values.
            event_processors: Optional list of event processors to receive execution events
            show_progress: Override runner-level show_progress for this call.
                None = use runner default, True/False = explicit override.
            **input_values: Input values shorthand for flat graph input names.
                Use values for dotted/nested inputs or names that match runner options.

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
        select: str | list[str] = SELECT_UNSET,
        on_missing: Literal["ignore", "warn", "error"] = "ignore",
        event_processors: list[EventProcessor] | None = None,
        show_progress: bool | None = None,
        **input_values: Any,
    ) -> MapResult:
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
            show_progress: Override runner-level show_progress for this call.
                None = use runner default, True/False = explicit override.
            **input_values: Input values shorthand for flat graph input names.
                Use values for dotted/nested inputs or names that match runner options.

        Returns:
            MapResult wrapping per-iteration RunResults with batch metadata
        """
        ...
