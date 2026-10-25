// The repository tree: changed files (default) or all files, a text filter, a gate
// filter from the pipeline view, and compact folders. Each changed file shows the five
// gates it passed, filled in live as the backend works; the folder holding the file
// under review is marked "Now". Rows are cached by a signature, so a live event only
// rebuilds the rows it changed. The selected file opens its details in place.
import type { FileState, PrFile, PrView, TreeEntry } from "../shared/types";
import { ancestors, buildNodes, entriesFromFiles, trail, type TreeNode } from "./derive";
import { h, preserveFocus } from "./dom";
import { icon } from "./icons";
import { FILE_STATUS, STAGE } from "./labels";
import type { State } from "./store";

export interface TreeHandlers {
  onFolder: (path: string) => void;
  onFile: (path: string) => void;
  /** The details shown under the selected file. */
  details: (path: string) => HTMLElement | null;
}

interface Row {
  sig: string;
  li: HTMLLIElement;
}

const same = (a: readonly unknown[], b: readonly unknown[]): boolean =>
  a.length === b.length && a.every((v, i) => v === b[i]);

let fallback: { pr: PrView | null; entries: TreeEntry[] } = { pr: null, entries: [] };
let nodesKey: unknown[] = [];
let nodes: TreeNode[] = [];
let rowsKey: unknown[] = [];
let rows = new Map<string, Row>();
let filesFor: PrView | null = null;
let files = new Map<string, PrFile>();
let followed: string | null = null;

export function treeEntries(state: State): TreeEntry[] {
  if (state.tree) return state.tree.entries;
  if (fallback.pr !== state.pr) fallback = { pr: state.pr, entries: state.pr ? entriesFromFiles(state.pr.files) : [] };
  return fallback.entries;
}

function fileOf(pr: PrView | null, path: string): PrFile | undefined {
  if (filesFor !== pr) {
    filesFor = pr;
    files = new Map((pr?.files ?? []).map((f) => [f.path, f]));
  }
  return files.get(path);
}

/** The five gate segments for one file. `live` marks the next gate as in progress. */
export function rail(state: FileState, live = false): HTMLElement {
  const segments = trail(state);
  const next = segments.indexOf("todo");
  return h(
    "span",
    { class: "rail", "aria-hidden": "true" },
    ...segments.map((segment, i) => h("span", { class: `seg seg-${segment}${live && i === next ? " seg-live" : ""}` })),
  );
}

export function stageChip(state: FileState): HTMLElement {
  const stage = STAGE[state];
  return h("span", { class: `stage stage-${state}` }, h("span", { "aria-hidden": "true" }, stage.icon), ` ${stage.text}`);
}

function highlight(name: string, needle: string): Array<Node | string> {
  const at = needle ? name.toLowerCase().indexOf(needle) : -1;
  if (at < 0) return [name];
  return [name.slice(0, at), h("mark", null, name.slice(at, at + needle.length)), name.slice(at + needle.length)];
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function folderRow(node: TreeNode, depth: number, open: boolean, live: boolean, needle: string): HTMLButtonElement {
  return h(
    "button",
    {
      type: "button",
      class: `row folder${open ? " open" : ""}${live ? " live" : ""}`,
      "aria-label": `${node.path}, folder${node.changed ? `, ${plural(node.changed, "changed file")}` : ""}${
        live ? ", the review is here now" : ""
      }`,
      title: node.path,
      dataset: { key: node.path, path: node.path, folder: "1" },
      style: `--depth:${depth}`,
    },
    h("span", { class: "twisty" }, icon("chevron")),
    icon("folder", "kind-icon"),
    h("span", { class: "name" }, ...highlight(node.name, needle)),
    live ? h("span", { class: "now-pill", "aria-hidden": "true" }, "Now") : null,
    node.changed ? h("span", { class: "badge", "aria-hidden": "true" }, String(node.changed)) : null,
  );
}

function fileRow(node: TreeNode, depth: number, state: State, needle: string): HTMLButtonElement {
  const changed = Boolean(node.status);
  const base = { type: "button", title: node.path, dataset: { key: node.path, path: node.path, folder: "0" }, style: `--depth:${depth}` };
  if (!changed) {
    return h(
      "button",
      { ...base, class: `row file${state.selected === node.path ? " selected" : ""}`, "aria-label": `${node.path}, unchanged` },
      h("span", { class: "twisty" }),
      icon("file", "kind-icon"),
      h("span", { class: "name" }, ...highlight(node.name, needle)),
    );
  }
  const file = fileOf(state.pr, node.path);
  const status = FILE_STATUS[node.status ?? "modified"] ?? FILE_STATUS.modified;
  const fileState = state.fileStates[node.path] ?? "listed";
  const current = state.view === "analysing" && state.currentPath === node.path;
  const finished = state.view === "result";
  const tracked = state.view !== "not_analysed";
  const signals = finished ? (file?.signals.length ?? 0) : 0;
  const citations = finished ? (file?.evidence.length ?? 0) : 0;
  const classes = [
    "row file changed",
    state.selected === node.path ? "selected" : "",
    current ? "current" : "",
    state.bumped.has(node.path) ? "bump" : "",
  ].join(" ");
  return h(
    "button",
    {
      ...base,
      class: classes,
      "aria-label":
        `${node.path}, ${status.text}` +
        (file ? `, ${file.additions} added and ${file.deletions} removed lines` : "") +
        (tracked ? `, ${STAGE[fileState].text}` : "") +
        (current ? ", under review now" : ""),
    },
    h(
      "span",
      { class: "row-main" },
      h("span", { class: `kind kind-${node.status ?? "modified"}`, "aria-hidden": "true" }, status.glyph),
      h("span", { class: "name" }, ...highlight(node.name, needle)),
      current ? h("span", { class: "now-pill", "aria-hidden": "true" }, "Now") : null,
      file && file.additions + file.deletions > 0
        ? h(
            "span",
            { class: "diffstat", "aria-hidden": "true" },
            h("span", { class: "add" }, `+${file.additions}`),
            h("span", { class: "del" }, `−${file.deletions}`),
          )
        : null,
    ),
    tracked
      ? h(
          "span",
          { class: "row-sub", "aria-hidden": "true" },
          rail(fileState, current),
          stageChip(fileState),
          signals ? h("span", { class: "fact" }, plural(signals, "signal")) : null,
          citations ? h("span", { class: "fact" }, plural(citations, "citation")) : null,
        )
      : null,
  );
}

export function renderTree(root: HTMLElement, state: State, handlers: TreeHandlers): void {
  const entries = treeEntries(state);
  const key = [entries, state.showAll, state.filter, state.gate, state.gate ? state.fileStates : null];
  if (!same(key, nodesKey)) {
    nodesKey = key;
    nodes = buildNodes(entries, { showAll: state.showAll, filter: state.filter, gate: state.gate, states: state.fileStates });
  }
  if (!same([entries, state.pr], rowsKey)) {
    rowsKey = [entries, state.pr];
    rows = new Map();
  }
  const needle = state.filter.trim().toLowerCase();
  const filtering = needle !== "" || state.gate !== null;
  const livePath = state.view === "analysing" ? state.currentPath : null;

  preserveFocus(root, () => {
    if (!nodes.length) {
      root.replaceChildren(
        h("p", { class: "tree-empty" }, filtering ? "No files match. Clear the filter to see every file." : "No files."),
      );
      return;
    }
    let list = root.querySelector<HTMLUListElement>(":scope > ul.tree");
    if (!list) {
      list = h("ul", { role: "tree", "aria-label": "Repository files", class: "tree" });
      root.replaceChildren(list);
    }
    const items: HTMLLIElement[] = [];
    const next = new Map<string, Row>();
    const walk = (level: TreeNode[], depth: number): void => {
      for (const node of level) {
        const folder = node.type === "tree";
        const open = folder && (filtering || state.expanded.has(node.path));
        const selected = state.selected === node.path;
        const live = folder && livePath !== null && livePath.startsWith(`${node.path}/`);
        const sig = [
          node.name,
          depth,
          open,
          live,
          node.changed,
          needle,
          state.view,
          folder ? "" : (state.fileStates[node.path] ?? ""),
          state.currentPath === node.path,
          state.bumped.has(node.path),
        ].join("|");
        const cached = rows.get(node.path);
        let li: HTMLLIElement;
        if (cached && cached.sig === sig && !selected) {
          li = cached.li;
        } else {
          li = h(
            "li",
            {
              role: "treeitem",
              "aria-level": String(depth + 1),
              "aria-expanded": folder ? String(open) : undefined,
              "aria-selected": String(selected),
            },
            folder ? folderRow(node, depth, open, live, needle) : fileRow(node, depth, state, needle),
          );
          li.querySelector("button")!.addEventListener("click", () =>
            folder ? handlers.onFolder(node.path) : handlers.onFile(node.path),
          );
          if (selected && !folder) {
            const details = handlers.details(node.path);
            if (details) li.append(details);
          }
        }
        if (!selected) next.set(node.path, { sig, li });
        items.push(li);
        if (open) walk(node.children, depth + 1);
      }
    };
    walk(nodes, 0);
    rows = next;
    list.replaceChildren(...items);
  });

  const follow = state.view === "analysing" && state.followLive ? state.currentPath : null;
  if (follow && follow !== followed) revealRow(root, follow);
  followed = follow;
}

/** Scroll the tree's own box (never the page) so the row for `path` is visible. */
export function revealRow(root: HTMLElement, path: string, toTop = false): void {
  const row = root.querySelector<HTMLElement>(`[data-key="${CSS.escape(path)}"]`);
  if (!row) return;
  const box = root.getBoundingClientRect();
  const rect = row.getBoundingClientRect();
  if (toTop || rect.top < box.top) root.scrollTop += rect.top - box.top - 8;
  else if (rect.bottom > box.bottom) root.scrollTop += rect.bottom - box.bottom + 8;
}

/** Arrow keys move between rows; Left/Right collapse and expand folders (without
 * navigating the GitHub tab; Enter or a click does that). */
export function treeKeys(
  root: HTMLElement,
  toggle: (path: string) => void,
  isOpen: (path: string) => boolean,
): void {
  root.addEventListener("keydown", (event) => {
    const target = event.target as HTMLElement;
    if (!target.matches("button.row")) return;
    const all = [...root.querySelectorAll<HTMLButtonElement>("button.row")];
    const index = all.indexOf(target as HTMLButtonElement);
    const path = target.dataset.path ?? "";
    const folder = target.dataset.folder === "1";
    if (event.key === "ArrowDown") all[index + 1]?.focus();
    else if (event.key === "ArrowUp") all[index - 1]?.focus();
    else if (event.key === "Home") all[0]?.focus();
    else if (event.key === "End") all[all.length - 1]?.focus();
    else if (event.key === "ArrowRight" && folder && !isOpen(path)) toggle(path);
    else if (event.key === "ArrowLeft") {
      if (folder && isOpen(path)) toggle(path);
      else {
        // A compact folder row holds several levels, so walk up to the nearest row.
        const parent = ancestors(path)
          .reverse()
          .map((p) => all.find((r) => r.dataset.path === p))
          .find(Boolean);
        parent?.focus();
      }
    } else return;
    event.preventDefault();
  });
}
