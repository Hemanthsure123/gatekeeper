// One small state store for the side panel: get, set (shallow merge), subscribe.
import type { Settings } from "../shared/settings";
import type { ActivityEvent, FileState, PrView, TreeResponse } from "../shared/types";

export type View =
  | "loading"
  | "not_connected"
  | "not_pr"
  | "not_analysed"
  | "analysing"
  | "result";
export type TabName = "activity" | "chat" | "insights";

export interface PrRef {
  owner: string;
  name: string;
  number: number;
}

export interface State {
  view: View;
  /** One plain line explaining the current state or the last error. */
  line: string;
  settings: Settings | null;
  windowId: number | null;
  pageUrl: string;
  ref: PrRef | null;
  pr: PrView | null;
  tree: TreeResponse | null;
  treeNote: string;
  events: ActivityEvent[];
  fileStates: Record<string, FileState>;
  /** Files whose stage changed a moment ago (subtle animation). */
  bumped: Set<string>;
  now: string;
  currentPath: string | null;
  selected: string | null;
  showAll: boolean;
  filter: string;
  expanded: Set<string>;
  tab: TabName;
  /** The analysis being shown or followed live. */
  analysisId: string | null;
  chatFocus: string | null;
}

const initial: State = {
  view: "loading",
  line: "Connecting…",
  settings: null,
  windowId: null,
  pageUrl: "",
  ref: null,
  pr: null,
  tree: null,
  treeNote: "",
  events: [],
  fileStates: {},
  bumped: new Set(),
  now: "",
  currentPath: null,
  selected: null,
  showAll: false,
  filter: "",
  expanded: new Set(),
  tab: "activity",
  analysisId: null,
  chatFocus: null,
};

type Listener = (state: State) => void;
let state: State = { ...initial };
const listeners = new Set<Listener>();
let scheduled = false;

export const store = {
  get(): State {
    return state;
  },
  set(patch: Partial<State>): void {
    state = { ...state, ...patch };
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      for (const listener of listeners) listener(state);
    });
  },
  subscribe(listener: Listener): void {
    listeners.add(listener);
  },
  /** Clear everything that belongs to one pull request. */
  resetPr(patch: Partial<State> = {}): void {
    store.set({
      ref: null,
      pr: null,
      tree: null,
      treeNote: "",
      events: [],
      fileStates: {},
      bumped: new Set(),
      now: "",
      currentPath: null,
      selected: null,
      expanded: new Set(),
      analysisId: null,
      chatFocus: null,
      ...patch,
    });
  },
};

// -- per-file progress, mirroring gatekeeper.activity.file_states ------------------------

const RANK: Record<FileState, number> = {
  listed: 0,
  signalled: 1,
  in_context: 2,
  omitted: 2,
  cited: 3,
  dropped: 4,
  verified: 5,
};
const STATE_BY_KIND: Record<string, FileState> = {
  file_signal: "signalled",
  file_cited: "cited",
  citation_verified: "verified",
  citation_dropped: "dropped",
};

function advance(states: Record<string, FileState>, path: string, next: FileState): boolean {
  const current = states[path];
  if (current === undefined || RANK[next] > RANK[current]) {
    states[path] = next;
    return true;
  }
  return false;
}

/** Apply one event to a copy of the per-file states; returns the paths that changed. */
export function applyToStates(
  states: Record<string, FileState>,
  event: ActivityEvent,
): string[] {
  const changed: string[] = [];
  if (event.kind === "files_listed") {
    const files = (event.data.files as Array<{ path?: unknown }> | undefined) ?? [];
    for (const file of files) {
      if (typeof file.path === "string" && advance(states, file.path, "listed")) {
        changed.push(file.path);
      }
    }
    return changed;
  }
  if (!event.path) return changed;
  let next: FileState | undefined;
  if (event.kind === "file_context") next = event.data.included ? "in_context" : "omitted";
  else next = STATE_BY_KIND[event.kind];
  if (next && advance(states, event.path, next)) changed.push(event.path);
  return changed;
}
