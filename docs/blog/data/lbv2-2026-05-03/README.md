# LongBench v2 ablation — 2026-05-03 snapshot

Per-question JSONL records for the three runs cited in [`docs/blog/v0.3-launch.md`](../../v0.3-launch.md). Captured 2026-05-03 against `leanctx==0.3.1`.

## Setup

- **Dataset:** `THUDM/LongBench-v2`, filtered `length=short`, stratified across domains.
- **Subset:** 15 items (rate-limit-friendly; full 503-item sweep deferred to v0.3.x).
- **Eval model:** `claude-haiku-4-5`, `max_tokens=512`. Same model for all three conditions.
- **Truncation:** Head+tail to 20K tokens (mirrors LongBench's official harness for over-context items).
- **Request delay:** 30 sec between calls (50K input tokens/minute rate-limit ceiling on the test Anthropic key).
- **Reproduce:** `leanctx bench run longbench-v2` with the env vars in the blog post's "Reproduce" section.

## Files

| File | Method | Accuracy | Tokens kept |
|---|---|---:|---:|
| `baseline.jsonl` | Head+tail truncation only | 20.0 % (3/15) | 100 % of 20K cap |
| `lingua.jsonl` | leanctx Lingua, ratio=0.5 | **40.0 % (6/15)** | 43 % |
| `selfllm.jsonl` | leanctx SelfLLM (Haiku, ratio=0.3) | 26.7 % (4/15) | 1.4 % |

## Schema

Each line is one JSON object:

```
id              str    LongBench v2 _id
domain          str    one of 6 official domains
difficulty      str    easy | hard
length          str    short | medium | long
gold            str    A | B | C | D
prediction      str?   model's extracted answer or null if regex didn't match
correct         bool
pre_tokens      int    context length before compression + truncation
post_tokens     int    context length sent to the eval model
ratio           float  post_tokens / pre_tokens
duration_ms     int    per-question wall time
```

## Caveats

- n=15 is directional; statistical significance of the 20% → 40% delta is borderline (Fisher's exact two-sided p ≈ 0.18). Re-run on the full 503-item set when rate limits permit to tighten.
- Haiku at this truncation underperforms the leaderboard. Sonnet 4.5 at full context should land much higher across all conditions; the relative ordering is the load-bearing claim.
- LongBench v2 is CC BY-SA 4.0; per-question text is not redistributed here (only ids + scores).
