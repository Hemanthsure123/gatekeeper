# ADR 0001: Thesis, non-goals and contributor-blind scoring

- **Status:** Accepted
- **Date:** 2026-10-25
- **Deciders:** project maintainers (`<YOUR NAME OR ORG>`)

## Context

Maintainers face a growing volume of pull requests, issues and comments. Many triage tools and heuristics lean on who the contributor is, such as account age, follower counts or prior activity, or on surface style. Those signals are unfair to newcomers and easy to game, and they say little about whether the work is good. Language models can help assess the work itself. But they also hallucinate, they can be steered by injected text, and when wired to automatic actions they can do real harm.

## Decision

Gatekeeper is built on this thesis:

1. **Judge the work, not the contributor.**
2. **Show verified evidence.** Every judgement cites facts that can be checked, such as diff hunks, CI results or linked issues.
3. **Admit uncertainty.** A calibrated "uncertain" beats a confident wrong answer.
4. **Never take harmful automatic actions.** Destructive actions are off by default and policy-gated, and a first PR is never auto-closed.
5. **Record every decision so it is replayable.** An append-only audit trail captures each step.

The scoring path is **contributor-blind**. Author name, account age, avatar, follower counts, organization membership and similar identity data are removed before the review core. They never reach a model prompt or the score. Reputation-like signals may be shown to a human as advisory context only, and they never change a score or trigger an action by themselves.

The tool runs locally with open models served by Ollama (Gemma 4). Hosted Gemma through an API key is an optional alternative.

## Non-goals

- **Not a general agent-tracing or observability product.** Gatekeeper records its own decisions for replay. It does not trace arbitrary agents.
- **No AI-authorship detection.** We don't try to guess whether a human or a model wrote a contribution. The work is judged on its merits either way.
- **No style-based scoring.** Formatting, prose style, language fluency and similar surface traits don't affect scores. Linters already cover style.

## Consequences

- Positive: newcomers and established contributors get equal treatment, and identity-based gaming becomes pointless.
- Positive: evidence, uncertainty and the audit trail make decisions explainable and reviewable.
- Negative: signals that might sometimes help (for example, a known spam account) can't be used for scoring. Spam handling needs work-based evidence or an explicit, separately audited policy.
- Engineering: blindness must be enforced structurally, by stripping identity fields at a boundary, and proven with tests in which identical work by different authors scores identically.
- Any future change to these principles needs a new ADR that supersedes this one.
