"""Effective per-node retry/timeout policy manifest (#232).

The manifest is persisted with run configuration and validated on
same-``workflow_id`` resume BEFORE checkpoint restoration and before
``create_run()`` can overwrite the stored config. It is resume-compatibility
identity ONLY, deliberately separate from the graph's definition/code/
structural hashes and from successful-output cache keys — changing a retry
budget never invalidates cached successes.

Field normalization mirrors the canonical :attr:`RetryPolicy.fingerprint`:
``retry_on`` is stored as sorted ``module.qualname`` strings, timing fields
as floats. Jitter draws are attempt state and never appear here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hypergraph.nodes.function import FunctionNode
from hypergraph.nodes.retry import qualified_exception_name

if TYPE_CHECKING:
    from hypergraph.graph import Graph

#: Run-config key carrying the serialized manifest. Present (possibly ``[]``)
#: for every run recorded at or after #232; absent means a legacy config whose
#: policy compatibility is enforced only by the attempt ledger's
#: ``begin_attempt()`` fingerprint check.
RETRY_POLICY_CONFIG_KEY = "retry_policies"

#: Fields that define a node's effective policy, in diagnostic order.
#: ``policy_fingerprint`` is derived from these and excluded from diffs.
_POLICY_FIELDS = (
    "max_attempts",
    "retry_on",
    "retry_window",
    "initial_delay",
    "backoff_multiplier",
    "max_delay",
    "jitter",
    "timeout",
)


@dataclass(frozen=True)
class NodePolicyRecord:
    """One node's effective retry/timeout declaration, normalized.

    ``retry_on`` holds sorted ``module.qualname`` strings (builtins bare),
    so equivalent declaration orders produce identical records. A node with
    only ``timeout=`` has every retry field ``None``/empty and no
    ``policy_fingerprint``.
    """

    node_name: str
    policy_fingerprint: str | None
    max_attempts: int | None
    retry_on: tuple[str, ...]
    retry_window: float | None
    initial_delay: float | None
    backoff_multiplier: float | None
    max_delay: float | None
    jitter: str | None
    timeout: float | None

    @classmethod
    def from_node(cls, node: FunctionNode) -> NodePolicyRecord:
        policy = node.retry
        if policy is None:
            return cls(
                node_name=node.name,
                policy_fingerprint=None,
                max_attempts=None,
                retry_on=(),
                retry_window=None,
                initial_delay=None,
                backoff_multiplier=None,
                max_delay=None,
                jitter=None,
                timeout=node.timeout,
            )
        return cls(
            node_name=node.name,
            policy_fingerprint=policy.fingerprint,
            max_attempts=policy.max_attempts,
            retry_on=tuple(sorted(qualified_exception_name(entry) for entry in policy.retry_on)),
            retry_window=policy.retry_window,
            initial_delay=policy.initial_delay,
            backoff_multiplier=policy.backoff_multiplier,
            max_delay=policy.max_delay,
            jitter=policy.jitter,
            timeout=node.timeout,
        )

    def to_config_value(self) -> dict[str, Any]:
        """JSON-friendly encoding (tuples become lists)."""
        return {
            "node": self.node_name,
            "fingerprint": self.policy_fingerprint,
            "max_attempts": self.max_attempts,
            "retry_on": list(self.retry_on),
            "retry_window": self.retry_window,
            "initial_delay": self.initial_delay,
            "backoff_multiplier": self.backoff_multiplier,
            "max_delay": self.max_delay,
            "jitter": self.jitter,
            "timeout": self.timeout,
        }

    @classmethod
    def _from_config_value(cls, raw: dict[str, Any]) -> NodePolicyRecord:
        """Decode one entry; raises on malformed input (caller handles)."""
        node_name = raw["node"]
        retry_on = raw["retry_on"]
        if not isinstance(node_name, str) or not isinstance(retry_on, list):
            raise TypeError(f"malformed manifest entry: {raw!r}")
        return cls(
            node_name=node_name,
            policy_fingerprint=raw["fingerprint"],
            max_attempts=raw["max_attempts"],
            retry_on=tuple(retry_on),
            retry_window=raw["retry_window"],
            initial_delay=raw["initial_delay"],
            backoff_multiplier=raw["backoff_multiplier"],
            max_delay=raw["max_delay"],
            jitter=raw["jitter"],
            timeout=raw["timeout"],
        )


def _absent_record(node_name: str) -> NodePolicyRecord:
    """The neutral record a manifest side contributes for a missing node."""
    return NodePolicyRecord(
        node_name=node_name,
        policy_fingerprint=None,
        max_attempts=None,
        retry_on=(),
        retry_window=None,
        initial_delay=None,
        backoff_multiplier=None,
        max_delay=None,
        jitter=None,
        timeout=None,
    )


@dataclass(frozen=True)
class RetryPolicyManifest:
    """All policy-bearing nodes of one graph level, sorted by node name.

    Nested ``GraphNode`` children are deliberately absent: each child run
    persists and validates the manifest of its own graph at its own resume
    boundary, mirroring how ``graph_struct_hash`` already layers.
    """

    entries: tuple[NodePolicyRecord, ...]

    @classmethod
    def from_graph(cls, graph: Graph) -> RetryPolicyManifest:
        entries = tuple(
            NodePolicyRecord.from_node(node)
            for _, node in sorted(graph._nodes.items())
            if isinstance(node, FunctionNode) and (node.retry is not None or node.timeout is not None)
        )
        return cls(entries=entries)

    def to_config_value(self) -> list[dict[str, Any]]:
        return [entry.to_config_value() for entry in self.entries]

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> RetryPolicyManifest | None:
        """Decode a stored run config. ``None`` when absent or unreadable.

        A malformed value is treated as absent rather than raised: resume
        validation then falls back to the ledger's ``begin_attempt()``
        fingerprint check instead of bricking the workflow on corrupt config.
        """
        if not config:
            return None
        raw = config.get(RETRY_POLICY_CONFIG_KEY)
        if not isinstance(raw, list):
            return None
        try:
            entries = tuple(sorted((NodePolicyRecord._from_config_value(item) for item in raw), key=lambda entry: entry.node_name))
        except (KeyError, TypeError):
            return None
        return cls(entries=entries)


@dataclass(frozen=True)
class PolicyFieldChange:
    """One field-level difference between stored and current policy."""

    node_name: str
    field: str
    stored: Any
    current: Any


def diff_policy_manifests(stored: RetryPolicyManifest, current: RetryPolicyManifest) -> tuple[PolicyFieldChange, ...]:
    """Field-level differences, deterministic by node then field order.

    A node present on only one side is compared against the neutral
    no-policy record, so added/removed declarations surface as ordinary
    field changes (for example ``max_attempts: None -> 3``).
    """
    stored_by_name = {entry.node_name: entry for entry in stored.entries}
    current_by_name = {entry.node_name: entry for entry in current.entries}
    changes: list[PolicyFieldChange] = []
    for node_name in sorted(stored_by_name.keys() | current_by_name.keys()):
        stored_entry = stored_by_name.get(node_name) or _absent_record(node_name)
        current_entry = current_by_name.get(node_name) or _absent_record(node_name)
        for field in _POLICY_FIELDS:
            stored_value = getattr(stored_entry, field)
            current_value = getattr(current_entry, field)
            if stored_value != current_value:
                changes.append(
                    PolicyFieldChange(
                        node_name=node_name,
                        field=field,
                        stored=stored_value,
                        current=current_value,
                    )
                )
    return tuple(changes)
