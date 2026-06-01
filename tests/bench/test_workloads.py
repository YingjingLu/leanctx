"""Sprint 1 tests — agent_extended workload fixture.

RED → GREEN sequence per implementation_test_plan.html Sprint 1.
All tests are pure Python with no I/O (unit marker).
"""

from __future__ import annotations

import pytest

from leanctx.bench.workloads import load_workload
from leanctx.tokens import count_tokens

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _all_tool_use_ids(messages: list[dict]) -> set[str]:
    ids: set[str] = set()
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_id = block.get("id")
                    if tool_id:
                        ids.add(tool_id)
    return ids


def _all_tool_result_ids(messages: list[dict]) -> set[str]:
    ids: set[str] = set()
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    ref_id = block.get("tool_use_id")
                    if ref_id:
                        ids.add(ref_id)
    return ids


def _concatenated_text(messages: list[dict]) -> str:
    parts: list[str] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    t = block.get("text", "")
                    if isinstance(t, str):
                        parts.append(t)
                elif btype == "tool_result":
                    c = block.get("content", "")
                    if isinstance(c, str):
                        parts.append(c)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_agent_extended_message_count():
    messages = load_workload("agent_extended")
    assert len(messages) == 50, f"expected 50 messages, got {len(messages)}"


@pytest.mark.unit
def test_agent_extended_has_five_tool_use_episodes():
    messages = load_workload("agent_extended")
    tool_use_messages = [
        msg for msg in messages
        if isinstance(msg.get("content"), list)
        and any(
            isinstance(b, dict) and b.get("type") == "tool_use"
            for b in msg["content"]
        )
    ]
    assert len(tool_use_messages) >= 5, (
        f"expected >= 5 messages with tool_use blocks, got {len(tool_use_messages)}"
    )


@pytest.mark.unit
def test_agent_extended_tool_ids_all_linked():
    messages = load_workload("agent_extended")
    use_ids = _all_tool_use_ids(messages)
    result_ids = _all_tool_result_ids(messages)
    assert use_ids == result_ids, (
        f"tool_use IDs and tool_result IDs do not match.\n"
        f"  only in tool_use:    {use_ids - result_ids}\n"
        f"  only in tool_result: {result_ids - use_ids}"
    )


@pytest.mark.unit
def test_agent_extended_has_compressible_prose():
    messages = load_workload("agent_extended")
    long_prose = [
        msg for msg in messages
        if msg.get("role") == "assistant"
        and isinstance(msg.get("content"), str)
        and len(msg["content"]) > 400
    ]
    assert long_prose, (
        "expected at least one assistant string message with len > 400 chars"
    )


@pytest.mark.unit
def test_agent_extended_has_code_blocks():
    messages = load_workload("agent_extended")
    combined = _concatenated_text(messages)
    assert "```" in combined, "expected fenced code blocks (```) in combined message content"


@pytest.mark.unit
def test_agent_extended_has_error_tracebacks():
    messages = load_workload("agent_extended")
    combined = _concatenated_text(messages)
    assert "Traceback" in combined, "expected 'Traceback' in combined message content"


@pytest.mark.unit
def test_agent_extended_total_tokens_above_threshold():
    messages = load_workload("agent_extended")
    combined = _concatenated_text(messages)
    total = count_tokens(combined)
    assert total > 8000, (
        f"expected combined token count > 8000 for leanctx threshold coverage, got {total}"
    )
