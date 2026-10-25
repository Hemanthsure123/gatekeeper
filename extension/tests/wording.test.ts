// Safety rule P04/P06: every fixed panel string is neutral and describes the work,
// never the person. The verdict wording is fixed.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import * as labels from "../src/panel/labels.ts";
import { applyToStates } from "../src/panel/store.ts";
import type { ActivityEvent, FileState } from "../src/shared/types.ts";

/** A copy of BANNED_WORDS in gatekeeper/review_core/reply.py (the single list); keep in sync. */
const BANNED_WORDS = [
  "spam", "spammer", "spammy", "spamming", "bot", "bots", "ai-generated",
  "ai generated", "chatgpt", "llm-generated", "machine-generated", "fake", "fraud",
  "scam", "lazy", "garbage", "junk", "trash", "rubbish", "worthless", "useless",
  "low-effort", "low effort", "stupid", "dumb", "idiot", "idiotic", "clueless",
  "incompetent", "sloppy", "troll", "farming", "cheater", "cheating",
];
const BANNED = BANNED_WORDS.map(
  (word) =>
    new RegExp(`(?<![A-Za-z0-9])${word.replace(/[.*+?^${}()|[\]\\-]/g, "\\$&").replace(/ /g, "[\\s-]+")}(?![A-Za-z0-9])`, "i"),
);

function strings(value: unknown): string[] {
  if (typeof value === "string") return [value];
  if (value && typeof value === "object") return Object.values(value).flatMap(strings);
  return [];
}

describe("panel wording", () => {
  it("uses the fixed verdict wording", () => {
    assert.deepEqual(
      Object.values(labels.VERDICT).map((v) => v.text),
      ["Looks valuable", "Needs a human look", "Likely low value"],
    );
  });

  it("contains no banned or accusatory word", () => {
    const all = Object.values(labels).flatMap(strings);
    assert.ok(all.length > 50);
    for (const text of all) {
      for (const pattern of BANNED) assert.doesNotMatch(text, pattern, text);
    }
  });

  it("never claims the hosted backend is local", () => {
    assert.doesNotMatch(labels.BACKEND.gemini_api, /local/i);
  });
});

describe("per-file progress", () => {
  const ev = (kind: string, path: string | null, data: Record<string, unknown> = {}): ActivityEvent => ({
    id: 1,
    ts: "2026-10-25T10:00:00Z",
    kind,
    step: null,
    path,
    level: "info",
    message: kind,
    data,
  });

  it("only moves a file forward", () => {
    const states: Record<string, FileState> = {};
    assert.deepEqual(applyToStates(states, ev("files_listed", null, { files: [{ path: "a.ts" }] })), ["a.ts"]);
    applyToStates(states, ev("file_context", "a.ts", { included: true }));
    applyToStates(states, ev("citation_verified", "a.ts"));
    assert.deepEqual(applyToStates(states, ev("citation_dropped", "a.ts")), []);
    assert.equal(states["a.ts"], "verified");
  });
});
