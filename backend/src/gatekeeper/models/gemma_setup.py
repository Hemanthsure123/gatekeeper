"""``make gemma-setup``: check every Gemini API key (by alias only), find the Gemma 4
model ids the API actually offers, and point the non-secret settings at them.

Never prints, edits or removes a key. Model ids come from the models endpoint, not
from memory: the docs list ``gemma-4-26b-a4b-it`` and ``gemma-4-31b-it``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx

from gatekeeper.action_needed import action_needed
from gatekeeper.config import Settings, get_settings
from gatekeeper.envfile import set_values
from gatekeeper.errors import GatekeeperError

PRIMARY_HINT: Final = "26b-a4b"
SECONDARY_HINT: Final = "31b"
QUOTA_REMINDER: Final = (
    "Quotas are usually per Google Cloud project, so keys from the same project share one quota."
)


@dataclass
class KeyCheck:
    alias: str
    status: str  # valid | invalid | quota-limited | error
    detail: str = ""
    models: list[str] = field(default_factory=list)


def classify_error(exc: Exception) -> tuple[str, str]:
    code = int(getattr(exc, "code", 0) or 0)
    status = str(getattr(exc, "status", "") or "")
    if code == 429 or status == "RESOURCE_EXHAUSTED":
        return "quota-limited", f"{code} {status}".strip()
    if code in (401, 403) or (code == 400 and "API key" in str(getattr(exc, "message", ""))):
        return "invalid", f"{code} {status}".strip()
    if isinstance(exc, httpx.TransportError):
        return "error", type(exc).__name__
    return "error", f"{code} {status}".strip() or type(exc).__name__


def check_keys(keys: Sequence[str], client_factory: Callable[[str], Any]) -> list[KeyCheck]:
    from google.genai import errors

    results: list[KeyCheck] = []
    for index, key in enumerate(keys, start=1):
        alias = f"key-{index}"
        client = client_factory(key)  # keep a reference: the SDK closes its HTTP client on GC
        try:
            names = [str(m.name).removeprefix("models/") for m in client.models.list()]
        except (errors.APIError, httpx.HTTPError) as exc:
            status, detail = classify_error(exc)
            results.append(KeyCheck(alias, status, detail))
            continue
        results.append(KeyCheck(alias, "valid", models=names))
    return results


def pick_models(names: Sequence[str]) -> tuple[str | None, str | None, list[str]]:
    gemma4 = sorted(n for n in names if n.startswith("gemma-4"))
    primary = next((n for n in gemma4 if PRIMARY_HINT in n), None)
    secondary = next((n for n in gemma4 if SECONDARY_HINT in n), None)
    return primary, secondary, gemma4


def run(settings: Settings, client_factory: Callable[[str], Any], env_path: Path) -> int:
    keys = [k.get_secret_value() for k in settings.gemini_keys()]
    if not keys:
        print(
            action_needed(
                "Add at least one Gemini API key",
                "The Gemma API backend needs a key; none is configured.",
                [
                    "Open https://aistudio.google.com/apikey and create an API key.",
                    "Open .env and put it after GEMINI_API_KEYS= (several keys: comma-separated).",
                    "Run make gemma-setup again.",
                ],
                "GEMINI_API_KEYS",
            )
        )
        return 2

    results = check_keys(keys, client_factory)
    print(f"Key pool: {len(results)} key(s), strategy {settings.gemini_key_strategy}")
    print(f"{'alias':<8} {'status':<14} detail")
    for r in results:
        detail = r.detail or (f"{len(r.models)} models visible" if r.status == "valid" else "")
        print(f"{r.alias:<8} {r.status:<14} {detail}")
    print(QUOTA_REMINDER)

    failed = [r.alias for r in results if r.status != "valid"]
    if failed:
        print(f"Not usable: {', '.join(failed)} (nothing was removed; fix them in .env yourself).")
    valid = [r for r in results if r.status == "valid"]
    if not valid:
        limited = all(r.status == "quota-limited" for r in results)
        print(
            action_needed(
                "Provide a working Gemini API key" if not limited else "Wait for the Gemini quota",
                "No key in the pool could list models.",
                [
                    "Check the key at https://aistudio.google.com/apikey (or create a new one).",
                    "Replace the failing entries after GEMINI_API_KEYS= in .env.",
                    "Run make gemma-setup again.",
                ]
                if not limited
                else [
                    "Wait until the quota resets, or add a key from a different project.",
                    "Run make gemma-setup again.",
                ],
                "GEMINI_API_KEYS",
            )
        )
        return 1

    primary, secondary, gemma4 = pick_models(valid[0].models)
    print(f"Gemma 4 models visible via {valid[0].alias}: {', '.join(gemma4) or 'none'}")
    updates = [("MODEL_BACKEND", "gemini_api")]
    if primary:
        updates.append(("GEMMA_MODEL_PRIMARY", primary))
    if secondary:
        updates.append(("GEMMA_MODEL_SECONDARY", secondary))
    if not primary and gemma4:
        updates.append(("GEMMA_MODEL_PRIMARY", gemma4[0]))
        print(f"Expected a *{PRIMARY_HINT}* model; using {gemma4[0]} as primary instead.")
    if not secondary:
        print(f"No *{SECONDARY_HINT}* Gemma 4 model listed; GEMMA_MODEL_SECONDARY left unchanged.")
    if not gemma4:
        print("No Gemma 4 model is listed for this key; only MODEL_BACKEND was set.")
    try:
        for key in set_values(updates, env_path):
            print(f"set {key}")
    except GatekeeperError as exc:
        print(f"Could not update .env: {exc.message}")
        return 1
    return 0 if primary else 1


def default_client_factory(settings: Settings) -> Callable[[str], Any]:
    from google import genai
    from google.genai import types

    timeout = int(settings.model_timeout_s * 1000)
    return lambda key: genai.Client(api_key=key, http_options=types.HttpOptions(timeout=timeout))


def main() -> int:
    try:
        settings = get_settings()
    except GatekeeperError as exc:
        print(f"Configuration problem: {exc.message}")
        return 2
    return run(settings, default_client_factory(settings), Path(".env"))


if __name__ == "__main__":
    sys.exit(main())
