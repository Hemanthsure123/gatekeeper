// File details, opened in place under the selected file in the tree: its gate trail,
// what the backend did to it (its own events from the feed), its signals and its
// verified evidence.
import type { PrFile } from "../shared/types";
import { GATES, trail } from "./derive";
import { formatOffset, h, span } from "./dom";
import { icon } from "./icons";
import { DIRECTION_ICON, DIRECTION_TEXT, EVENT_ICON, FILE_STATUS, GATE_TEXT, SEVERITY, STAGE } from "./labels";
import type { State } from "./store";

export interface InspectorHandlers {
  close: () => void;
  openInGitHub: (path: string) => void;
  revealRange: (path: string, start: number, end: number) => void;
  ask: (path: string) => void;
}

const SEGMENT_TEXT = { done: "passed", ok: "passed", blocked: "stopped here", todo: "not reached" } as const;
const SEGMENT_ICON = { done: "✓", ok: "✓", blocked: "✕", todo: "○" } as const;

function gateTrail(file: PrFile | undefined, state: State, path: string): HTMLElement {
  const fileState = state.fileStates[path] ?? file?.state ?? "listed";
  const segments = trail(fileState);
  return h(
    "ol",
    { class: "gate-trail", "aria-label": "Gates this file passed" },
    ...GATES.map((gate, i) =>
      h(
        "li",
        { class: `gate gate-${segments[i]}` },
        h("span", { class: `seg seg-${segments[i]}`, "aria-hidden": "true" }),
        h("span", { class: "gate-name" }, h("span", { "aria-hidden": "true" }, `${SEGMENT_ICON[segments[i]]} `), GATE_TEXT[gate]),
        h("span", { class: "visually-hidden" }, `: ${SEGMENT_TEXT[segments[i]]}`),
      ),
    ),
  );
}

function heading(text: string): HTMLElement {
  return h("h4", { class: "insp-h" }, text);
}

export function renderInspector(state: State, path: string, handlers: InspectorHandlers): HTMLElement {
  const file = state.pr?.files.find((f) => f.path === path);
  const status = file ? (FILE_STATUS[file.status] ?? FILE_STATUS.modified) : null;
  const fileState = state.fileStates[path] ?? file?.state;
  const slash = path.lastIndexOf("/");
  const finished = state.view === "result";
  const started = state.events.find((e) => e.kind === "run_started")?.ts ?? null;

  const body = h(
    "div",
    { class: "inspector", role: "region", "aria-label": `File details: ${path}` },
    h(
      "div",
      { class: "insp-head" },
      h(
        "p",
        { class: "insp-path" },
        slash >= 0 ? h("span", { class: "insp-dir" }, path.slice(0, slash + 1)) : null,
        h("strong", null, path.slice(slash + 1)),
      ),
      h(
        "button",
        { type: "button", class: "icon-btn", "aria-label": "Close file details", title: "Close (Esc)", on: { click: handlers.close } },
        icon("close"),
      ),
    ),
    h(
      "p",
      { class: "insp-meta" },
      status
        ? h("span", { class: `kind-tag kind-${file?.status ?? "modified"}` }, h("span", { "aria-hidden": "true" }, `${status.glyph} `), status.text)
        : h("span", { class: "kind-tag" }, "Unchanged in this pull request"),
      file
        ? h("span", { class: "diffstat" }, h("span", { class: "add" }, `+${file.additions}`), h("span", { class: "del" }, `−${file.deletions}`))
        : null,
      file?.binary ? h("span", { class: "muted" }, "Binary file") : null,
      file?.patch_missing ? h("span", { class: "muted" }, "No patch from GitHub") : null,
    ),
  );
  if (!file || !fileState) {
    body.append(actions(path, false, handlers));
    return body;
  }
  body.append(gateTrail(file, state, path), h("p", { class: "insp-hint" }, STAGE[fileState].hint));

  const events = state.events.filter((e) => e.path === path);
  body.append(heading("What the review did"));
  if (events.length) {
    body.append(
      h(
        "ol",
        { class: "insp-events" },
        ...events.map((e) =>
          h(
            "li",
            { class: `level-${e.level}` },
            h("time", { datetime: e.ts }, formatOffset(e.ts, started)),
            h("span", { class: "event-icon", "aria-hidden": "true" }, e.level === "info" ? (EVENT_ICON[e.kind] ?? "·") : "⚠"),
            h("span", null, e.level === "info" ? null : h("span", { class: "visually-hidden" }, "Warning: "), e.message),
          ),
        ),
      ),
    );
  } else {
    body.append(h("p", { class: "muted small" }, "No events for this file in the activity feed."));
  }

  if (!finished) {
    body.append(h("p", { class: "muted small" }, "Signals and evidence appear when the review finishes."));
    body.append(actions(path, true, handlers));
    return body;
  }

  body.append(heading("Signals"));
  if (file.signals.length) {
    body.append(
      h(
        "ul",
        { class: "insp-list" },
        ...file.signals.map((signal) =>
          h(
            "li",
            null,
            h("span", { class: `dir dir-${signal.direction}`, "aria-hidden": "true" }, DIRECTION_ICON[signal.direction] ?? "·"),
            h(
              "span",
              null,
              h("span", { class: "visually-hidden" }, `${DIRECTION_TEXT[signal.direction] ?? ""}: `),
              signal.explanation,
              h("span", { class: "sev" }, SEVERITY[signal.severity] ?? signal.severity),
            ),
          ),
        ),
      ),
    );
  } else {
    body.append(h("p", { class: "muted small" }, "No file-level rule fired."));
  }

  body.append(heading("Verified evidence"));
  if (file.evidence.length) {
    body.append(
      h(
        "ul",
        { class: "insp-list evidence" },
        ...file.evidence.map((item) =>
          h(
            "li",
            null,
            h(
              "button",
              {
                type: "button",
                class: "line-ref",
                title: "Reveal these lines in GitHub",
                on: { click: () => handlers.revealRange(path, item.line_start, item.line_end) },
              },
              span(item.line_start, item.line_end),
            ),
            h(
              "span",
              null,
              h(
                "span",
                { class: `polarity polarity-${item.polarity}` },
                item.polarity === "raises_concern" ? "Concern" : "Supports",
              ),
              ` ${item.claim}`,
            ),
          ),
        ),
      ),
    );
  } else {
    body.append(h("p", { class: "muted small" }, "No verified citation in this file."));
  }
  body.append(actions(path, true, handlers));
  return body;
}

function actions(path: string, changed: boolean, handlers: InspectorHandlers): HTMLElement {
  return h(
    "div",
    { class: "row-actions" },
    h("button", { type: "button", on: { click: () => handlers.openInGitHub(path) } }, icon("external"), " Open in GitHub"),
    changed ? h("button", { type: "button", on: { click: () => handlers.ask(path) } }, icon("chat"), " Ask about this file") : null,
  );
}
