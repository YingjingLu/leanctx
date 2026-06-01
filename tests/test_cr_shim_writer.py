"""Sprint 3 — write_shim_file() unit tests.

Tests verify the written cr_shim.mjs content without executing Node.js.
All marked unit.
"""
from __future__ import annotations

import pytest

from scripts.bench_clawtypeor_phase1 import write_shim_file

_SEVEN_LAYERS = [
    "deduplication",
    "whitespace",
    "dictionary",
    "paths",
    "jsonCompact",
    "observation",
    "dynamicCodebook",
]


@pytest.mark.unit
def test_write_shim_creates_file(tmp_path):
    write_shim_file(tmp_path)
    assert (tmp_path / "cr_shim.mjs").exists()


@pytest.mark.unit
def test_shim_uses_esm_imports(tmp_path):
    write_shim_file(tmp_path)
    src = (tmp_path / "cr_shim.mjs").read_text()
    assert src.startswith("import")


@pytest.mark.unit
def test_shim_imports_compress_context_from_dist(tmp_path):
    write_shim_file(tmp_path)
    src = (tmp_path / "cr_shim.mjs").read_text()
    assert "./dist/compression/index.js" in src


@pytest.mark.unit
def test_shim_has_health_endpoint(tmp_path):
    write_shim_file(tmp_path)
    src = (tmp_path / "cr_shim.mjs").read_text()
    assert "/health" in src


@pytest.mark.unit
def test_shim_has_all_seven_layer_flags(tmp_path):
    write_shim_file(tmp_path)
    src = (tmp_path / "cr_shim.mjs").read_text()
    for layer_key in _SEVEN_LAYERS:
        assert f"{layer_key}: true" in src, f"Missing layer flag: {layer_key}"


@pytest.mark.unit
def test_shim_port_reads_env_var(tmp_path):
    write_shim_file(tmp_path)
    src = (tmp_path / "cr_shim.mjs").read_text()
    assert "CR_SHIM_PORT" in src
