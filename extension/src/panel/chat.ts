// Chat tab (filled in by P06 Part 4).
import type { Api } from "./api";
import { h } from "./dom";
import type { State } from "./store";

export interface ChatDeps {
  api: () => Api | null;
  revealRange: (path: string, start: number, end: number) => void;
}

export function renderChat(root: HTMLElement, _state: State, _deps: ChatDeps): void {
  root.replaceChildren(h("p", { class: "muted" }, "Chat about this review is not available yet."));
}
