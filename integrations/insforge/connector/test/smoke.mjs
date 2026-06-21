// End-to-end smoke against a live leanctx sidecar (default http://localhost:8459).
//   LEANCTX_SIDECAR_URL=http://localhost:8459 node test/smoke.mjs
// Asserts the real round-trip: prose compressed, tool message verbatim, count preserved.
import assert from "node:assert/strict";
import { compressMessages } from "../dist/index.js";

const url = process.env.LEANCTX_SIDECAR_URL || "http://localhost:8459";
const prose =
  "The InsForge Model Gateway proxies chat requests to OpenRouter and meters " +
  "token usage for every call a coding agent makes. ".repeat(200);
const messages = [
  { role: "system", content: "You are a helpful assistant." },
  { role: "user", content: prose },
  { role: "tool", tool_call_id: "t1", content: '{"rows": 42}' },
];

const out = await compressMessages(messages, { url, timeoutMs: 60000 });

assert.equal(out.length, messages.length, "message count preserved");
const toolIn = messages.find((m) => m.role === "tool").content;
const toolOut = out.find((m) => m.role === "tool").content;
assert.equal(toolIn, toolOut, "tool message forwarded verbatim");
const userIn = messages.find((m) => m.role === "user").content.length;
const userOut = out.find((m) => m.role === "user").content.length;
assert.ok(userOut < userIn, "prose compressed");

console.log(
  `smoke OK — prose ${userIn} -> ${userOut} chars ` +
    `(${(100 * (1 - userOut / userIn)).toFixed(1)}% shorter), tool verbatim, count preserved`,
);
