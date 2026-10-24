"""Tests for the model-check command (fake backend, fakeredis; no live model)."""

from __future__ import annotations

import struct
import zlib
from typing import Any

import fakeredis
import pytest

from gatekeeper.config import Settings
from gatekeeper.errors import ConfigError, ModelUnavailable
from gatekeeper.models import check
from gatekeeper.models.backend import BackendCall
from gatekeeper.models.cache import RedisModelCache
from gatekeeper.models.testing import fake_adapter
from gatekeeper.models.types import BackendHealth, Capabilities

SECRET = "gemini-key-that-must-not-print"


def use_settings(monkeypatch: pytest.MonkeyPatch, **values: Any) -> None:
    base: dict[str, Any] = {"ollama_base_url": "http://127.0.0.1:11434", "gemini_api_key": SECRET}
    base.update(values)
    settings = Settings(_env_file=None, **base)  # type: ignore[call-arg]
    monkeypatch.setattr(check, "get_settings", lambda: settings)
    monkeypatch.setattr(check, "configure_logging", lambda _settings: None)


def reply(call: BackendCall) -> dict[str, Any]:
    if call.parts and any(part.kind == "image" for part in call.parts):
        return {"colour": "Red"}
    return {"greeting": "hello", "number": 3}


def test_solid_png_is_a_valid_png() -> None:
    png = check.solid_png((0, 0, 255), size=4)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    length, tag = struct.unpack(">I4s", png[8:16])
    assert (length, tag) == (13, b"IHDR")
    assert struct.unpack(">II", png[16:24]) == (4, 4)
    idat_start = png.index(b"IDAT") + 4
    idat_len = struct.unpack(">I", png[idat_start - 8 : idat_start - 4])[0]
    raw = zlib.decompress(png[idat_start : idat_start + idat_len])
    assert raw[:4] == b"\x00\x00\x00\xff"


def test_missing_model_prints_what_to_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_settings(monkeypatch)
    assert check.main([]) == 2
    out = capsys.readouterr().out
    assert "OLLAMA_MODEL=gemma4:e4b" in out
    assert SECRET not in out


def test_two_calls_second_is_cache_hit_and_vision(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_settings(monkeypatch, ollama_model="gemma4:e4b")
    cache = RedisModelCache(fakeredis.FakeRedis(decode_responses=True), ttl_s=60)
    adapter, _ = fake_adapter(reply, cache=cache)
    monkeypatch.setattr(check, "get_backend", lambda _settings: adapter)
    assert check.main(["--vision"]) == 0
    out = capsys.readouterr().out
    call_1, call_2 = (line for line in out.splitlines() if line.startswith("Call "))
    assert "cache_hit=False" in call_1
    assert "cache_hit=True" in call_2
    assert "Vision: PASS" in out
    assert SECRET not in out


def test_vision_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_settings(monkeypatch, ollama_model="m")
    no_vision = Capabilities(False, True, False, 8192)
    adapter, _ = fake_adapter(reply, capabilities=no_vision)
    monkeypatch.setattr(check, "get_backend", lambda _settings: adapter)
    assert check.main(["--vision"]) == 0
    out = capsys.readouterr().out
    assert "Vision: FAIL (capability_missing" in out
    assert "WARNING: vision check failed" in out
    assert check.main(["--require-vision"]) == 3


def test_unhealthy_backend(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_settings(monkeypatch, ollama_model="m")
    adapter, _ = fake_adapter(reply, health=BackendHealth(False, "model not installed"))
    monkeypatch.setattr(check, "get_backend", lambda _settings: adapter)
    assert check.main([]) == 1
    assert "NOT OK - model not installed" in capsys.readouterr().out


def test_model_call_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_settings(monkeypatch, ollama_model="m")
    adapter, _ = fake_adapter([ModelUnavailable("down", retryable=False)])
    monkeypatch.setattr(check, "get_backend", lambda _settings: adapter)
    assert check.main([]) == 1
    assert "Model call failed (model_unavailable): down" in capsys.readouterr().out


def test_configuration_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken() -> Settings:
        raise ConfigError("invalid setting(s): APP_ENV")

    monkeypatch.setattr(check, "get_settings", broken)
    assert check.main([]) == 2
    assert "APP_ENV" in capsys.readouterr().out

    use_settings(monkeypatch, model_backend="vllm", ollama_model="m")
    assert check.main([]) == 2
    assert "vllm backend not implemented yet" in capsys.readouterr().out
