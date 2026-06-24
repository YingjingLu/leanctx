// Unit tests for the InsForge connector. Mocks globalThis.fetch, so it runs in
// plain CI with no sidecar.   Run:  npm test   (builds, then `node --test`).
import test from "node:test";
import assert from "node:assert/strict";
import { compressMessages } from "../dist/index.js";

const realFetch = globalThis.fetch;
const mockFetch = (impl) => { globalThis.fetch = impl; };
const restore = () => { globalThis.fetch = realFetch; };

const big = [{ role: "user", content: "x".repeat(9000) }];

test("no-op when unconfigured (no url, no env): does not fetch", async () => {
  delete process.env.LEANCTX_SIDECAR_URL;
  let called = false;
  mockFetch(async () => { called = true; return { ok: true, json: async () => ({}) }; });
  const out = await compressMessages(big);
  assert.equal(out, big);
  assert.equal(called, false);
  restore();
});

test("returns the sidecar's messages on success; normalizes URL + payload", async () => {
  const compressed = [{ role: "user", content: "short" }];
  mockFetch(async (url, init) => {
    assert.equal(url, "http://sidecar:8459/compress"); // trailing slash trimmed
    assert.deepEqual(JSON.parse(init.body), { messages: big });
    return { ok: true, json: async () => ({ messages: compressed }) };
  });
  const out = await compressMessages(big, { url: "http://sidecar:8459/" });
  assert.deepEqual(out, compressed);
  restore();
});

test("falls back to input on a non-OK status", async () => {
  mockFetch(async () => ({ ok: false, status: 503, json: async () => ({}) }));
  const out = await compressMessages(big, { url: "http://sidecar:8459" });
  assert.equal(out, big);
  restore();
});

test("falls back to input on a message-count mismatch", async () => {
  mockFetch(async () => ({ ok: true, json: async () => ({ messages: [] }) }));
  const out = await compressMessages(big, { url: "http://sidecar:8459" });
  assert.equal(out, big);
  restore();
});

test("fail-open on a rejected fetch, and invokes onError", async () => {
  mockFetch(async () => { throw new Error("timeout"); });
  let seen;
  const out = await compressMessages(big, {
    url: "http://sidecar:8459",
    onError: (e) => { seen = e; },
  });
  assert.equal(out, big);
  assert.ok(seen instanceof Error);
  restore();
});

test("no-op when there is no eligible prose (tool message only)", async () => {
  let called = false;
  mockFetch(async () => { called = true; return { ok: true, json: async () => ({}) }; });
  const input = [{ role: "tool", content: "x".repeat(9000) }];
  const out = await compressMessages(input, { url: "http://sidecar:8459" });
  assert.equal(called, false);
  assert.equal(out, input);
  restore();
});

test("minChars gate skips small requests", async () => {
  let called = false;
  mockFetch(async () => { called = true; return { ok: true, json: async () => ({}) }; });
  const input = [{ role: "user", content: "tiny prompt" }];
  const out = await compressMessages(input, { url: "http://sidecar:8459", minChars: 6000 });
  assert.equal(called, false);
  assert.equal(out, input);
  restore();
});
