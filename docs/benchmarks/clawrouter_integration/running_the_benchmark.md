# ClawRouter × leanctx — Phase 1 Benchmark: Running Guide

This document covers how to run `benchmarks/clawrouter/bench_phase1.py` — the
go/no-go harness that measures whether leanctx Layer 8 (LLMLingua-2 prose
compression) reduces tokens beyond ClawRouter's 7 structural layers while
preserving LongBench v2 answer accuracy within the allowed threshold.

---

## Prerequisites

All commands assume the project virtualenv is active or that you prefix them
with `.venv/bin/python`.

```bash
# 1. Create the virtualenv (once)
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,lingua,longbench]"

# 2. Fill in .env (copy from template, add your keys)
cp .env.example .env
#   ANTHROPIC_API_KEY=sk-ant-...
#   OPENAI_API_KEY=sk-...           # alternative provider
#   BENCHMARK_EVAL_PROVIDER=anthropic
#   BENCHMARK_EVAL_MODEL=claude-sonnet-4-6

# 3. Clone + build ClawRouter at the pinned commit (once, ~5 min)
.venv/bin/python benchmarks/clawrouter/bench_phase1.py \
  --workdir /tmp/clawrouter_bench
  # Omit --skip-setup so it clones, patches, and builds automatically.
  # On subsequent runs add --skip-setup to reuse the existing build.
```

> **Node ≥ 20 required** for the ClawRouter build step.
> Check with `node --version`.

---

## Quick Reference

| Run type | Time | Cost | LB questions | Command flag |
|----------|------|------|--------------|--------------|
| Smoke test (no API key) | < 1 min | $0 | 0 | `--lb-stages 0` _(see below)_ |
| **Go/no-go (recommended first run)** | ~1 hr | ~$10 | 60 | `--lb-n 60` |
| Full publishable run | ~8 hrs | ~$80 | 503 | `--lb-n 503 --lb-stages 2` |

---

## Go / No-Go Run (60 LB questions, ~1 hr, ~$10)

The standard first run. Produces a verdict and a human-readable Markdown report.

```bash
.venv/bin/python benchmarks/clawrouter/bench_phase1.py \
  --skip-setup \
  --workdir /tmp/clawrouter_bench \
  --agent-stages 1 \
  --lb-stages 1 --lb-n 60 \
  --eval-provider anthropic \
  --eval-model claude-sonnet-4-6 \
  --out phase1_results.jsonl \
  --report phase1_report.md
```

Exit code **0 = PASS**, **1 = NO-GO**.

---

## Full Publishable Run (503 LB questions, ~8 hrs, ~$80)

Run this after the go/no-go PASS to produce numbers comparable to the
public LongBench v2 leaderboard.

```bash
.venv/bin/python benchmarks/clawrouter/bench_phase1.py \
  --skip-setup \
  --workdir /tmp/clawrouter_bench \
  --agent-stages 2 \
  --lb-stages 2 --lb-n 503 \
  --no-fail-fast \
  --eval-provider anthropic \
  --eval-model claude-sonnet-4-6 \
  --out phase1_results_full.jsonl \
  --report phase1_report_full.md
```

---

## All Arguments

### Setup

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--workdir PATH` | path | `/tmp/clawrouter_bench` | Directory where ClawRouter is cloned, built, and the CR shim is written. Reused across runs; safe to point at the same path every time. |
| `--cr-commit SHA` | string | `89269507b2173…` | Pinned ClawRouter git commit. The Layer 8 patch anchor has been verified unique at this commit. Change only if intentionally upgrading CR. |
| `--skip-setup` | flag | off | Skip the clone → `npm ci` → patch → `npm run build` pipeline. Use this on every run after the first successful build to save ~5 min. Fails fast if `dist/compression/index.js` is missing. |
| `--dry-run-patch` | flag | off | Print the Layer 8 TypeScript diff to stdout and exit immediately. No files are written and no processes are spawned. Useful for reviewing the patch before applying it. |

### Workload Stages

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--agent-stages 1\|2` | int | `1` | Agent workload depth. `1` = 9-message smoke fixture (`agent`); `2` = also runs the 50-message extended fixture (`agent_extended`, ~10 769 tokens). Agent items do not call the eval LLM; they contribute to token savings metrics only. |
| `--lb-stages 1\|2` | int | `1` | LongBench v2 depth. `1` = run the sample defined by `--lb-n`; `2` = run both Stage 1 and Stage 2 (full 503-question set). With `--fail-fast` (default), Stage 2 is skipped if Stage 1 clearly fails both gate conditions. |
| `--lb-n N` | int | `5` | Number of LongBench questions to run in Stage 1. Questions are drawn as a **seeded random** sample, stratified across the 6 length × difficulty cells (random *within* each cell — not the first-N of each cell). Recommended values: `5` (quick smoke), `60` (go/no-go), `503` (full set). |
| `--sample-seed N` | int | `1234` | Seed for the random LongBench sampler. Fix it for reproducible item selection across runs; change it to draw a different sample from the same cells. |
| `--no-oversample-long` | flag | off | By default the `long` length category is given **1.5× weight** in the sample, because that is where Layer 8 showed a real accuracy risk and needs more items before any safety claim. Pass this flag to weight all cells equally instead. |
| `--closed-book` / `--no-closed-book` | flag | on | Run (or skip) the **closed-book control leg** (Leg C): the same LB questions answered with *no* document context. Establishes the prior-knowledge baseline so accuracy can be read as "context lift" rather than absolute — rules out the null hypothesis that the context was irrelevant. Needs no shim/sidecar; adds one eval-LLM call per LB item. |

### Fail-fast Behaviour

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--fail-fast` | flag | on | If Stage 1 clearly fails **both** gate conditions, skip Stage 2 entirely. Prevents spending money on a run that is already a NO-GO. |
| `--no-fail-fast` | flag | — | Disable fail-fast; always run all requested stages regardless of Stage 1 results. Required for the full publishable run when you want all 503 questions regardless of intermediate results. |

### Services

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--sidecar-url URL` | string | `http://127.0.0.1:8459` | URL of the leanctx HTTP sidecar. If the sidecar is not reachable at this URL when the harness starts, it spawns `leanctx-serve` automatically and waits up to 120 s for warmup. If it is already running (e.g. from a previous run), it is reused and not stopped at the end. |
| `--shim-port INT` | int | `8461` | Port for the Leg A CR shim (no sidecar). Leg B shim uses `--shim-port + 1` (default: 8462). Change if these ports are in use on your machine. |
| `--lingua-ratio FLOAT` | float | `0.5` | Keep-ratio passed to LLMLingua-2 when the sidecar is auto-started. `0.5` means retain ~50% of tokens in eligible messages. Lower values = more aggressive compression; higher values = more conservative. Passed as `LEANCTX_SERVER_LINGUA_RATIO` to `leanctx-serve`. Has no effect if the sidecar is already running (its ratio was fixed at startup). |
| `--lingua-device DEV` | string | `$LEANCTX_SERVER_LINGUA_DEVICE` or `auto` | Device for Layer 8 Lingua inference: `auto` (let the server detect cuda > mps > cpu), or an explicit `cuda` / `cpu` / `mps`. Passed as `LEANCTX_SERVER_LINGUA_DEVICE` to `leanctx-serve`, which logs the **resolved** device at model load (e.g. `actual_device=cuda:0`) so the device used in a report is provable from logs, not assumed. Has no effect if the sidecar is already running. |

### Evaluation LLM

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--eval-provider anthropic\|openai` | string | `$BENCHMARK_EVAL_PROVIDER` or `anthropic` | Provider for LongBench eval calls. `anthropic` uses the Anthropic Messages API; `openai` uses the OpenAI Chat Completions API. The corresponding API key must be set in `.env` or the environment. |
| `--eval-model MODEL_ID` | string | `$BENCHMARK_EVAL_MODEL` or `claude-sonnet-4-6` | Model ID passed to the eval provider. Must match the provider: e.g. `claude-sonnet-4-6` for Anthropic, `gpt-4o` or `gpt-4o-mini` for OpenAI. Results are only comparable across runs that use the same model. |

> Both `--eval-provider` and `--eval-model` can also be set via environment
> variables `BENCHMARK_EVAL_PROVIDER` and `BENCHMARK_EVAL_MODEL` in `.env`.
> Command-line flags take precedence.

### Go / No-Go Gate Thresholds

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--savings-threshold FLOAT` | float | `0.20` | Minimum required Δ tokens for a PASS on Gate A: `(tokens_A − tokens_B) / tokens_A ≥ threshold`. Default 0.20 = Layer 8 must save at least 20% of the tokens that remain after CR's 7 structural layers. |
| `--accuracy-drop FLOAT` | float | `0.02` | Maximum allowed accuracy regression for Gate B: `acc_B ≥ acc_A − accuracy_drop`. Default 0.02 = LongBench accuracy may drop by at most 2 percentage points. An accuracy gain always passes. If Gate A passes but Gate B fails, the harness automatically retries at `--lingua-ratio 0.65` before declaring NO-GO. |

### Output

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--out PATH` | path | `./phase1_results.jsonl` | Path for per-item JSONL results. One line per workload item per leg (`A` = CR only, `B` = CR + leanctx, `C` = closed-book control), containing `leg`, `workload`, `item_id`, `tokens_raw`, `tokens_compressed`, `cr_compression_ratio`, `cr_stats` (per-layer breakdown), `compress_ms` (isolated sidecar latency), `eval_ms` (eval-LLM latency), `closed_book`, `accuracy`, `lb_gold`, `lb_pred`, `eval_input_tokens`, `duration_ms`. Append-safe across reruns if you use a new path each time. |
| `--report PATH` | path | `./phase1_report.md` | Path for the human-readable Markdown report. Contains the go/no-go verdict, token compression table, LongBench accuracy breakdown by difficulty/length/domain, CR layer contributions, latency stats, and cost analysis. |

---

## Output Files

### `phase1_results.jsonl`

One JSON object per line, one line per (leg, item) pair:

```json
{
  "leg": "A",
  "workload": "lb_s1",
  "item_id": "lb_0042",
  "tokens_raw": 15420,
  "tokens_compressed": 14830,
  "cr_compression_ratio": 0.962,
  "cr_stats": {
    "duplicatesRemoved": 0,
    "whitespaceSavedChars": 2140,
    "dictionarySubstitutions": 18,
    "pathsShortened": 0,
    "jsonCompactedChars": 0,
    "observationsCompressed": 0,
    "observationCharsSaved": 0,
    "dynamicSubstitutions": 52,
    "dynamicCharsSaved": 980
  },
  "accuracy": true,
  "lb_gold": "B",
  "lb_pred": "B",
  "lb_domain": "Single-Document QA",
  "lb_difficulty": "hard",
  "lb_length": "short",
  "eval_input_tokens": 14950,
  "duration_ms": 38420,
  "lx_route": "lingua",
  "lx_verbatim_tokens": 0,
  "lx_compressed_in_tokens": 14830,
  "lx_compressed_out_tokens": 7100
}
```

`accuracy` is `null` for agent workload items (no eval LLM is called).

The `lx_*` fields appear on **Leg B** records only and record the
verbatim-excluded split (Phase B): how the item's Layer-8 input was routed.
`lx_route` is `verbatim` (preserved unchanged), `lingua` (compressed), or
`hybrid` (block-mixed). `lx_verbatim_tokens` + `lx_compressed_in_tokens` is the
Layer-8 input; `lx_compressed_out_tokens` is the compressed size of the
non-verbatim portion. These are computed post-run by replaying leanctx's real
classifier on the Leg-A (CR-only) output, so they require no live sidecar.

### `phase1_report.md`

Structured Markdown with seven sections:

1. **Go / No-Go Gate** — threshold vs actual for both gate conditions
2. **Token Compression** — raw → CR → CR+leanctx table (absolute + %)
2b. **Compression Excluding Verbatim Content** — overall vs non-verbatim-only
   compression, the verbatim share, the routing mix, and the decomposition
   `overall ≈ (1 − verbatim_share) × non_verbatim`. This view is invariant to
   sample composition; the go/no-go gate still keys off the overall savings.
3. **LongBench v2 Accuracy** — overall + breakdowns by difficulty, length, domain
4. **Per-Workload Breakdown** — agent and LB items separated, with token deltas
5. **ClawRouter Layer Contributions** — per-layer stats (L1–L7) plus L8 Δ tokens/item
6. **Latency** — Layer 8 sidecar P50 and P95
7. **Cost Analysis** — tokens saved per request and $ saved per 1 000 requests

---

## Environment Variables (`.env`)

```bash
# Required for LongBench accuracy evaluation
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...              # alternative if Anthropic is not available

# Eval model (can be overridden with CLI flags)
BENCHMARK_EVAL_PROVIDER=anthropic
BENCHMARK_EVAL_MODEL=claude-sonnet-4-6

# Optional: point at a pre-running sidecar (harness auto-starts if absent)
# LEANCTX_SIDECAR_URL=http://127.0.0.1:8459

# Optional: force GPU/device for Lingua
# LEANCTX_SERVER_LINGUA_DEVICE=cuda
# LEANCTX_SERVER_LINGUA_DEVICE=mps
```

---

## Interpreting Results

### Gate A — Token savings

`(tokens_A − tokens_B) / tokens_A ≥ 0.20`

Measures how much additional compression Layer 8 adds on top of ClawRouter's
7 structural layers. `tokens_A` = token count after CR alone; `tokens_B` =
token count after CR + leanctx.

A result of 26% means that, for every 100 tokens that survived CR's structural
compression, leanctx removed a further 26.

### Gate B — Accuracy

`acc_B ≥ acc_A − 0.02`

LongBench v2 is a multiple-choice benchmark (A/B/C/D) over 503 long-context
documents across 6 domains. Accuracy is the fraction of questions answered
correctly. The gate allows up to a 2 percentage-point regression from
compression. An accuracy gain (leanctx improves the signal-to-noise ratio) is
always a pass.

> **Note on absolute accuracy values.** The harness truncates LongBench
> contexts to 60 000 characters (head + tail) before sending to CR. For the
> "long" and "hard" subsets (documents up to 1 M characters), this means the
> eval model is answering from a heavily truncated window. The absolute
> accuracy figures (e.g. 3–5%) will therefore be lower than published
> leaderboard numbers, which use the full document. **The delta between Leg A
> and Leg B is the meaningful metric**, not the absolute values.

### Latency note

The P50/P95 latency values reflect CPU inference (LLMLingua-2 on CPU takes
~36 s per request at `--lingua-ratio 0.5`). On a GPU deployment the same
inference runs in < 1 s. The token savings and accuracy metrics are identical
regardless of hardware; only the latency row changes.

---

## Re-running After a Failure

If a run fails mid-way (e.g. API key exhausted, network error), the output
JSONL will contain partial results. Re-run with a **new `--out` path** to
avoid mixing partial and complete data, then use a new `--report` path:

```bash
.venv/bin/python benchmarks/clawrouter/bench_phase1.py \
  --skip-setup \
  --workdir /tmp/clawrouter_bench \
  --agent-stages 1 --lb-stages 1 --lb-n 60 \
  --out phase1_results_retry.jsonl \
  --report phase1_report_retry.md
```

The ClawRouter build at `--workdir` is preserved between runs; `--skip-setup`
is always safe to use once it has been built successfully.
