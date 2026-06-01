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

# ── Layer 8 TypeScript block ───────────────────────────────────────────────
# Inserted into src/compression/index.ts immediately before the anchor line
#   const compressedChars = calculateTotalChars(result);
# Enabled only when LEANCTX_SIDECAR_URL is present in the environment.

LAYER8_TS_BLOCK = """\
  // ── Layer 8: leanctx ML prose compression (opt-in) ──────────────────────
  // Enabled only when LEANCTX_SIDECAR_URL is set in the environment.
  // When unset, this block is a complete no-op — CR behaviour is unchanged.
  const _leanctxUrl = process.env.LEANCTX_SIDECAR_URL;
  if (_leanctxUrl) {
    const _hasEligible = result.some(
      (m: NormalizedMessage) =>
        ["system", "user", "assistant"].includes(m.role) &&
        typeof m.content === "string",
    );
    if (_hasEligible) {
      try {
        const _resp = await fetch(`${_leanctxUrl}/compress`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ messages: result }),
          signal: AbortSignal.timeout(60000),
        });
        if (_resp.ok) {
          const _body = (await _resp.json()) as { messages?: NormalizedMessage[] };
          if (Array.isArray(_body?.messages) && _body.messages.length === result.length) {
            result = _body.messages;
          }
        }
      } catch (_e) {
        // sidecar timeout / unreachable — fall through with CR-only compression
      }
    }
  }
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
    if "LEANCTX_SIDECAR_URL" in src:
        return  # idempotent
    target.write_text(src.replace(_TS_ANCHOR, LAYER8_TS_BLOCK + "\n" + _TS_ANCHOR))
    print(f"[patch A] Layer 8 injected into {target.relative_to(cr_dir)}")


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


def compute_metrics(
    records_a: list[dict[str, Any]],
    records_b: list[dict[str, Any]],
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

    # $15 / 1M input tokens (Sonnet price) × 1K conversations
    cost_saved_per_1k = delta_tokens * avg_raw * (15 / 1e6) * 1000

    durations_b = [r["duration_ms"] for r in records_b if r.get("duration_ms") is not None]
    sidecar_p50_ms: float | None = None
    if durations_b:
        sorted_d = sorted(durations_b)
        mid = len(sorted_d) // 2
        sidecar_p50_ms = (
            sorted_d[mid] if len(sorted_d) % 2 else (sorted_d[mid - 1] + sorted_d[mid]) / 2
        )

    return {
        "delta_tokens": delta_tokens,
        "delta_accuracy": delta_accuracy,
        "e2e_ratio": e2e_ratio,
        "cr_savings": cr_savings,
        "cost_saved_per_1k": cost_saved_per_1k,
        "sidecar_p50_ms": sidecar_p50_ms,
        "tokens_a": tokens_a,
        "tokens_b": tokens_b,
        "acc_a": acc_a,
        "acc_b": acc_b,
    }


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

    date_str = run_date or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    verdict_icon = "✅" if gate.passed else "❌"

    def pct(v: float | None) -> str:
        return f"{v * 100:.1f}%" if v is not None else "N/A"

    def fmt_f(v: float | None, decimals: int = 4) -> str:
        return f"{v:.{decimals}f}" if v is not None else "N/A"

    lb_a = [r for r in records_a if r.get("accuracy") is not None]
    lb_b = [r for r in records_b if r.get("accuracy") is not None]
    agent_a = [r for r in records_a if r.get("accuracy") is None]
    agent_b = [r for r in records_b if r.get("accuracy") is None]

    n_lb = len(lb_a)
    acc_a = metrics.get("acc_a", 0.0)
    acc_b = metrics.get("acc_b", 0.0)
    delta_acc = metrics.get("delta_accuracy", 0.0)

    tokens_raw_avg = sum(r["tokens_raw"] for r in records_a) / max(len(records_a), 1)
    tokens_a_avg = metrics.get("tokens_a", 0) / max(len(records_a), 1)
    tokens_b_avg = metrics.get("tokens_b", 0) / max(len(records_b), 1)

    # go/no-go row helpers
    gate_row = lambda label, threshold, actual, passed_cond: (
        f"| {label} | {threshold} | {actual} | {'✅ PASS' if passed_cond else '❌ FAIL'} |"
    )
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

    p50 = metrics.get("sidecar_p50_ms")
    p50_str = f"{p50:,.0f} ms" if p50 is not None else "N/A"

    # latency p95 from records_b duration
    durations_b = sorted(r["duration_ms"] for r in records_b if r.get("duration_ms"))
    p95_str = "N/A"
    if durations_b:
        idx95 = int(len(durations_b) * 0.95)
        p95_str = f"{durations_b[min(idx95, len(durations_b)-1)]:,} ms"

    # ── build report ─────────────────────────────────────────────────────
    out: list[str] = []
    A = out.append

    A(f"# ClawRouter × leanctx — Phase 1 Benchmark Report")
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

    # 3. LongBench accuracy
    A("## 3. LongBench v2 Accuracy")
    A("")
    if n_lb > 0:
        A(f"| | N | Leg A (CR only) | Leg B (CR + leanctx) | Δ |")
        A("|---|---|---|---|---|")
        A(f"| **Overall** | {n_lb} | {pct(acc_a)} | {pct(acc_b)} | {pct(delta_acc)} |")
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
    layer8_delta = (metrics.get("tokens_a", 0) - metrics.get("tokens_b", 0)) / max(len(records_a), 1)
    A(f"| **L8 leanctx** | Δ tokens / item | **{layer8_delta:,.0f}** |")
    A("")

    # 6. Latency
    A("## 6. Latency")
    A("")
    A("| Metric | Value |")
    A("|--------|-------|")
    A(f"| Sidecar (Layer 8) P50 | {p50_str} |")
    A(f"| Sidecar (Layer 8) P95 | {p95_str} |")
    A("")

    # 7. Cost analysis
    A("## 7. Cost Analysis")
    A("")
    cost = metrics.get("cost_saved_per_1k", 0.0)
    delta_t = metrics.get("delta_tokens", 0.0)
    A(f"Assuming Claude Sonnet input pricing ($15 / 1M tokens):")
    A("")
    A(f"```")
    A(f"Δ tokens      = {pct(delta_t)} of avg {tokens_raw_avg:,.0f} raw tokens")
    A(f"Savings / req = {delta_t * tokens_raw_avg:,.0f} tokens")
    A(f"Cost saved    = {delta_t * tokens_raw_avg * 15 / 1e6:.4f} USD / request")
    A(f"              = ${cost:,.2f} per 1 000 requests")
    A(f"```")
    A("")
    A("---")
    A("")
    A(f"_Generated by `bench_clawtypeor_phase1.py` · CR commit `{cr_commit[:8] if cr_commit else 'unknown'}`_")

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
) -> subprocess.Popen | None:
    if health_check(url):
        return None  # already running
    import sys as _sys

    # Resolve leanctx-serve from the same venv as the running interpreter.
    venv_bin = str(Path(_sys.executable).parent)
    env = {
        **os.environ,
        "PATH": f"{venv_bin}:{os.environ.get('PATH', '')}",
        "LEANCTX_SERVER_LINGUA_RATIO": str(lingua_ratio),
    }
    proc = subprocess.Popen(["leanctx-serve"], env=env)
    # 120 s: includes one-time LLMLingua-2 weight download + warmup pass.
    wait_for_health(proc, url, timeout=120)
    return proc


# ═══════════════════════════════════════════════════════════════════════════
# Sprint 6 — CR shim integration helpers
# ═══════════════════════════════════════════════════════════════════════════


def setup_clawtypeor(workdir: Path, commit: str = CR_DEFAULT_COMMIT) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    if not (workdir / ".git").exists():
        subprocess.run(["git", "clone", CR_REPO_URL, str(workdir)], check=True)
    subprocess.run(["git", "checkout", commit], cwd=workdir, check=True)
    subprocess.run(["npm", "ci"], cwd=workdir, check=True)
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


def _extract_lb_answer(response: str) -> str | None:
    m = _LB_ANS_RE_PAREN.search(response.replace("*", ""))
    if not m:
        m = _LB_ANS_RE_BARE.search(response)
    return m.group(1) if m else None


def _lb_head_tail(text: str, max_chars: int = 60_000) -> str:
    """Keep first + last quarter when text exceeds max_chars."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n...[truncated]...\n" + text[-half:]


def _load_lb_items(limit: int = 5, workload_tag: str = "lb_s1") -> list[dict[str, Any]]:
    """Load and format LongBench v2 items for run_leg()."""
    from datasets import load_dataset

    ds = load_dataset("THUDM/LongBench-v2", split="train")
    items: list[dict] = list(ds)

    # stratified sample across length × difficulty cells
    if limit > 0 and len(items) > limit:
        cells: dict[tuple[str, str], list[dict]] = {}
        for it in items:
            key = (it.get("length", ""), it.get("difficulty", ""))
            cells.setdefault(key, []).append(it)
        per_cell = max(1, limit // len(cells))
        sample: list[dict] = []
        for cell_items in cells.values():
            sample.extend(cell_items[:per_cell])
        items = sample[:limit]

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


def call_eval_llm(
    compressed_messages: list[dict],
    item: dict,
    eval_cfg: dict,
) -> tuple[str | None, int]:
    """Build an LB prompt from compressed messages + item; call the eval LLM.

    Returns (predicted_letter | None, input_tokens).
    """
    context = _lb_head_tail(
        " ".join(
            m["content"]
            for m in compressed_messages
            if isinstance(m.get("content"), str)
            and m.get("role") in ("user", "system", "assistant")
        ).strip()
    )

    prompt = (
        _LB_PROMPT_TEMPLATE
        .replace("$DOC$", context)
        .replace("$Q$", item.get("question", ""))
        .replace("$C_A$", item.get("choice_A", ""))
        .replace("$C_B$", item.get("choice_B", ""))
        .replace("$C_C$", item.get("choice_C", ""))
        .replace("$C_D$", item.get("choice_D", ""))
    )

    provider = eval_cfg.get("provider", "anthropic")
    model = eval_cfg.get("model", "claude-sonnet-4-6")
    max_tokens = int(eval_cfg.get("max_tokens", 64))

    if provider == "anthropic":
        import anthropic

        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
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
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.choices[0].message.content or ""
        in_tok = int(resp.usage.prompt_tokens) if resp.usage else 0
    else:
        raise ValueError(f"Unsupported eval provider: {provider!r}")

    return _extract_lb_answer(text), in_tok


def run_leg(
    leg: str,
    items: list[dict[str, Any]],
    shim_url: str,
    lb_cfg: dict | None,
) -> list[dict[str, Any]]:
    records = []
    for item in items:
        t0 = time.perf_counter()
        messages = item["messages"]
        tokens_raw = _sum_tokens(messages)
        shim_result = compress_via_shim(messages, shim_url)
        compressed = shim_result["messages"]
        tokens_compressed = _sum_tokens(compressed)
        rec: dict[str, Any] = {
            "leg": leg,
            "workload": item.get("workload", "unknown"),
            "item_id": item.get("item_id"),
            "tokens_raw": tokens_raw,
            "tokens_compressed": tokens_compressed,
            "cr_compression_ratio": shim_result.get("compressionRatio"),
            "cr_stats": shim_result.get("stats"),
            "accuracy": None,
        }
        if lb_cfg and item.get("question"):
            answer, in_tok = call_eval_llm(compressed, item, lb_cfg)
            rec["accuracy"] = answer == item.get("gold")
            rec["lb_gold"] = item.get("gold")
            rec["lb_pred"] = answer
            rec["eval_input_tokens"] = in_tok
            rec["lb_domain"] = item.get("lb_domain", "")
            rec["lb_difficulty"] = item.get("lb_difficulty", "")
            rec["lb_length"] = item.get("lb_length", "")
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
    parser.add_argument("--workdir", type=Path, default=Path("/tmp/clawtypeor_bench"))
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
    parser.add_argument("--eval-provider",
                        default=os.environ.get("BENCHMARK_EVAL_PROVIDER", "anthropic"))
    parser.add_argument("--eval-model",
                        default=os.environ.get("BENCHMARK_EVAL_MODEL", "claude-sonnet-4-6"))
    parser.add_argument("--savings-threshold", type=float, default=0.20)
    parser.add_argument("--accuracy-drop", type=float, default=0.02)
    parser.add_argument("--out", type=Path, default=Path("./phase1_results.jsonl"))
    parser.add_argument("--report", type=Path, default=Path("./phase1_report.md"))
    args = parser.parse_args(argv)

    if args.dry_run_patch:
        print(LAYER8_TS_BLOCK)
        return 0

    workdir = args.workdir
    sidecar_url = args.sidecar_url
    eval_cfg: dict[str, Any] = {
        "provider": args.eval_provider,
        "model": args.eval_model,
        "max_tokens": 64,
    }

    # ── Phase A: Setup ────────────────────────────────────────────────────
    already_built = (workdir / "dist" / "compression" / "index.js").exists()
    if not args.skip_setup and not already_built:
        print("[setup] Cloning + building ClawRouter…")
        setup_clawtypeor(workdir, commit=args.cr_commit)
    write_shim_file(workdir)

    # ── Phase B: Spawn sidecar ────────────────────────────────────────────
    print(f"[sidecar] Checking {sidecar_url} …")
    sidecar_proc = spawn_leanctx_sidecar(sidecar_url, lingua_ratio=args.lingua_ratio)

    # ── Phase C: Leg A (CR only) ──────────────────────────────────────────
    print("[leg A] Starting CR shim without sidecar …")
    shim_a = spawn_cr_shim(workdir, port=args.shim_port, extra_env={})
    shim_a_url = f"http://127.0.0.1:{args.shim_port}"

    records_a: list[dict[str, Any]] = []
    try:
        from leanctx.bench.workloads import load_workload

        agent_msgs = load_workload("agent")
        agent_items_a = [{"messages": agent_msgs, "workload": "agent_s1"}]
        print("[leg A] Running agent_s1 …")
        records_a.extend(run_leg("A", agent_items_a, shim_a_url, lb_cfg=None))

        if args.lb_stages >= 1:
            n = args.lb_n
            print(f"[leg A] Loading {n} LongBench questions …")
            lb_items = _load_lb_items(limit=n, workload_tag="lb_s1")
            print(f"[leg A] Running lb_s1 ({n} items) …")
            records_a.extend(run_leg("A", lb_items, shim_a_url, lb_cfg=eval_cfg))
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
            records_b.extend(run_leg("B", lb_items, shim_b_url, lb_cfg=eval_cfg))
    finally:
        shim_b.terminate()
        shim_b.wait()

    # ── Phase D: Score + report ───────────────────────────────────────────
    if sidecar_proc is not None:
        sidecar_proc.terminate()
        sidecar_proc.wait()

    all_a = [r for r in records_a if r["tokens_compressed"] > 0]
    all_b = [r for r in records_b if r["tokens_compressed"] > 0]

    import datetime as _dt
    run_date = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    metrics = compute_metrics(all_a, all_b)
    gate = apply_gate(metrics, savings_threshold=args.savings_threshold,
                      accuracy_drop=args.accuracy_drop)
    report_text = format_report(
        metrics, gate,
        records_a=all_a, records_b=all_b,
        run_date=run_date,
        eval_model=f"{args.eval_provider}/{args.eval_model}",
        cr_commit=args.cr_commit,
        savings_threshold=args.savings_threshold,
        accuracy_drop=args.accuracy_drop,
    )

    # Write JSONL
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as f:
        for rec in records_a + records_b:
            f.write(json.dumps(rec) + "\n")

    # Write report
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report_text)

    print(report_text)
    print(f"\n[out] JSONL → {args.out}")
    print(f"[out] report → {args.report}")

    return 0 if gate.passed else 1


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(main())
