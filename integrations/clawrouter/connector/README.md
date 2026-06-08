# leanctx Layer-8 connector (P2b)

The ClawRouter-side half of the integration: a small, typed, **fail-open** TypeScript
function that hands ClawRouter's post-Layer-7 messages to the [leanctx sidecar](../README.md)
for an LLMLingua-2 semantic pass, then takes the compressed result back.

It productizes the hook the [Phase-1 benchmark](../../../benchmarks/clawrouter) proved out
into a clean module you wire in once — the benchmark now imports this connector rather than
string-injecting its own copy of the Layer-8 hook.

## Contract

`leanctxLayer8(messages, options?)` → `Promise<messages>`:

- **Same shape in, same shape out**; message count + order preserved.
- Only `system`/`user`/`assistant` turns with **string** content are compressed; `tool`
  results and multimodal content come back **verbatim** (enforced server-side).
- **Fail-open**: any error / timeout / misconfiguration returns the input unchanged, so
  Layer 8 can never break a request.
- **No-op when unconfigured**: if no `url` is passed and `LEANCTX_SIDECAR_URL` is unset, it
  returns the input untouched — safe to wire in unconditionally and enable via env.

```ts
import { leanctxLayer8 } from "@leanctx/clawrouter-connector";

result = await leanctxLayer8(result, {
  // url: "http://leanctx:8459",   // default: process.env.LEANCTX_SIDECAR_URL
  // timeoutMs: 60000,
  // minChars: 5000,               // request-level gate (skip tiny requests)
  // onError: console.warn,
});
```

## Wiring into ClawRouter

In ClawRouter's `src/compression/index.ts`, immediately **before** the anchor line

```ts
const compressedChars = calculateTotalChars(result);
```

add a single line (this is the same insertion point the benchmark patches, at CR commit
`89269507`):

```ts
// Layer 8: leanctx LLMLingua-2 prose pass (opt-in, fail-open)
result = await leanctxLayer8(result);
const compressedChars = calculateTotalChars(result);   // <- existing anchor
```

Then run the sidecar and point the env at it:

```bash
docker compose -f integrations/clawrouter/docker-compose.yml up -d   # the sidecar (P2a)
export LEANCTX_SIDECAR_URL=http://localhost:8459
```

With `LEANCTX_SIDECAR_URL` unset, the line is a complete no-op and ClawRouter behaves
exactly as before.

## Develop / verify

```bash
cd integrations/clawrouter/connector
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

ClawRouter is TypeScript; leanctx's compressors are a Python PyTorch model. The sidecar
keeps one warm model in memory and exposes it over HTTP; this connector is the thin,
fail-open TS client that calls it from inside `compressContext`. See
[`../README.md`](../README.md) for the sidecar and the full `/compress` contract.
