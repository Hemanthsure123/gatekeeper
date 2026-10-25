// Content script on github.com. It ONLY performs DOM actions (scroll to and briefly
// highlight a file's diff or a line range) and replies "found" or "not found".
// It makes no network calls and sends no page data anywhere. Keep it import-free: it
// is loaded as a classic script.
//
// GitHub's Files-changed page is dynamic and its markup is not a documented API, so
// several strategies are tried and retried for a few seconds:
// 1. the diff anchor id: "diff-" + SHA-256 hex of the path (classic page and links);
// 2. data attributes that carry the path (data-tagsearch-path, data-path, ...);
// 3. a link to "#diff-<sha>", or a file header whose text is the path.
// Line ranges use the new-side cell id "diff-<sha>R<n>" or cells with data-line-number.

interface RevealRequest {
  type: "gk-reveal";
  anchor: string;
  path: string;
  lineStart?: number;
  lineEnd?: number;
}

const RETRY_MS = 300;
const DEADLINE_MS = 4000;
const HIGHLIGHT_MS = 2500;
const STYLE_ID = "gatekeeper-reveal-style";

function ensureStyle(): void {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent =
    ".gk-reveal{outline:3px solid #3b5bdb!important;outline-offset:2px;" +
    "box-shadow:0 0 0 6px rgba(59,91,219,.18)!important;transition:outline-color .6s}" +
    "@media (prefers-reduced-motion: reduce){.gk-reveal{transition:none}}";
  document.head.appendChild(style);
}

function cssValue(value: string): string {
  return CSS.escape(value);
}

function container(el: Element): Element {
  return (
    el.closest(
      '.file, [data-details-container-group="file"], [data-file-path], [role="region"]',
    ) ?? el
  );
}

function findFile(anchor: string, path: string): Element | null {
  const byId = document.getElementById(`diff-${anchor}`);
  if (byId) return container(byId);
  const selectors = [
    `[data-anchor="diff-${anchor}"]`,
    `[data-tagsearch-path="${cssValue(path)}"]`,
    `[data-path="${cssValue(path)}"]`,
    `[data-file-path="${cssValue(path)}"]`,
    `a[href$="#diff-${anchor}"]`,
  ];
  for (const selector of selectors) {
    const el = document.querySelector(selector);
    if (el) return container(el);
  }
  const headers = document.querySelectorAll("a, h3, span, code, button, div[title]");
  for (const el of headers) {
    const text = (el.getAttribute("title") ?? el.textContent ?? "").trim();
    if (text === path && el.getBoundingClientRect().height > 0) return container(el);
  }
  return null;
}

function lineCell(file: Element, anchor: string, line: number): Element | null {
  const byId = document.getElementById(`diff-${anchor}R${line}`);
  if (byId) return byId;
  const cells = [...file.querySelectorAll(`[data-line-number="${line}"]`)];
  // Prefer new-side cells: ids ending in R<n>, or the second number cell of a row.
  const right = cells.find(
    (c) => c.id.endsWith(`R${line}`) || /right|addition|new/i.test(c.className),
  );
  if (right) return right;
  return (
    cells.find((c) => {
      const row = c.closest("tr");
      const numbers = row ? [...row.querySelectorAll("[data-line-number]")] : [];
      return numbers.length > 1 && numbers[numbers.length - 1] === c;
    }) ??
    cells[0] ??
    null
  );
}

function highlight(elements: Element[]): void {
  ensureStyle();
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  elements[0]?.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" });
  for (const el of elements) el.classList.add("gk-reveal");
  window.setTimeout(() => {
    for (const el of elements) el.classList.remove("gk-reveal");
  }, HIGHLIGHT_MS);
}

function rowsFor(file: Element, request: RevealRequest): Element[] | null {
  if (request.lineStart === undefined) return null;
  const end = request.lineEnd ?? request.lineStart;
  const rows: Element[] = [];
  for (let line = request.lineStart; line <= end && rows.length < 60; line += 1) {
    const cell = lineCell(file, request.anchor, line);
    if (cell) rows.push(cell.closest("tr") ?? cell);
  }
  return rows.length ? rows : null;
}

async function reveal(request: RevealRequest): Promise<{ found: boolean; method: string }> {
  const deadline = Date.now() + DEADLINE_MS;
  let file: Element | null = null;
  while (Date.now() < deadline) {
    file = findFile(request.anchor, request.path);
    if (file) {
      const rows = rowsFor(file, request);
      if (rows) {
        highlight(rows);
        return { found: true, method: "line" };
      }
      if (request.lineStart === undefined) break;
    }
    await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
  }
  if (file) {
    highlight([file]);
    return { found: true, method: "file" };
  }
  return { found: false, method: "none" };
}

chrome.runtime.onMessage.addListener((message: RevealRequest, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id || message?.type !== "gk-reveal") return false;
  reveal(message).then(sendResponse, () => sendResponse({ found: false, method: "error" }));
  return true;
});
