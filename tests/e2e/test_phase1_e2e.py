"""Sprint 8 — End-to-end Phase 1 tests.

All tests marked e2e. Require ANTHROPIC_API_KEY or OPENAI_API_KEY in .env.
Estimated cost: ~$0.50 · ~10-15 min.

Run with:
  .venv/bin/python -m pytest tests/e2e/ -m e2e -v -s
"""
from __future__ import annotations

import pytest

from scripts.bench_clawtypeor_phase1 import (
    GateResult,
    apply_gate,
    compute_metrics,
    format_report,
)

# ── Sprint 8 tests ─────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_e2e_setup_completes_cleanly(e2e_cr_workdir):
    """CR build artefacts exist and Layer 8 patch was applied."""
    wd = e2e_cr_workdir
    assert (wd / "dist" / "compression" / "index.js").exists(), \
        "dist/compression/index.js not found — npm run build may have failed"
    assert (wd / "cr_shim.mjs").exists(), \
        "cr_shim.mjs not written into workdir"
    ts_src = (wd / "src" / "compression" / "index.ts").read_text()
    assert "LEANCTX_SIDECAR_URL" in ts_src, \
        "Layer 8 patch not applied to src/compression/index.ts"


@pytest.mark.e2e
def test_e2e_leg_a_runs_5_lb_questions(full_pipeline):
    """Leg A returns one record per LB question with accuracy + positive token counts."""
    records_a, _records_b, lb_items = full_pipeline
    assert len(records_a) == len(lb_items), \
        f"Expected {len(lb_items)} records from Leg A, got {len(records_a)}"
    for rec in records_a:
        assert rec["accuracy"] in (True, False), \
            f"accuracy must be True/False, got {rec['accuracy']!r}"
        assert rec["tokens_compressed"] > 0, "tokens_compressed must be positive"


@pytest.mark.e2e
def test_e2e_leg_b_runs_5_lb_questions(full_pipeline):
    """Leg B returns one record per LB question with same invariants as Leg A."""
    _records_a, records_b, lb_items = full_pipeline
    assert len(records_b) == len(lb_items), \
        f"Expected {len(lb_items)} records from Leg B, got {len(records_b)}"
    for rec in records_b:
        assert rec["accuracy"] in (True, False), \
            f"accuracy must be True/False, got {rec['accuracy']!r}"
        assert rec["tokens_compressed"] > 0, "tokens_compressed must be positive"


@pytest.mark.e2e
def test_e2e_leg_b_tokens_lower_than_leg_a(full_pipeline):
    """Leg B (CR + leanctx) compresses to fewer tokens than Leg A (CR alone).

    Skipped when llmlingua is not installed — sidecar runs in passthrough mode,
    so tokens_B == tokens_A.
    """
    try:
        import llmlingua  # noqa: F401
    except ImportError:
        pytest.skip(
            "llmlingua not installed — sidecar in passthrough mode; "
            "install 'leanctx[lingua]' to run the Leg A vs Leg B comparison"
        )

    records_a, records_b, _ = full_pipeline
    sum_a = sum(r["tokens_compressed"] for r in records_a)
    sum_b = sum(r["tokens_compressed"] for r in records_b)
    assert sum_b < sum_a, (
        f"tokens_B ({sum_b}) must be < tokens_A ({sum_a}): "
        "leanctx Layer 8 should reduce tokens beyond CR alone"
    )


@pytest.mark.e2e
def test_e2e_compute_metrics_produces_valid_delta(full_pipeline):
    """compute_metrics() returns a dict with sane delta_tokens and delta_accuracy."""
    records_a, records_b, _ = full_pipeline
    metrics = compute_metrics(records_a, records_b)
    assert 0.0 <= metrics["delta_tokens"] < 1.0, \
        f"delta_tokens out of range: {metrics['delta_tokens']}"
    assert -0.5 < metrics["delta_accuracy"] < 0.5, \
        f"delta_accuracy out of sane range: {metrics['delta_accuracy']}"


@pytest.mark.e2e
def test_e2e_report_written_to_disk(full_pipeline, tmp_path):
    """main() writes results.jsonl + report.md; report contains PASS or NO-GO."""
    import sys

    from scripts.bench_clawtypeor_phase1 import main

    out_path = tmp_path / "r.jsonl"
    report_path = tmp_path / "report.md"

    main([
        "--skip-setup",
        "--workdir", "/tmp/clawtypeor_intg_test",
        "--lb-stages", "1",
        "--lb-n", "3",
        "--agent-stages", "1",
        "--shim-port", "8473",
        "--out", str(out_path),
        "--report", str(report_path),
    ])

    assert out_path.exists(), "JSONL output file was not written"
    assert report_path.exists(), "report.md was not written"
    report_text = report_path.read_text()
    assert "PASS" in report_text or "NO-GO" in report_text, \
        f"report.md does not contain verdict:\n{report_text[:500]}"
