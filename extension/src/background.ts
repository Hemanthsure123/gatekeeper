// Background service worker:
// - clicking the toolbar action opens the side panel (sidePanel.setPanelBehavior);
// - remembers the last active GitHub tab per window, so panel commands go to the tab
//   the panel is tied to (chrome.tabs.update; a new tab only if the window has none);
// - routes "navigate" and "reveal" commands. Reveal asks the content script to scroll
//   to the file's diff, and falls back to the diff anchor URL, then to the file page.
// No calls to the Gatekeeper API happen here; the side panel makes them.
import type { PanelMessage, RevealMessage, RevealResult } from "./shared/messages";

const GITHUB = "https://github.com/";
const lastGithubTab = new Map<number, number>();

chrome.runtime.onInstalled.addListener(() => {
  void chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
});
chrome.runtime.onStartup.addListener(() => {
  void chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
});

function remember(tab: chrome.tabs.Tab): void {
  if (tab.id !== undefined && tab.url?.startsWith(GITHUB)) lastGithubTab.set(tab.windowId, tab.id);
}

chrome.tabs.onActivated.addListener(({ tabId }) => {
  chrome.tabs.get(tabId).then(remember, () => undefined);
});
chrome.tabs.onUpdated.addListener((_tabId, change, tab) => {
  if (change.url || change.status === "complete") remember(tab);
});
chrome.tabs.onRemoved.addListener((tabId, info) => {
  if (lastGithubTab.get(info.windowId) === tabId) lastGithubTab.delete(info.windowId);
});

/** The GitHub tab this window's panel is tied to (survives service worker restarts). */
async function targetTab(windowId: number): Promise<chrome.tabs.Tab | undefined> {
  const [active] = await chrome.tabs.query({ active: true, windowId });
  if (active?.url?.startsWith(GITHUB)) return active;
  const remembered = lastGithubTab.get(windowId);
  if (remembered !== undefined) {
    try {
      const tab = await chrome.tabs.get(remembered);
      if (tab.url?.startsWith(GITHUB)) return tab;
    } catch {
      lastGithubTab.delete(windowId);
    }
  }
  const tabs = await chrome.tabs.query({ windowId, url: `${GITHUB}*` });
  tabs.sort((a, b) => (b.lastAccessed ?? 0) - (a.lastAccessed ?? 0));
  return tabs[0];
}

async function openUrl(windowId: number, url: string): Promise<chrome.tabs.Tab | undefined> {
  const tab = await targetTab(windowId);
  if (tab?.id !== undefined) {
    return chrome.tabs.update(tab.id, { url, active: true });
  }
  return chrome.tabs.create({ windowId, url });
}

function waitForComplete(tabId: number, timeoutMs = 15000): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(done, timeoutMs);
    function done(): void {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    }
    function listener(id: number, change: { status?: string }): void {
      if (id === tabId && change.status === "complete") done();
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Ask the content script; it may not be injected yet right after a navigation. */
async function askContent(
  tabId: number,
  message: Record<string, unknown>,
): Promise<RevealResult | undefined> {
  for (let attempt = 0; attempt < 10; attempt += 1) {
    try {
      return (await chrome.tabs.sendMessage(tabId, message)) as RevealResult;
    } catch {
      await sleep(300);
    }
  }
  return undefined;
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function encodePath(path: string): string {
  return path.split("/").map(encodeURIComponent).join("/");
}

async function reveal(msg: RevealMessage): Promise<RevealResult> {
  const prUrl = `${GITHUB}${msg.owner}/${msg.name}/pull/${msg.number}`;
  const anchor = await sha256Hex(msg.path);
  const lines =
    msg.lineStart !== undefined ? `R${msg.lineStart}-R${msg.lineEnd ?? msg.lineStart}` : "";
  const fragment = `diff-${anchor}${lines}`;
  const request = {
    type: "gk-reveal",
    anchor,
    path: msg.path,
    lineStart: msg.lineStart,
    lineEnd: msg.lineEnd,
  };

  let tab = await targetTab(msg.windowId);
  const filesPage = new RegExp(`^${escapeRegExp(prUrl)}/(files|changes)(?:[/?#]|$)`);
  if (tab?.id === undefined || !filesPage.test(tab.url ?? "")) {
    // /files keeps working and redirects to /changes in GitHub's current experience.
    tab = await openUrl(msg.windowId, `${prUrl}/files#${fragment}`);
    if (tab?.id === undefined) return { found: false, method: "none" };
    await waitForComplete(tab.id);
  }
  const tabId = tab.id as number;
  let result = await askContent(tabId, request);
  if (result?.found) return result;

  // Second strategy: let GitHub's own page code resolve the diff anchor.
  const current = await chrome.tabs.get(tabId).catch(() => tab);
  const base = (current?.url ?? `${prUrl}/files`).split("#")[0];
  await chrome.tabs.update(tabId, { url: `${base}#${fragment}` });
  await sleep(800);
  result = await askContent(tabId, request);
  if (result?.found) return { ...result, method: result.method === "line" ? "line" : "hash" };

  // Last resort: the file itself at the PR's commit.
  const range =
    msg.lineStart !== undefined ? `#L${msg.lineStart}-L${msg.lineEnd ?? msg.lineStart}` : "";
  await chrome.tabs.update(tabId, {
    url: `${GITHUB}${msg.owner}/${msg.name}/blob/${msg.fallbackSha}/${encodePath(msg.path)}${range}`,
  });
  return { found: false, method: "blob" };
}

chrome.runtime.onMessage.addListener((message: PanelMessage, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id) return false;
  if (message?.type === "gk-navigate") {
    openUrl(message.windowId, message.url).then(
      () => sendResponse({ ok: true }),
      () => sendResponse({ ok: false }),
    );
    return true;
  }
  if (message?.type === "gk-reveal-file") {
    reveal(message).then(sendResponse, () => sendResponse({ found: false, method: "error" }));
    return true;
  }
  return false;
});
