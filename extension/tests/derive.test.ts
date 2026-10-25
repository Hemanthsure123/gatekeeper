// Unit tests for the panel's pure derivations. Run with `npm test` (node:test; Node
// strips the TypeScript types, so there is no build step and no test dependency).
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  buildNodes,
  deriveRun,
  folderPaths,
  funnel,
  groupEvents,
  initialExpanded,
  matchesGate,
  trail,
} from "../src/panel/derive.ts";
import type { ActivityEvent, FileState, TreeEntry } from "../src/shared/types.ts";

let nextId = 0;
function event(kind: string, extra: Partial<ActivityEvent> = {}): ActivityEvent {
  nextId += 1;
  return {
    id: nextId,
    ts: new Date(Date.UTC(2026, 9, 25, 10, 0, 0, nextId * 100)).toISOString(),
    kind,
    step: null,
    path: null,
    level: "info",
    message: kind,
    data: {},
    ...extra,
  };
}

const ENTRIES: TreeEntry[] = [
  { path: "src", type: "tree" },
  { path: "src/app", type: "tree" },
  { path: "src/app/core", type: "tree" },
  { path: "src/app/core/parser.ts", type: "blob", status: "modified" },
  { path: "src/app/core/lexer.ts", type: "blob" },
  { path: "docs", type: "tree" },
  { path: "docs/guide.md", type: "blob", status: "added" },
  { path: "docs/old.md", type: "blob" },
  { path: "README.md", type: "blob" },
];

const NO_FILTER = { showAll: false, filter: "", gate: null, states: {} };

describe("gates", () => {
  it("marks the segments a file passed, stopped at, or never reached", () => {
    assert.deepEqual(trail("listed"), ["done", "todo", "todo", "todo", "todo"]);
    assert.deepEqual(trail("omitted"), ["done", "done", "blocked", "todo", "todo"]);
    assert.deepEqual(trail("in_context"), ["done", "done", "done", "todo", "todo"]);
    assert.deepEqual(trail("dropped"), ["done", "done", "done", "done", "blocked"]);
    assert.deepEqual(trail("verified"), ["done", "done", "done", "done", "ok"]);
  });

  it("matches gates cumulatively and the exits exactly", () => {
    assert.equal(matchesGate("verified", "read"), true);
    assert.equal(matchesGate("dropped", "cited"), true);
    assert.equal(matchesGate("dropped", "verified"), false);
    assert.equal(matchesGate("omitted", "read"), false);
    assert.equal(matchesGate("omitted", "checked"), true);
    assert.equal(matchesGate("omitted", "left_out"), true);
    assert.equal(matchesGate("verified", "dropped"), false);
  });

  it("counts how many files reached each gate", () => {
    const states: Record<string, FileState> = {
      a: "verified",
      b: "dropped",
      c: "in_context",
      d: "omitted",
      e: "signalled",
    };
    assert.deepEqual(funnel(states), {
      total: 5,
      listed: 5,
      checked: 5,
      read: 3,
      cited: 2,
      verified: 1,
      left_out: 1,
      dropped: 1,
    });
  });
});

describe("tree", () => {
  it("shows only changed files by default and compacts single-folder chains", () => {
    const roots = buildNodes(ENTRIES, NO_FILTER);
    assert.deepEqual(
      roots.map((n) => [n.name, n.path, n.changed]),
      [
        ["docs", "docs", 1],
        ["src/app/core", "src/app/core", 1],
      ],
    );
    assert.deepEqual(roots[1].children.map((n) => n.name), ["parser.ts"]);
  });

  it("does not compact a folder that holds files beside its subfolder", () => {
    const roots = buildNodes(ENTRIES, { ...NO_FILTER, showAll: true });
    assert.deepEqual(roots.map((n) => n.name), ["docs", "src/app/core", "README.md"]);
    assert.deepEqual(roots[1].children.map((n) => n.name), ["lexer.ts", "parser.ts"]);
    assert.equal(roots[0].changed, 1);
  });

  it("filters by path text and keeps the ancestors", () => {
    const roots = buildNodes(ENTRIES, { ...NO_FILTER, showAll: true, filter: "PARSER" });
    assert.deepEqual(roots.map((n) => n.path), ["src/app/core"]);
    assert.deepEqual(roots[0].children.map((n) => n.path), ["src/app/core/parser.ts"]);
  });

  it("filters by gate, even when all files are shown", () => {
    const states: Record<string, FileState> = { "src/app/core/parser.ts": "verified", "docs/guide.md": "omitted" };
    const left = buildNodes(ENTRIES, { showAll: true, filter: "", gate: "left_out", states });
    assert.deepEqual(left.map((n) => n.path), ["docs"]);
    assert.deepEqual(left[0].children.map((n) => n.path), ["docs/guide.md"]);
    const none = buildNodes(ENTRIES, { showAll: false, filter: "", gate: "dropped", states });
    assert.deepEqual(none, []);
  });

  it("expands the ancestors of changed files, and lists every folder for expand-all", () => {
    assert.deepEqual([...initialExpanded(ENTRIES)].sort(), ["docs", "src", "src/app", "src/app/core"]);
    assert.deepEqual([...folderPaths(ENTRIES)].sort(), ["docs", "src", "src/app", "src/app/core"]);
  });
});

describe("run facts", () => {
  const run = [
    event("run_started"),
    event("step_started", { step: "intake" }),
    event("files_listed", { step: "intake", data: { files: [], diff_truncated: true } }),
    event("step_finished", { step: "intake", data: { duration_ms: 12.5 } }),
    event("step_started", { step: "signals" }),
    event("signals_summary", { step: "signals", data: { positive: ["small_change"], negative: [], neutral: 2 } }),
    event("step_finished", { step: "signals", data: { duration_ms: 3 } }),
    event("step_started", { step: "review" }),
    event("model_call_started", { step: "review", data: { model: "gemma-4" } }),
    event("model_call_finished", {
      step: "review",
      data: { latency_ms: 4200, input_tokens: 3200, output_tokens: 410, cache_hit: false, evidence: 2 },
    }),
  ];

  it("follows a live run step by step", () => {
    const facts = deriveRun(run, "live");
    assert.deepEqual(
      facts.steps.map((s) => [s.name, s.status, s.ms]),
      [
        ["intake", "done", 12.5],
        ["signals", "done", 3],
        ["review", "running", null],
        ["verify", "pending", null],
        ["decision", "pending", null],
      ],
    );
    assert.equal(facts.current, "review");
    assert.equal(facts.diffTruncated, true);
    assert.deepEqual(facts.signals, { positive: ["small_change"], negative: [], neutral: 2, truncated: false });
    assert.equal(facts.model?.name, "gemma-4");
    assert.equal(facts.model?.inputTokens, 3200);
    assert.equal(facts.runMs, null);
  });

  it("reads the confidence inputs, the decision and the run time once finished", () => {
    const finished = [
      ...run,
      event("step_finished", { step: "review", data: { duration_ms: 4300 } }),
      event("step_started", { step: "verify" }),
      event("step_finished", { step: "verify", data: { duration_ms: 1 } }),
      event("step_started", { step: "decision" }),
      event("confidence_computed", {
        step: "decision",
        data: { confidence: 0.72, threshold: 0.6, self_confidence: 0.8, citation_validity: 1, agreement: 0.5, truncated: false },
      }),
      event("verdict", {
        step: "decision",
        data: { verdict: "needs_human_look", status: "completed", model_verdict: "likely_low_value", downgrade_reason: "no_verified_concern" },
      }),
      event("step_finished", { step: "decision", data: { duration_ms: 2 } }),
      event("run_finished"),
    ];
    const facts = deriveRun(finished, "done");
    assert.ok(facts.steps.every((s) => s.status === "done"));
    assert.equal(facts.current, null);
    assert.deepEqual(facts.confidence, {
      value: 0.72,
      threshold: 0.6,
      selfConfidence: 0.8,
      citationValidity: 1,
      agreement: 0.5,
      truncated: false,
    });
    assert.equal(facts.decision?.downgradeReason, "no_verified_concern");
    assert.equal(facts.runMs, (finished.length - 1) * 100);
  });

  it("marks the failing step and skips the rest", () => {
    const failed = [...run, event("run_failed", { step: "review", level: "error", data: { error_code: "model_timeout", step: "review" } })];
    const facts = deriveRun(failed, "failed");
    assert.deepEqual(
      facts.steps.map((s) => s.status),
      ["done", "done", "failed", "skipped", "skipped"],
    );
    assert.deepEqual(facts.failure, { code: "model_timeout", step: "review" });
  });

  it("treats a finished analysis without feed events as done", () => {
    const facts = deriveRun([], "done");
    assert.ok(facts.steps.every((s) => s.status === "done" && s.ms === null));
  });

  it("ignores malformed event data instead of guessing", () => {
    const facts = deriveRun([event("confidence_computed", { data: { confidence: "high" } })], "live");
    assert.equal(facts.confidence, null);
  });
});

describe("timeline groups", () => {
  it("groups consecutive events of one step and counts warnings", () => {
    const events = [
      event("run_started"),
      event("step_started", { step: "intake" }),
      event("file_context", { step: "intake", level: "warn" }),
      event("step_started", { step: "signals" }),
      event("run_finished"),
    ];
    const groups = groupEvents(events);
    assert.deepEqual(
      groups.map((g) => [g.step, g.events.length, g.warnings]),
      [
        [null, 1, 0],
        ["intake", 2, 1],
        ["signals", 1, 0],
        [null, 1, 0],
      ],
    );
    assert.equal(groups[1].id, `g${events[1].id}`);
  });
});
