// Inline SVG icons on a 16px grid with 1.5px strokes in currentColor. Icons are always
// decorative (aria-hidden): the text next to or inside the control carries the meaning.
const SVG = "http://www.w3.org/2000/svg";

const PATHS = {
  gate: "M4.5 2.5v11M11.5 2.5v11M4.5 6h7M4.5 10h7",
  chevron: "M6 3.5 10.5 8 6 12.5",
  folder:
    "M1.75 4.25c0-.55.45-1 1-1h3.1l1.4 1.5h5.99c.55 0 1 .45 1 1v6.5c0 .55-.45 1-1 1H2.75c-.55 0-1-.45-1-1z",
  file: "M4 1.75h5.25L12.5 5v8.25c0 .55-.45 1-1 1H4c-.55 0-1-.45-1-1V2.75c0-.55.45-1 1-1zM9.25 1.75V5h3.25",
  settings: "M2 4.5h7M12.5 4.5H14M2 11.5h2M7.5 11.5H14M10.75 3v3M5.75 10v3",
  refresh: "M13.25 8a5.25 5.25 0 1 1-1.54-3.71M13.25 2.5v3h-3",
  search: "M7 12A5 5 0 1 0 7 2a5 5 0 0 0 0 10zM10.75 10.75 14 14",
  unfold: "M5 6 8 3l3 3M5 10l3 3 3-3",
  fold: "M5 3l3 3 3-3M5 13l3-3 3 3",
  external:
    "M9.5 2.25h4.25V6.5M13.75 2.25 7.5 8.5M11.75 9.5v3.25c0 .55-.45 1-1 1h-7.5c-.55 0-1-.45-1-1v-7.5c0-.55.45-1 1-1H6.5",
  close: "M4 4l8 8M12 4l-8 8",
  chat: "M2.25 3.75c0-.55.45-1 1-1h9.5c.55 0 1 .45 1 1v6.5c0 .55-.45 1-1 1H7l-3 2.5v-2.5h-.75c-.55 0-1-.45-1-1z",
  activity: "M1.5 8.5h2.75l1.75-5 4 9 1.75-4h2.75",
  insights: "M3 13.5V9M8 13.5v-11M13 13.5V6M1.5 13.5h13",
  target: "M8 1.75v2.5M8 11.75v2.5M1.75 8h2.5M11.75 8h2.5M8 10.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z",
  filter: "M2 3.5h12l-4.5 5.25v4.25l-3-1.5V8.75z",
} as const;

export type IconName = keyof typeof PATHS;

export function icon(name: IconName, extra = ""): SVGSVGElement {
  const el = document.createElementNS(SVG, "svg");
  for (const [key, value] of Object.entries({
    viewBox: "0 0 16 16",
    width: "16",
    height: "16",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "1.5",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
    focusable: "false",
    class: extra ? `icon ${extra}` : "icon",
  })) {
    el.setAttribute(key, value);
  }
  const path = document.createElementNS(SVG, "path");
  path.setAttribute("d", PATHS[name]);
  el.append(path);
  return el;
}
