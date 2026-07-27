"""DefinitionId — the complete pinned identity of one served Definition.

Every durable submission pins the full typed identity
``DefinitionId(name, deployment_version, structural_hash)`` at accept time
(ADR 0007). Workers claim only submissions whose pinned identity they serve
exactly or via an explicit ``accepts=(DefinitionId(...), ...)`` declaration;
``structural_hash`` anchors fork compatibility checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DefinitionId:
    """Complete pinned identity of a served Definition.

    Attributes:
        name: Definition (graph) name.
        deployment_version: Human-set deployment version pinned by
            ``serve(deployment_version=...)``.
        structural_hash: Graph topology/interface hash (code excluded) that
            anchors compatibility checks.
    """

    name: str
    deployment_version: str
    structural_hash: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict for storage."""
        return {
            "name": self.name,
            "deployment_version": self.deployment_version,
            "structural_hash": self.structural_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DefinitionId:
        """Rebuild a DefinitionId from ``to_dict()`` output."""
        if not isinstance(data, dict):
            raise TypeError(f"DefinitionId.from_dict() expects a dict, got {type(data).__name__}.")
        try:
            name = data["name"]
            deployment_version = data["deployment_version"]
            structural_hash = data["structural_hash"]
        except KeyError as missing:
            raise ValueError(
                f"DefinitionId.from_dict() missing key {missing}; expected keys 'name', 'deployment_version', and 'structural_hash'."
            ) from None
        if not all(isinstance(value, str) for value in (name, deployment_version, structural_hash)):
            raise TypeError("DefinitionId 'name', 'deployment_version', and 'structural_hash' must be strings.")
        return cls(name=name, deployment_version=deployment_version, structural_hash=structural_hash)
