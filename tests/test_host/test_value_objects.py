"""The durable host's value objects and pure policy, at their edges.

Everything here is reachable from a detached operator process holding
nothing but JSON: it stored a ``RunRef`` in a product table months ago,
reads it back, and hands it to a client. The happy path of that round trip
is proven all over the Batch suites; what is proven HERE is what happens
when the stored value is wrong — the refusals, and the branch each
conflict message picks — because those are exactly the paths a live
workflow never walks and an incident does.

The pure refusal helpers in ``_batch_store`` are tested directly rather
than through a store: they take rows and raise, so a transaction adds
nothing but time to the falsification.
"""

from __future__ import annotations

import json

import pytest

from hypergraph import (
    BatchRef,
    BatchTolerance,
    DefinitionId,
    RunRef,
    WorkflowIdConflictError,
)
from hypergraph.host._batch_store import (
    BatchAcceptance,
    ChildSpec,
    DefinitionPin,
    closeout_kind,
    refuse_child_id_collision,
    refuse_run_owned_id,
    refuse_tier0_reuse,
)
from hypergraph.host.errors import AlreadyTerminalError, WorkerLockError
from hypergraph.host.fingerprint import batch_mismatch_aspect, canonical_json, fingerprint_mismatch_aspect

pytestmark = pytest.mark.host_batch_interrupt


# === 1. An inert ref survives its own JSON, or says why it cannot ===


class TestRefsSurviveTheirOwnJSON:
    """``to_dict``/``from_dict`` is the whole point of an inert address."""

    @pytest.mark.parametrize(
        ("value", "kind"),
        [
            (RunRef(home="file:./runs.db", run_id="wf-1"), RunRef),
            (BatchRef(home="file:./runs.db", batch_id="b-1"), BatchRef),
            (DefinitionId("ingest", "2026.07.3", "9f3a"), DefinitionId),
        ],
    )
    def test_a_stored_ref_rebuilds_equal(self, value, kind):
        assert kind.from_dict(json.loads(json.dumps(value.to_dict()))) == value

    @pytest.mark.parametrize("kind", [RunRef, BatchRef, DefinitionId])
    def test_a_non_dict_is_a_typeerror_naming_what_arrived(self, kind):
        with pytest.raises(TypeError, match="expects a dict, got list"):
            kind.from_dict(["file:./runs.db", "wf-1"])

    @pytest.mark.parametrize(
        ("kind", "data", "missing"),
        [
            (RunRef, {"home": "file:x"}, "run_id"),
            (BatchRef, {"batch_id": "b-1"}, "home"),
            (DefinitionId, {"name": "ingest", "deployment_version": "v1"}, "structural_hash"),
        ],
    )
    def test_a_missing_key_is_a_valueerror_naming_the_key(self, kind, data, missing):
        with pytest.raises(ValueError, match=missing):
            kind.from_dict(data)

    @pytest.mark.parametrize(
        ("kind", "data"),
        [
            (RunRef, {"home": "file:x", "run_id": 7}),
            (BatchRef, {"home": "file:x", "batch_id": None}),
            (DefinitionId, {"name": "ingest", "deployment_version": "v1", "structural_hash": 9}),
        ],
    )
    def test_a_non_string_field_is_a_typeerror(self, kind, data):
        with pytest.raises(TypeError, match="must be strings"):
            kind.from_dict(data)


# === 2. A conflict message names the aspect that actually differs ===


class TestConflictMessagesNameTheRightAspect:
    """A caller reusing an id needs to know WHICH field it changed.

    The two selectors run only after the fingerprints already differ, so
    each has to reach its own branch on real inputs — a message saying
    "start_at" for changed inputs would send someone hunting the wrong bug.
    """

    RUN_ROW = {
        "definition_name": "ingest",
        "def_version": "v1",
        "def_struct_hash": "9f3a",
        "inputs_json": json.dumps({"work_item_id": "w1"}),
        "start_at": None,
    }
    SAME_PIN = {"definition_name": "ingest", "def_version": "v1", "def_struct_hash": "9f3a"}

    def test_a_changed_definition_identity_is_named_first(self):
        aspect = fingerprint_mismatch_aspect(
            self.RUN_ROW,
            definition_name="ingest",
            def_version="v2",
            def_struct_hash="9f3a",
            inputs_json=self.RUN_ROW["inputs_json"],
            start_at=None,
        )
        assert aspect == "definition identity"

    def test_changed_inputs_are_named_over_start_at(self):
        aspect = fingerprint_mismatch_aspect(
            self.RUN_ROW,
            **self.SAME_PIN,
            inputs_json=json.dumps({"work_item_id": "w2"}),
            start_at="2030-01-01T00:00:00+00:00",
        )
        assert aspect == "inputs"

    def test_only_a_changed_start_at_falls_through_to_start_at(self):
        aspect = fingerprint_mismatch_aspect(
            self.RUN_ROW,
            **self.SAME_PIN,
            # Same mapping, different key order: canonical JSON must agree.
            inputs_json=json.dumps({"work_item_id": "w1"}),
            start_at="2030-01-01T00:00:00+00:00",
        )
        assert aspect == "start_at"

    BATCH_ROW = {
        **SAME_PIN,
        "items_json": json.dumps({"a": {"x": 1}}),
        "tolerance_json": None,
        "start_at": None,
        "admission_cost": None,
    }

    @pytest.mark.parametrize(
        ("items", "tolerance_json", "start_at", "expected"),
        [
            ({"a": {"x": 2}}, None, None, "items"),
            ({"a": {"x": 1}}, json.dumps({"max_failed": 2, "max_failed_percent": None}), None, "tolerance"),
            ({"a": {"x": 1}}, None, "2030-01-01T00:00:00+00:00", "start_at"),
        ],
    )
    def test_a_batch_conflict_names_items_tolerance_or_start_at(self, items, tolerance_json, start_at, expected):
        aspect = batch_mismatch_aspect(
            self.BATCH_ROW,
            **self.SAME_PIN,
            items_canonical=canonical_json(items),
            tolerance_json=tolerance_json,
            start_at=start_at,
        )
        assert aspect == expected

    def test_a_batch_with_a_changed_definition_names_identity(self):
        aspect = batch_mismatch_aspect(
            self.BATCH_ROW,
            definition_name="other",
            def_version="v1",
            def_struct_hash="9f3a",
            items_canonical=canonical_json({"a": {"x": 1}}),
            tolerance_json=None,
            start_at=None,
        )
        assert aspect == "definition identity"


# === 3. The pinned tolerance value rebuilds from partial stored JSON ===


class TestBatchToleranceValue:
    def test_both_keys_survive_the_round_trip(self):
        tolerance = BatchTolerance(max_failed=2, max_failed_percent=25)
        assert BatchTolerance.from_dict(tolerance.to_dict()) == tolerance

    def test_a_half_declared_tolerance_leaves_the_other_half_none(self):
        assert BatchTolerance.from_dict({"max_failed": 3}) == BatchTolerance(max_failed=3, max_failed_percent=None)

    def test_a_non_dict_is_refused_rather_than_read_positionally(self):
        with pytest.raises(TypeError, match="expects a dict, got list"):
            BatchTolerance.from_dict([2, 25])


# === 4. Workflow-id ownership refusals, on rows alone ===


class TestOwnershipRefusals:
    """Three owners of one namespace, and the typed error each earns."""

    def test_no_runs_row_means_the_id_is_free(self):
        assert refuse_tier0_reuse(None, workflow_id="wf-1") is None

    def test_a_terminal_tier0_run_is_already_terminal(self):
        with pytest.raises(AlreadyTerminalError, match="executed directly against this store"):
            refuse_tier0_reuse("completed", workflow_id="wf-1")

    def test_a_live_tier0_run_is_a_conflict(self):
        with pytest.raises(WorkflowIdConflictError, match="never submitted"):
            refuse_tier0_reuse("active", workflow_id="wf-1")

    def test_a_refused_child_id_names_its_item_and_the_id_syntax(self):
        with pytest.raises(AlreadyTerminalError) as excinfo:
            refuse_tier0_reuse("completed", workflow_id="drop-1:item-a", item_key="item-a")
        message = str(excinfo.value)
        assert "The child workflow id for item 'item-a'" in message
        assert "'<batch workflow_id>:<item key>'" in message

    def test_a_finished_run_submission_blocks_a_batch_of_the_same_id(self):
        with pytest.raises(AlreadyTerminalError):
            refuse_run_owned_id("finished", workflow_id="wf-1")

    def test_a_live_run_submission_is_a_conflict_not_a_dedup(self):
        with pytest.raises(WorkflowIdConflictError, match="an existing Run owns this workflow_id"):
            refuse_run_owned_id("pending", workflow_id="wf-1")

    def test_a_free_child_id_passes_and_a_taken_one_names_the_item(self):
        spec = ChildSpec("item-a", "drop-1:item-a", json.dumps({"x": 1}))
        assert refuse_child_id_collision(spec, collides=False) is None
        with pytest.raises(WorkflowIdConflictError, match="item 'item-a' collides"):
            refuse_child_id_collision(spec, collides=True)

    @pytest.mark.parametrize(("started", "kind"), [(False, "child_unstarted"), (True, "child_abandoned")])
    def test_closed_admission_names_a_child_by_whether_it_ran(self, started, kind):
        assert closeout_kind(started) == kind


# === 5. One acceptance value projects every row and fact it writes ===


class TestBatchAcceptanceProjections:
    """The request value IS the manifest, so its projections must agree."""

    REQUEST = BatchAcceptance(
        batch_id="b-1",
        workflow_id="drop-1",
        definition=DefinitionPin("ingest", "2026.07.3", "9f3a"),
        items=(("item-a", json.dumps({"x": 1})), ("item-b", json.dumps({"x": 2}))),
        fingerprint="fp-1",
        tolerance_json=json.dumps({"max_failed": 1, "max_failed_percent": None}),
        source_ref="operator-42",
        child_retry_of={"item-b": "old-drop:item-b"},
    )

    def test_the_manifest_fact_and_the_stored_row_agree_on_item_order(self):
        fact = self.REQUEST.manifest_fact("drop-1")
        row = self.REQUEST.batch_row("drop-1", now="2026-07-27T00:00:00+00:00")
        assert fact["item_keys"] == ["item-a", "item-b"]
        assert list(json.loads(row[5])) == fact["item_keys"]
        assert fact["definition_id"] == {"name": "ingest", "deployment_version": "2026.07.3", "structural_hash": "9f3a"}
        assert fact["tolerance"] == {"max_failed": 1, "max_failed_percent": None}

    def test_child_ids_are_derived_from_the_resolved_workflow_id(self):
        specs = self.REQUEST.child_specs("drop-1-retry-1")
        assert [spec.workflow_id for spec in specs] == ["drop-1-retry-1:item-a", "drop-1-retry-1:item-b"]
        assert [spec.item_key for spec in specs] == ["item-a", "item-b"]

    def test_only_a_repeated_item_carries_a_child_source(self):
        assert self.REQUEST.child_source("item-a") is None
        assert self.REQUEST.child_source("item-b") == "old-drop:item-b"

    def test_a_child_row_pins_its_own_start_fingerprint(self):
        specs = self.REQUEST.child_specs("drop-1")
        rows = [self.REQUEST.child_row(spec, retry_index=None, now="2026-07-27T00:00:00+00:00") for spec in specs]
        # Different inputs -> different pinned fingerprints, same Definition.
        assert rows[0][13] != rows[1][13]
        assert {row[1] for row in rows} == {"ingest"}
        assert {row[6] for row in rows} == {"pending"}
        assert [row[21] for row in rows] == ["item-a", "item-b"]

    def test_the_child_submitted_fact_names_its_membership(self):
        spec = self.REQUEST.child_specs("drop-1")[0]
        assert self.REQUEST.child_submitted_fact(spec) == {
            "definition_name": "ingest",
            "workflow_id": "drop-1:item-a",
            "batch_id": "b-1",
            "item_key": "item-a",
        }


# === 6. WorkerLockError is retired, exported, and unraisable ===


class TestTheRetiredWorkerLock:
    """The rule this error names is gone; the name is kept for one release.

    A Run Home used to admit exactly one ``work_forever`` worker, refused
    by an OS lock at startup. Leases replaced that: each claim is a
    compare-and-set that stamps a holder and an expiry, so two workers can
    never hold one submission and a dead worker's claims are adopted rather
    than waited on. What is pinned here is the deprecation shape — the
    symbol still imports and still constructs, so ``except WorkerLockError``
    written against the old rule keeps compiling, and its message says the
    rule is retired instead of instructing an operator to go stop a worker
    that is allowed to be running.
    """

    def test_the_symbol_is_still_exported_from_both_doors(self):
        import hypergraph
        import hypergraph.host

        assert hypergraph.WorkerLockError is WorkerLockError
        assert hypergraph.host.WorkerLockError is WorkerLockError

    def test_its_message_names_the_retirement_not_a_worker_to_stop(self):
        error = WorkerLockError("/tmp/runs.db.lock")
        assert "retired" in str(error)
        assert error.lock_path == "/tmp/runs.db.lock"

    def test_nothing_in_the_host_package_raises_it_any_more(self):
        """The grep that keeps the deprecation honest.

        A retired error that some path still raises is not retired; it is
        undocumented. ``errors.py`` defines it, so the definition site is
        the only mention the source is allowed to keep.
        """
        import pathlib

        import hypergraph.host

        package = pathlib.Path(hypergraph.host.__file__).parent
        raisers = [path.name for path in sorted(package.glob("*.py")) if "raise WorkerLockError" in path.read_text()]
        assert raisers == []

    def test_the_worker_module_kept_only_the_bounded_drain(self):
        """The lock's machinery is deleted, not merely unused.

        Leaving ``_WorkerLock`` behind would leave a second, contradictory
        answer to "who may execute this Home's work" one import away from
        any caller.
        """
        from hypergraph.host import worker

        assert not hasattr(worker, "_WorkerLock")
        assert not hasattr(worker, "lock_path_for")
        assert hasattr(worker, "_drain")
