# ADR 0002: Sync SQLAlchemy and Alembic, identity in its own table, fail-closed hash-chained audit

- **Status:** Accepted
- **Date:** 2026-10-25
- **Builds on:** [ADR 0001](0001-thesis-and-non-goals.md)

## Context

P01 adds the persistence layer every later feature depends on. It has to make three principles from ADR 0001 structural rather than conventional:

- contributor-blind scoring;
- replayable decisions;
- no action without a record.

## Decisions

### 1. Sync SQLAlchemy 2.1 + psycopg 3 + Alembic

- **Typed declarative models** (`Mapped[...]`) with sync `Session`s. Work is queue- and batch-shaped, and the model calls dominate latency. Sync code is simpler to test, debug and type-check. Async can be added later at the edges if a prompt needs it.
- **psycopg 3** (`postgresql+psycopg`). SQLAlchemy 2.1 makes it the default PostgreSQL driver, and `sqlalchemy_url()` forces it explicitly.
- **Alembic owns the schema**, in `backend/alembic`, configured from `[tool.alembic]` in `pyproject.toml`. Revisions are hand-reviewed and frozen: they copy constants rather than importing app code. An integration test asserts the migrated schema matches the models (`compare_metadata`) and that downgrade/upgrade round-trips.
- **Constraint naming convention** (`pk_`, `fk_`, `uq_`, `ix_`, `ck_`), so every constraint has a predictable name that migrations can reference.
- **UTC timestamps:**
  - all columns are `timestamptz`;
  - connections set `timezone=UTC`;
  - Python code uses `gatekeeper.db.utcnow()`, never naive datetimes.
- **Enumerations are `text` + `CHECK`**, not PostgreSQL ENUM types. Adding a value is then a one-line constraint change. SQLAlchemy 2.1 also changed how named types relate to metadata.
- **Engines use `hide_parameters=True`** so bound values (payloads, content) never appear in exception messages or logs.

### 2. Identity is stored only in `item_authors`

- `items.content` must never contain identity fields. Two layers enforce this:
  - an ORM validator rejects identity keys at any depth (`author`, `user`, `login`, `email`, `avatar_url`, and so on; see `IDENTITY_KEYS`);
  - a database `CHECK` rejects them at the top level of the JSON.
- `item_authors` (login, user id, account age, association, first-contribution flag) is the **only** place identity lives. It is keyed 1:1 by `item_id` and cascades with the item.
- **The review core must never read `item_authors`.** A later prompt enforces this structurally, for example with a separate repository/session for the review core and a test that fails if review code imports or queries it. Reputation may be shown to humans as advisory context only (rule b).

### 3. The audit trail is append-only, hash-chained and fails closed

- `events` is append-only. A trigger rejects `UPDATE`, `DELETE` and `TRUNCATE`, and the migration's downgrade removes it. **Any future retention job needs an explicit, audited path**: its own migration and process, recorded and reviewed. It must never just disable the trigger.
- Before hashing and storage, the `Redactor` masks:
  - GitHub token formats;
  - bearer tokens;
  - private key blocks;
  - email addresses;
  - exact secret values from Settings.
- `payload_hash` is the SHA-256 of the canonical JSON of the **redacted** payload. Canonical means sorted keys, no whitespace, UTF-8 and no NaN.
- `chain_hash` is the SHA-256 of the canonical JSON object with keys `actor`, `created_at`, `item_id`, `parent_seq`, `payload_hash`, `previous` (the previous chain hash, `""` for seq 1), `run_id`, `schema_version`, `seq` and `type`. Absent values are `null`. This forms a per-run chain, and `AuditReader.verify_chain(run_id)` reports the first bad `seq`.
- `created_at` is assigned by the recorder, not by the database. It is a UTC timestamp with microsecond precision (which Postgres `timestamptz` stores exactly) and is hashed in the fixed form `YYYY-MM-DDTHH:MM:SS.ffffffZ`, so the hashed value is exactly the stored value.
- **Format change (P02, 2026-10-25):** the first version chained only `previous, run_id, seq, type, payload_hash`, so `actor`, `parent_seq`, `item_id`, `schema_version` and `created_at` could be altered undetected by anyone able to bypass the trigger. The format was changed in place, without a new `schema_version`, because no released data exists. Development databases created before this change fail `verify_chain` and must be reset with `make db-reset`.
- **Fail closed.** `Run.record` writes each event in its own committed transaction. On any redaction, serialization or write failure it raises `AuditError`, and the run then refuses all further writes. Callers must stop the step: a decision that was not recorded must not happen.
- **One writer per run.** `seq` and the previous hash are tracked in memory. The unique `(run_id, seq)` constraint is the safety net against a second writer.

## Consequences

- Positive: blindness, replayability and "no unrecorded action" are enforced by the schema and library, not by convention.
- Positive: tampering that bypasses the trigger (for example, a superuser) is detected by chain verification.
- Negative: truncation of a run's tail isn't detectable from the chain alone; `run_end` events help.
- Negative: items or repos with audit history can't be deleted, because the events foreign key is `RESTRICT` and the trigger blocks deletes. This is intended.
- Negative: a write per event costs one transaction. That is acceptable at triage volumes, and it gives the durability that "fail closed" requires.
