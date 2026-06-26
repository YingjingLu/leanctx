# ClawRouter × leanctx — Phase 1 Benchmark Report

**Verdict: ✅ PASS**  ·  Date: 2026-06-13 22:18 UTC  ·  LB N=503  ·  Eval: anthropic/claude-haiku-4-5-20251001  ·  CR commit: `89269507`

---

## 1. Go / No-Go Gate

| Condition | Threshold | Actual | Result |
|-----------|-----------|--------|--------|
| Δ tokens — extra savings from Layer 8 | ≥ 20% | 24.1% | ✅ PASS |
| Δ accuracy — LongBench quality | ≥ −2% | -1.8% | ✅ PASS |

## 2. Token Compression

| Stage | Avg tokens / request | vs raw | vs Leg A |
|-------|---------------------|--------|----------|
| Raw (uncompressed) | 27,810 | — | — |
| **Leg A** — CR 7 layers | 26,345 | −5.3% | — |
| **Leg B** — CR + leanctx Layer 8 | 19,983 | −28.1% | −24.1% |

## 2b. Compression Excluding Verbatim Content

_Layer 8 routes code, errors, and structured content to **Verbatim (0%)**. This view reports compression on only the content actually sent to the compressor — a figure invariant to how much of the sample is verbatim-routed (see `oversampling_analysis.html`)._

**Verbatim share:** 54.2% of Layer-8 input tokens were preserved verbatim. Routing mix over 503 items: 228 compressed · 275 verbatim · 0 mixed.

| View | Avg input / req | Avg output / req | Ratio | Savings | Δ tokens / req |
|------|-----------------|------------------|-------|---------|----------------|
| Overall (incl. verbatim) | 26,397 | 20,022 | 0.759 | 24.1% | 6,374 |
| **Non-verbatim only** | 12,079 | 5,705 | 0.472 | 52.8% | 6,374 |

_Decomposition: overall 24.1% ≈ (1 − 54.2%) × non-verbatim 52.8%. The absolute token delta is identical in both views — verbatim content contributes 0._

## 3. LongBench v2 Accuracy

| | N | Leg A (CR only) | Leg B (CR + leanctx) | Δ |
|---|---|---|---|---|
| **Overall** | 503 | 45.3% | 43.5% | -1.8% |

**Closed-book control** (no document — answers from priors only): 34.8%. Context lift: Leg A 10.5%, Leg B 8.7%.

### By leanctx route

_`verbatim` = leanctx passed the context through unchanged; `lingua` = leanctx actually compressed it. With the shared eval draw, identical-input (verbatim) items score identically on both legs, so any non-zero Δ here lives entirely in `lingua`._

| route | N | Leg A | Leg B | Δ |
|---|---|---|---|---|
| verbatim | 275 | 46.9% | 46.9% | 0.0% |
| lingua | 228 | 43.4% | 39.5% | -3.9% |
| **overall** | 503 | 45.3% | 43.5% | -1.8% |

### By difficulty

| | route | N | Leg A | Leg B | Δ |
|---|---|---|---|---|---|
| easy | verbatim | 94 | 50.0% | 50.0% | 0.0% |
| easy | lingua | 98 | 48.0% | 42.9% | -5.1% |
| easy | **overall** | 192 | 49.0% | 46.4% | -2.6% |
| hard | verbatim | 181 | 45.3% | 45.3% | 0.0% |
| hard | lingua | 130 | 40.0% | 36.9% | -3.1% |
| hard | **overall** | 311 | 43.1% | 41.8% | -1.3% |

### By length

| | route | N | Leg A | Leg B | Δ |
|---|---|---|---|---|---|
| long | verbatim | 67 | 34.3% | 34.3% | 0.0% |
| long | lingua | 41 | 51.2% | 46.3% | -4.9% |
| long | **overall** | 108 | 40.7% | 38.9% | -1.9% |
| medium | verbatim | 96 | 45.8% | 45.8% | 0.0% |
| medium | lingua | 119 | 37.8% | 42.0% | 4.2% |
| medium | **overall** | 215 | 41.4% | 43.7% | 2.3% |
| short | verbatim | 112 | 55.4% | 55.4% | 0.0% |
| short | lingua | 68 | 48.5% | 30.9% | -17.6% |
| short | **overall** | 180 | 52.8% | 46.1% | -6.7% |

### By domain

| | route | N | Leg A | Leg B | Δ |
|---|---|---|---|---|---|
| Code Repository Understanding | verbatim | 50 | 54.0% | 54.0% | 0.0% |
| Code Repository Understanding | **overall** | 50 | 54.0% | 54.0% | 0.0% |
| Long In-context Learning | verbatim | 27 | 44.4% | 44.4% | 0.0% |
| Long In-context Learning | lingua | 54 | 48.1% | 46.3% | -1.9% |
| Long In-context Learning | **overall** | 81 | 46.9% | 45.7% | -1.2% |
| Long Structured Data Understanding | verbatim | 1 | 0.0% | 0.0% | 0.0% |
| Long Structured Data Understanding | lingua | 32 | 31.2% | 40.6% | 9.4% |
| Long Structured Data Understanding | **overall** | 33 | 30.3% | 39.4% | 9.1% |
| Long-dialogue History Understanding | verbatim | 3 | 33.3% | 33.3% | 0.0% |
| Long-dialogue History Understanding | lingua | 36 | 44.4% | 36.1% | -8.3% |
| Long-dialogue History Understanding | **overall** | 39 | 43.6% | 35.9% | -7.7% |
| Multi-Document QA | verbatim | 97 | 41.2% | 41.2% | 0.0% |
| Multi-Document QA | lingua | 28 | 42.9% | 46.4% | 3.6% |
| Multi-Document QA | **overall** | 125 | 41.6% | 42.4% | 0.8% |
| Single-Document QA | verbatim | 97 | 50.5% | 50.5% | 0.0% |
| Single-Document QA | lingua | 78 | 44.9% | 33.3% | -11.5% |
| Single-Document QA | **overall** | 175 | 48.0% | 42.9% | -5.1% |

## 4. ClawRouter Layer Contributions (Leg A avg)

| Layer | Statistic | Avg value |
|-------|-----------|-----------|
| L1 Dedup | duplicatesRemoved | 0.0 |
| L2 Whitespace | whitespaceSavedChars | 4,376.3 |
| L3 Dictionary | dictionarySubstitutions | 42.7 |
| L4 Paths | pathsShortened | 0.7 |
| L5 JSON | jsonCompactedChars | 0.0 |
| L6 Observation | observationsCompressed | 0.0 |
| L7 Codebook | dynamicSubstitutions | 105.7 |
| **L8 leanctx** | Δ tokens / item | **6,362** |

## 5. Latency

_Sidecar = the Layer 8 compression call in isolation (Leg B). Eval LLM time is shown separately and is **not** part of the sidecar figure._

| Metric | P50 | P95 |
|--------|-----|-----|
| Sidecar (Layer 8 compression) | 47 ms | 959 ms |
| Eval LLM (LongBench answer) | 0 ms | 5,840 ms |

## 6. Cost Analysis

Assuming Claude Sonnet input pricing ($15 / 1M tokens):

```
Δ tokens      = 24.1% of avg 27,810 raw tokens
Savings / req = 6,716 tokens
Cost saved    = 0.1007 USD / request
              = $100.73 per 1 000 requests
```

---

_Generated by `bench_phase1.py` · CR commit `89269507`_