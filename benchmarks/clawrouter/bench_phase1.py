"""Phase 1 go/no-go benchmark harness for ClawRouter × leanctx integration.

Architecture:
  Phase A: clone + patch + build ClawRouter at pinned commit
  Phase B: spawn leanctx sidecar + CR shim
  Phase C: two-leg run (Leg A = CR only, Leg B = CR + leanctx Layer 8)
  Phase D: score, apply gate, write report
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

# ── Repository + defaults ──────────────────────────────────────────────────

CR_REPO_URL = "https://github.com/BlockRunAI/ClawRouter.git"
CR_DEFAULT_COMMIT = "89269507b2173200222d03c1d4a0f80665b525d1"

# Productized Layer-8 connector — integrations/clawrouter/connector, published
# as `@leanctx/clawrouter-connector`. The benchmark builds + installs this into
# the cloned CR and imports it, instead of carrying its own hand-maintained copy
# of the Layer-8 hook (the former LAYER8_TS_BLOCK). Single source of truth.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONNECTOR_DIR = _REPO_ROOT / "integrations" / "clawrouter" / "connector"
_CONNECTOR_PKG = "@leanctx/clawrouter-connector"

# ── Layer 8 injection ──────────────────────────────────────────────────────
# The connector owns all the behaviour (env gate, eligibility, fetch, timeout,
# fail-open, one-in-one-out guard). The patch just imports it and calls it
# immediately before the anchor line
#   const compressedChars = calculateTotalChars(result);
# When LEANCTX_SIDECAR_URL is unset the connector is a no-op, so CR behaviour is
# unchanged — exactly as a real ClawRouter user would wire it (see the
# connector README).

_LAYER8_IMPORT = f'import {{ leanctxLayer8 }} from "{_CONNECTOR_PKG}";\n'

LAYER8_CALL_BLOCK = """\
  // ── Layer 8: leanctx LLMLingua-2 prose pass (opt-in, fail-open) ──────────
  // Productized connector (integrations/clawrouter/connector). No-op unless
  // LEANCTX_SIDECAR_URL is set; any error/timeout returns `result` unchanged.
  result = await leanctxLayer8(result);
  // ── end Layer 8 ──────────────────────────────────────────────────────────"""

# ── CR shim — Node.js ESM source written into workdir during setup ─────────

CR_SHIM_MJS = """\
import { createServer } from "http";
import { compressContext } from "./dist/compression/index.js";

const PORT = parseInt(process.env.CR_SHIM_PORT ?? "8461");

// CompressionConfig uses a nested `layers` object — flat keys are ignored.
const FULL_CONFIG = {
  layers: {
    deduplication: true,
    whitespace: true,
    dictionary: true,
    paths: true,
    jsonCompact: true,
    observation: true,
    dynamicCodebook: true,
  },
};

createServer(async (req, res) => {
  if (req.method === "GET" && req.url === "/health") {
    res.writeHead(200).end("ok");
    return;
  }
  if (req.method !== "POST") { res.writeHead(405).end(); return; }
  let body = "";
  for await (const chunk of req) body += chunk;
  try {
    const { messages, config } = JSON.parse(body);
    const callerCfg = config ?? {};
    const mergedCfg = {
      ...FULL_CONFIG,
      ...callerCfg,
      layers: { ...FULL_CONFIG.layers, ...(callerCfg.layers ?? {}) },
    };
    const result = await compressContext(messages, mergedCfg);
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify(result));
  } catch (e) {
    res.writeHead(500, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: String(e) }));
  }
}).listen(PORT, () => console.log(`CR shim listening on :${PORT}`));
"""

# ═══════════════════════════════════════════════════════════════════════════
# Sprint 2 — Patch machinery
# ═══════════════════════════════════════════════════════════════════════════

_TS_ANCHOR = "  const compressedChars = calculateTotalChars(result);"
_TSUP_OLD = 'entry: ["src/index.ts", "src/cli.ts"],'
_TSUP_NEW = 'entry: ["src/index.ts", "src/cli.ts", "src/compression/index.ts"],'


def apply_patch(cr_dir: Path) -> None:
    _patch_compression_ts(cr_dir)
    _patch_tsup_config(cr_dir)


def _patch_compression_ts(cr_dir: Path) -> None:
    target = cr_dir / "src" / "compression" / "index.ts"
    src = target.read_text()
    if _TS_ANCHOR not in src:
        raise RuntimeError(
            f"Patch A anchor not found in {target}. "
            f"Expected: {_TS_ANCHOR!r}. Re-check --cr-commit."
        )
    if "leanctxLayer8" in src:
        return  # idempotent
    src = _LAYER8_IMPORT + src
    src = src.replace(_TS_ANCHOR, LAYER8_CALL_BLOCK + "\n" + _TS_ANCHOR)
    target.write_text(src)
    print(f"[patch A] Layer 8 connector wired into {target.relative_to(cr_dir)}")


def _patch_tsup_config(cr_dir: Path) -> None:
    target = cr_dir / "tsup.config.ts"
    src = target.read_text()
    if _TSUP_OLD not in src:
        if "src/compression/index.ts" in src:
            return  # idempotent
        raise RuntimeError(
            f"Patch B anchor not found in {target}. Re-check --cr-commit."
        )
    target.write_text(src.replace(_TSUP_OLD, _TSUP_NEW))
    print(f"[patch B] Compression entry added to {target.relative_to(cr_dir)}")


# ═══════════════════════════════════════════════════════════════════════════
# Sprint 3 — Shim file writer
# ═══════════════════════════════════════════════════════════════════════════


def write_shim_file(workdir: Path) -> Path:
    path = workdir / "cr_shim.mjs"
    path.write_text(CR_SHIM_MJS)
    return path


# ═══════════════════════════════════════════════════════════════════════════
# Sprint 4 — Scorer & reporter
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class GateResult:
    passed: bool
    fail_reason: str = ""


def _percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in [0, 1]); None for an empty sample."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    idx = min(int(round(q * (len(s) - 1))), len(s) - 1)
    return float(s[idx])


def compute_metrics(
    records_a: list[dict[str, Any]],
    records_b: list[dict[str, Any]],
    records_cb: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    tokens_a = sum(r["tokens_compressed"] for r in records_a)
    tokens_b = sum(r["tokens_compressed"] for r in records_b)

    avg_raw = sum(r["tokens_raw"] for r in records_a) / len(records_a)
    avg_a = tokens_a / len(records_a)
    avg_b = tokens_b / len(records_b)

    delta_tokens = (tokens_a - tokens_b) / tokens_a if tokens_a > 0 else 0.0
    cr_savings = (avg_raw - avg_a) / avg_raw if avg_raw > 0 else 0.0
    e2e_ratio = avg_b / avg_raw if avg_raw > 0 else 0.0

    acc_a_vals = [r["accuracy"] for r in records_a if r.get("accuracy") is not None]
    acc_b_vals = [r["accuracy"] for r in records_b if r.get("accuracy") is not None]
    acc_a = sum(acc_a_vals) / len(acc_a_vals) if acc_a_vals else 0.0
    acc_b = sum(acc_b_vals) / len(acc_b_vals) if acc_b_vals else 0.0
    delta_accuracy = acc_b - acc_a

    # Closed-book control: accuracy when the model answers from priors alone.
    # If Leg A/B accuracy is at or below this, the context did no work and the
    # "compression preserved information" claim is unsupported.
    cb_vals = [
        r["accuracy"]
        for r in (records_cb or [])
        if r.get("accuracy") is not None
    ]
    acc_closed_book: float | None = (
        sum(cb_vals) / len(cb_vals) if cb_vals else None
    )

    # $15 / 1M input tokens (Sonnet price) × 1K conversations
    cost_saved_per_1k = delta_tokens * avg_raw * (15 / 1e6) * 1000

    # Sidecar (Layer 8) latency is the compression call in isolation, taken
    # from Leg B records (the only leg with the sidecar in the path). The eval
    # LLM time is reported separately and never folded into this figure.
    compress_ms_b = [
        r["compress_ms"] for r in records_b if r.get("compress_ms") is not None
    ]
    eval_ms_b = [r["eval_ms"] for r in records_b if r.get("eval_ms") is not None]

    return {
        "delta_tokens": delta_tokens,
        "delta_accuracy": delta_accuracy,
        "e2e_ratio": e2e_ratio,
        "cr_savings": cr_savings,
        "cost_saved_per_1k": cost_saved_per_1k,
        "sidecar_p50_ms": _percentile(compress_ms_b, 0.50),
        "sidecar_p95_ms": _percentile(compress_ms_b, 0.95),
        "eval_p50_ms": _percentile(eval_ms_b, 0.50),
        "eval_p95_ms": _percentile(eval_ms_b, 0.95),
        "tokens_a": tokens_a,
        "tokens_b": tokens_b,
        "acc_a": acc_a,
        "acc_b": acc_b,
        "acc_closed_book": acc_closed_book,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Verbatim-excluded compression metrics (Phase B)
#
# The headline Layer-8 savings is diluted by content the classifier routes to
# Verbatim (code, errors, structured data). This second view reports the
# compression ratio on ONLY the content leanctx actually compressed — a number
# that is invariant to how much of the sample happens to be verbatim-routed.
#
# We replay leanctx's real eligibility filter + classifier on the Leg-A
# (CR-only) output, which — because CR's 7 layers are deterministic — equals
# the input leanctx saw inside Leg B. No leanctx core changes (that is Phase A).
# ═══════════════════════════════════════════════════════════════════════════

# Mirrors leanctx.server._default_config(): the request-level token gate below
# which the sidecar passes the whole request through verbatim. The harness
# spawns the sidecar with the same environment, so reading it here reproduces
# the value the sidecar actually used.
_SIDECAR_THRESHOLD_DEFAULT = 1500


def _sidecar_threshold() -> int:
    return int(os.environ.get("LEANCTX_SERVER_THRESHOLD", str(_SIDECAR_THRESHOLD_DEFAULT)))


def _verbatim_split_item(
    msgs_a: list[dict[str, Any]],
    msgs_b: list[dict[str, Any]],
    *,
    threshold: int,
) -> tuple[int, int, int, str] | None:
    """Bucket one item's Layer-8 input into verbatim vs compressed tokens.

    ``msgs_a`` is the CR-only output (== what leanctx saw inside Leg B), aligned
    message-for-message with ``msgs_b`` (the CR + leanctx output). Reuses
    leanctx's *real* eligibility roles and ``classify()`` so there is no copied
    routing logic to drift.

    Returns ``(verbatim_tokens, compressed_in, compressed_out, route)`` where
    ``route`` is ``"verbatim"`` | ``"lingua"`` | ``"hybrid"``, or ``None`` when
    the two legs cannot be aligned (different message counts → caller skips).
    """
    from leanctx.classifier import classify
    from leanctx.compressors import ContentType
    from leanctx.server import _COMPRESSIBLE_ROLES

    if len(msgs_a) != len(msgs_b):
        return None

    eligible = {
        i
        for i, m in enumerate(msgs_a)
        if m.get("role") in _COMPRESSIBLE_ROLES and isinstance(m.get("content"), str)
    }
    # Request-level gate: if the eligible subset is under the threshold the
    # whole request is passed through, so nothing is compressed.
    eligible_tokens = sum(_sum_tokens([msgs_a[i]]) for i in eligible)
    request_compressible = eligible_tokens >= threshold

    verbatim = comp_in = comp_out = 0
    n_compressed = 0
    for i, (ma, mb) in enumerate(zip(msgs_a, msgs_b, strict=True)):
        ta = _sum_tokens([ma])
        is_compressed = (
            request_compressible
            and i in eligible
            and classify(ma) is ContentType.PROSE
        )
        if is_compressed:
            comp_in += ta
            comp_out += _sum_tokens([mb])
            n_compressed += 1
        else:
            verbatim += ta

    if n_compressed == 0:
        route = "verbatim"
    elif n_compressed == len(msgs_a):
        route = "lingua"
    else:
        route = "hybrid"
    return verbatim, comp_in, comp_out, route


def verbatim_split_by_item(
    msgs_a_by_id: dict[str, list[dict[str, Any]]],
    msgs_b_by_id: dict[str, list[dict[str, Any]]],
    *,
    threshold: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Per-item verbatim/non-verbatim token split for every item present in
    both leg maps. Returns ``{item_id: {lx_* fields}}`` (items that cannot be
    aligned are omitted)."""
    if threshold is None:
        threshold = _sidecar_threshold()
    out: dict[str, dict[str, Any]] = {}
    for item_id in msgs_a_by_id:
        if item_id not in msgs_b_by_id:
            continue
        split = _verbatim_split_item(
            msgs_a_by_id[item_id], msgs_b_by_id[item_id], threshold=threshold
        )
        if split is None:
            continue
        v, c_in, c_out, route = split
        out[item_id] = {
            "lx_verbatim_tokens": v,
            "lx_compressed_in_tokens": c_in,
            "lx_compressed_out_tokens": c_out,
            "lx_route": route,
        }
    return out


def aggregate_verbatim_metrics(
    per_item: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Aggregate per-item splits into the figures the report needs. Returns
    ``None`` when there is nothing to analyse."""
    if not per_item:
        return None

    tot_v = sum(d["lx_verbatim_tokens"] for d in per_item.values())
    tot_in = sum(d["lx_compressed_in_tokens"] for d in per_item.values())
    tot_out = sum(d["lx_compressed_out_tokens"] for d in per_item.values())
    n = len(per_item)

    route_counts = {"verbatim": 0, "lingua": 0, "hybrid": 0}
    for d in per_item.values():
        route_counts[d["lx_route"]] = route_counts.get(d["lx_route"], 0) + 1

    layer8_in = tot_v + tot_in
    layer8_out = tot_v + tot_out
    verbatim_share = tot_v / layer8_in if layer8_in else 0.0
    nv_ratio = (tot_out / tot_in) if tot_in else None
    nv_savings = (1.0 - nv_ratio) if nv_ratio is not None else None
    overall_savings = (layer8_in - layer8_out) / layer8_in if layer8_in else 0.0

    return {
        "n_items": n,
        "verbatim_tokens": tot_v,
        "compressed_in_tokens": tot_in,
        "compressed_out_tokens": tot_out,
        "layer8_in_tokens": layer8_in,
        "layer8_out_tokens": layer8_out,
        "verbatim_share": verbatim_share,
        "nonverbatim_ratio": nv_ratio,
        "nonverbatim_savings": nv_savings,
        "overall_savings": overall_savings,
        "token_delta": tot_in - tot_out,
        "route_counts": route_counts,
        "avg_layer8_in": layer8_in / n,
        "avg_layer8_out": layer8_out / n,
        "avg_nv_in": tot_in / n,
        "avg_nv_out": tot_out / n,
    }


def write_by_item_dumps(
    out_dir: Path,
    records_a: list[dict[str, Any]],
    records_b: list[dict[str, Any]],
    records_cb: list[dict[str, Any]],
    msgs_a_by_id: dict[str, list[dict[str, Any]]],
    msgs_b_by_id: dict[str, list[dict[str, Any]]],
) -> list[Path]:
    """Write one deep-dive JSON file per benchmark item under ``out_dir``.

    Each file carries every field the flat JSONL holds for that item — the
    Leg A record (CR only), the Leg B record (CR + leanctx Layer 8, already
    enriched with the ``lx_*`` verbatim split), and the closed-book control
    when present — plus the **full message payloads** on either side of
    Layer 8 that the JSONL deliberately omits:

      * ``input_before_layer8``  — the CR-only output, i.e. exactly what
        leanctx saw as input inside Leg B (CR's 7 layers are deterministic).
      * ``output_after_layer8``  — the CR + leanctx Layer 8 output.

    Only items carrying an ``item_id`` are dumped (the agent warmup item has
    none and is not tracked in the message sinks). Returns the files written.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    def _index(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {r["item_id"]: r for r in records if r.get("item_id") is not None}

    a_by_id = _index(records_a)
    b_by_id = _index(records_b)
    cb_by_id = _index(records_cb)

    item_ids = (
        set(a_by_id) | set(b_by_id) | set(cb_by_id)
        | set(msgs_a_by_id) | set(msgs_b_by_id)
    )

    written: list[Path] = []
    for item_id in sorted(item_ids):
        rec_a = a_by_id.get(item_id)
        rec_b = b_by_id.get(item_id)
        dump = {
            "item_id": item_id,
            "workload": (rec_b or rec_a or {}).get("workload"),
            "leg_a": rec_a,
            "leg_b": rec_b,
            "leg_closed_book": cb_by_id.get(item_id),
            "input_before_layer8": msgs_a_by_id.get(item_id),
            "output_after_layer8": msgs_b_by_id.get(item_id),
        }
        # item_id is a dataset hash, but sanitize defensively for filesystem use.
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(item_id))
        path = out_dir / f"{safe}.json"
        path.write_text(json.dumps(dump, indent=2, ensure_ascii=False))
        written.append(path)
    return written


def apply_gate(
    metrics: dict[str, Any],
    *,
    savings_threshold: float = 0.20,
    accuracy_drop: float = 0.02,
) -> GateResult:
    if metrics["delta_tokens"] < savings_threshold:
        return GateResult(
            passed=False,
            fail_reason=(
                f"savings delta {metrics['delta_tokens']:.4f} below threshold {savings_threshold}"
            ),
        )
    if metrics["delta_accuracy"] < -accuracy_drop:
        return GateResult(
            passed=False,
            fail_reason=(
                f"accuracy dropped {-metrics['delta_accuracy']:.4f} exceeds allowed {accuracy_drop}"
            ),
        )
    return GateResult(passed=True)


def acc_by_route(
    records_a: list[dict[str, Any]],
    records_b: list[dict[str, Any]],
) -> list[tuple[str, int, float, float]]:
    """Accuracy split by leanctx route — ``verbatim`` / ``lingua`` / ``hybrid``
    plus an ``overall`` row — as ``(label, N, acc_a, acc_b)``.

    Only Leg B carries ``lx_route``; each item's route is joined back onto the
    Leg A record by ``item_id`` so both legs are compared on the *same*
    partition. This is the lens that separates real compression effects (the
    ``lingua`` row, where leanctx actually rewrote the context) from eval noise
    on untouched items (the ``verbatim`` row, which — with the shared eval draw —
    is pinned to Δ = 0 by construction). Items lacking a route or an accuracy on
    either leg are skipped.
    """
    route_by_id = {
        r.get("item_id"): r.get("lx_route")
        for r in records_b
        if r.get("item_id") is not None and r.get("lx_route")
    }
    a_by_id = {r.get("item_id"): r for r in records_a if r.get("item_id") is not None}
    b_by_id = {r.get("item_id"): r for r in records_b if r.get("item_id") is not None}

    buckets: dict[str, list[tuple[bool, bool]]] = {}
    overall: list[tuple[bool, bool]] = []
    for item_id, route in route_by_id.items():
        ra, rb = a_by_id.get(item_id), b_by_id.get(item_id)
        if ra is None or rb is None:
            continue
        if ra.get("accuracy") is None or rb.get("accuracy") is None:
            continue
        pair = (bool(ra["accuracy"]), bool(rb["accuracy"]))
        buckets.setdefault(route, []).append(pair)
        overall.append(pair)

    def _row(label: str, pairs: list[tuple[bool, bool]]) -> tuple[str, int, float, float]:
        n = len(pairs)
        acc_a = sum(a for a, _ in pairs) / n if n else 0.0
        acc_b = sum(b for _, b in pairs) / n if n else 0.0
        return (label, n, acc_a, acc_b)

    rows = [
        _row(route, buckets[route])
        for route in ("verbatim", "lingua", "hybrid")
        if route in buckets
    ]
    if overall:
        rows.append(_row("overall", overall))
    return rows


def format_report(
    metrics: dict[str, Any],
    gate: GateResult,
    records_a: list[dict[str, Any]] | None = None,
    records_b: list[dict[str, Any]] | None = None,
    *,
    run_date: str = "",
    eval_model: str = "",
    cr_commit: str = "",
    savings_threshold: float = 0.20,
    accuracy_drop: float = 0.02,
    verbatim_metrics: dict[str, Any] | None = None,
) -> str:
    """Generate a rich Markdown Phase 1 report.

    Falls back to a compact 7-line summary when records are not supplied
    (keeps Sprint 4 unit tests green).
    """
    verdict = "PASS" if gate.passed else "NO-GO"

    # ── compact fallback (used by unit tests) ────────────────────────────
    if records_a is None or records_b is None:
        lines = [
            f"## Phase 1 Report — {verdict}",
            "",
            f"delta_tokens      : {metrics.get('delta_tokens', 'N/A'):.4f}"
            if isinstance(metrics.get("delta_tokens"), float)
            else f"delta_tokens      : {metrics.get('delta_tokens', 'N/A')}",
            f"delta_accuracy    : {metrics.get('delta_accuracy', 'N/A'):.4f}"
            if isinstance(metrics.get("delta_accuracy"), float)
            else f"delta_accuracy    : {metrics.get('delta_accuracy', 'N/A')}",
            f"e2e_ratio         : {metrics.get('e2e_ratio', 'N/A'):.4f}"
            if isinstance(metrics.get("e2e_ratio"), float)
            else f"e2e_ratio         : {metrics.get('e2e_ratio', 'N/A')}",
            f"cr_savings        : {metrics.get('cr_savings', 'N/A'):.4f}"
            if isinstance(metrics.get("cr_savings"), float)
            else f"cr_savings        : {metrics.get('cr_savings', 'N/A')}",
            f"cost_saved_per_1k : {metrics.get('cost_saved_per_1k', 'N/A'):.4f}"
            if isinstance(metrics.get("cost_saved_per_1k"), float)
            else f"cost_saved_per_1k : {metrics.get('cost_saved_per_1k', 'N/A')}",
            f"sidecar_p50_ms    : {metrics.get('sidecar_p50_ms', 'N/A')}",
        ]
        if not gate.passed:
            lines.append(f"\nFail reason: {gate.fail_reason}")
        return "\n".join(lines)

    # ── rich report ──────────────────────────────────────────────────────
    import datetime

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    date_str = run_date or now_utc.strftime("%Y-%m-%d %H:%M UTC")
    verdict_icon = "✅" if gate.passed else "❌"

    def pct(v: float | None) -> str:
        return f"{v * 100:.1f}%" if v is not None else "N/A"

    def fmt_f(v: float | None, decimals: int = 4) -> str:
        return f"{v:.{decimals}f}" if v is not None else "N/A"

    lb_a = [r for r in records_a if r.get("accuracy") is not None]
    lb_b = [r for r in records_b if r.get("accuracy") is not None]

    n_lb = len(lb_a)
    acc_a = metrics.get("acc_a", 0.0)
    acc_b = metrics.get("acc_b", 0.0)
    delta_acc = metrics.get("delta_accuracy", 0.0)

    tokens_raw_avg = sum(r["tokens_raw"] for r in records_a) / max(len(records_a), 1)
    tokens_a_avg = metrics.get("tokens_a", 0) / max(len(records_a), 1)
    tokens_b_avg = metrics.get("tokens_b", 0) / max(len(records_b), 1)

    # go/no-go row helpers
    def gate_row(label: str, threshold: str, actual: str, passed_cond: bool) -> str:
        return f"| {label} | {threshold} | {actual} | {'✅ PASS' if passed_cond else '❌ FAIL'} |"
    savings_ok = metrics.get("delta_tokens", 0.0) >= savings_threshold
    accuracy_ok = delta_acc >= -accuracy_drop

    # CR layer stats (average across all records_a)
    def _avg_stat(key: str) -> float:
        vals = [r.get("cr_stats", {}).get(key, 0) for r in records_a if r.get("cr_stats")]
        return sum(vals) / len(vals) if vals else 0.0

    cr_layers = [
        ("L1 Dedup",      "duplicatesRemoved",       _avg_stat("duplicatesRemoved")),
        ("L2 Whitespace", "whitespaceSavedChars",     _avg_stat("whitespaceSavedChars")),
        ("L3 Dictionary", "dictionarySubstitutions",  _avg_stat("dictionarySubstitutions")),
        ("L4 Paths",      "pathsShortened",           _avg_stat("pathsShortened")),
        ("L5 JSON",       "jsonCompactedChars",       _avg_stat("jsonCompactedChars")),
        ("L6 Observation","observationsCompressed",   _avg_stat("observationsCompressed")),
        ("L7 Codebook",   "dynamicSubstitutions",     _avg_stat("dynamicSubstitutions")),
    ]

    # accuracy breakdown helpers
    def _acc_breakdown(records: list[dict], key: str) -> dict[str, tuple[int, float]]:
        cells: dict[str, list[bool]] = {}
        for r in records:
            val = r.get(key, "") or "unknown"
            acc = r.get("accuracy")
            if acc is not None:
                cells.setdefault(val, []).append(bool(acc))
        return {k: (len(v), sum(v) / len(v)) for k, v in sorted(cells.items())}

    def _acc_table(breakdown_a: dict, breakdown_b: dict) -> list[str]:
        keys = sorted(set(breakdown_a) | set(breakdown_b))
        rows = ["| | N | Leg A | Leg B | Δ |", "|---|---|---|---|---|"]
        for k in keys:
            n, a = breakdown_a.get(k, (0, 0.0))
            _, b = breakdown_b.get(k, (0, 0.0))
            rows.append(f"| {k} | {n} | {pct(a)} | {pct(b)} | {pct(b-a)} |")
        return rows

    def _ms(v: float | None) -> str:
        return f"{v:,.0f} ms" if v is not None else "N/A"

    p50_str = _ms(metrics.get("sidecar_p50_ms"))
    p95_str = _ms(metrics.get("sidecar_p95_ms"))
    eval_p50_str = _ms(metrics.get("eval_p50_ms"))
    eval_p95_str = _ms(metrics.get("eval_p95_ms"))

    # ── build report ─────────────────────────────────────────────────────
    out: list[str] = []
    A = out.append

    A("# ClawRouter × leanctx — Phase 1 Benchmark Report")
    A("")
    A(f"**Verdict: {verdict_icon} {verdict}**  ·  "
      f"Date: {date_str}  ·  "
      f"LB N={n_lb}  ·  "
      + (f"Eval: {eval_model}  ·  " if eval_model else "")
      + (f"CR commit: `{cr_commit[:8]}`" if cr_commit else ""))
    A("")
    A("---")
    A("")

    # 1. Go/No-Go gate
    A("## 1. Go / No-Go Gate")
    A("")
    A("| Condition | Threshold | Actual | Result |")
    A("|-----------|-----------|--------|--------|")
    A(gate_row(
        "Δ tokens — extra savings from Layer 8",
        f"≥ {savings_threshold * 100:.0f}%",
        pct(metrics.get("delta_tokens")),
        savings_ok,
    ))
    A(gate_row(
        "Δ accuracy — LongBench quality",
        f"≥ −{accuracy_drop * 100:.0f}%",
        pct(delta_acc),
        accuracy_ok,
    ))
    A("")
    if not gate.passed:
        A(f"> ⚠️  **Fail reason:** {gate.fail_reason}")
        A("")

    # 2. Token compression
    A("## 2. Token Compression")
    A("")
    A("| Stage | Avg tokens / request | vs raw | vs Leg A |")
    A("|-------|---------------------|--------|----------|")
    A(f"| Raw (uncompressed) | {tokens_raw_avg:,.0f} | — | — |")
    A(f"| **Leg A** — CR 7 layers | {tokens_a_avg:,.0f} | "
      f"−{pct(metrics.get('cr_savings'))} | — |")
    A(f"| **Leg B** — CR + leanctx Layer 8 | {tokens_b_avg:,.0f} | "
      f"−{pct(1 - metrics.get('e2e_ratio', 1.0))} | "
      f"−{pct(metrics.get('delta_tokens'))} |")
    A("")

    # 2b. Compression excluding verbatim content
    if verbatim_metrics is not None:
        vm = verbatim_metrics
        rc = vm["route_counts"]
        n = vm["n_items"]
        overall_ratio = (
            vm["layer8_out_tokens"] / vm["layer8_in_tokens"]
            if vm["layer8_in_tokens"]
            else None
        )
        A("## 2b. Compression Excluding Verbatim Content")
        A("")
        A("_Layer 8 routes code, errors, and structured content to **Verbatim "
          "(0%)**. This view reports compression on only the content actually "
          "sent to the compressor — a figure invariant to how much of the sample "
          "is verbatim-routed (see `oversampling_analysis.html`)._")
        A("")
        A(f"**Verbatim share:** {pct(vm['verbatim_share'])} of Layer-8 input "
          f"tokens were preserved verbatim. Routing mix over {n} items: "
          f"{rc.get('lingua', 0)} compressed · {rc.get('verbatim', 0)} verbatim · "
          f"{rc.get('hybrid', 0)} mixed.")
        A("")
        A("| View | Avg input / req | Avg output / req | Ratio | Savings | Δ tokens / req |")
        A("|------|-----------------|------------------|-------|---------|----------------|")
        A(f"| Overall (incl. verbatim) | {vm['avg_layer8_in']:,.0f} | "
          f"{vm['avg_layer8_out']:,.0f} | {fmt_f(overall_ratio, 3)} | "
          f"{pct(vm['overall_savings'])} | {vm['token_delta'] / n:,.0f} |")
        A(f"| **Non-verbatim only** | {vm['avg_nv_in']:,.0f} | "
          f"{vm['avg_nv_out']:,.0f} | {fmt_f(vm['nonverbatim_ratio'], 3)} | "
          f"{pct(vm['nonverbatim_savings'])} | {vm['token_delta'] / n:,.0f} |")
        A("")
        A(f"_Decomposition: overall {pct(vm['overall_savings'])} ≈ "
          f"(1 − {pct(vm['verbatim_share'])}) × non-verbatim "
          f"{pct(vm['nonverbatim_savings'])}. The absolute token delta is "
          f"identical in both views — verbatim content contributes 0._")
        A("")

    # 3. LongBench accuracy
    A("## 3. LongBench v2 Accuracy")
    A("")
    if n_lb > 0:
        A("| | N | Leg A (CR only) | Leg B (CR + leanctx) | Δ |")
        A("|---|---|---|---|---|")
        A(f"| **Overall** | {n_lb} | {pct(acc_a)} | {pct(acc_b)} | {pct(delta_acc)} |")
        A("")
        acc_cb = metrics.get("acc_closed_book")
        if acc_cb is not None:
            lift_a = acc_a - acc_cb
            lift_b = acc_b - acc_cb
            A("**Closed-book control** (no document — answers from priors only): "
              f"{pct(acc_cb)}. Context lift: Leg A {pct(lift_a)}, Leg B {pct(lift_b)}.")
            if lift_b <= 0:
                A("")
                A("> ⚠️  Leg B accuracy does not beat the closed-book baseline — "
                  "the compressed context may not be carrying the answer. Treat the "
                  "accuracy result with caution.")
            A("")
        route_rows = acc_by_route(lb_a, lb_b)
        if route_rows:
            A("### By leanctx route")
            A("")
            A("_`verbatim` = leanctx passed the context through unchanged; "
              "`lingua` = leanctx actually compressed it. With the shared eval "
              "draw, identical-input (verbatim) items score identically on both "
              "legs, so any non-zero Δ here lives entirely in `lingua`._")
            A("")
            A("| route | N | Leg A | Leg B | Δ |")
            A("|---|---|---|---|---|")
            for label, n, ra, rb in route_rows:
                disp = f"**{label}**" if label == "overall" else label
                A(f"| {disp} | {n} | {pct(ra)} | {pct(rb)} | {pct(rb - ra)} |")
            A("")
        diff_bd = _acc_breakdown(lb_a, "lb_difficulty")
        diff_bd_b = _acc_breakdown(lb_b, "lb_difficulty")
        if diff_bd:
            A("### By difficulty")
            A("")
            out.extend(_acc_table(diff_bd, diff_bd_b))
            A("")
        len_bd = _acc_breakdown(lb_a, "lb_length")
        len_bd_b = _acc_breakdown(lb_b, "lb_length")
        if len_bd:
            A("### By length")
            A("")
            out.extend(_acc_table(len_bd, len_bd_b))
            A("")
        dom_bd = _acc_breakdown(lb_a, "lb_domain")
        dom_bd_b = _acc_breakdown(lb_b, "lb_domain")
        if dom_bd:
            A("### By domain")
            A("")
            out.extend(_acc_table(dom_bd, dom_bd_b))
            A("")
    else:
        A("_No LongBench items in this run._")
        A("")

    # 4. Per-workload breakdown
    A("## 4. Per-Workload Breakdown")
    A("")
    workloads = sorted({r.get("workload", "unknown") for r in records_a})
    A("| Workload | Items | tokens_A | tokens_B | Δ tokens |"
      + (" Acc A | Acc B | Δ acc |" if n_lb > 0 else ""))
    A("|----------|-------|----------|----------|----------|"
      + ("-------|-------|-------|" if n_lb > 0 else ""))
    for wl in workloads:
        wa = [r for r in records_a if r.get("workload") == wl]
        wb = [r for r in records_b if r.get("workload") == wl]
        if not wa or not wb:
            continue
        ta = sum(r["tokens_compressed"] for r in wa)
        tb = sum(r["tokens_compressed"] for r in wb)
        dt = pct((ta - tb) / ta) if ta > 0 else "N/A"
        row = f"| {wl} | {len(wa)} | {ta:,} | {tb:,} | {dt} |"
        if n_lb > 0:
            wa_lb = [r for r in wa if r.get("accuracy") is not None]
            wb_lb = [r for r in wb if r.get("accuracy") is not None]
            aa = sum(r["accuracy"] for r in wa_lb) / len(wa_lb) if wa_lb else None
            ab = sum(r["accuracy"] for r in wb_lb) / len(wb_lb) if wb_lb else None
            row += (f" {pct(aa)} | {pct(ab)} | {pct((ab or 0) - (aa or 0))} |"
                    if aa is not None else " — | — | — |")
        A(row)
    A("")

    # 5. CR layer contribution (Leg A)
    A("## 5. ClawRouter Layer Contributions (Leg A avg)")
    A("")
    A("| Layer | Statistic | Avg value |")
    A("|-------|-----------|-----------|")
    for name, stat, val in cr_layers:
        A(f"| {name} | {stat} | {val:,.1f} |")
    layer8_delta = (
        (metrics.get("tokens_a", 0) - metrics.get("tokens_b", 0)) / max(len(records_a), 1)
    )
    A(f"| **L8 leanctx** | Δ tokens / item | **{layer8_delta:,.0f}** |")
    A("")

    # 6. Latency
    A("## 6. Latency")
    A("")
    A("_Sidecar = the Layer 8 compression call in isolation (Leg B). Eval LLM "
      "time is shown separately and is **not** part of the sidecar figure._")
    A("")
    A("| Metric | P50 | P95 |")
    A("|--------|-----|-----|")
    A(f"| Sidecar (Layer 8 compression) | {p50_str} | {p95_str} |")
    A(f"| Eval LLM (LongBench answer) | {eval_p50_str} | {eval_p95_str} |")
    A("")

    # 7. Cost analysis
    A("## 7. Cost Analysis")
    A("")
    cost = metrics.get("cost_saved_per_1k", 0.0)
    delta_t = metrics.get("delta_tokens", 0.0)
    A("Assuming Claude Sonnet input pricing ($15 / 1M tokens):")
    A("")
    A("```")
    A(f"Δ tokens      = {pct(delta_t)} of avg {tokens_raw_avg:,.0f} raw tokens")
    A(f"Savings / req = {delta_t * tokens_raw_avg:,.0f} tokens")
    A(f"Cost saved    = {delta_t * tokens_raw_avg * 15 / 1e6:.4f} USD / request")
    A(f"              = ${cost:,.2f} per 1 000 requests")
    A("```")
    A("")
    A("---")
    A("")
    short_commit = cr_commit[:8] if cr_commit else "unknown"
    A(f"_Generated by `bench_phase1.py` · CR commit `{short_commit}`_")

    return "\n".join(out)


# ═══════════════════════════════════════════════════════════════════════════
# Sprint 5 — Service lifecycle
# ═══════════════════════════════════════════════════════════════════════════


def health_check(url: str) -> bool:
    try:
        r = httpx.get(f"{url}/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def wait_for_health(
    proc: subprocess.Popen | None,
    url: str,
    timeout: float = 30,
) -> None:
    deadline = time.time() + timeout
    while not health_check(url):
        if time.time() > deadline:
            raise TimeoutError(f"{url} did not become healthy within {timeout}s")
        time.sleep(1)


def spawn_cr_shim(
    workdir: Path,
    port: int,
    extra_env: dict[str, str],
) -> subprocess.Popen:
    env = {**os.environ, "CR_SHIM_PORT": str(port), **extra_env}
    proc = subprocess.Popen(["node", str(workdir / "cr_shim.mjs")], env=env)
    wait_for_health(proc, f"http://127.0.0.1:{port}")
    return proc


def spawn_leanctx_sidecar(
    url: str,
    lingua_ratio: float,
    device: str = "auto",
) -> subprocess.Popen | None:
    if health_check(url):
        return None  # already running
    import sys as _sys

    # Resolve leanctx-serve from the same venv as the running interpreter.
    venv_bin = str(Path(_sys.executable).parent)
    # Pass the device through explicitly. An empty value lets the server
    # auto-detect; anything else (cuda / cpu / mps) is honored verbatim. The
    # server logs the *resolved* device at model load so the choice is
    # provable from logs, not assumed.
    device_env = "" if device == "auto" else device
    env = {
        **os.environ,
        "PATH": f"{venv_bin}:{os.environ.get('PATH', '')}",
        "LEANCTX_SERVER_LINGUA_RATIO": str(lingua_ratio),
        "LEANCTX_SERVER_LINGUA_DEVICE": device_env,
    }
    print(f"[sidecar] LEANCTX_SERVER_LINGUA_DEVICE={device_env or '(auto)'}")
    proc = subprocess.Popen(["leanctx-serve"], env=env)
    # 120 s: includes one-time LLMLingua-2 weight download + warmup pass.
    wait_for_health(proc, url, timeout=120)
    return proc


# ═══════════════════════════════════════════════════════════════════════════
# Sprint 6 — CR shim integration helpers
# ═══════════════════════════════════════════════════════════════════════════


def install_connector(cr_dir: Path) -> None:
    """Build the productized Layer-8 connector and install it into the CR clone.

    The Layer-8 hook now lives in ``integrations/clawrouter/connector`` as
    ``@leanctx/clawrouter-connector``; the benchmark no longer carries its own
    copy. We build it (``tsc`` → ``dist``) and ``npm install`` it into the cloned
    CR so tsup can resolve the ``leanctxLayer8`` import when bundling Layer 8.
    """
    if not _CONNECTOR_DIR.exists():
        raise RuntimeError(
            f"Layer-8 connector not found at {_CONNECTOR_DIR}. "
            "Expected integrations/clawrouter/connector in the repo."
        )
    subprocess.run(["npm", "ci"], cwd=_CONNECTOR_DIR, check=True)
    subprocess.run(["npm", "run", "build"], cwd=_CONNECTOR_DIR, check=True)
    subprocess.run(["npm", "install", str(_CONNECTOR_DIR)], cwd=cr_dir, check=True)
    print(f"[connector] built + installed {_CONNECTOR_PKG} from {_CONNECTOR_DIR}")


def setup_clawrouter(workdir: Path, commit: str = CR_DEFAULT_COMMIT) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    if not (workdir / ".git").exists():
        subprocess.run(["git", "clone", CR_REPO_URL, str(workdir)], check=True)
    subprocess.run(["git", "checkout", commit], cwd=workdir, check=True)
    subprocess.run(["npm", "ci"], cwd=workdir, check=True)
    install_connector(workdir)
    apply_patch(workdir)
    subprocess.run(["npm", "run", "build"], cwd=workdir, check=True)


def compress_via_shim(messages: list[dict], shim_url: str) -> dict:
    # 120 s: allows for Layer 8 Lingua inference on CPU (~38 s for agent_extended).
    r = httpx.post(shim_url, json={"messages": messages}, timeout=120)
    r.raise_for_status()
    return r.json()


# ═══════════════════════════════════════════════════════════════════════════
# Sprint 7 — Leg runner & CLI
# ═══════════════════════════════════════════════════════════════════════════

# LongBench v2 prompt template (mirrors THUDM/LongBench pred.py)
_LB_PROMPT_TEMPLATE = (
    "Please read the following text and answer the question below.\n\n"
    "<text>\n$DOC$\n</text>\n\n"
    "What is the correct answer to this question: $Q$\n"
    "Choices:\n"
    "(A) $C_A$\n(B) $C_B$\n(C) $C_C$\n(D) $C_D$\n\n"
    'Format your response as follows: "The correct answer is (insert answer here)".'
)
_LB_ANS_RE_PAREN = re.compile(r"The correct answer is \(([A-D])\)")
_LB_ANS_RE_BARE = re.compile(r"The correct answer is ([A-D])")

# Closed-book forced-choice rider. Without the document, the eval model tends to
# hedge ("I can't answer without the text"), which the extraction regex scores
# as a miss — dragging the control *below* the 25% random-chance floor for a
# 4-way MC and thereby inflating the apparent context lift. Requiring a single
# best-guess letter makes the control measure the model's actual prior instead
# of its willingness to abstain, so the lift over it is honest.
_LB_FORCE_CHOICE = (
    "\n\nYou do not have the source text, but you must still answer. Pick the "
    "single most likely option from your own knowledge and best guess — never "
    "reply that you cannot answer or need the document. Respond with exactly "
    'one letter in the required format: "The correct answer is (X)".'
)


def _build_lb_prompt(item: dict, context: str, *, closed_book: bool = False) -> str:
    """Render the LongBench MC prompt for an item against a context string.

    In the closed-book control the ``$DOC$`` slot is a placeholder, so we append
    a forced-choice rider (see ``_LB_FORCE_CHOICE``) that stops the model from
    hedging its way below the random-chance floor.
    """
    prompt = (
        _LB_PROMPT_TEMPLATE
        .replace("$DOC$", context)
        .replace("$Q$", item.get("question", ""))
        .replace("$C_A$", item.get("choice_A", ""))
        .replace("$C_B$", item.get("choice_B", ""))
        .replace("$C_C$", item.get("choice_C", ""))
        .replace("$C_D$", item.get("choice_D", ""))
    )
    if closed_book:
        prompt += _LB_FORCE_CHOICE
    return prompt


def _extract_lb_answer(response: str) -> str | None:
    m = _LB_ANS_RE_PAREN.search(response.replace("*", ""))
    if not m:
        m = _LB_ANS_RE_BARE.search(response)
    return m.group(1) if m else None


def _lb_head_tail(text: str, max_chars: int = 112_000) -> str:
    """Truncate to ~28K tokens (112K chars ≈ 4 chars/token) to fit 30K tok/min rate limit."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n...[truncated]...\n" + text[-half:]


def _load_lb_items(
    limit: int = 5,
    workload_tag: str = "lb_s1",
    *,
    seed: int = 1234,
    oversample_long: bool = True,
) -> list[dict[str, Any]]:
    """Load and format LongBench v2 items for run_leg().

    Sampling is stratified across (length × difficulty) cells but **random
    within each cell** (seeded for reproducibility) — the previous
    ``cell_items[:per_cell]`` took the first-N of each cell, which produced
    the suspiciously balanced buckets the reviewer called out.

    With ``oversample_long`` the ``long`` length category is given 1.5×
    weight, since that is where Layer 8 showed a real (−10%) accuracy risk at
    small N and needs more samples before any safety claim.
    """
    import random as _random

    from datasets import load_dataset

    ds = load_dataset("THUDM/LongBench-v2", split="train")
    items: list[dict] = list(ds)

    if limit > 0 and len(items) > limit:
        rng = _random.Random(seed)
        cells: dict[tuple[str, str], list[dict]] = {}
        for it in items:
            key = (it.get("length", ""), it.get("difficulty", ""))
            cells.setdefault(key, []).append(it)

        # Weight cells; long-context cells get 1.5× weight when oversampling.
        weights = {
            key: (1.5 if oversample_long and key[0] == "long" else 1.0)
            for key in cells
        }
        total_weight = sum(weights.values())

        sample: list[dict] = []
        for key, cell_items in cells.items():
            quota = max(1, round(limit * weights[key] / total_weight))
            quota = min(quota, len(cell_items))
            sample.extend(rng.sample(cell_items, quota))

        # Trim/pad to exactly `limit` from the remaining pool, randomly.
        rng.shuffle(sample)
        if len(sample) > limit:
            sample = sample[:limit]
        elif len(sample) < limit:
            chosen = {id(x) for x in sample}
            remaining = [it for it in items if id(it) not in chosen]
            rng.shuffle(remaining)
            sample.extend(remaining[: limit - len(sample)])
        items = sample

    result = []
    for i, it in enumerate(items):
        context = _lb_head_tail(it["context"])
        result.append({
            "messages": [{"role": "user", "content": context}],
            "workload": workload_tag,
            "item_id": it.get("_id", f"lb_{i:04d}"),
            "question": it["question"],
            "choice_A": it["choice_A"],
            "choice_B": it["choice_B"],
            "choice_C": it["choice_C"],
            "choice_D": it["choice_D"],
            "gold": it["answer"],
            # metadata threaded into records for accuracy breakdown tables
            "lb_domain": it.get("domain", ""),
            "lb_difficulty": it.get("difficulty", ""),
            "lb_length": it.get("length", ""),
        })
    return result


def _sum_tokens(messages: list[dict]) -> int:
    from leanctx.tokens import count_tokens

    combined = " ".join(
        m["content"] if isinstance(m.get("content"), str) else ""
        for m in messages
    )
    return count_tokens(combined)


def _build_eval_context(
    compressed_messages: list[dict],
    *,
    closed_book: bool = False,
) -> str:
    """Assemble the ``$DOC$`` context string the eval prompt is built from.

    Factored out of ``call_eval_llm`` so ``run_leg`` can compute the *same*
    string as a cache key for the shared-eval-draw optimisation: two legs whose
    compressed messages yield an identical context are guaranteed to send an
    identical prompt to the judge, so the second leg can reuse the first leg's
    answer instead of drawing a fresh (and, at temperature > 0, noisy) sample.
    """
    if closed_book:
        return "[no document provided]"
    return _lb_head_tail(
        " ".join(
            m["content"]
            for m in compressed_messages
            if isinstance(m.get("content"), str)
            and m.get("role") in ("user", "system", "assistant")
        ).strip()
    )


# Eval decoding temperature. LongBench scoring is a single-letter multiple-choice
# task; we want the judge as close to deterministic as practical so that the
# per-item accuracy signal reflects the *context* (and thus compression), not
# decoder sampling. 0.1 (rather than 0.0) keeps a hair of slack for providers
# that treat exactly-0 specially, while collapsing the ~20% identical-input flip
# rate observed at the API default of 1.0.
_EVAL_TEMPERATURE = 0.1


def call_eval_llm(
    compressed_messages: list[dict],
    item: dict,
    eval_cfg: dict,
    *,
    closed_book: bool = False,
) -> tuple[str | None, int]:
    """Build an LB prompt from compressed messages + item; call the eval LLM.

    When ``closed_book`` is True the document context is dropped entirely and
    the model must answer from priors alone — the control leg that rules out
    the null hypothesis "the context was irrelevant" (see reviewer feedback).

    Returns (predicted_letter | None, input_tokens).
    """
    context = _build_eval_context(compressed_messages, closed_book=closed_book)
    prompt = _build_lb_prompt(item, context, closed_book=closed_book)

    provider = eval_cfg.get("provider", "anthropic")
    model = eval_cfg.get("model", "claude-sonnet-4-6")
    max_tokens = int(eval_cfg.get("max_tokens", 64))

    max_retries = 8
    for attempt in range(max_retries):
        try:
            if provider == "anthropic":
                import anthropic

                client = anthropic.Anthropic()
                resp = client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=_EVAL_TEMPERATURE,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = resp.content[0].text if resp.content else ""
                in_tok = int(resp.usage.input_tokens)
            elif provider == "openai":
                import openai

                client = openai.OpenAI()
                resp = client.chat.completions.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=_EVAL_TEMPERATURE,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = resp.choices[0].message.content or ""
                in_tok = int(resp.usage.prompt_tokens) if resp.usage else 0
            else:
                raise ValueError(f"Unsupported eval provider: {provider!r}")
            break  # success
        except Exception as exc:
            is_rate_limit = "rate_limit" in type(exc).__name__.lower() or "429" in str(exc)
            if is_rate_limit and attempt < max_retries - 1:
                wait = min(2 ** attempt * 15, 120)  # 15s, 30s, 60s, 120s…
                print(f"  [rate-limit] waiting {wait}s before retry {attempt + 2}/{max_retries}")
                time.sleep(wait)
            else:
                raise

    return _extract_lb_answer(text), in_tok


def run_leg(
    leg: str,
    items: list[dict[str, Any]],
    shim_url: str,
    lb_cfg: dict | None,
    *,
    closed_book: bool = False,
    msg_sink: dict[str, list[dict[str, Any]]] | None = None,
    reuse_eval: dict[str, dict[str, Any]] | None = None,
    eval_sink: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Run one benchmark leg.

    Shared eval draw (de-biasing): when ``reuse_eval`` is supplied (the prior
    leg's ``eval_sink``) and this item's eval context is byte-identical to the
    context the prior leg scored, the prior leg's answer is reused verbatim
    instead of issuing a fresh judge call. This makes verbatim-routed items —
    where leanctx changed nothing — contribute Δacc = 0 by construction, instead
    of injecting decoder noise. ``eval_sink`` records this leg's (context,
    answer, tokens) per item so a later leg can reuse them.
    """
    records = []
    for item in items:
        t0 = time.perf_counter()
        messages = item["messages"]
        tokens_raw = _sum_tokens(messages)
        if closed_book:
            # The control leg drops the document entirely, so there is nothing
            # to compress and no shim is needed — skip it to avoid depending on
            # a live sidecar/shim and to keep the leg cheap.
            compressed = messages
            compress_ms = 0
            shim_result = {}
        else:
            # Time the compression sidecar call in isolation. The reviewer
            # flagged that an end-to-end duration_ms wraps the eval LLM (and its
            # retries), so it cannot stand in for Layer 8 latency — compress_ms
            # is the real sidecar cost, eval_ms is reported separately and
            # never conflated.
            t_compress0 = time.perf_counter()
            shim_result = compress_via_shim(messages, shim_url)
            compress_ms = int((time.perf_counter() - t_compress0) * 1000)
            compressed = shim_result["messages"]
        tokens_compressed = _sum_tokens(compressed)
        # Retain the compressed messages for the post-run verbatim split
        # (Phase B). Keyed by item_id; agent items without an id are skipped.
        item_id = item.get("item_id")
        if msg_sink is not None and item_id is not None:
            msg_sink[item_id] = compressed
        rec: dict[str, Any] = {
            "leg": leg,
            "workload": item.get("workload", "unknown"),
            "item_id": item.get("item_id"),
            "tokens_raw": tokens_raw,
            "tokens_compressed": tokens_compressed,
            "cr_compression_ratio": shim_result.get("compressionRatio"),
            "cr_stats": shim_result.get("stats"),
            "compress_ms": compress_ms,
            "closed_book": closed_book,
            "accuracy": None,
        }
        if lb_cfg and item.get("question"):
            context_key = _build_eval_context(compressed, closed_book=closed_book)
            prev = (
                reuse_eval.get(item_id)
                if reuse_eval is not None and item_id is not None
                else None
            )
            if prev is not None and prev.get("context") == context_key:
                # Identical prompt to the prior leg → reuse its draw (no judge
                # call, no decoder noise). eval_ms is 0 because nothing ran.
                answer, in_tok = prev["answer"], prev["in_tok"]
                rec["eval_ms"] = 0
                rec["eval_reused"] = True
            else:
                t_eval0 = time.perf_counter()
                answer, in_tok = call_eval_llm(
                    compressed, item, lb_cfg, closed_book=closed_book
                )
                rec["eval_ms"] = int((time.perf_counter() - t_eval0) * 1000)
                rec["eval_reused"] = False
            rec["accuracy"] = answer == item.get("gold")
            rec["lb_gold"] = item.get("gold")
            rec["lb_pred"] = answer
            rec["eval_input_tokens"] = in_tok
            rec["lb_domain"] = item.get("lb_domain", "")
            rec["lb_difficulty"] = item.get("lb_difficulty", "")
            rec["lb_length"] = item.get("lb_length", "")
            if eval_sink is not None and item_id is not None:
                eval_sink[item_id] = {
                    "context": context_key,
                    "answer": answer,
                    "in_tok": in_tok,
                }
        rec["duration_ms"] = int((time.perf_counter() - t0) * 1000)
        records.append(rec)
    return records


# ═══════════════════════════════════════════════════════════════════════════
# CLI entry point (Sprint 8)
# ═══════════════════════════════════════════════════════════════════════════


def main(argv: list[str] | None = None) -> int:
    """Run the Phase 1 harness. Returns 0 (PASS) or 1 (NO-GO)."""
    import argparse

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description="ClawRouter × leanctx Phase 1 harness")
    parser.add_argument("--cr-commit", default=CR_DEFAULT_COMMIT)
    parser.add_argument("--workdir", type=Path, default=Path("/tmp/clawrouter_bench"))
    parser.add_argument("--skip-setup", action="store_true")
    parser.add_argument("--dry-run-patch", action="store_true")
    parser.add_argument("--agent-stages", type=int, choices=[1, 2], default=1)
    parser.add_argument("--lb-stages", type=int, choices=[1, 2], default=1)
    parser.add_argument("--lb-n", type=int, default=5,
                        help="LongBench questions per stage (default: 5 for stage 1, 60 for full)")
    parser.add_argument("--fail-fast", action="store_true", default=True)
    parser.add_argument("--no-fail-fast", dest="fail_fast", action="store_false")
    parser.add_argument("--sidecar-url", default="http://127.0.0.1:8459")
    parser.add_argument("--shim-port", type=int, default=8461)
    parser.add_argument("--lingua-ratio", type=float, default=0.5)
    parser.add_argument("--lingua-device",
                        default=os.environ.get("LEANCTX_SERVER_LINGUA_DEVICE", "auto"),
                        help="Device for Layer 8 Lingua: auto|cuda|cpu|mps. "
                             "Passed to the sidecar; resolved device is logged.")
    parser.add_argument("--eval-provider",
                        default=os.environ.get("BENCHMARK_EVAL_PROVIDER", "anthropic"))
    parser.add_argument("--eval-model",
                        default=os.environ.get("BENCHMARK_EVAL_MODEL", "claude-sonnet-4-6"))
    parser.add_argument("--savings-threshold", type=float, default=0.20)
    parser.add_argument("--accuracy-drop", type=float, default=0.02)
    parser.add_argument("--sample-seed", type=int, default=1234,
                        help="Seed for random LongBench sampling (reproducibility).")
    parser.add_argument("--no-oversample-long", dest="oversample_long",
                        action="store_false", default=True,
                        help="Disable double-weighting of long-context LB items.")
    parser.add_argument("--closed-book", dest="closed_book", action="store_true",
                        default=True,
                        help="Run the no-context control leg (default: on).")
    parser.add_argument("--no-closed-book", dest="closed_book", action="store_false")
    parser.add_argument("--out", type=Path, default=Path("./phase1_results.jsonl"))
    parser.add_argument("--report", type=Path, default=Path("./phase1_report.md"))
    parser.add_argument(
        "--by-item-dir", type=Path, default=None,
        help="Directory for per-item deep-dive dumps (full input before / output "
             "after Layer 8 + all JSONL fields). Default: <out dir>/by_item.",
    )
    args = parser.parse_args(argv)

    if args.dry_run_patch:
        print(_LAYER8_IMPORT + LAYER8_CALL_BLOCK)
        return 0

    # Token-count validation. Savings figures derive from leanctx.tokens, which
    # silently falls back to a len(text)//4 char estimate when tiktoken is
    # absent — which would make the savings a character ratio, not a token
    # ratio. Hard-fail here so the reported number is always a real token count.
    from leanctx.tokens import _load_tiktoken

    if _load_tiktoken() is None:
        print(
            "[fatal] tiktoken is not installed — token counts would be a "
            "char/4 estimate, not real tokens. Install with: "
            "pip install 'leanctx[tokens]'",
            flush=True,
        )
        return 2
    print("[tokens] tiktoken present — token counts are exact (cl100k_base).")

    workdir = args.workdir
    sidecar_url = args.sidecar_url
    eval_cfg: dict[str, Any] = {
        "provider": args.eval_provider,
        "model": args.eval_model,
        "max_tokens": 1024,
    }

    # ── Phase A: Setup ────────────────────────────────────────────────────
    already_built = (workdir / "dist" / "compression" / "index.js").exists()
    if not args.skip_setup and not already_built:
        print("[setup] Cloning + building ClawRouter…")
        setup_clawrouter(workdir, commit=args.cr_commit)
    write_shim_file(workdir)

    # ── Phase B: Spawn sidecar ────────────────────────────────────────────
    print(f"[sidecar] Checking {sidecar_url} …")
    sidecar_proc = spawn_leanctx_sidecar(
        sidecar_url, lingua_ratio=args.lingua_ratio, device=args.lingua_device
    )

    # ── Phase C: Leg A (CR only) ──────────────────────────────────────────
    print("[leg A] Starting CR shim without sidecar …")
    shim_a = spawn_cr_shim(workdir, port=args.shim_port, extra_env={})
    shim_a_url = f"http://127.0.0.1:{args.shim_port}"

    # Compressed-message sinks for the verbatim split (Phase B).
    msgs_a_by_id: dict[str, list[dict[str, Any]]] = {}
    msgs_b_by_id: dict[str, list[dict[str, Any]]] = {}
    # Leg A's per-item eval draws (context + answer + tokens). Leg B reuses an
    # entry whenever its eval context is identical, collapsing decoder noise on
    # verbatim-routed items to exactly zero.
    eval_a_by_id: dict[str, dict[str, Any]] = {}

    records_a: list[dict[str, Any]] = []
    try:
        from leanctx.bench.workloads import load_workload

        agent_msgs = load_workload("agent")
        agent_items_a = [{"messages": agent_msgs, "workload": "agent_s1"}]
        print("[leg A] Running agent_s1 …")
        records_a.extend(run_leg("A", agent_items_a, shim_a_url, lb_cfg=None))

        if args.lb_stages >= 1:
            n = args.lb_n
            print(f"[leg A] Loading {n} LongBench questions "
                  f"(seed={args.sample_seed}, oversample_long={args.oversample_long}) …")
            lb_items = _load_lb_items(
                limit=n,
                workload_tag="lb_s1",
                seed=args.sample_seed,
                oversample_long=args.oversample_long,
            )
            print(f"[leg A] Running lb_s1 ({n} items) …")
            records_a.extend(
                run_leg(
                    "A", lb_items, shim_a_url, lb_cfg=eval_cfg,
                    msg_sink=msgs_a_by_id, eval_sink=eval_a_by_id,
                )
            )
    finally:
        shim_a.terminate()
        shim_a.wait()

    # ── Phase C: Leg B (CR + leanctx) ────────────────────────────────────
    print("[leg B] Starting CR shim with sidecar …")
    shim_b = spawn_cr_shim(
        workdir,
        port=args.shim_port + 1,
        extra_env={"LEANCTX_SIDECAR_URL": sidecar_url},
    )
    shim_b_url = f"http://127.0.0.1:{args.shim_port + 1}"

    records_b: list[dict[str, Any]] = []
    try:
        agent_items_b = [{"messages": load_workload("agent"), "workload": "agent_s1"}]
        print("[leg B] Running agent_s1 …")
        records_b.extend(run_leg("B", agent_items_b, shim_b_url, lb_cfg=None))

        if args.lb_stages >= 1:
            # reuse same LB items from Leg A
            print(f"[leg B] Running lb_s1 ({len(lb_items)} items) …")
            records_b.extend(
                run_leg(
                    "B", lb_items, shim_b_url, lb_cfg=eval_cfg,
                    msg_sink=msgs_b_by_id, reuse_eval=eval_a_by_id,
                )
            )
    finally:
        shim_b.terminate()
        shim_b.wait()

    # ── Phase C': Leg C (closed-book control) ─────────────────────────────
    # No document at all — the model answers LongBench from priors. This is the
    # control that rules out "the context was irrelevant"; it needs no shim and
    # no sidecar, just the eval LLM. Runs over the same LB items as Leg A/B.
    records_cb: list[dict[str, Any]] = []
    if args.closed_book and args.lb_stages >= 1 and lb_items:
        print(f"[leg C] Closed-book control ({len(lb_items)} items, no context) …")
        records_cb.extend(
            run_leg("C", lb_items, shim_url="", lb_cfg=eval_cfg, closed_book=True)
        )

    # ── Phase D: Score + report ───────────────────────────────────────────
    if sidecar_proc is not None:
        sidecar_proc.terminate()
        sidecar_proc.wait()

    all_a = [r for r in records_a if r["tokens_compressed"] > 0]
    all_b = [r for r in records_b if r["tokens_compressed"] > 0]

    import datetime as _dt
    run_date = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    metrics = compute_metrics(all_a, all_b, records_cb)
    gate = apply_gate(metrics, savings_threshold=args.savings_threshold,
                      accuracy_drop=args.accuracy_drop)

    # Verbatim-excluded compression view (Phase B): split each item's Layer-8
    # input into verbatim vs compressed tokens, then enrich the Leg B records
    # so the per-item split lands in the JSONL too.
    per_item_split = verbatim_split_by_item(msgs_a_by_id, msgs_b_by_id)
    verbatim_metrics = aggregate_verbatim_metrics(per_item_split)
    for rec in records_b:
        rec_id = rec.get("item_id")
        extra = per_item_split.get(rec_id) if rec_id is not None else None
        if extra:
            rec.update(extra)

    report_text = format_report(
        metrics, gate,
        records_a=all_a, records_b=all_b,
        run_date=run_date,
        eval_model=f"{args.eval_provider}/{args.eval_model}",
        cr_commit=args.cr_commit,
        savings_threshold=args.savings_threshold,
        accuracy_drop=args.accuracy_drop,
        verbatim_metrics=verbatim_metrics,
    )

    # Write JSONL
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as f:
        for rec in records_a + records_b + records_cb:
            f.write(json.dumps(rec) + "\n")

    # Write per-item deep-dive dumps (all JSONL fields + full input before /
    # output after Layer 8). records_b carries the lx_* split merged above.
    by_item_dir = args.by_item_dir or (args.out.parent / "by_item")
    item_files = write_by_item_dumps(
        by_item_dir, records_a, records_b, records_cb, msgs_a_by_id, msgs_b_by_id
    )

    # Write report
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report_text)

    print(report_text)
    print(f"\n[out] JSONL → {args.out}")
    print(f"[out] report → {args.report}")
    print(f"[out] per-item dumps → {by_item_dir} ({len(item_files)} items)")

    return 0 if gate.passed else 1


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(main())
