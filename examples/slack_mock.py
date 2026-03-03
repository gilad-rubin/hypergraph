"""Shared Slack mock classes for local interrupt demos."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib import error, request
from urllib.parse import urlparse


def _post_json(url: str, payload: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        method="POST",
        headers={"content-type": "application/json"},
    )
    with request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local demo endpoint
        data = resp.read().decode("utf-8")
    return json.loads(data) if data else {}


def _get_json(url: str, timeout: float = 5.0) -> tuple[int, dict[str, Any]]:
    req = request.Request(url, method="GET")
    try:
        with request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local demo endpoint
            status = resp.status
            data = resp.read().decode("utf-8")
    except error.HTTPError as exc:
        status = exc.code
        data = exc.read().decode("utf-8")
    payload = json.loads(data) if data else {}
    return status, payload


class SlackClient:
    """HTTP client for posting messages and polling responses."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def post_message(self, text: str) -> None:
        _post_json(f"{self.base_url}/messages", {"text": text})

    def queue_response(self, text: str) -> None:
        _post_json(f"{self.base_url}/responses", {"text": text})

    async def receive_response(self, *, poll_seconds: float = 1.0) -> str:
        while True:
            status, payload = await asyncio.to_thread(_get_json, f"{self.base_url}/responses/next")
            if status == 200 and "response" in payload:
                return str(payload["response"])
            if status not in (200, 204):
                raise RuntimeError(f"Mock Slack polling failed: status={status}, payload={payload}")
            await asyncio.sleep(poll_seconds)

    def list_messages(self) -> list[str]:
        status, payload = _get_json(f"{self.base_url}/messages")
        if status != 200:
            raise RuntimeError(f"Failed to read messages: status={status}, payload={payload}")
        messages = payload.get("messages", [])
        return [str(m) for m in messages] if isinstance(messages, list) else []


@dataclass
class SlackState:
    """In-memory storage used by the mock server."""

    messages: list[str] = field(default_factory=list)
    responses: deque[str] = field(default_factory=deque)


class MockSlackServer:
    """Tiny HTTP server that emulates a send + queued-reply Slack flow."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.host = host
        self.port = port
        self.state = SlackState()
        handler_cls = self._build_handler()
        self._server = ThreadingHTTPServer((self.host, self.port), handler_cls)

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def _write_json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    parsed = {}
                return parsed if isinstance(parsed, dict) else {}

            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path == "/health":
                    self._write_json(HTTPStatus.OK, {"ok": True})
                    return
                if path == "/messages":
                    self._write_json(HTTPStatus.OK, {"messages": state.messages})
                    return
                if path == "/responses/next":
                    if state.responses:
                        text = state.responses.popleft()
                        self._write_json(HTTPStatus.OK, {"response": text})
                        return
                    self._write_json(HTTPStatus.NO_CONTENT, {})
                    return
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                payload = self._read_json()
                text = str(payload.get("text", "")).strip()

                if path == "/messages":
                    if not text:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"error": "expected JSON {'text': '...'}"})
                        return
                    state.messages.append(text)
                    print(f"[server] message posted: {text}")
                    self._write_json(HTTPStatus.CREATED, {"ok": True, "message_count": len(state.messages)})
                    return

                if path == "/responses":
                    if not text:
                        self._write_json(HTTPStatus.BAD_REQUEST, {"error": "expected JSON {'text': '...'}"})
                        return
                    state.responses.append(text)
                    print(f"[server] response queued: {text}")
                    self._write_json(HTTPStatus.CREATED, {"ok": True, "queued_responses": len(state.responses)})
                    return

                self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        return Handler

    def print_banner(self) -> None:
        print(f"Mock Slack server listening on http://{self.host}:{self.port}")
        print("POST /messages         -> graph posts question text")
        print("POST /responses        -> you queue a user reply")
        print("GET  /responses/next   -> demo polls for next reply")
        print("GET  /messages         -> inspect posted questions")

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def close(self) -> None:
        self._server.server_close()
