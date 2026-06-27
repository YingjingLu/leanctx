"""Scoring + go/no-go gate for the benchmark harness (extracted from bench_phase1).

Provider-agnostic: ``compute_metrics`` aggregates per-item records (token counts
+ accuracy) and ``apply_gate`` applies the savings/accuracy thresholds.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class GateResult:
    passed: bool
    fail_reason: str = ""


def _percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in [0, 1]); None for an empty sample."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    idx = min(int(round(q * (len(s) - 1))), len(s) - 1)
    return float(s[idx])


def compute_metrics(
    records_a: list[dict[str, Any]],
    records_b: list[dict[str, Any]],
    records_cb: list[dict[str, Any]] | None = None,
    *,
    token_field: str = "tokens_compressed",
    input_price_per_token: float = 15 / 1e6,
) -> dict[str, Any]:
    """Aggregate per-item leg records into the headline metrics.

    ``token_field`` selects which per-record count drives the savings figure —
    ``"tokens_compressed"`` (tiktoken, the ClawRouter default) or, for InsForge,
    ``"usage_prompt_tokens"`` (the provider's own ``usage.prompt_tokens``).
    ``input_price_per_token`` sets the cost model (default $15/1M = Sonnet; pass
    the model's OpenRouter price for InsForge).

    An empty aligned pool (e.g. a transcript-only run, since the transcript is
    excluded from the metric pool, or every item dropped) yields a zeroed metric
    dict rather than a ``ZeroDivisionError`` that would discard a finished run.
    """
    if not records_a or not records_b:
        return {
            "delta_tokens": 0.0,
            "delta_accuracy": 0.0,
            "e2e_ratio": 0.0,
            "cr_savings": 0.0,
            "cost_saved_per_1k": 0.0,
            "sidecar_p50_ms": None,
            "sidecar_p95_ms": None,
            "eval_p50_ms": None,
            "eval_p95_ms": None,
            "tokens_a": 0,
            "tokens_b": 0,
            "acc_a": 0.0,
            "acc_b": 0.0,
            "acc_closed_book": None,
        }

    tokens_a = sum(r[token_field] for r in records_a)
    tokens_b = sum(r[token_field] for r in records_b)

    avg_raw = sum(r["tokens_raw"] for r in records_a) / len(records_a)
    avg_a = tokens_a / len(records_a)
    avg_b = tokens_b / len(records_b)

    delta_tokens = (tokens_a - tokens_b) / tokens_a if tokens_a > 0 else 0.0
    cr_savings = (avg_raw - avg_a) / avg_raw if avg_raw > 0 else 0.0
    e2e_ratio = avg_b / avg_raw if avg_raw > 0 else 0.0

    acc_a_vals = [r["accuracy"] for r in records_a if r.get("accuracy") is not None]
    acc_b_vals = [r["accuracy"] for r in records_b if r.get("accuracy") is not None]
    acc_a = sum(acc_a_vals) / len(acc_a_vals) if acc_a_vals else 0.0
    acc_b = sum(acc_b_vals) / len(acc_b_vals) if acc_b_vals else 0.0
    delta_accuracy = acc_b - acc_a

    # Closed-book control: accuracy when the model answers from priors alone.
    # If Leg A/B accuracy is at or below this, the context did no work and the
    # "compression preserved information" claim is unsupported.
    cb_vals = [
        r["accuracy"]
        for r in (records_cb or [])
        if r.get("accuracy") is not None
    ]
    acc_closed_book: float | None = (
        sum(cb_vals) / len(cb_vals) if cb_vals else None
    )

    # Cost saved over 1K conversations at the configured input price.
    cost_saved_per_1k = delta_tokens * avg_raw * input_price_per_token * 1000

    # Sidecar (Layer 8) latency is the compression call in isolation, taken
    # from Leg B records (the only leg with the sidecar in the path). The eval
    # LLM time is reported separately and never folded into this figure.
    compress_ms_b = [
        r["compress_ms"] for r in records_b if r.get("compress_ms") is not None
    ]
    # Eval-LLM latency is sourced from Leg A, not Leg B: under the shared eval
    # draw, verbatim Leg-B items reuse Leg A's answer and record eval_ms=0, so a
    # Leg-B P50 would read ~0ms and misrepresent the judge as "free". Leg A
    # always issues a real judge call (no prior to reuse), so its distribution is
    # the honest per-call cost.
    eval_ms_a = [r["eval_ms"] for r in records_a if r.get("eval_ms") is not None]

    return {
        "delta_tokens": delta_tokens,
        "delta_accuracy": delta_accuracy,
        "e2e_ratio": e2e_ratio,
        "cr_savings": cr_savings,
        "cost_saved_per_1k": cost_saved_per_1k,
        "sidecar_p50_ms": _percentile(compress_ms_b, 0.50),
        "sidecar_p95_ms": _percentile(compress_ms_b, 0.95),
        "eval_p50_ms": _percentile(eval_ms_a, 0.50),
        "eval_p95_ms": _percentile(eval_ms_a, 0.95),
        "tokens_a": tokens_a,
        "tokens_b": tokens_b,
        "acc_a": acc_a,
        "acc_b": acc_b,
        "acc_closed_book": acc_closed_book,
    }


def apply_gate(
    metrics: dict[str, Any],
    *,
    savings_threshold: float = 0.20,
    accuracy_drop: float = 0.02,
) -> GateResult:
    if metrics["delta_tokens"] < savings_threshold:
        return GateResult(
            passed=False,
            fail_reason=(
                f"savings delta {metrics['delta_tokens']:.4f} below threshold {savings_threshold}"
            ),
        )
    if metrics["delta_accuracy"] < -accuracy_drop:
        return GateResult(
            passed=False,
            fail_reason=(
                f"accuracy dropped {-metrics['delta_accuracy']:.4f} exceeds allowed {accuracy_drop}"
            ),
        )
    return GateResult(passed=True)
