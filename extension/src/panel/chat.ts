// Chat tab: grounded, read-only questions about this pull request's review.
// Untrusted text (answers, past messages) is rendered with the safe Markdown subset in
// dom.ts, from text nodes only.
import type { ChatMessage, ChatReference } from "../shared/types";
import type { Api } from "./api";
import { ApiError } from "./api";
import { h, renderMarkdown, span } from "./dom";
import { icon } from "./icons";
import type { State } from "./store";
import { store } from "./store";

export interface ChatDeps {
  api: () => Api | null;
  revealRange: (path: string, start: number, end: number) => void;
}

const SUGGESTIONS = [
  "Why this verdict?",
  "What should the contributor fix?",
  "Which file matters most?",
  "What did the model see?",
];
const HISTORY_SENT = 8;

interface ChatView {
  key: string;
  messages: ChatMessage[];
  thinking: boolean;
  error: string;
  version: number;
}

let view: ChatView = { key: "", messages: [], thinking: false, error: "", version: 0 };
let renderedVersion = -1;
let mountedRoot: HTMLElement | null = null;
let deps: ChatDeps | null = null;
let historyFor = "";

function prKey(state: State): string {
  return state.ref ? `${state.ref.owner}/${state.ref.name}#${state.ref.number}` : "";
}

function update(patch: Partial<ChatView>): void {
  view = { ...view, ...patch, version: view.version + 1 };
  if (mountedRoot) paint(mountedRoot, store.get());
}

async function loadHistory(state: State): Promise<void> {
  const api = deps?.api();
  if (!api || !state.ref) return;
  const key = prKey(state);
  try {
    const { messages } = await api.chatHistory(state.ref.owner, state.ref.name, state.ref.number);
    if (view.key === key) update({ messages });
  } catch {
    if (view.key === key) update({ error: "Could not load the chat history." });
  }
}

async function send(text: string): Promise<void> {
  const state = store.get();
  const api = deps?.api();
  const message = text.trim();
  if (!api || !state.ref || !message || view.thinking) return;
  const key = view.key;
  const local: ChatMessage = {
    id: `local-${Date.now()}`,
    role: "user",
    content: message,
    references: [],
    created_at: new Date().toISOString(),
  };
  const history = view.messages.slice(-HISTORY_SENT).map((m) => ({ role: m.role, content: m.content }));
  update({ messages: [...view.messages, local], thinking: true, error: "" });
  try {
    const reply = await api.chat({
      owner: state.ref.owner,
      name: state.ref.name,
      number: state.ref.number,
      message,
      history,
      ...(state.chatFocus ? { focus_path: state.chatFocus } : {}),
    });
    if (view.key !== key) return;
    update({
      thinking: false,
      messages: [
        ...view.messages,
        {
          id: reply.message_id,
          role: "assistant",
          content: reply.answer,
          references: reply.references,
          created_at: new Date().toISOString(),
        },
      ],
    });
  } catch (error) {
    if (view.key !== key) return;
    const line =
      error instanceof ApiError
        ? error.status === 404
          ? "Analyse this pull request before asking about it."
          : `The answer failed: ${error.message}`
        : "The answer failed.";
    update({ thinking: false, error: line });
  }
}

function referenceChip(ref: ChatReference): HTMLElement {
  const label = `${ref.path.split("/").pop() ?? ref.path}:${span(ref.line_start, ref.line_end)}`;
  return h(
    "button",
    {
      type: "button",
      class: `chip ref ${ref.verified ? "verified" : "unverified"}`,
      title: `${ref.path}:${span(ref.line_start, ref.line_end)}${
        ref.verified ? " (verified in the diff)" : " (not found in the diff shown)"
      }`,
      on: { click: () => deps?.revealRange(ref.path, ref.line_start, ref.line_end) },
    },
    h("span", { "aria-hidden": "true" }, ref.verified ? "✓ " : "? "),
    label,
    h("span", { class: "visually-hidden" }, ref.verified ? ", verified" : ", not verified"),
  );
}

function bubble(message: ChatMessage): HTMLElement {
  const body = h("div", { class: "bubble-body" });
  if (message.role === "assistant") body.append(renderMarkdown(message.content));
  else body.append(h("p", null, message.content));
  const refs = message.references.length
    ? h("div", { class: "refs" }, ...message.references.map(referenceChip))
    : null;
  return h(
    "li",
    { class: `bubble ${message.role}` },
    h("span", { class: "visually-hidden" }, message.role === "user" ? "You:" : "Gatekeeper:"),
    body,
    refs,
  );
}

function mount(root: HTMLElement): void {
  const input = h("textarea", {
    id: "chat-input",
    rows: 2,
    placeholder: "Ask about this review",
    "aria-label": "Your question",
    maxlength: "2000",
  });
  const form = h(
    "form",
    {
      class: "chat-form",
      on: {
        submit: (event: SubmitEvent) => {
          event.preventDefault();
          const text = input.value;
          input.value = "";
          void send(text);
        },
      },
    },
    input,
    h("button", { type: "submit", class: "primary", id: "chat-send" }, "Send"),
  );
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  root.replaceChildren(
    h("p", { id: "chat-note", class: "muted small" }),
    h("div", { id: "chat-focus", class: "focus", hidden: true }),
    h(
      "div",
      { id: "chat-empty", class: "empty chat-empty", hidden: true },
      h("span", { class: "empty-mark", "aria-hidden": "true" }, icon("chat")),
      h("h2", null, "Ask about this review"),
      h("p", null, "Why the verdict, what to fix, which file matters most. Answers cite lines you can open in GitHub."),
    ),
    h("ol", { id: "chat-list", class: "chat-list", "aria-live": "polite" }),
    h("p", { id: "chat-error", class: "state-line", role: "status" }),
    h(
      "div",
      { class: "suggestions", role: "group", "aria-label": "Suggested questions" },
      ...SUGGESTIONS.map((text) =>
        h("button", { type: "button", class: "chip", on: { click: () => void send(text) } }, text),
      ),
    ),
    form,
  );
}

function paint(root: HTMLElement, state: State): void {
  const available = state.view === "result" && !!state.pr;
  const note = root.querySelector("#chat-note") as HTMLElement;
  note.textContent = available
    ? "Answers come only from this review and its diff. Chat cannot take any action."
    : "Open an analysed pull request to ask about its review.";
  (root.querySelector("#chat-send") as HTMLButtonElement).disabled = !available || view.thinking;
  for (const chip of root.querySelectorAll<HTMLButtonElement>(".suggestions button")) {
    chip.disabled = !available || view.thinking;
  }

  const focus = root.querySelector("#chat-focus") as HTMLElement;
  focus.hidden = !state.chatFocus;
  focus.replaceChildren(
    ...(state.chatFocus
      ? [
          h("span", { class: "muted" }, "About: "),
          h("code", null, state.chatFocus),
          h(
            "button",
            {
              type: "button",
              class: "icon-btn",
              "aria-label": "Stop focusing on this file",
              on: { click: () => store.set({ chatFocus: null }) },
            },
            "✕",
          ),
        ]
      : []),
  );

  (root.querySelector("#chat-error") as HTMLElement).textContent = view.error;
  (root.querySelector("#chat-empty") as HTMLElement).hidden = !available || view.messages.length > 0 || view.thinking;
  if (renderedVersion === view.version) return;
  renderedVersion = view.version;
  const list = root.querySelector("#chat-list") as HTMLElement;
  list.replaceChildren(...view.messages.map(bubble));
  if (view.thinking) {
    list.append(
      h(
        "li",
        { class: "bubble assistant thinking", "aria-busy": "true" },
        h("span", { class: "spinner", "aria-hidden": "true" }),
        " Thinking…",
      ),
    );
  }
  list.lastElementChild?.scrollIntoView({ block: "nearest" });
}

export function renderChat(root: HTMLElement, state: State, chatDeps: ChatDeps): void {
  deps = chatDeps;
  if (mountedRoot !== root) {
    mount(root);
    mountedRoot = root;
    renderedVersion = -1;
  }
  const key = prKey(state);
  if (key !== view.key) {
    view = { key, messages: [], thinking: false, error: "", version: view.version + 1 };
  }
  if (key && state.view === "result" && historyFor !== key) {
    historyFor = key;
    void loadHistory(state);
  }
  paint(root, state);
}
