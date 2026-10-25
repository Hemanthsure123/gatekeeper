# Gatekeeper side panel (Chromium, Manifest V3)

A side panel for GitHub pull requests, backed by the local Gatekeeper API.

## Tabs

- **Activity:**
  - the verdict card;
  - a live "Now:" line;
  - the repository tree, with each changed file's stage (listed, signalled, in context or omitted, cited, verified or dropped);
  - the timeline.
- **Chat:** questions about the review, answered from the stored analysis and diff.
- **Insights:** simple metrics, plus what the model saw.

## Build and load

```sh
make extension-install   # npm dev tools: typescript, vite, @types/chrome
make extension-build     # draws the icons, type-checks, builds extension/dist
```

1. Open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked** and pick `extension/dist`.
2. Open the extension's **Options**:
   - set the server URL (default `http://127.0.0.1:8000`);
   - paste the value of `GATEKEEPER_ADMIN_TOKEN` from your `.env`;
   - click **Test connection**.
3. Open a pull request on github.com and click the Gatekeeper toolbar icon.

`make extension-dev` rebuilds on every change. After a rebuild, click **Reload** on the extension card.

## Design rules

These are also in CLAUDE.md.

- **Network:**
  - every API call comes from the side panel, which runs on the extension origin;
  - the content script only scrolls and highlights on github.com, and replies found or not found. It makes no network calls.
- **No remote code or framework:** plain TypeScript built by Vite.
- **Accessibility:** status is always an icon plus text; reduced motion is respected; the panel works from 320px wide.
- **Read-only, with one exception:** posting one comment, after a person approves its exact text.

## Revealing files in GitHub

GitHub doesn't document the markup or the anchors of its Files changed page, so the content script tries these in order, retrying for about 4 seconds:

1. the diff anchor `#diff-<sha256(path)>`, with `R<n>` added for new-side lines;
2. data attributes that carry the path;
3. links to the anchor;
4. a file header whose text is the path.

If none of these finds the file:

1. the background script sets the anchor in the URL;
2. if that fails too, it opens the file's own page at the PR's commit.
