"""BoundaryProjection - single owner of GraphNode parent/child address translation.

A GraphNode boundary answers one question in many shapes: "what is this port
called on the other side?". Inputs and outputs each have three name layers:

- original: the name inside the wrapped graph, before any rename
- local: the current name on the GraphNode surface, after rename_inputs/
  rename_outputs (still un-prefixed)
- address: the parent-facing name after projection (flat, ``name.local`` when
  namespaced, or the expose alias)

``BoundaryProjection`` precomputes every map between those layers exactly once
per boundary mutation. Validation, binding, defaults, selection, execution,
and inspection all consume this one object (directly or through the thin
GraphNode methods that delegate here); nothing else may rebuild rename or
projection semantics.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from hypergraph.nodes._rename import RenameEntry, build_forward_rename_map, build_reverse_rename_map


@dataclass(frozen=True)
class BoundaryProjection:
    """Immutable translation table for one GraphNode boundary.

    Built via :meth:`build` from the boundary's construction state (name,
    namespacing, expose aliases, local port lists, rename history). All maps
    are precomputed; every method is a pure lookup.
    """

    node_name: str
    namespaced: bool
    exposed: Mapping[str, str]
    former_names: tuple[str, ...]
    local_inputs: tuple[str, ...]
    local_outputs: tuple[str, ...]
    local_data_outputs: tuple[str, ...]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    data_outputs: tuple[str, ...]
    input_locals_by_address: Mapping[str, tuple[str, ...]]
    output_local_by_address: Mapping[str, str]
    output_address_by_local: Mapping[str, str]
    input_original_by_local: Mapping[str, str]
    input_local_by_original: Mapping[str, str]
    output_original_by_local: Mapping[str, str]
    output_local_by_original: Mapping[str, str]

    @classmethod
    def build(
        cls,
        *,
        node_name: str,
        namespaced: bool,
        exposed: Mapping[str, str],
        local_inputs: tuple[str, ...],
        local_outputs: tuple[str, ...],
        local_data_outputs: tuple[str, ...],
        rename_history: Sequence[RenameEntry],
    ) -> BoundaryProjection:
        """Compute the full projection for one boundary state.

        Raises:
            ValueError: If two outputs project to the same parent address, or
                an input and a *differently named* output share an address.
        """
        exposed = dict(exposed)
        former_names = tuple(entry.old for entry in rename_history if entry.kind == "name")
        history = list(rename_history)
        input_original_by_local = build_reverse_rename_map(history, "inputs")
        output_original_by_local = build_reverse_rename_map(history, "outputs")
        input_local_by_original = build_forward_rename_map(history, "inputs")
        output_local_by_original = build_forward_rename_map(history, "outputs")

        def project(local_name: str) -> str:
            if namespaced:
                if local_name in exposed:
                    return exposed[local_name]
                return f"{node_name}.{local_name}"
            return local_name

        input_address_to_local: dict[str, list[str]] = {}
        projected_inputs: list[str] = []
        for local in local_inputs:
            address = project(local)
            input_address_to_local.setdefault(address, []).append(local)
            if address not in projected_inputs:
                projected_inputs.append(address)

        output_local_to_address: dict[str, str] = {}
        projected_outputs: list[str] = []
        for local in local_outputs:
            address = project(local)
            if address in projected_outputs:
                raise ValueError(f"GraphNode '{node_name}' projects multiple outputs to {address!r}")
            input_locals = input_address_to_local.get(address, ())
            if input_locals and local not in input_locals:
                raise ValueError(
                    f"GraphNode '{node_name}' projects input(s) {input_locals!r} and output {local!r} "
                    f"to the same address {address!r}. Use the same local name for cyclic seed/update "
                    f"ports, or choose distinct aliases."
                )
            output_local_to_address[local] = address
            projected_outputs.append(address)

        return cls(
            node_name=node_name,
            namespaced=namespaced,
            exposed=exposed,
            former_names=former_names,
            local_inputs=local_inputs,
            local_outputs=local_outputs,
            local_data_outputs=local_data_outputs,
            inputs=tuple(projected_inputs),
            outputs=tuple(projected_outputs),
            data_outputs=tuple(project(local) for local in local_data_outputs if local in output_local_to_address),
            input_locals_by_address={address: tuple(locals_) for address, locals_ in input_address_to_local.items()},
            output_local_by_address={address: local for local, address in output_local_to_address.items()},
            output_address_by_local=output_local_to_address,
            input_original_by_local=input_original_by_local,
            input_local_by_original=input_local_by_original,
            output_original_by_local=output_original_by_local,
            output_local_by_original=output_local_by_original,
        )

    # === Layer hops (single-step lookups) ===

    def project_local(self, local_name: str) -> str:
        """Parent-facing address for a local port name."""
        if self.namespaced:
            if local_name in self.exposed:
                return self.exposed[local_name]
            return f"{self.node_name}.{local_name}"
        return local_name

    def locals_for_input(self, address: str) -> tuple[str, ...]:
        """Local input names behind a parent-facing address."""
        return self.input_locals_by_address.get(address, (address,))

    def local_for_output(self, address: str) -> str:
        """Local output name behind a parent-facing address."""
        return self.output_local_by_address.get(address, address)

    def original_input(self, local_name: str) -> str:
        """Original inner-graph name for a local input name."""
        return self.input_original_by_local.get(local_name, local_name)

    def original_output(self, local_name: str) -> str:
        """Original inner-graph name for a local output name."""
        return self.output_original_by_local.get(local_name, local_name)

    # === Address <-> original (composed hops) ===

    def original_inputs_for_address(self, address: str) -> tuple[str, ...]:
        """Original inner-graph names behind a parent-facing input address."""
        return tuple(self.original_input(local) for local in self.locals_for_input(address))

    def original_output_for_address(self, address: str) -> str:
        """Original inner-graph name behind a parent-facing output address."""
        return self.original_output(self.local_for_output(address))

    def input_address_for_original(self, original_name: str) -> str:
        """Parent-facing address for an original inner-graph input name."""
        return self.project_local(self.input_local_by_original.get(original_name, original_name))

    def output_address_for_original(self, original_name: str) -> str:
        """Parent-facing address for an original inner-graph output name."""
        local = self.output_local_by_original.get(original_name, original_name)
        return self.output_address_by_local.get(local, local)

    # === Dict translation (execution boundary) ===

    def translate_inputs(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Parent-address-keyed input values -> original inner-graph names."""
        mapped: dict[str, Any] = {}
        for address, value in values.items():
            for local in self.locals_for_input(address):
                mapped[self.original_input(local)] = value
        return mapped

    def translate_outputs(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Original-keyed output values -> parent-facing addresses.

        Emit-only local outputs the inner run did not produce are backfilled
        with the emit sentinel so downstream ordering edges stay satisfied.
        """
        from hypergraph.nodes.base import _EMIT_SENTINEL

        mapped = {self.output_address_for_original(key): value for key, value in values.items()}
        for local in self.local_outputs:
            if local in self.local_data_outputs:
                continue
            address = self.output_address_by_local.get(local)
            if address is not None and address not in mapped:
                mapped[address] = _EMIT_SENTINEL
        return mapped

    # === Structured keys ===

    def resume_key_from_original(self, resume_key: str) -> str:
        """Map a nested resume key from inner names to parent-facing names.

        If the key is on this boundary's current local output surface, map the
        whole key. This matters when a child namespaced GraphNode has already
        produced a local output address such as ``inner.decision``.

        Only the first path component can be renamed by the boundary: for
        example ``decision`` may become ``verdict`` while ``review.decision``
        stays unchanged because ``review`` is internal.
        """
        if resume_key in self.local_outputs:
            return self.output_address_for_original(resume_key)
        head, sep, tail = resume_key.partition(".")
        mapped_head = self.output_address_for_original(head)
        if not sep:
            return mapped_head
        return f"{mapped_head}.{tail}"

    def replacement_for_stale_input_address(self, address: str) -> str | None:
        """Current parent-facing input address for a stale namespaced one."""
        prefixes = [self.node_name, *self.former_names]
        prefix = next((f"{name}." for name in prefixes if address.startswith(f"{name}.")), None)
        if prefix is None:
            return None
        local_name = address[len(prefix) :]
        if local_name not in self.local_inputs:
            for current in self.local_inputs:
                if self.original_input(current) == local_name:
                    local_name = current
                    break
            else:
                if local_name in self.inputs:
                    return local_name
                return None
        current = self.project_local(local_name)
        return current if current != address else None

    # === Inspection maps (nx_attrs / viz dagre boundary renames) ===

    @property
    def input_name_map(self) -> dict[str, tuple[str, ...]]:
        """Parent input address -> original inner names (viz boundary renames)."""
        return {address: self.original_inputs_for_address(address) for address in self.inputs}

    @property
    def output_name_map(self) -> dict[str, str]:
        """Parent output address -> original inner name (viz boundary renames)."""
        return {address: self.original_output_for_address(address) for address in self.outputs}
