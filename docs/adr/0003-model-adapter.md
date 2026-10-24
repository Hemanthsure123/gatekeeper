# ADR 0003: One model adapter, native structured output plus validation, fail-open cache, audit inside the adapter, explicit context size

- **Status:** Accepted
- **Date:** 2026-10-25
- **Builds on:** [ADR 0001](0001-thesis-and-non-goals.md), [ADR 0002](0002-database-identity-and-audit.md)

## Context

Every later feature asks a model questions. If each feature called Ollama or the Gemini API directly, each would re-implement retries, validation, caching and audit. Each would also be one forgotten line away from:

- an unrecorded model call (breaking rule f);
- loose, unvalidated text reaching a decision (breaking rule d);
- a silently truncated diff, if it relied on Ollama's small default context.

## Decisions

### 1. A single adapter over thin drivers

- `ModelAdapter` (from `gatekeeper.models.get_backend()`) is the only way to call a model.
- It wraps a `BackendDriver`, which only translates one call to a wire format:
  - `OllamaBackend` uses httpx against the REST API.
  - `GeminiBackend` uses google-genai, as an optional extra that is imported lazily.
  - `FakeBackend` is for tests.
- MODEL_BACKEND picks the driver. `vllm` raises "vllm backend not implemented yet".
- Every backend gets the same behaviour, implemented once:
  - request validators (a later prompt adds the identity-leak guard);
  - a concurrency limit;
  - retries with exponential backoff, jitter and Retry-After;
  - the size checks;
  - structured output with one repair attempt;
  - the cache, audit and logging.
- Each driver calls `settings.require()` only for its own keys, so Ollama mode never needs a Gemini key.

### 2. Native structured output, always validated

- The JSON schema comes from the request's Pydantic `response_model`. It is sent through the backend's native feature:
  - Ollama `format`;
  - Gemini `response_json_schema` with `response_mime_type=application/json`.
- The schema is also stated in the system text, as the Ollama docs recommend.
- The result is **always** validated with `model_validate_json`. On failure there is exactly one repair turn, which returns the concise validation error to the model. Then `ModelOutputInvalid` is raised.
- There is no regex or free-text fallback: invalid output is an error, not a guess.

### 3. Explicit context size; refuse rather than truncate

- Ollama's default context is 4096 tokens on machines with < 24 GiB VRAM (32k at 24–48 GiB, 256k at ≥ 48 GiB, per the Ollama context-length docs). Longer prompts are silently cut.
- The adapter therefore always sends `num_ctx = OLLAMA_NUM_CTX`. The default is **16384**, capped by the model's native `context_length` from `/api/show` (128K for `gemma4:e4b`). It fits typical PR diffs while keeping the KV cache affordable on CPU-only or low-VRAM machines. This dev machine has 15 GB RAM and only integrated graphics.
- **Before sending**, the input is estimated at 3 UTF-8 bytes per token, plus 1024 per image and 64 overhead. If it exceeds `context − max_output_tokens`, `InputTooLarge` is raised.
- **After the call**, if the backend reports prompt tokens at or above that budget, the input may have been truncated. The adapter raises `InputTooLarge` instead of returning.
- The Gemini API rejects oversize requests itself, so its limit is left unknown and the pre-check is skipped.

### 4. Redis cache that fails open

- The key is the SHA-256 over:
  - backend, model, system text and text parts;
  - image hashes;
  - the JSON schema;
  - temperature, seed and reasoning;
  - prompt_id and prompt_version.
- Only validated responses are stored, with TTL `MODEL_CACHE_TTL_S`.
- Policies:
  - `use` reads and writes;
  - `refresh` writes only;
  - `bypass` does neither.
- Any Redis error logs a warning, and the call proceeds uncached. Failures are never cached. With no REDIS_URL, caching is off.

### 5. The adapter writes its own audit events and fails closed

- With a `run`, the adapter records `model_request` before the call. It holds the prompt id and version, model, backend, settings, and the redacted system and text content. Images appear only as SHA-256, MIME type and size.
- After the call it records `model_response`, holding the parsed result, usage, latency, cache_hit and attempts.
- A failed call records an `error` event.
- If an audit write fails, the call fails, and nothing is cached or returned.
- Calls without a run need an explicit `allow_unaudited=True`, which is logged as a warning. Only tools like `make model-check` use it.

### 6. Concurrency is per process for now

- `MODEL_MAX_CONCURRENCY` sizes a per-process semaphore, held only during the backend call and not during backoff sleeps.
- **A global limit across worker processes comes later**, for example a Redis-based semaphore when the worker prompt lands.

### 7. Logging never contains content

Logs carry backend, model, prompt_id, latency, token counts, cache_hit and attempts. They never carry prompt or response text.

## Consequences

- Positive: features get typed objects or a typed error, never loose text. Every call is replayable from the audit trail.
- Negative: the token estimate is conservative and may refuse inputs that would just fit. That is preferable to silent truncation, and `OLLAMA_NUM_CTX` can be raised.
- Negative: audit stores image hashes, not bytes. **True replay of screenshot cases needs a blob store**, which is deferred to the screenshot feature.
- Not confirmed from Google's Gemma docs: native structured output for Gemma models on the Gemini API. It is requested anyway and always validated.
