// Pure derivations for the side panel: the five gates a changed file passes, the file
// tree, and run facts replayed from the activity feed. No DOM and type-only imports,
// so `node --test` runs this module as is (see extension/tests).
import type { ActivityEvent, FileState, PrFile, TreeEntry } from "../shared/types";

// -- gates ---------------------------------------------------------------------------

/** The gates a changed file passes, in pipeline order. */
export const GATES = ["listed", "checked", "read", "cited", "verified"] as const;
export type Gate = (typeof GATES)[number];
/** Tree filters offered by the pipeline view: a gate reached, or one of the two exits. */
export type GateFilter = Gate | "left_out" | "dropped";
export type Segment = "done" | "ok" | "blocked" | "todo";

/** How many gates each per-file state has passed. */
const PASSED: Record<FileState, number> = {
  listed: 1,
  signalled: 2,
  omitted: 2,
  in_context: 3,
  cited: 4,
  dropped: 4,
  verified: 5,
};

export function gatesPassed(state: FileState): number {
  return PASSED[state];
}

export function matchesGate(state: FileState, filter: GateFilter): boolean {
  if (filter === "left_out") return state === "omitted";
  if (filter === "dropped") return state === "dropped";
  return PASSED[state] > GATES.indexOf(filter);
}

/** One segment per gate: passed, passed the last gate, stopped at it, or not reached. */
export function trail(state: FileState): Segment[] {
  const passed = PASSED[state];
  const stopped = state === "omitted" || state === "dropped";
  return GATES.map((_, i) => {
    if (i < passed) return i === GATES.length - 1 ? "ok" : "done";
    return i === passed && stopped ? "blocked" : "todo";
  });
}

export type Funnel = Record<GateFilter, number> & { total: number };

/** How many changed files reached each gate (cumulative), plus the two exits. */
export function funnel(states: Record<string, FileState>): Funnel {
  const out: Funnel = {
    total: 0,
    listed: 0,
    checked: 0,
    read: 0,
    cited: 0,
    verified: 0,
    left_out: 0,
    dropped: 0,
  };
  for (const state of Object.values(states)) {
    out.total += 1;
    for (const key of [...GATES, "left_out", "dropped"] as const) {
      if (matchesGate(state, key)) out[key] += 1;
    }
  }
  return out;
}

// -- the file tree -------------------------------------------------------------------

export interface TreeNode {
  path: string;
  /** The display name; a chain of single-folder parents is shown as "a/b/c". */
  name: string;
  type: "tree" | "blob" | "commit";
  status?: string;
  children: TreeNode[];
  /** Changed files at or below this node. */
  changed: number;
}

export interface TreeOptions {
  showAll: boolean;
  filter: string;
  gate: GateFilter | null;
  states: Record<string, FileState>;
}

export function ancestors(path: string): string[] {
  const parts = path.split("/").slice(0, -1);
  return parts.map((_, i) => parts.slice(0, i + 1).join("/"));
}

/** Entries from the PR's own file list, when the repo tree is unavailable. */
export function entriesFromFiles(files: PrFile[]): TreeEntry[] {
  const out = new Map<string, TreeEntry>();
  for (const file of files) {
    for (const folder of ancestors(file.path)) out.set(folder, { path: folder, type: "tree" });
    out.set(file.path, { path: file.path, type: "blob", status: file.status });
  }
  return [...out.values()];
}

/** Build the visible tree. A gate filter shows only the changed files it matches. */
export function buildNodes(entries: TreeEntry[], options: TreeOptions): TreeNode[] {
  const { filter, gate, states } = options;
  const showAll = options.showAll && !gate;
  const needle = filter.trim().toLowerCase();
  const keep = new Set<string>();
  for (const entry of entries) {
    if (!showAll && !entry.status) continue;
    if (gate && !matchesGate(states[entry.path] ?? "listed", gate)) continue;
    if (needle && (entry.type === "tree" || !entry.path.toLowerCase().includes(needle))) continue;
    keep.add(entry.path);
    for (const folder of ancestors(entry.path)) keep.add(folder);
  }
  if (showAll && !needle) for (const entry of entries) keep.add(entry.path);

  const nodes = new Map<string, TreeNode>();
  for (const entry of entries) {
    if (!keep.has(entry.path)) continue;
    nodes.set(entry.path, {
      path: entry.path,
      name: entry.path.split("/").pop() ?? entry.path,
      type: entry.type,
      status: entry.status,
      children: [],
      changed: 0,
    });
  }
  for (const path of keep) {
    if (!nodes.has(path)) {
      nodes.set(path, { path, name: path.split("/").pop() ?? path, type: "tree", children: [], changed: 0 });
    }
  }
  const roots: TreeNode[] = [];
  for (const node of nodes.values()) {
    const parent = ancestors(node.path).pop();
    const owner = parent !== undefined ? nodes.get(parent) : undefined;
    (owner ? owner.children : roots).push(node);
  }
  const finish = (list: TreeNode[]): number => {
    list.sort((a, b) =>
      a.type === "tree" && b.type !== "tree"
        ? -1
        : a.type !== "tree" && b.type === "tree"
          ? 1
          : a.name.localeCompare(b.name),
    );
    let count = 0;
    for (const node of list) {
      node.changed = node.type === "tree" ? finish(node.children) : node.status ? 1 : 0;
      count += node.changed;
    }
    return count;
  };
  finish(roots);
  compact(roots);
  return roots;
}

/** Merge each folder whose only child is a folder into one row ("src/panel"). The row
 * keeps the deepest path, so expanding, selecting and GitHub links use that folder. */
function compact(list: TreeNode[]): void {
  for (const node of list) {
    while (node.type === "tree" && node.children.length === 1 && node.children[0].type === "tree") {
      const only = node.children[0];
      node.name = `${node.name}/${only.name}`;
      node.path = only.path;
      node.children = only.children;
    }
    compact(node.children);
  }
}

/** Folders that start expanded: every ancestor of a changed file. */
export function initialExpanded(entries: TreeEntry[]): Set<string> {
  const out = new Set<string>();
  for (const entry of entries) if (entry.status) for (const f of ancestors(entry.path)) out.add(f);
  return out;
}

/** Every folder path, for "Expand all". */
export function folderPaths(entries: TreeEntry[]): Set<string> {
  const out = new Set<string>();
  for (const entry of entries) {
    if (entry.type === "tree") out.add(entry.path);
    for (const folder of ancestors(entry.path)) out.add(folder);
  }
  return out;
}

// -- run facts, replayed from the activity feed ------------------------------------

/** The pipeline steps, in order (mirrors gatekeeper.pipeline.core.STEP_LABELS). */
export const STEPS = ["intake", "signals", "review", "verify", "decision"] as const;
export type StepName = (typeof STEPS)[number];
export type StepStatus = "pending" | "running" | "done" | "failed" | "skipped";
/** live: still running; done: finished or abstained; failed: the run failed. */
export type Outcome = "live" | "done" | "failed";

export interface StepFacts {
  name: StepName;
  status: StepStatus;
  ms: number | null;
}

export interface RunFacts {
  steps: StepFacts[];
  current: StepName | null;
  startedAt: string | null;
  runMs: number | null;
  failure: { code: string | null; step: string | null } | null;
  signals: { positive: string[]; negative: string[]; neutral: number; truncated: boolean } | null;
  model: {
    name: string | null;
    latencyMs: number | null;
    inputTokens: number | null;
    outputTokens: number | null;
    cacheHit: boolean;
    failovers: number | null;
    evidence: number;
  } | null;
  confidence: {
    value: number;
    threshold: number | null;
    selfConfidence: number | null;
    citationValidity: number | null;
    agreement: number | null;
    truncated: boolean | null;
  } | null;
  decision: {
    verdict: string | null;
    status: string | null;
    modelVerdict: string | null;
    downgradeReason: string | null;
  } | null;
  diffTruncated: boolean;
}

export function isStep(value: unknown): value is StepName {
  return typeof value === "string" && (STEPS as readonly string[]).includes(value);
}

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const str = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const bool = (v: unknown): boolean | null => (typeof v === "boolean" ? v : null);
const strings = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x) => typeof x === "string") : []);

export function msBetween(from: string | null, to: string | null): number | null {
  if (!from || !to) return null;
  const ms = Date.parse(to) - Date.parse(from);
  return Number.isFinite(ms) && ms >= 0 ? ms : null;
}

export function deriveRun(events: readonly ActivityEvent[], outcome: Outcome): RunFacts {
  const steps = new Map<StepName, StepFacts>(STEPS.map((name) => [name, { name, status: "pending", ms: null }]));
  const facts: RunFacts = {
    steps: [],
    current: null,
    startedAt: null,
    runMs: null,
    failure: null,
    signals: null,
    model: null,
    confidence: null,
    decision: null,
    diffTruncated: false,
  };
  let endedAt: string | null = null;
  let sawStep = false;
  let modelName: string | null = null;
  for (const event of events) {
    const step = isStep(event.step) ? steps.get(event.step) : undefined;
    const d = event.data ?? {};
    switch (event.kind) {
      case "run_started":
        facts.startedAt = event.ts;
        break;
      case "step_started":
        if (step) {
          sawStep = true;
          step.status = "running";
          facts.current = step.name;
        }
        break;
      case "step_finished":
        if (step) {
          sawStep = true;
          step.status = "done";
          step.ms = num(d.duration_ms);
        }
        break;
      case "files_listed":
        facts.diffTruncated = d.diff_truncated === true;
        break;
      case "signals_summary":
        facts.signals = {
          positive: strings(d.positive),
          negative: strings(d.negative),
          neutral: num(d.neutral) ?? 0,
          truncated: d.truncated === true,
        };
        break;
      case "model_call_started":
        modelName = str(d.model);
        break;
      case "model_call_finished":
        facts.model = {
          name: str(d.model) ?? modelName,
          latencyMs: num(d.latency_ms),
          inputTokens: num(d.input_tokens),
          outputTokens: num(d.output_tokens),
          cacheHit: d.cache_hit === true,
          failovers: num(d.failovers),
          evidence: num(d.evidence) ?? 0,
        };
        break;
      case "confidence_computed": {
        const value = num(d.confidence);
        if (value !== null) {
          facts.confidence = {
            value,
            threshold: num(d.threshold),
            selfConfidence: num(d.self_confidence),
            citationValidity: num(d.citation_validity),
            agreement: num(d.agreement),
            truncated: bool(d.truncated),
          };
        }
        break;
      }
      case "verdict":
        facts.decision = {
          verdict: str(d.verdict),
          status: str(d.status),
          modelVerdict: str(d.model_verdict),
          downgradeReason: str(d.downgrade_reason),
        };
        break;
      case "run_failed":
        facts.failure = { code: str(d.error_code), step: str(d.step) ?? event.step };
        endedAt = event.ts;
        break;
      case "run_finished":
        endedAt = event.ts;
        break;
    }
  }
  if (facts.failure) {
    const failed = isStep(facts.failure.step) ? steps.get(facts.failure.step) : undefined;
    const target = failed ?? [...steps.values()].find((s) => s.status === "running");
    if (target) target.status = "failed";
  }
  if (outcome !== "live") {
    for (const step of steps.values()) {
      if (!sawStep && outcome === "done") step.status = "done"; // stored before the feed existed
      else if (step.status === "running") step.status = outcome === "failed" ? "failed" : "done";
      else if (step.status === "pending") step.status = "skipped";
    }
    facts.current = null;
  }
  facts.steps = [...steps.values()];
  facts.runMs = msBetween(facts.startedAt, endedAt);
  return facts;
}

// -- the timeline --------------------------------------------------------------------

export interface EventGroup {
  /** Stable while events only append: the first event's id. */
  id: string;
  step: StepName | null;
  events: ActivityEvent[];
  warnings: number;
}

/** Consecutive events of the same step form one group; run-level events (start,
 * approval, finish) form their own groups between them. */
export function groupEvents(events: readonly ActivityEvent[]): EventGroup[] {
  const groups: EventGroup[] = [];
  for (const event of events) {
    const step = isStep(event.step) ? event.step : null;
    let group = groups[groups.length - 1];
    if (!group || group.step !== step) {
      group = { id: `g${event.id}`, step, events: [], warnings: 0 };
      groups.push(group);
    }
    group.events.push(event);
    if (event.level !== "info") group.warnings += 1;
  }
  return groups;
}
