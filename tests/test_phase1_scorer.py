"""Sprint 4 — compute_metrics / apply_gate / format_report unit tests.

Tests use hardcoded record dicts — no real compression, no I/O.
All marked unit.
"""
from __future__ import annotations

import pytest

from benchmarks.clawrouter.bench_phase1 import (
    GateResult,
    acc_by_route,
    apply_gate,
    compute_metrics,
    format_report,
)

# ── Shared fixtures ────────────────────────────────────────────────────────

RECORDS_A = [
    {"leg": "A", "workload": "lb_s1", "tokens_raw": 10000,
     "tokens_compressed": 6000, "accuracy": True},
    {"leg": "A", "workload": "lb_s1", "tokens_raw": 8000,
     "tokens_compressed": 5000, "accuracy": False},
]
RECORDS_B = [
    {"leg": "B", "workload": "lb_s1", "tokens_raw": 10000,
     "tokens_compressed": 4500, "accuracy": True},
    {"leg": "B", "workload": "lb_s1", "tokens_raw": 8000,
     "tokens_compressed": 3500, "accuracy": False},
]

# ── compute_metrics tests ──────────────────────────────────────────────────


@pytest.mark.unit
def test_compute_delta_tokens_correct():
    # A compressed total: 6000+5000=11000, B total: 4500+3500=8000
    metrics = compute_metrics(RECORDS_A, RECORDS_B)
    assert metrics["delta_tokens"] == pytest.approx((11000 - 8000) / 11000, rel=1e-3)


@pytest.mark.unit
def test_compute_accuracy_delta_correct():
    # A accuracy: 1/2=0.5, B accuracy: 1/2=0.5 → delta=0.0
    metrics = compute_metrics(RECORDS_A, RECORDS_B)
    assert metrics["delta_accuracy"] == pytest.approx(0.0)


@pytest.mark.unit
def test_compute_cr_savings_correct():
    # avg raw = (10000+8000)/2 = 9000, avg compressed A = 11000/2 = 5500
    metrics = compute_metrics(RECORDS_A, RECORDS_B)
    assert metrics["cr_savings"] == pytest.approx(3500 / 9000, rel=1e-3)


# ── apply_gate tests ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_gate_passes_when_both_conditions_met():
    metrics = {"delta_tokens": 0.25, "delta_accuracy": 0.01}
    result = apply_gate(metrics, savings_threshold=0.20, accuracy_drop=0.02)
    assert result.passed is True


@pytest.mark.unit
def test_gate_fails_when_savings_below_threshold():
    metrics = {"delta_tokens": 0.10, "delta_accuracy": 0.01}
    result = apply_gate(metrics, savings_threshold=0.20, accuracy_drop=0.02)
    assert result.passed is False
    assert "savings" in result.fail_reason.lower()


@pytest.mark.unit
def test_gate_fails_when_accuracy_drops_too_much():
    metrics = {"delta_tokens": 0.25, "delta_accuracy": -0.05}
    result = apply_gate(metrics, savings_threshold=0.20, accuracy_drop=0.02)
    assert result.passed is False
    assert "accuracy" in result.fail_reason.lower()


@pytest.mark.unit
def test_gate_passes_with_accuracy_gain():
    metrics = {"delta_tokens": 0.25, "delta_accuracy": 0.03}
    result = apply_gate(metrics, savings_threshold=0.20, accuracy_drop=0.02)
    assert result.passed is True


# ── format_report tests ────────────────────────────────────────────────────


@pytest.mark.unit
def test_format_report_contains_all_six_metrics():
    metrics = {
        "delta_tokens": 0.25,
        "delta_accuracy": 0.01,
        "e2e_ratio": 0.50,
        "cr_savings": 0.38,
        "cost_saved_per_1k": 3.37,
        "sidecar_p50_ms": 120.0,
    }
    gate = GateResult(passed=True)
    report = format_report(metrics, gate)

    for key in [
        "delta_tokens",
        "delta_accuracy",
        "e2e_ratio",
        "cr_savings",
        "cost_saved_per_1k",
        "sidecar_p50_ms",
    ]:
        assert key in report, f"metric key {key!r} not found in report"

    assert "PASS" in report or "NO-GO" in report


# ── acc_by_route tests ──────────────────────────────────────────────────────


def _route_records():
    """4 LB items: 2 verbatim, 2 lingua. Leg B carries lx_route + accuracy."""
    meta = {"workload": "lb_s1", "tokens_raw": 100, "tokens_compressed": 100,
            "lb_difficulty": "hard", "lb_length": "long", "lb_domain": "x"}
    records_a = [
        {"item_id": "v1", "accuracy": True, **meta},
        {"item_id": "v2", "accuracy": False, **meta},
        {"item_id": "l1", "accuracy": True, **meta},
        {"item_id": "l2", "accuracy": False, **meta},
    ]
    records_b = [
        {"item_id": "v1", "accuracy": True, "lx_route": "verbatim", **meta},   # == A
        {"item_id": "v2", "accuracy": False, "lx_route": "verbatim", **meta},  # == A
        {"item_id": "l1", "accuracy": False, "lx_route": "lingua", **meta},    # B regressed
        {"item_id": "l2", "accuracy": True, "lx_route": "lingua", **meta},     # B improved
    ]
    return records_a, records_b


@pytest.mark.unit
def test_acc_by_route_splits_verbatim_lingua_overall():
    records_a, records_b = _route_records()
    rows = {label: (n, a, b) for label, n, a, b in acc_by_route(records_a, records_b)}

    # verbatim: shared draw → identical accuracy both legs, Δ pinned to 0
    assert rows["verbatim"] == (2, 0.5, 0.5)
    # lingua: 1 regress + 1 improve → 50% both legs here, but A and B differ per item
    assert rows["lingua"][0] == 2
    # overall spans all 4 items
    assert rows["overall"][0] == 4
    assert rows["overall"][1] == pytest.approx(0.5)  # acc_a: v1,l1 correct
    assert rows["overall"][2] == pytest.approx(0.5)  # acc_b: v1,l2 correct


@pytest.mark.unit
def test_acc_by_route_skips_items_without_route_or_accuracy():
    records_a = [{"item_id": "a", "accuracy": True}, {"item_id": "b", "accuracy": True}]
    records_b = [
        {"item_id": "a", "accuracy": True, "lx_route": "lingua"},
        {"item_id": "b", "accuracy": True},  # no route → skipped
    ]
    rows = dict((label, n) for label, n, _a, _b in acc_by_route(records_a, records_b))
    assert rows.get("lingua") == 1
    assert rows["overall"] == 1


@pytest.mark.unit
def test_acc_by_route_empty_when_no_routes():
    assert acc_by_route([{"item_id": "x", "accuracy": True}], [{"item_id": "x"}]) == []


@pytest.mark.unit
def test_format_report_includes_route_accuracy_section():
    records_a, records_b = _route_records()
    metrics = compute_metrics(records_a, records_b)
    report = format_report(
        metrics, GateResult(passed=True),
        records_a=records_a, records_b=records_b,
    )
    assert "By leanctx route" in report
    assert "verbatim" in report
    assert "lingua" in report
