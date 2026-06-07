#!/usr/bin/env bash
# Smoke-test the leanctx Layer-8 sidecar: /health + a real /compress round-trip.
# Assumes the service is listening on $LEANCTX_URL (default localhost:8459).
#
#   ./integrations/clawrouter/scripts/smoke.sh
#
set -euo pipefail

URL="${LEANCTX_URL:-http://localhost:8459}"

echo "==> GET $URL/health"
curl -fsS "$URL/health"; echo

echo "==> POST $URL/compress  (long prose; expect output_tokens < input_tokens)"
# A long, redundant prose paragraph, repeated to clear the 1500-token threshold.
payload=$(python3 - <<'PY'
import json
prose = ("The quick brown fox jumps over the lazy dog, and in doing so it "
         "demonstrates a sentence that is intentionally verbose and padded "
         "with redundant filler words that carry very little information. ") * 120
print(json.dumps({"messages": [{"role": "user", "content": prose}]}))
PY
)
# Capture first, then parse. (Piping into `python3 - <<'PY'` collides on stdin:
# the heredoc becomes both the script AND sys.stdin, so json.load sees nothing.)
resp=$(curl -fsS "$URL/compress" -H 'content-type: application/json' -d "$payload")
printf '%s' "$resp" | python3 -c '
import json, sys
r = json.load(sys.stdin)
s = r["stats"]
print("  method=%s  in=%d  out=%d  ratio=%.3f" % (s["method"], s["input_tokens"], s["output_tokens"], s["ratio"]))
assert s["input_tokens"] > 0, "no tokens counted (is the [tokens]/tiktoken extra installed?)"
assert s["method"] != "passthrough", "service is in passthrough (LEANCTX_SERVER_MODE=off?)"
assert s["output_tokens"] < s["input_tokens"], "compression did not reduce tokens"
print("  OK — saved %.1f%% on prose" % (100 * (1 - s["ratio"])))
'
echo "smoke: PASS"
