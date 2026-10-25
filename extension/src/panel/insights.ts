// Insights tab (filled in by P06 Part 5).
import type { Api } from "./api";
import { h } from "./dom";
import type { State } from "./store";

export interface InsightsDeps {
  api: () => Api | null;
}

export function renderInsights(root: HTMLElement, _state: State, _deps: InsightsDeps): void {
  root.replaceChildren(h("p", { class: "muted" }, "Insights are not available yet."));
}
