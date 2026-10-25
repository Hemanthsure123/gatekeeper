// Messages between the side panel and the background service worker. The content
// script defines its own copy of the reveal message (it must stay import-free).

export interface NavigateMessage {
  type: "gk-navigate";
  windowId: number;
  url: string;
}

export interface RevealMessage {
  type: "gk-reveal-file";
  windowId: number;
  owner: string;
  name: string;
  number: number;
  path: string;
  /** Commit to open the file at if the diff cannot be found (head, or base if removed). */
  fallbackSha: string;
  lineStart?: number;
  lineEnd?: number;
}

export type PanelMessage = NavigateMessage | RevealMessage;

export interface RevealResult {
  found: boolean;
  /** How it was revealed: "line", "file", "hash" or "blob" (the last-resort file page). */
  method: string;
}
