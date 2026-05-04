"""Runner: longbench-v2 — Tsinghua KEG's canonical long-context MC bench.

Scores accuracy on the 503 multiple-choice items in
``THUDM/LongBench-v2`` and lets users compare compressors head-to-head
on the same workload that the public leaderboard reports.

The prompt template, head+tail truncation rule, and answer-extraction
regex are mirrored from the official harness (THUDM/LongBench
``pred.py``) so leanctx numbers are directly comparable to the
leaderboard.

Configuration via env vars (matches the bench-CLI shape elsewhere; the
existing CLI doesn't surface every dial as a flag):

    LEANCTX_LBV2_COMPRESSOR    = none|lingua|selfllm   (default: none)
    LEANCTX_LBV2_LIMIT         = N items, 0=full set    (default: 60)
    LEANCTX_LBV2_PROVIDER      = anthropic|openai|gemini (default: anthropic)
    LEANCTX_LBV2_MODEL         = model id               (default: claude-sonnet-4-6)
    LEANCTX_LBV2_RATIO         = 0.0..1.0 keep ratio    (default: 0.5)
    LEANCTX_LBV2_MAX_LEN       = head+tail truncation tokens (default: 100000)
    LEANCTX_LBV2_OUT           = JSONL path for per-question records (default: '')
    LEANCTX_LBV2_DOMAIN        = filter to one domain   (default: all)
    LEANCTX_LBV2_DIFFICULTY    = easy|hard              (default: all)
    LEANCTX_LBV2_LENGTH        = short|medium|long      (default: all)
    LEANCTX_LBV2_REQUEST_DELAY = seconds between requests (default: 0)

The runner emits one aggregate ``BenchRecord`` per invocation. When
``LEANCTX_LBV2_OUT`` is set, one JSONL row per question (id, domain,
difficulty, length, gold, prediction, correct, pre_tokens, post_tokens,
duration_ms) is written for downstream analysis.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from leanctx.bench.scenarios import register
from leanctx.bench.schema import BenchRecord

_PROMPT_TEMPLATE = (
    "Please read the following text and answer the question below.\n\n"
    "<text>\n$DOC$\n</text>\n\n"
    "What is the correct answer to this question: $Q$\n"
    "Choices:\n"
    "(A) $C_A$\n"
    "(B) $C_B$\n"
    "(C) $C_C$\n"
    "(D) $C_D$\n\n"
    'Format your response as follows: "The correct answer is (insert answer here)".'
)

_ANSWER_RE_PAREN = re.compile(r"The correct answer is \(([A-D])\)")
_ANSWER_RE_BARE = re.compile(r"The correct answer is ([A-D])")


@register(
    "longbench-v2",
    description=(
        "LongBench v2 (THUDM/LongBench-v2) — 503 MC questions, 8K-2M words, "
        "6 domains. Scores compression methods against the public leaderboard."
    ),
    required_extras=("longbench", "tokens"),
    required_env=("ANTHROPIC_API_KEY|OPENAI_API_KEY|GEMINI_API_KEY|GOOGLE_API_KEY",),
)
def run(*, workload: str, **opts: object) -> BenchRecord:
    cfg = _load_config()

    items = _load_dataset(cfg)
    if not items:
        raise RuntimeError(
            "longbench-v2 dataset returned 0 items after filters; "
            "loosen LEANCTX_LBV2_{LIMIT,DOMAIN,DIFFICULTY,LENGTH} or check network access."
        )

    compressor = _build_compressor(cfg)
    eval_caller = _build_eval_caller(cfg)

    out_handle: Any = None
    if cfg["out_path"]:
        out_handle = open(cfg["out_path"], "w", encoding="utf-8")  # noqa: SIM115

    n_correct = 0
    total_pre = 0
    total_post = 0
    total_eval_in = 0
    total_eval_out = 0
    total_compress_cost = 0.0
    by_length: dict[str, dict[str, int]] = {}
    by_difficulty: dict[str, dict[str, int]] = {}
    by_domain: dict[str, dict[str, int]] = {}

    t0_total = time.perf_counter()
    delay_sec = float(cfg.get("request_delay_sec", 0))
    try:
        for idx, item in enumerate(items):
            if idx > 0 and delay_sec > 0:
                time.sleep(delay_sec)
            t0 = time.perf_counter()
            context = item["context"]
            pre_tokens = _count_tokens(context)
            compressed_context, compress_stats = compressor(context)
            compressed_context = _head_tail_truncate(
                compressed_context, cfg["max_len"]
            )
            post_tokens = _count_tokens(compressed_context)

            prompt = (
                _PROMPT_TEMPLATE
                .replace("$DOC$", compressed_context.strip())
                .replace("$Q$", item["question"])
                .replace("$C_A$", item["choice_A"])
                .replace("$C_B$", item["choice_B"])
                .replace("$C_C$", item["choice_C"])
                .replace("$C_D$", item["choice_D"])
            )

            response_text, eval_in, eval_out = eval_caller(prompt)
            prediction = _extract_answer(response_text)
            correct = prediction == item["answer"]

            duration_ms = int((time.perf_counter() - t0) * 1000)
            total_pre += pre_tokens
            total_post += post_tokens
            total_eval_in += eval_in
            total_eval_out += eval_out
            total_compress_cost += compress_stats.get("cost_usd", 0.0)
            n_correct += int(correct)

            _bump(by_length, item["length"], correct)
            _bump(by_difficulty, item["difficulty"], correct)
            _bump(by_domain, item["domain"], correct)

            if out_handle is not None:
                out_handle.write(
                    json.dumps(
                        {
                            "id": item["_id"],
                            "domain": item["domain"],
                            "difficulty": item["difficulty"],
                            "length": item["length"],
                            "gold": item["answer"],
                            "prediction": prediction,
                            "correct": correct,
                            "pre_tokens": pre_tokens,
                            "post_tokens": post_tokens,
                            "ratio": (post_tokens / pre_tokens) if pre_tokens else 1.0,
                            "duration_ms": duration_ms,
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                out_handle.flush()
    finally:
        if out_handle is not None:
            out_handle.close()

    duration_ms = int((time.perf_counter() - t0_total) * 1000)
    n = len(items)
    accuracy = n_correct / n if n else 0.0

    return BenchRecord(
        leanctx_version=_lc_version(),
        scenario="longbench-v2",
        workload=workload or "all",
        status="success",
        request_provider=cfg["provider"],
        request_model=cfg["model"],
        compression_provider=cfg["compressor"] if cfg["compressor"] != "none" else None,
        compression_model=(
            None if cfg["compressor"] == "none" else cfg.get("compressor_model")
        ),
        compressor=cfg["compressor"],
        input_tokens=total_post,
        output_tokens=total_eval_out,
        tokens_saved=total_pre - total_post,
        ratio=(total_post / total_pre) if total_pre else 1.0,
        cost_usd=total_compress_cost,
        duration_ms=duration_ms,
        warmup=False,
        timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        extra={
            "n_questions": n,
            "n_correct": n_correct,
            "accuracy": accuracy,
            "total_pre_tokens": total_pre,
            "total_post_tokens": total_post,
            "by_length": _summarize(by_length),
            "by_difficulty": _summarize(by_difficulty),
            "by_domain": _summarize(by_domain),
            "max_len": cfg["max_len"],
            "ratio_setting": cfg["ratio"],
            "limit": cfg["limit"],
        },
    )


# --------------------------------------------------------------------------- #
# Config + dataset loading
# --------------------------------------------------------------------------- #


def _load_config() -> dict[str, Any]:
    return {
        "compressor": os.environ.get("LEANCTX_LBV2_COMPRESSOR", "none"),
        "limit": int(os.environ.get("LEANCTX_LBV2_LIMIT", "60")),
        "provider": os.environ.get("LEANCTX_LBV2_PROVIDER", "anthropic"),
        "model": os.environ.get("LEANCTX_LBV2_MODEL", "claude-sonnet-4-6"),
        "ratio": float(os.environ.get("LEANCTX_LBV2_RATIO", "0.5")),
        "max_len": int(os.environ.get("LEANCTX_LBV2_MAX_LEN", "100000")),
        "out_path": os.environ.get("LEANCTX_LBV2_OUT", ""),
        "domain": os.environ.get("LEANCTX_LBV2_DOMAIN", ""),
        "difficulty": os.environ.get("LEANCTX_LBV2_DIFFICULTY", ""),
        "length": os.environ.get("LEANCTX_LBV2_LENGTH", ""),
        "compressor_model": os.environ.get("LEANCTX_LBV2_COMPRESSOR_MODEL", ""),
        "max_tokens": int(os.environ.get("LEANCTX_LBV2_MAX_RESPONSE_TOKENS", "128")),
        "request_delay_sec": float(
            os.environ.get("LEANCTX_LBV2_REQUEST_DELAY", "0")
        ),
    }


def _load_dataset(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Load the HF dataset and apply filters + limit. Stratified subset
    by length × difficulty when limit > 0 and no other filter is set."""
    try:
        from datasets import load_dataset  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "the [longbench] extra is required for longbench-v2. "
            "Install with: pip install 'leanctx[longbench]'"
        ) from exc

    ds = load_dataset("THUDM/LongBench-v2", split="train")
    items: list[dict[str, Any]] = list(ds)

    if cfg["domain"]:
        items = [it for it in items if it["domain"] == cfg["domain"]]
    if cfg["difficulty"]:
        items = [it for it in items if it["difficulty"] == cfg["difficulty"]]
    if cfg["length"]:
        items = [it for it in items if it["length"] == cfg["length"]]

    limit = cfg["limit"]
    if limit > 0 and len(items) > limit:
        if not (cfg["domain"] or cfg["difficulty"] or cfg["length"]):
            items = _stratified_sample(items, limit)
        else:
            items = items[:limit]

    return items


def _stratified_sample(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Sample roughly evenly across length × difficulty cells (6 cells)."""
    cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for it in items:
        key = (it["length"], it["difficulty"])
        cells.setdefault(key, []).append(it)
    if not cells:
        return items[:limit]
    per_cell = max(1, limit // len(cells))
    out: list[dict[str, Any]] = []
    for cell_items in cells.values():
        out.extend(cell_items[:per_cell])
    return out[:limit]


# --------------------------------------------------------------------------- #
# Compression hooks
# --------------------------------------------------------------------------- #


def _build_compressor(cfg: dict[str, Any]) -> Any:
    name = cfg["compressor"]
    if name == "none":
        return _passthrough_compressor
    if name == "lingua":
        return _build_lingua_compressor(cfg)
    if name == "selfllm":
        return _build_selfllm_compressor(cfg)
    raise ValueError(
        f"unknown compressor {name!r}; expected one of none|lingua|selfllm"
    )


def _passthrough_compressor(text: str) -> tuple[str, dict[str, float]]:
    return text, {"cost_usd": 0.0}


def _build_lingua_compressor(cfg: dict[str, Any]) -> Any:
    try:
        from leanctx.compressors import Lingua  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "the [lingua] extra is required for compressor=lingua. "
            "Install with: pip install 'leanctx[lingua]'"
        ) from exc

    lingua = Lingua(ratio=cfg["ratio"])

    def _compress(text: str) -> tuple[str, dict[str, float]]:
        msgs = [{"role": "user", "content": text}]
        out, stats = lingua.compress(msgs)
        return _msgs_to_text(out), {"cost_usd": stats.cost_usd}

    return _compress


def _build_selfllm_compressor(cfg: dict[str, Any]) -> Any:
    try:
        from leanctx.compressors import SelfLLM  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "a provider extra is required for compressor=selfllm. "
            "Install with: pip install 'leanctx[anthropic]' (or [openai], [gemini])"
        ) from exc

    provider = cfg["provider"]
    selfllm = SelfLLM(
        provider=provider,
        model=cfg.get("compressor_model") or None,
        ratio=cfg["ratio"],
    )

    def _compress(text: str) -> tuple[str, dict[str, float]]:
        msgs = [{"role": "user", "content": text}]
        out, stats = selfllm.compress(msgs)
        return _msgs_to_text(out), {"cost_usd": stats.cost_usd}

    return _compress


def _msgs_to_text(msgs: list[dict[str, Any]]) -> str:
    if not msgs:
        return ""
    content = msgs[0].get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("text") or block.get("content") or ""
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return str(content)


# --------------------------------------------------------------------------- #
# Eval LLM caller (per-provider)
# --------------------------------------------------------------------------- #


def _build_eval_caller(cfg: dict[str, Any]) -> Any:
    provider = cfg["provider"]
    if provider == "anthropic":
        return _anthropic_caller(cfg)
    if provider == "openai":
        return _openai_caller(cfg)
    if provider == "gemini":
        return _gemini_caller(cfg)
    raise ValueError(
        f"unknown provider {provider!r}; expected one of anthropic|openai|gemini"
    )


def _anthropic_caller(cfg: dict[str, Any]) -> Any:
    try:
        import anthropic  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "the [anthropic] extra is required for provider=anthropic. "
            "Install with: pip install 'leanctx[anthropic]'"
        ) from exc

    client = anthropic.Anthropic()

    def _call(prompt: str) -> tuple[str, int, int]:
        resp = client.messages.create(
            model=cfg["model"],
            max_tokens=cfg["max_tokens"],
            messages=[{"role": "user", "content": prompt}],
        )
        text = ""
        if resp.content:
            block = resp.content[0]
            text = getattr(block, "text", "") or ""
        usage = resp.usage
        return text, int(usage.input_tokens), int(usage.output_tokens)

    return _call


def _openai_caller(cfg: dict[str, Any]) -> Any:
    try:
        import openai  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "the [openai] extra is required for provider=openai."
        ) from exc

    client = openai.OpenAI()

    def _call(prompt: str) -> tuple[str, int, int]:
        resp = client.chat.completions.create(
            model=cfg["model"],
            max_tokens=cfg["max_tokens"],
            messages=[{"role": "user", "content": prompt}],
        )
        choice = resp.choices[0]
        text = choice.message.content or ""
        usage = resp.usage
        in_tok = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
        out_tok = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
        return text, in_tok, out_tok

    return _call


def _gemini_caller(cfg: dict[str, Any]) -> Any:
    try:
        from google import genai  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "the [gemini] extra is required for provider=gemini."
        ) from exc

    client = genai.Client()

    def _call(prompt: str) -> tuple[str, int, int]:
        resp = client.models.generate_content(model=cfg["model"], contents=prompt)
        text = resp.text or ""
        meta = getattr(resp, "usage_metadata", None)
        in_tok = int(getattr(meta, "prompt_token_count", 0) or 0) if meta else 0
        out_tok = (
            int(getattr(meta, "candidates_token_count", 0) or 0) if meta else 0
        )
        return text, in_tok, out_tok

    return _call


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _extract_answer(response: str) -> str | None:
    """Mirrors THUDM/LongBench pred.py's extraction. Returns A/B/C/D or None."""
    m = _ANSWER_RE_PAREN.search(response.replace("*", ""))
    if not m:
        m = _ANSWER_RE_BARE.search(response)
    return m.group(1) if m else None


def _head_tail_truncate(text: str, max_len: int) -> str:
    """If the text exceeds max_len tokens, keep the first half and the
    last half (THUDM/LongBench's truncation rule)."""
    tokens = _count_tokens(text)
    if tokens <= max_len:
        return text
    # Approximate: split by characters proportional to the token ratio.
    # The official harness uses tokenizer-exact slicing; for parity we
    # would need the same tokenizer the eval LLM uses. char-based
    # head+tail is a documented approximation — fine for compression
    # comparison purposes since all candidate compressors are subject
    # to the same rule.
    keep_chars = int(len(text) * (max_len / tokens))
    half = keep_chars // 2
    return text[:half] + text[-half:]


def _count_tokens(text: str) -> int:
    """Token count via leanctx.tokens (tiktoken fast path with char/4 fallback)."""
    from leanctx.tokens import count_tokens  # noqa: PLC0415

    return count_tokens(text)


def _bump(bucket: dict[str, dict[str, int]], key: str, correct: bool) -> None:
    cell = bucket.setdefault(key, {"n": 0, "correct": 0})
    cell["n"] += 1
    if correct:
        cell["correct"] += 1


def _summarize(bucket: dict[str, dict[str, int]]) -> dict[str, dict[str, float]]:
    return {
        k: {
            "n": v["n"],
            "correct": v["correct"],
            "accuracy": (v["correct"] / v["n"]) if v["n"] else 0.0,
        }
        for k, v in bucket.items()
    }


def _lc_version() -> str:
    from leanctx import __version__  # noqa: PLC0415

    return __version__
