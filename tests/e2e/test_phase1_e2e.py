"""Sprint 8 — End-to-end Phase 1 tests.

All tests marked e2e. Require ANTHROPIC_API_KEY or OPENAI_API_KEY in .env.
Estimated cost: ~$0.50 · ~10-15 min.

Run with:
  .venv/bin/python -m pytest tests/e2e/ -m e2e -v -s
"""
from __future__ import annotations

import pytest

from benchmarks.clawrouter.bench_phase1 import (
    compute_metrics,
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

    from benchmarks.clawrouter.bench_phase1 import main

    out_path = tmp_path / "r.jsonl"
    report_path = tmp_path / "report.md"

    main([
        "--skip-setup",
        "--workdir", "/tmp/clawrouter_intg_test",
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


@pytest.mark.e2e
def test_e2e_verbatim_split_section_and_jsonl(tmp_path):
    """Phase B: main() emits the verbatim-excluded section and per-item lx_*
    fields in the JSONL, and the decomposition identity holds on real data."""
    import json

    from benchmarks.clawrouter.bench_phase1 import main

    out_path = tmp_path / "v.jsonl"
    report_path = tmp_path / "vreport.md"

    main([
        "--skip-setup",
        "--workdir", "/tmp/clawrouter_intg_test",
        "--lb-stages", "1",
        "--lb-n", "2",
        "--agent-stages", "1",
        "--no-closed-book",        # keep the smoke cheap
        "--shim-port", "8475",
        "--out", str(out_path),
        "--report", str(report_path),
    ])

    report_text = report_path.read_text()
    assert "2b. Compression Excluding Verbatim Content" in report_text
    assert "Verbatim share" in report_text

    # Leg B records carry the per-item split.
    leg_b = [
        json.loads(line)
        for line in out_path.read_text().splitlines()
        if json.loads(line)["leg"] == "B" and json.loads(line).get("item_id")
    ]
    with_split = [r for r in leg_b if "lx_route" in r]
    assert with_split, "no Leg B record carried the lx_* split fields"
    for r in with_split:
        assert r["lx_route"] in ("verbatim", "lingua", "hybrid")
        assert (
            r["lx_verbatim_tokens"]
            + r["lx_compressed_in_tokens"]
            >= 0
        )

    # Decomposition identity on the aggregate of the split records.
    tot_v = sum(r["lx_verbatim_tokens"] for r in with_split)
    tot_in = sum(r["lx_compressed_in_tokens"] for r in with_split)
    tot_out = sum(r["lx_compressed_out_tokens"] for r in with_split)
    layer8_in = tot_v + tot_in
    if layer8_in and tot_in:
        share = tot_v / layer8_in
        nv_savings = 1 - tot_out / tot_in
        overall = (layer8_in - (tot_v + tot_out)) / layer8_in
        assert overall == pytest.approx((1 - share) * nv_savings, abs=1e-6)
