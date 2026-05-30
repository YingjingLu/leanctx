# leanctx HTTP compression service

`leanctx[server]` runs leanctx as a long-lived HTTP sidecar so **non-Python consumers** — a TypeScript proxy, a LiteLLM callback, a Go gateway, any cross-language caller — can use leanctx's LLMLingua-2 / SelfLLM compression over a simple `POST /compress`.

The service is opt-in and is never imported by `import leanctx`, so the core library's cold-import budget is unaffected.

## Run it

```bash
pip install 'leanctx[server,lingua]'
leanctx-serve --host 127.0.0.1 --port 8459
# or: uvicorn leanctx.server:app --host 127.0.0.1 --port 8459 --workers 1
```

The first startup pays the one-time ~1.2 GB LLMLingua-2 model load (warmed before the service accepts traffic), so per-request latency is steady-state afterward.

## Call it

```bash
curl -s localhost:8459/compress \
  -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"<long prose>"}]}'
```

Response:

```json
{
  "messages": [ ... compressed messages, same order/count ... ],
  "stats": {
    "input_tokens": 280,
    "output_tokens": 129,
    "ratio": 0.461,
    "method": "lingua",
    "cost_usd": 0.0
  },
  "compressed_message_count": 2
}
```

`GET /health` returns `{"status":"ok","mode":"on"}`.

## What it compresses (and what it leaves alone)

The service is deliberately conservative about what it rewrites:

- **Eligible:** `system` / `user` / `assistant` messages whose `content` is a plain string.
- **Forwarded verbatim:** `tool` messages (tool results often carry structured payloads a lossy prose pass must not touch), any message with a multimodal `content` **list**, and `null` content.

This mirrors the message-shape contract an OpenAI-format proxy expects (`role` ∈ {system, user, assistant, tool}), so the service drops cleanly behind a fronting router/gateway. Message count is preserved one-in-one-out, so a caller can splice results back by index; if that invariant is ever violated the service returns the originals unchanged.

## Configuration

Defaults are chosen to be safe behind another compression layer:

| Env var | Default | Notes |
|---|---|---|
| `LEANCTX_SERVER_MODE` | `on` | `off` = passthrough smoke test (no model load) |
| `LEANCTX_SERVER_THRESHOLD` | `1500` | Per-message token gate. **Not 0** — compressing very short turns risks dropping load-bearing instruction tokens. |
| `LEANCTX_SERVER_ROUTING` | `{"prose":"lingua"}` | JSON. Code / errors route to Verbatim (passthrough) by default. |
| `LEANCTX_SERVER_LINGUA_RATIO` | `0.5` | Fraction of tokens to keep. |
| `LEANCTX_SERVER_LINGUA_DEVICE` | auto | `cpu` / `cuda` / `mps`. Auto-detects CUDA > MPS > CPU. |
| `LEANCTX_SERVER_DEDUP` | `off` | Leave off when a fronting layer (a proxy, a router) already deduplicates. |
| `LEANCTX_SERVER_CONFIG` | — | Full JSON `leanctx_config`, wins over the individual vars. |

## Deployment notes

- Build **one** service at startup and reuse it — the model cache lives on the Lingua instance.
- Use `--workers 1` unless you have the GPU/RAM headroom for multiple model copies; the service uses `compress_messages_async` so a single worker won't block the event loop while torch runs.
- On a CPU-only host the LLMLingua-2 pass takes seconds; for production throughput, run the sidecar with a GPU (CUDA) or Apple Silicon (MPS).

## Why a sidecar

leanctx is a Python library wrapping a PyTorch model. Rather than port the model to every runtime, the sidecar lets any stack call leanctx over localhost HTTP. This is the integration pattern for fronting proxies and gateways (e.g. an LLM router that wants an ML prose-compression pass on large requests): the proxy POSTs the message array to the sidecar, gets back compressed messages, and degrades gracefully to the uncompressed body if the sidecar is unreachable.
