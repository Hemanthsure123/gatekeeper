// The repository tree: changed files (default) or all files, a filter, collapsible
// folders with a count of changed descendants, and a stage indicator per changed file.
import type { FileState, PrFile, TreeEntry } from "../shared/types";
import { h, preserveFocus } from "./dom";
import { FILE_STATUS, STAGE } from "./labels";
import type { State } from "./store";

export interface TreeNode {
  path: string;
  name: string;
  type: "tree" | "blob" | "commit";
  status?: string;
  children: TreeNode[];
  changed: number;
}

export interface TreeHandlers {
  onFolder: (path: string) => void;
  onFile: (path: string) => void;
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

export function buildNodes(entries: TreeEntry[], showAll: boolean, filter: string): TreeNode[] {
  const changed = entries.filter((e) => e.status);
  const keep = new Set<string>();
  const needle = filter.trim().toLowerCase();
  const pool = showAll ? entries : changed;
  for (const entry of pool) {
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
  return roots;
}

/** Folders that start expanded: every ancestor of a changed file. */
export function initialExpanded(entries: TreeEntry[]): Set<string> {
  const out = new Set<string>();
  for (const entry of entries) if (entry.status) for (const f of ancestors(entry.path)) out.add(f);
  return out;
}

function stageChip(state: FileState | undefined): HTMLElement {
  const stage = STAGE[state ?? "listed"];
  return h(
    "span",
    { class: `stage stage-${state ?? "listed"}` },
    h("span", { "aria-hidden": "true" }, stage.icon),
    ` ${stage.text}`,
  );
}

export function renderTree(root: HTMLElement, state: State, handlers: TreeHandlers): void {
  const entries = state.tree?.entries ?? (state.pr ? entriesFromFiles(state.pr.files) : []);
  const nodes = buildNodes(entries, state.showAll, state.filter);
  const filtering = state.filter.trim() !== "";
  preserveFocus(root, () => {
    root.replaceChildren();
    if (!nodes.length) {
      root.append(h("p", { class: "muted" }, filtering ? "No files match the filter." : "No files."));
      return;
    }
    const list = h("ul", { role: "tree", "aria-label": "Repository files", class: "tree" });
    const walk = (items: TreeNode[], depth: number): void => {
      for (const node of items) {
        const folder = node.type === "tree";
        const open = folder && (filtering || state.expanded.has(node.path));
        const fileState = state.fileStates[node.path];
        const status = node.status ? (FILE_STATUS[node.status] ?? FILE_STATUS.modified) : null;
        const label = folder
          ? `${node.path}, folder${node.changed ? `, ${node.changed} changed` : ""}`
          : `${node.path}, ${status ? status.text : "unchanged"}${
              status ? `, ${STAGE[fileState ?? "listed"].text}` : ""
            }`;
        const classes = [
          "row",
          folder ? "folder" : "file",
          node.status ? "changed" : "",
          state.selected === node.path ? "selected" : "",
          state.currentPath === node.path && state.view === "analysing" ? "current" : "",
          state.bumped.has(node.path) ? "bump" : "",
        ].join(" ");
        const button = h(
          "button",
          {
            type: "button",
            class: classes,
            "aria-label": label,
            title: node.path,
            dataset: { key: node.path, path: node.path, folder: folder ? "1" : "0" },
            style: `--depth:${depth}`,
            on: {
              click: () => (folder ? handlers.onFolder(node.path) : handlers.onFile(node.path)),
            },
          },
          folder
            ? h("span", { class: "twisty", "aria-hidden": "true" }, open ? "▾" : "▸")
            : h(
                "span",
                { class: `glyph ${node.status ?? "unchanged"}`, "aria-hidden": "true" },
                status ? status.glyph : " ",
              ),
          h("span", { class: "name" }, node.name),
          folder && node.changed
            ? h("span", { class: "badge", "aria-hidden": "true" }, String(node.changed))
            : null,
          !folder && status ? stageChip(fileState) : null,
        );
        list.append(
          h(
            "li",
            {
              role: "treeitem",
              "aria-level": String(depth + 1),
              "aria-expanded": folder ? String(open) : undefined,
              "aria-selected": String(state.selected === node.path),
            },
            button,
          ),
        );
        if (open) walk(node.children, depth + 1);
      }
    };
    walk(nodes, 0);
    root.append(list);
  });
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
    const rows = [...root.querySelectorAll<HTMLButtonElement>("button.row")];
    const index = rows.indexOf(target as HTMLButtonElement);
    const path = target.dataset.path ?? "";
    const folder = target.dataset.folder === "1";
    if (event.key === "ArrowDown") rows[index + 1]?.focus();
    else if (event.key === "ArrowUp") rows[index - 1]?.focus();
    else if (event.key === "Home") rows[0]?.focus();
    else if (event.key === "End") rows[rows.length - 1]?.focus();
    else if (event.key === "ArrowRight" && folder && !isOpen(path)) toggle(path);
    else if (event.key === "ArrowLeft") {
      if (folder && isOpen(path)) toggle(path);
      else {
        const parent = ancestors(path).pop();
        rows.find((r) => r.dataset.path === parent)?.focus();
      }
    } else return;
    event.preventDefault();
  });
}
