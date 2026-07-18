"""Persisted materialization branches over one HyperTable root."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Generator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hypergraph import Graph
from hypergraph.graph import GraphConfigError
from hypergraph.materialization._branch_registry import (
    ArtifactRecord,
    BranchRecord,
    GrainRecord,
    OutputRecord,
    load_branch_records,
    save_branch_record,
)
from hypergraph.materialization._indexes import BranchIndexBinding, IndexPolicy
from hypergraph.materialization._provenance import Provenance, normalize_value, split_boundary_provenance
from hypergraph.materialization._recipe_journal import RecipeJournal
from hypergraph.materialization._schema import (
    PARENT_LINK_COLUMN,
    PROVENANCE_PREFIX,
    QUESTION_COLUMN,
    RECIPE_COLUMN,
    ColumnSpec,
    TableSpec,
    analyze_table,
)
from hypergraph.materialization._types import RowReceipt, RowStatus, TableReceipt, TableStatus, WriteOutcome
from hypergraph.materialization._write_actions import RunGraph, WriteOperation
from hypergraph.materialization._writes import dedup_child_rows, dedup_rows, normalize_to_dict

if TYPE_CHECKING:
    from hypergraph.materialization._hypertable import HyperTable


def _digest(*parts: Any) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _run_values(result: Any) -> dict[str, Any]:
    if hasattr(result, "values") and isinstance(result.values, dict):
        return result.values
    if isinstance(result, dict):
        return result
    return {}


def _namespace(name: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_") or "branch"
    return f"{readable[:32]}_{_digest(name)[:8]}"


@dataclass(frozen=True)
class MaterializedArtifact:
    """One stored column or child table reached by a Materialization Branch."""

    table: str
    column: str | None
    lineage: str
    references: int

    @property
    def shared(self) -> bool:
        """Whether more than one persisted plan currently reaches the artifact."""

        return self.references > 1


@dataclass(frozen=True)
class _LogicalGrain:
    key: str
    spec: TableSpec
    lineage: str
    column_lineages: dict[str, str]
    column_recipes: dict[str, str | None]
    boundary_node: Any | None = None


@dataclass(frozen=True)
class _Layout:
    source_graph: Graph
    root_graph: Graph
    spec: TableSpec
    components: dict[str, Any]
    provenance: Provenance
    grains: tuple[_LogicalGrain, ...]

    def grain(self, key: str) -> _LogicalGrain:
        for grain in self.grains:
            if grain.key == key:
                return grain
        raise KeyError(key)


def _node_lineage(provenance: Provenance, node: Any, known: Mapping[str, str], output: str) -> str:
    inputs = []
    for name in getattr(node, "inputs", ()):
        if name in provenance.components:
            continue
        inputs.append((name, known.get(name, _digest("external", name))))
    return _digest("column", output, provenance.node_recipe(node), sorted(inputs))


def _column_lineages(provenance: Provenance, spec: TableSpec, seeds: dict[str, str]) -> tuple[dict[str, str], dict[str, str | None]]:
    lineages = dict(seeds)
    recipes: dict[str, str | None] = {name: None for name in seeds}
    for node in provenance.nodes_in_dependency_order(spec):
        for column in provenance.node_columns(node, spec):
            producers = provenance.column_producers(column)
            producer_lineages = [_node_lineage(provenance, producer, lineages, column.name) for producer in producers]
            lineages[column.name] = _digest("producers", sorted(producer_lineages))
            recipes[column.name] = provenance.column_recipe(column)
    return lineages, recipes


def _build_layout(graph: Graph, identity: str, root_name: str) -> _Layout:
    if not isinstance(graph, Graph):
        raise TypeError(
            "HyperTable.attach() requires graph=Graph(...).\n\n"
            f"Received: {type(graph).__name__}\n\n"
            "How to fix: pass the configured derivation Graph returned by the recipe space."
        )
    components = dict(graph._bound)
    graph_nodes = list(graph.nodes.values()) if isinstance(graph.nodes, dict) else []
    if not graph_nodes:
        raise GraphConfigError(
            "HyperTable.attach() requires a graph with derivation nodes.\n\n"
            f"Graph: {graph.name or 'unnamed'}\n\n"
            "How to fix: pass the complete configured search-index recipe Graph."
        )
    mapped = [node for node in graph_nodes if getattr(node, "_map_config", None)]
    if mapped:
        root_graph = Graph([node for node in graph_nodes if node not in mapped], name=graph.name)
        root_bindings = {key: value for key, value in components.items() if key in set(root_graph.inputs.all)}
        if root_bindings:
            root_graph = root_graph.bind(root_bindings)
    else:
        root_graph = graph
    spec = analyze_table(root_graph, identity, components, mapped, name=root_name)
    provenance = Provenance(root_graph, spec, components, {})

    root_seed = {
        identity: _digest("root-identity", root_name, identity),
        **{column.name: _digest("root-source", root_name, column.name) for column in spec.columns if column.role == "source"},
    }
    root_lineages, root_recipes = _column_lineages(provenance, spec, root_seed)
    root_grain = _LogicalGrain(
        key="root",
        spec=spec,
        lineage=_digest("root-grain", root_name, identity),
        column_lineages=root_lineages,
        column_recipes=root_recipes,
    )
    grains = [root_grain]
    for child_spec in spec.children:
        boundary = provenance.boundary_node(child_spec)
        if boundary is None or child_spec.map_input is None:
            raise GraphConfigError(
                "HyperTable.attach() could not resolve a child-grain boundary.\n\n"
                f"Child table: {child_spec.name!r}\n\n"
                "How to fix: produce the mapped item list from one named graph node before map_over()."
            )
        boundary_lineage = _node_lineage(provenance, boundary, root_lineages, child_spec.map_input)
        grain_lineage = _digest("child-grain", root_grain.lineage, boundary_lineage, child_spec.identity)
        child_seed = {
            child_spec.identity: _digest("child-identity", grain_lineage, child_spec.identity),
            **{column.name: _digest("child-source", grain_lineage, column.name) for column in child_spec.columns if column.role == "source"},
        }
        child_lineages, child_recipes = _column_lineages(provenance, child_spec, child_seed)
        grains.append(
            _LogicalGrain(
                key=f"child:{child_spec.name}",
                spec=child_spec,
                lineage=grain_lineage,
                column_lineages=child_lineages,
                column_recipes=child_recipes,
                boundary_node=boundary,
            )
        )
    return _Layout(graph, root_graph, spec, components, provenance, tuple(grains))


def _base_plan(layout: _Layout) -> dict[str, GrainRecord]:
    grains: dict[str, GrainRecord] = {}
    for grain in layout.grains:
        grains[grain.key] = GrainRecord(
            logical_table=grain.spec.name,
            physical_table=grain.spec.name,
            lineage=grain.lineage,
            identity=grain.spec.identity,
            map_input=grain.spec.map_input,
            boundary_physical=grain.spec.map_input,
            columns={
                logical: ArtifactRecord(
                    physical=logical,
                    lineage=lineage,
                    recipe=grain.column_recipes.get(logical),
                    role=next((column.role for column in grain.spec.columns if column.name == logical), "identity"),
                )
                for logical, lineage in grain.column_lineages.items()
            },
        )
    return grains


def _artifact_catalog(
    base: Mapping[str, GrainRecord], branches: Mapping[str, BranchRecord]
) -> tuple[dict[tuple[str, str], tuple[ArtifactRecord, str]], dict[str, GrainRecord]]:
    artifacts: dict[tuple[str, str], tuple[ArtifactRecord, str]] = {}
    grains: dict[str, GrainRecord] = {}
    for plan in (base, *(record.grains for record in branches.values())):
        for grain in plan.values():
            grains.setdefault(grain.lineage, grain)
            for column in grain.columns.values():
                artifacts.setdefault((grain.lineage, column.lineage), (column, grain.physical_table))
    return artifacts, grains


def _allocate_name(logical: str, attachment_id: str, used: set[str]) -> str:
    candidate = f"{logical}__{_namespace(attachment_id)}"
    if candidate not in used:
        return candidate
    return f"{candidate}_{_digest(logical, attachment_id)[:8]}"


def _new_branch_record(
    layout: _Layout,
    attachment_id: str,
    outputs: Mapping[str, str],
    base: Mapping[str, GrainRecord],
    branches: Mapping[str, BranchRecord],
) -> BranchRecord:
    artifacts, known_grains = _artifact_catalog(base, branches)
    used_tables = {grain.physical_table for grain in known_grains.values()}
    used_columns: dict[str, set[str]] = {}
    for artifact, table in artifacts.values():
        used_columns.setdefault(table, set()).add(artifact.physical)

    planned_grains: dict[str, GrainRecord] = {}
    for logical_grain in layout.grains:
        existing_grain = known_grains.get(logical_grain.lineage)
        if logical_grain.key == "root":
            physical_table = layout.spec.name
            boundary_physical = None
        elif existing_grain is not None:
            physical_table = existing_grain.physical_table
            boundary_physical = existing_grain.boundary_physical
        else:
            physical_table = _allocate_name(logical_grain.spec.name, attachment_id, used_tables)
            used_tables.add(physical_table)
            root_columns = used_columns.setdefault(layout.spec.name, set())
            boundary_physical = _allocate_name(str(logical_grain.spec.map_input), attachment_id, root_columns)
            root_columns.add(boundary_physical)
        physical_columns = used_columns.setdefault(physical_table, set())
        columns: dict[str, ArtifactRecord] = {}
        for logical, lineage in logical_grain.column_lineages.items():
            match = artifacts.get((logical_grain.lineage, lineage))
            if match is not None:
                physical = match[0].physical
            elif (logical_grain.key != "root" and existing_grain is None) or logical == logical_grain.spec.identity:
                physical = logical
            else:
                physical = _allocate_name(logical, attachment_id, physical_columns)
            physical_columns.add(physical)
            columns[logical] = ArtifactRecord(
                physical=physical,
                lineage=lineage,
                recipe=logical_grain.column_recipes.get(logical),
                role=next((column.role for column in logical_grain.spec.columns if column.name == logical), "identity"),
            )
        planned_grains[logical_grain.key] = GrainRecord(
            logical_table=logical_grain.spec.name,
            physical_table=physical_table,
            lineage=logical_grain.lineage,
            identity=logical_grain.spec.identity,
            map_input=logical_grain.spec.map_input,
            boundary_physical=boundary_physical,
            columns=columns,
        )

    resolved_outputs: dict[str, OutputRecord] = {}
    for alias, logical in outputs.items():
        matches = [(key, column) for key, grain in planned_grains.items() if (column := grain.columns.get(logical)) is not None]
        if len(matches) != 1:
            locations = [f"{key}.{logical}" for key, _column in matches] or ["none"]
            raise GraphConfigError(
                "HyperTable.attach() output does not resolve to one materialized column.\n\n"
                f"Output: {alias!r} -> {logical!r}\nResolved locations: {', '.join(locations)}\n\n"
                "How to fix: choose a unique source or derived column from the configured recipe."
            )
        grain_key, column = matches[0]
        resolved_outputs[alias] = OutputRecord(grain=grain_key, logical=logical, artifact=column)

    signature = _digest(
        "materialization-branch-v1",
        [(key, grain.lineage, sorted((name, column.lineage) for name, column in grain.columns.items())) for key, grain in planned_grains.items()],
        sorted((alias, output.grain, output.logical, output.artifact.lineage) for alias, output in resolved_outputs.items()),
    )
    return BranchRecord(1, attachment_id, layout.spec.name, signature, resolved_outputs, planned_grains)


class BranchPolicy:
    """Attach or reopen persisted branches for one analyzed HyperTable."""

    def __init__(self, root: HyperTable) -> None:
        self._root = root

    def attach(self, name: str, graph: Graph, outputs: Mapping[str, str]) -> MaterializationBranch:
        if not name:
            raise GraphConfigError(
                "HyperTable.attach() requires a stable attachment name.\n\n"
                "Received an empty name.\n\n"
                "How to fix: pass the stable Search Index id, not its display label."
            )
        if not outputs:
            raise GraphConfigError(
                "HyperTable.attach() requires terminal outputs.\n\n"
                "Received no output bindings.\n\n"
                "How to fix: pass outputs={'text': 'chunk_text', 'vector': 'vector'}."
            )
        if not self._root._store.supports_manifests():
            raise NotImplementedError(
                f"{type(self._root._store).__name__} cannot persist Materialization Branches.\n\n"
                "The store does not implement save_manifest/load_manifest.\n\n"
                "How to fix: implement both manifest hooks or use LanceDBStore."
            )
        layout = _build_layout(graph, self._root._identity, self._root.table_name)
        base_layout = _build_layout(self._root.graph, self._root._identity, self._root.table_name)
        base = _base_plan(base_layout)
        branches = load_branch_records(self._root._store, self._root.table_name)
        candidate = _new_branch_record(layout, name, outputs, base, branches)
        existing = branches.get(name)
        if existing is not None:
            if existing.signature != candidate.signature:
                raise GraphConfigError(
                    "A Materialization Branch with this name already has a different recipe.\n\n"
                    f"Branch: {name!r}\n\n"
                    "How to fix: reopen it with the original complete recipe, or attach the changed recipe under a new stable id."
                )
            record = existing
        else:
            save_branch_record(self._root._store, self._root.table_name, candidate)
            record = candidate
        return MaterializationBranch(self._root, layout, record, base)


class MaterializationBranch:
    """A persisted lineage plan rooted in a HyperTable's current source rows."""

    def __init__(
        self,
        root: HyperTable,
        layout: _Layout,
        record: BranchRecord,
        base: dict[str, GrainRecord],
    ) -> None:
        self._root = root
        self._layout = layout
        self._record = record
        self._base = base
        self._journal = RecipeJournal(root._store)

    @property
    def name(self) -> str:
        return self._record.name

    def _plans(self) -> tuple[dict[str, GrainRecord], ...]:
        registered = load_branch_records(self._root._store, self._root.table_name)
        return (self._base, *(record.grains for record in registered.values()))

    def _reference_count(self, lineage: str, table: str, column: str | None) -> int:
        references = 0
        for plan in self._plans():
            if column is None:
                references += any(grain.physical_table == table and grain.lineage == lineage for grain in plan.values())
            else:
                seen = {(grain.physical_table, artifact.physical, artifact.lineage) for grain in plan.values() for artifact in grain.columns.values()}
                references += (table, column, lineage) in seen
        return references

    def artifacts(self) -> tuple[MaterializedArtifact, ...]:
        """Project every column and child-table artifact reachable by this branch."""

        artifacts: list[MaterializedArtifact] = []
        output_columns = {(output.grain, output.logical) for output in self._record.outputs.values()}
        for grain_key, grain in self._record.grains.items():
            if grain_key != "root":
                artifacts.append(
                    MaterializedArtifact(
                        grain.physical_table,
                        None,
                        grain.lineage,
                        self._reference_count(grain.lineage, grain.physical_table, None),
                    )
                )
            for logical, column in grain.columns.items():
                if column.role in ("identity", "internal"):
                    continue
                if grain_key == "root" and column.role == "source" and (grain_key, logical) not in output_columns:
                    continue
                artifacts.append(
                    MaterializedArtifact(
                        grain.physical_table,
                        column.physical,
                        column.lineage,
                        self._reference_count(column.lineage, grain.physical_table, column.physical),
                    )
                )
        return tuple(artifacts)

    def output(self, name: str) -> MaterializedArtifact:
        """Resolve one declared terminal output to its physical artifact."""

        try:
            output = self._record.outputs[name]
        except KeyError as error:
            known = ", ".join(sorted(self._record.outputs)) or "none"
            raise KeyError(
                "unknown Materialization Branch output.\n\n"
                f"Requested: {name!r}\nAvailable: {known}\n\n"
                "How to fix: pass one of the aliases declared in attach(outputs=...)."
            ) from error
        grain = self._record.grains[output.grain]
        table = grain.physical_table
        column = output.artifact.physical
        lineage = output.artifact.lineage
        return MaterializedArtifact(table, column, lineage, self._reference_count(lineage, table, column))

    def _physical_name(self, grain_key: str, logical: str) -> str:
        return self._record.grains[grain_key].columns[logical].physical

    def _logical_values(self, grain: _LogicalGrain, grain_key: str, row: Mapping[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for logical in grain.column_lineages:
            physical = self._physical_name(grain_key, logical)
            if physical in row and row[physical] is not None:
                values[logical] = normalize_value(row[physical])
        return values

    def _record_recipe(self, node: Any) -> None:
        for entry in self._layout.provenance.recipe_entries(node):
            self._journal.record(entry.hash, entry.kind, entry.payload)

    def _converge_columns(
        self,
        grain: _LogicalGrain,
        grain_key: str,
        existing: Mapping[str, Any],
        values: dict[str, Any],
    ) -> Generator[RunGraph, Any, dict[str, Any]]:
        changes: dict[str, Any] = {}
        for node in self._layout.provenance.nodes_in_dependency_order(grain.spec):
            provenance = self._layout.provenance.node_provenance(node, values)
            if provenance is None:
                raise RuntimeError(
                    "Materialization Branch cannot derive a column from the stored lineage.\n\n"
                    f"Node: {node.name!r}\n\n"
                    "How to fix: keep every required recipe input as a source or derived column in the branch graph."
                )
            columns = self._layout.provenance.node_columns(node, grain.spec)
            fresh = all(
                (physical := self._physical_name(grain_key, column.name)) in existing
                and existing.get(physical) is not None
                and existing.get(f"{PROVENANCE_PREFIX}{physical}") == provenance
                for column in columns
            )
            if fresh:
                for column in columns:
                    values[column.name] = normalize_value(existing[self._physical_name(grain_key, column.name)])
                continue
            result = yield RunGraph(
                self._layout.provenance.column_graph(node),
                self._layout.provenance.node_inputs(node, values),
            )
            outputs = _run_values(result)
            for column in columns:
                if column.name not in outputs:
                    continue
                physical = self._physical_name(grain_key, column.name)
                value = normalize_value(outputs[column.name])
                values[column.name] = value
                changes[physical] = value
                changes[f"{PROVENANCE_PREFIX}{physical}"] = provenance
            self._record_recipe(node)
        return changes

    def _physical_spec(self, grain: _LogicalGrain, grain_key: str) -> TableSpec:
        plan = self._record.grains[grain_key]
        boundary_names = {
            child.spec.map_input: self._record.grains[child.key].boundary_physical for child in self._layout.grains if child.key != "root"
        }
        columns: list[ColumnSpec] = []
        seen: set[str] = set()
        for column in grain.spec.columns:
            name = column.name
            if column.role in ("identity", "source", "derived", "answer"):
                name = self._physical_name(grain_key, column.name)
            elif column.name.startswith(PROVENANCE_PREFIX):
                logical = column.name[len(PROVENANCE_PREFIX) :]
                if logical in plan.columns:
                    name = f"{PROVENANCE_PREFIX}{self._physical_name(grain_key, logical)}"
                elif logical in boundary_names:
                    name = f"{PROVENANCE_PREFIX}{boundary_names[logical]}"
            if name in seen:
                continue
            seen.add(name)
            columns.append(
                ColumnSpec(
                    name=name,
                    role=column.role,
                    produced_by=column.produced_by,
                    content_key=column.content_key,
                    arrow_type=column.arrow_type,
                )
            )
        return TableSpec(
            name=plan.physical_table,
            identity=grain.spec.identity,
            columns=columns,
            parent_link=grain.spec.parent_link,
            child_graph=grain.spec.child_graph,
            map_input=grain.spec.map_input,
        )

    def _ensure_storage(self) -> None:
        physical = {grain.key: self._physical_spec(grain, grain.key) for grain in self._layout.grains}
        children = [spec for key, spec in physical.items() if key != "root"]
        self._root._store.open(self._root._spec, children)
        for spec in physical.values():
            current = set(self._root._store.column_names(spec.name))
            additions = {column.name: column.arrow_type for column in spec.columns if column.name not in current}
            if additions:
                self._root._store.evolve_schema(spec.name, additions)

    @staticmethod
    def _source_item(grain: _LogicalGrain, grain_key: str, plan: BranchRecord, row: Mapping[str, Any]) -> dict[str, Any]:
        item: dict[str, Any] = {}
        for column in grain.spec.columns:
            if column.role not in ("identity", "source"):
                continue
            physical = plan.grains[grain_key].columns[column.name].physical
            if physical in row:
                item[column.name] = normalize_value(row[physical])
        return item

    def _write_root(self, existing: Mapping[str, Any], changes: Mapping[str, Any], write_gen: int) -> None:
        row = {key: normalize_value(value) for key, value in existing.items()}
        row.update(changes)
        row["_write_gen"] = write_gen
        self._root._store.write_rows(self._root.table_name, [row])
        self._root._store.delete_rows(
            self._root.table_name,
            [
                (self._root._identity, "eq", existing[self._root._identity]),
                ("_write_gen", "lt", write_gen),
            ],
        )

    def _converge_child(
        self,
        grain: _LogicalGrain,
        item: dict[str, Any],
        parent_id: Any,
        write_gen: int,
    ) -> Generator[RunGraph, Any, bool]:
        plan = self._record.grains[grain.key]
        table_name = plan.physical_table
        identity = grain.spec.identity
        identity_value = item.get(identity, "")
        rows = self._root._store.read_rows(
            table_name,
            [(PARENT_LINK_COLUMN, "eq", parent_id), (identity, "eq", identity_value)],
        )
        existing = max(rows, key=lambda row: row.get("_write_gen", 0)) if rows else None
        logical_values = dict(item)
        if existing is not None:
            logical_values.update(self._logical_values(grain, grain.key, existing))
        changes = yield from self._converge_columns(grain, grain.key, existing or {}, logical_values)
        if not changes and existing is not None:
            return False
        if existing is None:
            row: dict[str, Any] = {
                identity: identity_value,
                PARENT_LINK_COLUMN: parent_id,
                "_row_fingerprint": self._layout.provenance.child_fingerprint(
                    {column.name: item[column.name] for column in grain.spec.columns if column.role == "source" and column.name in item},
                    grain.spec,
                ),
                RECIPE_COLUMN: self._layout.provenance.current_child_recipe_fingerprint(grain.spec),
                "_status": "complete",
                "_error": None,
                QUESTION_COLUMN: None,
            }
            for column in grain.spec.columns:
                if column.role == "source" and column.name in item:
                    row[self._physical_name(grain.key, column.name)] = item[column.name]
        else:
            row = {key: normalize_value(value) for key, value in existing.items()}
        row.update(changes)
        row["_write_gen"] = write_gen
        self._root._store.write_rows(table_name, [row])
        self._root._store.delete_rows(
            table_name,
            [
                (PARENT_LINK_COLUMN, "eq", parent_id),
                (identity, "eq", identity_value),
                ("_write_gen", "lt", write_gen),
            ],
        )
        return True

    def _sync_plan(self) -> WriteOperation:
        self._ensure_storage()
        root_rows = dedup_rows(self._root._store.read_rows(self._root.table_name), self._root._identity)
        root_grain = self._layout.grain("root")
        receipts: list[RowReceipt] = []
        deleted = 0
        root_gen = self._root._store.max_write_gen(self._root.table_name) + 1
        child_gens: dict[str, int] = {}
        for root_row in root_rows:
            parent_id = root_row[self._root._identity]
            values = self._logical_values(root_grain, "root", root_row)
            changes = yield from self._converge_columns(root_grain, "root", root_row, values)
            wrote = bool(changes)
            for grain in self._layout.grains:
                if grain.key == "root":
                    continue
                plan = self._record.grains[grain.key]
                table_name = plan.physical_table
                child_rows = dedup_child_rows(
                    self._root._store.read_rows(table_name, [(PARENT_LINK_COLUMN, "eq", parent_id)]),
                    grain.spec.identity,
                )
                provenance = self._layout.provenance.node_provenance(grain.boundary_node, values)
                if provenance is None:
                    raise RuntimeError(f"Materialization Branch boundary {grain.spec.map_input!r} is missing stored inputs")
                stored = root_row.get(f"{PROVENANCE_PREFIX}{plan.boundary_physical}")
                stored_provenance, stored_count = split_boundary_provenance(stored)
                if stored_provenance == provenance and stored_count == len(child_rows):
                    items = [self._source_item(grain, grain.key, self._record, row) for row in child_rows]
                else:
                    boundary_input = grain.spec.map_input
                    assert boundary_input is not None
                    result = yield RunGraph(
                        self._layout.provenance.column_graph(grain.boundary_node),
                        self._layout.provenance.node_inputs(grain.boundary_node, values),
                    )
                    raw_items = _run_values(result).get(boundary_input)
                    items = [normalize_to_dict(item) for item in raw_items] if isinstance(raw_items, list) else []
                    changes[f"{PROVENANCE_PREFIX}{plan.boundary_physical}"] = self._layout.provenance.boundary_provenance_value(
                        provenance,
                        items,
                    )
                    self._record_recipe(grain.boundary_node)
                    wrote = True
                child_gen = child_gens.setdefault(table_name, self._root._store.max_write_gen(table_name) + 1)
                incoming_ids: set[str] = set()
                for item in items:
                    incoming_ids.add(str(item.get(grain.spec.identity, "")))
                    wrote = (yield from self._converge_child(grain, item, parent_id, child_gen)) or wrote
                for row in child_rows:
                    if str(row.get(grain.spec.identity, "")) not in incoming_ids:
                        deleted += self._root._store.delete_rows(
                            table_name,
                            [
                                (PARENT_LINK_COLUMN, "eq", parent_id),
                                (grain.spec.identity, "eq", row[grain.spec.identity]),
                            ],
                        )
                        wrote = True
            if changes:
                self._write_root(root_row, changes, root_gen)
            receipts.append(
                RowReceipt(
                    str(parent_id),
                    WriteOutcome.UPDATED if wrote else WriteOutcome.SKIPPED,
                    RowStatus.COMPLETE,
                )
            )
        return TableReceipt(tuple(receipts), deleted=deleted)

    def sync(self) -> TableReceipt | Awaitable[TableReceipt]:
        """Derive missing or stale branch artifacts from the root's current rows."""

        operation = self._sync_plan()
        if self._root._is_async_runner():
            return self._root._drive_async(operation)
        return self._root._drive_sync(operation)

    def _stale_columns(self, grain: _LogicalGrain, grain_key: str, row: Mapping[str, Any]) -> tuple[str, ...]:
        values = self._logical_values(grain, grain_key, row)
        stale: list[str] = []
        for node in self._layout.provenance.nodes_in_dependency_order(grain.spec):
            provenance = self._layout.provenance.node_provenance(node, values)
            for column in self._layout.provenance.node_columns(node, grain.spec):
                physical = self._physical_name(grain_key, column.name)
                if provenance is None or row.get(physical) is None or row.get(f"{PROVENANCE_PREFIX}{physical}") != provenance:
                    stale.append(column.name)
                elif physical in row:
                    values[column.name] = normalize_value(row[physical])
        return tuple(stale)

    def status(self) -> TableStatus:
        """Project branch readiness and freshness without executing the graph."""

        root_rows = dedup_rows(self._root._store.read_rows(self._root.table_name), self._root._identity)
        root_grain = self._layout.grain("root")
        root_stale_ids: list[str] = []
        root_stale_columns: dict[str, int] = {}
        child_statuses: list[TableStatus] = []
        for grain in self._layout.grains:
            if grain.key == "root":
                continue
            plan = self._record.grains[grain.key]
            rows = dedup_child_rows(self._root._store.read_rows(plan.physical_table), grain.spec.identity)
            stale_ids: list[str] = []
            stale_columns: dict[str, int] = {}
            errored_ids: list[str] = []
            for row in rows:
                identity_value = str(row.get(grain.spec.identity, ""))
                if row.get("_status") == "error":
                    errored_ids.append(identity_value)
                    continue
                child_stale_names = self._stale_columns(grain, grain.key, row)
                if child_stale_names:
                    stale_ids.append(identity_value)
                    for column in child_stale_names:
                        stale_columns[column] = stale_columns.get(column, 0) + 1
            child_statuses.append(
                TableStatus(
                    table=plan.physical_table,
                    total=len(rows),
                    fresh=len(rows) - len(stale_ids) - len(errored_ids),
                    stale=len(stale_ids),
                    errored=len(errored_ids),
                    stale_ids=tuple(sorted(stale_ids)),
                    errored_ids=tuple(sorted(errored_ids)),
                    stale_columns=tuple(sorted(stale_columns.items())),
                )
            )
        for row in root_rows:
            identity_value = str(row.get(self._root._identity, ""))
            values = self._logical_values(root_grain, "root", row)
            root_stale_names = list(self._stale_columns(root_grain, "root", row))
            for grain in self._layout.grains:
                if grain.key == "root":
                    continue
                plan = self._record.grains[grain.key]
                provenance = self._layout.provenance.node_provenance(grain.boundary_node, values)
                stored_provenance, stored_count = split_boundary_provenance(row.get(f"{PROVENANCE_PREFIX}{plan.boundary_physical}"))
                child_count = len(
                    dedup_child_rows(
                        self._root._store.read_rows(plan.physical_table, [(PARENT_LINK_COLUMN, "eq", row[self._root._identity])]),
                        grain.spec.identity,
                    )
                )
                if provenance is None or stored_provenance != provenance or stored_count != child_count:
                    root_stale_names.append(str(grain.spec.map_input))
            if root_stale_names:
                root_stale_ids.append(identity_value)
                for column in root_stale_names:
                    root_stale_columns[column] = root_stale_columns.get(column, 0) + 1
        return TableStatus(
            table=self._root.table_name,
            total=len(root_rows),
            fresh=len(root_rows) - len(root_stale_ids),
            stale=len(root_stale_ids),
            errored=0,
            stale_ids=tuple(sorted(root_stale_ids)),
            stale_columns=tuple(sorted(root_stale_columns.items())),
            children=tuple(child_statuses),
        )

    def create_index(self, name: str, *, rows: Any = None) -> dict[str, Any]:
        """Persist a Query Index Spec over the branch's declared terminal outputs."""

        vector = self.output("vector")
        text = self.output("text") if "text" in self._record.outputs else None
        assert vector.column is not None
        if text is not None and text.table != vector.table:
            raise GraphConfigError(
                "Materialization Branch query outputs are on different grains.\n\n"
                f"Text: {text.table}.{text.column}\nVector: {vector.table}.{vector.column}\n\n"
                "How to fix: bind text and vector outputs from the same terminal grain."
            )
        output = self._record.outputs["vector"]
        root_spec = self._root._spec
        assert root_spec is not None
        indexes = IndexPolicy(self._root._store, root_spec, self._root._provenance_policy)
        return indexes.create(
            name,
            on=vector.table,
            rows=rows,
            text=text.column if text is not None else None,
            vector=vector.column,
            _branch=BranchIndexBinding(
                branch=self.name,
                on=vector.table,
                recipe_fingerprint=output.artifact.recipe,
                artifact_lineage=vector.lineage,
            ),
        )
