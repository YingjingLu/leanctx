"""Behavioral unit tests for the Phase 1 harness internals the reviewer
flagged as untested: answer extraction, token summing, the gold-matching /
scoring pipeline (via run_leg with the network mocked out), latency
percentiles, the closed-book control, and the LongBench sampler.

All marked unit — no real network, no real model, no real dataset.
"""
from __future__ import annotations

import pytest

import benchmarks.clawrouter.bench_phase1 as harness
from benchmarks.clawrouter.bench_phase1 import (
    _extract_lb_answer,
    _load_lb_items,
    _percentile,
    _sum_tokens,
    compute_metrics,
    run_leg,
)

# ── _extract_lb_answer ──────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "text,expected",
    [
        ("The correct answer is (B).", "B"),
        ("blah blah The correct answer is (D) because…", "D"),
        ("The correct answer is C", "C"),  # bare, no parens
        ("The correct answer is **(A)**", "A"),  # markdown bold stripped
        ("I think it is probably A.", None),  # no template phrase
        ("The correct answer is (E)", None),  # out of A–D range
        ("", None),
    ],
)
def test_extract_lb_answer(text, expected):
    assert _extract_lb_answer(text) == expected


# ── _sum_tokens ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_sum_tokens_counts_string_content():
    msgs = [{"role": "user", "content": "hello world"}]
    assert _sum_tokens(msgs) > 0


@pytest.mark.unit
def test_sum_tokens_ignores_non_string_content():
    # structured blocks (list content) contribute nothing — only plain strings
    only_blocks = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
    assert _sum_tokens(only_blocks) == 0


@pytest.mark.unit
def test_sum_tokens_scales_with_length():
    short = _sum_tokens([{"role": "user", "content": "word"}])
    long = _sum_tokens([{"role": "user", "content": "word " * 200}])
    assert long > short


# ── run_leg: gold matching & scoring pipeline ───────────────────────────────


@pytest.fixture
def lb_item():
    return {
        "messages": [{"role": "user", "content": "a long document " * 100}],
        "workload": "lb_s1",
        "item_id": "lb_0001",
        "question": "What is the answer?",
        "choice_A": "alpha",
        "choice_B": "beta",
        "choice_C": "gamma",
        "choice_D": "delta",
        "gold": "B",
        "lb_domain": "single",
        "lb_difficulty": "hard",
        "lb_length": "long",
    }


@pytest.mark.unit
def test_run_leg_scores_correct_answer(monkeypatch, lb_item):
    monkeypatch.setattr(
        harness, "compress_via_shim",
        lambda messages, url: {"messages": messages, "stats": {}},
    )
    # model returns the gold letter → accuracy True
    monkeypatch.setattr(
        harness, "call_eval_llm",
        lambda compressed, item, cfg, *, closed_book=False: ("B", 1234),
    )
    [rec] = run_leg("B", [lb_item], "http://x", lb_cfg={"provider": "anthropic"})
    assert rec["accuracy"] is True
    assert rec["lb_gold"] == "B"
    assert rec["lb_pred"] == "B"
    assert rec["eval_input_tokens"] == 1234
    assert rec["compress_ms"] >= 0
    assert "eval_ms" in rec


@pytest.mark.unit
def test_run_leg_scores_wrong_answer(monkeypatch, lb_item):
    monkeypatch.setattr(
        harness, "compress_via_shim",
        lambda messages, url: {"messages": messages, "stats": {}},
    )
    monkeypatch.setattr(
        harness, "call_eval_llm",
        lambda compressed, item, cfg, *, closed_book=False: ("C", 10),
    )
    [rec] = run_leg("B", [lb_item], "http://x", lb_cfg={"provider": "anthropic"})
    assert rec["accuracy"] is False
    assert rec["lb_pred"] == "C"


@pytest.mark.unit
def test_run_leg_unparseable_answer_is_incorrect(monkeypatch, lb_item):
    monkeypatch.setattr(
        harness, "compress_via_shim",
        lambda messages, url: {"messages": messages, "stats": {}},
    )
    monkeypatch.setattr(
        harness, "call_eval_llm",
        lambda compressed, item, cfg, *, closed_book=False: (None, 10),
    )
    [rec] = run_leg("B", [lb_item], "http://x", lb_cfg={"provider": "anthropic"})
    assert rec["accuracy"] is False  # None != gold
    assert rec["lb_pred"] is None


@pytest.mark.unit
def test_run_leg_closed_book_skips_shim(monkeypatch, lb_item):
    """Closed-book leg must not touch the shim (it runs after shims die)."""
    def boom(*a, **k):  # pragma: no cover - asserts it's never called
        raise AssertionError("compress_via_shim must not be called closed-book")

    monkeypatch.setattr(harness, "compress_via_shim", boom)

    seen = {}

    def fake_eval(compressed, item, cfg, *, closed_book=False):
        seen["closed_book"] = closed_book
        return ("B", 5)

    monkeypatch.setattr(harness, "call_eval_llm", fake_eval)
    [rec] = run_leg(
        "C", [lb_item], shim_url="", lb_cfg={"provider": "anthropic"},
        closed_book=True,
    )
    assert seen["closed_book"] is True
    assert rec["closed_book"] is True
    assert rec["compress_ms"] == 0


# ── _percentile ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_percentile_empty_is_none():
    assert _percentile([], 0.5) is None


@pytest.mark.unit
def test_percentile_single_value():
    assert _percentile([42.0], 0.95) == 42.0


@pytest.mark.unit
def test_percentile_median_and_p95():
    data = [float(i) for i in range(1, 101)]  # 1..100
    assert _percentile(data, 0.50) == pytest.approx(50.0, abs=1.0)
    assert _percentile(data, 0.95) == pytest.approx(95.0, abs=1.0)


# ── compute_metrics: latency isolation + closed-book ────────────────────────


@pytest.mark.unit
def test_compute_metrics_sidecar_latency_from_compress_ms():
    """Sidecar latency must come from compress_ms, NOT end-to-end duration_ms."""
    records_a = [{"tokens_raw": 100, "tokens_compressed": 80, "accuracy": True}]
    records_b = [
        {"tokens_raw": 100, "tokens_compressed": 50, "accuracy": True,
         "compress_ms": 300, "eval_ms": 9000, "duration_ms": 9300},
        {"tokens_raw": 100, "tokens_compressed": 50, "accuracy": True,
         "compress_ms": 400, "eval_ms": 12000, "duration_ms": 12400},
    ]
    metrics = compute_metrics(records_a, records_b)
    # p50 is in the 300–400 ms range, nowhere near the 9–12 s end-to-end times
    assert metrics["sidecar_p50_ms"] in (300, 400)
    assert metrics["sidecar_p50_ms"] < 1000
    assert metrics["eval_p50_ms"] >= 9000


@pytest.mark.unit
def test_compute_metrics_closed_book_accuracy_and_none():
    records_a = [{"tokens_raw": 100, "tokens_compressed": 80, "accuracy": True}]
    records_b = [{"tokens_raw": 100, "tokens_compressed": 50, "accuracy": True}]
    # no closed-book records → None
    assert compute_metrics(records_a, records_b).get("acc_closed_book") is None
    # with closed-book records → averaged
    records_cb = [
        {"accuracy": True}, {"accuracy": False}, {"accuracy": False}, {"accuracy": False},
    ]
    m = compute_metrics(records_a, records_b, records_cb)
    assert m["acc_closed_book"] == pytest.approx(0.25)


# ── _load_lb_items: random, reproducible, oversampled ───────────────────────


def _fake_dataset(monkeypatch):
    """Patch datasets.load_dataset to return a synthetic LongBench-ish pool.

    The real ``datasets`` package is never exercised — it is fully mocked — and
    it is not a ``[dev]`` dependency, so CI installs without it. When it is
    absent we inject a lightweight stub module so ``_load_lb_items``'s
    ``from datasets import load_dataset`` still resolves.
    """
    import sys
    import types

    items = []
    for length in ("short", "medium", "long"):
        for diff in ("easy", "hard"):
            for i in range(20):
                items.append({
                    "_id": f"{length}_{diff}_{i}",
                    "context": f"ctx {length} {diff} {i} " * 5,
                    "question": "q?",
                    "choice_A": "a", "choice_B": "b",
                    "choice_C": "c", "choice_D": "d",
                    "answer": "A",
                    "domain": "d", "difficulty": diff, "length": length,
                })

    try:
        import datasets
    except ModuleNotFoundError:
        datasets = types.ModuleType("datasets")
        monkeypatch.setitem(sys.modules, "datasets", datasets)

    monkeypatch.setattr(datasets, "load_dataset", lambda *a, **k: items, raising=False)


@pytest.mark.unit
def test_load_lb_items_is_reproducible(monkeypatch):
    _fake_dataset(monkeypatch)
    a = _load_lb_items(limit=12, seed=7)
    _fake_dataset(monkeypatch)
    b = _load_lb_items(limit=12, seed=7)
    assert [x["item_id"] for x in a] == [x["item_id"] for x in b]
    assert len(a) == 12


@pytest.mark.unit
def test_load_lb_items_different_seed_differs(monkeypatch):
    _fake_dataset(monkeypatch)
    a = _load_lb_items(limit=12, seed=1)
    _fake_dataset(monkeypatch)
    b = _load_lb_items(limit=12, seed=2)
    assert [x["item_id"] for x in a] != [x["item_id"] for x in b]


@pytest.mark.unit
def test_load_lb_items_is_not_first_n(monkeypatch):
    """Regression: sampling must not just take the first-N items per cell."""
    _fake_dataset(monkeypatch)
    sample = _load_lb_items(limit=18, seed=99)
    # the deterministic first-N bug would pick only "_0", "_1", … suffixes
    suffixes = {x["item_id"].rsplit("_", 1)[1] for x in sample}
    assert suffixes != {str(i) for i in range(len(sample))}
    assert any(int(s) >= 5 for s in suffixes)  # reaches deeper into the pool


@pytest.mark.unit
def test_load_lb_items_oversamples_long(monkeypatch):
    _fake_dataset(monkeypatch)
    sample = _load_lb_items(limit=30, seed=3, oversample_long=True)
    lengths = [x["lb_length"] for x in sample]
    n_long = lengths.count("long")
    n_short = lengths.count("short")
    # long is double-weighted, so it should out-represent short
    assert n_long > n_short
