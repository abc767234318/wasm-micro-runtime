import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the bilingual specification landing page", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);
  const html = await response.text();
  assert.match(html, /Wasm Spec 中英对照/);
  assert.match(html, /WebAssembly Core Specification/);
  assert.match(html, /开始阅读/);
  assert.match(html, /\/spec\/index\.html/);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton|Your site is taking shape/);
});

test("packages the complete verified static specification", async () => {
  const [metadata, index, socialCard, bilingualCss] = await Promise.all([
    readFile(new URL("../public/spec/_bilingual/metadata.json", import.meta.url), "utf8"),
    readFile(new URL("../public/spec/index.html", import.meta.url), "utf8"),
    readFile(new URL("../public/spec/_bilingual/og.png", import.meta.url)),
    readFile(new URL("../public/spec/_bilingual/bilingual.css", import.meta.url), "utf8"),
  ]);
  const parsed = JSON.parse(metadata);
  assert.equal(parsed.page_count, 50);
  assert.equal(parsed.skipped_page_count, 0);
  assert.ok(parsed.search_entry_count >= 900);
  assert.match(index, /wasm-bi-panel-en/);
  assert.match(index, /wasm-bi-panel-zh/);
  assert.ok(socialCard.byteLength > 100_000);
  assert.match(bilingualCss, /\.wasm-bi-active \.documentwrapper,/);
  assert.match(bilingualCss, /\.wasm-bi-active \.body \{\s*display: flex;/);
  assert.match(bilingualCss, /\.wasm-bi-columns \{[\s\S]*?flex: 1 1 auto;[\s\S]*?min-height: 0;/);
  assert.match(bilingualCss, /\.wasm-bi-panel \{[\s\S]*?min-height: 0;[\s\S]*?overflow-y: auto;/);
});
