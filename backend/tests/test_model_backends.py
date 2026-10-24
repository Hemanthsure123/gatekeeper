"""Backend drivers against mocked HTTP (respx), plus the factory. No live model calls."""

from __future__ import annotations

import base64
import json
import sys
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from pydantic import BaseModel

from gatekeeper.config import Settings
from gatekeeper.errors import ConfigError, ExternalServiceError, ModelTimeout, ModelUnavailable
from gatekeeper.models.adapter import ModelAdapter
from gatekeeper.models.backend import BackendCall, Turn
from gatekeeper.models.factory import get_backend, get_driver
from gatekeeper.models.gemini import GeminiBackend, retry_delay
from gatekeeper.models.ollama import OllamaBackend, parse_retry_after
from gatekeeper.models.types import ImagePart, ModelRequest, ModelRole, Reasoning, TextPart

OLLAMA = "http://ollama.test:11434"
GEMINI = "https://generativelanguage.googleapis.com"
PNG = ImagePart.from_bytes(b"\x89PNG\r\n\x1a\nimg", "image/png")


class Answer(BaseModel):
    greeting: str
    number: int


def settings(**values: Any) -> Settings:
    base: dict[str, Any] = {
        "ollama_base_url": OLLAMA,
        "ollama_model": "gemma4:e4b",
        "ollama_num_ctx": 16384,
        "model_timeout_s": 5,
    }
    base.update(values)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def show_payload(
    *, capabilities: list[str] | None = None, thinking: Any = None, ctx: int | None = 131072
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "capabilities": capabilities if capabilities is not None else ["completion", "vision"],
        "model_info": {"gemma4.context_length": ctx} if ctx else {},
    }
    if thinking is not None:
        payload["thinking"] = thinking
    return payload


def chat_reply(content: str, prompt_tokens: int = 40, eval_tokens: int = 9) -> dict[str, Any]:
    return {
        "model": "gemma4:e4b",
        "message": {"role": "assistant", "content": content, "thinking": "secret thoughts"},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": prompt_tokens,
        "eval_count": eval_tokens,
    }


def make_call(**overrides: Any) -> BackendCall:
    values: dict[str, Any] = {
        "model": "gemma4:e4b",
        "system": "system text",
        "parts": (TextPart(text="first"), TextPart(text="second")),
        "json_schema": Answer.model_json_schema(),
        "temperature": 0.0,
        "seed": 42,
        "max_output_tokens": 256,
        "reasoning": Reasoning.OFF,
    }
    values.update(overrides)
    return BackendCall(**values)


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


# -- Ollama --------------------------------------------------------------------------------


def test_ollama_chat_payload_and_parsing(mock: respx.MockRouter) -> None:
    mock.post(f"{OLLAMA}/api/show").respond(
        json=show_payload(thinking={"values": [True, False], "default": True})
    )
    chat = mock.post(f"{OLLAMA}/api/chat").respond(json=chat_reply('{"greeting":"hi","number":1}'))
    backend = OllamaBackend(settings(ollama_keep_alive="10m"))
    completion = backend.complete(
        make_call(
            parts=(TextPart(text="first"), PNG, TextPart(text="second")),
            followups=(Turn("assistant", "bad"), Turn("user", "fix it")),
        )
    )
    assert completion.text == '{"greeting":"hi","number":1}'
    assert (completion.input_tokens, completion.output_tokens) == (40, 9)
    assert completion.finish_reason == "stop"

    body = json.loads(chat.calls.last.request.content)
    assert body["model"] == "gemma4:e4b"
    assert body["stream"] is False
    assert body["format"] == Answer.model_json_schema()
    assert body["options"] == {"num_ctx": 16384, "temperature": 0.0, "seed": 42, "num_predict": 256}
    assert body["keep_alive"] == "10m"
    assert body["think"] is False
    system, user, assistant, repair = body["messages"]
    assert system == {"role": "system", "content": "system text"}
    assert user["content"] == "first\n\nsecond"
    assert user["images"] == [base64.b64encode(PNG.data).decode()]
    assert assistant == {"role": "assistant", "content": "bad"}
    assert repair == {"role": "user", "content": "fix it"}


@pytest.mark.parametrize(
    ("show", "reasoning", "expected"),
    [
        (show_payload(), Reasoning.ON, None),  # no thinking metadata: omit `think`
        (show_payload(capabilities=["completion", "thinking"]), Reasoning.ON, True),
        (show_payload(thinking={"values": ["low", "high"]}), Reasoning.ON, "high"),
        (show_payload(thinking={"values": ["low", "medium"]}), Reasoning.OFF, None),
        (show_payload(thinking={"values": [False]}), Reasoning.ON, None),
    ],
)
def test_ollama_think_value(show: dict[str, Any], reasoning: Reasoning, expected: Any) -> None:
    assert OllamaBackend._think_value(show, reasoning) == expected


def test_ollama_keep_alive_seconds_are_sent_as_numbers(mock: respx.MockRouter) -> None:
    mock.post(f"{OLLAMA}/api/show").respond(json=show_payload())
    chat = mock.post(f"{OLLAMA}/api/chat").respond(json=chat_reply("{}"))
    OllamaBackend(settings(ollama_keep_alive="-1")).complete(make_call())
    body = json.loads(chat.calls.last.request.content)
    assert body["keep_alive"] == -1
    assert "think" not in body


def test_ollama_capabilities(mock: respx.MockRouter) -> None:
    mock.post(f"{OLLAMA}/api/show").respond(
        json=show_payload(capabilities=["completion", "vision", "thinking"], ctx=8192)
    )
    caps = OllamaBackend(settings()).capabilities()
    assert caps.supports_images
    assert caps.supports_structured_output
    assert caps.supports_reasoning_control
    assert caps.max_context_tokens == 8192  # capped by the model's native context


def test_ollama_capabilities_without_native_context(mock: respx.MockRouter) -> None:
    mock.post(f"{OLLAMA}/api/show").respond(
        json=show_payload(capabilities=["completion"], ctx=None)
    )
    caps = OllamaBackend(settings()).capabilities()
    assert (caps.supports_images, caps.max_context_tokens) == (False, 16384)
    assert caps.supports_reasoning_control is False


@pytest.mark.parametrize(
    ("response", "error", "retryable"),
    [
        (httpx.Response(500, json={"error": "boom"}), ModelUnavailable, True),
        (httpx.Response(503, json={"error": "busy"}), ModelUnavailable, True),
        (httpx.Response(404, json={"error": "model not found"}), ModelUnavailable, False),
        (httpx.Response(400, json={"error": "bad format"}), ExternalServiceError, False),
        (httpx.Response(200, text="not json"), ExternalServiceError, False),
        (httpx.Response(200, json={"done": True}), ExternalServiceError, False),
    ],
)
def test_ollama_error_mapping(
    mock: respx.MockRouter, response: httpx.Response, error: type[Exception], retryable: bool
) -> None:
    mock.post(f"{OLLAMA}/api/show").respond(json=show_payload())
    mock.post(f"{OLLAMA}/api/chat").mock(return_value=response)
    with pytest.raises(error) as caught:
        OllamaBackend(settings()).complete(make_call())
    assert getattr(caught.value, "retryable", None) is retryable


def test_ollama_429_honours_retry_after(mock: respx.MockRouter) -> None:
    mock.post(f"{OLLAMA}/api/show").respond(json=show_payload())
    mock.post(f"{OLLAMA}/api/chat").respond(429, headers={"Retry-After": "12"}, json={})
    with pytest.raises(ModelUnavailable) as caught:
        OllamaBackend(settings()).complete(make_call())
    assert caught.value.retry_after == 12.0


@pytest.mark.parametrize(
    ("side_effect", "error"),
    [(httpx.ReadTimeout("slow"), ModelTimeout), (httpx.ConnectError("refused"), ModelUnavailable)],
)
def test_ollama_transport_errors(
    mock: respx.MockRouter, side_effect: Exception, error: type[Exception]
) -> None:
    mock.post(f"{OLLAMA}/api/show").mock(side_effect=side_effect)
    with pytest.raises(error):
        OllamaBackend(settings()).complete(make_call())


def test_ollama_health(mock: respx.MockRouter) -> None:
    tags = mock.get(f"{OLLAMA}/api/tags")
    tags.respond(json={"models": [{"name": "gemma4:e4b", "model": "gemma4:e4b"}]})
    assert OllamaBackend(settings()).health().ok

    tags.respond(json={"models": []})
    health = OllamaBackend(settings()).health()
    assert not health.ok
    assert "ollama pull gemma4:e4b" in health.message

    tags.mock(side_effect=httpx.ConnectError("refused"))
    health = OllamaBackend(settings()).health()
    assert not health.ok
    assert "not responding" in health.message


def test_ollama_untagged_model_matches_latest(mock: respx.MockRouter) -> None:
    mock.get(f"{OLLAMA}/api/tags").respond(json={"models": [{"name": "gemma4:latest"}]})
    assert OllamaBackend(settings(ollama_model="gemma4")).health().ok


def test_ollama_roles_and_required_settings() -> None:
    backend = OllamaBackend(settings())
    assert backend.model_for(ModelRole.PRIMARY) == "gemma4:e4b"
    with pytest.raises(ConfigError, match="OLLAMA_MODEL_SECONDARY"):
        backend.model_for(ModelRole.SECONDARY)
    assert (
        OllamaBackend(settings(ollama_model_secondary="small:1b")).model_for(ModelRole.SECONDARY)
        == "small:1b"
    )
    with pytest.raises(ConfigError, match="OLLAMA_MODEL"):
        OllamaBackend(settings(ollama_model=None))


def test_ollama_end_to_end_through_adapter(mock: respx.MockRouter) -> None:
    mock.post(f"{OLLAMA}/api/show").respond(json=show_payload())
    mock.post(f"{OLLAMA}/api/chat").respond(json=chat_reply('{"greeting":"hi","number":3}'))
    adapter = ModelAdapter(OllamaBackend(settings()))
    request = ModelRequest[Answer](
        prompt_id="t", prompt_version="1", parts=[TextPart(text="hi")], response_model=Answer
    )
    response = adapter.generate(request, allow_unaudited=True)
    assert response.parsed == Answer(greeting="hi", number=3)
    assert response.backend == "ollama"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("5", 5.0), ("0", 0.0), ("", None), (None, None), ("soon", None), ("-3", 0.0)],
)
def test_parse_retry_after(value: str | None, expected: float | None) -> None:
    assert parse_retry_after(value) == expected


def test_parse_retry_after_http_date() -> None:
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


# -- Gemini --------------------------------------------------------------------------------


def gemini_settings(**values: Any) -> Settings:
    base: dict[str, Any] = {
        "model_backend": "gemini_api",
        "gemini_api_key": "test-key-not-real",
        "gemma_model_primary": "gemma-4-26b-a4b-it",
        "model_timeout_s": 5,
    }
    base.update(values)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def gemini_reply(text: str) -> dict[str, Any]:
    return {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}
        ],
        "usageMetadata": {"promptTokenCount": 21, "candidatesTokenCount": 6},
    }


def test_gemini_request_shape_and_parsing(mock: respx.MockRouter) -> None:
    route = mock.post(url__regex=rf"{GEMINI}/.*/models/gemma-4-26b-a4b-it:generateContent").respond(
        json=gemini_reply('{"greeting":"hi","number":2}')
    )
    backend = GeminiBackend(gemini_settings())
    completion = backend.complete(
        make_call(
            model="gemma-4-26b-a4b-it",
            parts=(TextPart(text="look"), PNG),
            reasoning=Reasoning.ON,
            followups=(Turn("assistant", "bad"), Turn("user", "fix")),
        )
    )
    assert completion.text == '{"greeting":"hi","number":2}'
    assert (completion.input_tokens, completion.output_tokens) == (21, 6)

    body = json.loads(route.calls.last.request.content)
    user, model_turn, repair = body["contents"]
    assert user["parts"][0] == {"text": "look"}
    assert user["parts"][1]["inlineData"]["mimeType"] == "image/png"
    assert (model_turn["role"], repair["role"]) == ("model", "user")
    assert body["systemInstruction"]["parts"][0]["text"] == "system text"
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"] == Answer.model_json_schema()
    assert config["thinkingConfig"]["thinkingLevel"] == "HIGH"
    assert (config["temperature"], config["seed"], config["maxOutputTokens"]) == (0.0, 42, 256)
    assert route.calls.last.request.headers["x-goog-api-key"] == "test-key-not-real"


@pytest.mark.parametrize(
    ("status", "error", "retryable"),
    [
        (429, ModelUnavailable, True),
        (503, ModelUnavailable, True),
        (400, ExternalServiceError, False),
        (403, ExternalServiceError, False),
    ],
)
def test_gemini_error_mapping(
    mock: respx.MockRouter, status: int, error: type[Exception], retryable: bool
) -> None:
    body = {
        "error": {
            "code": status,
            "status": "X",
            "message": "m",
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "31s"}],
        }
    }
    mock.post(url__regex=r".*:generateContent").respond(status, json=body)
    with pytest.raises(error) as caught:
        GeminiBackend(gemini_settings()).complete(make_call(model="gemma-4-26b-a4b-it"))
    assert getattr(caught.value, "retryable", None) is retryable
    if status == 429:
        assert getattr(caught.value, "retry_after", None) == 31.0


def test_gemini_timeout(mock: respx.MockRouter) -> None:
    mock.post(url__regex=r".*:generateContent").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ModelTimeout):
        GeminiBackend(gemini_settings()).complete(make_call(model="gemma-4-26b-a4b-it"))


def test_gemini_health(mock: respx.MockRouter) -> None:
    route = mock.get(url__regex=r".*/models/gemma-4-26b-a4b-it$")
    route.respond(json={"name": "models/gemma-4-26b-a4b-it"})
    assert GeminiBackend(gemini_settings()).health().ok
    route.respond(404, json={"error": {"code": 404, "status": "NOT_FOUND", "message": "x"}})
    assert not GeminiBackend(gemini_settings()).health().ok


def test_gemini_requires_its_own_keys_only() -> None:
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        GeminiBackend(gemini_settings(gemini_api_key=None))
    backend = GeminiBackend(gemini_settings())
    with pytest.raises(ConfigError, match="GEMMA_MODEL_SECONDARY"):
        backend.model_for(ModelRole.SECONDARY)
    assert backend.capabilities().max_context_tokens is None


def test_gemini_extra_is_imported_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "google.genai", None)
    with pytest.raises(ConfigError, match="uv sync --extra gemini"):
        GeminiBackend(gemini_settings())


def test_retry_delay_search() -> None:
    assert retry_delay({"error": {"details": [{"retryDelay": "2.5s"}]}}) == 2.5
    assert retry_delay({"details": [{"retryDelay": "soon"}]}) is None
    assert retry_delay(None) is None


# -- factory -------------------------------------------------------------------------------


def test_factory_picks_backend_and_never_demands_gemini_keys_for_ollama() -> None:
    assert isinstance(get_driver(settings()), OllamaBackend)
    assert isinstance(get_driver(gemini_settings()), GeminiBackend)
    adapter = get_backend(settings(), use_cache=False)
    assert adapter.backend_name == "ollama"


def test_factory_vllm_not_implemented() -> None:
    with pytest.raises(ConfigError, match="vllm backend not implemented yet"):
        get_driver(settings(model_backend="vllm"))


def test_factory_builds_cache_only_with_redis_url() -> None:
    assert get_backend(settings(redis_url=None)).backend_name == "ollama"
    assert get_backend(settings(redis_url="redis://127.0.0.1:1/0")).backend_name == "ollama"
