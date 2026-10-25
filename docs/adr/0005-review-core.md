# ADR 0005: The review core: scrub at the boundary, deterministic signals, verified citations, heuristic confidence

- **Status:** Accepted
- **Date:** 2026-10-25
- **Builds on:** [ADR 0001](0001-thesis-and-non-goals.md), [ADR 0003](0003-model-adapter.md), [ADR 0004](0004-intake-queue-and-identity-boundary.md)

## Context

P04 replaces the stub steps with a real PR review. Local inference runs on a CPU, so each review gets exactly one model call. The review has to be contributor-blind, evidence-based and honest about uncertainty, and it must never accuse anyone.

## Decisions

### 1. Identity is scrubbed at the trusted boundary, with a leak guard behind it

- `gatekeeper.identity.scrub` runs in the `process_pr` handler, before content is stored or reviewed. The stored `items.content` and `content_hash` are the scrubbed version.
- In the title and body it replaces:
  - e-mail addresses → `[email]`;
  - `github.com/<login>` profile URLs → `github.com/[user]`;
  - `@org/team` → `@[team]`;
  - `@login` mentions → `@[user]`;
  - the author's login as a word → `[author]`.
- Patches lose only e-mails and the author's login. `@decorators` are code and stay.
- Paths, labels and check names also lose the login, so the leak guard never trips on a legitimate field.
- The handler passes an opaque `forbidden_terms` set (the login) into the pipeline. A request validator on the model adapter rejects any outgoing system or text part containing a term, as PolicyViolation, before the backend is called. The review core never learns where the terms came from.
- Known limits:
  - real names, nicknames, other handles and identity inside image URLs are not caught;
  - logins shorter than 3 characters are not replaced as plain words.

### 2. Signals are deterministic evidence, not judgements of people

- Ten small rules over identity-free inputs only: tiny_change, whitespace_only, docs_only, typo_scale, tests_touched, ci_status, linked_issue, description_present, lockfile_or_generated_only and truncated_input.
- Each gives a value, a direction, a severity and one plain sentence. All thresholds live in one module.
- `typo_scale` is neutral (changed in P05): small documentation fixes are legitimate work, so the signal only describes the size.
- Signals feed the prompt (as untrusted text), the confidence and the reply suggestions.

### 3. Citations are verifiable, and nothing is ever claimed without proof

- The diff is rendered with new-side line numbers (`L42| +code`). Only added and context lines are citable.
- A token budget keeps whole files in importance order. If the most important file cannot fit, there is no model call, and the review abstains with `too_large`.
- The verifier is pure code. It keeps an evidence item only if:
  - the path was shown;
  - the range is valid and at most 40 lines;
  - every cited line exists;
  - the whitespace-normalised quote appears in those lines.
- **No accusation without proof:** `likely_low_value` needs at least one verified `raises_concern` item or one high-severity negative signal. Otherwise it becomes `needs_human_look`.

### 4. Confidence v1 is a transparent heuristic, not yet calibrated

- `confidence = self_confidence × (0.5 + 0.5·citation_validity) × (0.6 + 0.4·signal_agreement) × completeness × evidence_factor`.
- It is monotone in every input. It is **not a probability**; later work fits it to labelled data.
- The review abstains (final verdict `needs_human_look`) when:
  - the input was truncated;
  - confidence is below `REVIEW_ABSTAIN_THRESHOLD` (0.6);
  - a `likely_low_value` verdict has confidence below `REVIEW_NEGATIVE_MIN_CONFIDENCE` (0.75).
- A model failure that persists after the repair attempt, or input too large for the context, also abstains, never "fails".

### 5. One model call; the reply is composed by code

- Prompt `review.pr` v1 puts the title, body, signal summary and diff in untrusted blocks. The system text forbids guessing who wrote the change, or whether a person or an AI wrote it.
- The reply draft is deterministic. It uses fixed summary wording ("Looks valuable", "Needs a human look", "Likely low value"), up to three verified evidence bullets and signal-derived suggestions.
- The model's contributor note is included only if it passes the banned-word filter (`BANNED_WORDS` in `review_core/reply.py`); otherwise a neutral line replaces it. Since P05 the filter scans only that model-authored note, not fixed wording, paths or diff quotes. `@mentions` are defused everywhere.
- A leak-guard trip fails the analysis with code `identity_leak_blocked` (non-retryable).
- The draft is stored, never posted.

## Consequences

- Positive: every stored claim is checkable against the diff, and identity cannot reach the model unnoticed.
- Positive: on weak models the safe failure mode is abstaining.
- Negative: the banned-word filter is lexical. It can drop a harmless note that mentions, say, a "bot" feature. That is acceptable, because a neutral fallback is always available.
- Negative: confidence is uncalibrated, so thresholds are policy choices, not error rates, until the eval prompt calibrates them.
