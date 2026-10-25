// Activity tab views: the verdict card, the pipeline (the five backend steps, each with
// how many changed files passed its gate), the "Now" line and the grouped timeline.
import type { ActivityEvent } from "../shared/types";
import { meter } from "./charts";
import { funnel, groupEvents, STEPS, type Funnel, type GateFilter, type RunFacts, type StepFacts } from "./derive";
import { formatDuration, formatOffset, formatTime, h, pct } from "./dom";
import { icon } from "./icons";
import {
  abstainText,
  BACKEND,
  CLAIM_MATCH,
  downgradeText,
  errorText,
  EVENT_ICON,
  STEP_STATUS,
  STEP_TEXT,
  VERDICT,
} from "./labels";
import type { State } from "./store";

const TIMELINE_MAX = 300;
const RATIONALE_CLAMP = 240;

export interface VerdictHandlers {
  /** Renders the approve-and-post control under the reply draft (Part 6). */
  approval?: (state: State) => HTMLElement | null;
  /** Per-repository consent for private repositories on the hosted model. */
  consent?: (state: State) => HTMLElement | null;
  analyse: () => void;
}

let rationaleOpenFor = "";

function spinner(): HTMLElement {
  return h("span", { class: "spinner", "aria-hidden": "true" });
}

function stat(value: string, label: string, hint = ""): HTMLElement {
  return h(
    "div",
    { class: "stat", title: hint || undefined },
    h("span", { class: "stat-value" }, value),
    h("span", { class: "stat-label" }, label),
  );
}

function head(mark: Node | string, title: string, sub: string, extra: HTMLElement | null = null, id?: string): HTMLElement {
  return h(
    "div",
    { class: "verdict-head" },
    h("span", { class: "verdict-mark", "aria-hidden": "true" }, mark),
    h(
      "div",
      { class: "verdict-titles" },
      h("h2", { id, class: "verdict-text" }, title),
      sub ? h("p", { class: "verdict-sub" }, sub) : null,
    ),
    extra,
  );
}

export function renderVerdict(root: HTMLElement, state: State, facts: RunFacts, handlers: VerdictHandlers): void {
  root.replaceChildren();
  const analysis = state.pr?.analysis;
  if (state.view === "not_analysed") {
    root.append(
      h(
        "article",
        { class: "card hero-empty" },
        h("h2", { class: "card-title" }, "Not reviewed yet"),
        h(
          "p",
          { class: "muted" },
          "Gatekeeper reads the change, checks every line the model cites against the diff, and suggests a verdict. Nothing is posted without you.",
        ),
        h("button", { type: "button", class: "primary big", on: { click: handlers.analyse } }, "Analyse this pull request"),
        state.line ? h("p", { class: "error-line", role: "status" }, state.line) : null,
      ),
    );
    return;
  }
  if (state.view === "analysing" || !analysis) {
    const index = facts.current ? STEPS.indexOf(facts.current) : -1;
    root.append(
      h(
        "article",
        { class: "card tone-live", "aria-busy": "true" },
        head(
          spinner(),
          "Reviewing this pull request",
          index >= 0 ? `Step ${index + 1} of ${STEPS.length}: ${STEP_TEXT[STEPS[index]]}` : state.line || "Following the run live.",
        ),
      ),
    );
    return;
  }
  if (analysis.status === "failed") {
    const failed = h(
      "article",
      { class: "card tone-bad" },
      head("⚠", "The review could not finish", `Code: ${analysis.error_code ?? "unknown"}. Fix the cause, then re-analyse.`),
      h("p", { class: "card-text" }, errorText(analysis.error_code)),
    );
    const consent = handlers.consent?.(state);
    if (consent) failed.append(consent);
    root.append(failed);
    return;
  }

  const verdict = VERDICT[analysis.verdict ?? "needs_human_look"];
  const files = state.pr?.files ?? [];
  const verified = files.reduce((n, f) => n + f.evidence.length, 0);
  const concerns = files.reduce((n, f) => n + f.evidence.filter((e) => e.polarity === "raises_concern").length, 0);
  const signals = state.pr?.signals ?? [];
  const positive = signals.filter((s) => s.direction === "positive").length;
  const negative = signals.filter((s) => s.direction === "negative").length;
  const sub =
    `Reviewed ${files.length} file${files.length === 1 ? "" : "s"}` +
    (facts.runMs !== null ? ` in ${formatDuration(facts.runMs)}` : "");
  const threshold = facts.confidence?.threshold ?? null;

  const card = h(
    "article",
    { class: `card verdict-card tone-${verdict.tone}`, "aria-labelledby": "verdict-title" },
    head(
      verdict.icon,
      verdict.text,
      sub,
      analysis.status === "abstained" ? h("span", { class: "pill" }, h("span", { "aria-hidden": "true" }, "◎ "), "Abstained") : null,
      "verdict-title",
    ),
    h(
      "div",
      { class: "confidence", title: "Heuristic, not calibrated" },
      h("span", { class: "confidence-label" }, "Confidence"),
      meter("Confidence", analysis.confidence, threshold),
      h("span", { class: "confidence-value" }, pct(analysis.confidence)),
    ),
  );
  if (threshold !== null) {
    card.append(h("p", { class: "confidence-note" }, `The tick marks ${pct(threshold)}: below it the review abstains and a person decides.`));
  }
  if (analysis.status === "abstained" && analysis.abstain_reason) {
    card.append(h("p", { class: "note" }, h("span", { "aria-hidden": "true" }, "◎ "), abstainText(analysis.abstain_reason)));
  }
  if (analysis.downgrade_reason) {
    card.append(h("p", { class: "note" }, h("span", { "aria-hidden": "true" }, "▽ "), downgradeText(analysis.downgrade_reason)));
  }
  if (analysis.rationale) {
    const long = analysis.rationale.length > RATIONALE_CLAMP;
    const open = !long || rationaleOpenFor === analysis.id;
    const text = h("p", { class: `rationale${open ? "" : " clamped"}` }, analysis.rationale);
    card.append(text);
    if (long) {
      const toggle = h(
        "button",
        { type: "button", class: "link", "aria-expanded": String(open) },
        open ? "Show less" : "Show more",
      );
      toggle.addEventListener("click", () => {
        const clamp = !text.classList.contains("clamped");
        text.classList.toggle("clamped", clamp);
        toggle.textContent = clamp ? "Show more" : "Show less";
        toggle.setAttribute("aria-expanded", String(!clamp));
        rationaleOpenFor = clamp ? "" : analysis.id;
      });
      card.append(toggle);
    }
  }
  card.append(
    h(
      "div",
      { class: "stats" },
      stat(String(verified), verified === 1 ? "Verified citation" : "Verified citations", concerns ? `${concerns} raise a concern` : ""),
      stat(String(analysis.dropped_evidence_count), "Dropped", "Citations that did not match the diff"),
      stat(`+${positive} −${negative}`, "Signals", "Positive and negative rule signals"),
    ),
  );
  const list = h("dl", { class: "facts" });
  const fact = (term: string, value: string): void => {
    list.append(h("div", null, h("dt", null, term), h("dd", null, value)));
  };
  if (analysis.claim_match) fact("Description", CLAIM_MATCH[analysis.claim_match] ?? analysis.claim_match);
  if (analysis.model) fact("Model", `${analysis.model}${analysis.cache_hit ? " (cache hit)" : ""}`);
  if (state.pr?.model_backend) fact("Runs on", BACKEND[state.pr.model_backend] ?? state.pr.model_backend);
  if (list.childElementCount) card.append(list);
  if (analysis.reply_draft) {
    card.append(
      h("details", { class: "draft" }, h("summary", null, "Reply draft (not posted)"), h("pre", null, analysis.reply_draft)),
    );
  }
  const approval = handlers.approval?.(state);
  if (approval) card.append(approval);
  const consent = handlers.consent?.(state);
  if (consent) card.append(consent);
  root.append(card);
}

export function renderNow(root: HTMLElement, state: State): void {
  const text = state.view === "analysing" ? state.now || "Now: waiting for the worker" : "";
  if (root.textContent !== text) root.textContent = text;
  root.hidden = !text;
}

// -- the pipeline ----------------------------------------------------------------------

export interface PipelineHandlers {
  filter: (gate: GateFilter) => void;
}

const GATE_WORD: Record<GateFilter, string> = {
  listed: "listed",
  checked: "checked",
  read: "read",
  left_out: "left out",
  cited: "cited",
  verified: "verified",
  dropped: "dropped",
};

function gateButton(gate: GateFilter, f: Funnel, state: State, handlers: PipelineHandlers): HTMLElement {
  const count = f[gate];
  const pressed = state.gate === gate;
  return h(
    "button",
    {
      type: "button",
      class: `gate-btn gate-btn-${gate}`,
      "aria-pressed": String(pressed),
      disabled: count === 0 && !pressed,
      title: pressed ? "Show every file again" : "Show only these files in the tree",
      on: { click: () => handlers.filter(gate) },
    },
    h("strong", null, String(count)),
    ` ${GATE_WORD[gate]}`,
  );
}

function gateBar(count: number, total: number): HTMLElement {
  const share = total ? count / total : 0;
  return h(
    "span",
    { class: "gate-bar", title: `${count} of ${total} changed files`, "aria-hidden": "true" },
    h("span", { style: `width:${(share * 100).toFixed(1)}%` }),
  );
}

function note(text: string, strong = false): HTMLElement {
  return h("span", { class: strong ? "step-note strong" : "step-note" }, text);
}

function stepDetail(step: StepFacts, f: Funnel, facts: RunFacts, state: State, handlers: PipelineHandlers): HTMLElement | null {
  if (step.status === "pending" || step.status === "skipped") return null;
  if (!f.total && step.name !== "decision") return null;
  const row = (bar: HTMLElement | null, ...parts: Array<HTMLElement | null>): HTMLElement =>
    h("div", { class: "step-detail" }, bar, h("div", { class: "step-gates" }, ...parts));
  switch (step.name) {
    case "intake":
      return row(
        gateBar(f.listed, f.total),
        gateButton("listed", f, state, handlers),
        facts.diffTruncated
          ? h("span", { class: "warn-text" }, h("span", { "aria-hidden": "true" }, "⚠ "), "GitHub truncated the diff")
          : null,
      );
    case "signals":
      return row(
        gateBar(f.checked, f.total),
        gateButton("checked", f, state, handlers),
        facts.signals ? note(`${facts.signals.positive.length} positive, ${facts.signals.negative.length} negative`) : null,
      );
    case "review": {
      const model = facts.model;
      const said = !model
        ? null
        : model.cacheHit
          ? "answer reused from the cache"
          : model.latencyMs !== null
            ? `answered in ${formatDuration(model.latencyMs)}`
            : null;
      return row(
        gateBar(f.read, f.total),
        gateButton("read", f, state, handlers),
        f.left_out || state.gate === "left_out" ? gateButton("left_out", f, state, handlers) : null,
        said ? note(said) : null,
      );
    }
    case "verify":
      return row(
        gateBar(f.verified, f.total),
        gateButton("cited", f, state, handlers),
        gateButton("verified", f, state, handlers),
        f.dropped || state.gate === "dropped" ? gateButton("dropped", f, state, handlers) : null,
      );
    case "decision": {
      const decided = facts.decision;
      const confidence = facts.confidence?.value ?? state.pr?.analysis.confidence ?? null;
      if (!decided && confidence === null) return null;
      const verdict = decided?.verdict && decided.verdict in VERDICT ? VERDICT[decided.verdict as keyof typeof VERDICT] : null;
      return row(
        null,
        note(`Confidence ${pct(confidence)}`),
        verdict ? note(`${verdict.icon} ${verdict.text}`, true) : null,
        decided?.status === "abstained" ? note("abstained") : null,
      );
    }
  }
}

export function renderPipeline(
  steps: HTMLElement,
  meta: HTMLElement,
  state: State,
  facts: RunFacts,
  handlers: PipelineHandlers,
): void {
  const f = funnel(state.fileStates);
  meta.textContent =
    state.view === "analysing"
      ? f.total
        ? `${f.total} changed file${f.total === 1 ? "" : "s"}`
        : "Starting"
      : facts.runMs !== null
        ? `Total ${formatDuration(facts.runMs)}`
        : "";
  steps.replaceChildren(
    ...facts.steps.map((step, i) => {
      const status = STEP_STATUS[step.status];
      const time =
        step.status === "done" && step.ms !== null
          ? formatDuration(step.ms)
          : step.status === "running"
            ? "Running…"
            : status.text;
      return h(
        "li",
        { class: `step step-${step.status}`, "aria-current": step.status === "running" ? "step" : undefined },
        h("span", { class: "step-mark", "aria-hidden": "true" }, step.status === "running" ? spinner() : status.icon),
        h(
          "div",
          { class: "step-body" },
          h(
            "div",
            { class: "step-head" },
            h("span", { class: "step-name" }, h("span", { class: "visually-hidden" }, `Step ${i + 1}: `), STEP_TEXT[step.name]),
            h("span", { class: "step-time" }, h("span", { class: "visually-hidden" }, `${status.text}, `), time),
          ),
          stepDetail(step, f, facts, state, handlers),
        ),
      );
    }),
  );
}

// -- the timeline ----------------------------------------------------------------------

/** Groups the user opened or closed; others follow the defaults below. */
const userOpen = new Map<string, boolean>();
let warningsOnly = false;
let last: { root: HTMLElement; state: State; facts: RunFacts; onPath: (path: string) => void } | null = null;

function groupLabel(step: string | null, events: ActivityEvent[]): string {
  if (step && step in STEP_TEXT) return STEP_TEXT[step as keyof typeof STEP_TEXT];
  if (events.some((e) => e.kind === "run_failed")) return "Run failed";
  if (events.some((e) => e.kind === "run_finished")) return "Run finished";
  if (events.some((e) => e.kind === "run_started")) return "Run started";
  return "Run";
}

function eventRow(event: ActivityEvent, started: string | null, onPath: (path: string) => void): HTMLElement {
  const info = event.level === "info";
  return h(
    "li",
    { class: `event level-${event.level}` },
    h("time", { datetime: event.ts, title: formatTime(event.ts) }, formatOffset(event.ts, started)),
    h("span", { class: "event-icon", "aria-hidden": "true" }, info ? (EVENT_ICON[event.kind] ?? "·") : "⚠"),
    h(
      "span",
      { class: "event-msg" },
      info ? null : h("span", { class: "visually-hidden" }, event.level === "error" ? "Error: " : "Warning: "),
      event.message,
    ),
    event.path
      ? h(
          "button",
          {
            type: "button",
            class: "icon-btn small-btn",
            "aria-label": `Show ${event.path} in the tree`,
            title: `Show ${event.path} in the tree`,
            on: { click: () => onPath(event.path as string) },
          },
          icon("target"),
        )
      : h("span"),
  );
}

export function renderTimeline(root: HTMLElement, state: State, facts: RunFacts, onPath: (path: string) => void): void {
  last = { root, state, facts, onPath };
  const events = state.events.slice(-TIMELINE_MAX);
  const warnings = events.filter((e) => e.level !== "info").length;
  const toggle = h("input", { type: "checkbox", id: "tl-warnings", checked: warningsOnly, disabled: !warnings && !warningsOnly });
  toggle.addEventListener("change", () => {
    warningsOnly = toggle.checked;
    if (last) renderTimeline(last.root, last.state, last.facts, last.onPath);
    document.getElementById("tl-warnings")?.focus();
  });
  const header = h(
    "div",
    { class: "section-head" },
    h("h2", { id: "timeline-heading" }, "Timeline"),
    h("span", { class: "section-meta" }, `${events.length} event${events.length === 1 ? "" : "s"}`),
    h("label", { class: "check small push", for: "tl-warnings" }, toggle, ` Warnings only (${warnings})`),
  );
  if (!events.length) {
    root.replaceChildren(header, h("p", { class: "muted" }, "No activity yet. Events appear here as the backend works."));
    return;
  }
  const live = state.view === "analysing";
  const started = facts.startedAt ?? events[0]?.ts ?? null;
  const groups = groupEvents(events);
  const ms = new Map<string, number | null>(facts.steps.map((s) => [s.name, s.ms]));
  const parts: HTMLElement[] = [];
  groups.forEach((group, index) => {
    const shown = warningsOnly ? group.events.filter((e) => e.level !== "info") : group.events;
    if (!shown.length) return;
    const open = userOpen.get(group.id) ?? ((live && index === groups.length - 1) || group.warnings > 0 || warningsOnly);
    const duration = group.step ? ms.get(group.step) : null;
    const summary = h(
      "summary",
      null,
      h("span", { class: "tl-title" }, groupLabel(group.step, group.events)),
      h(
        "span",
        { class: "tl-meta" },
        group.warnings
          ? h(
              "span",
              { class: "warn-text" },
              h("span", { "aria-hidden": "true" }, "⚠ "),
              `${group.warnings} warning${group.warnings === 1 ? "" : "s"}`,
            )
          : null,
        h("span", null, `${group.events.length} event${group.events.length === 1 ? "" : "s"}`),
        duration !== null && duration !== undefined ? h("span", null, formatDuration(duration)) : null,
      ),
    );
    const details = h("details", { class: "tl-group", open }, summary);
    // Record only what the user chose; a summary click toggles after this runs.
    summary.addEventListener("click", () => userOpen.set(group.id, !details.open));
    details.append(h("ol", { class: "tl-events" }, ...shown.map((event) => eventRow(event, started, onPath))));
    parts.push(details);
  });
  root.replaceChildren(header, ...parts);
}
