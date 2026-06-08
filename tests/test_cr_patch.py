"""Sprint 2 — apply_patch() unit tests.

Tests operate on synthetic TypeScript/tsup strings written into tmp_path.
No Node.js, no filesystem I/O beyond tmp_path. All marked unit.
"""
from __future__ import annotations

import re

import pytest

from benchmarks.clawrouter.bench_phase1 import (
    LAYER8_CALL_BLOCK,
    apply_patch,
)

# ── Shared fixtures ────────────────────────────────────────────────────────

ANCHOR = "  const compressedChars = calculateTotalChars(result);"

FAKE_TS_SRC = """\
// ... earlier layers ...
  const compressedChars = calculateTotalChars(result);
  const compressionRatio = compressedChars / originalChars;
  return { messages: result };
}
"""

FAKE_TS_NO_ANCHOR = "export function foo() { return 42; }"

FAKE_TSUP_SRC = """\
import { defineConfig } from "tsup";
export default defineConfig({
  entry: ["src/index.ts", "src/cli.ts"],
  format: ["esm"],
  dts: true,
});
"""


def _setup_fake_cr(tmp_path):
    """Write minimal synthetic CR layout so apply_patch(tmp_path) can run."""
    ts_dir = tmp_path / "src" / "compression"
    ts_dir.mkdir(parents=True)
    (ts_dir / "index.ts").write_text(FAKE_TS_SRC)
    (tmp_path / "tsup.config.ts").write_text(FAKE_TSUP_SRC)


def _setup_fake_cr_no_anchor(tmp_path):
    ts_dir = tmp_path / "src" / "compression"
    ts_dir.mkdir(parents=True)
    (ts_dir / "index.ts").write_text(FAKE_TS_NO_ANCHOR)
    (tmp_path / "tsup.config.ts").write_text(FAKE_TSUP_SRC)


# ── Sprint 2 tests ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_layer8_block_inserted_before_anchor(tmp_path):
    _setup_fake_cr(tmp_path)
    apply_patch(tmp_path)
    result = (tmp_path / "src" / "compression" / "index.ts").read_text()

    assert LAYER8_CALL_BLOCK in result
    assert result.index(LAYER8_CALL_BLOCK) < result.index(ANCHOR)


@pytest.mark.unit
def test_anchor_line_still_present_after_patch(tmp_path):
    _setup_fake_cr(tmp_path)
    apply_patch(tmp_path)
    result = (tmp_path / "src" / "compression" / "index.ts").read_text()

    assert ANCHOR in result


@pytest.mark.unit
def test_missing_anchor_raises_runtime_error(tmp_path):
    _setup_fake_cr_no_anchor(tmp_path)
    with pytest.raises(RuntimeError, match=re.escape(ANCHOR.strip())):
        apply_patch(tmp_path)


@pytest.mark.unit
def test_idempotent_no_double_patch(tmp_path):
    _setup_fake_cr(tmp_path)
    apply_patch(tmp_path)
    apply_patch(tmp_path)
    result = (tmp_path / "src" / "compression" / "index.ts").read_text()

    assert result.count(LAYER8_CALL_BLOCK) == 1
    assert result.count("leanctxLayer8") == 2  # one import, one call


@pytest.mark.unit
def test_layer8_imports_productized_connector(tmp_path):
    # Behaviour (env gate, timeout, fail-open, length guard) now lives in the
    # `@leanctx/clawrouter-connector` package — the patch only imports + calls it.
    _setup_fake_cr(tmp_path)
    apply_patch(tmp_path)
    result = (tmp_path / "src" / "compression" / "index.ts").read_text()

    assert '@leanctx/clawrouter-connector' in result
    assert "import { leanctxLayer8 }" in result


@pytest.mark.unit
def test_layer8_call_reassigns_result(tmp_path):
    _setup_fake_cr(tmp_path)
    apply_patch(tmp_path)
    result = (tmp_path / "src" / "compression" / "index.ts").read_text()

    assert "result = await leanctxLayer8(result);" in result


@pytest.mark.unit
def test_tsup_compression_entry_added(tmp_path):
    _setup_fake_cr(tmp_path)
    apply_patch(tmp_path)
    result = (tmp_path / "tsup.config.ts").read_text()

    assert "src/compression/index.ts" in result


@pytest.mark.unit
def test_tsup_patch_idempotent(tmp_path):
    _setup_fake_cr(tmp_path)
    apply_patch(tmp_path)
    apply_patch(tmp_path)
    result = (tmp_path / "tsup.config.ts").read_text()

    assert result.count("src/compression/index.ts") == 1
