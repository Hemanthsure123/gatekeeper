// Side panel entry: detects the GitHub page in the active tab, asks the API about it,
// and shows exactly one of: Not connected, Not a PR page (or not enabled for this repo),
// Not analysed, Analysing (live), Result.
import { loadSettings } from "../shared/settings";
import type { ActivityEvent, PrView } from "../shared/types";
import { renderDrawer, renderNow, renderTimeline, renderVerdict } from "./activity";
import { Api, ApiError } from "./api";
import { renderChat } from "./chat";
import { renderInsights } from "./insights";
import { openBlob, openFolder, openPr, revealFile } from "./reveal";
import { applyToStates, type PrRef, type State, store, type TabName, type View } from "./store";
import { ancestors, initialExpanded, entriesFromFiles, renderTree, treeKeys } from "./tree";

const PLAYBACK_MS = 120;
const BUMP_MS = 700;
const POLL_MS = 1500;
const STILL_QUEUED_MS = 20000;
const TERMINAL = new Set(["run_finished", "run_failed"]);

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;

let api: Api | null = null;
let stopStream: (() => void) | null = null;
let queue: ActivityEvent[] = [];
let playTimer: number | null = null;
let pollTimer: number | null = null;
let lastApplied = 0;
let contextTimer: number | null = null;
let generation = 0; // bumps on every PR switch; stale async results are ignored

// -- helpers -------------------------------------------------------------------------------

function setView(view: View, line = ""): void {
  store.set({ view, line });
}

function stopLive(): void {
  stopStream?.();
  stopStream = null;
  queue = [];
  if (playTimer !== null) clearInterval(playTimer);
  if (pollTimer !== null) clearInterval(pollTimer);
  playTimer = pollTimer = null;
  lastApplied = 0;
}

function explain(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 0) {
      const server = store.get().settings?.serverUrl ?? "the server";
      return `Cannot reach Gatekeeper at ${server}. Is it running (make dev)?`;
    }
    if (error.status === 401) return "The access token was rejected. Check it in Options.";
    if (error.code === "config_error") return `The server is not configured: ${error.message}`;
    return error.message;
  }
  return "Something went wrong.";
}

function connectionLost(error: unknown): void {
  stopLive();
  store.resetPr();
  setView("not_connected", explain(error));
}

function sameRepo(ref: PrRef, owner?: string, name?: string): boolean {
  return ref.owner.toLowerCase() === owner?.toLowerCase() && ref.name.toLowerCase() === name?.toLowerCase();
}

function nowText(event: ActivityEvent): string | null {
  const path = event.path ?? "";
  switch (event.kind) {
    case "run_started":
      return "Now: starting the review";
    case "step_started":
      return `Now: ${event.message.charAt(0).toLowerCase()}${event.message.slice(1)}`;
    case "files_listed":
      return "Now: listing the changed files";
    case "file_signal":
      return `Now: checking ${path}`;
    case "file_context":
      return event.data.included ? `Now: reading ${path}` : `Now: leaving out ${path}`;
    case "model_call_started":
      return "Now: asking the model";
    case "model_call_finished":
      return "Now: reading the model's answer";
    case "file_cited":
    case "citation_verified":
    case "citation_dropped":
      return `Now: verifying a citation in ${path}`;
    case "confidence_computed":
      return "Now: computing confidence";
    case "verdict":
    case "abstained":
      return "Now: writing the reply draft";
    default:
      return null;
  }
}

// -- live playback -------------------------------------------------------------------------

function applyEvent(event: ActivityEvent, animate: boolean): void {
  if (event.id <= lastApplied) return;
  lastApplied = event.id;
  const s = store.get();
  const fileStates = { ...s.fileStates };
  const changed = applyToStates(fileStates, event);
  const patch: Partial<State> = {
    events: [...s.events, event],
    fileStates,
    now: nowText(event) ?? s.now,
    currentPath: event.path ?? s.currentPath,
  };
  if (animate && changed.length) {
    patch.bumped = new Set(changed);
    window.setTimeout(() => store.set({ bumped: new Set() }), BUMP_MS);
  }
  if (event.kind === "run_started") patch.line = "Following the run live.";
  store.set(patch);
  if (TERMINAL.has(event.kind)) void finishRun();
}

function enqueue(event: ActivityEvent): void {
  queue.push(event);
  if (playTimer !== null) return;
  playTimer = window.setInterval(() => {
    const next = queue.shift();
    if (!next) {
      if (playTimer !== null) clearInterval(playTimer);
      playTimer = null;
      return;
    }
    applyEvent(next, true);
  }, PLAYBACK_MS);
}

async function follow(analysisId: string): Promise<void> {
  if (!api) return;
  const mine = generation;
  stopStream?.();
  store.set({ analysisId, events: [], now: "", currentPath: null });
  lastApplied = 0;
  try {
    const history = await api.activity(analysisId, 0);
    if (mine !== generation) return;
    for (const event of history.events) applyEvent(event, false);
    if (history.events.some((e) => TERMINAL.has(e.kind))) return;
  } catch (error) {
    if (mine === generation) store.set({ line: explain(error) });
  }
  stopStream = api.stream(analysisId, lastApplied, {
    onEvent: enqueue,
    onEnd: () => {
      if (mine === generation && store.get().view === "analysing") void finishRun();
    },
    onError: (message) => {
      if (mine === generation) store.set({ line: message });
    },
  });
}

async function finishRun(): Promise<void> {
  const { ref } = store.get();
  stopStream?.();
  stopStream = null;
  if (!ref || !api) return;
  const mine = generation;
  window.setTimeout(async () => {
    try {
      const pr = await api!.pr(ref.owner, ref.name, ref.number);
      if (mine === generation) showPr(pr, { keepEvents: true });
    } catch (error) {
      if (mine === generation) store.set({ line: explain(error) });
    }
  }, 300);
}

// -- loading -------------------------------------------------------------------------------

function showPr(pr: PrView, options: { keepEvents?: boolean } = {}): void {
  const s = store.get();
  const fileStates = { ...s.fileStates };
  for (const file of pr.files) fileStates[file.path] = file.state;
  const entries = s.tree?.entries ?? entriesFromFiles(pr.files);
  const expanded = s.expanded.size ? s.expanded : initialExpanded(entries);
  const running = pr.analysis.status === "queued" || pr.analysis.status === "running";
  store.set({ pr, fileStates, expanded, analysisId: pr.analysis.id });
  if (running) {
    setView(
      "analysing",
      pr.analysis.status === "queued" ? "Queued; waiting for the worker." : "Following the run live.",
    );
    void follow(pr.analysis.id);
    return;
  }
  setView("result");
  if (!options.keepEvents || !s.events.length) void loadHistory(pr.analysis.id);
}

async function loadHistory(analysisId: string): Promise<void> {
  if (!api) return;
  const mine = generation;
  try {
    const { events } = await api.activity(analysisId, 0);
    if (mine === generation) store.set({ events });
  } catch {
    // the timeline is optional for a finished review
  }
}

async function loadTree(ref: PrRef): Promise<void> {
  if (!api) return;
  const mine = generation;
  try {
    const tree = await api.tree(ref.owner, ref.name, ref.number);
    if (mine !== generation) return;
    const s = store.get();
    store.set({
      tree,
      expanded: new Set([...s.expanded, ...initialExpanded(tree.entries)]),
      treeNote: tree.truncated ? `Showing ${tree.entries.length} of ${tree.total} entries.` : "",
    });
  } catch (error) {
    if (mine === generation) {
      store.set({ treeNote: `Repository tree unavailable (${explain(error)}); showing changed files only.` });
    }
  }
}

async function loadPr(ref: PrRef): Promise<void> {
  if (!api) return;
  stopLive();
  generation += 1;
  const mine = generation;
  store.resetPr({ ref, showAll: false, filter: "" });
  setView("loading", "Loading…");
  void loadTree(ref);
  try {
    const pr = await api.pr(ref.owner, ref.name, ref.number);
    if (mine === generation) showPr(pr);
  } catch (error) {
    if (mine !== generation) return;
    if (error instanceof ApiError && error.status === 404) setView("not_analysed");
    else if (error instanceof ApiError && (error.status === 0 || error.status === 401)) connectionLost(error);
    else setView("not_pr", explain(error));
  }
}

async function refreshContext(): Promise<void> {
  const { pageUrl, ref } = store.get();
  if (!api) return;
  if (!pageUrl.startsWith("https://github.com/")) {
    if (!ref) setView("not_pr", "Open a pull request on github.com to see its review.");
    return;
  }
  let context;
  try {
    context = await api.context(pageUrl);
  } catch (error) {
    connectionLost(error);
    return;
  }
  const isPr = context.kind === "pr" || context.kind === "pr_files";
  if (!isPr) {
    // Sticky: browsing files of the same repository keeps the pull request open here.
    if (ref && sameRepo(ref, context.owner, context.name)) {
      if (store.get().view === "not_connected") await loadPr(ref);
      return;
    }
    stopLive();
    generation += 1;
    store.resetPr();
    setView(
      "not_pr",
      context.kind === "unsupported"
        ? "This is not a GitHub repository page."
        : "Not a pull request page. Open a pull request to see its review.",
    );
    return;
  }
  if (!context.allowed) {
    stopLive();
    generation += 1;
    store.resetPr();
    setView("not_pr", `Gatekeeper is not enabled for ${context.owner}/${context.name} (GATEKEEPER_ALLOWED_REPOS).`);
    return;
  }
  const next = { owner: context.owner!, name: context.name!, number: context.number! };
  const view = store.get().view;
  if (ref && sameRepo(ref, next.owner, next.name) && ref.number === next.number && view !== "not_connected") {
    return;
  }
  await loadPr(next);
}

function scheduleContext(url: string | undefined): void {
  if (!url) return;
  store.set({ pageUrl: url });
  if (contextTimer !== null) clearTimeout(contextTimer);
  contextTimer = window.setTimeout(() => void refreshContext(), 250);
}

// -- actions -------------------------------------------------------------------------------

async function analyse(): Promise<void> {
  const { ref, pr, view } = store.get();
  if (!ref || !api || view === "analysing") return;
  const force = view === "result" || pr !== null;
  const previous = pr?.analysis.id ?? null;
  stopLive();
  const mine = generation;
  const fileStates: State["fileStates"] = {};
  store.set({ events: [], now: "", currentPath: null, fileStates, selected: null });
  setView("analysing", "Queueing the review…");
  let response;
  try {
    response = await api.analyze(ref.owner, ref.name, ref.number, force);
  } catch (error) {
    setView(previous ? "result" : "not_analysed", explain(error));
    return;
  }
  if (mine !== generation) return;
  if (response.analysis_id) {
    store.set({ line: "Queued; waiting for the worker." });
    void follow(response.analysis_id);
    return;
  }
  const started = Date.now();
  store.set({ line: response.created ? "Queued; waiting for the worker." : "Already queued." });
  pollTimer = window.setInterval(async () => {
    if (mine !== generation || !api) return;
    if (Date.now() - started > STILL_QUEUED_MS) {
      store.set({ line: "Still queued. Is the worker running (make dev)?" });
    }
    try {
      const latest = await api.pr(ref.owner, ref.name, ref.number);
      if (latest.analysis.id !== previous) {
        if (pollTimer !== null) clearInterval(pollTimer);
        pollTimer = null;
        showPr(latest);
      }
    } catch {
      // not there yet
    }
  }, POLL_MS);
}

function selectFile(path: string): void {
  const s = store.get();
  const expanded = new Set(s.expanded);
  for (const folder of ancestors(path)) expanded.add(folder);
  store.set({ selected: path, expanded, tab: "activity" });
  window.setTimeout(() => {
    document.querySelector<HTMLElement>(`#tree [data-key="${CSS.escape(path)}"]`)?.scrollIntoView({ block: "nearest" });
  }, 50);
}

function revealMessage(result: { found: boolean; method: string } | null): void {
  if (!result) return;
  if (result.method === "blob") store.set({ line: "The diff was not found on the page; opened the file instead." });
  else if (!result.found) store.set({ line: "Could not reveal that file in the GitHub tab." });
}

function toggleFolder(path: string): void {
  const expanded = new Set(store.get().expanded);
  if (expanded.has(path)) expanded.delete(path);
  else expanded.add(path);
  store.set({ expanded });
}

const treeHandlers = {
  onFolder: (path: string) => {
    toggleFolder(path);
    void openFolder(path);
  },
  onFile: (path: string) => {
    selectFile(path);
    const changed = store.get().pr?.files.some((f) => f.path === path) ?? false;
    if (changed) void revealFile(path).then(revealMessage);
    else void openBlob(path);
  },
};

function revealRange(path: string, start: number, end: number): void {
  void revealFile(path, start, end, true).then(revealMessage);
}

function openInGitHub(path: string): void {
  const changed = store.get().pr?.files.some((f) => f.path === path) ?? false;
  if (changed) void revealFile(path, undefined, undefined, true).then(revealMessage);
  else void openBlob(path, true);
}

// -- rendering -----------------------------------------------------------------------------

let lastTreeKey: unknown[] = [];
let lastEvents: ActivityEvent[] | null = null;

function changedInputs(next: unknown[]): boolean {
  const changed = next.length !== lastTreeKey.length || next.some((v, i) => v !== lastTreeKey[i]);
  lastTreeKey = next;
  return changed;
}

function renderHeader(s: State): void {
  const conn = $("conn");
  const connected = s.view !== "not_connected" && s.view !== "loading";
  conn.className = `conn ${s.view === "not_connected" ? "bad" : connected ? "ok" : ""}`;
  conn.querySelector(".conn-text")!.textContent =
    s.view === "not_connected" ? "Not connected" : connected ? "Connected" : "Connecting";
  $("repo").textContent = s.ref ? `${s.ref.owner}/${s.ref.name}` : "Gatekeeper";
  const prLink = $<HTMLButtonElement>("pr-link");
  prLink.hidden = !s.ref;
  prLink.textContent = s.ref ? `#${s.ref.number}` : "";
  const title = $("pr-title");
  title.hidden = !s.pr;
  title.textContent = s.pr?.title ?? "";
  title.title = s.pr?.title ?? "";
  const button = $<HTMLButtonElement>("analyse");
  button.hidden = !["not_analysed", "analysing", "result"].includes(s.view);
  button.textContent = s.view === "not_analysed" ? "Analyse" : s.view === "analysing" ? "Analysing…" : "Re-analyse";
  button.disabled = s.view === "analysing";
}

function renderTabs(s: State): void {
  for (const name of ["activity", "chat", "insights"] as TabName[]) {
    const tab = $(`tab-${name}`);
    const active = s.tab === name;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    $(`panel-${name}`).hidden = !active;
  }
}

function render(s: State): void {
  renderHeader(s);
  renderTabs(s);
  const prShown = ["not_analysed", "analysing", "result"].includes(s.view);
  $("state-line").textContent = prShown ? (s.view === "analysing" ? "" : s.line) : s.line;
  $("pr-sections").hidden = !prShown;
  if (s.tab === "activity" && prShown) {
    renderVerdict($("verdict"), s, {});
    renderNow($("now"), s);
    $("show-changed").setAttribute("aria-pressed", String(!s.showAll));
    $("show-all").setAttribute("aria-pressed", String(s.showAll));
    const note = $("tree-note");
    note.textContent = s.treeNote;
    note.hidden = !s.treeNote;
    const key = [s.tree, s.pr, s.fileStates, s.expanded, s.selected, s.currentPath, s.bumped, s.showAll, s.filter, s.view];
    if (changedInputs(key)) renderTree($("tree"), s, treeHandlers);
    renderDrawer($("drawer"), s, {
      close: () => store.set({ selected: null }),
      openInGitHub,
      revealRange,
      ask: (path) => store.set({ tab: "chat", chatFocus: path }),
    });
    if (s.events !== lastEvents) {
      lastEvents = s.events;
      renderTimeline($("timeline"), s.events, selectFile);
    }
  }
  if (s.tab === "chat") renderChat($("panel-chat"), s, { api: () => api, revealRange });
  if (s.tab === "insights") renderInsights($("panel-insights"), s, { api: () => api });
}

// -- wiring --------------------------------------------------------------------------------

function wire(): void {
  store.subscribe(render);
  $("analyse").addEventListener("click", () => void analyse());
  $("options").addEventListener("click", () => void chrome.runtime.openOptionsPage());
  $("pr-link").addEventListener("click", () => {
    const { ref } = store.get();
    if (ref) void openPr(ref.owner, ref.name, ref.number);
  });
  $("show-changed").addEventListener("click", () => store.set({ showAll: false }));
  $("show-all").addEventListener("click", () => store.set({ showAll: true }));
  $<HTMLInputElement>("filter").addEventListener("input", (event) =>
    store.set({ filter: (event.target as HTMLInputElement).value }),
  );
  treeKeys($("tree"), toggleFolder, (path) => store.get().expanded.has(path));

  const tabs: TabName[] = ["activity", "chat", "insights"];
  for (const name of tabs) {
    const tab = $(`tab-${name}`);
    tab.addEventListener("click", () => store.set({ tab: name }));
    tab.addEventListener("keydown", (event) => {
      const index = tabs.indexOf(name);
      let next: TabName | null = null;
      if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
      if (event.key === "ArrowLeft") next = tabs[(index + tabs.length - 1) % tabs.length];
      if (!next) return;
      event.preventDefault();
      store.set({ tab: next });
      $(`tab-${next}`).focus();
    });
  }

  chrome.tabs.onActivated.addListener(async ({ tabId, windowId }) => {
    if (windowId !== store.get().windowId) return;
    const tab = await chrome.tabs.get(tabId).catch(() => undefined);
    scheduleContext(tab?.url);
  });
  chrome.tabs.onUpdated.addListener((_tabId, change, tab) => {
    if (tab.windowId !== store.get().windowId || !tab.active || !change.url) return;
    scheduleContext(change.url);
  });
  chrome.storage.onChanged.addListener((_changes, area) => {
    if (area === "local") void boot();
  });
}

async function boot(): Promise<void> {
  stopLive();
  generation += 1;
  const settings = await loadSettings();
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  store.resetPr();
  store.set({ settings, windowId: tab?.windowId ?? null, pageUrl: tab?.url ?? "" });
  if (!settings.token) {
    api = null;
    setView("not_connected", "Add the server URL and access token in Options to connect.");
    return;
  }
  api = new Api(settings.serverUrl, settings.token);
  if (!(await api.health())) {
    setView("not_connected", `Cannot reach Gatekeeper at ${settings.serverUrl}. Is it running (make dev)?`);
    return;
  }
  await refreshContext();
}

wire();
void boot();
