#!/usr/bin/env bash
# Smoke-test the leanctx sidecar for InsForge: /health + a real /compress round-trip.
# Asserts prose is compressed AND the tool message is forwarded verbatim.
# Assumes the service is listening on $LEANCTX_URL (default localhost:8459).
#
#   ./integrations/insforge/scripts/smoke.sh
#
set -euo pipefail

URL="${LEANCTX_URL:-http://localhost:8459}"

echo "==> GET $URL/health"
curl -fsS "$URL/health"; echo

echo "==> POST $URL/compress  (prose + tool; expect prose shrinks, tool verbatim)"
payload=$(python3 - <<'PY'
import json
prose = ("The InsForge Model Gateway proxies chat requests to OpenRouter and meters "
         "token usage for every call a coding agent makes. ") * 160
print(json.dumps({"messages": [
    {"role": "user", "content": prose},
    {"role": "tool", "tool_call_id": "t1", "content": "{\"rows\": 42}"},
]}))
PY
)
# Capture first, then parse. (Piping into `python3 - <<'PY'` collides on stdin:
# the heredoc becomes both the script AND sys.stdin, so json.load sees nothing.)
resp=$(curl -fsS "$URL/compress" -H 'content-type: application/json' -d "$payload")
printf '%s' "$resp" | python3 -c '
import json, sys
r = json.load(sys.stdin)
s = r["stats"]
msgs = r["messages"]
print("  method=%s  in=%d  out=%d  ratio=%.3f" % (s["method"], s["input_tokens"], s["output_tokens"], s["ratio"]))
assert s["input_tokens"] > 0, "no tokens counted (is the [tokens]/tiktoken extra installed?)"
assert s["method"] != "passthrough", "service is in passthrough (LEANCTX_SERVER_MODE=off?)"
assert s["output_tokens"] < s["input_tokens"], "compression did not reduce tokens"
tool = [m for m in msgs if m["role"] == "tool"][0]["content"]
assert tool == "{\"rows\": 42}", "tool message was not forwarded verbatim"
print("  OK — saved %.1f%% on prose; tool verbatim" % (100 * (1 - s["ratio"])))
'
echo "smoke: PASS"
