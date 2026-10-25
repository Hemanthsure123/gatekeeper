// Activity tab views: the verdict card, the "Now" line and the timeline.
import type { ActivityEvent, PrFile } from "../shared/types";
import { formatTime, h, span } from "./dom";
import { abstainText, DIRECTION_ICON, EVENT_ICON, FILE_STATUS, STAGE, VERDICT } from "./labels";
import type { State } from "./store";

const TIMELINE_MAX = 300;

export interface VerdictHandlers {
  /** Renders the approve-and-post control under the reply draft (Part 6). */
  approval?: (state: State) => HTMLElement | null;
}

export function renderVerdict(root: HTMLElement, state: State, handlers: VerdictHandlers): void {
  root.replaceChildren();
  const analysis = state.pr?.analysis;
  if (state.view === "not_analysed") {
    root.append(
      h(
        "div",
        { class: "card" },
        h("p", { class: "card-title" }, "Not analysed yet"),
        h("p", { class: "muted" }, "Analyse this pull request to see a verdict with verified evidence."),
      ),
    );
    return;
  }
  if (state.view === "analysing" || !analysis) {
    root.append(
      h(
        "div",
        { class: "card", "aria-busy": "true" },
        h("p", { class: "card-title" }, h("span", { class: "spinner", "aria-hidden": "true" }), " Analysing"),
        h("p", { class: "muted" }, state.line || "Following the run live."),
      ),
    );
    return;
  }
  if (analysis.status === "failed") {
    root.append(
      h(
        "div",
        { class: "card tone-bad" },
        h("p", { class: "card-title" }, h("span", { "aria-hidden": "true" }, "⚠ "), "The review could not finish"),
        h("p", { class: "muted" }, `Error: ${analysis.error_code ?? "unknown"}. Try Re-analyse.`),
      ),
    );
    return;
  }
  const verdict = VERDICT[analysis.verdict ?? "needs_human_look"];
  const confidence = analysis.confidence;
  const percent = confidence === null ? null : Math.round(confidence * 100);
  const card = h(
    "div",
    { class: `card tone-${verdict.tone}` },
    h(
      "p",
      { class: "verdict" },
      h("span", { class: "verdict-icon", "aria-hidden": "true" }, verdict.icon),
      h("span", null, verdict.text),
      analysis.status === "abstained" ? h("span", { class: "pill" }, "abstained") : null,
    ),
    h(
      "div",
      { class: "confidence" },
      h("span", { class: "muted" }, "Confidence"),
      h(
        "div",
        {
          class: "bar",
          role: "meter",
          "aria-label": "Confidence",
          "aria-valuemin": "0",
          "aria-valuemax": "100",
          "aria-valuenow": percent === null ? undefined : String(percent),
          "aria-valuetext": percent === null ? "not computed" : `${percent} percent`,
        },
        h("span", { style: `width:${percent ?? 0}%` }),
      ),
      h("span", { class: "num" }, percent === null ? "–" : `${percent}%`),
    ),
  );
  if (analysis.status === "abstained" && analysis.abstain_reason) {
    card.append(h("p", { class: "note" }, h("span", { "aria-hidden": "true" }, "◎ "), abstainText(analysis.abstain_reason)));
  }
  if (analysis.rationale) {
    card.append(h("p", { class: "rationale", title: analysis.rationale }, analysis.rationale));
  }
  const evidence = state.pr?.files.reduce((n, f) => n + f.evidence.length, 0) ?? 0;
  card.append(
    h(
      "p",
      { class: "muted small" },
      `${evidence} verified citation(s)` +
        (analysis.dropped_evidence_count ? `, ${analysis.dropped_evidence_count} dropped` : "") +
        (analysis.model ? ` · ${analysis.model}` : "") +
        (analysis.cache_hit ? " · cache hit" : ""),
    ),
  );
  if (analysis.reply_draft) {
    card.append(
      h(
        "details",
        { class: "draft" },
        h("summary", null, "Reply draft (not posted)"),
        h("pre", null, analysis.reply_draft),
      ),
    );
  }
  const approval = handlers.approval?.(state);
  if (approval) card.append(approval);
  root.append(card);
}

export function renderNow(root: HTMLElement, state: State): void {
  const text = state.view === "analysing" ? state.now || "Now: waiting for the worker" : "";
  root.textContent = text;
  root.hidden = !text;
}

export function renderTimeline(
  root: HTMLElement,
  events: ActivityEvent[],
  onChip: (path: string) => void,
): void {
  root.replaceChildren();
  if (!events.length) {
    root.append(h("li", { class: "muted" }, "No activity yet."));
    return;
  }
  const shown = events.slice(-TIMELINE_MAX).reverse();
  for (const event of shown) {
    root.append(
      h(
        "li",
        { class: `event level-${event.level}` },
        h("time", { datetime: event.ts }, formatTime(event.ts)),
        h("span", { class: "event-icon", "aria-hidden": "true" }, EVENT_ICON[event.kind] ?? "·"),
        h("span", { class: "event-msg" }, event.message),
        event.path
          ? h(
              "button",
              {
                type: "button",
                class: "chip",
                title: `Select ${event.path}`,
                on: { click: () => onChip(event.path as string) },
              },
              event.path.split("/").pop() ?? event.path,
            )
          : null,
      ),
    );
  }
}

// -- the file drawer -------------------------------------------------------------------

export interface DrawerHandlers {
  close: () => void;
  openInGitHub: (path: string) => void;
  revealRange: (path: string, start: number, end: number) => void;
  ask: (path: string) => void;
}

export function renderDrawer(root: HTMLElement, state: State, handlers: DrawerHandlers): void {
  root.replaceChildren();
  const path = state.selected;
  root.hidden = !path;
  if (!path) return;
  const file: PrFile | undefined = state.pr?.files.find((f) => f.path === path);
  const status = file ? (FILE_STATUS[file.status] ?? FILE_STATUS.modified) : null;
  const stage = file ? STAGE[state.fileStates[path] ?? file.state] : null;
  const body = h(
    "div",
    { class: "drawer-card", role: "region", "aria-label": `File details: ${path}` },
    h(
      "div",
      { class: "drawer-head" },
      h("code", { class: "path" }, path),
      h(
        "button",
        { type: "button", class: "icon-btn", "aria-label": "Close file details", on: { click: handlers.close } },
        "✕",
      ),
    ),
    h(
      "p",
      { class: "muted small" },
      status ? `${status.text} · +${file?.additions ?? 0} −${file?.deletions ?? 0}` : "unchanged in this pull request",
      stage ? ` · ${stage.icon} ${stage.text}` : "",
    ),
  );
  if (file) {
    body.append(h("h4", null, "Signals"));
    if (file.signals.length) {
      const list = h("ul", { class: "plain" });
      for (const signal of file.signals) {
        list.append(
          h(
            "li",
            null,
            h("span", { class: `dir dir-${signal.direction}`, "aria-label": signal.direction }, DIRECTION_ICON[signal.direction] ?? "·"),
            ` ${signal.explanation}`,
          ),
        );
      }
      body.append(list);
    } else {
      body.append(h("p", { class: "muted small" }, "No file-level rule fired."));
    }
    body.append(h("h4", null, "Verified evidence"));
    if (file.evidence.length) {
      const list = h("ul", { class: "plain" });
      for (const item of file.evidence) {
        list.append(
          h(
            "li",
            null,
            h(
              "button",
              {
                type: "button",
                class: "link",
                title: "Reveal these lines in GitHub",
                on: { click: () => handlers.revealRange(path, item.line_start, item.line_end) },
              },
              span(item.line_start, item.line_end),
            ),
            h("span", { class: "muted" }, item.polarity === "raises_concern" ? " concern: " : " supports: "),
            item.claim,
          ),
        );
      }
      body.append(list);
    } else {
      body.append(h("p", { class: "muted small" }, "No verified citation in this file."));
    }
  }
  body.append(
    h(
      "div",
      { class: "row-actions" },
      h("button", { type: "button", on: { click: () => handlers.openInGitHub(path) } }, "Open in GitHub"),
      file
        ? h("button", { type: "button", on: { click: () => handlers.ask(path) } }, "Ask about this file")
        : null,
    ),
  );
  root.append(body);
}
