// Behavioral smoke test for the leanctx Layer-8 connector.
// Runs against a live leanctx sidecar (default localhost:8459).
//
//   npm test            # builds, then runs this
//
import assert from "node:assert/strict";
import { leanctxLayer8 } from "../dist/index.js";

const URL = process.env.LEANCTX_SIDECAR_URL || "http://localhost:8459";
const longProse =
  "The quick brown fox jumps over the lazy dog, and in doing so it demonstrates " +
  "a sentence that is intentionally verbose and padded with redundant filler words. ";

const messages = [
  { role: "system", content: "You are a helpful assistant." },
  { role: "user", content: longProse.repeat(120) },
  { role: "tool", content: '{"result": [1, 2, 3], "ok": true}' }, // must stay verbatim
];

// 1) compresses eligible prose, preserves count + verbatim tool content
const out = await leanctxLayer8(messages, { url: URL });
assert.equal(out.length, messages.length, "message count must be preserved");
assert.equal(out[2].content, messages[2].content, "tool message must be verbatim");
assert.equal(typeof out[1].content, "string", "user content stays a string");
assert.ok(
  out[1].content.length < messages[1].content.length,
  "user prose should be compressed (shorter)",
);
const saved = (1 - out[1].content.length / messages[1].content.length) * 100;
console.log(
  `OK compress: user ${messages[1].content.length} -> ${out[1].content.length} chars ` +
    `(${saved.toFixed(1)}% shorter); tool verbatim; count preserved`,
);

// 2) FAIL-OPEN: an unreachable sidecar returns the input unchanged
const out2 = await leanctxLayer8(messages, { url: "http://127.0.0.1:1", timeoutMs: 1000 });
assert.deepEqual(out2, messages, "fail-open must return input unchanged");
console.log("OK fail-open: unreachable sidecar -> input returned unchanged");

// 3) no-op when unconfigured (no url, no env)
const prevEnv = process.env.LEANCTX_SIDECAR_URL;
delete process.env.LEANCTX_SIDECAR_URL;
const out3 = await leanctxLayer8(messages, {});
assert.deepEqual(out3, messages, "no url -> no-op");
if (prevEnv !== undefined) process.env.LEANCTX_SIDECAR_URL = prevEnv;
console.log("OK no-op: unconfigured -> input returned unchanged");

console.log("smoke: PASS");
