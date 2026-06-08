/**
 * leanctx "Layer 8" connector for ClawRouter.
 *
 * Productizes the prose-compression hook the Phase-1 benchmark proved out
 * (../../benchmarks/clawrouter, which now imports this module rather than
 * string-injecting its own copy): after ClawRouter's structural layers (1–7)
 * produce the message list, hand it to the leanctx sidecar for an LLMLingua-2
 * semantic pass, then take the result back.
 *
 * Design contract (matches the sidecar's POST /compress):
 *   - Same shape in, same shape out; message COUNT is preserved.
 *   - Only system/user/assistant turns with string content are compressed;
 *     tool results and multimodal content are returned verbatim (server-side).
 *   - FAIL-OPEN: any error/timeout/misconfiguration returns the input
 *     unchanged, so Layer 8 can never break a request.
 *
 * Wiring: in ClawRouter's `src/compression/index.ts`, immediately before
 *   const compressedChars = calculateTotalChars(result);
 * add:
 *   result = await leanctxLayer8(result);
 * and set LEANCTX_SIDECAR_URL in the environment. See README.md.
 */

/**
 * A ClawRouter message — structurally typed so this module needn't import
 * ClawRouter's internal `NormalizedMessage`. Anything with a `role` + `content`.
 */
export interface Layer8Message {
  role: string;
  content: unknown;
  [key: string]: unknown;
}

export interface Layer8Options {
  /**
   * leanctx sidecar base URL. Defaults to `process.env.LEANCTX_SIDECAR_URL`.
   * When neither is provided, the connector is a no-op (returns input
   * unchanged) — so it's safe to wire in unconditionally and enable via env.
   */
  url?: string;
  /** Per-request timeout in ms. Default 60000. */
  timeoutMs?: number;
  /**
   * Request-level gate: skip the round-trip when the total string-content
   * length is below this many characters. The sidecar also gates per message
   * at `threshold_tokens`, but gating here avoids the network hop for small
   * requests. Default 0 (always call when there's eligible content).
   */
  minChars?: number;
  /** Optional error hook for observability (e.g. `console.warn`). Default: silent. */
  onError?: (err: unknown) => void;
}

const ELIGIBLE_ROLES = new Set(["system", "user", "assistant"]);

function envUrl(): string | undefined {
  return typeof process !== "undefined" ? process.env?.LEANCTX_SIDECAR_URL : undefined;
}

/**
 * Compress eligible prose in `messages` via the leanctx sidecar.
 *
 * Always resolves; on any error/timeout/misconfiguration it returns the input
 * unchanged (fail-open). Message count and order are preserved.
 */
// Constraint is the structural minimum (role + content) rather than the full
// `Layer8Message` interface: the latter's `[key: string]: unknown` index
// signature blocks generic inference for callers whose message type lacks one
// (e.g. ClawRouter's `NormalizedMessage`, whose `role` is a closed union), so
// the documented `result = await leanctxLayer8(result)` wiring would not compile.
export async function leanctxLayer8<M extends { role: string; content: unknown }>(
  messages: M[],
  options: Layer8Options = {},
): Promise<M[]> {
  const url = options.url ?? envUrl();
  if (!url) return messages; // unconfigured → no-op

  // Only worth a round-trip if there's eligible (compressible) prose.
  let eligible = false;
  let totalChars = 0;
  for (const m of messages) {
    if (ELIGIBLE_ROLES.has(m.role) && typeof m.content === "string") {
      eligible = true;
      totalChars += m.content.length;
    }
  }
  if (!eligible) return messages;
  if (options.minChars && totalChars < options.minChars) return messages;

  const timeoutMs = options.timeoutMs ?? 60000;
  try {
    const resp = await fetch(`${url.replace(/\/+$/, "")}/compress`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages }),
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!resp.ok) return messages;
    const body = (await resp.json()) as { messages?: M[] };
    // Trust the result only if the one-in-one-out invariant holds.
    if (Array.isArray(body?.messages) && body.messages.length === messages.length) {
      return body.messages;
    }
    return messages;
  } catch (err) {
    options.onError?.(err);
    return messages; // sidecar down / timeout / bad response → fail-open
  }
}

export default leanctxLayer8;
