"""Session-scoped fixtures for Sprint 8 end-to-end tests.

Reuses the built ClawRouter from Sprint 6 (/tmp/clawtypeor_intg_test).
Makes real API calls using keys from .env (loaded via dotenv).
Estimated cost: ~$0.50 for 5 LB questions × 2 legs.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.bench_clawtypeor_phase1 import (
    CR_DEFAULT_COMMIT,
    _load_lb_items,
    health_check,
    run_leg,
    setup_clawtypeor,
    spawn_cr_shim,
    write_shim_file,
)

WORKDIR = Path("/tmp/clawtypeor_intg_test")  # reuse Sprint 6 build
SIDECAR_URL = "http://127.0.0.1:8459"
_LEANCTX_SERVE = str(Path(sys.executable).parent / "leanctx-serve")
_LB_LIMIT = 5  # questions per leg — keeps cost ~$0.50 total

_EVAL_CFG = {
    "provider": os.environ.get("BENCHMARK_EVAL_PROVIDER", "anthropic"),
    "model": os.environ.get("BENCHMARK_EVAL_MODEL", "claude-sonnet-4-6"),
    "max_tokens": 64,
}


def _ensure_api_keys() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("No ANTHROPIC_API_KEY or OPENAI_API_KEY in environment / .env")


# ── one-time CR setup (skip if already built) ──────────────────────────────


@pytest.fixture(scope="session")
def e2e_cr_workdir():
    WORKDIR.mkdir(parents=True, exist_ok=True)
    if not (WORKDIR / "dist" / "compression" / "index.js").exists():
        setup_clawtypeor(WORKDIR, commit=CR_DEFAULT_COMMIT)
    write_shim_file(WORKDIR)
    return WORKDIR


# ── sidecar ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def e2e_sidecar():
    import subprocess
    import time

    if health_check(SIDECAR_URL):
        yield SIDECAR_URL
        return

    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
        "LEANCTX_SERVER_MODE": "off",  # passthrough — no llmlingua needed for e2e
    }
    proc = subprocess.Popen([_LEANCTX_SERVE], env=env)
    deadline = time.time() + 60
    while not health_check(SIDECAR_URL):
        if time.time() > deadline:
            proc.terminate()
            proc.wait()
            pytest.skip("leanctx-serve did not start within 60 s")
        time.sleep(1)
    yield SIDECAR_URL
    proc.terminate()
    proc.wait()


# ── full pipeline fixture (both legs, 5 LB questions) ─────────────────────


@pytest.fixture(scope="session")
def full_pipeline(e2e_cr_workdir, e2e_sidecar):
    """Run both legs and return (records_a, records_b, lb_items)."""
    _ensure_api_keys()

    lb_items = _load_lb_items(limit=_LB_LIMIT, workload_tag="lb_s1")

    # Leg A — CR only
    proc_a = spawn_cr_shim(e2e_cr_workdir, port=8471, extra_env={})
    try:
        records_a = run_leg("A", lb_items, "http://127.0.0.1:8471", lb_cfg=_EVAL_CFG)
    finally:
        proc_a.terminate()
        proc_a.wait()

    # Leg B — CR + leanctx sidecar (passthrough if no llmlingua)
    proc_b = spawn_cr_shim(
        e2e_cr_workdir,
        port=8472,
        extra_env={"LEANCTX_SIDECAR_URL": e2e_sidecar},
    )
    try:
        records_b = run_leg("B", lb_items, "http://127.0.0.1:8472", lb_cfg=_EVAL_CFG)
    finally:
        proc_b.terminate()
        proc_b.wait()

    return records_a, records_b, lb_items
