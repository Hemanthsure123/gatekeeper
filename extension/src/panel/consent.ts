// Per-repository consent to review a PRIVATE repository with the hosted model.
// Shown only after a review was blocked for that reason. The maintainer must tick a box
// that states exactly what leaves the machine; the server audits the decision before it
// takes effect, and it can be revoked. Nothing is relaxed for other repositories.
import type { Api } from "./api";
import { ApiError } from "./api";
import { h } from "./dom";
import type { State } from "./store";

export interface ConsentDeps {
  api: () => Api | null;
  /** Called after consent is granted (re-analyse) or revoked (refresh). */
  onChanged: (granted: boolean) => void;
}

const BLOCKED = "private_repo_hosted_model_blocked";
let current: { key: string; el: HTMLElement } | null = null;

function grantControl(state: State, deps: ConsentDeps): HTMLElement {
  const ref = state.ref!;
  const repo = `${ref.owner}/${ref.name}`;
  const id = `consent-${ref.owner}-${ref.name}`;
  const box = h("input", { type: "checkbox", id });
  const allow = h("button", { type: "button", class: "primary", disabled: true }, "Allow and re-analyse");
  const error = h("p", { class: "error-line", role: "status" });
  box.addEventListener("change", () => {
    allow.disabled = !box.checked;
  });
  allow.addEventListener("click", async () => {
    const api = deps.api();
    if (!api || !box.checked) return;
    allow.disabled = true;
    allow.textContent = "Allowing…";
    error.textContent = "";
    try {
      await api.setHostedConsent(ref.owner, ref.name, true);
      deps.onChanged(true);
    } catch (err) {
      error.textContent = err instanceof ApiError ? err.message : "Could not save the choice.";
      allow.textContent = "Allow and re-analyse";
      allow.disabled = !box.checked;
    }
  });
  return h(
    "div",
    { class: "sheet", role: "group", "aria-label": "Allow hosted review for this private repository" },
    h("p", { class: "small" }, h("strong", null, "Allow hosted review for this private repository?")),
    h(
      "p",
      { class: "small muted" },
      `The model runs on Google's Gemini API. Reviewing ${repo} sends the title, description ` +
        "and diff of each pull request you analyse there. Author logins, mentions and e-mail " +
        "addresses are removed first; names inside code or comments are not.",
    ),
    h("label", { class: "check", for: id }, box, ` I accept this for ${repo} only`),
    h("div", { class: "row-actions" }, allow),
    h("p", { class: "muted small" }, "Recorded in the audit trail. You can revoke it at any time."),
    error,
  );
}

function revokeNote(state: State, deps: ConsentDeps): HTMLElement {
  const ref = state.ref!;
  const revoke = h("button", { type: "button", class: "link" }, "Revoke");
  revoke.addEventListener("click", async () => {
    const api = deps.api();
    if (!api) return;
    revoke.disabled = true;
    try {
      await api.setHostedConsent(ref.owner, ref.name, false);
      deps.onChanged(false);
    } catch {
      revoke.disabled = false;
    }
  });
  return h(
    "p",
    { class: "muted small consent-note" },
    "Hosted review is allowed for this private repository. ",
    revoke,
  );
}

/** The grant control after a private-repository block, or a revoke note once allowed. */
export function consentControl(state: State, deps: ConsentDeps): HTMLElement | null {
  const pr = state.pr;
  if (!pr || !state.ref || state.view !== "result") return null;
  const blocked =
    !pr.hosted_consent && pr.analysis.status === "failed" && pr.analysis.error_code === BLOCKED;
  if (!blocked && !pr.hosted_consent) return null;
  const key = `${pr.analysis.id}:${blocked ? "grant" : "revoke"}`;
  if (current?.key !== key) {
    current = { key, el: blocked ? grantControl(state, deps) : revokeNote(state, deps) };
  }
  return current.el;
}
