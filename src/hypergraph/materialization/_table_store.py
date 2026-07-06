"""Storage interface for HyperTable — decouples table logic from any database."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import pyarrow as pa

RowOperator = Literal["eq", "ne", "lt", "lte", "gt", "gte", "in"]
RowPredicate = Sequence[tuple[str, RowOperator, Any]]


class TableStore(ABC):
    """Abstract storage backend for HyperTable."""

    @abstractmethod
    def open(self, spec: Any, children: list[Any]) -> dict[str, list[str]]:
        """Ensure physical tables exist. Returns {table_name: [column_names]}."""

    @abstractmethod
    def count(self, table_name: str) -> int:
        """Return physical row count for a table."""

    @abstractmethod
    def read_rows(
        self,
        table_name: str,
        where: RowPredicate | None = None,
        *,
        limit: int | None = None,
        columns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Read rows, optionally filtered by a row predicate.

        ``columns`` projects the result to just those column names — a metadata
        read never has to drag a ``large_binary`` blob column off disk. ``None``
        (the default) returns every column, so existing callers are unaffected.

        A subclass gets projection FOR FREE: it may fetch full rows and hand
        them to ``TableStore._project_rows(rows, columns)``, which drops the
        unwanted keys. A store that can push projection down to its backend
        (e.g. LanceDB) overrides this for the real on-disk I/O saving. Either
        way the observable contract is identical, and the conformance harness
        checks it against both.
        """

    @abstractmethod
    def read_one(
        self,
        table_name: str,
        identity_column: str,
        identity_value: Any,
        *,
        columns: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Read one row by identity, returning the newest generation when duplicated.

        ``columns`` projects the result exactly as in ``read_rows``. The identity
        column is always retrievable regardless of the projection list (the
        dedup-by-generation logic and the caller both rely on it).
        """

    @abstractmethod
    def write_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        """Append or upsert rows into the physical table."""

    @abstractmethod
    def delete_rows(self, table_name: str, where: RowPredicate) -> int:
        """Delete rows matching a predicate and return the physical count deleted."""

    @abstractmethod
    def max_write_gen(self, table_name: str) -> int:
        """Return the highest write generation currently persisted."""

    @abstractmethod
    def evolve_schema(self, table_name: str, new_columns: dict[str, pa.DataType]) -> list[str]:
        """Add columns and return the table's column names.

        ``new_columns`` maps column name to a pyarrow ``DataType``. Arrow is the
        intermediate type system: stores map Arrow to their native format (or
        ignore types when schemaless). No store performs Python-to-Arrow
        conversion — the HyperTable layer does it once before calling here.

        Evolving a column the physical schema ALREADY holds must be a no-op for
        that column, not an error. HyperTable decides a metadata column is "new"
        without always knowing the physical schema (e.g. it re-derives that set
        from an empty table after every row is deleted), so it can ask to add a
        column that already exists. A conforming store skips such columns and
        returns the current column names; it never appends a duplicate field.
        The conformance harness asserts this idempotence.
        """

    def column_names(self, table_name: str) -> list[str]:
        """The physical column names of a table, ``[]`` when it does not exist yet.

        This is the schema-consultation seam: HyperTable's metadata evolution asks
        the store what columns physically exist rather than inferring the set by
        sampling a row — a sample is empty exactly when the table has been emptied,
        which is when inference goes wrong. A store that cannot introspect its
        schema may leave this default (``[]``); it is then protected by
        ``evolve_schema`` idempotence instead. Stores that track a schema override
        this to return it.
        """
        return []

    def search(
        self,
        table_name: str,
        *,
        query: str | None = None,
        query_vector: list[float] | None = None,
        vector_column: str | None = None,
        where: RowPredicate | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Search is optional because not every TableStore is a retrieval adapter.

        Implementations run a vector search on ``vector_column`` with ``where``
        applied as a pre-filter, and include a ``_distance`` field per hit.
        """
        raise NotImplementedError("This store does not support search")

    def save_manifest(self, table_name: str, manifest: dict[str, Any]) -> None:
        """Persist table metadata when the backend supports manifests.

        Manifests are OPTIONAL: a store that never uses named indexes never
        touches them and can leave this base no-op in place. The loud failure
        for a store that *does* get asked to persist an index lives at the call
        site (``HyperTable.create_index`` checks ``supports_manifests`` and
        raises, naming the store and this method) so it fires exactly when the
        capability is used — not silently at ``list_indexes``-returns-nothing.
        """
        return None

    def load_manifest(self, table_name: str) -> dict[str, Any] | None:
        """Load table metadata when the backend supports manifests."""
        return None

    def supports_manifests(self) -> bool:
        """True when the store overrides the manifest hooks (index persistence).

        A store must implement both ``save_manifest`` and ``load_manifest`` to
        support named indexes; the base no-ops do not count.
        """
        return type(self).save_manifest is not TableStore.save_manifest and type(self).load_manifest is not TableStore.load_manifest

    def supports_column_projection(self) -> bool:
        """Whether ``read_rows``/``read_one`` accept the ``columns=`` kwarg.

        A store advertises support only when BOTH read methods carry a
        ``columns`` parameter. This lets a caller (HyperTable's metadata-only
        reads) push a projection down to conforming stores while staying
        compatible with older external stores whose ``read_rows`` predates the
        kwarg — they are simply never handed ``columns``. Checked by signature,
        so a store implements projection just by accepting the parameter; no
        registration step.
        """
        import inspect

        for name in ("read_rows", "read_one"):
            try:
                params = inspect.signature(getattr(self, name)).parameters
            except (TypeError, ValueError):
                return False
            if "columns" not in params:
                return False
        return True

    @staticmethod
    def _project_rows(rows: list[dict[str, Any]], columns: list[str] | None) -> list[dict[str, Any]]:
        """Post-filter full rows to ``columns`` (the base-class projection default).

        A store that cannot push projection into its backend fetches full rows
        and calls this — the observable result matches a native projection. When
        ``columns`` is ``None`` the rows pass through untouched. Keys requested
        but absent from a row are silently omitted (mirrors a native projection
        of a null-valued column), so callers must not treat a missing key as an
        error here; the loud "unknown column" check belongs at the backend that
        actually knows its schema.
        """
        if columns is None:
            return rows
        wanted = set(columns)
        return [{k: v for k, v in row.items() if k in wanted} for row in rows]


def validate_store(store: Any) -> TableStore:
    """Validate that an external store satisfies the concrete HyperTable seam."""

    if not isinstance(store, TableStore):
        raise TypeError(f"store must subclass TableStore, got {type(store).__name__}")

    from hypergraph.materialization._schema import ColumnSpec, TableSpec

    spec = TableSpec(
        name="__validate_store",
        identity="id",
        columns=[ColumnSpec("id", role="identity"), ColumnSpec("_write_gen", role="internal")],
    )
    store.open(spec, [])
    return store
