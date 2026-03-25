#!/usr/bin/env python3
"""
Rate Limiter Lab — experiment.py

What this demonstrates:
  1. Global rate limiting: Redis-backed counter enforced across api1 + api2
  2. Local rate limiting failure: why per-server counters allow 2× the rate
  3. 429 responses when limit exceeded
  4. Fail-open: Redis down → requests pass through with warning
  5. Per-API-key limits: trial vs standard vs premium tiers

Run:
  docker compose up -d
  # Wait ~30s for all services to be healthy
  python experiment.py
  (or from host directly — no container needed for this script)
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

NGINX_URL = "http://localhost:8080"
API1_URL = "http://localhost:8001"
API2_URL = "http://localhost:8002"

DEFAULT_LIMIT = 10
DEFAULT_WINDOW = 10


# ── Helpers ───────────────────────────────────────────────────────────────────

def section(title: str):
    print(f"\n{'=' * 62}")
    print(f"  {title}")
    print("=" * 62)


def get(url: str, timeout: int = 5) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = {}
        return e.code, body
    except Exception as ex:
        return 0, {"error": str(ex)}


def wait_for_service(url: str, max_wait: int = 60):
    print(f"  Waiting for {url} ...")
    for i in range(max_wait):
        status, _ = get(f"{url}/health")
        if status == 200:
            print(f"  Ready after {i + 1}s")
            return
        time.sleep(1)
    raise RuntimeError(f"Service not ready: {url}")


def burst(base_url: str, n: int, api_key: str = "anonymous", delay: float = 0.0) -> dict:
    """Send n requests, return stats."""
    results = {"200": 0, "429": 0, "other": 0, "servers": defaultdict(int)}
    for _ in range(n):
        url = f"{base_url}/api/data?key={api_key}"
        status, body = get(url)
        if status == 200:
            results["200"] += 1
            results["servers"][body.get("server", "?")] += 1
        elif status == 429:
            results["429"] += 1
        else:
            results["other"] += 1
        if delay > 0:
            time.sleep(delay)
    return results


# ── Phase 1: Global rate limiting ─────────────────────────────────────────────

def phase1_global_rate_limit():
    section("Phase 1: Global Rate Limiting via Redis")

    print(f"""
  Sending {DEFAULT_LIMIT + 5} requests through Nginx (routes to api1 + api2).
  Global limit: {DEFAULT_LIMIT} requests per {DEFAULT_WINDOW}s window.
  Both api servers share the SAME Redis counter.
""")

    results = burst(NGINX_URL, DEFAULT_LIMIT + 5, api_key="anonymous")

    print(f"  {'Status':<10} {'Count':>8}  Bar")
    print(f"  {'-'*10}  {'-'*8}  {'-'*30}")
    for status in ["200", "429"]:
        bar = "#" * results[status]
        print(f"  {status:<10}  {results[status]:>8}  {bar}")

    print(f"\n  Requests served by each API server:")
    for server, count in sorted(results["servers"].items()):
        print(f"    {server}: {count}")

    print(f"""
  Expected: {DEFAULT_LIMIT} OK (200) + {5} rejected (429)
  Actual:   {results['200']} OK + {results['429']} rejected

  KEY INSIGHT: Even though requests hit BOTH api1 and api2,
  the total allowed count is still {DEFAULT_LIMIT} — not {DEFAULT_LIMIT * 2}.
  This is because both servers increment the SAME Redis key.
""")


# ── Phase 2: Local vs global rate limiting ─────────────────────────────────────

def phase2_local_vs_global():
    section("Phase 2: Why Local Rate Limiting Fails")

    print(f"""
  PROBLEM: If each server had its own in-memory counter,
  with 2 servers each allowing {DEFAULT_LIMIT} req/window,
  the effective global rate would be {DEFAULT_LIMIT * 2} req/window.

  Demonstration:
  - We send {DEFAULT_LIMIT * 2 - 2} requests split ~evenly between api1 and api2
  - LOCAL mode (hypothetical): each server allows {DEFAULT_LIMIT} → all {DEFAULT_LIMIT * 2 - 2} pass
  - GLOBAL mode (actual Redis): only {DEFAULT_LIMIT} pass total
""")

    # First, wait for current window to expire
    now = int(time.time())
    wait_secs = DEFAULT_WINDOW - (now % DEFAULT_WINDOW) + 1
    print(f"  Waiting {wait_secs}s for rate limit window to reset ...")
    time.sleep(wait_secs)

    # Send requests split between api1 and api2 directly (bypassing Nginx)
    n_each = DEFAULT_LIMIT - 1  # send n-1 to each server
    total = n_each * 2

    api1_results = burst(API1_URL, n_each, api_key="anonymous")
    api2_results = burst(API2_URL, n_each, api_key="anonymous")

    combined_ok = api1_results["200"] + api2_results["200"]
    combined_429 = api1_results["429"] + api2_results["429"]

    print(f"\n  Sent {n_each} requests to api1 directly:")
    print(f"    200 OK: {api1_results['200']},  429: {api1_results['429']}")
    print(f"\n  Sent {n_each} requests to api2 directly:")
    print(f"    200 OK: {api2_results['200']},  429: {api2_results['429']}")
    print(f"\n  Combined: {combined_ok} OK,  {combined_429} rejected")

    print(f"""
  RESULT: With global Redis counter, combined total ≤ {DEFAULT_LIMIT}.
  Once api1 uses up {DEFAULT_LIMIT} tokens, api2 requests also get 429.

  In a LOCAL-only rate limiter (no Redis):
    Each server independently counts to {DEFAULT_LIMIT} → {DEFAULT_LIMIT * 2} total allowed.
    At 100 servers: effective rate is 100× the intended limit.

  Redis Lua script ensures atomicity:
    INCR is atomic in Redis → no race condition between servers.
    Without Lua: INCR + EXPIRE are two separate commands → tiny race window.
    With Lua: both execute atomically on the Redis server.
""")


# ── Phase 3: 429 response format ──────────────────────────────────────────────

def phase3_429_response():
    section("Phase 3: 429 Response Headers")

    print("""
  Rate limit headers tell clients how to back off:
    X-RateLimit-Limit:     total allowed requests in window
    X-RateLimit-Remaining: remaining requests in current window
    Retry-After:           seconds until window resets (optional)
""")

    # Wait for fresh window
    now = int(time.time())
    wait_secs = DEFAULT_WINDOW - (now % DEFAULT_WINDOW) + 1
    print(f"  Waiting {wait_secs}s for fresh window ...")
    time.sleep(wait_secs)

    print(f"  Sending {DEFAULT_LIMIT + 2} requests to api1 directly:\n")
    print(f"  {'Req #':<6} {'Status':>8}  {'Remaining':>10}  {'Body snippet'}")
    print(f"  {'-'*6}  {'-'*8}  {'-'*10}  {'-'*30}")

    for i in range(1, DEFAULT_LIMIT + 3):
        url = f"{API1_URL}/api/data?key=anonymous"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                status = resp.status
                remaining = resp.headers.get("X-RateLimit-Remaining", "?")
                body = json.loads(resp.read())
                snippet = f"count={body.get('request_count', '?')}"
        except urllib.error.HTTPError as e:
            status = e.code
            remaining = e.headers.get("X-RateLimit-Remaining", "?")
            try:
                body = json.loads(e.read())
                snippet = body.get("error", "")[:30]
            except Exception:
                snippet = "rate_limit_exceeded"
        except Exception:
            status, remaining, snippet = 0, "?", "error"

        marker = " ← BLOCKED" if status == 429 else ""
        print(f"  {i:<6}  {status:>8}  {str(remaining):>10}  {snippet}{marker}")


# ── Phase 4: Fail-open ────────────────────────────────────────────────────────

def phase4_fail_open():
    section("Phase 4: Fail-Open Behavior (Redis Unavailable)")

    print("""
  When Redis is unreachable, the rate limiter faces a choice:
    FAIL OPEN:   allow all requests (availability over security)
    FAIL CLOSED: block all requests (security over availability)

  For most APIs: fail-open is preferred.
  For financial APIs: fail-closed may be required.

  Our api_server.py implements fail-open:
    If Redis connection fails → allow request + set X-Redis-Available: False
""")

    # Check current state
    status, body = get(f"{API1_URL}/health")
    redis_state = body.get("redis", "unknown")
    print(f"  Current Redis state (api1): {redis_state}")

    print(f"""
  To observe fail-open (manual step):
    1. Stop Redis:  docker compose stop redis
    2. Send requests: python -c "
         import urllib.request, json
         for i in range(5):
             with urllib.request.urlopen('http://localhost:8001/api/data?key=anonymous') as r:
                 body = json.loads(r.read())
                 print(f'  OK: {{body[\"server\"]}}, redis={{body[\"redis_available\"]}}')"
    3. All requests pass (fail-open), redis_available=False in response
    4. Restart Redis: docker compose start redis

  SECURITY NOTE:
  - Financial APIs (Stripe) use fail-closed to prevent abuse during outages
  - Public APIs use fail-open to avoid a Redis blip causing a user-visible outage
  - Hybrid: use a local in-memory counter as fallback (approximate, not global)
""")


# ── Phase 5: Per-API-key limits ───────────────────────────────────────────────

def phase5_per_key_limits():
    section("Phase 5: Per-API-Key Rate Limits")

    print("""
  Stripe-style per-key limits:
    key-premium:  50 req / 10s   (enterprise customers)
    key-standard: 10 req / 10s   (default)
    key-trial:     3 req / 10s   (free tier)
    anonymous:    10 req / 10s   (default)
""")

    # Wait for fresh window
    now = int(time.time())
    wait_secs = DEFAULT_WINDOW - (now % DEFAULT_WINDOW) + 1
    print(f"  Waiting {wait_secs}s for fresh rate limit windows ...")
    time.sleep(wait_secs)

    test_cases = [
        ("key-trial",    3,  5),   # limit=3, send 5 → 3 OK + 2 blocked
        ("key-standard", 10, 12),  # limit=10, send 12 → 10 OK + 2 blocked
        ("key-premium",  50, 20),  # limit=50, send 20 → all pass
    ]

    for api_key, expected_limit, n_requests in test_cases:
        results = burst(API1_URL, n_requests, api_key=api_key)
        expected_ok = min(expected_limit, n_requests)
        expected_blocked = max(0, n_requests - expected_limit)
        match_ok = results["200"] == expected_ok
        match_blocked = results["429"] == expected_blocked

        print(f"\n  API key: {api_key} (limit: {expected_limit}/window)")
        print(f"  Sent: {n_requests}  |  200 OK: {results['200']}  |  429: {results['429']}")
        print(f"  Expected: {expected_ok} OK, {expected_blocked} blocked  "
              f"[{'CORRECT' if match_ok and match_blocked else 'UNEXPECTED'}]")

    print(f"""
  How per-key limits work in Redis:
    Key format:  ratelimit:{{api_key}}:{{window_number}}
    e.g.:        ratelimit:key-trial:172345678
    Window:      math.floor(now / window_size)

  Each API key gets its own counter namespace in Redis.
  Counter auto-expires after window_size seconds (no manual cleanup).
""")


# ── Phase 6: Algorithm comparison ─────────────────────────────────────────────

def phase6_algorithm_comparison():
    section("Phase 6: Rate Limiting Algorithm Trade-offs")

    print("""
  ┌──────────────────────┬──────────────────────────────────────────────────┐
  │ Algorithm            │ Properties                                       │
  ├──────────────────────┼──────────────────────────────────────────────────┤
  │ Fixed Window Counter │ Simple. Allows 2× limit at window boundary.      │
  │                      │ Example: 10 req allowed at t=9.9s + 10 at t=10s  │
  ├──────────────────────┼──────────────────────────────────────────────────┤
  │ Sliding Window Log   │ Exact. Stores timestamp per request. High memory  │
  │                      │ (N entries per key for N requests in window)     │
  ├──────────────────────┼──────────────────────────────────────────────────┤
  │ Sliding Window       │ Approx. 2 fixed window counters + interpolation. │
  │ Counter (approx)     │ O(1) memory, < 0.003% error in practice.        │
  ├──────────────────────┼──────────────────────────────────────────────────┤
  │ Token Bucket         │ Allows bursts up to bucket size. Refills at rate  │
  │                      │ R tokens/second. More flexible than fixed window. │
  ├──────────────────────┼──────────────────────────────────────────────────┤
  │ Leaky Bucket         │ Smooths bursts: requests processed at fixed rate. │
  │                      │ Useful for outbound rate limiting (API calls out).│
  └──────────────────────┴──────────────────────────────────────────────────┘

  This lab uses Fixed Window Counter (simple, O(1) Redis INCR).
  Cloudflare uses Sliding Window Counter for their global rate limiter.
  Stripe uses Token Bucket per API key.

  Fixed window boundary problem (demonstration):
""")

    now = int(time.time())
    window = 10
    current_window_end = (now // window + 1) * window
    seconds_left = current_window_end - now

    print(f"  Current time:         {now}")
    print(f"  Current window ends:  {current_window_end} (in {seconds_left}s)")
    print(f"""
  If an attacker sends 10 requests at t={current_window_end - 1}s (end of window)
  and 10 requests at t={current_window_end + 1}s (start of next window),
  they get 20 requests in 2 seconds — double the intended rate.

  Sliding window counter fix:
    rate = prev_window_count × (remaining_time / window) + current_window_count
    This approximates a true sliding window with O(1) memory per key.
""")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    section("RATE LIMITER LAB")
    print(f"""
  Architecture:
    Client → Nginx (port 8080) → api1:8000 / api2:8000
                                      ↕
                                  Redis (shared counter)

  Rate limiting: Fixed Window Counter via Redis Lua script (atomic).
  Fail-open: if Redis is unavailable, all requests are allowed.
  Per-key limits: trial=3, standard=10, premium=50 req/10s.
""")

    wait_for_service(NGINX_URL)

    phase1_global_rate_limit()
    phase2_local_vs_global()
    phase3_429_response()
    phase4_fail_open()
    phase5_per_key_limits()
    phase6_algorithm_comparison()

    section("Lab Complete")
    print("""
  Summary:
  - Local rate limiting (per-server counters) allows N × limit at N servers
  - Redis Lua script: INCR is atomic → no race between servers or commands
  - 429 response + X-RateLimit-* headers tell clients how to back off
  - Fail-open: Redis outage allows all traffic (availability > security)
  - Per-key limits: each API key has its own Redis counter namespace
  - Fixed window boundary problem → sliding window counter for stricter control

  Next: 11-distributed-cache/ — Redis internals, eviction, and clustering
""")


if __name__ == "__main__":
    main()
