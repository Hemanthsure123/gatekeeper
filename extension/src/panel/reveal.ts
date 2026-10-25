// GitHub-tab interaction from the panel. The panel never touches github.com itself:
// it asks the background worker, which updates the tied tab and talks to the content
// script. Nothing happens when "Follow clicks in GitHub tab" is off.
import type { NavigateMessage, RevealMessage, RevealResult } from "../shared/messages";
import { store } from "./store";

const GITHUB = "https://github.com";

function following(): boolean {
  return store.get().settings?.follow ?? true;
}

function encodePath(path: string): string {
  return path.split("/").map(encodeURIComponent).join("/");
}

export async function navigate(url: string, force = false): Promise<void> {
  const { windowId } = store.get();
  if ((!following() && !force) || windowId === null) return;
  const message: NavigateMessage = { type: "gk-navigate", windowId, url };
  await chrome.runtime.sendMessage(message);
}

/** Reveal a changed file (optionally a new-side line range) in the PR's Files changed. */
export async function revealFile(
  path: string,
  lineStart?: number,
  lineEnd?: number,
  force = false,
): Promise<RevealResult | null> {
  const { windowId, ref, pr } = store.get();
  if ((!following() && !force) || windowId === null || !ref || !pr) return null;
  const file = pr.files.find((f) => f.path === path);
  const message: RevealMessage = {
    type: "gk-reveal-file",
    windowId,
    owner: ref.owner,
    name: ref.name,
    number: ref.number,
    path,
    fallbackSha: file?.status === "removed" ? pr.base_sha : pr.head_sha,
    lineStart,
    lineEnd,
  };
  return (await chrome.runtime.sendMessage(message)) as RevealResult;
}

function baseSha(): string | null {
  const { tree, pr } = store.get();
  return tree?.ref ?? pr?.base_sha ?? null;
}

export function openFolder(path: string): Promise<void> {
  const { ref } = store.get();
  const sha = baseSha();
  if (!ref || !sha) return Promise.resolve();
  return navigate(`${GITHUB}/${ref.owner}/${ref.name}/tree/${sha}/${encodePath(path)}`);
}

export function openBlob(path: string, force = false): Promise<void> {
  const { ref } = store.get();
  const sha = baseSha();
  if (!ref || !sha) return Promise.resolve();
  return navigate(`${GITHUB}/${ref.owner}/${ref.name}/blob/${sha}/${encodePath(path)}`, force);
}

export function openPr(owner: string, name: string, number: number): Promise<void> {
  return navigate(`${GITHUB}/${owner}/${name}/pull/${number}`, true);
}
