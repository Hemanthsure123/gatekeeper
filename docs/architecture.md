# Architecture

Status: **partly built**. As of P01 the infrastructure layer and the audit trail library exist. The other layers are planned. The guiding decisions are in [ADR 0001](adr/0001-thesis-and-non-goals.md) and [ADR 0002](adr/0002-database-identity-and-audit.md).

```mermaid
flowchart LR
    GH["GitHub<br/>PRs, issues, comments"] -->|webhooks / API| GW[Gateway]
    GW --> IR[Item router]
    IR --> RC[Review core]
    RC <-->|backend adapter only| LLM["Ollama<br/>Gemma 4"]
    RC --> DC[Decision chain]
    DC --> AE[Action executor]
    AE -->|policy-checked, safe actions| GH

    GW -.-> AT[(Audit trail)]
    IR -.-> AT
    RC -.-> AT
    DC -.-> AT
    AE -.-> AT

    subgraph INFRA[Infrastructure]
        PG[(PostgreSQL 16)]
        RD[(Redis 7)]
    end
    AT --- PG
    IR --- RD
```

## Layers

| Layer | Responsibility | Key constraints |
| --- | --- | --- |
| **Gateway** | Receives GitHub events (webhooks through a local relay, or API polling), verifies webhook signatures and normalizes payloads into internal items. | Rejects unsigned or invalid events. Treats all payload text as untrusted. |
| **Item router** | Classifies items (PR, issue, comment), dedupes redeliveries, and queues work via Redis. | Idempotent. Routing never depends on who the author is. |
| **Review core** | Gathers verifiable evidence (diff facts, CI results, linked issues, repo policy) and asks models focused questions. | **Contributor-blind**: identity fields are stripped before this layer. Every model call goes through the backend adapter, and its output is schema-validated. |
| **Decision chain** | Combines evidence into a typed decision with explicit confidence. "Uncertain" is a valid outcome. | Reputation may be shown as advisory context but never changes a score. |
| **Action executor** | Turns decisions into allowed actions (labels, comments, review requests) under a policy. | Dry-run by default. Auto-close is off by default and a first PR is never auto-closed. |
| **Audit trail** | An append-only event log of every pipeline step: inputs (by reference), model and prompt versions, outputs and actions. | Enough to replay any decision. Contains no secrets. |
| **Infrastructure** | Postgres (state and audit), Redis (queues and cache), Ollama (models), configuration and observability (OpenTelemetry). | Local only. Ports are bound to `127.0.0.1`. Secrets come from `.env`. |

## Cross-cutting rules

These are enforced in code and tests from the first feature prompt onward. The full list is in [CLAUDE.md](../CLAUDE.md).

- Untrusted text is data, never instructions.
- Typed schemas sit at every model boundary.
- Every step emits an audit event.
- No harmful automatic actions.

## Built in P01

| Module | What it provides |
| --- | --- |
| `gatekeeper.config` | Typed `Settings` for every `.env.example` key. Secrets are `SecretStr`. Components call `settings.require(...)` for what they need. |
| `gatekeeper.logging` | structlog over stdlib, JSON outside development, UTC timestamps, contextvars (`request_id`, `job_id`, `run_id`, `item_id`), and redaction before rendering. |
| `gatekeeper.errors` | `GatekeeperError` with stable codes. Only `to_public_dict()` may leave the process. |
| `gatekeeper.redaction` | The `Redactor` shared by logging and audit: secret values, GitHub tokens, bearer tokens, private keys and emails. |
| `gatekeeper.db` | SQLAlchemy 2.1 models and session helpers. Alembic revision `0001` lives in `backend/alembic`. |
| `gatekeeper.audit` | `AuditRecorder` / `Run` (append-only, hash-chained, fail-closed) and `AuditReader` (`get_events`, `verify_chain`). |

### Schema (revision 0001)

```mermaid
erDiagram
    repos ||--o{ items : has
    items ||--o| item_authors : "identity (never read by review core)"
    items ||--o{ analyses : has
    items ||--o{ events : "audited by"
    repos { uuid id bigint github_repo_id text owner text name jsonb config }
    items { uuid id text kind text external_id jsonb content text content_hash text state }
    item_authors { uuid item_id text login bigint github_user_id bool is_first_contribution }
    analyses { uuid id uuid run_id text status jsonb result float confidence }
    events { bigint id uuid run_id int seq text type text actor jsonb payload text chain_hash }
```

- **`items.content` is identity-free.** Identity lives only in `item_authors`, which the review core must never read.
- **`events` is append-only.** A trigger rejects UPDATE, DELETE and TRUNCATE. Each run's events form a SHA-256 chain over redacted payloads. Writing fails closed: if an event cannot be recorded, the step stops.
