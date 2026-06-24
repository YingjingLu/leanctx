# InsForge integration — leanctx Model Gateway compression

A deployable prompt-compression layer for [InsForge](https://github.com/InsForge/InsForge)'s
**Model Gateway**: a containerized leanctx sidecar that adds an **LLMLingua-2 semantic pass**
over the prose in each chat request, before it goes upstream to OpenRouter — cutting prompt
tokens, and therefore the OpenRouter bill, with no change to InsForge's public API.

InsForge's gateway forwards every agent LLM call to OpenRouter, billed per token. The
natural-language ("prose") parts of those prompts — pulled-in docs, schemas, logs — are
highly compressible; code, tracebacks, tool results, and multimodal content are routed to
**verbatim** so nothing load-bearing is dropped. The two are complementary.

```
InsForge Model Gateway  (chat-completion.service.ts)
   └─ formatMessages()  →  OpenAI message list
        └─ compressMessages()  ── HTTP ──▶  leanctx sidecar (this directory)
                                              POST /compress  →  LLMLingua-2 on prose
        └─ OpenRouter  (fewer prompt tokens)
```

This integration has two parts:

| Part | What | Where |
|------|------|-------|
| **Sidecar** | The Dockerized leanctx service here — the thing the gateway calls. | this directory |
| **Connector** | The InsForge-side hook that POSTs to the sidecar after `formatMessages()` and splices the result back, **fail-open**. | [`connector/`](connector/) |

> This is the *serving* sidecar. It differs from the repo-root [`Dockerfile`](../../Dockerfile),
> which is a base image that just pre-installs leanctx — this one **runs** `leanctx-serve`
> with the model baked in. Both build leanctx from the in-repo source (no PyPI release needed).

---

## Quick start (CPU)

```bash
# from the repo root
cp integrations/insforge/.env.example integrations/insforge/.env   # optional: tune ratio / threshold
docker compose -f integrations/insforge/docker-compose.yml up -d --build   # first build bakes the ~1.2 GB model in
./integrations/insforge/scripts/smoke.sh                                    # GET /health + a real /compress round-trip
```

`smoke.sh` should print something like:

```
==> GET http://localhost:8459/health
{"status":"ok","mode":"on"}
==> POST http://localhost:8459/compress  (prose + tool; expect prose shrinks, tool verbatim)
  method=hybrid  in=4879  out=2369  ratio=0.486
  OK — saved 51.4% on prose; tool verbatim
smoke: PASS
```

Then wire the connector into InsForge and point the env at the sidecar — see
[`connector/README.md`](connector/README.md). With the URL unset, the connector is a complete
no-op and the gateway behaves exactly as before.

### GPU

LLMLingua-2 is much faster on a GPU (≈ 46 ms vs. multi-second per call). Set
`LEANCTX_SERVER_LINGUA_DEVICE=cuda` (or `mps` on Apple Silicon) and run on a CUDA host; for a
ready-made GPU image, the [ClawRouter `Dockerfile.gpu`](../clawrouter/Dockerfile.gpu) is a
drop-in template (same base, `--build-arg CUDA_IMAGE` / `TORCH_INDEX_URL` to match your card).

---

## The `/compress` contract

This is the interface the [connector](connector/) codes against. The service is a pure
function over a message list — same shape in, same shape out, message count preserved.

**`POST /compress`**

```jsonc
// request
{
  "messages": [
    { "role": "system",    "content": "..." },
    { "role": "user",      "content": "<long prose>" },
    { "role": "assistant", "content": "..." },
    { "role": "tool",      "content": "<tool result>" }   // forwarded VERBATIM
  ]
}
```

```jsonc
// response
{
  "messages": [ /* same list, eligible prose turns compressed in place */ ],
  "stats": {
    "input_tokens": 4879,
    "output_tokens": 2369,
    "ratio": 0.486,            // output/input — lower is more compression
    "method": "hybrid",        // or "passthrough" when nothing was eligible
    "cost_usd": 0.0            // 0 for local LLMLingua-2
  },
  "compressed_message_count": 1
}
```

**Contract guarantees** (what the connector relies on):

- **Message count is preserved** — one-in-one-out per message; splice back by index.
- **Only `system` / `user` / `assistant` turns with *string* `content` are eligible.**
  `tool` messages and any non-string (multimodal / content-list) message are returned
  **byte-for-byte unchanged**. A lossy prose pass must never touch tool results or images.
- **Short turns are protected** — turns under `LEANCTX_SERVER_THRESHOLD` (default 1500
  tokens) pass through uncompressed, so system prompts / terse instructions are safe.
- **Fail-open is the caller's job** — the connector falls back to the un-compressed messages
  on any error or timeout, so compression can never break a request.

**`GET /health`** → `{"status":"ok","mode":"on"}` — for container/orchestrator probes.

---

## Configuration

All optional; the image ships with Model-Gateway-tuned defaults. Override via `.env`,
compose `environment:`, or `docker run -e`.

| Env var | Default | Meaning |
|---|---|---|
| `LEANCTX_SERVER_MODE` | `on` | `on` compresses; `off` = passthrough smoke test (no model load) |
| `LEANCTX_SERVER_THRESHOLD` | `1500` | Don't compress turns below this many tokens |
| `LEANCTX_SERVER_LINGUA_RATIO` | `0.5` | Fraction of prose tokens to **keep** (lower = more aggressive) |
| `LEANCTX_SERVER_LINGUA_DEVICE` | `cpu` | Inference device (`cuda` / `mps` for GPU) |
| `LEANCTX_SERVER_DEDUP` | `off` | Leave off — the gateway sends raw OpenAI messages |
| `LEANCTX_SERVER_CONFIG` | — | Full JSON Middleware config; wins over all of the above |
