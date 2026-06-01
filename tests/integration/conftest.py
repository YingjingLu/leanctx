"""Session-scoped fixtures for Sprint 6 + 7 integration tests.

Setup (runs once per test session):
  1. Clone + patch + build ClawRouter at pinned commit → /tmp/clawtypeor_intg_test
  2. Write cr_shim.mjs into the workdir
  3. Spawn CR shim without sidecar (port 8461)    → built_cr_shim_no_sidecar
  4. Auto-start leanctx-serve, spawn CR shim      → built_cr_shim_with_sidecar

leanctx-serve is resolved from the Python interpreter's bin directory so
it works correctly when tests run inside a venv.  The sidecar starts with
LEANCTX_SERVER_MODE=on when llmlingua is installed, mode=off otherwise.
Test 7 (Layer 8 comparison) skips automatically when llmlingua is absent.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.bench_clawtypeor_phase1 import (
    CR_DEFAULT_COMMIT,
    health_check,
    setup_clawtypeor,
    spawn_cr_shim,
    write_shim_file,
)

WORKDIR = Path("/tmp/clawtypeor_intg_test")
SIDECAR_URL = "http://127.0.0.1:8459"

# leanctx-serve lives next to the Python interpreter — works in venv and system install.
_LEANCTX_SERVE = str(Path(sys.executable).parent / "leanctx-serve")


def _llmlingua_available() -> bool:
    try:
        import llmlingua  # noqa: F401

        return True
    except ImportError:
        return False


# ── one-time CR clone + build ──────────────────────────────────────────────


@pytest.fixture(scope="session")
def built_cr():
    """Clone, patch, and build ClawRouter once for the whole test session."""
    WORKDIR.mkdir(parents=True, exist_ok=True)
    if not (WORKDIR / "dist" / "compression" / "index.js").exists():
        setup_clawtypeor(WORKDIR, commit=CR_DEFAULT_COMMIT)
    write_shim_file(WORKDIR)
    return WORKDIR


# ── CR shim — no sidecar (Leg A) ──────────────────────────────────────────


@pytest.fixture(scope="session")
def built_cr_shim_no_sidecar(built_cr):
    """Start CR shim on port 8461 with no leanctx sidecar (Leg A mode)."""
    proc = spawn_cr_shim(built_cr, port=8461, extra_env={})
    yield "http://127.0.0.1:8461"
    proc.terminate()
    proc.wait()


# ── leanctx sidecar ───────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def leanctx_sidecar():
    """Start leanctx-serve if not already running; yield (url, proc, lingua_ok)."""
    lingua_ok = _llmlingua_available()
    mode = "on" if lingua_ok else "off"
    env = {
        **os.environ,
        # Prepend venv bin so leanctx-serve is found even if the shell wasn't activated.
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
        "LEANCTX_SERVER_MODE": mode,
        "LEANCTX_SERVER_LINGUA_RATIO": "0.5",
    }

    already_running = health_check(SIDECAR_URL)
    if already_running:
        yield SIDECAR_URL, None, lingua_ok
        return

    proc = subprocess.Popen([_LEANCTX_SERVE], env=env)

    deadline = time.time() + 60  # model warmup can take ~30 s
    while not health_check(SIDECAR_URL):
        if time.time() > deadline:
            proc.terminate()
            proc.wait()
            pytest.skip("leanctx-serve did not become healthy within 60 s")
        time.sleep(1)

    yield SIDECAR_URL, proc, lingua_ok

    proc.terminate()
    proc.wait()


# ── CR shim + leanctx sidecar (Leg B) ─────────────────────────────────────


@pytest.fixture(scope="session")
def built_cr_shim_with_sidecar(built_cr, leanctx_sidecar):
    """Start CR shim on port 8462 pointing at the leanctx sidecar (Leg B mode)."""
    sidecar_url, _proc, lingua_ok = leanctx_sidecar
    proc = spawn_cr_shim(
        built_cr,
        port=8462,
        extra_env={"LEANCTX_SIDECAR_URL": sidecar_url},
    )
    yield "http://127.0.0.1:8462", lingua_ok
    proc.terminate()
    proc.wait()
