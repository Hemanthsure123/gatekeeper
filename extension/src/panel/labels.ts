// Fixed, neutral wording. Every status pairs an icon with text (never colour alone).
// Type-only imports, so the wording test in extension/tests can load this module.
import type { Gate, GateFilter, StepName, StepStatus } from "./derive";
import type { FileState, Verdict } from "../shared/types";

export const VERDICT: Record<Verdict, { text: string; icon: string; tone: string }> = {
  looks_valuable: { text: "Looks valuable", icon: "✓", tone: "ok" },
  needs_human_look: { text: "Needs a human look", icon: "◎", tone: "neutral" },
  likely_low_value: { text: "Likely low value", icon: "▽", tone: "warn" },
};

/** Per-file stage: a short chip for tree rows and a sentence for the file details. */
export const STAGE: Record<FileState, { text: string; icon: string; hint: string }> = {
  listed: { text: "Listed", icon: "○", hint: "Listed in the change; not checked yet." },
  signalled: { text: "Checked", icon: "◔", hint: "File-level rules ran on it." },
  in_context: { text: "Read", icon: "◑", hint: "Its diff was part of the model's input." },
  omitted: { text: "Left out", icon: "⊘", hint: "Left out of the model's input, so nothing in it could be cited." },
  cited: { text: "Cited", icon: "◕", hint: "The model cited lines in it; the check has not run yet." },
  dropped: { text: "Dropped", icon: "✕", hint: "A citation did not match the diff, so it was dropped." },
  verified: { text: "Verified", icon: "✓", hint: "At least one citation matched the diff exactly." },
};

/** The five gates, as labelled in the file details and the pipeline view. */
export const GATE_TEXT: Record<Gate, string> = {
  listed: "Listed",
  checked: "Checked",
  read: "Read",
  cited: "Cited",
  verified: "Verified",
};

/** Completes "Showing N files …" above the tree when a gate filter is on. */
export const GATE_FILTER_TEXT: Record<GateFilter, string> = {
  listed: "listed in the change",
  checked: "checked by file-level rules",
  read: "read by the model",
  left_out: "left out of the model's input",
  cited: "cited by the model",
  verified: "with a verified citation",
  dropped: "with a dropped citation",
};

/** Where the model runs. API mode sends the scrubbed change to Google; never say local. */
export const BACKEND: Record<string, string> = {
  ollama: "Local model (Ollama)",
  gemini_api: "Hosted model (Gemini API)",
};

/** Mirrors gatekeeper.pipeline.core.STEP_LABELS. */
export const STEP_TEXT: Record<StepName, string> = {
  intake: "Reading the change",
  signals: "Computing signals",
  review: "Asking the model",
  verify: "Checking every citation",
  decision: "Deciding",
};

export const STEP_STATUS: Record<StepStatus, { text: string; icon: string }> = {
  pending: { text: "Waiting", icon: "○" },
  running: { text: "Running", icon: "◌" },
  done: { text: "Done", icon: "✓" },
  failed: { text: "Failed", icon: "⚠" },
  skipped: { text: "Skipped", icon: "–" },
};

export const FILE_STATUS: Record<string, { text: string; glyph: string }> = {
  added: { text: "Added", glyph: "+" },
  modified: { text: "Modified", glyph: "•" },
  changed: { text: "Changed", glyph: "•" },
  removed: { text: "Removed", glyph: "−" },
  renamed: { text: "Renamed", glyph: "→" },
  copied: { text: "Copied", glyph: "⧉" },
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

export const DOWNGRADE: Record<string, string> = {
  no_verified_concern:
    "No verified concern or high-severity signal supports a low-value verdict, so a person should decide.",
};

export function downgradeText(reason: string | null): string {
  return reason ? (DOWNGRADE[reason] ?? reason.replaceAll("_", " ")) : "";
}

/** Whether the pull request description matches the change, as judged by the model. */
export const CLAIM_MATCH: Record<string, string> = {
  matches: "Matches the change",
  partial: "Partly matches the change",
  mismatch: "Does not match the change",
  unclear: "Unclear",
};

export const SEVERITY: Record<string, string> = { low: "Low", medium: "Medium", high: "High" };

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

export const DIRECTION_TEXT: Record<string, string> = {
  positive: "Positive",
  negative: "Negative",
  neutral: "Neutral",
};
