"""Public rich-display value returned by runner result inspection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar, cast

if TYPE_CHECKING:
    from hypergraph.runners._shared._inspect import RunInspection


ArtifactT = TypeVar("ArtifactT")


@dataclass(frozen=True, slots=True)
class InspectionDisplay(Generic[ArtifactT]):
    """Explicit rich display returned by ``RunResult.inspect()`` and ``MapResult.inspect()``."""

    artifact: ArtifactT

    def _repr_html_(self) -> str:
        """Render the artifact lazily so importing result types cannot cycle."""
        from hypergraph.runners._shared._inspect import MapInspection
        from hypergraph.runners._shared._inspect_html import (
            render_map_inspection,
            render_run_inspection,
        )

        if isinstance(self.artifact, MapInspection):
            return render_map_inspection(self.artifact)
        return render_run_inspection(cast("RunInspection", self.artifact))


__all__ = ["InspectionDisplay"]
