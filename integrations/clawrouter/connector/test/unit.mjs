// Unit tests for the leanctx Layer-8 connector — no live sidecar required.
// `globalThis.fetch` is mocked, so these run in plain CI:
//
//   npm test            # builds, then runs this
//
// The behavioral round-trip against a real sidecar lives in smoke.mjs
// (`npm run test:smoke`).
import { test } from "node:test";
import assert from "node:assert/strict";
import { leanctxLayer8 } from "../dist/index.js";

const realFetch = globalThis.fetch;
function withFetch(impl, fn) {
  globalThis.fetch = impl;
  return Promise.resolve()
    .then(fn)
    .finally(() => {
      globalThis.fetch = realFetch;
    });
}

const eligible = [
  { role: "system", content: "you are a helpful assistant" },
  { role: "user", content: "x".repeat(200) },
  { role: "tool", content: "verbatim tool result" },
];

test("no-op when unconfigured (no url, no env) — never hits the network", async () => {
  const prev = process.env.LEANCTX_SIDECAR_URL;
  delete process.env.LEANCTX_SIDECAR_URL;
  let called = false;
  try {
    await withFetch(
      async () => {
        called = true;
      },
      async () => {
        const out = await leanctxLayer8(eligible, {});
        assert.equal(out, eligible, "same reference returned");
        assert.equal(called, false, "must not call fetch when unconfigured");
      },
    );
  } finally {
    if (prev !== undefined) process.env.LEANCTX_SIDECAR_URL = prev;
  }
});

test("no eligible prose (tool-only) → no round-trip", async () => {
  let called = false;
  await withFetch(
    async () => {
      called = true;
    },
    async () => {
      const toolOnly = [{ role: "tool", content: "x".repeat(500) }];
      const out = await leanctxLayer8(toolOnly, { url: "http://sidecar" });
      assert.equal(out, toolOnly);
      assert.equal(called, false);
    },
  );
});

test("minChars gate skips the round-trip for small requests", async () => {
  let called = false;
  await withFetch(
    async () => {
      called = true;
    },
    async () => {
      const small = [{ role: "user", content: "short" }];
      const out = await leanctxLayer8(small, { url: "http://sidecar", minChars: 1000 });
      assert.equal(out, small);
      assert.equal(called, false);
    },
  );
});

test("success → returns the sidecar's messages; POSTs to /compress", async () => {
  const compressed = [
    { role: "system", content: "you are a helpful assistant" },
    { role: "user", content: "short" },
    { role: "tool", content: "verbatim tool result" },
  ];
  await withFetch(
    async (url, init) => {
      assert.match(String(url), /\/compress$/);
      assert.equal(init.method, "POST");
      assert.equal(JSON.parse(init.body).messages.length, eligible.length);
      return { ok: true, json: async () => ({ messages: compressed }) };
    },
    async () => {
      const out = await leanctxLayer8(eligible, { url: "http://sidecar" });
      assert.deepEqual(out, compressed);
    },
  );
});

test("trailing slashes in the url are normalized", async () => {
  let seen = "";
  await withFetch(
    async (url) => {
      seen = String(url);
      return { ok: true, json: async () => ({ messages: eligible }) };
    },
    async () => {
      await leanctxLayer8(eligible, { url: "http://sidecar///" });
      assert.equal(seen, "http://sidecar/compress");
    },
  );
});

test("count mismatch → returns input unchanged (one-in-one-out guard)", async () => {
  await withFetch(
    async () => ({
      ok: true,
      json: async () => ({ messages: [{ role: "user", content: "only one" }] }),
    }),
    async () => {
      const out = await leanctxLayer8(eligible, { url: "http://sidecar" });
      assert.equal(out, eligible);
    },
  );
});

test("non-ok response → returns input unchanged", async () => {
  await withFetch(
    async () => ({ ok: false, json: async () => ({}) }),
    async () => {
      const out = await leanctxLayer8(eligible, { url: "http://sidecar" });
      assert.equal(out, eligible);
    },
  );
});

test("fail-open on fetch rejection; onError receives the error", async () => {
  let errSeen = null;
  await withFetch(
    async () => {
      throw new Error("boom");
    },
    async () => {
      const out = await leanctxLayer8(eligible, {
        url: "http://sidecar",
        onError: (e) => {
          errSeen = e;
        },
      });
      assert.equal(out, eligible, "fail-open returns the input");
      assert.ok(errSeen instanceof Error);
      assert.equal(errSeen.message, "boom");
    },
  );
});
