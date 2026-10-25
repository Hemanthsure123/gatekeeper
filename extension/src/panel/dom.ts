// Tiny DOM helpers. Text is always set with textContent / text nodes, never innerHTML,
// because everything shown can contain untrusted PR or model text.

type Child = Node | string | number | null | undefined | false;
type Props = Record<string, unknown> & {
  class?: string;
  on?: Partial<{ [K in keyof HTMLElementEventMap]: (event: HTMLElementEventMap[K]) => void }>;
  dataset?: Record<string, string>;
};

export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  props: Props | null = null,
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") el.className = String(value);
      else if (key === "on") {
        for (const [name, handler] of Object.entries(value as Record<string, EventListener>)) {
          el.addEventListener(name, handler);
        }
      } else if (key === "dataset") {
        Object.assign(el.dataset, value as Record<string, string>);
      } else if (key in el && typeof value !== "string") {
        (el as unknown as Record<string, unknown>)[key] = value;
      } else {
        el.setAttribute(key, value === true ? "" : String(value));
      }
    }
  }
  append(el, children);
  return el;
}

export function append(el: Element, children: Child[]): void {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function clear(el: Element): void {
  el.replaceChildren();
}

/** Keep keyboard focus across a re-render: focus the element with the same data-key. */
export function preserveFocus(root: Element, render: () => void): void {
  const active = document.activeElement as HTMLElement | null;
  const key = active && root.contains(active) ? active.dataset.key : undefined;
  render();
  if (key) root.querySelector<HTMLElement>(`[data-key="${CSS.escape(key)}"]`)?.focus();
}

export function formatTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** "120 ms", "4.2 s" or "2 min 5 s". */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "–";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  const seconds = Math.round(ms / 1000);
  return `${Math.floor(seconds / 60)} min ${seconds % 60} s`;
}

/** Time since the run started, as "+4.2 s". */
export function formatOffset(iso: string, start: string | null): string {
  const ms = start ? Date.parse(iso) - Date.parse(start) : Number.NaN;
  return Number.isFinite(ms) && ms >= 0 ? `+${formatDuration(ms)}` : formatTime(iso);
}

/** 1234 -> "1,234"; 12345 -> "12.3k". */
export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined) return "–";
  return value >= 10000 ? `${(value / 1000).toFixed(1)}k` : value.toLocaleString("en-US");
}

export function pct(value: number | null | undefined): string {
  return value === null || value === undefined ? "–" : `${Math.round(value * 100)}%`;
}

export function span(lineStart: number, lineEnd: number): string {
  return lineStart === lineEnd ? `L${lineStart}` : `L${lineStart}-L${lineEnd}`;
}

/** A small, safe Markdown subset: paragraphs, bullet and numbered lists, `code` spans
 * and **bold**. Built from text nodes only. */
export function renderMarkdown(text: string): DocumentFragment {
  const fragment = document.createDocumentFragment();
  const blocks = text.replace(/\r\n?/g, "\n").split(/\n{2,}/);
  for (const block of blocks) {
    const lines = block.split("\n").filter((line) => line.trim() !== "");
    if (!lines.length) continue;
    const bullet = lines.every((line) => /^\s*[-*]\s+/.test(line));
    const numbered = lines.every((line) => /^\s*\d+[.)]\s+/.test(line));
    if (bullet || numbered) {
      const list = h(numbered ? "ol" : "ul");
      for (const line of lines) {
        const li = h("li");
        inline(li, line.replace(/^\s*(?:[-*]|\d+[.)])\s+/, ""));
        list.append(li);
      }
      fragment.append(list);
    } else {
      const p = h("p");
      inline(p, lines.join(" "));
      fragment.append(p);
    }
  }
  return fragment;
}

function inline(el: Element, text: string): void {
  const parts = text.split(/(`[^`]+`|\*\*[^*]+\*\*)/g);
  for (const part of parts) {
    if (!part) continue;
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      el.append(h("code", null, part.slice(1, -1)));
    } else if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      el.append(h("strong", null, part.slice(2, -2)));
    } else {
      el.append(document.createTextNode(part));
    }
  }
}
