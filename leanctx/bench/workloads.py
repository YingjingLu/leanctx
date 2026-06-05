"""Bundled workload fixtures.

Three workloads ship with leanctx:

- ``rag``  — a single 1.7 KB SRE-incident document; matches the
  benchmark in docs/benchmarks/agent-workload.md's RAG harness
- ``chat`` — multi-turn conversation, ~3 KB across 8 turns
- ``agent`` — 9-message coding-agent transcript with tool_use /
  tool_result blocks (matches scripts/integration_test_agent_workload.py)

Workloads are returned as a list of message dicts in the leanctx
canonical shape (``role``, ``content``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

WorkloadName = str


def _rag() -> list[dict[str, Any]]:
    document = (
        "Service degradation timeline:\n"
        "14:02 — checkout-api p99 latency rises from 120ms to 480ms.\n"
        "14:04 — order-events Kafka topic partition 7 lag spikes to 200k.\n"
        "14:05 — pager triggers on the SRE rotation.\n"
        "14:08 — first responder confirms checkout-api pod CPU is at 95%.\n"
        "14:11 — restart of payment-gateway-* pods returns latency to baseline.\n"
        "14:12 — root cause is a connection-pool exhaustion in payment-gateway after a "
        "deploy that increased the per-request DB query count from 3 to 7 without "
        "raising the pool size.\n"
        "14:30 — fix lands: pool size raised from 20 to 50, plus an alert on pool "
        "saturation > 80%.\n"
    ) * 4
    return [
        {"role": "user", "content": document},
        {
            "role": "user",
            "content": "Summarize this incident in two sentences.",
        },
    ]


def _chat() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "What is the average annual rainfall in Seattle?"},
        {"role": "assistant", "content": "About 37 inches per year."},
        {"role": "user", "content": "How does that compare to Portland?"},
        {
            "role": "assistant",
            "content": "Portland averages around 43 inches — slightly wetter than Seattle.",
        },
        {"role": "user", "content": "What about San Francisco?"},
        {"role": "assistant", "content": "San Francisco gets about 23 inches annually."},
        {"role": "user", "content": "Now give me a summary of all three."},
    ]


def _agent() -> list[dict[str, Any]]:
    # Compact agent-workload fixture: tool_use + tool_result with a code
    # block, an error trace, and a long log span. Mirrors the structural
    # invariants exercised by scripts/integration_test_agent_workload.py.
    return [
        {
            "role": "user",
            "content": "Find the bug that's making checkouts return 502.",
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I'll grep the recent error logs."},
                {
                    "type": "tool_use",
                    "id": "tu_001",
                    "name": "grep_logs",
                    "input": {"pattern": "ERROR.*checkout", "since": "30m"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_001",
                    "content": (
                        "2026-04-25T14:02:13Z ERROR checkout-api: payment_gateway timeout\n"
                        "2026-04-25T14:02:14Z ERROR checkout-api: payment_gateway timeout\n"
                        "2026-04-25T14:02:14Z ERROR checkout-api: payment_gateway timeout\n"
                        + ("2026-04-25T14:02:15Z INFO routine heartbeat\n" * 30)
                        + "2026-04-25T14:03:01Z ERROR payment-gateway: connection pool exhausted"
                    ),
                }
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Pool exhaustion. Let me view the config."},
                {
                    "type": "tool_use",
                    "id": "tu_002",
                    "name": "read_file",
                    "input": {"path": "payment-gateway/config/db.py"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_002",
                    "content": (
                        "```python\n"
                        "DB_POOL_SIZE = 20  # raised from 5 in 2024\n"
                        "DB_POOL_TIMEOUT = 5\n"
                        "DB_QUERY_BUDGET = 7  # was 3 before deploy 2026-04-25 14:00\n"
                        "```"
                    ),
                }
            ],
        },
        {
            "role": "assistant",
            "content": (
                "DB_QUERY_BUDGET went from 3 to 7 in today's deploy without raising "
                "DB_POOL_SIZE — that's the regression. Recommend raising pool size to 50 "
                "and adding an alert on pool saturation > 80%."
            ),
        },
        {"role": "user", "content": "Confirm the deploy SHA and the relevant traceback."},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Traceback (most recent call last):\n"
                        "  File 'app.py', line 142, in process_payment\n"
                        "    conn = pool.acquire(timeout=5)\n"
                        "  File 'pool.py', line 87, in acquire\n"
                        "    raise TimeoutError('pool exhausted after 5s')\n"
                        "TimeoutError: pool exhausted after 5s"
                    ),
                }
            ],
        },
        {"role": "user", "content": "Thanks. Summarize the fix in one paragraph."},
    ]


def _agent_extended() -> list[dict[str, Any]]:
    """50-turn agent workload spanning 5 episodes (10 turns each).

    Episode 1 — grep & triage:  auth/service.py,                 TimeoutError
    Episode 2 — file reads:     payments/gateway.py + config.py, ConnectionError
    Episode 3 — error analysis: orders/api.py,                   AssertionError
    Episode 4 — fix iteration:  infra/k8s.yaml,                  KeyError
    Episode 5 — verification:   tests/integration/test_checkout.py, RuntimeError

    Structural invariants exercised:
    - 10 tool_use / tool_result pairs with matched IDs
    - Long repeated-log tool_result output for CR observation compression
    - Multiple fenced code blocks for verbatim-preservation testing
    - Python Traceback for error-verbatim testing
    - Multiple assistant string messages > 400 chars for Lingua prose compression
    - Total token count well above the leanctx 1500-token compression threshold
    """
    msgs: list[dict[str, Any]] = []

    # ── Episode 1: grep & triage (auth/service.py, TimeoutError) ─────────
    msgs.extend([
        {
            "role": "user",
            "content": (
                "We're getting a wave of 504 Gateway Timeout errors from the auth service "
                "starting around 09:15 UTC. The on-call engineer escalated after two alerts "
                "fired back-to-back. Can you grep the auth service logs for errors in the "
                "last 30 minutes and tell me what's going on?"
            ),
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "I'll grep the auth service logs for errors in the last 30 minutes.",
                },
                {
                    "type": "tool_use",
                    "id": "tu_e1_01",
                    "name": "grep_logs",
                    "input": {
                        "service": "auth-service",
                        "pattern": "ERROR|WARN",
                        "since": "30m",
                    },
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_e1_01",
                    "content": (
                        "2026-04-30T09:15:02Z ERROR auth-service: upstream token-validator timeout after 5000ms\n"
                        "2026-04-30T09:15:02Z ERROR auth-service: upstream token-validator timeout after 5000ms\n"
                        "2026-04-30T09:15:03Z ERROR auth-service: upstream token-validator timeout after 5000ms\n"
                        + (
                            "2026-04-30T09:15:04Z INFO  auth-service: routine health-check OK — "
                            "pid=7423 mem=142MB goroutines=18\n"
                        )
                        * 80
                        + "2026-04-30T09:15:05Z ERROR auth-service: circuit breaker OPEN on token-validator after 3 consecutive failures\n"
                        "2026-04-30T09:15:06Z WARN  auth-service: falling back to cached token validation — cache_size=512 ttl=40s\n"
                        + (
                            "2026-04-30T09:15:07Z INFO  auth-service: routine health-check OK — "
                            "pid=7423 mem=143MB goroutines=18\n"
                        )
                        * 80
                        + "2026-04-30T09:16:00Z ERROR auth-service: cache TTL expired; 0 valid tokens remain\n"
                        "2026-04-30T09:16:01Z ERROR auth-service: all auth paths failing; returning 504 to upstream gateway\n"
                        "2026-04-30T09:16:01Z ERROR auth-service: TimeoutError — token-validator did not respond within SLA\n"
                    ),
                }
            ],
        },
        {
            "role": "assistant",
            "content": (
                "The logs reveal a cascading failure starting at 09:15:02 UTC. "
                "The auth service's upstream token-validator began timing out after 5000ms, "
                "which immediately triggered the circuit breaker after just 3 consecutive failures. "
                "Once the circuit breaker opened, the service fell back to cached token validation "
                "— but the cache TTL expired at 09:16:00 UTC, leaving zero valid tokens available. "
                "At that point all authentication paths failed and the service started returning "
                "504 to the upstream gateway. "
                "\n\n"
                "The 160 routine health-check lines between errors are normal heartbeat noise "
                "that visually obscures the real failure sequence. "
                "The key signal is the token-validator timeout, which cascaded through circuit "
                "breaker open → cache expiry → total outage in under 60 seconds. "
                "\n\n"
                "Possible root causes for the token-validator becoming unresponsive: a recent "
                "deploy that changed its timeout budget, a network partition between the auth "
                "service pods and the token-validator pods, or the token-validator itself being "
                "overloaded by a traffic spike. Let me look at the auth service source to "
                "understand the exact timeout and circuit breaker configuration."
            ),
        },
        {
            "role": "user",
            "content": (
                "Good analysis. Let's look at the auth service source — specifically the "
                "part that calls the token validator. I want to understand the timeout value "
                "and the circuit breaker failure threshold."
            ),
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Reading auth/service.py to inspect the token validator call "
                        "and circuit breaker configuration."
                    ),
                },
                {
                    "type": "tool_use",
                    "id": "tu_e1_02",
                    "name": "read_file",
                    "input": {"path": "auth/service.py"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_e1_02",
                    "content": (
                        "```python\n"
                        "# auth/service.py\n"
                        "import os\n"
                        "import httpx\n"
                        "from circuitbreaker import circuit\n"
                        "\n"
                        "TOKEN_VALIDATOR_URL = os.environ.get(\n"
                        "    'TOKEN_VALIDATOR_URL', 'http://token-validator:8080'\n"
                        ")\n"
                        "# Changed from 2000ms to 5000ms in deploy 2026-04-30 09:00\n"
                        "REQUEST_TIMEOUT_MS = int(os.environ.get('REQUEST_TIMEOUT_MS', '5000'))\n"
                        "CB_FAILURE_THRESHOLD = int(os.environ.get('CB_FAILURE_THRESHOLD', '3'))\n"
                        "CB_RECOVERY_TIMEOUT  = int(os.environ.get('CB_RECOVERY_TIMEOUT',  '30'))\n"
                        "TOKEN_CACHE_TTL      = int(os.environ.get('TOKEN_CACHE_TTL',      '40'))\n"
                        "\n"
                        "@circuit(\n"
                        "    failure_threshold=CB_FAILURE_THRESHOLD,\n"
                        "    recovery_timeout=CB_RECOVERY_TIMEOUT,\n"
                        ")\n"
                        "async def validate_token(token: str) -> dict:\n"
                        "    async with httpx.AsyncClient(\n"
                        "        timeout=REQUEST_TIMEOUT_MS / 1000\n"
                        "    ) as client:\n"
                        "        resp = await client.post(\n"
                        "            TOKEN_VALIDATOR_URL + '/validate',\n"
                        "            json={'token': token},\n"
                        "        )\n"
                        "        resp.raise_for_status()\n"
                        "        return resp.json()\n"
                        "```"
                    ),
                }
            ],
        },
        {
            "role": "assistant",
            "content": (
                "The source confirms the problem. CB_FAILURE_THRESHOLD is 3 and TOKEN_CACHE_TTL "
                "is only 40 seconds — shorter than CB_RECOVERY_TIMEOUT of 30 seconds plus any "
                "realistic recovery time. This means there's a window where the circuit breaker "
                "is still open when the cache expires, producing a complete outage. "
                "The REQUEST_TIMEOUT_MS was increased (not decreased) in the 09:00 deploy, "
                "so the longer timeout isn't the trigger — the token-validator must have "
                "genuinely stopped responding."
            ),
        },
        {
            "role": "user",
            "content": "What env-var changes will make this more resilient without a code deploy?",
        },
        {
            "role": "assistant",
            "content": (
                "Two configmap changes via kubectl: set CB_FAILURE_THRESHOLD to 5 (reduces "
                "nuisance trips on transient blips) and TOKEN_CACHE_TTL to 120 seconds "
                "(longer than any plausible circuit-breaker recovery window). Both are read "
                "at startup so a rolling restart will pick them up with no code change."
            ),
        },
    ])

    # ── Episode 2: file reads (payments/gateway.py, ConnectionError) ──────
    msgs.extend([
        {
            "role": "user",
            "content": (
                "Switching to the payments team's problem. They're reporting ConnectionError "
                "exceptions from payment-gateway when it tries to reach the card processor. "
                "Error rate spiked to 12% over the last hour. Can you read the payment "
                "gateway source to understand the connection handling?"
            ),
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "Reading payments/gateway.py to understand the connection handling.",
                },
                {
                    "type": "tool_use",
                    "id": "tu_e2_01",
                    "name": "read_file",
                    "input": {"path": "payments/gateway.py"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_e2_01",
                    "content": (
                        "```python\n"
                        "# payments/gateway.py\n"
                        "import time\n"
                        "import httpx\n"
                        "from payments.config import CARD_PROCESSOR_URL, MAX_RETRIES, CONNECT_TIMEOUT\n"
                        "\n"
                        "# Shared connection pool — size is static; no back-pressure mechanism.\n"
                        "# Under load spikes the pool exhausts before retries drain.\n"
                        "_client = httpx.Client(\n"
                        "    base_url=CARD_PROCESSOR_URL,\n"
                        "    limits=httpx.Limits(\n"
                        "        max_connections=10,\n"
                        "        max_keepalive_connections=5,\n"
                        "    ),\n"
                        "    timeout=httpx.Timeout(\n"
                        "        connect=CONNECT_TIMEOUT,\n"
                        "        read=30.0,\n"
                        "        write=10.0,\n"
                        "    ),\n"
                        ")\n"
                        "\n"
                        "def charge_card(amount: int, token: str) -> dict:\n"
                        "    for attempt in range(MAX_RETRIES):\n"
                        "        try:\n"
                        "            resp = _client.post(\n"
                        "                '/charge',\n"
                        "                json={'amount': amount, 'token': token},\n"
                        "            )\n"
                        "            resp.raise_for_status()\n"
                        "            return resp.json()\n"
                        "        except httpx.ConnectError as exc:\n"
                        "            if attempt == MAX_RETRIES - 1:\n"
                        "                raise\n"
                        "            # no backoff — can amplify load on struggling processor\n"
                        "            continue\n"
                        "```"
                    ),
                }
            ],
        },
        {
            "role": "assistant",
            "content": (
                "Several issues jump out from the gateway code. "
                "The connection pool is severely undersized: max_connections=10 with no "
                "back-pressure means that under load the pool exhausts and new requests "
                "immediately throw ConnectError. The retry loop has no exponential backoff "
                "— it retries MAX_RETRIES times in a tight loop, which can amplify load on "
                "an already-struggling card processor rather than giving it time to recover. "
                "\n\n"
                "The static _client singleton is created at import time with config values "
                "baked in, so any pool-size change requires a process restart. In a "
                "high-throughput scenario, 10 connections to a single card processor endpoint "
                "is almost always insufficient — a typical payment flow involves at least "
                "one auth + one capture HTTP round-trip, meaning peak throughput is bounded "
                "to 5 concurrent checkouts on this pool. "
                "\n\n"
                "Let me read the config file to see the actual numeric values for "
                "MAX_RETRIES and CONNECT_TIMEOUT so I can quantify the full picture."
            ),
        },
        {
            "role": "user",
            "content": "Yes, check payments/config.py for the timeout values and retry count.",
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "Reading payments/config.py for connection timeout and retry config.",
                },
                {
                    "type": "tool_use",
                    "id": "tu_e2_02",
                    "name": "read_file",
                    "input": {"path": "payments/config.py"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_e2_02",
                    "content": (
                        "```python\n"
                        "# payments/config.py\n"
                        "import os\n"
                        "\n"
                        "CARD_PROCESSOR_URL = os.environ.get(\n"
                        "    'CARD_PROCESSOR_URL', 'https://api.cardprocessor.example.com'\n"
                        ")\n"
                        "MAX_RETRIES    = int(os.environ.get('MAX_RETRIES',    '3'))\n"
                        "# Reduced from 5.0s to 2.0s in cost-cutting exercise 2026-03-14\n"
                        "CONNECT_TIMEOUT = float(os.environ.get('CONNECT_TIMEOUT', '2.0'))\n"
                        "# TODO(INFRA-2847): expose pool size as POOL_MAX_CONNECTIONS env var\n"
                        "```"
                    ),
                }
            ],
        },
        {
            "role": "assistant",
            "content": (
                "The comment is the smoking gun: CONNECT_TIMEOUT was reduced from 5.0s to "
                "2.0s in a cost-cutting exercise on 2026-03-14. The card processor's SLA "
                "requires more than 2 seconds to establish a TLS connection under load, "
                "especially cross-region. Combined with max_connections=10 and retry without "
                "backoff, this creates a two-front problem: connections time out faster than "
                "they should, and retries amplify load rather than absorbing it."
            ),
        },
        {
            "role": "user",
            "content": "What's the minimum change to stop the connection errors immediately?",
        },
        {
            "role": "assistant",
            "content": (
                "Two changes: restore CONNECT_TIMEOUT to 5.0 via environment variable (zero "
                "downtime — just update the configmap and roll the pods), and add "
                "`time.sleep(2 ** attempt)` before the `continue` in the retry loop "
                "(one-line code change). The pool size increase (INFRA-2847) is the right "
                "long-term fix but it requires the client singleton to be refactored first."
            ),
        },
    ])

    # ── Episode 3: error analysis (orders/api.py, AssertionError) ─────────
    msgs.extend([
        {
            "role": "user",
            "content": (
                "Now the orders team has a separate problem. Order creation is failing "
                "with AssertionError in production. The error started exactly 22 minutes "
                "ago and is affecting about 8% of new order requests. Can you grep the "
                "orders service logs for the AssertionError traceback?"
            ),
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "Grepping orders service logs for AssertionError tracebacks.",
                },
                {
                    "type": "tool_use",
                    "id": "tu_e3_01",
                    "name": "grep_logs",
                    "input": {
                        "service": "orders-api",
                        "pattern": "AssertionError|Traceback",
                        "since": "30m",
                    },
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_e3_01",
                    "content": (
                        "Traceback (most recent call last):\n"
                        "  File '/app/orders/api.py', line 214, in create_order\n"
                        "    validated = _validate_line_items(items)\n"
                        "  File '/app/orders/validation.py', line 89, in _validate_line_items\n"
                        "    assert all(item['qty'] > 0 for item in items), 'qty must be positive'\n"
                        "AssertionError: qty must be positive\n"
                        "\n"
                        "Traceback (most recent call last):\n"
                        "  File '/app/orders/api.py', line 214, in create_order\n"
                        "    validated = _validate_line_items(items)\n"
                        "  File '/app/orders/validation.py', line 89, in _validate_line_items\n"
                        "    assert all(item['qty'] > 0 for item in items), 'qty must be positive'\n"
                        "AssertionError: qty must be positive\n"
                        "\n"
                        "Traceback (most recent call last):\n"
                        "  File '/app/orders/api.py', line 214, in create_order\n"
                        "    validated = _validate_line_items(items)\n"
                        "  File '/app/orders/validation.py', line 89, in _validate_line_items\n"
                        "    assert all(item['qty'] > 0 for item in items), 'qty must be positive'\n"
                        "AssertionError: qty must be positive\n"
                    ),
                }
            ],
        },
        {
            "role": "assistant",
            "content": (
                "The traceback is clear: the assertion `all(item['qty'] > 0 for item in items)` "
                "is failing in `_validate_line_items`. Some order creation requests arrive "
                "with `qty` values that are zero or negative. "
                "\n\n"
                "The interesting question is why this started 22 minutes ago. This validation "
                "has presumably always been present — so either: (a) a frontend change is "
                "now submitting zero-quantity items; (b) a new client integration is sending "
                "malformed requests; or (c) a deploy introduced a regression in how the API "
                "layer parses or defaults the quantity field. The 8% error rate suggests a "
                "specific flow or client rather than a systemic regression. "
                "\n\n"
                "Let me read the orders API to trace the input path from the HTTP request "
                "body through to the validation call — there may be a type coercion issue "
                "where a missing field is defaulting to zero."
            ),
        },
        {
            "role": "user",
            "content": "Let's look at the orders API code to trace the create_order input path.",
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "Reading orders/api.py to trace the create_order input path.",
                },
                {
                    "type": "tool_use",
                    "id": "tu_e3_02",
                    "name": "read_file",
                    "input": {"path": "orders/api.py"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_e3_02",
                    "content": (
                        "```python\n"
                        "# orders/api.py  (lines 200–225)\n"
                        "@router.post('/orders')\n"
                        "def create_order(payload: dict) -> dict:\n"
                        "    items = [\n"
                        "        {\n"
                        "            'product_id': item.get('product_id'),\n"
                        "            # CHANGED in commit a3f8b2c: was item['quantity']\n"
                        "            # Default of 0 causes AssertionError downstream\n"
                        "            'qty':   item.get('quantity', 0),\n"
                        "            'price': item.get('price'),\n"
                        "        }\n"
                        "        for item in payload.get('line_items', [])\n"
                        "    ]\n"
                        "    validated = _validate_line_items(items)\n"
                        "    return _persist_order(validated)\n"
                        "```"
                    ),
                }
            ],
        },
        {
            "role": "assistant",
            "content": (
                "Found the regression. Commit `a3f8b2c` changed `item['quantity']` to "
                "`item.get('quantity', 0)`. The intent was to avoid KeyError when `quantity` "
                "is absent — but `0` is the wrong default because the downstream assertion "
                "rejects non-positive values. The result: any request that omits `quantity` "
                "now causes a 500 AssertionError instead of a 400 validation error. "
                "\n\n"
                "The safest fix is to change the default from `0` to `None`, then update "
                "_validate_line_items to check `item['qty'] is None or item['qty'] <= 0` "
                "and raise an HTTPException(422) with a descriptive message. This is "
                "backward-compatible: clients that send quantity continue to work, and "
                "clients that omit it now get a clear 422 with a field-level error message."
            ),
        },
        {
            "role": "user",
            "content": "Agreed. What's the one-line fix to stop the 500s right now?",
        },
        {
            "role": "assistant",
            "content": (
                "Change `item.get('quantity', 0)` to `item.get('quantity')` — removing the "
                "bad default. This makes the `qty` value `None` for missing quantities, and "
                "the existing assertion `qty > 0` will throw a TypeError rather than a silent "
                "zero. That's still a 500, but it's a different code path that can be caught "
                "and converted to a 422 in a follow-up commit."
            ),
        },
    ])

    # ── Episode 4: fix iteration (infra/k8s.yaml, KeyError) ──────────────
    msgs.extend([
        {
            "role": "user",
            "content": (
                "The infra team is reporting that the new k8s deployment for the reporting "
                "service keeps crashing on startup with a KeyError. Pod logs show it cannot "
                "find an expected environment variable. Can you read the k8s manifest to "
                "identify what's missing?"
            ),
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "Reading infra/k8s.yaml to check the deployment environment configuration.",
                },
                {
                    "type": "tool_use",
                    "id": "tu_e4_01",
                    "name": "read_file",
                    "input": {"path": "infra/k8s.yaml"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_e4_01",
                    "content": (
                        "```yaml\n"
                        "apiVersion: apps/v1\n"
                        "kind: Deployment\n"
                        "metadata:\n"
                        "  name: reporting-service\n"
                        "  namespace: production\n"
                        "spec:\n"
                        "  replicas: 3\n"
                        "  template:\n"
                        "    spec:\n"
                        "      containers:\n"
                        "      - name: reporting\n"
                        "        image: reporting-service:v1.4.2\n"
                        "        env:\n"
                        "        - name: DATABASE_URL\n"
                        "          valueFrom:\n"
                        "            secretKeyRef:\n"
                        "              name: reporting-secrets\n"
                        "              key: database_url\n"
                        "        - name: REDIS_URL\n"
                        "          valueFrom:\n"
                        "            secretKeyRef:\n"
                        "              name: reporting-secrets\n"
                        "              key: redis_url\n"
                        "        # MISSING: ANALYTICS_API_KEY added in v1.4.2 but absent from secrets\n"
                        "```"
                    ),
                }
            ],
        },
        {
            "role": "assistant",
            "content": (
                "The manifest comment tells the story: reporting-service v1.4.2 added a "
                "dependency on ANALYTICS_API_KEY but this environment variable was never "
                "added to the `reporting-secrets` Kubernetes Secret or the Deployment spec. "
                "\n\n"
                "When the pod starts and the service calls `os.environ['ANALYTICS_API_KEY']` "
                "(or equivalent), it raises a KeyError because the env var is not mounted. "
                "The fix requires two sequential steps: first, add the analytics API key to "
                "the `reporting-secrets` Secret; second, add the corresponding env entry to "
                "the Deployment manifest. The sequencing is critical — updating the manifest "
                "before the secret exists will cause the pod to fail to start with a "
                "CreateContainerConfigError rather than a KeyError, which is harder to debug. "
                "\n\n"
                "Let me add the missing env entry to the manifest now, with a placeholder for "
                "the secret key name that the infra team will fill in."
            ),
        },
        {
            "role": "user",
            "content": "Please add the ANALYTICS_API_KEY env entry to the manifest.",
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "Adding the ANALYTICS_API_KEY environment variable to infra/k8s.yaml.",
                },
                {
                    "type": "tool_use",
                    "id": "tu_e4_02",
                    "name": "edit_file",
                    "input": {
                        "path": "infra/k8s.yaml",
                        "old": (
                            "        # MISSING: ANALYTICS_API_KEY added in v1.4.2 but absent from secrets"
                        ),
                        "new": (
                            "        - name: ANALYTICS_API_KEY\n"
                            "          valueFrom:\n"
                            "            secretKeyRef:\n"
                            "              name: reporting-secrets\n"
                            "              key: analytics_api_key"
                        ),
                    },
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_e4_02",
                    "content": "File updated successfully. Diff applied to infra/k8s.yaml.",
                }
            ],
        },
        {
            "role": "assistant",
            "content": (
                "The manifest is updated. Here's the block that was added:\n"
                "\n"
                "```yaml\n"
                "        - name: ANALYTICS_API_KEY\n"
                "          valueFrom:\n"
                "            secretKeyRef:\n"
                "              name: reporting-secrets\n"
                "              key: analytics_api_key\n"
                "```\n"
                "\n"
                "Before applying this manifest, the infra team must patch the secret:\n"
                "```bash\n"
                "kubectl create secret generic reporting-secrets \\\n"
                "  --from-literal=analytics_api_key=<actual-key> \\\n"
                "  --namespace=production --dry-run=client -o yaml | kubectl apply -f -\n"
                "```\n"
                "This patches the existing secret without recreating it, so DATABASE_URL "
                "and REDIS_URL are preserved. After the secret is updated, apply the "
                "manifest with `kubectl apply -f infra/k8s.yaml` and the pods will roll."
            ),
        },
        {
            "role": "user",
            "content": "Will a rolling restart after the secret update be enough, or do we need a full redeploy?",
        },
        {
            "role": "assistant",
            "content": (
                "A rolling restart is sufficient and is the preferred approach. Kubernetes "
                "will trigger an automatic roll when the Deployment spec changes (which it "
                "does — we added a new env entry). The default rolling update strategy "
                "(maxUnavailable=1, maxSurge=1) ensures at least 2 of the 3 replicas stay "
                "available during the restart. "
                "\n\n"
                "The sequence to follow: (1) patch the Secret with the analytics key, "
                "(2) `kubectl apply -f infra/k8s.yaml`, (3) monitor with "
                "`kubectl rollout status deployment/reporting-service -n production`. "
                "If any pod fails to start, `kubectl rollout undo` reverts immediately. "
                "A full redeploy (delete + recreate) is not needed and introduces "
                "unnecessary downtime risk."
            ),
        },
    ])

    # ── Episode 5: verification (test_checkout.py, RuntimeError) ─────────
    msgs.extend([
        {
            "role": "user",
            "content": (
                "Good. Let's run the integration tests for the checkout flow to verify "
                "none of our fixes broke anything. The checkout flow touches auth, "
                "payments, and orders — all three services we changed today."
            ),
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "Running the integration tests for the checkout flow.",
                },
                {
                    "type": "tool_use",
                    "id": "tu_e5_01",
                    "name": "run_tests",
                    "input": {
                        "path": "tests/integration/test_checkout.py",
                        "verbose": True,
                    },
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_e5_01",
                    "content": (
                        "============================= test session starts ==============================\n"
                        "platform linux -- Python 3.12.3, pytest-8.2.0, pluggy-1.5.0\n"
                        "collected 18 items\n"
                        "\n"
                        "tests/integration/test_checkout.py::test_checkout_happy_path PASSED        [  5%]\n"
                        "tests/integration/test_checkout.py::test_checkout_auth_token_valid PASSED  [ 11%]\n"
                        "tests/integration/test_checkout.py::test_checkout_payment_charge PASSED    [ 16%]\n"
                        "tests/integration/test_checkout.py::test_checkout_order_creation PASSED    [ 22%]\n"
                        "tests/integration/test_checkout.py::test_checkout_idempotency_key PASSED   [ 27%]\n"
                        "tests/integration/test_checkout.py::test_checkout_invalid_token PASSED     [ 33%]\n"
                        "tests/integration/test_checkout.py::test_checkout_payment_retry PASSED     [ 38%]\n"
                        "tests/integration/test_checkout.py::test_checkout_zero_qty_rejected PASSED [ 44%]\n"
                        "tests/integration/test_checkout.py::test_checkout_missing_qty_rejected PASSED [ 50%]\n"
                        "tests/integration/test_checkout.py::test_checkout_negative_amount PASSED   [ 55%]\n"
                        "tests/integration/test_checkout.py::test_checkout_circuit_breaker PASSED   [ 61%]\n"
                        "tests/integration/test_checkout.py::test_checkout_cache_fallback PASSED    [ 66%]\n"
                        "tests/integration/test_checkout.py::test_checkout_analytics_recorded PASSED [ 72%]\n"
                        "tests/integration/test_checkout.py::test_checkout_analytics_schema FAILED  [ 77%]\n"
                        "tests/integration/test_checkout.py::test_checkout_concurrent PASSED        [ 83%]\n"
                        "tests/integration/test_checkout.py::test_checkout_large_order PASSED       [ 88%]\n"
                        "tests/integration/test_checkout.py::test_checkout_refund_path PASSED       [ 94%]\n"
                        "tests/integration/test_checkout.py::test_checkout_rollback_on_fail PASSED  [100%]\n"
                        "\n"
                        "=================================== FAILURES ===================================\n"
                        "_________________ test_checkout_analytics_schema _________________________\n"
                        "\n"
                        "    def test_checkout_analytics_schema():\n"
                        "        event = checkout_client.complete_checkout(order_id='test-123')\n"
                        "        assert 'analytics_version' in event['metadata']\n"
                        "AssertionError: assert 'analytics_version' in "
                        "{'order_id': 'test-123', 'timestamp': '2026-04-30T09:35:00Z'}\n"
                        "\n"
                        "HINT: RuntimeError would be raised if analytics_version is None\n"
                        "===================== 1 failed, 17 passed in 42.31s ==========================\n"
                    ),
                }
            ],
        },
        {
            "role": "assistant",
            "content": (
                "17 of 18 tests pass. The one failure is `test_checkout_analytics_schema`, "
                "which expects `analytics_version` in the analytics event metadata. This "
                "field is missing from the current event payload. "
                "\n\n"
                "This is related to the ANALYTICS_API_KEY we just added — the analytics "
                "integration is newly active in v1.4.2, but the checkout service hasn't "
                "been updated to emit the version field in the event metadata. Let me read "
                "the failing test to understand the exact schema expectation."
            ),
        },
        {
            "role": "user",
            "content": "Please look at the failing test to understand what analytics_version value it expects.",
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "Reading the failing test to understand the expected analytics event schema.",
                },
                {
                    "type": "tool_use",
                    "id": "tu_e5_02",
                    "name": "read_file",
                    "input": {"path": "tests/integration/test_checkout.py"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_e5_02",
                    "content": (
                        "```python\n"
                        "# tests/integration/test_checkout.py (lines 89–104)\n"
                        "def test_checkout_analytics_schema():\n"
                        "    \"\"\"Analytics events must carry version for downstream schema validation.\"\"\"\n"
                        "    event = checkout_client.complete_checkout(order_id='test-123')\n"
                        "    metadata = event['metadata']\n"
                        "    assert 'analytics_version' in metadata, (\n"
                        "        f'analytics_version missing from metadata; got: {list(metadata.keys())}'\n"
                        "    )\n"
                        "    assert metadata['analytics_version'] == '2.0', (\n"
                        "        f\"expected '2.0', got {metadata['analytics_version']!r}\"\n"
                        "    )\n"
                        "```"
                    ),
                }
            ],
        },
        {
            "role": "assistant",
            "content": (
                "The test expects `metadata['analytics_version'] == '2.0'`. The checkout "
                "service emits analytics events but does not yet include the version field "
                "that reporting-service v1.4.2 requires. "
                "\n\n"
                "The fix is a one-line addition to the checkout service's event construction: "
                "add `'analytics_version': '2.0'` to the metadata dict. This does not "
                "require any changes to the reporting service or the k8s manifest."
            ),
        },
        {
            "role": "user",
            "content": "Great. Please summarize everything we investigated and fixed in this session.",
        },
        {
            "role": "assistant",
            "content": (
                "Here is a full summary of the five issues investigated and resolved.\n"
                "\n"
                "**Episode 1 — Auth service 504 Gateway Timeouts (TimeoutError / circuit breaker)**\n"
                "Root cause: the circuit breaker threshold (CB_FAILURE_THRESHOLD=3) fired "
                "too quickly during a transient token-validator slowdown. When the circuit "
                "opened, the cache TTL (40s) expired before the circuit recovery window (30s) "
                "completed, producing a total auth outage with no fallback path. "
                "Fix: raise CB_FAILURE_THRESHOLD to 5 and TOKEN_CACHE_TTL to 120s via "
                "Kubernetes configmap — zero-downtime rolling restart required.\n"
                "\n"
                "**Episode 2 — Payment gateway ConnectionErrors (CONNECT_TIMEOUT regression)**\n"
                "Root cause: CONNECT_TIMEOUT was reduced from 5.0s to 2.0s in a cost-cutting "
                "exercise, combined with a 10-connection pool and tight retry loops with no "
                "backoff. Under load, connections time out faster than the processor can "
                "recover, and retries amplify traffic rather than absorbing it. "
                "Fix: restore CONNECT_TIMEOUT to 5.0s via configmap; add exponential "
                "backoff to the retry loop in a follow-up code deploy; address INFRA-2847 "
                "(pool size as env var) to make the pool configurable.\n"
                "\n"
                "**Episode 3 — Orders AssertionError on zero-qty line items (commit a3f8b2c)**\n"
                "Root cause: a commit changed `item['quantity']` to `item.get('quantity', 0)`, "
                "causing requests that omit the quantity field to default to 0 and fail the "
                "`qty > 0` assertion with a 500 error instead of a 422. "
                "Fix: change the default to `None` and add explicit 422 validation in "
                "_validate_line_items; this is backward-compatible for all well-formed clients.\n"
                "\n"
                "**Episode 4 — Reporting service KeyError on startup (missing k8s env var)**\n"
                "Root cause: reporting-service v1.4.2 added a dependency on ANALYTICS_API_KEY "
                "but neither the Kubernetes Secret (reporting-secrets) nor the Deployment "
                "manifest was updated. The pod crashes on startup when the service reads "
                "the missing env var. "
                "Fix: (1) patch the Secret to add `analytics_api_key`, then (2) apply the "
                "updated manifest; pods roll automatically with the new env var mounted.\n"
                "\n"
                "**Episode 5 — Checkout integration test failure (analytics_version schema)**\n"
                "Root cause: the checkout service emits analytics events but does not include "
                "the `analytics_version: '2.0'` field that the test (written for the v1.4.2 "
                "integration) now requires. "
                "Fix: add `'analytics_version': '2.0'` to the analytics event metadata in "
                "the checkout service — one-line change, no other services affected.\n"
                "\n"
                "All five fixes are targeted and backward-compatible. No emergency rollbacks "
                "are necessary. The auth and payments fixes can be deployed immediately via "
                "configmap updates; the orders, reporting, and checkout fixes require code "
                "deploys that can be batched into the next scheduled release window."
            ),
        },
    ])

    assert len(msgs) == 50, f"Expected 50 messages, got {len(msgs)}"
    return msgs


_WORKLOADS: dict[str, Callable[[], list[dict[str, Any]]]] = {
    "rag": _rag,
    "chat": _chat,
    "agent": _agent,
    "agent_extended": _agent_extended,
}


def list_workloads() -> list[str]:
    return sorted(_WORKLOADS)


def load_workload(name: str) -> list[dict[str, Any]]:
    if name not in _WORKLOADS:
        known = ", ".join(sorted(_WORKLOADS))
        raise KeyError(f"unknown workload {name!r}; known: {known}")
    return _WORKLOADS[name]()
