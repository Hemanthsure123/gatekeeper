// Options: server URL, access token (password field, never logged), follow toggle and
// a connection test (/healthz, then an authenticated /api/context call).
import { loadSettings, normaliseServer, saveSettings } from "../shared/settings";

const form = document.getElementById("form") as HTMLFormElement;
const server = document.getElementById("server") as HTMLInputElement;
const token = document.getElementById("token") as HTMLInputElement;
const follow = document.getElementById("follow") as HTMLInputElement;
const status = document.getElementById("status") as HTMLParagraphElement;
const SAMPLE_PR = "https://github.com/octocat/Hello-World/pull/1";

function say(text: string): void {
  status.textContent = text;
}

async function save(): Promise<void> {
  await saveSettings({ serverUrl: server.value, token: token.value, follow: follow.checked });
}

async function test(): Promise<void> {
  const base = normaliseServer(server.value);
  say("Testing…");
  try {
    const health = await fetch(`${base}/healthz`, { cache: "no-store" });
    if (!health.ok) {
      say(`The server answered HTTP ${health.status} on /healthz.`);
      return;
    }
  } catch {
    say(`Cannot reach ${base}. Start Gatekeeper with make dev.`);
    return;
  }
  try {
    const res = await fetch(`${base}/api/context?url=${encodeURIComponent(SAMPLE_PR)}`, {
      headers: { Authorization: `Bearer ${token.value.trim()}` },
      cache: "no-store",
    });
    if (res.ok) say("Connected. The server accepted the access token.");
    else if (res.status === 401) say("The server is up, but the access token was rejected.");
    else if (res.status === 503) say("The server has no GATEKEEPER_ADMIN_TOKEN set.");
    else say(`The server answered HTTP ${res.status}.`);
  } catch {
    say("The server is up, but the API request failed.");
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  void save().then(() => say("Saved."));
});
document.getElementById("test")?.addEventListener("click", () => void test());

void loadSettings().then((settings) => {
  server.value = settings.serverUrl;
  token.value = settings.token;
  follow.checked = settings.follow;
});
