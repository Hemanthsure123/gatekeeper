// Insights tab, in two views:
// - This pull request: KPIs, how confidence was computed, where the time went, the
//   largest changes, the signals, every verified citation, and what the model saw;
// - Last 30 days: KPIs, verdicts, confidence over time, abstain reasons, failures,
//   queue state and the recent analyses.
// Charts are single-series HTML bars with text labels (see charts.ts), so each one is
// also its own table view.
import type { Metrics, ModelInput, Verdict } from "../shared/types";
import type { Api } from "./api";
import { ApiError } from "./api";
import { barList, columns, meter, tile, type Bar } from "./charts";
import type { RunFacts } from "./derive";
import { formatCount, formatDuration, formatTime, h, pct, span } from "./dom";
import { icon } from "./icons";
import { abstainText, CLAIM_MATCH, DIRECTION_ICON, DIRECTION_TEXT, errorText, SEVERITY, STEP_TEXT, VERDICT } from "./labels";
import { openPr } from "./reveal";
import type { State } from "./store";
import { store } from "./store";

export interface InsightsDeps {
  api: () => Api | null;
  facts: RunFacts;
  selectFile: (path: string) => void;
  revealRange: (path: string, start: number, end: number) => void;
}

type Scope = "pr" | "all";

const REFRESH_MS = 30000;
const VERDICTS: Verdict[] = ["looks_valuable", "needs_human_look", "likely_low_value"];
const LARGEST = 8;

let mounted: HTMLElement | null = null;
let deps: InsightsDeps | null = null;
let metrics: Metrics | null = null;
let metricsAt = 0;
let metricsError = "";
let loading = false;
let modelFor = "";
let modelInput: ModelInput | null = null;
let modelNote = "";
let chosen: Scope | null = null;

function section(title: string, ...children: Array<Node | null>): HTMLElement {
  return h("section", { class: "insight" }, h("h3", null, title), ...children);
}

function pathLabel(path: string): HTMLElement {
  const slash = path.lastIndexOf("/");
  return h(
    "span",
    { class: "path-label" },
    slash >= 0 ? h("span", { class: "insp-dir" }, path.slice(0, slash + 1)) : null,
    path.slice(slash + 1),
  );
}

// -- this pull request -----------------------------------------------------------------

function prKpis(state: State, facts: RunFacts): HTMLElement {
  const pr = state.pr!;
  const analysis = pr.analysis;
  const verified = pr.files.reduce((n, f) => n + f.evidence.length, 0);
  const cited = verified + analysis.dropped_evidence_count;
  const model = facts.model;
  const tokens =
    model && (model.inputTokens !== null || model.outputTokens !== null)
      ? `${formatCount(model.inputTokens)} in, ${formatCount(model.outputTokens)} out`
      : "–";
  return h(
    "div",
    { class: "kpis" },
    tile("Lines changed", `+${formatCount(pr.totals.additions)} −${formatCount(pr.totals.deletions)}`, `in ${pr.totals.files} files`),
    tile("Verified citations", String(verified), cited ? `of ${cited} cited` : "none cited"),
    tile("Citation validity", pct(analysis.citation_validity), "Share of citations that matched the diff"),
    tile("Review time", formatDuration(facts.runMs), model?.latencyMs ? `model ${formatDuration(model.latencyMs)}` : ""),
    tile("Model tokens", tokens, model?.cacheHit ? "answer reused from the cache" : (analysis.model ?? "")),
    tile("Description", analysis.claim_match ? (CLAIM_MATCH[analysis.claim_match] ?? analysis.claim_match) : "–", "vs the change"),
  );
}

function confidenceSection(state: State, facts: RunFacts): HTMLElement {
  const c = facts.confidence;
  if (!c) {
    return section(
      "How confidence was computed",
      h("p", { class: "muted small" }, "No confidence was computed for this run (it abstained before the decision, or ran before the activity feed)."),
    );
  }
  const bars: Bar[] = [];
  const add = (label: string, value: number | null, title: string): void => {
    if (value !== null) bars.push({ label, value, display: pct(value), title });
  };
  add("Model's self-confidence", c.selfConfidence, "How sure the model said it was");
  add("Citation validity", c.citationValidity, "Share of the model's citations that matched the diff");
  add("Signal agreement", c.agreement, "How well the rule signals agree with the verdict");
  return section(
    "How confidence was computed",
    barList("Inputs", bars, { max: 1 }),
    h(
      "div",
      { class: "confidence" },
      h("span", { class: "confidence-label" }, "Result"),
      meter("Confidence", c.value, c.threshold),
      h("span", { class: "confidence-value" }, pct(c.value)),
    ),
    h(
      "p",
      { class: "muted small" },
      c.threshold !== null ? `Abstain threshold ${pct(c.threshold)}. ` : "",
      c.truncated ? "Part of the input was truncated, which lowers confidence. " : "",
      "A transparent heuristic, not a calibrated probability.",
    ),
    state.pr?.analysis.status === "abstained"
      ? h("p", { class: "note" }, h("span", { "aria-hidden": "true" }, "◎ "), abstainText(state.pr.analysis.abstain_reason))
      : null,
  );
}

function timeSection(facts: RunFacts): HTMLElement {
  const bars: Bar[] = facts.steps
    .filter((s) => s.ms !== null)
    .map((s) => ({ label: STEP_TEXT[s.name], value: s.ms ?? 0, display: formatDuration(s.ms) }));
  return section(
    "Where the time went",
    barList(facts.runMs !== null ? `Total ${formatDuration(facts.runMs)}` : "Per step", bars, {
      empty: "No step timings in the activity feed for this run.",
    }),
  );
}

function largestSection(state: State, d: InsightsDeps): HTMLElement {
  const files = [...(state.pr?.files ?? [])]
    .filter((f) => f.additions + f.deletions > 0)
    .sort((a, b) => b.additions + b.deletions - (a.additions + a.deletions))
    .slice(0, LARGEST);
  return section(
    "Largest changes",
    barList(
      "Lines added and removed",
      files.map((f) => ({
        label: pathLabel(f.path),
        value: f.additions + f.deletions,
        display: `+${f.additions} −${f.deletions}`,
        title: `Show ${f.path} in the tree`,
        onClick: () => d.selectFile(f.path),
      })),
      { empty: "No line changes (binary or renamed files only)." },
    ),
  );
}

function signalsSection(state: State): HTMLElement {
  const order = { negative: 0, positive: 1, neutral: 2 } as const;
  const signals = [...(state.pr?.signals ?? [])].sort((a, b) => order[a.direction] - order[b.direction]);
  if (!signals.length) return section("Signals", h("p", { class: "muted small" }, "No rule signal fired for this change."));
  return section(
    "Signals",
    h(
      "ul",
      { class: "insp-list" },
      ...signals.map((s) =>
        h(
          "li",
          null,
          h("span", { class: `dir dir-${s.direction}`, "aria-hidden": "true" }, DIRECTION_ICON[s.direction] ?? "·"),
          h(
            "span",
            null,
            h("span", { class: "visually-hidden" }, `${DIRECTION_TEXT[s.direction] ?? ""}: `),
            s.explanation,
            h("span", { class: "sev" }, SEVERITY[s.severity] ?? s.severity),
          ),
        ),
      ),
    ),
  );
}

function evidenceSection(state: State, d: InsightsDeps): HTMLElement {
  const items = (state.pr?.files ?? []).flatMap((f) => f.evidence.map((e) => ({ path: f.path, e })));
  if (!items.length) return section("Verified evidence", h("p", { class: "muted small" }, "No citation passed the check."));
  return section(
    "Verified evidence",
    h(
      "ul",
      { class: "insp-list evidence" },
      ...items.map(({ path, e }) =>
        h(
          "li",
          null,
          h(
            "button",
            {
              type: "button",
              class: "line-ref",
              title: `Reveal ${path}:${span(e.line_start, e.line_end)} in GitHub`,
              on: { click: () => d.revealRange(path, e.line_start, e.line_end) },
            },
            `${path.split("/").pop()}:${span(e.line_start, e.line_end)}`,
          ),
          h(
            "span",
            null,
            h("span", { class: `polarity polarity-${e.polarity}` }, e.polarity === "raises_concern" ? "Concern" : "Supports"),
            ` ${e.claim}`,
          ),
        ),
      ),
    ),
  );
}

function modelSection(state: State): HTMLElement {
  const box = h(
    "section",
    { class: "insight model-input", "aria-labelledby": "model-heading" },
    h("h3", { id: "model-heading" }, "What the model saw"),
    h("p", { class: "note" }, "Author name and account details are never sent to the model."),
  );
  if (!state.analysisId || state.view !== "result") {
    box.append(h("p", { class: "muted small" }, "Open an analysed pull request to see its model input."));
    return box;
  }
  if (!modelInput) {
    box.append(h("p", { class: "muted small" }, modelNote || "Loading…"));
    return box;
  }
  box.append(
    h(
      "dl",
      { class: "facts" },
      h("div", null, h("dt", null, "Model"), h("dd", null, `${modelInput.model ?? "model"} (${modelInput.backend ?? "backend"})`)),
      h("div", null, h("dt", null, "Prompt"), h("dd", null, `${modelInput.prompt_id} v${modelInput.prompt_version}`)),
      modelInput.images ? h("div", null, h("dt", null, "Images"), h("dd", null, String(modelInput.images))) : null,
    ),
    h("details", null, h("summary", null, "System instructions"), h("pre", { class: "scroll" }, modelInput.system)),
    h(
      "details",
      { open: true },
      h("summary", null, "Input (redacted, exactly as stored in the audit trail)"),
      h("pre", { class: "scroll" }, modelInput.user),
    ),
    h("p", { class: "muted small" }, modelInput.schema_note),
  );
  return box;
}

function prView(state: State, d: InsightsDeps): HTMLElement[] {
  if (state.view !== "result" || !state.pr) {
    return [
      h(
        "p",
        { class: "empty-line" },
        state.view === "analysing"
          ? "Insights for this pull request appear when its review finishes."
          : "Open an analysed pull request to see its insights.",
      ),
    ];
  }
  if (state.pr.analysis.status === "failed") {
    return [h("p", { class: "empty-line" }, "This review did not finish, so there is nothing to measure yet."), modelSection(state)];
  }
  return [
    prKpis(state, d.facts),
    confidenceSection(state, d.facts),
    timeSection(d.facts),
    largestSection(state, d),
    signalsSection(state),
    evidenceSection(state, d),
    modelSection(state),
  ];
}

// -- last 30 days ------------------------------------------------------------------------

function allKpis(m: Metrics): HTMLElement {
  const rate = m.total ? m.abstained.count / m.total : null;
  const latency = m.latency_ms;
  return h(
    "div",
    { class: "kpis" },
    tile("Analyses", String(m.total), `${m.failed.count} failed`),
    tile("Abstain rate", pct(rate), `${m.abstained.count} abstained`),
    tile("Avg confidence", pct(m.confidence.average), `median ${pct(m.confidence.median)}`, "Heuristic, not calibrated"),
    tile("Citation validity", pct(m.citation_validity_average), "average", "Share of model citations that passed the verifier"),
    tile(
      "Model latency",
      latency.average === null ? "–" : formatDuration(latency.average),
      latency.p95 === null ? "average" : `p95 ${formatDuration(latency.p95)}`,
      `Model calls only; ${latency.note}`,
    ),
    tile("Models", String(m.models.length), m.models.join(", ")),
  );
}

function verdictChart(m: Metrics): HTMLElement {
  return barList(
    "Verdicts",
    VERDICTS.map((v) => ({
      label: h("span", null, h("span", { "aria-hidden": "true" }, `${VERDICT[v].icon} `), VERDICT[v].text),
      value: m.by_verdict[v] ?? 0,
      display: String(m.by_verdict[v] ?? 0),
    })),
  );
}

function confidenceChart(m: Metrics): HTMLElement {
  const recent = [...m.recent].reverse(); // oldest first
  const values = recent.map((r) => r.confidence).filter((v): v is number => v !== null);
  const summary = values.length
    ? `Confidence of the last ${recent.length} analyses, from ${pct(Math.min(...values))} to ${pct(Math.max(...values))}; latest ${pct(recent[recent.length - 1]?.confidence)}`
    : "No confidence values yet";
  return columns(
    "Confidence, recent analyses",
    recent.map((r) => ({
      value: r.confidence,
      title: `${r.repo}#${r.number}: ${r.confidence === null ? "no confidence" : pct(r.confidence)} (${r.verdict ? VERDICT[r.verdict].text : r.status})`,
    })),
    summary,
  );
}

function countBars(caption: string, counts: Record<string, number>, label: (key: string) => string, empty: string, title?: (key: string) => string): HTMLElement {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  return barList(
    caption,
    entries.map(([key, value]) => ({ label: label(key), value, display: String(value), title: title?.(key) })),
    { empty },
  );
}

function recentList(m: Metrics): HTMLElement {
  if (!m.recent.length) return h("p", { class: "muted" }, "No analyses yet.");
  return h(
    "ol",
    { class: "recent" },
    ...m.recent.map((r) => {
      const [owner, name] = r.repo.split("/");
      const verdict = r.verdict ? VERDICT[r.verdict] : null;
      return h(
        "li",
        null,
        h(
          "button",
          {
            type: "button",
            class: "recent-row",
            title: `Open ${r.repo}#${r.number} in the GitHub tab`,
            on: { click: () => void openPr(owner, name, r.number) },
          },
          h("span", { class: "recent-pr" }, `${r.repo}#${r.number}`),
          h("span", { class: "recent-num" }, pct(r.confidence)),
          h(
            "span",
            { class: `recent-verdict tone-${verdict?.tone ?? "bad"}` },
            h("span", { "aria-hidden": "true" }, `${verdict ? verdict.icon : "⚠"} `),
            verdict ? verdict.text : r.status,
            r.status === "abstained" ? " (abstained)" : "",
          ),
          h("time", { datetime: r.created_at, class: "recent-time" }, formatTime(r.created_at)),
        ),
      );
    }),
  );
}

function allView(): HTMLElement[] {
  if (metricsError) return [h("p", { class: "notice" }, metricsError)];
  if (!metrics) return [h("p", { class: "empty-line" }, deps?.api() ? "Loading metrics…" : "Connect to the server to see metrics.")];
  const m = metrics;
  const jobs = Object.entries(m.jobs_by_status);
  return [
    allKpis(m),
    section("Outcomes", verdictChart(m), confidenceChart(m)),
    section(
      "Why reviews abstained or failed",
      countBars("Abstain reasons", m.abstained.reasons, (k) => abstainText(k), "No review abstained."),
      countBars("Failures", m.failed.error_codes, (k) => k.replaceAll("_", " "), "No review failed.", (k) => errorText(k)),
    ),
    section(
      "Queue",
      jobs.length
        ? h("ul", { class: "chips" }, ...jobs.map(([status, n]) => h("li", { class: "count-chip" }, h("strong", null, String(n)), ` ${status}`)))
        : h("p", { class: "muted small" }, "No jobs in this period."),
    ),
    section("Recent analyses", recentList(m)),
  ];
}

// -- loading and painting --------------------------------------------------------------

async function loadMetrics(): Promise<void> {
  const api = deps?.api();
  if (!api || loading) return;
  loading = true;
  try {
    metrics = await api.metrics(30);
    metricsAt = Date.now();
    metricsError = "";
  } catch (error) {
    metricsError = error instanceof ApiError ? error.message : "Could not load metrics.";
  } finally {
    loading = false;
  }
}

async function loadModelInput(id: string): Promise<void> {
  const api = deps?.api();
  if (!api) return;
  try {
    const data = await api.modelInput(id);
    if (modelFor === id) {
      modelInput = data;
      modelNote = "";
    }
  } catch (error) {
    if (modelFor === id) {
      modelNote =
        error instanceof ApiError && error.status === 404
          ? "This run made no model call (for example, the diff was too large)."
          : "Could not load the model input.";
    }
  }
}

function scopeOf(state: State): Scope {
  return chosen ?? (state.view === "result" ? "pr" : "all");
}

let painted: unknown[] = [];

function paint(root: HTMLElement, state: State, force = false): void {
  const scope = scopeOf(state);
  const inputs = [scope, metrics, metricsError, modelInput, modelNote, state.analysisId, state.view, state.pr, deps?.facts];
  if (!force && inputs.length === painted.length && inputs.every((v, i) => v === painted[i])) return;
  painted = inputs;
  for (const button of root.querySelectorAll<HTMLButtonElement>(".scope button")) {
    button.setAttribute("aria-pressed", String(button.dataset.scope === scope));
  }
  const body = root.querySelector("#insights-body") as HTMLElement;
  body.replaceChildren(...(scope === "pr" && deps ? prView(state, deps) : allView()));
}

function refresh(root: HTMLElement): void {
  metricsAt = 0;
  void loadMetrics().then(() => paint(root, store.get(), true));
}

export function renderInsights(root: HTMLElement, state: State, insightsDeps: InsightsDeps): void {
  deps = insightsDeps;
  if (mounted !== root) {
    mounted = root;
    const pick = (scope: Scope) => () => {
      chosen = scope;
      paint(root, store.get(), true);
    };
    root.replaceChildren(
      h(
        "div",
        { class: "insights-bar" },
        h(
          "div",
          { class: "segmented scope", role: "group", "aria-label": "Which insights to show" },
          h("button", { type: "button", dataset: { scope: "pr" }, on: { click: pick("pr") } }, "This pull request"),
          h("button", { type: "button", dataset: { scope: "all" }, on: { click: pick("all") } }, "Last 30 days"),
        ),
        h(
          "button",
          { type: "button", class: "icon-btn", "aria-label": "Refresh metrics", title: "Refresh metrics", on: { click: () => refresh(root) } },
          icon("refresh"),
        ),
      ),
      h("div", { id: "insights-body", class: "insights-body" }),
    );
  }
  if (Date.now() - metricsAt > REFRESH_MS && !loading) {
    void loadMetrics().then(() => mounted && paint(mounted, store.get(), true));
  }
  const id = state.view === "result" ? (state.analysisId ?? "") : "";
  if (id !== modelFor) {
    modelFor = id;
    modelInput = null;
    modelNote = "";
    if (id) void loadModelInput(id).then(() => mounted && paint(mounted, store.get(), true));
  }
  paint(root, state);
}
