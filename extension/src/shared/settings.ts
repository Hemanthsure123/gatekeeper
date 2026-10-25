// Extension settings in chrome.storage.local. The access token is never logged.

export interface Settings {
  serverUrl: string;
  token: string;
  follow: boolean;
}

export const DEFAULT_SERVER = "http://127.0.0.1:8000";
const DEFAULTS: Settings = { serverUrl: DEFAULT_SERVER, token: "", follow: true };

export async function loadSettings(): Promise<Settings> {
  const stored = await chrome.storage.local.get(["serverUrl", "token", "follow"]);
  return {
    serverUrl: normaliseServer(typeof stored.serverUrl === "string" ? stored.serverUrl : ""),
    token: typeof stored.token === "string" ? stored.token : DEFAULTS.token,
    follow: typeof stored.follow === "boolean" ? stored.follow : DEFAULTS.follow,
  };
}

export async function saveSettings(settings: Settings): Promise<void> {
  await chrome.storage.local.set({
    serverUrl: normaliseServer(settings.serverUrl),
    token: settings.token.trim(),
    follow: settings.follow,
  });
}

export function normaliseServer(url: string): string {
  const value = url.trim().replace(/\/+$/, "");
  return value || DEFAULT_SERVER;
}
