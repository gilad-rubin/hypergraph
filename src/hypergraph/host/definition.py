"""DefinitionId — the complete pinned identity of one served Definition.

Every durable submission pins the full typed identity
``DefinitionId(name, deployment_version, structural_hash)`` at accept time
(ADR 0007). Workers claim only submissions whose pinned identity they serve
exactly or via an explicit ``accepts=(DefinitionId(...), ...)`` declaration;
``structural_hash`` anchors fork compatibility checks.

The pinned hash is ``definition_struct_hash(graph)``, not the Graph's own
``structural_hash``: a graph modifier that changes WHAT RUNS is part of
Definition identity, because the worker executes the served object and
nothing else.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hypergraph.graph import Graph


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


def narrowing_description(graph: Graph) -> str:
    """Name the narrowing in the caller's own spelling, or ``""``.

    A chainable spelling — ``"select('cheap')"``,
    ``"select('cheap').with_entrypoint('costly')"`` — so a refusal can paste
    it straight after ``graph.`` and name a call site that exists.
    """
    parts = []
    if graph.selected is not None:
        parts.append("select(" + ", ".join(repr(name) for name in graph.selected) + ")")
    if graph.entrypoints_config is not None:
        parts.append("with_entrypoint(" + ", ".join(repr(name) for name in graph.entrypoints_config) + ")")
    return ".".join(parts)


def definition_struct_hash(graph: Graph) -> str:
    """The structural hash one Graph object pins as a Definition.

    ``Graph.structural_hash`` is topology and node interfaces alone, which is
    what fork compatibility needs: it must not move when a graph is narrowed
    for reading. But a Host resolves a submission by that hash and then runs
    the SERVED graph object, so a narrowed graph that hashed the same would
    silently run the unnarrowed Definition — the selection discarded without
    a word (#408).

    So a Definition folds the narrowing in: ``select()`` and
    ``with_entrypoint()`` change what runs, and a graph carrying either is a
    different Definition than its unnarrowed twin. A graph carrying neither
    hashes to ``graph.structural_hash`` byte for byte, so every stored
    submission keeps resolving.

    Both modifiers are order-insensitive in meaning — ``select("a", "b")``
    returns what ``select("b", "a")`` returns — so each is sorted before it
    is folded in, and a rebuild that spelled one in another order still
    resolves to the same Definition.
    """
    base = graph.structural_hash
    selected = graph.selected
    entrypoints = graph.entrypoints_config
    if selected is None and entrypoints is None:
        return base
    scope = json.dumps(
        {
            "selected": None if selected is None else sorted(selected),
            "entrypoints": None if entrypoints is None else sorted(entrypoints),
        },
        sort_keys=True,
    )
    return hashlib.sha256(f"{base}|{scope}".encode()).hexdigest()
