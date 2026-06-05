"""CI-gated integration test for the leanctx HTTP sidecar.

Unlike tests/test_server.py (which drives the ASGI app in-process via
TestClient), this test boots the **real** ``leanctx-serve`` console script as
a subprocess and talks to it over HTTP. It therefore exercises the parts the
in-process tests cannot: the packaged entry point, uvicorn startup, the
env-var → config path (build_config_from_env), and a genuine network round
trip through the middleware.

It needs neither Node/ClawRouter nor the 1.2 GB Lingua model — routing is set
to ``{}`` so compression falls through to Verbatim — so it runs in CI on every
push (marked ``behavioral``, not ``integration``), giving the benchmark suite at
least one always-on gate that exercises the real compress round-trip.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

import httpx  # noqa: E402

from benchmarks.clawrouter.bench_phase1 import (  # noqa: E402
    health_check,
    wait_for_health,
)

pytestmark = pytest.mark.behavioral

_PORT = 8473
_URL = f"http://127.0.0.1:{_PORT}"
_LEANCTX_SERVE = str(Path(sys.executable).parent / "leanctx-serve")


@pytest.fixture(scope="module")
def sidecar():
    """Boot a real leanctx-serve process (mode=on, no model) for the module."""
    if health_check(_URL):
        pytest.skip(f"something is already listening on {_URL}")
    env = {
        **os.environ,
        "LEANCTX_SERVER_MODE": "on",
        "LEANCTX_SERVER_THRESHOLD": "0",  # process every request
        "LEANCTX_SERVER_ROUTING": "{}",  # no type maps to Lingua → Verbatim
        "LEANCTX_SERVER_DEDUP": "off",
    }
    proc = subprocess.Popen(
        [_LEANCTX_SERVE, "--port", str(_PORT)],
        env=env,
    )
    try:
        wait_for_health(proc, _URL, timeout=30)
    except Exception:
        proc.terminate()
        proc.wait()
        raise
    yield _URL
    proc.terminate()
    proc.wait()


def test_health_reports_mode_on(sidecar):
    r = httpx.get(f"{sidecar}/health", timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["mode"] == "on"


def test_compress_round_trip_preserves_content(sidecar):
    """A real HTTP round trip returns a well-formed response and, with
    Verbatim routing, preserves message content exactly."""
    msgs = [
        {"role": "user", "content": "the quick brown fox " * 50},
        {"role": "assistant", "content": "jumps over the lazy dog " * 50},
    ]
    r = httpx.post(f"{sidecar}/compress", json={"messages": msgs}, timeout=30)
    assert r.status_code == 200
    body = r.json()
    assert {"messages", "stats", "compressed_message_count"} <= set(body)
    assert len(body["messages"]) == len(msgs)
    assert [m["content"] for m in body["messages"]] == [m["content"] for m in msgs]
    assert body["stats"]["method"] in ("verbatim", "passthrough")


def test_compress_leaves_tool_and_multimodal_untouched(sidecar):
    """Structured (non-string) content must survive the round trip verbatim."""
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "tool", "content": "tool output"},
    ]
    r = httpx.post(f"{sidecar}/compress", json={"messages": msgs}, timeout=30)
    assert r.status_code == 200
    body = r.json()
    assert body["messages"] == msgs
