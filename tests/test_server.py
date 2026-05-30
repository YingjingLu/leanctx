"""Tests for the leanctx HTTP compression service.

These run without the [lingua] extra / the 1.2 GB model: the service is
exercised in mode=off (passthrough) for HTTP-plumbing tests, and with a
fake compressor injected for the role-filtering / splice-by-index
behavior. No network, no torch.
"""

from __future__ import annotations

from typing import Any

import pytest

from leanctx import server as srv

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def _client(config: dict[str, Any]) -> TestClient:
    # Build the app explicitly so the startup warmup is a no-op for
    # mode=off and harmless otherwise (warmup is guarded).
    app = srv.create_app(config)
    return TestClient(app)


def test_health_reports_mode() -> None:
    with _client({"mode": "off"}) as client:
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["mode"] == "off"


def test_compress_passthrough_when_mode_off() -> None:
    """mode=off must return messages unchanged with passthrough stats —
    and never load a model."""
    with _client({"mode": "off"}) as client:
        msgs = [{"role": "user", "content": "hello " * 100}]
        r = client.post("/compress", json={"messages": msgs})
        assert r.status_code == 200
        body = r.json()
        assert body["messages"] == msgs
        assert body["stats"]["method"] == "passthrough"


def test_empty_messages_returns_passthrough_record() -> None:
    with _client({"mode": "off"}) as client:
        r = client.post("/compress", json={"messages": []})
        assert r.status_code == 200
        body = r.json()
        assert body["messages"] == []
        assert body["compressed_message_count"] == 0


def test_split_compressible_skips_tool_and_multimodal() -> None:
    """Only string-content system/user/assistant turns are eligible.
    tool messages and multimodal content lists are left untouched."""
    messages = [
        {"role": "system", "content": "you are helpful"},          # eligible 0
        {"role": "user", "content": "a long question " * 50},      # eligible 1
        {"role": "tool", "content": "tool result text"},           # skip (tool)
        {"role": "assistant", "content": "an answer " * 50},       # eligible 3
        {"role": "user", "content": [{"type": "text", "text": "x"}]},  # skip (list)
        {"role": "user", "content": None},                          # skip (null)
    ]
    subset, indices = srv._split_compressible(messages)
    assert indices == [0, 1, 3]
    assert [m["role"] for m in subset] == ["system", "user", "assistant"]


def test_splice_preserves_non_eligible_messages_verbatim() -> None:
    """With a fake compressor that uppercases content, only the eligible
    indices change; tool / multimodal / null messages are byte-identical."""

    class _FakeMiddleware:
        async def compress_messages_async(
            self, messages: list[dict[str, Any]]
        ) -> tuple[list[dict[str, Any]], Any]:
            from leanctx.stats import CompressionStats

            out = [
                {**m, "content": str(m["content"]).upper()} for m in messages
            ]
            return out, CompressionStats(
                input_tokens=100, output_tokens=40, ratio=0.4, method="lingua"
            )

    service = srv.CompressionService.__new__(srv.CompressionService)
    service._config = {"mode": "on"}
    service._middleware = _FakeMiddleware()  # type: ignore[assignment]

    import asyncio

    messages = [
        {"role": "user", "content": "compress me"},
        {"role": "tool", "content": "do not touch"},
        {"role": "user", "content": [{"type": "text", "text": "image-ish"}]},
    ]
    result = asyncio.run(service.compress(messages))

    assert result["messages"][0]["content"] == "COMPRESS ME"   # eligible → changed
    assert result["messages"][1]["content"] == "do not touch"  # tool → verbatim
    assert result["messages"][2]["content"] == [{"type": "text", "text": "image-ish"}]
    assert result["compressed_message_count"] == 1
    assert result["stats"]["method"] == "lingua"


def test_splice_falls_back_on_length_mismatch() -> None:
    """If the middleware returns a different message count than it was
    given, the splice is skipped and originals are preserved (safety)."""

    class _BadMiddleware:
        async def compress_messages_async(
            self, messages: list[dict[str, Any]]
        ) -> tuple[list[dict[str, Any]], Any]:
            from leanctx.stats import CompressionStats

            # Returns FEWER messages than given — must not corrupt output.
            return messages[:-1], CompressionStats(method="lingua")

    service = srv.CompressionService.__new__(srv.CompressionService)
    service._config = {"mode": "on"}
    service._middleware = _BadMiddleware()  # type: ignore[assignment]

    import asyncio

    messages = [
        {"role": "user", "content": "one"},
        {"role": "user", "content": "two"},
    ]
    result = asyncio.run(service.compress(messages))
    # length mismatch → originals preserved
    assert result["messages"] == messages


def test_default_config_from_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("LEANCTX_SERVER_MODE", "on")
    monkeypatch.setenv("LEANCTX_SERVER_THRESHOLD", "2000")
    monkeypatch.setenv("LEANCTX_SERVER_LINGUA_RATIO", "0.3")
    monkeypatch.delenv("LEANCTX_SERVER_CONFIG", raising=False)
    monkeypatch.delenv("LEANCTX_SERVER_ROUTING", raising=False)
    monkeypatch.delenv("LEANCTX_SERVER_DEDUP", raising=False)

    cfg = srv._default_config()
    assert cfg["mode"] == "on"
    assert cfg["trigger"]["threshold_tokens"] == 2000
    assert cfg["routing"] == {"prose": "lingua"}
    assert cfg["lingua"]["ratio"] == 0.3
    assert cfg["strategies"]["dedup"] is False  # safe default: host owns dedup


def test_default_config_threshold_is_nonzero() -> None:
    """Regression guard: the default threshold must NOT be 0 — compressing
    very short turns risks dropping load-bearing instruction tokens."""
    import os

    for k in (
        "LEANCTX_SERVER_CONFIG",
        "LEANCTX_SERVER_MODE",
        "LEANCTX_SERVER_THRESHOLD",
        "LEANCTX_SERVER_ROUTING",
        "LEANCTX_SERVER_DEDUP",
    ):
        os.environ.pop(k, None)
    cfg = srv._default_config()
    assert cfg["trigger"]["threshold_tokens"] >= 1000


def test_full_config_env_overrides_everything(monkeypatch: Any) -> None:
    monkeypatch.setenv(
        "LEANCTX_SERVER_CONFIG",
        '{"mode":"off","routing":{"code":"verbatim"}}',
    )
    cfg = srv._default_config()
    assert cfg == {"mode": "off", "routing": {"code": "verbatim"}}
