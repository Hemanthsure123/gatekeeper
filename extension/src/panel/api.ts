// Gatekeeper API client for the side panel (extension origin; host permissions cover
// 127.0.0.1:8000 and localhost:8000). Sends the bearer token; never logs it.
// The live feed uses fetch streaming, because EventSource cannot send an
// Authorization header.
import type {
  ActivityEvent,
  AnalyzeResponse,
  ChatAnswer,
  ChatMessage,
  CommentResult,
  JobStatus,
  Metrics,
  ModelInput,
  PageContext,
  PrView,
  TreeResponse,
} from "../shared/types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

export interface StreamHandlers {
  onEvent: (event: ActivityEvent) => void;
  onEnd: (reason: "end" | "timeout") => void;
  onError: (message: string) => void;
}

const enc = encodeURIComponent;

export class Api {
  constructor(
    private readonly base: string,
    private readonly token: string,
  ) {}

  async health(): Promise<boolean> {
    try {
      const res = await fetch(`${this.base}/healthz`, { cache: "no-store" });
      return res.ok;
    } catch {
      return false;
    }
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    let res: Response;
    try {
      res = await fetch(`${this.base}${path}`, {
        ...init,
        cache: "no-store",
        headers: {
          Accept: "application/json",
          ...(init.body ? { "Content-Type": "application/json" } : {}),
          Authorization: `Bearer ${this.token}`,
        },
      });
    } catch {
      throw new ApiError(0, "network", "Cannot reach the Gatekeeper server.");
    }
    if (!res.ok) {
      let body: { error?: { code?: string; message?: string } } = {};
      try {
        body = await res.json();
      } catch {
        // not JSON
      }
      throw new ApiError(
        res.status,
        body.error?.code ?? `http_${res.status}`,
        body.error?.message ?? `HTTP ${res.status}`,
      );
    }
    return (await res.json()) as T;
  }

  context(url: string): Promise<PageContext> {
    return this.request(`/api/context?url=${enc(url)}`);
  }

  pr(owner: string, name: string, number: number): Promise<PrView> {
    return this.request(`/api/pr/${enc(owner)}/${enc(name)}/${number}`);
  }

  analyze(owner: string, name: string, number: number, force: boolean): Promise<AnalyzeResponse> {
    const query = force ? "?force=true" : "";
    return this.request(`/api/pr/${enc(owner)}/${enc(name)}/${number}/analyze${query}`, {
      method: "POST",
    });
  }

  activity(id: string, after = 0): Promise<{ events: ActivityEvent[]; status: string }> {
    return this.request(`/api/analyses/${enc(id)}/activity?after=${after}`);
  }

  job(id: string): Promise<JobStatus> {
    return this.request(`/api/jobs/${enc(id)}`);
  }

  tree(owner: string, name: string, pr: number): Promise<TreeResponse> {
    return this.request(`/api/repos/${enc(owner)}/${enc(name)}/tree?pr=${pr}`);
  }

  metrics(days = 30): Promise<Metrics> {
    return this.request(`/api/metrics?days=${days}`);
  }

  modelInput(id: string): Promise<ModelInput> {
    return this.request(`/api/analyses/${enc(id)}/model-input`);
  }

  chatHistory(owner: string, name: string, number: number): Promise<{ messages: ChatMessage[] }> {
    return this.request(
      `/api/chat/history?owner=${enc(owner)}&name=${enc(name)}&number=${number}`,
    );
  }

  chat(body: {
    owner: string;
    name: string;
    number: number;
    message: string;
    history: Array<{ role: string; content: string }>;
    focus_path?: string;
  }): Promise<ChatAnswer> {
    return this.request("/api/chat", { method: "POST", body: JSON.stringify(body) });
  }

  comment(id: string, text: string | null): Promise<CommentResult> {
    return this.request(`/api/analyses/${enc(id)}/comment`, {
      method: "POST",
      body: JSON.stringify({ confirm: true, ...(text !== null ? { text } : {}) }),
    });
  }

  /** Follow an analysis' activity feed. Reconnects with ?after=<last id> on network
   * errors (1, 2, 4 ... 10 s). Returns a function that stops it. */
  stream(id: string, after: number, handlers: StreamHandlers): () => void {
    const controller = new AbortController();
    let last = after;
    let delay = 1000;
    let stopped = false;

    const run = async (): Promise<void> => {
      while (!stopped) {
        try {
          const res = await fetch(`${this.base}/api/analyses/${enc(id)}/stream?after=${last}`, {
            headers: { Accept: "text/event-stream", Authorization: `Bearer ${this.token}` },
            signal: controller.signal,
            cache: "no-store",
          });
          if (!res.ok || !res.body) {
            if (res.status === 401 || res.status === 403 || res.status === 404) {
              handlers.onError(`Live feed refused (HTTP ${res.status}).`);
              return;
            }
            throw new Error(`HTTP ${res.status}`);
          }
          delay = 1000;
          const ended = await readSse(res.body, (event, data, eventId) => {
            if (event === "activity") {
              const parsed = JSON.parse(data) as ActivityEvent;
              last = eventId ? Number(eventId) : parsed.id;
              handlers.onEvent(parsed);
            }
          });
          if (ended) {
            handlers.onEnd(ended);
            return;
          }
        } catch {
          if (stopped) return;
          handlers.onError("Live feed interrupted; reconnecting.");
        }
        await new Promise((resolve) => setTimeout(resolve, delay));
        delay = Math.min(delay * 2, 10000);
      }
    };
    void run();
    return () => {
      stopped = true;
      controller.abort();
    };
  }
}

/** Parse a text/event-stream body. Returns "end"/"timeout" if the server said so. */
async function readSse(
  body: ReadableStream<Uint8Array>,
  onMessage: (event: string, data: string, id?: string) => void,
): Promise<"end" | "timeout" | null> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let event = "message";
  let data: string[] = [];
  let id: string | undefined;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return null;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n?/g, "\n");
    let index: number;
    while ((index = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, index);
      buffer = buffer.slice(index + 1);
      if (line === "") {
        if (data.length) {
          if (event === "end" || event === "timeout") return event;
          onMessage(event, data.join("\n"), id);
        }
        event = "message";
        data = [];
        id = undefined;
        continue;
      }
      if (line.startsWith(":")) continue; // heartbeat comment
      const colon = line.indexOf(":");
      const field = colon < 0 ? line : line.slice(0, colon);
      const raw = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
      if (field === "event") event = raw;
      else if (field === "data") data.push(raw);
      else if (field === "id") id = raw;
    }
  }
}
