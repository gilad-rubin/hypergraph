"""Versioned, offline notebook transport for typed inspection artifacts.

The normal transport has two physical notebook outputs: an immutable iframe
shell and one display-ID payload channel. One measured server-side executor
needs a private payload-only append fallback. Runtime attachment belongs to
runner templates; this module deliberately knows only typed inspection
sessions and artifacts.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import importlib.metadata
import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import lru_cache
from importlib.resources import files
from typing import Literal, Protocol, cast

from hypergraph._repr import plain_reprs
from hypergraph.runners._shared._inspect import (
    InspectionSession,
    MapInspection,
    MapInspectionSession,
    RunInspection,
)
from hypergraph.runners._shared._inspect_html import (
    build_inspection_payload,
    render_inspection_payload,
)
from hypergraph.runners._shared._inspect_serialization import (
    SerializedValue,
    serialize_value,
    serialized_value_to_wire,
)

INSPECTION_PROTOCOL_VERSION = 1
_UPDATE_MESSAGE = "hypergraph.inspect.update"
_COALESCE_SECONDS = 0.250
_DeliveryState = Literal["live", "stale", "saved"]
_DeliveryLabel = Literal[
    "Live",
    "Saved snapshot",
    "Live inspection unavailable",
    "Waiting for live inspection",
]
_InspectionArtifact = RunInspection | MapInspection


@dataclass(frozen=True, slots=True)
class InspectionDelivery:
    """Typed presentation state carried independently of execution status."""

    state: _DeliveryState
    label: _DeliveryLabel

    def __post_init__(self) -> None:
        if self.state not in {"live", "stale", "saved"}:
            raise ValueError("Inspection delivery state must be 'live', 'stale', or 'saved'.")
        if self.label not in {
            "Live",
            "Saved snapshot",
            "Live inspection unavailable",
            "Waiting for live inspection",
        }:
            raise ValueError("Inspection delivery label is not a supported transport label.")


@dataclass(frozen=True, slots=True)
class InspectionEnvelope:
    """One authenticated delivery of the shared typed inspection artifact."""

    protocol_version: int
    widget_id: str
    nonce: str
    sequence: int
    delivery: InspectionDelivery
    artifact: _InspectionArtifact
    message: SerializedValue | None = None

    def __post_init__(self) -> None:
        if self.protocol_version != INSPECTION_PROTOCOL_VERSION:
            raise ValueError(f"Inspection protocol_version must be {INSPECTION_PROTOCOL_VERSION}.")
        if not self.widget_id:
            raise ValueError("Inspection widget_id must not be empty.")
        if not self.nonce:
            raise ValueError("Inspection nonce must not be empty.")
        if self.sequence < 1:
            raise ValueError("Inspection sequence must be at least 1.")


def inspection_envelope_to_wire(envelope: InspectionEnvelope) -> dict[str, object]:
    """Cross the sole explicit Python-to-browser dictionary boundary."""
    payload = build_inspection_payload(
        envelope.artifact,
        delivery_state=envelope.delivery.state,
        delivery_label=envelope.delivery.label,
    )
    return {
        "type": _UPDATE_MESSAGE,
        "version": envelope.protocol_version,
        "widget_id": envelope.widget_id,
        "nonce": envelope.nonce,
        "sequence": envelope.sequence,
        "payload": payload,
        "message": (serialized_value_to_wire(envelope.message) if envelope.message is not None else None),
    }


def _script_safe_json(value: object) -> str:
    """Encode inert inline JSON, including closing tags and JS separators."""
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    )
    return encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


@lru_cache(maxsize=1)
def _bridge_asset() -> str:
    return files("hypergraph.runners._shared.assets").joinpath("inspect_transport.js").read_text(encoding="utf-8")


_CHILD_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; "
    "object-src 'none'; base-uri 'none'; form-action 'none'"
)


def _dom_id(widget_id: str, suffix: str) -> str:
    return f"{widget_id}-{suffix}"


def render_notebook_shell(
    envelope: InspectionEnvelope,
    *,
    handshake_timeout_ms: int = 1_500,
) -> str:
    """Render the one immutable iframe shell for a notebook cell."""
    if handshake_timeout_ms < 1:
        raise ValueError("handshake_timeout_ms must be at least 1.")

    # The second notebook display call is the proof that a mutable payload
    # channel exists. Until it succeeds, the immutable shell must not claim to
    # be live: a channel-display failure would otherwise orphan false-live UI.
    shell_envelope = (
        replace(
            envelope,
            delivery=InspectionDelivery(
                state="stale",
                label="Waiting for live inspection",
            ),
        )
        if envelope.delivery.state == "live"
        else envelope
    )
    wire = inspection_envelope_to_wire(shell_envelope)
    payload = cast(dict[str, object], wire["payload"])
    renderer = render_inspection_payload(payload)
    child_config = _script_safe_json(
        {
            "widgetId": envelope.widget_id,
            "nonce": envelope.nonce,
        }
    )
    bridge = _bridge_asset()
    child_document = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" '
        f'content="{_CHILD_CSP}">'
        "<style>html,body{margin:0;min-width:0;background:transparent}"
        "body{overflow-x:hidden}</style></head><body>"
        f"{renderer}"
        f"<script>{bridge}</script>"
        "<script data-hg-inspect-child-bootstrap>"
        f"window.__hypergraphInspectTransport.installChild({child_config});"
        "</script></body></html>"
    )

    frame_id = _dom_id(envelope.widget_id, "frame")
    host_id = _dom_id(envelope.widget_id, "host")
    status_id = _dom_id(envelope.widget_id, "host-status")
    display_id = _dom_id(envelope.widget_id, "payload")
    parent_config = _script_safe_json(
        {
            "widgetId": envelope.widget_id,
            "nonce": envelope.nonce,
            "frameId": frame_id,
            "statusId": status_id,
            "handshakeTimeoutMs": handshake_timeout_ms,
        }
    )
    return (
        f'<div id="{html.escape(host_id, quote=True)}" '
        f'data-hg-inspect-host="{html.escape(envelope.widget_id, quote=True)}" '
        'style="width:100%;min-width:0">'
        f'<div id="{html.escape(status_id, quote=True)}" '
        f'data-hg-inspect-host-status="{html.escape(envelope.widget_id, quote=True)}" '
        'data-state="connecting" role="status" aria-live="polite" '
        'style="box-sizing:border-box;margin:0 0 8px;padding:8px 10px;'
        'border:1px solid #d0d5dd;border-radius:8px;font:13px system-ui,sans-serif">'
        "Connecting live inspection…</div>"
        f'<iframe id="{html.escape(frame_id, quote=True)}" '
        f'name="{html.escape(frame_id, quote=True)}" '
        f'data-hg-inspect-frame="{html.escape(envelope.widget_id, quote=True)}" '
        'title="Hypergraph execution inspection" sandbox="allow-scripts" '
        'style="display:block;box-sizing:border-box;width:100%;min-width:0;'
        'height:720px;border:0" '
        f'srcdoc="{html.escape(child_document, quote=True)}"></iframe>'
        "</div>"
        f"<script>{bridge}</script>"
        "<script data-hg-inspect-parent-bootstrap>"
        f"window.__hypergraphInspectTransport.installParent({parent_config});"
        "</script>"
        # Record the stable logical display ID even though each rendered payload
        # envelope has a sequence-qualified physical DOM identity.
        f"<!-- payload-channel:{html.escape(display_id)} -->"
    )


def _fallback_markup(envelope: InspectionEnvelope, payload: dict[str, object]) -> str:
    kind = cast(str, payload["kind"])
    data = cast(dict[str, object], payload[kind])
    graph_name = str(data.get("graph_name") or "Hypergraph execution")
    status = str(data.get("status") or "unknown")
    if kind == "map":
        counts = cast(dict[str, object], data.get("counts") or {})
        detail = f"{counts.get('completed', 0)} completed, {counts.get('failed', 0)} failed, {counts.get('unstarted', 0)} unstarted"
    else:
        nodes = cast(list[object], data.get("nodes") or [])
        detail = f"{len(nodes)} captured node{'s' if len(nodes) != 1 else ''}"
    error_detail = ""
    if envelope.message is not None:
        message = serialized_value_to_wire(envelope.message)
        error_detail = f" {message.get('type_name', 'Error')}: {message.get('text', '')}"
    delivery_label = "Waiting for live inspection" if envelope.delivery.state == "live" else envelope.delivery.label
    return (
        f"<strong>{html.escape(delivery_label)}</strong> — "
        f"{html.escape(graph_name)} is {html.escape(status)}; "
        f"{html.escape(detail)}.{html.escape(error_detail)}"
    )


def render_payload_channel(envelope: InspectionEnvelope) -> str:
    """Render one payload-only display-ID value with an inert safe fallback."""
    wire = inspection_envelope_to_wire(envelope)
    payload = cast(dict[str, object], wire["payload"])
    encoded = _script_safe_json(wire)
    channel_dom_id = _dom_id(envelope.widget_id, f"payload-output-s{envelope.sequence}")
    key = _script_safe_json(f"{envelope.widget_id}::{envelope.nonce}")
    fallback_state = "waiting" if envelope.delivery.state == "live" else envelope.delivery.state
    return (
        f'<div id="{html.escape(channel_dom_id, quote=True)}" '
        f'data-hg-inspect-channel="{html.escape(envelope.widget_id, quote=True)}">'
        f'<div data-hg-inspect-channel-fallback="{html.escape(envelope.widget_id, quote=True)}" '
        f'data-delivery-state="{fallback_state}" '
        'role="status" style="box-sizing:border-box;margin:8px 0;padding:8px 10px;'
        'border:1px solid #d0d5dd;border-radius:8px;font:13px system-ui,sans-serif">'
        f"{_fallback_markup(envelope, payload)}</div>"
        '<script type="application/json" data-hg-inspect-envelope>'
        f"{encoded}</script>"
        "<script data-hg-inspect-channel-runtime>(function(){"
        "var channel=document.currentScript.closest('[data-hg-inspect-channel]');"
        "if(!channel)return;"
        "var source=channel.querySelector('[data-hg-inspect-envelope]');"
        "if(!source)return;"
        "var envelope=JSON.parse(source.textContent||'{}');"
        "var queues=window.__hypergraphInspectQueues||(window.__hypergraphInspectQueues=Object.create(null));"
        f"var key={key};"
        "var hosts=window.__hypergraphInspectHosts;"
        "if(hosts&&hosts[key])hosts[key].deliver(envelope,channel.id);"
        "else{var queued=queues[key];"
        "if(!queued||envelope.sequence>queued.envelope.sequence){"
        "if(queued&&queued.channelElement){"
        "var oldFallback=queued.channelElement.querySelector('[data-hg-inspect-channel-fallback]');"
        "if(oldFallback)oldFallback.hidden=true;"
        "queued.channelElement.setAttribute('data-delivered','true');}"
        "queues[key]={envelope:envelope,channelId:channel.id,channelElement:channel};}"
        "else{var fallback=channel.querySelector('[data-hg-inspect-channel-fallback]');"
        "if(fallback)fallback.hidden=true;channel.setAttribute('data-delivered','true');}}"
        "})();</script></div>"
    )


class _ScheduledHandle(Protocol):
    def cancel(self) -> None: ...


class _InspectionScheduler(Protocol):
    owner_thread_id: int
    supports_cross_thread: bool

    def now(self) -> float: ...

    def call_at(
        self,
        deadline: float,
        callback: Callable[[], None],
    ) -> _ScheduledHandle | None: ...


class _GuardedCall:
    """Cancellation flag that remains safe when a backend timer already fired."""

    def __init__(self, callback: Callable[[], None]) -> None:
        self._callback = callback
        self._cancelled = False
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    def run(self) -> None:
        with self._lock:
            if self._cancelled:
                return
        self._callback()


class OwnerThreadScheduler:
    """Marshal display work to the captured notebook/kernel owner thread."""

    def __init__(
        self,
        *,
        asyncio_loop: asyncio.AbstractEventLoop | None,
        kernel_ioloop: object | None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.owner_thread_id = threading.get_ident()
        self._asyncio_loop = asyncio_loop
        self._kernel_ioloop = kernel_ioloop
        self._clock = clock

    @classmethod
    def capture(cls) -> OwnerThreadScheduler:
        """Capture asyncio first, then IPykernel's thread-safe Tornado loop."""
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        return cls(
            asyncio_loop=loop,
            kernel_ioloop=None if loop is not None else _current_kernel_ioloop(),
        )

    @property
    def supports_cross_thread(self) -> bool:
        loop = self._asyncio_loop
        if loop is not None and loop.is_running():
            return True
        return callable(getattr(self._kernel_ioloop, "add_callback", None))

    def now(self) -> float:
        return self._clock()

    def call_at(
        self,
        deadline: float,
        callback: Callable[[], None],
    ) -> _GuardedCall | None:
        guarded = _GuardedCall(callback)

        def arm() -> None:
            if guarded.cancelled:
                return
            delay = max(0.0, deadline - self.now())
            if delay <= 0:
                guarded.run()
                return
            if self._asyncio_loop is not None and self._asyncio_loop.is_running():
                self._asyncio_loop.call_later(delay, guarded.run)
                return
            call_later = getattr(self._kernel_ioloop, "call_later", None)
            if callable(call_later):
                call_later(delay, guarded.run)

        if threading.get_ident() == self.owner_thread_id:
            if deadline > self.now() and not self.supports_cross_thread:
                return None
            arm()
            return guarded

        if self._asyncio_loop is not None and self._asyncio_loop.is_running():
            self._asyncio_loop.call_soon_threadsafe(arm)
            return guarded
        add_callback = getattr(self._kernel_ioloop, "add_callback", None)
        if callable(add_callback):
            add_callback(arm)
            return guarded
        return None


def _current_kernel_ioloop() -> object | None:
    try:
        from IPython import get_ipython

        shell = get_ipython()
    except (ImportError, NameError):
        return None
    kernel = getattr(shell, "kernel", None)
    return getattr(kernel, "io_loop", None) or getattr(kernel, "ioloop", None)


@dataclass(frozen=True, slots=True)
class _PendingDelivery:
    artifact: _InspectionArtifact
    urgent: bool
    delivery: InspectionDelivery
    message: SerializedValue | None
    close_after: bool


class InspectionCoalescer:
    """Thread-safe 250 ms latest-wins gate with urgent preemption."""

    def __init__(
        self,
        *,
        widget_id: str,
        nonce: str,
        scheduler: _InspectionScheduler,
        deliver: Callable[[InspectionEnvelope], None],
        initial_sequence: int = 0,
        initial_sent_at: float | None = None,
        initially_closed: bool = False,
        initial_accepted_revision: int = -1,
    ) -> None:
        self._widget_id = widget_id
        self._nonce = nonce
        self._scheduler = scheduler
        self._deliver = deliver
        self._lock = threading.RLock()
        self._pending: _PendingDelivery | None = None
        self._scheduled_token: object | None = None
        self._scheduled_handle: _ScheduledHandle | None = None
        self._last_sent_at = initial_sent_at
        self._sequence = initial_sequence
        self._closed = initially_closed
        self._delivery_failed = False
        self._latest_accepted_revision = initial_accepted_revision

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def delivery_failed(self) -> bool:
        with self._lock:
            return self._delivery_failed

    def publish(
        self,
        artifact: _InspectionArtifact,
        urgent: bool,
        *,
        delivery: InspectionDelivery | None = None,
        message: SerializedValue | None = None,
        close_after: bool = False,
    ) -> bool:
        """Offer one state without allowing presentation failure to escape."""
        handle_to_cancel: _ScheduledHandle | None = None
        token: object | None = None
        deadline = 0.0
        try:
            with self._lock:
                if self._closed:
                    return False
                if self._pending is not None and (self._pending.artifact.terminal or self._pending.close_after):
                    return False
                if artifact.revision < self._latest_accepted_revision:
                    return False
                effective_delivery = delivery or (
                    InspectionDelivery(state="saved", label="Saved snapshot") if artifact.terminal else InspectionDelivery(state="live", label="Live")
                )
                retain_immediate_delivery = self._pending is not None and self._pending.urgent and self._scheduled_token is not None
                self._pending = _PendingDelivery(
                    artifact=artifact,
                    urgent=urgent or retain_immediate_delivery,
                    delivery=effective_delivery,
                    message=message,
                    close_after=close_after,
                )
                self._latest_accepted_revision = artifact.revision
                if urgent and self._scheduled_token is not None:
                    handle_to_cancel = self._scheduled_handle
                    self._scheduled_token = None
                    self._scheduled_handle = None
                if self._scheduled_token is None:
                    now = self._scheduler.now()
                    deadline = (
                        now
                        if urgent or self._last_sent_at is None or now >= self._last_sent_at + _COALESCE_SECONDS
                        else self._last_sent_at + _COALESCE_SECONDS
                    )
                    token = object()
                    self._scheduled_token = token
            if handle_to_cancel is not None:
                handle_to_cancel.cancel()
            if token is not None:
                self._arm(token, deadline)
            return True
        except Exception:
            self._mark_delivery_failed()
            return False

    def _arm(self, token: object, deadline: float) -> None:
        handle = self._scheduler.call_at(
            deadline,
            lambda: self._flush(token),
        )
        with self._lock:
            if self._scheduled_token is token:
                if handle is None:
                    self._scheduled_token = None
                    self._scheduled_handle = None
                else:
                    self._scheduled_handle = handle
            elif handle is not None:
                handle.cancel()

    def _flush(self, token: object) -> None:
        pending: _PendingDelivery | None = None
        with self._lock:
            if self._closed or self._scheduled_token is not token:
                return
            self._scheduled_token = None
            self._scheduled_handle = None
            pending = self._pending
            if pending is None:
                return
            now = self._scheduler.now()
            if not pending.urgent and self._last_sent_at is not None and now < self._last_sent_at + _COALESCE_SECONDS:
                next_token = object()
                self._scheduled_token = next_token
                deadline = self._last_sent_at + _COALESCE_SECONDS
            else:
                next_token = None
                deadline = 0.0
                self._pending = None
                self._sequence += 1
                sequence = self._sequence
                self._last_sent_at = now
                if pending.artifact.terminal or pending.close_after:
                    self._closed = True

        if next_token is not None:
            try:
                self._arm(next_token, deadline)
            except Exception:
                self._mark_delivery_failed()
            return

        assert pending is not None
        envelope = InspectionEnvelope(
            protocol_version=INSPECTION_PROTOCOL_VERSION,
            widget_id=self._widget_id,
            nonce=self._nonce,
            sequence=sequence,
            delivery=pending.delivery,
            artifact=pending.artifact,
            message=pending.message,
        )
        try:
            self._deliver(envelope)
        except Exception:
            self._mark_delivery_failed()

    def _mark_delivery_failed(self) -> None:
        with self._lock:
            self._delivery_failed = True
            self._closed = True
            handle = self._scheduled_handle
            self._scheduled_token = None
            self._scheduled_handle = None
            self._pending = None
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.cancel()


class _NotebookDisplayHandle(Protocol):
    def update(self, markup: str) -> None: ...


class NotebookDisplay(Protocol):
    """Optional-IPython string boundary used by the transport and tests."""

    def display_shell(self, markup: str) -> None: ...

    def display_channel(
        self,
        markup: str,
        *,
        display_id: str,
    ) -> _NotebookDisplayHandle: ...


class _IPythonDisplayHandle:
    def __init__(
        self,
        handle: object,
        *,
        display_id: str,
        append_payloads: bool,
    ) -> None:
        self._handle = handle
        self._display_id = display_id
        self._append_payloads = append_payloads

    def update(self, markup: str) -> None:
        from IPython.display import HTML

        if self._append_payloads:
            from IPython.display import display

            display(HTML(markup), display_id=self._display_id)
            return
        update = getattr(self._handle, "update", None)
        if not callable(update):
            raise RuntimeError("IPython display handle does not support update().")
        update(HTML(markup))


class _IPythonNotebookDisplay:
    def display_shell(self, markup: str) -> None:
        from IPython.display import HTML, display

        display(HTML(markup))

    def display_channel(
        self,
        markup: str,
        *,
        display_id: str,
    ) -> _IPythonDisplayHandle:
        from IPython.display import HTML, display

        try:
            package_version = importlib.metadata.version("jupyter-server-nbmodel")
        except importlib.metadata.PackageNotFoundError:
            package_version = None
        append_payloads = package_version == "0.1.1a4"
        handle = display(HTML(markup), display_id=display_id)
        if handle is None:
            raise RuntimeError("IPython did not return a display-ID handle.")
        return _IPythonDisplayHandle(
            handle,
            display_id=display_id,
            append_payloads=append_payloads,
        )


class NotebookInspectionTransport:
    """Own one immutable notebook shell and its one mutable payload channel."""

    def __init__(
        self,
        *,
        initial_artifact: _InspectionArtifact,
        channel_handle: _NotebookDisplayHandle,
        scheduler: _InspectionScheduler,
        widget_id: str,
        nonce: str,
        initially_closed: bool,
    ) -> None:
        self.widget_id = widget_id
        self.nonce = nonce
        self.display_id = _dom_id(widget_id, "payload")
        self._channel_handle = channel_handle
        self._scheduler = scheduler
        self._latest_artifact = initial_artifact
        self._unsubscribe: Callable[[], None] | None = None
        self._attachment_lock = threading.RLock()
        self._coalescer = InspectionCoalescer(
            widget_id=widget_id,
            nonce=nonce,
            scheduler=scheduler,
            deliver=self._deliver,
            initial_sequence=1,
            initial_sent_at=scheduler.now(),
            initially_closed=initially_closed,
            initial_accepted_revision=initial_artifact.revision,
        )

    @classmethod
    def create(
        cls,
        initial_artifact: _InspectionArtifact,
        *,
        display: NotebookDisplay,
        scheduler: _InspectionScheduler,
        widget_id: str | None = None,
        nonce: str | None = None,
        initial_delivery: InspectionDelivery | None = None,
        close_after_initial: bool | None = None,
    ) -> NotebookInspectionTransport:
        """Display exactly two outputs on the current notebook owner thread."""
        resolved_widget_id = widget_id or f"hg-inspect-{secrets.token_hex(12)}"
        resolved_nonce = nonce or secrets.token_urlsafe(32)
        delivery = initial_delivery or (
            InspectionDelivery(state="saved", label="Saved snapshot") if initial_artifact.terminal else InspectionDelivery(state="live", label="Live")
        )
        envelope = InspectionEnvelope(
            protocol_version=INSPECTION_PROTOCOL_VERSION,
            widget_id=resolved_widget_id,
            nonce=resolved_nonce,
            sequence=1,
            delivery=delivery,
            artifact=initial_artifact,
        )
        display.display_shell(render_notebook_shell(envelope))
        display_id = _dom_id(resolved_widget_id, "payload")
        handle = display.display_channel(
            render_payload_channel(envelope),
            display_id=display_id,
        )
        initially_closed = initial_artifact.terminal if close_after_initial is None else close_after_initial
        return cls(
            initial_artifact=initial_artifact,
            channel_handle=handle,
            scheduler=scheduler,
            widget_id=resolved_widget_id,
            nonce=resolved_nonce,
            initially_closed=initially_closed,
        )

    @property
    def closed(self) -> bool:
        return self._coalescer.closed

    def publish(self, artifact: _InspectionArtifact, urgent: bool) -> None:
        if self._coalescer.publish(artifact, urgent):
            self._latest_artifact = artifact

    def attach(self, session: InspectionSession | MapInspectionSession) -> None:
        """Replay the current snapshot, then observe future session publications."""
        with self._attachment_lock:
            if self.closed:
                return
            if self._unsubscribe is not None:
                raise RuntimeError("Notebook inspection transport is already attached.")

            def publish(artifact: _InspectionArtifact, urgent: bool) -> None:
                with self._attachment_lock:
                    self.publish(artifact, urgent)

            snapshot, unsubscribe = session.subscribe_with_snapshot(publish)
            self._unsubscribe = unsubscribe
            self.publish(snapshot, urgent=snapshot.terminal)

    def fail_to_start(self, error: BaseException) -> None:
        """Settle a pre-opened shell without fabricating an execution artifact."""
        self._coalescer.publish(
            self._latest_artifact,
            urgent=True,
            delivery=InspectionDelivery(
                state="stale",
                label="Live inspection unavailable",
            ),
            message=serialize_value(error),
            close_after=True,
        )

    def _deliver(self, envelope: InspectionEnvelope) -> None:
        self._latest_artifact = envelope.artifact
        try:
            self._channel_handle.update(render_payload_channel(envelope))
        except Exception:
            self._detach()
            raise
        if envelope.artifact.terminal or envelope.delivery.state == "stale":
            self._detach()

    def _detach(self) -> None:
        with self._attachment_lock:
            unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()


def _is_notebook() -> bool:
    try:
        from IPython import get_ipython

        shell = get_ipython()
        if shell is None:
            return False
        module = type(shell).__module__.lower()
        name = type(shell).__name__.lower()
        if "zmq" in module or "zmq" in name:
            return True
        if getattr(shell, "kernel", None) is not None:
            return True
        config = getattr(shell, "config", {})
        with contextlib.suppress(Exception):
            return "IPKernelApp" in config
        return False
    except (ImportError, NameError):
        return False


def open_notebook_inspection_transport(
    initial_artifact: _InspectionArtifact,
    *,
    notebook: bool | None = None,
    display: NotebookDisplay | None = None,
    scheduler: _InspectionScheduler | None = None,
    require_cross_thread: bool = False,
) -> NotebookInspectionTransport | None:
    """Failure-isolated automatic display factory used by Wave 2D integration."""
    try:
        if plain_reprs():
            return None
        if not (_is_notebook() if notebook is None else notebook):
            return None
        resolved_scheduler = scheduler or OwnerThreadScheduler.capture()
        resolved_display = display or _IPythonNotebookDisplay()
        if require_cross_thread and not resolved_scheduler.supports_cross_thread:
            return NotebookInspectionTransport.create(
                initial_artifact,
                display=resolved_display,
                scheduler=resolved_scheduler,
                initial_delivery=InspectionDelivery(
                    state="stale",
                    label="Live inspection unavailable",
                ),
                close_after_initial=True,
            )
        return NotebookInspectionTransport.create(
            initial_artifact,
            display=resolved_display,
            scheduler=resolved_scheduler,
        )
    except Exception:
        # Presentation is observational. Capture and workflow behavior survive
        # missing IPython, host policy, a closed display, or renderer failure.
        return None
