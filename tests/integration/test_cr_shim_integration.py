"""Sprint 6 — CR shim integration tests.

All tests marked integration. Require Node >=20 and built ClawRouter dist/.
The session fixture clones + builds ClawRouter once (~60-90 s first run;
cached in /tmp/clawtypeor_intg_test on subsequent runs).

Key CR behaviour discovered during Sprint 6:
- Whitespace compression triggers on content with multiple spaces.
- Observation compression (Layer 6) only fires on role="tool" messages with
  string content > 500 chars (NOT on Anthropic tool_result blocks).
- Dynamic codebook compresses repeated phrases aggressively (~96% on 3+ repeats).
- For tests that must NOT show Lingua-level compression (test 6), use a single
  unique non-repetitive paragraph (ratio ~0.9992 with CR alone).

Test 7 (Layer 8 produces lower ratio) skips automatically when llmlingua is
not installed.
"""
from __future__ import annotations

import httpx
import pytest

# ── Test fixtures ──────────────────────────────────────────────────────────

# Single unique incident-report paragraph (~1 200 chars).
# CR's structural layers barely touch it (ratio ≈ 0.999) — good for the
# "Layer 8 did NOT run" assertion in test 6.
_UNIQUE_PROSE = (
    "At 14:32 UTC the authentication microservice began returning HTTP 503 errors. "
    "The on-call engineer noticed the spike in Grafana at 14:34 UTC. "
    "Initial investigation pointed to an exhausted thread pool in the Java service. "
    "A heap dump revealed that threads were blocked waiting for a database connection. "
    "The connection pool was configured with max-size 10 while concurrent requests peaked at 47. "
    "The fix was a two-step process: first, an emergency configmap update to raise max-size to 50; "
    "second, a rolling restart of the affected pods to pick up the new configuration. "
    "After the restart, error rates dropped from 34 percent back to baseline within 90 seconds. "
    "A post-incident review identified three other services with similar pool-sizing risk. "
    "The team scheduled capacity-planning sessions for the following sprint to address each one. "
    "Going forward, connection pool utilisation will be added to the SLO dashboard so that "
    "sustained high-watermark events trigger a pager alert before full exhaustion occurs. "
    "This incident highlighted the gap between load-test scenarios and production traffic, "
    "particularly the bursty nature of authentication requests during peak business hours."
)

# Repeated log lines in a role="tool" message (OpenAI format).
# Layer 6 (observation compression) fires on: role="tool", string content > 500 chars.
_TOOL_LOG_MESSAGES = [
    {
        "role": "assistant",
        "content": "Let me check the service logs.",
        "tool_calls": [
            {
                "id": "call_01",
                "type": "function",
                "function": {"name": "grep_logs", "arguments": "{}"},
            }
        ],
    },
    {
        "role": "tool",
        "tool_call_id": "call_01",
        "content": "\n".join(
            [f"INFO health-check ok pid=1234 ts={1717000000 + i}" for i in range(80)]
        ),
    },
]

# Prose with a fenced code block — tests verbatim code preservation.
_CODE_PROSE = (
    "The configuration for the database connection pool requires careful tuning. "
    "After extensive load testing we settled on the following settings:\n\n"
    "```python\nDB_POOL_SIZE = 20\nDB_MAX_OVERFLOW = 10\nDB_POOL_TIMEOUT = 30\n```\n\n"
    "These values were chosen empirically by measuring throughput degradation across "
    "a range of concurrent connection counts during peak load scenarios."
)

# Five structurally distinct messages (no repetition) for message-count test.
_FIVE_MESSAGES = [
    {"role": "system", "content": "You are a senior SRE helping diagnose incidents."},
    {"role": "user", "content": "We had a 503 storm on the auth service at 14:32 UTC."},
    {"role": "assistant", "content": _UNIQUE_PROSE[:400]},
    {"role": "user", "content": "What should we check next?"},
    {"role": "assistant", "content": "I recommend inspecting the connection pool metrics."},
]


def _post_compress(shim_url: str, messages: list[dict]) -> dict:
    # 120 s: Layer 8 Lingua inference on CPU takes ~38 s for agent_extended.
    r = httpx.post(shim_url, json={"messages": messages}, timeout=120)
    r.raise_for_status()
    return r.json()


# ── Sprint 6 tests ─────────────────────────────────────────────────────────


@pytest.mark.integration
def test_shim_health_endpoint_responds(built_cr_shim_no_sidecar):
    shim_url = built_cr_shim_no_sidecar
    r = httpx.get(f"{shim_url}/health", timeout=5)
    assert r.status_code == 200
    assert r.text == "ok"


@pytest.mark.integration
def test_shim_compresses_long_prose_message(built_cr_shim_no_sidecar):
    """Layer 6 (observation) compresses a role='tool' message with 80 repeated log lines."""
    shim_url = built_cr_shim_no_sidecar
    result = _post_compress(shim_url, _TOOL_LOG_MESSAGES)
    assert result["compressionRatio"] < 0.95, (
        f"Expected compressionRatio < 0.95, got {result['compressionRatio']:.4f}"
    )


@pytest.mark.integration
def test_shim_preserves_message_count(built_cr_shim_no_sidecar):
    shim_url = built_cr_shim_no_sidecar
    result = _post_compress(shim_url, _FIVE_MESSAGES)
    assert len(result["messages"]) == 5


@pytest.mark.integration
def test_shim_preserves_code_block_verbatim(built_cr_shim_no_sidecar):
    shim_url = built_cr_shim_no_sidecar
    messages = [{"role": "user", "content": _CODE_PROSE}]
    result = _post_compress(shim_url, messages)
    compressed_content = result["messages"][0]["content"]
    assert "DB_POOL_SIZE = 20" in compressed_content, (
        "Code block content was not preserved verbatim in compressed output"
    )


@pytest.mark.integration
def test_shim_applies_all_seven_layers(built_cr_shim_no_sidecar):
    """Layer 6 observation compression fires on role='tool' content > 500 chars."""
    shim_url = built_cr_shim_no_sidecar
    result = _post_compress(shim_url, _TOOL_LOG_MESSAGES)
    obs = result.get("stats", {}).get("observationsCompressed", 0)
    assert obs > 0, (
        f"Expected observationsCompressed > 0 (got {obs}); "
        "Layer 6 should compress a role='tool' string message longer than 500 chars"
    )


@pytest.mark.integration
def test_shim_layer8_inactive_without_sidecar_env(built_cr_shim_no_sidecar):
    """Without sidecar env, Layer 8 (Lingua) must not run.

    Uses a single unique prose paragraph (no phrase repetition) so CR's dynamic
    codebook also doesn't fire — giving a ratio very close to 1.0. A Lingua run
    would push the ratio below 0.35.
    """
    shim_url = built_cr_shim_no_sidecar
    messages = [{"role": "user", "content": _UNIQUE_PROSE}]
    result = _post_compress(shim_url, messages)
    ratio = result["compressionRatio"]
    assert ratio > 0.35, (
        f"compressionRatio={ratio:.3f} suspiciously small — "
        "Lingua should NOT have run on the no-sidecar shim"
    )


@pytest.mark.integration
def test_shim_layer8_produces_lower_ratio_than_cr_alone(
    built_cr_shim_no_sidecar, built_cr_shim_with_sidecar
):
    """Layer 8 (Lingua) must compress prose further than CR alone.

    Uses the agent_extended workload: 30 string-content messages, ~3 364 tokens
    after CR compression — comfortably above leanctx's 1 500-token threshold.
    CR dynamic codebook barely touches unique prose, so the full token mass is
    available for Lingua to compress (confirmed 47.7 % reduction in manual tests).

    Skipped automatically when llmlingua is not installed.
    """
    from leanctx.bench.workloads import load_workload

    shim_url_b, lingua_ok = built_cr_shim_with_sidecar
    if not lingua_ok:
        pytest.skip(
            "llmlingua not installed — sidecar runs in passthrough mode; "
            "install 'leanctx[lingua]' to run the Layer 8 comparison test"
        )

    messages = load_workload("agent_extended")
    result_a = _post_compress(built_cr_shim_no_sidecar, messages)
    result_b = _post_compress(shim_url_b, messages)

    ratio_a = result_a["compressionRatio"]
    ratio_b = result_b["compressionRatio"]

    assert ratio_b < ratio_a, (
        f"Layer 8 (Lingua) should compress further: "
        f"ratio_without={ratio_a:.3f} ratio_with={ratio_b:.3f}"
    )
