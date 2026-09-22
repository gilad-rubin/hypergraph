"""FailureLogProcessor: a failed node's real message and traceback reach a stdlib logger.

Durable records keep only the safe projection (#233), so without a live
consumer of ``NodeErrorEvent.error_detail`` a failed durable run leaves no
trace of *why* it failed. ``FailureLogProcessor`` is the opt-in consumer that
writes that detail to ``logging`` — and persists nothing itself.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from pathlib import Path

import pytest

import hypergraph
from hypergraph import (
    AsyncRunner,
    Graph,
    HostRuntime,
    RetryPolicy,
    SyncRunner,
    node,
)
from hypergraph.checkpointers import SqliteCheckpointer
from hypergraph.checkpointers.types import WorkflowStatus
from hypergraph.events import EventProcessor, NodeErrorEvent, TypedEventProcessor

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET = "sk-live-SECRET-4242"
MISSING = "ModuleNotFoundError: No module named 'hg_n1_missing_module'"


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self) -> list[str]:
        return [record.getMessage() for record in self.records]


@pytest.fixture
def capture():
    """Attach a capturing handler to a named logger; restore it afterwards."""
    attached: list[tuple[logging.Logger, _Capture, int]] = []

    def attach(name: str = "hypergraph.failures", *, level: int | None = None) -> _Capture:
        target = logging.getLogger(name)
        handler = _Capture()
        attached.append((target, handler, target.level))
        target.addHandler(handler)
        if level is not None:
            target.setLevel(level)
        return handler

    yield attach
    for target, handler, previous in attached:
        target.removeHandler(handler)
        target.setLevel(previous)


class ErrorRecorder(EventProcessor):
    def __init__(self) -> None:
        self.errors: list[NodeErrorEvent] = []

    def on_event(self, event) -> None:
        if isinstance(event, NodeErrorEvent):
            self.errors.append(event)


@pytest.fixture(params=["sync", "async"])
def family(request) -> str:
    return request.param


def _runner(family: str, **kwargs):
    return SyncRunner(**kwargs) if family == "sync" else AsyncRunner(**kwargs)


async def _settle(outcome):
    return await outcome if inspect.iscoroutine(outcome) else outcome


def _first_line(text: str) -> str:
    return text.splitlines()[0]


def _last_line(text: str) -> str:
    return text.splitlines()[-1]


@node(output_name="y")
def boom(x: int) -> int:
    import hg_n1_missing_module  # noqa: F401

    return x


@node(output_name="y")
def fine(x: int) -> int:
    return x


@node(output_name="z")
def after(y: int) -> int:
    return y


# ---------------------------------------------------------------------------
# exports
# ---------------------------------------------------------------------------


def test_it_is_exported_next_to_the_console():
    from hypergraph import FailureLogProcessor as from_root
    from hypergraph.events import FailureLogProcessor as from_events
    from hypergraph.events.failure_log import FailureLogProcessor

    assert from_root is FailureLogProcessor
    assert from_events is FailureLogProcessor
    assert "FailureLogProcessor" in hypergraph.events.__all__
    root = list(hypergraph.__all__)
    assert root[root.index("render_console") + 1] == "FailureLogProcessor"
    assert isinstance(FailureLogProcessor(), TypedEventProcessor)


# ---------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------


async def test_a_failed_node_logs_one_record_with_its_message_and_traceback(family, capture):
    from hypergraph import FailureLogProcessor

    sink = capture()
    recorder = ErrorRecorder()
    result = await _settle(
        _runner(family).run(
            Graph([boom], name="g"),
            {"x": 1},
            error_handling="continue",
            event_processors=[FailureLogProcessor(), recorder],
        )
    )

    assert result.failed
    [record] = sink.records
    [event] = recorder.errors
    assert record.name == "hypergraph.failures"
    assert record.levelno == logging.ERROR
    assert record.exc_info is None
    message = record.getMessage()
    assert _first_line(message) == f"node 'boom' in graph 'g' failed (run_id={event.run_id}): {MISSING}"
    assert _last_line(message) == MISSING
    assert any(line.startswith("Traceback (most recent call last):") for line in message.splitlines())
    # Lazy %-args: the template carries no exception text, the args do.
    assert "hg_n1_missing_module" not in record.msg
    assert record.args


async def test_a_node_that_returns_normally_logs_nothing(family, capture):
    from hypergraph import FailureLogProcessor

    sink = capture()
    result = await _settle(_runner(family).run(Graph([fine], name="g"), {"x": 1}, event_processors=[FailureLogProcessor()]))

    assert result.completed
    assert sink.records == []


@pytest.mark.parametrize("text", ["first reason", "a different reason"])
async def test_the_raised_message_is_what_the_record_carries(family, capture, text):
    from hypergraph import FailureLogProcessor

    @node(output_name="y")
    def reject(x: int) -> int:
        raise ValueError(text)

    sink = capture()
    await _settle(_runner(family).run(Graph([reject], name="g"), {"x": 1}, error_handling="continue", event_processors=[FailureLogProcessor()]))

    [message] = sink.messages()
    assert _first_line(message).endswith(f"): ValueError: {text}")
    assert _last_line(message) == f"ValueError: {text}"


def test_an_empty_message_logs_the_type_name_alone(capture):
    from hypergraph import FailureLogProcessor

    @node(output_name="y")
    def reject(x: int) -> int:
        raise KeyError

    sink = capture()
    SyncRunner().run(Graph([reject], name="g"), {"x": 1}, error_handling="continue", event_processors=[FailureLogProcessor()])

    [message] = sink.messages()
    assert _first_line(message).endswith("): KeyError")


# ---------------------------------------------------------------------------
# ids
# ---------------------------------------------------------------------------


async def test_the_workflow_id_follows_the_run_id(family, capture, tmp_path):
    from hypergraph import FailureLogProcessor

    sink = capture()
    recorder = ErrorRecorder()
    checkpointer = SqliteCheckpointer(str(tmp_path / "runs.db"))
    try:
        await _settle(
            _runner(family, checkpointer=checkpointer).run(
                Graph([boom], name="g"),
                {"x": 1},
                workflow_id="wf-1",
                error_handling="continue",
                event_processors=[FailureLogProcessor(), recorder],
            )
        )
    finally:
        await checkpointer.close()

    [message] = sink.messages()
    [event] = recorder.errors
    assert f"(run_id={event.run_id}, workflow_id=wf-1)" in _first_line(message)


async def test_each_failed_map_item_logs_its_own_item_index(family, capture):
    from hypergraph import FailureLogProcessor

    sink = capture()
    await _settle(
        _runner(family).map(
            Graph([boom], name="g"),
            {"x": [1, 2]},
            map_over="x",
            error_handling="continue",
            event_processors=[FailureLogProcessor()],
        )
    )

    first_lines = [_first_line(message) for message in sink.messages()]
    assert len(first_lines) == 2
    assert sum("item_index=0)" in line for line in first_lines) == 1
    assert sum("item_index=1)" in line for line in first_lines) == 1


async def test_an_unnamed_graph_drops_the_graph_clause(family, capture):
    from hypergraph import FailureLogProcessor

    sink = capture()
    await _settle(_runner(family).run(Graph([boom]), {"x": 1}, error_handling="continue", event_processors=[FailureLogProcessor()]))

    [message] = sink.messages()
    assert _first_line(message).startswith("node 'boom' failed (run_id=")


# ---------------------------------------------------------------------------
# one record per NodeErrorEvent
# ---------------------------------------------------------------------------


async def test_a_nested_failure_logs_once_per_level_innermost_first(family, capture):
    from hypergraph import FailureLogProcessor

    sink = capture()
    outer = Graph([Graph([boom], name="inner").as_node(), after], name="outer")
    await _settle(_runner(family).run(outer, {"x": 1}, error_handling="continue", event_processors=[FailureLogProcessor()]))

    first_lines = [_first_line(message) for message in sink.messages()]
    assert len(first_lines) == 2
    assert first_lines[0].startswith("node 'boom' in graph 'inner' failed")
    assert first_lines[1].startswith("node 'inner' in graph 'outer' failed")
    headlines = [line.split("): ", 1)[1] for line in first_lines]
    assert headlines[0] == headlines[1] == MISSING


async def test_an_exhausted_retry_logs_once_with_the_last_attempt(family, capture):
    from hypergraph import FailureLogProcessor

    attempts = {"n": 0}

    @node(
        output_name="y",
        retry=RetryPolicy(max_attempts=3, retry_on=(ConnectionError,), initial_delay=0.001, jitter="none"),
    )
    def flaky(x: int) -> int:
        attempts["n"] += 1
        raise ConnectionError(f"attempt {attempts['n']}")

    sink = capture()
    await _settle(_runner(family).run(Graph([flaky], name="g"), {"x": 1}, error_handling="continue", event_processors=[FailureLogProcessor()]))

    [message] = sink.messages()
    assert _first_line(message).endswith("): ConnectionError: attempt 3")


async def test_a_recovered_retry_logs_nothing(family, capture):
    from hypergraph import FailureLogProcessor

    attempts = {"n": 0}

    @node(
        output_name="y",
        retry=RetryPolicy(max_attempts=3, retry_on=(ConnectionError,), initial_delay=0.001, jitter="none"),
    )
    def flaky(x: int) -> int:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("attempt 1")
        return x

    sink = capture()
    result = await _settle(_runner(family).run(Graph([flaky], name="g"), {"x": 1}, event_processors=[FailureLogProcessor()]))

    assert result.completed
    assert sink.records == []


# ---------------------------------------------------------------------------
# the sink
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("by", ["name", "logger"])
def test_a_custom_logger_and_level_are_honoured(capture, by):
    from hypergraph import FailureLogProcessor

    sink = capture("app.failures")
    target = "app.failures" if by == "name" else logging.getLogger("app.failures")
    SyncRunner().run(
        Graph([boom], name="g"),
        {"x": 1},
        error_handling="continue",
        event_processors=[FailureLogProcessor(target, level=logging.WARNING)],
    )

    [record] = sink.records
    assert record.name == "app.failures"
    assert record.levelno == logging.WARNING
    assert _last_line(record.getMessage()) == MISSING


def test_a_logger_that_filters_the_level_out_gets_nothing(capture):
    from hypergraph import FailureLogProcessor

    sink = capture("quiet.failures", level=logging.CRITICAL)
    SyncRunner().run(
        Graph([boom], name="g"),
        {"x": 1},
        error_handling="continue",
        event_processors=[FailureLogProcessor("quiet.failures")],
    )

    assert sink.records == []


def test_an_event_without_detail_falls_back_to_the_safe_text(capture):
    from hypergraph import FailureLogProcessor

    sink = capture()
    FailureLogProcessor().on_event(
        NodeErrorEvent(
            run_id="run-1",
            node_name="n",
            graph_name="g",
            error="ValueError [HG_NODE_FAILED]: Node 'n' raised ValueError.",
            error_type="builtins.ValueError",
        )
    )

    assert sink.messages() == ["node 'n' in graph 'g' failed (run_id=run-1): ValueError [HG_NODE_FAILED]: Node 'n' raised ValueError."]


@pytest.mark.parametrize(
    ("kwargs", "parameter"),
    [
        ({"logger": 42}, "logger"),
        ({"level": "ERROR"}, "level"),
        ({"level": True}, "level"),
    ],
)
def test_a_wrong_argument_is_refused_at_construction(kwargs, parameter):
    from hypergraph import FailureLogProcessor

    with pytest.raises(TypeError) as excinfo:
        FailureLogProcessor(**kwargs)

    text = str(excinfo.value)
    assert text.startswith(f"FailureLogProcessor {parameter} ")
    assert "How to fix:" in text


# ---------------------------------------------------------------------------
# privacy: the detail goes to the log, never to a durable record
# ---------------------------------------------------------------------------


async def test_the_secret_reaches_the_log_and_no_durable_byte(family, capture, tmp_path):
    from hypergraph import FailureLogProcessor

    @node(output_name="y")
    def leaky(x: int) -> int:
        raise ValueError(f"auth failed for {SECRET}")

    sink = capture()
    checkpointer = SqliteCheckpointer(str(tmp_path / "privacy.db"))
    try:
        result = await _settle(
            _runner(family, checkpointer=checkpointer).run(
                Graph([leaky], name="auth"),
                {"x": 1},
                workflow_id="wf-privacy",
                error_handling="continue",
                event_processors=[FailureLogProcessor()],
            )
        )
    finally:
        await checkpointer.close()

    [message] = sink.messages()
    assert SECRET in message
    database_files = list(tmp_path.glob("*.db*"))
    assert database_files
    assert all(SECRET.encode() not in path.read_bytes() for path in database_files)
    assert SECRET not in str(result.to_dict())


# ---------------------------------------------------------------------------
# durable Host execution
# ---------------------------------------------------------------------------


async def _terminal(client, ref):
    async for _update in client.watch(ref):
        pass
    view = await client.get(ref)
    if view is None:
        raise AssertionError("watch ended without a terminal view")
    return view


async def test_a_failed_durable_run_logs_its_message_under_its_workflow_id(capture, tmp_path):
    from hypergraph import FailureLogProcessor

    @node(output_name="y")
    async def reject(x: int) -> int:
        raise ValueError(f"document {x} rejected")

    sink = capture()
    runtime = HostRuntime(tmp_path / "runs.db", deployment_version="v1", event_processors=[FailureLogProcessor()])
    graph = Graph([reject], name="durable")
    try:
        host = await runtime.serving(graph)
        receipt = await host.submit(graph, {"x": 7}, workflow_id="doc-7")
        view = await asyncio.wait_for(_terminal(runtime.client, receipt.run_ref), timeout=10)
    finally:
        await runtime.close()

    assert view.status == WorkflowStatus.FAILED
    [message] = sink.messages()
    assert "workflow_id=doc-7" in _first_line(message)
    assert "ValueError: document 7 rejected" in _first_line(message)


# ---------------------------------------------------------------------------
# docs
# ---------------------------------------------------------------------------


def test_the_docs_describe_the_processor_and_the_boundary():
    events = (REPO_ROOT / "docs/06-api-reference/events.md").read_text()
    assert "## FailureLogProcessor" in events
    assert "hypergraph.failures" in events
    assert "opt-in" in events

    errors = (REPO_ROOT / "docs/06-api-reference/errors.md").read_text()
    boundary = errors.split("## The privacy boundary", 1)[1].split("\n## ", 1)[0]
    assert "FailureLogProcessor" in boundary
