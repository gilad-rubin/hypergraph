"""Public rich-display value returned by runner result inspection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, TypeVar, cast

from hypergraph._repr import plain_reprs

if TYPE_CHECKING:
    from hypergraph.runners._shared._inspect import RunInspection


ArtifactT = TypeVar("ArtifactT")


@dataclass(frozen=True, slots=True)
class InspectionDisplay(Generic[ArtifactT]):
    """Presentation value returned by ``RunResult.inspect()`` and ``MapResult.inspect()``."""

    _artifact: ArtifactT = field(repr=False)

    def __repr__(self) -> str:
        """Summarize the run or map without exposing its private artifact."""
        from hypergraph.runners._shared._inspect import MapInspection

        artifact = self._artifact
        if isinstance(artifact, MapInspection):
            kind = "map"
            count = artifact.requested_count
            count_label = f"{count} item{'s' if count != 1 else ''}"
            status = artifact.status
            captured = artifact.captured
        else:
            run_artifact = cast("RunInspection", artifact)
            kind = "run"
            count = len(run_artifact.nodes)
            count_label = f"{count} node{'s' if count != 1 else ''}"
            status = run_artifact.status
            captured = run_artifact.captured
        capture_label = "captured" if captured else "degraded"
        return f"InspectionDisplay({kind} | {status} | {count_label} | {capture_label})"

    def _repr_html_(self) -> str | None:
        """Render the artifact lazily so importing result types cannot cycle."""
        if plain_reprs():
            return None

        from hypergraph.runners._shared._inspect import MapInspection
        from hypergraph.runners._shared._inspect_html import (
            render_inspection_frame,
            render_map_inspection,
            render_run_inspection,
        )

        artifact = self._artifact
        if isinstance(artifact, MapInspection):
            child_html = render_map_inspection(artifact)
        else:
            child_html = render_run_inspection(cast("RunInspection", artifact))
        return render_inspection_frame(child_html)


__all__ = ["InspectionDisplay"]
