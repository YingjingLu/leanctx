/**
 * leanctx connector for the InsForge Model Gateway.
 *
 * After InsForge's `ChatCompletionService.formatMessages()` produces the OpenAI
 * message list, hand it to the leanctx sidecar for an LLMLingua-2 semantic pass
 * over the prose, then take the result back — cutting prompt tokens (and the
 * OpenRouter bill) before the upstream call.
 *
 * Design contract (matches the sidecar's POST /compress):
 *   - Same shape in, same shape out; message COUNT is preserved.
 *   - Only system/user/assistant turns with string content are compressed;
 *     tool results and multimodal content are returned verbatim (server-side).
 *   - FAIL-OPEN: any error/timeout/misconfiguration returns the input
 *     unchanged, so compression can never break a chat request.
 *
 * Wiring: in InsForge's `backend/src/services/ai/chat-completion.service.ts`,
 * wrap the formatMessages() call in both chat() and streamChat():
 *   const formattedMessages = await compressMessages(this.formatMessages(messages));
 * and set LEANCTX_SIDECAR_URL in the environment. See README.md.
 */

/**
 * An OpenAI-format chat message — structurally typed so this module needn't
 * import InsForge's `ChatCompletionMessageParam`. Anything with a `role` +
 * `content` works (the gateway already hands us exactly that shape).
 */
export interface ChatMessage {
  role: string;
  content: unknown;
  [key: string]: unknown;
}

export interface CompressOptions {
  /**
   * leanctx sidecar base URL. Defaults to `process.env.LEANCTX_SIDECAR_URL`.
   * When neither is provided, the connector is a no-op (returns input
   * unchanged) — so it's safe to wire in unconditionally and enable via env.
   */
  url?: string;
  /**
   * Per-request timeout in ms. Default 60000. This is an inline gateway call,
   * but the shipped sidecar runs LLMLingua-2 on CPU where a pass is
   * multi-second, so a tight budget would make the call time out and fail-open
   * to the *uncompressed* request — i.e. silently disable compression. A dead
   * sidecar still fails fast (connection refused), so this only bounds the
   * reachable-but-slow case. Lower it (e.g. ~2000) only with a GPU sidecar.
   */
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
 *
 * The constraint is the structural minimum (`role` + `content`) rather than the
 * full `ChatMessage` interface: the latter's `[key: string]: unknown` index
 * signature blocks generic inference for callers whose message type lacks one
 * (e.g. a closed-union `role`), so the documented one-line wiring would not
 * compile.
 */
export async function compressMessages<M extends { role: string; content: unknown }>(
  messages: M[],
  options: CompressOptions = {},
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

export default compressMessages;
