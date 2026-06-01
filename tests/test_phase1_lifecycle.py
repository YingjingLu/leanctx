"""Sprint 5 — service lifecycle unit tests (all I/O mocked).

subprocess.Popen, httpx.get, time.sleep, and time.time are mocked so no
real processes are spawned and no real network calls are made.
All marked unit.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import scripts.bench_clawtypeor_phase1 as harness
from scripts.bench_clawtypeor_phase1 import (
    health_check,
    spawn_cr_shim,
    spawn_leanctx_sidecar,
    wait_for_health,
)

# ── health_check tests ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_health_check_returns_true_on_200():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    with patch.object(harness, "httpx") as mock_httpx:
        mock_httpx.get.return_value = mock_resp
        assert health_check("http://127.0.0.1:8461") is True
        mock_httpx.get.assert_called_once_with("http://127.0.0.1:8461/health", timeout=2)


@pytest.mark.unit
def test_health_check_returns_false_on_connect_error():
    with patch.object(harness, "httpx") as mock_httpx:
        mock_httpx.get.side_effect = Exception("connect error")
        assert health_check("http://127.0.0.1:8461") is False


# ── wait_for_health tests ──────────────────────────────────────────────────


@pytest.mark.unit
def test_wait_for_health_succeeds_when_server_starts():
    with (
        patch.object(harness, "health_check", side_effect=[False, False, True]),
        patch.object(harness.time, "sleep") as mock_sleep,
    ):
        wait_for_health(None, "http://x", timeout=10)
        assert mock_sleep.call_count == 2


@pytest.mark.unit
def test_wait_for_health_raises_on_timeout():
    with (
        patch.object(harness, "health_check", return_value=False),
        patch.object(harness.time, "time", side_effect=[0, 100]),
        patch.object(harness.time, "sleep"),pytest.raises(TimeoutError, match="http://x")
    ):
        wait_for_health(None, "http://x", timeout=10)


# ── spawn_cr_shim tests ────────────────────────────────────────────────────


@pytest.mark.unit
def test_spawn_cr_shim_passes_env_to_node():
    mock_proc = MagicMock()
    with (
        patch.object(harness.subprocess, "Popen", return_value=mock_proc) as mock_popen,
        patch.object(harness, "wait_for_health"),
    ):
        spawn_cr_shim(
            Path("/tmp/x"),
            8461,
            {"LEANCTX_SIDECAR_URL": "http://127.0.0.1:8459"},
        )
        args, kwargs = mock_popen.call_args
        assert args[0][0] == "node"
        assert "LEANCTX_SIDECAR_URL" in kwargs["env"]


@pytest.mark.unit
def test_spawn_cr_shim_sets_cr_shim_port_env():
    mock_proc = MagicMock()
    with (
        patch.object(harness.subprocess, "Popen", return_value=mock_proc) as mock_popen,
        patch.object(harness, "wait_for_health"),
    ):
        spawn_cr_shim(Path("/tmp/x"), 8461, {})
        _, kwargs = mock_popen.call_args
        assert kwargs["env"]["CR_SHIM_PORT"] == "8461"


# ── spawn_leanctx_sidecar tests ────────────────────────────────────────────


@pytest.mark.unit
def test_spawn_sidecar_skips_if_already_healthy():
    with (
        patch.object(harness, "health_check", return_value=True),
        patch.object(harness.subprocess, "Popen") as mock_popen,
    ):
        result = spawn_leanctx_sidecar("http://127.0.0.1:8459", 0.5)
        mock_popen.assert_not_called()
        assert result is None


@pytest.mark.unit
def test_spawn_sidecar_starts_process_if_not_reachable():
    mock_proc = MagicMock()
    with (
        patch.object(harness, "health_check", side_effect=[False, True]),
        patch.object(harness.subprocess, "Popen", return_value=mock_proc) as mock_popen,
        patch.object(harness, "wait_for_health"),
    ):
        spawn_leanctx_sidecar("http://127.0.0.1:8459", 0.5)
        args, kwargs = mock_popen.call_args
        assert "leanctx-serve" in args[0]
        assert "LEANCTX_SERVER_LINGUA_RATIO" in kwargs["env"]
