"""Tests for scripts/doctor.py. Network checks only use a throwaway local server."""

from __future__ import annotations

import json
import shutil
import socket
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import doctor


class _TagsHandler(BaseHTTPRequestHandler):
    body: bytes = b"{}"

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, format: str, *args: object) -> None:
        """Keep test output quiet."""


@pytest.fixture
def ollama_server() -> Iterator[tuple[str, type[_TagsHandler]]]:
    handler = type("Handler", (_TagsHandler,), {})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def closed_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def test_missing_tool_is_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    check = doctor.tool_version("ollama", ("ollama", "--version"))
    assert not check.ok
    assert check.detail == "not found on PATH"


def test_tool_version_reports_version_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: sys.executable)
    check = doctor.tool_version(
        "python", ("python", "-c", "print('warning: x'); print('v version 1')")
    )
    assert check.ok
    assert check.detail == "v version 1"


def test_tool_failure_is_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: sys.executable)
    check = doctor.tool_version("python", ("python", "-c", "raise SystemExit(3)"))
    assert not check.ok
    assert "exited with code 3" in check.detail


def test_lists_installed_models(ollama_server: tuple[str, type[_TagsHandler]]) -> None:
    url, handler = ollama_server
    handler.body = json.dumps({"models": [{"name": "b:latest"}, {"name": "a:1b"}, {}]}).encode()
    check = doctor.ollama_check(url)
    assert check.ok
    assert check.hints == ("a:1b", "b:latest")


def test_no_models_is_a_warning(ollama_server: tuple[str, type[_TagsHandler]]) -> None:
    url, handler = ollama_server
    handler.body = b'{"models": []}'
    check = doctor.ollama_check(url)
    assert not check.ok
    assert "no models" in check.detail


@pytest.mark.parametrize("body", [b"not json", b'{"models": "nope"}'])
def test_bad_payload_is_a_warning(
    ollama_server: tuple[str, type[_TagsHandler]], body: bytes
) -> None:
    url, handler = ollama_server
    handler.body = body
    assert not doctor.ollama_check(url).ok


def test_unreachable_ollama_is_a_warning() -> None:
    check = doctor.ollama_check(closed_port_url())
    assert not check.ok
    assert "no response" in check.detail


@pytest.mark.parametrize("url", ["file:///etc/passwd", "localhost:11434", ""])
def test_rejects_non_http_urls(url: str) -> None:
    check = doctor.ollama_check(url)
    assert not check.ok
    assert "http(s)" in check.detail


def test_display_url_hides_credentials() -> None:
    shown = doctor.display_url("http://someone:not-a-secret@example.test:8080/api/tags?x=1")
    assert shown == "http://example.test:8080/api/tags"


def test_main_never_crashes_when_everything_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setenv("OLLAMA_BASE_URL", closed_port_url())
    assert doctor.main() == 0
    out = capsys.readouterr().out
    assert "[WARN] git" in out
    assert "[WARN] ollama api" in out
    assert "warning(s)." in out
