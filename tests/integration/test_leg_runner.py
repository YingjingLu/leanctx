"""Sprint 7 — Leg runner integration tests.

Tests marked integration. Require Node >=20 + built ClawRouter dist/ + leanctx sidecar.
No eval LLM is used (agent workload only, lb_cfg=None).

The core hypothesis test (test 3) requires llmlingua to show tokens_B < tokens_A;
it is skipped automatically when llmlingua is not installed.
"""
from __future__ import annotations

import pytest

from leanctx.bench.workloads import load_workload
from scripts.bench_clawtypeor_phase1 import run_leg


def _make_items(workload_name: str) -> list[dict]:
    """Wrap raw messages into the item dict expected by run_leg."""
    messages = load_workload(workload_name)
    return [{"messages": messages, "workload": workload_name}]


@pytest.mark.integration
def test_run_leg_returns_one_record_per_item(built_cr_shim_no_sidecar):
    shim_url = built_cr_shim_no_sidecar
    items = _make_items("agent")
    records = run_leg("A", items, shim_url, lb_cfg=None)

    assert len(records) == 1
    assert records[0]["leg"] == "A"


@pytest.mark.integration
def test_run_leg_records_positive_token_counts(built_cr_shim_no_sidecar):
    shim_url = built_cr_shim_no_sidecar
    items = _make_items("agent")
    records = run_leg("A", items, shim_url, lb_cfg=None)

    for rec in records:
        assert rec["tokens_raw"] > 0, "tokens_raw must be positive"
        assert rec["tokens_compressed"] > 0, "tokens_compressed must be positive"


@pytest.mark.integration
def test_leg_b_tokens_less_than_leg_a(built_cr_shim_no_sidecar, built_cr_shim_with_sidecar):
    """Core Phase 1 hypothesis: CR + leanctx (Leg B) compresses further than CR alone (Leg A)."""
    shim_url_b, lingua_ok = built_cr_shim_with_sidecar
    if not lingua_ok:
        pytest.skip(
            "llmlingua not installed — install 'leanctx[lingua]' to run the "
            "Leg A vs Leg B token comparison"
        )

    items = _make_items("agent_extended")

    records_a = run_leg("A", items, built_cr_shim_no_sidecar, lb_cfg=None)
    records_b = run_leg("B", items, shim_url_b, lb_cfg=None)

    sum_a = sum(r["tokens_compressed"] for r in records_a)
    sum_b = sum(r["tokens_compressed"] for r in records_b)

    assert sum_b < sum_a, (
        f"tokens_B ({sum_b}) should be < tokens_A ({sum_a}): "
        "leanctx Layer 8 should reduce tokens beyond CR alone"
    )
