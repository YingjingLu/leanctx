# leanctx connector for the InsForge Model Gateway

The InsForge-side half of the integration: a small, typed, **fail-open** TypeScript
function that hands the gateway's formatted OpenAI messages to the
[leanctx sidecar](../README.md) for an LLMLingua-2 semantic pass over the prose, then
takes the compressed result back — before the upstream OpenRouter call.

## Contract

`compressMessages(messages, options?)` → `Promise<messages>`:

- **Same shape in, same shape out**; message count + order preserved.
- Only `system`/`user`/`assistant` turns with **string** content are compressed; `tool`
  results and multimodal content come back **verbatim** (enforced server-side).
- **Fail-open**: any error / timeout / misconfiguration returns the input unchanged, so
  compression can never break a chat request.
- **No-op when unconfigured**: if no `url` is passed and `LEANCTX_SIDECAR_URL` is unset, it
  returns the input untouched — safe to wire in unconditionally and enable via env.

```ts
import { compressMessages } from "@leanctx/insforge-connector";

const out = await compressMessages(messages, {
  // url: "http://localhost:8459",  // default: process.env.LEANCTX_SIDECAR_URL
  // timeoutMs: 2000,
  // minChars: 6000,                // request-level gate (skip tiny requests)
  // onError: console.warn,
});
```

## Wiring into InsForge

In `backend/src/services/ai/chat-completion.service.ts`, wrap the `formatMessages()` call
in **both** `chat()` and `streamChat()` — the line right before the `request` object is
built:

```ts
import { compressMessages } from "@leanctx/insforge-connector";

// was: const formattedMessages = this.formatMessages(messages);
const formattedMessages = await compressMessages(this.formatMessages(messages));
```

Then run the sidecar and point the env at it:

```bash
docker compose -f integrations/insforge/docker-compose.yml up -d   # the sidecar
export LEANCTX_SIDECAR_URL=http://localhost:8459
```

With `LEANCTX_SIDECAR_URL` unset, the line is a complete no-op and the gateway behaves
exactly as before. If you wire the URL through InsForge's own config (e.g. an
`AI_COMPRESSION_URL` env on `appConfig.ai`), pass it explicitly:
`compressMessages(msgs, { url: appConfig.ai.compressionUrl })`.

## Develop / verify

```bash
cd integrations/insforge/connector
npm install
npm run typecheck     # strict tsc, no emit
npm test              # builds, then runs the unit tests (mocked fetch, no infra)
npm run test:smoke    # builds, then runs a behavioral test against a live sidecar
```

`npm test` (`test/unit.mjs`) mocks `globalThis.fetch`, so it runs in plain CI with no
sidecar. It asserts: success returns the sidecar's messages, count-mismatch / non-ok
responses fall back to the input, fail-open on a rejected fetch (with `onError`), the
no-op-when-unconfigured path, the eligibility and `minChars` gates, and URL normalization.

`npm run test:smoke` (`test/smoke.mjs`) expects a real sidecar on `http://localhost:8459`
(override with `LEANCTX_SIDECAR_URL`) and asserts the end-to-end round-trip: prose
compressed, tool message verbatim, count preserved.

## Why a connector + a sidecar (not one process)?

InsForge's backend is TypeScript; leanctx's compressors are a Python PyTorch model. The
sidecar keeps one warm model in memory and exposes it over HTTP; this connector is the
thin, fail-open TS client that calls it from inside the Model Gateway. See
[`../README.md`](../README.md) for the sidecar and the full `/compress` contract.
