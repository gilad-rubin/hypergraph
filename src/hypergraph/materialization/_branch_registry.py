"""Typed persisted records for Materialization Branch reachability."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

BRANCHES_KEY = "materialization_branches"


@dataclass(frozen=True)
class ArtifactRecord:
    physical: str
    lineage: str
    recipe: str | None
    role: str

    @classmethod
    def from_manifest(cls, value: dict[str, Any]) -> ArtifactRecord:
        return cls(
            physical=str(value["physical"]),
            lineage=str(value["lineage"]),
            recipe=value.get("recipe"),
            role=str(value["role"]),
        )

    def to_manifest(self) -> dict[str, Any]:
        return {
            "physical": self.physical,
            "lineage": self.lineage,
            "recipe": self.recipe,
            "role": self.role,
        }


@dataclass(frozen=True)
class OutputRecord:
    grain: str
    logical: str
    artifact: ArtifactRecord

    @classmethod
    def from_manifest(cls, value: dict[str, Any]) -> OutputRecord:
        return cls(
            grain=str(value["grain"]),
            logical=str(value["logical"]),
            artifact=ArtifactRecord.from_manifest(value),
        )

    def to_manifest(self) -> dict[str, Any]:
        return {
            "grain": self.grain,
            "logical": self.logical,
            **self.artifact.to_manifest(),
        }


@dataclass(frozen=True)
class GrainRecord:
    logical_table: str
    physical_table: str
    lineage: str
    identity: str
    map_input: str | None
    boundary_physical: str | None
    columns: dict[str, ArtifactRecord]

    @classmethod
    def from_manifest(cls, value: dict[str, Any]) -> GrainRecord:
        return cls(
            logical_table=str(value["logical_table"]),
            physical_table=str(value["physical_table"]),
            lineage=str(value["lineage"]),
            identity=str(value["identity"]),
            map_input=value.get("map_input"),
            boundary_physical=value.get("boundary_physical"),
            columns={name: ArtifactRecord.from_manifest(column) for name, column in value.get("columns", {}).items()},
        )

    def to_manifest(self) -> dict[str, Any]:
        return {
            "logical_table": self.logical_table,
            "physical_table": self.physical_table,
            "lineage": self.lineage,
            "identity": self.identity,
            "map_input": self.map_input,
            "boundary_physical": self.boundary_physical,
            "columns": {name: column.to_manifest() for name, column in self.columns.items()},
        }


@dataclass(frozen=True)
class BranchRecord:
    version: int
    name: str
    root_table: str
    signature: str
    outputs: dict[str, OutputRecord]
    grains: dict[str, GrainRecord]

    @classmethod
    def from_manifest(cls, value: dict[str, Any]) -> BranchRecord:
        return cls(
            version=int(value["version"]),
            name=str(value["name"]),
            root_table=str(value["root_table"]),
            signature=str(value["signature"]),
            outputs={name: OutputRecord.from_manifest(output) for name, output in value.get("outputs", {}).items()},
            grains={name: GrainRecord.from_manifest(grain) for name, grain in value.get("grains", {}).items()},
        )

    def to_manifest(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "root_table": self.root_table,
            "signature": self.signature,
            "outputs": {name: output.to_manifest() for name, output in self.outputs.items()},
            "grains": {name: grain.to_manifest() for name, grain in self.grains.items()},
        }


def load_branch_records(store: Any, root_table: str) -> dict[str, BranchRecord]:
    manifest = store.load_manifest(root_table) or {}
    return {name: BranchRecord.from_manifest(record) for name, record in manifest.get(BRANCHES_KEY, {}).items()}


def save_branch_record(store: Any, root_table: str, record: BranchRecord) -> None:
    manifest = store.load_manifest(root_table) or {}
    branches = dict(manifest.get(BRANCHES_KEY, {}))
    branches[record.name] = record.to_manifest()
    manifest[BRANCHES_KEY] = branches
    store.save_manifest(root_table, manifest)


def registered_child_tables(store: Any, root_table: str) -> tuple[str, ...]:
    """Every persisted child grain reachable from a registered branch."""

    tables = {
        grain.physical_table for record in load_branch_records(store, root_table).values() for key, grain in record.grains.items() if key != "root"
    }
    return tuple(sorted(tables))
