"""The recipe journal: a per-store table resolving a provenance stamp to readable text.

Fingerprints hash the recipe — node definitions (``hash_definition``: source
or bytecode, plus bound-instance state), component config reprs, bound
plain-value payloads — and then discard the text (see ``_fingerprint.py``).
A stamp on a stored row can therefore detect that a row was "built under
something else" but can never NAME what that was: an uncommitted node edit
that derived rows is unrecoverable from the hash alone.

The journal closes that gap. At the moment a stamp is written onto a row the
payload behind it is still in hand, so it is persisted — keyed by its own hash —
into a table in the SAME store the HyperTable writes. Append-once: a hash
already journaled is a no-op, guarded by an in-memory seen-set so the hot write
path never reads the store to check. Reads happen only in the resolve/explain
API. The journal stores MEANING (code + config + bound-value text), never raw
row input VALUES — those can be megabytes and already live on the row as source
columns.
"""

from __future__ import annotations

import datetime
from typing import Any

from hypergraph.materialization._schema import ColumnSpec, TableSpec

# A plain (no leading underscore) physical table name. A leading-underscore
# table name once collided with the internal-COLUMN conventions downstream
# (``is_internal_column`` / ``is_reserved_name`` treat leading-underscore names
# as framework-managed), so the journal takes an ordinary name — it sits beside
# the root/child tables and the ``<table>__manifest.json`` sidecar, none of which
# lead with an underscore either.
JOURNAL_TABLE = "recipe_journal"

# Payload kinds. One row per (hash, kind, payload); the kind labels what the
# text means so a reader (or a UI) can group node source apart from configs. The
# journal is keyed by each payload's OWN hash (a node's definition hash, a
# config/value payload hash) — never a row's value-chained provenance stamp — so
# it holds one row per recipe, not one per derived row.
KIND_NODE_SOURCE = "node_source"
KIND_COMPONENT_CONFIG = "component_config"
KIND_BOUND_VALUE = "bound_value"


def _journal_spec() -> TableSpec:
    """The journal's physical shape: ``hash`` identity + kind/payload/first_seen_at."""
    return TableSpec(
        name=JOURNAL_TABLE,
        identity="hash",
        columns=[
            ColumnSpec("hash", role="identity"),
            ColumnSpec("kind", role="source"),
            ColumnSpec("payload", role="source"),
            ColumnSpec("first_seen_at", role="source"),
            # HyperTable's write path expects _write_gen on every row it dedups;
            # the journal carries it as a constant so a store that dedups by it
            # (LanceDB read_one) stays happy. The journal never re-writes a hash,
            # so the value is immaterial — it is always 0.
            ColumnSpec("_write_gen", role="internal", arrow_type=None),
        ],
    )


class RecipeJournal:
    """Append-once hash -> recipe-text store, backed by one table in the HyperTable's store.

    One instance per HyperTable (created lazily). The ``_seen`` set makes a
    repeated ``record`` of the same hash free — the 100-row insert in F2 touches
    the store exactly once per distinct payload, never once per row.
    """

    def __init__(self, store: Any):
        self._store = store
        self._seen: set[str] = set()
        self._opened = False

    def _ensure_open(self) -> None:
        if not self._opened:
            self._store.open(_journal_spec(), [])
            self._opened = True

    def record(self, hash_: str, kind: str, payload: str) -> None:
        """Persist ``hash -> payload`` on first sight; a no-op if already seen.

        Cheap on the hot path: the seen-set short-circuits before any store I/O,
        so re-recording the same recipe for row 2..100 costs a set lookup.
        """
        if hash_ in self._seen:
            return
        self._seen.add(hash_)
        self._ensure_open()
        # Another table instance in this process (or a prior run) may already hold
        # the hash — the seen-set only guards THIS instance. Skip the write when
        # the store already has it so the append stays truly once-per-store.
        if self._store.read_one(JOURNAL_TABLE, "hash", hash_) is not None:
            return
        self._store.write_rows(
            JOURNAL_TABLE,
            [
                {
                    "hash": hash_,
                    "kind": kind,
                    "payload": payload,
                    "first_seen_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "_write_gen": 0,
                }
            ],
        )

    def resolve(self, hash_: str) -> str | None:
        """The payload text recorded under ``hash``, or None if never journaled."""
        self._ensure_open()
        row = self._store.read_one(JOURNAL_TABLE, "hash", hash_)
        return row["payload"] if row is not None else None

    def rows(self) -> list[dict[str, Any]]:
        """Every journaled ``(hash, kind, payload, first_seen_at)`` row."""
        self._ensure_open()
        return [
            {"hash": r["hash"], "kind": r.get("kind"), "payload": r.get("payload"), "first_seen_at": r.get("first_seen_at")}
            for r in self._store.read_rows(JOURNAL_TABLE)
        ]
