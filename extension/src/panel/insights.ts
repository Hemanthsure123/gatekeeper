// Insights tab: KPI tiles, two small inline-SVG charts (no chart library), the recent
// analyses list (which doubles as the charts' table view) and "What the model saw".
// Single-series charts: one accent colour, direct labels, a native tooltip per mark.
import type { Metrics, ModelInput, Verdict } from "../shared/types";
import type { Api } from "./api";
import { ApiError } from "./api";
import { formatTime, h } from "./dom";
import { VERDICT } from "./labels";
import { openPr } from "./reveal";
import type { State } from "./store";
import { store } from "./store";

export interface InsightsDeps {
  api: () => Api | null;
}

const SVG = "http://www.w3.org/2000/svg";
const REFRESH_MS = 30000;
const VERDICTS: Verdict[] = ["looks_valuable", "needs_human_look", "likely_low_value"];

let mounted: HTMLElement | null = null;
let deps: InsightsDeps | null = null;
let metrics: Metrics | null = null;
let metricsAt = 0;
let metricsError = "";
let loading = false;
let modelFor = "";
let modelInput: ModelInput | null = null;
let modelNote = "";

function svg<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attrs: Record<string, string | number>,
  ...children: Array<SVGElement | string>
): SVGElementTagNameMap[K] {
  const el = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, String(value));
  for (const child of children) el.append(child);
  return el;
}

function title(text: string): SVGTitleElement {
  const t = document.createElementNS(SVG, "title");
  t.textContent = text;
  return t;
}

/** A bar with a 4px rounded data end and a square base. */
function barPath(x: number, y: number, w: number, hgt: number, horizontal: boolean): string {
  if (horizontal) {
    const r = Math.min(4, hgt / 2, w);
    return `M${x},${y}h${w - r}a${r},${r} 0 0 1 ${r},${r}v${hgt - 2 * r}a${r},${r} 0 0 1 ${-r},${r}h${-(w - r)}z`;
  }
  const r = Math.min(4, w / 2, hgt);
  return `M${x},${y + hgt}v${-(hgt - r)}a${r},${r} 0 0 1 ${r},${-r}h${w - 2 * r}a${r},${r} 0 0 1 ${r},${r}v${hgt - r}z`;
}

function pct(value: number | null | undefined): string {
  return value === null || value === undefined ? "–" : `${Math.round(value * 100)}%`;
}

function tile(label: string, value: string, hint = ""): HTMLElement {
  return h(
    "div",
    { class: "tile", title: hint || undefined },
    h("span", { class: "tile-value" }, value),
    h("span", { class: "tile-label" }, label),
  );
}

function kpis(m: Metrics): HTMLElement {
  const count = (v: Verdict) => String(m.by_verdict[v] ?? 0);
  const rate = m.total ? m.abstained.count / m.total : null;
  const latency = m.latency_ms.average;
  return h(
    "div",
    { class: "kpis" },
    tile("Analyses", String(m.total)),
    tile(`${VERDICT.looks_valuable.icon} Looks valuable`, count("looks_valuable")),
    tile(`${VERDICT.needs_human_look.icon} Needs a human look`, count("needs_human_look")),
    tile(`${VERDICT.likely_low_value.icon} Likely low value`, count("likely_low_value")),
    tile("Abstain rate", pct(rate)),
    tile("Avg confidence", pct(m.confidence.average), "Heuristic, not calibrated"),
    tile("Avg citation validity", pct(m.citation_validity_average), "Share of model citations that passed the verifier"),
    tile("Avg latency", latency === null ? "–" : `${(latency / 1000).toFixed(1)} s`, "Model calls only; cache hits excluded"),
  );
}

function verdictChart(m: Metrics): HTMLElement {
  const width = 300;
  const row = 26;
  const labelW = 132;
  const valueW = 28;
  const max = Math.max(1, ...VERDICTS.map((v) => m.by_verdict[v] ?? 0));
  const chart = svg("svg", {
    viewBox: `0 0 ${width} ${row * VERDICTS.length}`,
    class: "chart",
    role: "img",
    "aria-label": `Verdicts: ${VERDICTS.map((v) => `${VERDICT[v].text} ${m.by_verdict[v] ?? 0}`).join(", ")}`,
  });
  VERDICTS.forEach((verdict, i) => {
    const value = m.by_verdict[verdict] ?? 0;
    const y = i * row;
    const w = Math.max(value ? 2 : 0, ((width - labelW - valueW - 8) * value) / max);
    chart.append(
      svg("text", { x: 0, y: y + 17, class: "chart-label" }, `${VERDICT[verdict].icon} ${VERDICT[verdict].text}`),
      svg("path", { d: barPath(labelW, y + 6, w, row - 12, true), class: "chart-mark" }, title(`${VERDICT[verdict].text}: ${value}`)),
      svg("text", { x: labelW + w + 6, y: y + 17, class: "chart-value" }, String(value)),
    );
  });
  return h("figure", { class: "figure" }, h("figcaption", null, "Verdicts"), chart);
}

function confidenceChart(m: Metrics): HTMLElement {
  const recent = [...m.recent].reverse(); // oldest first
  const width = 300;
  const height = 96;
  const top = 8;
  const bottom = 16;
  const left = 30;
  const plot = height - top - bottom;
  const slots = Math.max(recent.length, 1);
  const slot = (width - left) / slots;
  const barW = Math.max(2, Math.min(14, slot - 2)); // 2px gap between bars
  const values = recent.map((r) => r.confidence).filter((v): v is number => v !== null);
  const summary = values.length
    ? `Confidence of the last ${recent.length} analyses, from ${pct(Math.min(...values))} to ${pct(Math.max(...values))}; latest ${pct(recent[recent.length - 1]?.confidence)}`
    : "No confidence values yet";
  const chart = svg("svg", { viewBox: `0 0 ${width} ${height}`, class: "chart", role: "img", "aria-label": summary });
  for (const tick of [0, 0.5, 1]) {
    const y = top + plot - tick * plot;
    chart.append(
      svg("line", { x1: left, x2: width, y1: y, y2: y, class: "chart-grid" }),
      svg("text", { x: left - 4, y: y + 4, class: "chart-axis", "text-anchor": "end" }, pct(tick)),
    );
  }
  recent.forEach((r, i) => {
    const x = left + i * slot + (slot - barW) / 2;
    const label = `${r.repo}#${r.number}: ${r.confidence === null ? "no confidence" : pct(r.confidence)} (${r.verdict ? VERDICT[r.verdict].text : r.status})`;
    if (r.confidence === null) {
      chart.append(svg("rect", { x, y: top + plot - 2, width: barW, height: 2, class: "chart-empty" }, title(label)));
      return;
    }
    const hgt = Math.max(2, r.confidence * plot);
    chart.append(svg("path", { d: barPath(x, top + plot - hgt, barW, hgt, false), class: "chart-mark" }, title(label)));
  });
  chart.append(svg("text", { x: left, y: height - 2, class: "chart-axis" }, "older → newer"));
  return h("figure", { class: "figure" }, h("figcaption", null, "Confidence, recent analyses"), chart);
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
          h("span", null, verdict ? `${verdict.icon} ${verdict.text}` : `⚠ ${r.status}`),
          h("span", { class: "num" }, pct(r.confidence)),
          h("time", { datetime: r.created_at, class: "muted" }, formatTime(r.created_at)),
        ),
      );
    }),
  );
}

function modelSection(state: State): HTMLElement {
  const box = h(
    "section",
    { class: "model-input", "aria-labelledby": "model-heading" },
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
      "p",
      { class: "muted small" },
      `${modelInput.model ?? "model"} (${modelInput.backend ?? "backend"}) · prompt ${modelInput.prompt_id} v${modelInput.prompt_version}`,
    ),
    h("details", null, h("summary", null, "System instructions"), h("pre", { class: "scroll" }, modelInput.system)),
    h("details", { open: true }, h("summary", null, "Input (redacted, exactly as stored in the audit trail)"), h("pre", { class: "scroll" }, modelInput.user)),
    h("p", { class: "muted small" }, modelInput.schema_note),
  );
  return box;
}

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

let painted: unknown[] = [];

function paint(root: HTMLElement, state: State, force = false): void {
  const inputs = [metrics, metricsError, modelInput, modelNote, state.analysisId, state.view];
  if (!force && inputs.length === painted.length && inputs.every((v, i) => v === painted[i])) return;
  painted = inputs;
  const body = root.querySelector("#insights-body") as HTMLElement;
  const parts: HTMLElement[] = [];
  if (metricsError) parts.push(h("p", { class: "state-line" }, metricsError));
  if (metrics) {
    parts.push(kpis(metrics), verdictChart(metrics), confidenceChart(metrics));
    parts.push(h("h3", null, "Recent analyses"), recentList(metrics));
    if (metrics.models.length) parts.push(h("p", { class: "muted small" }, `Models used: ${metrics.models.join(", ")}`));
  } else if (!metricsError) {
    parts.push(h("p", { class: "muted" }, "Loading metrics…"));
  }
  parts.push(modelSection(state));
  body.replaceChildren(...parts);
}

export function renderInsights(root: HTMLElement, state: State, insightsDeps: InsightsDeps): void {
  deps = insightsDeps;
  if (mounted !== root) {
    mounted = root;
    root.replaceChildren(
      h(
        "div",
        { class: "section-head" },
        h("h3", null, "Last 30 days"),
        h(
          "button",
          {
            type: "button",
            class: "quiet",
            on: {
              click: () => {
                metricsAt = 0;
                void loadMetrics().then(() => paint(root, store.get(), true));
              },
            },
          },
          "Refresh",
        ),
      ),
      h("div", { id: "insights-body" }),
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
