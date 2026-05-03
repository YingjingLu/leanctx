"""longbench-v2 runner tests — mocked HF dataset + Anthropic API.

These tests exercise the runner end-to-end without touching the network
or spending API credits:

- Mock ``datasets.load_dataset`` to return a small synthetic LongBench-v2
  shaped list (4 items, deterministic answers).
- Monkeypatch the Anthropic SDK so ``anthropic.Anthropic().messages.create``
  returns a canned response.
- Verify prompt template formatting, answer-extraction regex (both paren
  and bare forms), head+tail truncation, scoring buckets, and the
  per-question JSONL output path.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from leanctx.bench import scenarios
from leanctx.bench.runners import longbench_v2 as lbv2


def _synthetic_dataset() -> list[dict[str, Any]]:
    return [
        {
            "_id": "q1",
            "domain": "Single-Document QA",
            "sub_domain": "Academic",
            "difficulty": "easy",
            "length": "short",
            "question": "What is the answer?",
            "choice_A": "alpha",
            "choice_B": "beta",
            "choice_C": "gamma",
            "choice_D": "delta",
            "answer": "B",
            "context": "the truth is beta. " * 200,
        },
        {
            "_id": "q2",
            "domain": "Multi-Document QA",
            "sub_domain": "News",
            "difficulty": "hard",
            "length": "medium",
            "question": "Which option?",
            "choice_A": "alpha",
            "choice_B": "beta",
            "choice_C": "gamma",
            "choice_D": "delta",
            "answer": "C",
            "context": "the truth is gamma. " * 200,
        },
        {
            "_id": "q3",
            "domain": "Code Repository",
            "sub_domain": "Python",
            "difficulty": "easy",
            "length": "short",
            "question": "Pick one.",
            "choice_A": "alpha",
            "choice_B": "beta",
            "choice_C": "gamma",
            "choice_D": "delta",
            "answer": "A",
            "context": "the truth is alpha. " * 200,
        },
        {
            "_id": "q4",
            "domain": "Long Dialogue",
            "sub_domain": "Chat",
            "difficulty": "hard",
            "length": "long",
            "question": "Which one?",
            "choice_A": "alpha",
            "choice_B": "beta",
            "choice_C": "gamma",
            "choice_D": "delta",
            "answer": "D",
            "context": "the truth is delta. " * 200,
        },
    ]


@pytest.fixture
def mock_dataset(monkeypatch: Any) -> None:
    monkeypatch.setattr(lbv2, "_load_dataset", lambda cfg: _synthetic_dataset())


@pytest.fixture
def mock_anthropic(monkeypatch: Any) -> dict[str, Any]:
    """Mock the Anthropic eval caller. Returns a state dict so tests can
    inspect what prompts were sent. The mock answers 'B' for all calls
    (so on the synthetic dataset, only q1 should score correct)."""
    state: dict[str, Any] = {"prompts_sent": [], "fixed_answer": "B"}

    def _fake_caller(cfg: dict[str, Any]) -> Any:
        def _call(prompt: str) -> tuple[str, int, int]:
            state["prompts_sent"].append(prompt)
            return f"The correct answer is ({state['fixed_answer']}).", 100, 10

        return _call

    monkeypatch.setattr(lbv2, "_anthropic_caller", _fake_caller)
    return state


def _set_env(monkeypatch: Any, **kwargs: str) -> None:
    """Set LEANCTX_LBV2_* env vars; clear any unset to avoid leakage."""
    keys = (
        "COMPRESSOR",
        "LIMIT",
        "PROVIDER",
        "MODEL",
        "RATIO",
        "MAX_LEN",
        "OUT",
        "DOMAIN",
        "DIFFICULTY",
        "LENGTH",
        "COMPRESSOR_MODEL",
        "MAX_RESPONSE_TOKENS",
    )
    for k in keys:
        monkeypatch.delenv(f"LEANCTX_LBV2_{k}", raising=False)
    for k, v in kwargs.items():
        monkeypatch.setenv(f"LEANCTX_LBV2_{k.upper()}", v)


def test_extract_answer_handles_both_regex_forms() -> None:
    assert lbv2._extract_answer("The correct answer is (C).") == "C"
    assert lbv2._extract_answer("The correct answer is C.") == "C"
    # Asterisks (markdown emphasis) are stripped before regex match.
    assert lbv2._extract_answer("The correct answer is (**A**).") == "A"
    assert lbv2._extract_answer("I think it's option B but I'm unsure.") is None


def test_head_tail_truncate_keeps_short_text_unchanged() -> None:
    text = "x" * 100
    assert lbv2._head_tail_truncate(text, max_len=10_000) == text


def test_head_tail_truncate_keeps_first_and_last_halves() -> None:
    text = "A" + ("middle " * 5000) + "Z"
    out = lbv2._head_tail_truncate(text, max_len=50)
    assert out.startswith("A")
    assert out.endswith("Z")
    assert len(out) < len(text)


def test_prompt_template_substitutes_all_placeholders(
    mock_dataset: None, mock_anthropic: dict[str, Any], monkeypatch: Any
) -> None:
    _set_env(monkeypatch, COMPRESSOR="none", LIMIT="1", PROVIDER="anthropic")
    lbv2.run(workload="all")
    sent = mock_anthropic["prompts_sent"][0]
    assert "$DOC$" not in sent
    assert "$Q$" not in sent
    assert "$C_A$" not in sent
    assert "(A) alpha" in sent
    assert "(B) beta" in sent
    assert "(C) gamma" in sent
    assert "(D) delta" in sent
    assert "What is the correct answer to this question:" in sent


def test_scoring_buckets_aggregate_correctly(
    mock_dataset: None, mock_anthropic: dict[str, Any], monkeypatch: Any
) -> None:
    """Mock answers 'B' for everything; on the synthetic 4-item dataset
    only q1 (gold=B) scores correct. Verify accuracy + per-bucket."""
    _set_env(monkeypatch, COMPRESSOR="none", LIMIT="0", PROVIDER="anthropic")
    record = lbv2.run(workload="all")
    assert record.status == "success"
    extra = record.extra
    assert extra["n_questions"] == 4
    assert extra["n_correct"] == 1  # only q1
    assert abs(extra["accuracy"] - 0.25) < 1e-9
    # Bucket: q1 is short/easy
    assert extra["by_length"]["short"]["correct"] == 1
    assert extra["by_difficulty"]["easy"]["correct"] == 1
    assert extra["by_difficulty"]["hard"]["correct"] == 0


def test_jsonl_output_writes_per_question_records(
    mock_dataset: None,
    mock_anthropic: dict[str, Any],
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    out = tmp_path / "lbv2.jsonl"
    _set_env(
        monkeypatch,
        COMPRESSOR="none",
        LIMIT="0",
        PROVIDER="anthropic",
        OUT=str(out),
    )
    lbv2.run(workload="all")
    rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert len(rows) == 4
    assert rows[0]["id"] == "q1"
    assert rows[0]["correct"] is True  # mock answers B; q1 gold=B
    assert rows[1]["correct"] is False
    assert "ratio" in rows[0]
    assert "duration_ms" in rows[0]


def test_longbench_v2_is_registered_in_scenario_list() -> None:
    """Sanity check that the scenario reaches the public registry."""
    names = {info.name for info in scenarios.list_scenarios()}
    assert "longbench-v2" in names


def test_passthrough_compressor_does_not_change_text() -> None:
    out, stats = lbv2._passthrough_compressor("hello world")
    assert out == "hello world"
    assert stats["cost_usd"] == 0.0


def test_unknown_compressor_raises_value_error(monkeypatch: Any) -> None:
    cfg = lbv2._load_config()
    cfg["compressor"] = "made-up-compressor"
    with pytest.raises(ValueError, match="unknown compressor"):
        lbv2._build_compressor(cfg)


def test_unknown_provider_raises_value_error() -> None:
    cfg = {
        "provider": "made-up-provider",
        "model": "x",
        "max_tokens": 128,
    }
    with pytest.raises(ValueError, match="unknown provider"):
        lbv2._build_eval_caller(cfg)


def test_runner_raises_when_dataset_filters_to_empty(
    monkeypatch: Any, mock_anthropic: dict[str, Any]
) -> None:
    monkeypatch.setattr(lbv2, "_load_dataset", lambda cfg: [])
    _set_env(monkeypatch, COMPRESSOR="none", PROVIDER="anthropic")
    with pytest.raises(RuntimeError, match="returned 0 items"):
        lbv2.run(workload="all")


def test_stratified_sample_distributes_across_cells() -> None:
    items = [
        {"length": "short", "difficulty": "easy", "_id": f"e{i}"} for i in range(10)
    ] + [
        {"length": "long", "difficulty": "hard", "_id": f"h{i}"} for i in range(10)
    ]
    sample = lbv2._stratified_sample(items, 4)
    assert len(sample) <= 4
    cells = {(it["length"], it["difficulty"]) for it in sample}
    # Both cells should appear.
    assert len(cells) == 2


def test_msgs_to_text_handles_string_and_list_content() -> None:
    assert lbv2._msgs_to_text([{"role": "user", "content": "hello"}]) == "hello"
    assert (
        lbv2._msgs_to_text(
            [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        )
        == "hi"
    )
    assert lbv2._msgs_to_text([]) == ""
