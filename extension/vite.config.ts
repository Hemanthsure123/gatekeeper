// Plain Vite multi-entry build for a Manifest V3 extension (no extension framework).
// - panel/index.html and options/index.html are extension pages;
// - background.js is the module service worker;
// - content.js is a classic content script, so src/content.ts must stay import-free.
// public/ (manifest.json, icons/) is copied to dist/ as-is.
import { resolve } from "node:path";
import { defineConfig } from "vite";

const src = resolve(import.meta.dirname, "src");

export default defineConfig({
  root: src,
  publicDir: resolve(import.meta.dirname, "public"),
  base: "/",
  build: {
    outDir: resolve(import.meta.dirname, "dist"),
    emptyOutDir: true,
    target: "chrome116",
    modulePreload: { polyfill: false },
    sourcemap: false,
    rollupOptions: {
      input: {
        panel: resolve(src, "panel/index.html"),
        options: resolve(src, "options/index.html"),
        background: resolve(src, "background.ts"),
        content: resolve(src, "content.ts"),
      },
      output: {
        entryFileNames: "[name].js",
        chunkFileNames: "chunks/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]",
      },
    },
  },
});
