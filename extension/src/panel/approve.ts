// "Approve and post comment": the panel's only write. A confirm sheet shows the exact,
// editable text; posting needs the "I reviewed this" box ticked. The element persists
// per analysis so typing is never lost to a re-render.
import type { Api } from "./api";
import { ApiError } from "./api";
import { h } from "./dom";
import type { State } from "./store";

export interface ApprovalDeps {
  api: () => Api | null;
  /** Reload the pull request after a successful post. */
  onPosted: () => void;
}

const FOOTER =
  "Reviewed by Gatekeeper using an open-weight model. A maintainer approved this comment.";
/** The server default for COMMENT_MAX_CHARS; the server enforces the real limit. */
const DEFAULT_MAX = 3000;

let current: { id: string; el: HTMLElement } | null = null;

function posted(url: string): HTMLElement {
  return h(
    "p",
    { class: "posted" },
    h("span", { "aria-hidden": "true" }, "✓ "),
    "Posted. ",
    h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, "Open the comment"),
  );
}

function build(state: State, deps: ApprovalDeps): HTMLElement {
  const analysis = state.pr!.analysis;
  const wrap = h("div", { class: "approve" });
  const error = h("p", { class: "error-line", role: "status" });
  const textarea = h("textarea", {
    rows: 8,
    "aria-label": "Comment text (you can edit it)",
  });
  textarea.value = analysis.reply_draft ?? "";
  const counter = h("span", { class: "muted small" });
  const reviewed = h("input", { type: "checkbox", id: `reviewed-${analysis.id}` });
  const post = h("button", { type: "button", class: "primary", disabled: true }, "Post comment");
  const cancel = h("button", { type: "button" }, "Cancel");
  const sheet = h(
    "div",
    { class: "sheet", role: "group", "aria-label": "Confirm the comment", hidden: true },
    h("p", { class: "small" }, "This exact text will be posted to the pull request. Edit it if needed."),
    textarea,
    h("p", { class: "muted small" }, counter, ` · A footer is added: “${FOOTER}”`),
    h("label", { class: "check", for: `reviewed-${analysis.id}` }, reviewed, " I reviewed this"),
    h("div", { class: "row-actions" }, post, cancel),
  );
  const open = h("button", { type: "button" }, "Approve and post comment");

  const refresh = (): void => {
    counter.textContent = `${textarea.value.length} characters (edited text: up to ${DEFAULT_MAX} by default)`;
    post.disabled = !reviewed.checked || !textarea.value.trim();
  };
  textarea.addEventListener("input", refresh);
  reviewed.addEventListener("change", refresh);
  open.addEventListener("click", () => {
    sheet.hidden = false;
    open.hidden = true;
    textarea.focus();
    refresh();
  });
  cancel.addEventListener("click", () => {
    sheet.hidden = true;
    open.hidden = false;
    error.textContent = "";
    open.focus();
  });
  post.addEventListener("click", async () => {
    const api = deps.api();
    if (!api || !reviewed.checked) return;
    post.disabled = true;
    post.textContent = "Posting…";
    error.textContent = "";
    const original = (analysis.reply_draft ?? "").trim();
    const text = textarea.value.trim() === original ? null : textarea.value;
    try {
      const result = await api.comment(analysis.id, text);
      wrap.replaceChildren(posted(result.html_url));
      deps.onPosted();
    } catch (err) {
      error.textContent =
        err instanceof ApiError
          ? err.code === "github_write_forbidden"
            ? "GitHub refused: the token needs Issues (write) or Pull requests (write) on this repository."
            : err.message
          : "Posting failed.";
      post.textContent = "Post comment";
      refresh();
    }
  });
  wrap.append(open, sheet, error);
  return wrap;
}

export function approvalControl(state: State, deps: ApprovalDeps): HTMLElement | null {
  const analysis = state.pr?.analysis;
  if (!analysis || state.view !== "result") return null;
  if (analysis.comment_posted) return posted(analysis.comment_posted.html_url);
  if (!analysis.reply_draft || !["completed", "abstained"].includes(analysis.status)) return null;
  if (current?.id !== analysis.id) current = { id: analysis.id, el: build(state, deps) };
  return current.el;
}
