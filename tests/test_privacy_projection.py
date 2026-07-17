"""Durable privacy-boundary tests (#233, red-green item 3 — the graduation).

Local object surfaces (the raised exception, ``RunResult.error``,
``FailureEvidence.error``) keep the exact exception object. Durable and
telemetry surfaces (events, RunLog, StepRecord, ``RunResult.to_dict()``,
attempt-ledger rows, OTel export) receive only safe projections: codes,
exception type names, node identity, counts/timing, booleans, static help —
never raw exception message text.

RED on master: ``str(exception)`` flows NodeErrorEvent.error -> RunLog /
StepRecord -> RunResult.to_dict() -> OTel exception.message, so a secret in
an exception message leaks to every durable surface.
"""

from __future__ import annotations

import inspect
import json

import pytest
import pytest_asyncio

from hypergraph import (
    AsyncRunner,
    Graph,
    RetryPolicy,
    SyncRunner,
    node,
)
from hypergraph.checkpointers import SqliteCheckpointer
from hypergraph.events import NodeErrorEvent, RunEndEvent
from hypergraph.events.processor import EventProcessor

aiosqlite = pytest.importorskip("aiosqlite")

SECRET = "sk-live-SECRET-4242"


class Recorder(EventProcessor):
    def __init__(self) -> None:
        self.events: list = []

    def on_event(self, event) -> None:
        self.events.append(event)

    def of(self, event_type):
        return [e for e in self.events if type(e) is event_type]


def _make_runner(family: str, **kwargs):
    return SyncRunner(**kwargs) if family == "sync" else AsyncRunner(**kwargs)


async def _run(runner, *args, **kwargs):
    result = runner.run(*args, **kwargs)
    if inspect.iscoroutine(result):
        result = await result
    return result


@pytest.fixture(params=["sync", "async"])
def family(request) -> str:
    return request.param


@pytest_asyncio.fixture
async def make_sqlite(tmp_path):
    created = []

    def factory(name: str = "privacy.db") -> SqliteCheckpointer:
        cp = SqliteCheckpointer(str(tmp_path / name))
        created.append(cp)
        return cp

    yield factory
    for cp in created:
        await cp.close()


@node(
    output_name="value",
    retry=RetryPolicy(
        max_attempts=2,
        retry_on=(ValueError,),
        initial_delay=0.001,
        jitter="none",
    ),
)
def leaky(token: str) -> str:
    raise ValueError(f"auth failed for {token}")


async def _run_leaky(family, checkpointer, *, event_processors=None):
    runner = _make_runner(family, checkpointer=checkpointer)
    return await _run(
        runner,
        Graph([leaky]),
        {"token": SECRET},
        workflow_id="wf-privacy",
        error_handling="continue",
        event_processors=event_processors or [],
    )


async def test_local_surfaces_keep_the_exact_exception_object(family, make_sqlite):
    result = await _run_leaky(family, make_sqlite())

    assert result.failed
    assert isinstance(result.error, ValueError)
    assert SECRET in str(result.error), "the local exact exception keeps its full message"
    assert result.failure is not None
    assert result.failure.error is result.error, "FailureEvidence.error is the exact object"


async def test_run_result_to_dict_contains_no_raw_message_text(family, make_sqlite):
    result = await _run_leaky(family, make_sqlite())

    serialized = json.dumps(result.to_dict())
    assert SECRET not in serialized, "RunResult.to_dict() is a durable surface: safe projection only"
    assert "ValueError" in serialized, "the projection keeps the exception type name"


async def test_step_records_contain_no_raw_message_text(family, make_sqlite):
    cp = make_sqlite()
    await _run_leaky(family, cp)

    steps = await cp.get_steps("wf-privacy")
    failed = [s for s in steps if s.error is not None]
    assert failed, "the failed logical step must be persisted"
    for step in failed:
        assert SECRET not in step.error
        assert SECRET not in json.dumps(step.to_dict())
        assert "ValueError" in step.error, "the projection keeps the exception type name"


async def test_attempt_ledger_rows_contain_no_raw_message_text(family, make_sqlite):
    cp = make_sqlite()
    await _run_leaky(family, cp)

    steps = await cp.get_steps("wf-privacy")
    series_ids = {s.attempt_series_id for s in steps if s.attempt_series_id}
    assert series_ids, "the failed step must link its attempt series"

    checked = 0
    for series_id in series_ids:
        for record in await cp.get_attempt_records(series_id):
            if record.error is None:
                continue
            checked += 1
            assert SECRET not in record.error.message, "attempt-ledger rows are durable: no raw message text"
            assert record.error.type_name == "ValueError"
    assert checked >= 1


async def test_events_and_run_log_contain_no_raw_message_text(family, make_sqlite):
    recorder = Recorder()
    result = await _run_leaky(family, make_sqlite(), event_processors=[recorder])

    node_errors = recorder.of(NodeErrorEvent)
    assert len(node_errors) == 1
    assert SECRET not in node_errors[0].error
    assert "ValueError" in node_errors[0].error_type

    run_ends = recorder.of(RunEndEvent)
    assert run_ends
    for event in run_ends:
        assert event.error is None or SECRET not in event.error

    assert result.log is not None
    assert SECRET not in json.dumps(result.log.to_dict())
    failed_steps = result.log.errors
    assert failed_steps and all(SECRET not in (s.error or "") for s in failed_steps)


@pytest.mark.usefixtures("family")
class TestOTelExportPrivacy:
    @pytest.fixture(autouse=True)
    def otel_setup(self):
        pytest.importorskip("opentelemetry.sdk")
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor

        try:
            from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        except ImportError:
            from opentelemetry.sdk.trace.export.in_memory import InMemorySpanExporter

        self.exporter = InMemorySpanExporter()
        provider = trace.get_tracer_provider()
        if not isinstance(provider, TracerProvider):
            provider = TracerProvider()
            trace._set_tracer_provider(provider, log=False)
        self.span_processor = SimpleSpanProcessor(self.exporter)
        provider.add_span_processor(self.span_processor)
        yield
        self.exporter.clear()
        provider._active_span_processor._span_processors = tuple(
            processor for processor in provider._active_span_processor._span_processors if processor is not self.span_processor
        )
        self.span_processor.shutdown()

    async def test_otel_export_contains_no_raw_message_text(self, family, make_sqlite):
        from hypergraph.events.otel import OpenTelemetryProcessor

        await _run_leaky(family, make_sqlite(), event_processors=[OpenTelemetryProcessor()])

        spans = self.exporter.get_finished_spans()
        assert spans, "the failed run must still export spans"
        for span in spans:
            status_desc = span.status.description or ""
            assert SECRET not in status_desc, f"span status leaked the secret: {span.name}"
            for event in span.events:
                for key, value in (event.attributes or {}).items():
                    assert SECRET not in str(value), f"span event attr {key} leaked the secret: {span.name}"
            for key, value in (span.attributes or {}).items():
                assert SECRET not in str(value), f"span attr {key} leaked the secret: {span.name}"
