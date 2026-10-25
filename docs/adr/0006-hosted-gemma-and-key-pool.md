# ADR 0006: Run on hosted Gemma 4 through the Gemini API with a rotating key pool

- **Status:** Accepted
- **Date:** 2026-10-25
- **Builds on:** [ADR 0003](0003-model-adapter.md), [ADR 0005](0005-review-core.md)

## Context

The development machine has 15 GB RAM and no discrete GPU, so local Gemma 4 inference is impractically slow. Gemma 4 is available as a hosted model on the Gemini API (`gemma-4-26b-a4b-it`, `gemma-4-31b-it`). Free-tier rate limits are low, and Google applies them **per project, not per key**.

## Decisions

1. **The default runtime is `MODEL_BACKEND=gemini_api`.**
   - It uses `GEMMA_MODEL_PRIMARY=gemma-4-26b-a4b-it` and `GEMMA_MODEL_SECONDARY=gemma-4-31b-it`, as `make gemma-setup` confirmed from the models endpoint.
   - **The Ollama driver, its code and its tests stay.** Local mode remains supported, for example for private repositories.
2. **The key pool** (`gatekeeper.models.key_pool`):
   - `GEMINI_API_KEYS` is a comma-separated list of up to 20 keys. Keys are stripped and de-duplicated. A bad entry produces an error that names only its position.
   - The legacy `GEMINI_API_KEY` is used only when the list is empty.
   - Keys are referred to only as `key-1`, `key-2`, and so on. Their values never reach logs, errors, audit events, test output or the cache.
   - The Redactor masks every pool key, plus anything shaped like a Google API key (`AIza` followed by 35 URL-safe characters, the same pattern as gitleaks' `gcp-api-key` rule).
   - Strategies:
     - `round_robin` (the default) rotates from a random start per process;
     - `primary_first` always prefers the lowest-numbered healthy key.
   - **A 429 or RESOURCE_EXHAUSTED response:**
     - the key cools down for `Retry-After` or the `retryDelay` in the error body; otherwise for `GEMINI_KEY_COOLDOWN_BASE_S × 2^(n−1)`, up to `GEMINI_KEY_COOLDOWN_MAX_S`, with the count reset after a success;
     - the same request moves straight to the next healthy key without using an adapter retry, with failovers bounded by the pool size;
     - a key is never retried within its cooldown.
   - **A 401, 403 or 400 with API_KEY_INVALID response:** the key is dead for the rest of the process. When every key is dead, the error is `gemini_keys_invalid`, which is not retryable.
   - **Timeouts and 5xx:** the adapter's normal retries handle them, and the next attempt prefers another key.
   - **All keys cooling:** wait for the shortest cooldown if it is at most `GEMINI_MAX_WAIT_FOR_KEY_S`. Otherwise raise a retryable `ModelUnavailable`, so the queue backs off.
   - **Cache and audit:** the cache key does not depend on the API key, so a result cached through one key is a hit for all. `model_response` audit events record `backend_meta.key_alias` and `failovers`.
   - **Cooldown state is per process.** It is not shared through Redis; that is deferred.
   - **Caveat:** quotas are per Google Cloud project, so several keys from one project do not add capacity.
3. **Private repositories are guarded.**
   - With a hosted backend, a PR whose base repository is private, or whose visibility is unknown, becomes a failed analysis with code `private_repo_hosted_model_blocked`.
   - That holds unless `ALLOW_PRIVATE_REPOS_ON_HOSTED_MODEL=true`.
   - Code is never silently sent to a hosted model.
4. **Each backend uses its own context limit.**
   - Gemini reads `input_token_limit` from `models.get`; for `gemma-4-26b-a4b-it` that is 262,144. If the lookup fails it falls back to 131,072, the smallest documented Gemma 4 window.
   - Ollama keeps `OLLAMA_NUM_CTX`.
   - The `REVIEW_DIFF_TOKEN_BUDGET` default is unchanged.

## Consequences

- Positive: reviews take seconds rather than minutes on this machine. An invalid or rate-limited key degrades the service instead of breaking it.
- Negative: scrubbed PR content leaves the machine for Google's Gemini API. The README and CLAUDE.md state this, and private repositories are refused by default.
- Negative: per-process cooldowns mean two worker processes can both hit a cooling key. Redis sharing can be added later.

## Amendment (P06): per-repository consent

The global switch was the only way past the private-repository guard. Turning it on allows **every** private repository the token can read, which is too broad for reviewing one repository.

A maintainer can now allow a single private repository instead:

- **How:** the side panel shows the control after a review was blocked with `private_repo_hosted_model_blocked`. A checkbox states what is sent (title, description and diff, with the author's login, mentions and e-mails removed), and its label names the repository.
- **Storage:** the consent is kept in `repos.config.hosted_model_private_consent`, with the time and the backend.
- **API:** `POST` grants it with `confirm: true`; `DELETE /api/repos/{owner}/{name}/hosted-consent` revokes it.
- **Audit:** each change is an audited `action` event by the maintainer, recorded before the change takes effect (fail closed).
- **Effect:** the worker reads the consent at job time, so no restart is needed.
- **Unchanged:** the guard's default (refuse), the global switch, and Ollama as the private option. Code never grants consent on its own.
