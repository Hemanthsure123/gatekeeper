// Fixed, neutral wording. Every status pairs an icon with text (never colour alone).
import type { FileState, Verdict } from "../shared/types";

export const VERDICT: Record<Verdict, { text: string; icon: string; tone: string }> = {
  looks_valuable: { text: "Looks valuable", icon: "✓", tone: "ok" },
  needs_human_look: { text: "Needs a human look", icon: "◎", tone: "neutral" },
  likely_low_value: { text: "Likely low value", icon: "▽", tone: "warn" },
};

export const STAGE: Record<FileState, { text: string; icon: string }> = {
  listed: { text: "listed", icon: "○" },
  signalled: { text: "signalled", icon: "◔" },
  in_context: { text: "in context", icon: "◑" },
  omitted: { text: "omitted", icon: "⊘" },
  cited: { text: "cited", icon: "◕" },
  dropped: { text: "dropped", icon: "✕" },
  verified: { text: "verified", icon: "✓" },
};

export const FILE_STATUS: Record<string, { text: string; glyph: string }> = {
  added: { text: "added", glyph: "+" },
  modified: { text: "modified", glyph: "•" },
  changed: { text: "changed", glyph: "•" },
  removed: { text: "removed", glyph: "−" },
  renamed: { text: "renamed", glyph: "→" },
  copied: { text: "copied", glyph: "⧉" },
};

export const EVENT_ICON: Record<string, string> = {
  run_started: "▶",
  step_started: "›",
  step_finished: "·",
  files_listed: "☰",
  file_signal: "◔",
  file_context: "◑",
  signals_summary: "Σ",
  model_call_started: "✦",
  model_call_finished: "✦",
  file_cited: "❝",
  citation_verified: "✓",
  citation_dropped: "✕",
  confidence_computed: "%",
  verdict: "◆",
  abstained: "◎",
  awaiting_approval: "✉",
  comment_posted: "✉",
  run_finished: "■",
  run_failed: "⚠",
};

export const ABSTAIN: Record<string, string> = {
  too_large: "The diff is too large to review in one model call.",
  input_too_large: "The input does not fit the model's context.",
  model_output_invalid: "The model's answer did not match the required schema.",
  truncated_input: "Part of the change could not be reviewed.",
  low_confidence: "Confidence is below the threshold, so a person should decide.",
  negative_needs_more_confidence: "A low-value verdict needs more confidence than this.",
  no_review: "No review was produced.",
};

export function abstainText(reason: string | null): string {
  return reason ? (ABSTAIN[reason] ?? reason.replaceAll("_", " ")) : "";
}

/** One plain line for an error code from a failed job or analysis. */
const ERRORS: Record<string, string> = {
  private_repo_hosted_model_blocked:
    "This repository is private, so its code is not sent to the hosted model unless you allow it for this repository (below) or use the local Ollama backend.",
  policy_violation: "This repository is not in GATEKEEPER_ALLOWED_REPOS.",
  not_found: "GitHub could not find this pull request with the configured token.",
  external_service_error:
    "GitHub or another service refused a request. See the [worker] lines in the make dev terminal.",
  gemini_keys_invalid: "Every Gemini API key was rejected. Run make gemma-setup.",
  model_unavailable: "The model is unavailable or rate-limited. Try again in a minute.",
  model_timeout: "The model call timed out. Try again.",
  identity_leak_blocked: "Blocked: the model input still contained the author's name.",
  job_timeout: "The review took longer than JOB_TIMEOUT_S.",
  lease_expired: "The worker stopped while running this review.",
};

export function errorText(code: string | null): string {
  if (!code) return "The review could not run.";
  return ERRORS[code] ?? `The review could not run (${code}).`;
}

export const DIRECTION_ICON: Record<string, string> = {
  positive: "+",
  negative: "−",
  neutral: "·",
};
