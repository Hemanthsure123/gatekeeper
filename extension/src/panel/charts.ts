// Small single-series charts built from HTML, so text keeps its size at any panel width.
// One accent colour for marks; labels and values are text, so every chart is also its
// own table view, and each mark has a native tooltip.
import { h } from "./dom";

export interface Bar {
  label: Node | string;
  value: number;
  /** The value as shown at the end of the bar. */
  display: string;
  title?: string;
  onClick?: () => void;
}

/** Horizontal bars: label above, bar and value below (fits 320px). */
export function barList(caption: string, bars: Bar[], options: { max?: number; empty?: string } = {}): HTMLElement {
  const figure = h("figure", { class: "figure" }, h("figcaption", null, caption));
  if (!bars.length) {
    figure.append(h("p", { class: "muted small" }, options.empty ?? "Nothing yet."));
    return figure;
  }
  const max = options.max ?? Math.max(1, ...bars.map((b) => b.value));
  const list = h("ol", { class: "bars" });
  for (const bar of bars) {
    const share = Math.max(0, Math.min(1, bar.value / max));
    const body = [
      h("span", { class: "bar-label" }, bar.label),
      h(
        "span",
        { class: "bar-track", "aria-hidden": "true" },
        h("span", { class: "bar-fill", style: `width:${(share * 100).toFixed(1)}%` }),
      ),
      h("span", { class: "bar-value" }, bar.display),
    ];
    list.append(
      h(
        "li",
        null,
        bar.onClick
          ? h("button", { type: "button", class: "bar-row", title: bar.title, on: { click: bar.onClick } }, ...body)
          : h("div", { class: "bar-row", title: bar.title }, ...body),
      ),
    );
  }
  figure.append(list);
  return figure;
}

/** A 0-100% meter with an optional threshold tick. */
export function meter(label: string, value: number | null, threshold: number | null = null): HTMLElement {
  const percent = value === null ? null : Math.round(value * 100);
  const mark = threshold === null ? null : Math.round(threshold * 100);
  return h(
    "div",
    {
      class: "meter",
      role: "meter",
      "aria-label": label,
      "aria-valuemin": "0",
      "aria-valuemax": "100",
      "aria-valuenow": percent === null ? undefined : String(percent),
      "aria-valuetext":
        percent === null
          ? "not computed"
          : `${percent} percent${mark === null ? "" : `; the abstain threshold is ${mark} percent`}`,
    },
    h("span", { class: "meter-fill", style: `width:${percent ?? 0}%` }),
    mark === null
      ? null
      : h("span", { class: "meter-mark", style: `left:${mark}%`, title: `Abstain threshold ${mark}%` }),
  );
}

export interface Column {
  value: number | null;
  title: string;
}

/** Vertical columns on a 0-100% scale, oldest first, with three gridlines. */
export function columns(caption: string, items: Column[], summary: string): HTMLElement {
  const plot = h(
    "div",
    { class: "cols", role: "img", "aria-label": summary },
    ...[100, 50, 0].map((tick) =>
      h("span", { class: "cols-grid", style: `bottom:${tick}%` }, h("span", null, `${tick}%`)),
    ),
    h(
      "div",
      { class: "cols-bars" },
      ...items.map((item) =>
        h("span", {
          class: item.value === null ? "col col-empty" : "col",
          style: `height:${item.value === null ? 0 : Math.max(2, item.value * 100).toFixed(1)}%`,
          title: item.title,
        }),
      ),
    ),
  );
  return h(
    "figure",
    { class: "figure" },
    h("figcaption", null, caption),
    plot,
    h("p", { class: "cols-axis" }, h("span", null, "Older"), h("span", null, "Newer")),
  );
}

export function tile(label: string, value: string, sub = "", hint = ""): HTMLElement {
  return h(
    "div",
    { class: "tile", title: hint || undefined },
    h("span", { class: "tile-label" }, label),
    h("span", { class: "tile-value" }, value),
    sub ? h("span", { class: "tile-sub" }, sub) : null,
  );
}
