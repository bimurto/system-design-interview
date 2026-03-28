#!/usr/bin/env python3
"""
Rate Limiter Lab — experiment.py

What this demonstrates:
  1. Global rate limiting: Redis-backed counter enforced across api1 + api2
  2. Local vs global: same API key split across servers still hits one counter
  3. 429 headers: X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset,
     and Retry-After — observed request-by-request
  4. Fail-open + auto-recovery: stop Redis → requests pass through;
     restart Redis → rate limiting resumes automatically (no server restart)
  5. Per-API-key limits: trial=3, standard=10, premium=50
  6. Fixed-window boundary problem: 2× throughput attack at window rollover
  7. Sliding window counter: the interpolation formula with live numbers

Run:
  docker compose up -d
  # Wait ~60s for all services to be healthy (pip install runs on first start)
  python experiment.py
"""

import json
import subprocess
import time
import urllib.error
import urllib.request
from collections import defaultdict

NGINX_URL = "http://localhost:8080"
API1_URL  = "http://localhost:8001"
API2_URL  = "http://localhost:8002"

DEFAULT_LIMIT  = 10
DEFAULT_WINDOW = 10  # seconds


# ── Helpers ────────────────────────────────────────────────────────────────────

def section(title: str):
    print(f"\n{'=' * 66}")
    print(f"  {title}")
    print("=" * 66)


def subsection(title: str):
    print(f"\n  --- {title} ---")


def get(url: str, timeout: int = 5) -> tuple[int, dict, dict]:
    """Returns (status_code, body_dict, headers_dict)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            headers = dict(resp.headers)
            return resp.status, json.loads(resp.read()), headers
    except urllib.error.HTTPError as e:
        headers = dict(e.headers)
        try:
            body = json.loads(e.read())
        except Exception:
            body = {}
        return e.code, body, headers
    except Exception as ex:
        return 0, {"error": str(ex)}, {}


def wait_for_service(url: str, max_wait: int = 90):
    print(f"  Waiting for {url} ...")
    for i in range(max_wait):
        status, _, _ = get(f"{url}/health")
        if status == 200:
            print(f"  Ready after {i + 1}s")
            return
        time.sleep(1)
    raise RuntimeError(
        f"Service not ready after {max_wait}s: {url}\n"
        "  Run: docker compose ps   (check all services are Up/healthy)"
    )


def wait_for_window_reset(window: int = DEFAULT_WINDOW, label: str = "window"):
    """Block until the start of the next fixed window."""
    now = int(time.time())
    wait_secs = window - (now % window) + 1
    print(f"  Waiting {wait_secs}s for {label} to reset ...")
    time.sleep(wait_secs)


def burst(base_url: str, n: int, api_key: str = "anonymous",
          delay: float = 0.0) -> dict:
    """Send n requests sequentially, return aggregated stats."""
    results = {
        "200": 0, "429": 0, "other": 0,
        "servers": defaultdict(int),
        "last_remaining": None,
        "last_reset_ts": None,
    }
    for _ in range(n):
        url = f"{base_url}/api/data?key={api_key}"
        status, body, headers = get(url)
        if status == 200:
            results["200"] += 1
            results["servers"][body.get("server", "?")] += 1
        elif status == 429:
            results["429"] += 1
        else:
            results["other"] += 1
        # Python's http.client normalises header names to title-case on
        # Python 3.12+ but lowercases them on older versions.  Try both.
        results["last_remaining"] = (
            headers.get("X-Ratelimit-Remaining")
            or headers.get("x-ratelimit-remaining")
        )
        results["last_reset_ts"] = (
            headers.get("X-Ratelimit-Reset")
            or headers.get("x-ratelimit-reset")
        )
        if delay > 0:
            time.sleep(delay)
    return results


def docker_cmd(args: list[str]) -> tuple[int, str]:
    """Run a docker compose command, return (returncode, stdout+stderr)."""
    cmd = ["docker", "compose"] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, (result.stdout + result.stderr).strip()


# ── Phase 1: Global rate limiting ─────────────────────────────────────────────

def phase1_global_rate_limit():
    section("Phase 1: Global Rate Limiting via Redis")

    print(f"""
  Sending {DEFAULT_LIMIT + 5} requests through Nginx (load-balances to api1 + api2).
  Global limit: {DEFAULT_LIMIT} requests per {DEFAULT_WINDOW}s window.
  Both API servers share the SAME Redis counter — same key, same INCR.
""")

    results = burst(NGINX_URL, DEFAULT_LIMIT + 5, api_key="phase1-test")

    print(f"  {'Status':<10} {'Count':>6}  Bar")
    print(f"  {'-'*10}  {'-'*6}  {'-'*30}")
    for status in ["200", "429"]:
        bar = "#" * results[status]
        print(f"  {status:<10}  {results[status]:>6}  {bar}")

    print(f"\n  Requests served by each API server:")
    for server, count in sorted(results["servers"].items()):
        print(f"    {server}: {count}")

    print(f"""
  Expected: {DEFAULT_LIMIT} OK (200) + 5 rejected (429)
  Actual:   {results['200']} OK + {results['429']} rejected

  KEY INSIGHT: Even though requests fan out across api1 and api2,
  the total allowed is still {DEFAULT_LIMIT} — not {DEFAULT_LIMIT * 2}.
  Both servers call INCR on the same Redis key:
    ratelimit:phase1-test:<window_id>
  Redis serialises the INCRs. The Lua script's atomic execution
  ensures no two servers can both read the same counter value
  and both decide "allowed" past the limit.
""")


# ── Phase 2: Local vs global rate limiting ─────────────────────────────────────

def phase2_local_vs_global():
    section("Phase 2: Why Local Rate Limiting Fails (Concrete Demo)")

    print(f"""
  PROBLEM: With N servers each maintaining their own in-memory counter,
  a client can exhaust each server's budget independently.

  Simulation with 2 servers (api1, api2), limit={DEFAULT_LIMIT}/window:
    LOCAL model:  api1 allows {DEFAULT_LIMIT}, api2 allows {DEFAULT_LIMIT} → {DEFAULT_LIMIT * 2} total pass
    GLOBAL model: shared Redis counter → exactly {DEFAULT_LIMIT} pass total

  We send {DEFAULT_LIMIT - 1} requests DIRECTLY to api1, then {DEFAULT_LIMIT - 1} to api2.
  With global Redis both batches draw from the same budget.
""")

    wait_for_window_reset(label="rate-limit window (phase 2)")

    n_each = DEFAULT_LIMIT - 1
    api1_results = burst(API1_URL, n_each, api_key="phase2-test")
    api2_results = burst(API2_URL, n_each, api_key="phase2-test")

    combined_ok  = api1_results["200"] + api2_results["200"]
    combined_429 = api1_results["429"] + api2_results["429"]

    print(f"  Sent {n_each} requests to api1 directly:")
    print(f"    200 OK: {api1_results['200']},  429: {api1_results['429']}")
    print(f"\n  Sent {n_each} requests to api2 directly:")
    print(f"    200 OK: {api2_results['200']},  429: {api2_results['429']}")
    print(f"\n  Combined: {combined_ok} OK,  {combined_429} rejected")

    print(f"""
  RESULT: Global Redis counter — combined total ≤ {DEFAULT_LIMIT}.
  Once api1 consumes {DEFAULT_LIMIT} tokens, api2's requests get 429 too.

  If this were LOCAL-only rate limiting:
    api1 sees {n_each} requests → all {n_each} pass (under its budget of {DEFAULT_LIMIT})
    api2 sees {n_each} requests → all {n_each} pass (under its budget of {DEFAULT_LIMIT})
    Combined: {n_each * 2} pass — {n_each * 2 - DEFAULT_LIMIT} more than the intended global limit

  At 100 servers: effective rate is 100× the intended limit.
  This is why Stripe, Cloudflare, and every production API use a
  centralised counter store rather than per-server memory.
""")


# ── Phase 3: 429 response format ──────────────────────────────────────────────

def phase3_429_response():
    section("Phase 3: 429 Response Headers — Request-by-Request")

    print("""
  RFC 6585 (429 Too Many Requests) mandates these headers:
    X-RateLimit-Limit:     total quota for this window
    X-RateLimit-Remaining: remaining requests (0 on blocked requests)
    X-RateLimit-Reset:     Unix timestamp when the window resets
    Retry-After:           seconds until client may retry (on 429 only)

  Clients use Retry-After to implement precise backoff instead of
  guessing with exponential backoff — this dramatically reduces
  thundering-herd retries at the window boundary.
""")

    wait_for_window_reset(label="rate-limit window (phase 3)")

    n = DEFAULT_LIMIT + 3
    print(f"  Sending {n} requests to api1. Watch Remaining count down:\n")
    print(f"  {'Req':>4}  {'Status':>6}  {'Remaining':>10}  {'Reset in':>9}  Note")
    print(f"  {'---':>4}  {'------':>6}  {'---------':>10}  {'--------':>9}  ----")

    for i in range(1, n + 1):
        url = f"{API1_URL}/api/data?key=phase3-test"
        # Capture time immediately before the request for accurate "reset in" math
        req_time = int(time.time())
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                status    = resp.status
                headers   = dict(resp.headers)
                body      = json.loads(resp.read())
                remaining = headers.get("X-Ratelimit-Remaining", "?")
                reset_ts  = headers.get("X-Ratelimit-Reset", "?")
                snippet   = f"count={body.get('request_count', '?')}"
        except urllib.error.HTTPError as e:
            status    = e.code
            headers   = dict(e.headers)
            remaining = headers.get("X-Ratelimit-Remaining", "?")
            reset_ts  = headers.get("X-Ratelimit-Reset", "?")
            retry     = headers.get("Retry-After", "?")
            try:
                body    = json.loads(e.read())
                snippet = f"retry_after={retry}s"
            except Exception:
                snippet = "rate_limit_exceeded"
        except Exception:
            status, remaining, reset_ts, snippet = 0, "?", "?", "error"

        # Use the per-request timestamp so "reset in" stays accurate even
        # when the loop takes several seconds to complete.
        try:
            reset_in = int(reset_ts) - req_time
            reset_str = f"{reset_in:>7}s"
        except Exception:
            reset_str = "       ?"

        marker = " <- BLOCKED" if status == 429 else ""
        print(f"  {i:>4}  {status:>6}  {str(remaining):>10}  {reset_str}  {snippet}{marker}")

    print(f"""
  OBSERVE: Remaining decrements from {DEFAULT_LIMIT - 1} down to 0.
  All blocked requests (429) carry Retry-After = seconds until reset.
  X-RateLimit-Reset is a Unix timestamp — clients convert to local time.
  Smart clients sleep exactly Retry-After seconds rather than backing off
  exponentially, which would cause a thundering herd at window reset.
""")


# ── Phase 4: Fail-open + automatic Redis recovery ─────────────────────────────

def phase4_fail_open_and_recovery():
    section("Phase 4: Fail-Open + Automatic Redis Recovery")

    print("""
  When Redis goes down, the API server faces a choice:
    FAIL OPEN:   allow all requests (availability wins)
    FAIL CLOSED: block all requests (security wins)

  Our server implements fail-open with AUTOMATIC RECOVERY:
    - While Redis is down: every request is allowed, X-Redis-Available: False
    - X-RateLimit-Remaining: "unlimited" (sentinel — rate limiting suspended)
    - On each request while down: silently attempts to reconnect
    - When Redis comes back: rate limiting resumes immediately
    - No server restart required — recovery is transparent

  This matches Stripe's production behaviour for non-financial endpoints.
""")

    # Verify Redis is up first
    status, body, _ = get(f"{API1_URL}/health")
    print(f"  Current api1 health: {body}")

    # ---- Stop Redis ----
    subsection("Stopping Redis")
    rc, out = docker_cmd(["stop", "redis"])
    print(f"  docker compose stop redis → rc={rc}")
    time.sleep(2)  # give the server time to detect the failure

    print(f"\n  Sending 5 requests while Redis is DOWN:")
    print(f"  {'Req':>4}  {'Status':>6}  {'Redis-Available':>16}  {'Remaining':>12}  Note")
    print(f"  {'---':>4}  {'------':>6}  {'---------------':>16}  {'---------':>12}  ----")
    for i in range(1, 6):
        status, body, headers = get(f"{API1_URL}/api/data?key=failopen-test")
        redis_avail = headers.get("X-Redis-Available", body.get("redis_available", "?"))
        remaining   = headers.get("X-Ratelimit-Remaining", "?")
        note = "fail-open (allowed despite Redis down)" if status == 200 else "unexpected"
        print(f"  {i:>4}  {status:>6}  {str(redis_avail):>16}  {str(remaining):>12}  {note}")

    # ---- Restart Redis ----
    subsection("Restarting Redis — observing automatic recovery")
    rc, out = docker_cmd(["start", "redis"])
    print(f"  docker compose start redis → rc={rc}")
    print(f"  Waiting 5s for Redis to be ready ...")
    time.sleep(5)

    print(f"\n  Sending 5 requests after Redis restart (no server restart!):")
    print(f"  {'Req':>4}  {'Status':>6}  {'Redis-Available':>16}  {'Remaining':>12}  Note")
    print(f"  {'---':>4}  {'------':>6}  {'---------------':>16}  {'---------':>12}  ----")
    for i in range(1, 6):
        status, body, headers = get(f"{API1_URL}/api/data?key=failopen-test")
        redis_avail = headers.get("X-Redis-Available", body.get("redis_available", "?"))
        remaining   = headers.get("X-Ratelimit-Remaining", "?")
        if status == 200 and str(redis_avail).lower() in ("true", "1"):
            note = "rate limiting RESUMED — Redis reconnected"
        elif status == 200:
            note = "still reconnecting..."
        elif status == 429:
            note = "rate limited (Redis back + counter from before outage)"
        else:
            note = f"status={status}"
        print(f"  {i:>4}  {status:>6}  {str(redis_avail):>16}  {str(remaining):>12}  {note}")

    print(f"""
  DESIGN DECISION — fail-open vs fail-closed:

    FAIL OPEN   → availability: users never see errors due to Redis blip
                  risk: abusive clients get unlimited access during outage
                  used by: GitHub, Twitter, most public APIs

    FAIL CLOSED → security: rate limit is never bypassed
                  risk: ALL traffic blocked if Redis is unavailable
                  used by: Stripe /v1/charges, financial APIs, fraud signals

    HYBRID      → local in-memory counter as fallback
                  each server enforces (limit / N) while Redis is down
                  approximates global enforcement without full fail-open
                  complexity: N must be known and kept consistent

  AUTOMATIC RECOVERY (what we just demonstrated):
    The server probes Redis on every request while in fail-open mode.
    No operator action needed — it heals itself when Redis comes back.
    Note: the "unlimited" Remaining header during fail-open is intentional —
    it signals to monitoring tools that rate limiting is suspended, rather
    than implying the client has 0 requests left.
""")


# ── Phase 5: Per-API-key limits ───────────────────────────────────────────────

def phase5_per_key_limits():
    section("Phase 5: Per-API-Key Rate Limits (Stripe-Style Tiers)")

    print("""
  Real API platforms segment limits by customer tier:

    key-premium:  50 req / 10s   (enterprise — high-volume, high-trust)
    key-standard: 10 req / 10s   (default paid tier)
    key-trial:     3 req / 10s   (free tier — motivates upgrades)
    anonymous:    10 req / 10s   (unauthenticated default)

  Redis key format: ratelimit:{api_key}:{window_number}
  Each key tier gets its own independent counter namespace.
  Counters auto-expire after the window — no background cleanup job.
""")

    wait_for_window_reset(label="rate-limit windows (phase 5)")

    test_cases = [
        ("key-trial",    3,  5),   # limit=3, send 5 → 3 OK + 2 blocked
        ("key-standard", 10, 12),  # limit=10, send 12 → 10 OK + 2 blocked
        ("key-premium",  50, 20),  # limit=50, send 20 → all 20 pass
    ]

    for api_key, expected_limit, n_requests in test_cases:
        results = burst(API1_URL, n_requests, api_key=api_key)
        expected_ok      = min(expected_limit, n_requests)
        expected_blocked = max(0, n_requests - expected_limit)
        match = results["200"] == expected_ok and results["429"] == expected_blocked
        status_str = "CORRECT" if match else "UNEXPECTED"

        print(f"\n  Key: {api_key} (limit: {expected_limit}/window)")
        print(f"    Sent {n_requests}: {results['200']} OK / {results['429']} blocked  [{status_str}]")
        print(f"    Expected:  {expected_ok} OK / {expected_blocked} blocked")

    print(f"""
  Redis key inspection (run in another terminal):
    docker compose exec redis redis-cli keys "ratelimit:*"
    docker compose exec redis redis-cli get "ratelimit:key-trial:<window_id>"

  Window ID: floor(unix_timestamp / window_seconds)
  Example at t=1720000050, window=10: window_id = floor(1720000050/10) = 172000005
  Key: ratelimit:key-trial:172000005  →  TTL: 0–10s depending on when it was created

  UPGRADE PATH: when a key changes tier, the new limit takes effect at
  the next window. No cache invalidation required — the counter is already
  namespaced by window ID. Old windows expire naturally.
""")


# ── Phase 6: Fixed-window boundary problem (live) ─────────────────────────────

def phase6_boundary_attack():
    section("Phase 6: Fixed-Window Boundary Attack (Live Demo)")

    print(f"""
  PROBLEM: Fixed-window counters allow 2× the intended rate at window boundaries.

  Attack: send L requests at t=window_end - 0.5s,
          then L requests at t=window_end + 0.5s.
  Both windows see exactly L requests → both allow → 2L pass in 1 second.

  We'll demonstrate this live by waiting for a window boundary.
""")

    window = DEFAULT_WINDOW
    now = int(time.time())
    window_end = (now // window + 1) * window
    wait_secs = window_end - now - 1  # arrive 1s before boundary
    if wait_secs < 0:
        wait_secs = window - abs(wait_secs) % window

    print(f"  Current time:      {now} (window ends at {window_end})")
    print(f"  Waiting {wait_secs:.0f}s to position at window boundary ...")
    time.sleep(max(0, wait_secs))

    api_key = "boundary-attack"
    batch_size = DEFAULT_LIMIT  # exactly the limit per window

    print(f"\n  Batch A: {batch_size} requests (end of window {window_end - window}–{window_end}):")
    results_a = {"200": 0, "429": 0}
    for i in range(batch_size):
        status, body, _ = get(f"{API1_URL}/api/data?key={api_key}")
        results_a["200" if status == 200 else "429"] += 1
        count = body.get("request_count", "?")
        marker = " <- BLOCKED" if status == 429 else ""
        print(f"    req {i+1:>2}: status={status}  count={count}{marker}")
        time.sleep(0.05)

    # Brief pause to cross the window boundary
    print(f"\n  [crossing window boundary — sleeping 1.5s]")
    time.sleep(1.5)

    new_now = int(time.time())
    new_window_end = (new_now // window + 1) * window
    print(f"\n  Batch B: {batch_size} requests (start of new window {new_window_end - window}–{new_window_end}):")
    results_b = {"200": 0, "429": 0}
    for i in range(batch_size):
        status, body, _ = get(f"{API1_URL}/api/data?key={api_key}")
        results_b["200" if status == 200 else "429"] += 1
        count = body.get("request_count", "?")
        marker = " <- BLOCKED" if status == 429 else ""
        print(f"    req {i+1:>2}: status={status}  count={count}{marker}")
        time.sleep(0.05)

    total_ok = results_a["200"] + results_b["200"]
    print(f"""
  RESULT:
    Batch A (end of window):   {results_a['200']} OK / {results_a['429']} blocked
    Batch B (start of window): {results_b['200']} OK / {results_b['429']} blocked
    Total passed: {total_ok} / {batch_size * 2} sent

  Intended limit: {DEFAULT_LIMIT}/window.
  Observed: up to {total_ok} requests passed in ~{window + 2}s — potentially {total_ok}/{DEFAULT_LIMIT:.0f}x the rate.

  FIX — Sliding window counter (Cloudflare's approach):
    rate = prev_count x (1 - elapsed_fraction) + current_count
    where elapsed_fraction = seconds_since_window_start / window_size

  At the boundary (elapsed_fraction ~= 0):
    rate ~= prev_count x 1.0 + current_count
         ~= {DEFAULT_LIMIT} x 1.0 + {DEFAULT_LIMIT} = {DEFAULT_LIMIT * 2}  -> DENIED (> {DEFAULT_LIMIT})
  This prevents the burst even at the boundary.
""")


# ── Phase 7: Sliding window formula (live calculation) ────────────────────────

def phase7_sliding_window():
    section("Phase 7: Sliding Window Counter — Live Formula")

    print("""
  The sliding window counter approximates a true sliding window using
  only 2 fixed-window counters (O(1) memory per key).

  Formula (Cloudflare, 2023):
    rate = prev_window_count x (1 - elapsed_in_current_window / window_size)
         + current_window_count

  Interpretation: the previous window's count is weighted by the fraction
  of that window still overlapping the current 1-window lookback period.

  Error vs a true sliding window: < 0.003% in Cloudflare's analysis,
  because request arrival is approximately uniform over time in aggregate.
""")

    window = DEFAULT_WINDOW
    now = int(time.time())
    elapsed = now % window
    elapsed_fraction = elapsed / window
    prev_weight = 1 - elapsed_fraction

    # Simulate realistic scenario
    prev_count    = 8
    current_count = 4
    limit         = DEFAULT_LIMIT

    approx_rate = prev_count * prev_weight + current_count
    true_minimum = current_count  # lower bound
    true_maximum = prev_count + current_count  # upper bound (fixed window)

    print(f"  Live values (this moment):")
    print(f"    now                 = {now}")
    print(f"    window_size         = {window}s")
    print(f"    elapsed_in_window   = {elapsed}s   ({elapsed_fraction:.2%} through current window)")
    print(f"    prev_window_weight  = {prev_weight:.2f}   (prev window is {prev_weight:.0%} within our lookback)")
    print()
    print(f"  Simulated counters:")
    print(f"    prev_window_count   = {prev_count}")
    print(f"    current_window_count= {current_count}")
    print(f"    limit               = {limit}")
    print()
    print(f"  Sliding window approximation:")
    print(f"    rate = {prev_count} x {prev_weight:.2f} + {current_count}")
    print(f"         = {prev_count * prev_weight:.2f} + {current_count}")
    print(f"         = {approx_rate:.2f}")
    print(f"    Decision: {'ALLOW' if approx_rate <= limit else 'DENY'} (limit={limit})")
    print()
    print(f"  Compare to fixed-window:")
    print(f"    Would allow up to {true_maximum} in the worst-case boundary attack.")
    print(f"    Sliding window bounds this to ~= {approx_rate:.1f} — much closer to {limit}.")
    print()

    print("  Redis implementation requires 2 keys per rate-limited entity:")
    print(f"    ratelimit:{{key}}:{{window_id - 1}}  -> prev_window_count (TTL = 2x window)")
    print(f"    ratelimit:{{key}}:{{window_id}}      -> current_window_count (TTL = window)")
    print()
    print("  Algorithm comparison:")
    print(f"  {'Algorithm':<26}  {'Memory/key':>12}  {'Accuracy':>10}  {'Burst?':>7}")
    print(f"  {'-'*26}  {'-'*12}  {'-'*10}  {'-'*7}")
    rows = [
        ("Fixed Window Counter",      "O(1) — 1 key",    "+-100% at boundary", "Yes"),
        ("Sliding Window Log (ZADD)", "O(N) — N=limit",  "Exact",              "No"),
        ("Sliding Window Counter",    "O(1) — 2 keys",   "<0.003% error",      "No"),
        ("Token Bucket",              "O(1) — 2 fields", "Exact avg rate",     "Yes"),
        ("Leaky Bucket",              "O(1) — 1 queue",  "Exact smoothing",    "No"),
    ]
    for name, mem, acc, burst in rows:
        print(f"  {name:<26}  {mem:>12}  {acc:>10}  {burst:>7}")

    print(f"""
  PRODUCTION CHOICES:
    Stripe:     Token Bucket — allows burst, enforces average rate
    Cloudflare: Sliding Window Counter — O(1) memory at 55M RPS
    GitHub:     Fixed Window — simple, boundary leakage acceptable
    Twitter:    Sliding Window Log — exact, low limit (15 req/15min)
""")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    section("RATE LIMITER LAB")
    print(f"""
  Architecture:
    Client -> Nginx :8080 (round-robin) -> api1:8001 / api2:8002
                                               |
                                     Redis (shared counter store)

  Rate limiting: Fixed-Window Counter via Redis Lua script (atomic INCR).
  Fail-open + auto-recovery: Redis outage allows traffic; heals on reconnect.
  Per-key limits: trial=3, standard=10, premium=50 req/10s.
  Retry-After + X-RateLimit-Reset headers on every 429 response.
""")

    wait_for_service(NGINX_URL)

    phase1_global_rate_limit()
    phase2_local_vs_global()
    phase3_429_response()
    phase4_fail_open_and_recovery()
    phase5_per_key_limits()
    phase6_boundary_attack()
    phase7_sliding_window()

    section("Lab Complete")
    print("""
  Summary of demonstrated concepts:
    1. Global Redis counter: N servers x (limit/N) = limit total — not N x limit
    2. Lua script atomicity: INCR + EXPIRE in one atomic step, no TOCTOU race
    3. Header contract: Retry-After enables precise client backoff, not guessing
    4. Fail-open + auto-recovery: Redis outage is transient, not permanent
    5. Per-key tiers: independent counter namespaces, auto-expire, no cleanup
    6. Boundary attack: fixed-window allows 2x rate; sliding window prevents it
    7. Sliding window formula: O(1) memory, <0.003% error vs true sliding window

  Useful commands:
    docker compose exec redis redis-cli keys "ratelimit:*"
    docker compose exec redis redis-cli monitor   # live command stream
    curl -s http://localhost:8001/stats | python3 -m json.tool

  Next: 11-distributed-cache/ — Redis internals, eviction, and clustering
""")


if __name__ == "__main__":
    main()
