#!/usr/bin/env python3
"""
Rate Limiting Algorithms Lab — Redis-backed implementations of all 4 algorithms.

What this demonstrates:
  1. Token Bucket — smooth rate limiting with burst tolerance
  2. Sliding Window Log — most accurate, O(requests) memory
  3. Fixed Window Counter — simplest, has boundary burst problem
  4. Sliding Window Counter — approximation balancing accuracy vs memory
  5. Comparison table: memory, accuracy, burst handling
"""

import os
import redis
import time
import threading
import sys
from collections import defaultdict

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
r = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)


# ── Algorithm Implementations ──────────────────────────────────────────────

def token_bucket(key, rate, capacity):
    """
    Returns True if request is allowed.
    rate: tokens added per second
    capacity: max tokens (burst size)
    """
    now = time.time()
    pipe = r.pipeline()
    pipe.hgetall(f"tb:{key}")
    result = pipe.execute()
    bucket = result[0]
    tokens = float(bucket.get("tokens", capacity))
    last_refill = float(bucket.get("last_refill", now))
    # Refill tokens based on elapsed time
    elapsed = now - last_refill
    tokens = min(capacity, tokens + elapsed * rate)
    if tokens >= 1:
        tokens -= 1
        allowed = True
    else:
        allowed = False
    r.hset(f"tb:{key}", mapping={"tokens": tokens, "last_refill": now})
    return allowed


def sliding_window_log(key, window_seconds, max_requests):
    """
    Most accurate. Stores timestamp of every request in a sorted set.
    Memory: O(max_requests) per key
    """
    now = time.time()
    pipe = r.pipeline()
    pipe.zremrangebyscore(f"swl:{key}", 0, now - window_seconds)
    pipe.zadd(f"swl:{key}", {str(now) + str(threading.get_ident()): now})
    pipe.zcard(f"swl:{key}")
    pipe.expire(f"swl:{key}", window_seconds)
    results = pipe.execute()
    return results[2] <= max_requests


def fixed_window(key, window_seconds, max_requests):
    """
    Simplest. Resets counter every fixed window.
    Problem: allows 2x burst at window boundary.
    """
    window_key = f"fw:{key}:{int(time.time() / window_seconds)}"
    count = r.incr(window_key)
    if count == 1:
        r.expire(window_key, window_seconds * 2)
    return count <= max_requests


def sliding_window_counter(key, window_seconds, max_requests):
    """
    Approximation using two fixed windows + weighted estimate.
    Memory: O(1) per key — just two counters.
    """
    now = time.time()
    current_window = int(now / window_seconds)
    prev_window = current_window - 1
    elapsed_in_window = now - (current_window * window_seconds)
    prev_count = int(r.get(f"swc:{key}:{prev_window}") or 0)
    current_count = r.incr(f"swc:{key}:{current_window}")
    r.expire(f"swc:{key}:{current_window}", window_seconds * 2)
    # Weighted estimate: weight previous window by how much of it still overlaps
    weight = 1 - (elapsed_in_window / window_seconds)
    estimated = prev_count * weight + current_count
    return estimated <= max_requests


# ── Helpers ────────────────────────────────────────────────────────────────

def section(title):
    print(f"\n{'=' * 65}")
    print(f"  {title}")
    print("=" * 65)


def flush_keys(prefix):
    """Delete all keys matching prefix."""
    keys = r.keys(f"{prefix}:*")
    if keys:
        r.delete(*keys)


def send_requests(algo_fn, algo_key, count, delay_between, **kwargs):
    """Send `count` requests, return (allowed, rejected, elapsed)."""
    allowed = 0
    rejected = 0
    start = time.time()
    for _ in range(count):
        if algo_fn(algo_key, **kwargs):
            allowed += 1
        else:
            rejected += 1
        if delay_between > 0:
            time.sleep(delay_between)
    elapsed = time.time() - start
    return allowed, rejected, elapsed


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    section("RATE LIMITING ALGORITHMS LAB")
    print("""
  Four algorithms, all backed by Redis:

  Token Bucket         Fixed Window Counter
  ┌──────────────┐     ┌──────────────┐
  │ ●●●●●○○○○○  │     │ count: 7    │  reset at t=10
  │ tokens=5/10  │     │ limit: 10   │
  └──────────────┘     └──────────────┘

  Sliding Window Log   Sliding Window Counter
  ┌──────────────┐     ┌──────────────────────┐
  │ [t1,t2,t3..] │     │ prev×weight + current │
  │ zset in Redis│     │ two counters only     │
  └──────────────┘     └──────────────────────┘
""")

    RATE_LIMIT = 10       # requests per second
    BURST_CAP  = 10       # token bucket capacity
    WINDOW     = 1        # 1-second window
    TOTAL_REQS = 50       # requests to send

    # Send at 2x the rate limit: one request every 50ms = 20 req/s against 10/s limit
    DELAY = 0.05

    # ── Phase 1: All 4 algorithms at 2x rate limit ─────────────────
    section("Phase 1: 200 Requests at 2x Rate Limit (20 req/s vs limit 10/s)")
    print(f"  Sending {TOTAL_REQS} requests at ~20 req/s (limit: {RATE_LIMIT}/s)\n")

    results = {}
    for name, fn, key, kwargs in [
        ("token_bucket",           token_bucket,           "p1_tb",
            {"rate": RATE_LIMIT, "capacity": BURST_CAP}),
        ("sliding_window_log",     sliding_window_log,     "p1_swl",
            {"window_seconds": WINDOW, "max_requests": RATE_LIMIT}),
        ("fixed_window",           fixed_window,           "p1_fw",
            {"window_seconds": WINDOW, "max_requests": RATE_LIMIT}),
        ("sliding_window_counter", sliding_window_counter, "p1_swc",
            {"window_seconds": WINDOW, "max_requests": RATE_LIMIT}),
    ]:
        flush_keys(key.split("_")[0])
        allowed, rejected, elapsed = send_requests(fn, key, TOTAL_REQS, DELAY, **kwargs)
        effective_rate = allowed / elapsed
        results[name] = (allowed, rejected, effective_rate)
        print(f"  {name:<28} allowed={allowed:3d}  rejected={rejected:3d}  "
              f"effective={effective_rate:.1f} req/s")

    # ── Phase 2: Fixed window boundary burst problem ───────────────
    section("Phase 2: Fixed Window Boundary Burst Problem")
    print("""
  Fixed window resets at exact second boundaries.
  Send 10 requests near the END of a window, then 10 at the START
  of the next window → 20 requests pass in ~1 second (2x the limit).

  Timeline:
    t=0.9s  [■■■■■■■■■■] ← 10 requests, window 0 (all allowed, count=10)
    t=1.0s  window resets
    t=1.1s  [■■■■■■■■■■] ← 10 more requests, window 1 (all allowed, count=10)
    Total in ~0.2s: 20 requests allowed (should be ≤10!)
""")

    # Find the next window boundary
    now = time.time()
    window_size = 1.0
    next_boundary = (int(now / window_size) + 1) * window_size
    wait_time = next_boundary - now - 0.15  # start 150ms before boundary

    if wait_time > 0:
        print(f"  Waiting {wait_time:.2f}s to align with window boundary...")
        time.sleep(wait_time)

    flush_keys("fw:boundary")
    burst_allowed = 0
    burst_times = []

    # 10 requests before boundary
    for i in range(10):
        if fixed_window("boundary_test", 1, 10):
            burst_allowed += 1
        burst_times.append(time.time())
        time.sleep(0.01)

    # Wait for boundary to pass
    now2 = time.time()
    boundary = (int(now2 / window_size) + 1) * window_size
    gap = boundary - now2
    if gap > 0:
        time.sleep(gap + 0.02)

    # 10 requests after boundary
    for i in range(10):
        if fixed_window("boundary_test", 1, 10):
            burst_allowed += 1
        burst_times.append(time.time())
        time.sleep(0.01)

    span = burst_times[-1] - burst_times[0]
    print(f"  Sent 20 requests spanning window boundary over {span:.2f}s")
    print(f"  Allowed: {burst_allowed}/20  (fixed window permits up to 2x burst!)")
    print(f"  Sliding window log would have allowed ≤10 in any 1s window.")

    # ── Phase 3: Token bucket burst demonstration ──────────────────
    section("Phase 3: Token Bucket — Burst Up to Bucket Capacity")
    print(f"""
  Token bucket (rate=5/s, capacity={BURST_CAP}):
  After 2 seconds of idle, bucket fills to {BURST_CAP} tokens.
  Then send 15 requests instantly — first {BURST_CAP} burst through, rest rejected.
""")

    flush_keys("tb:burst")
    # Let bucket fill (2 seconds idle)
    time.sleep(2.1)

    burst_results = []
    for i in range(15):
        allowed = token_bucket("burst_demo", rate=5, capacity=BURST_CAP)
        burst_results.append(allowed)

    burst_ok = sum(burst_results)
    burst_no = len(burst_results) - burst_ok
    print(f"  15 immediate requests after 2s idle (bucket filled to {BURST_CAP}):")
    print(f"  Result: " + "".join("✓" if x else "✗" for x in burst_results))
    print(f"  Allowed: {burst_ok}  Rejected: {burst_no}")
    print(f"  → Token bucket permits a burst of {BURST_CAP} followed by steady {5}/s")

    # ── Phase 4: Sliding window log accuracy ──────────────────────
    section("Phase 4: Sliding Window Log — Most Accurate, High Memory")
    print("""
  Sliding window log stores every request timestamp.
  Memory scales with requests, not just counters.
  It enforces the limit in ANY rolling 1-second window (not just aligned).
""")
    flush_keys("swl:acc")
    acc_allowed = 0
    acc_rejected = 0
    for i in range(25):
        if sliding_window_log("accuracy_test", 1, 10):
            acc_allowed += 1
        else:
            acc_rejected += 1
        time.sleep(0.04)  # 25Hz

    # Check Redis memory for swl key
    key_info = r.execute_command("DEBUG", "OBJECT", "swl:accuracy_test") if False else ""
    card = r.zcard("swl:accuracy_test")
    print(f"  25 requests at 25 req/s (limit 10/s):")
    print(f"  Allowed: {acc_allowed}  Rejected: {acc_rejected}")
    print(f"  Entries in sorted set: {card}  (memory ∝ window * rate)")
    print(f"  With 1M users × 100 req/window = 100M Redis entries — impractical at scale")

    # ── Phase 5: Comparison table ──────────────────────────────────
    section("Phase 5: Algorithm Comparison Table")
    print(f"""
  {'Algorithm':<28} {'Memory':<22} {'Accuracy':<20} {'Burst Handling'}
  {'─'*28} {'─'*22} {'─'*20} {'─'*20}
  {'Token Bucket':<28} {'O(1) — 2 fields':<22} {'Good':<20} {'Allows up to capacity'}
  {'Sliding Window Log':<28} {'O(requests/window)':<22} {'Exact':<20} {'No bursts allowed'}
  {'Fixed Window Counter':<28} {'O(1) — 1 counter':<22} {'Poor at boundary':<20} {'2x burst at boundary'}
  {'Sliding Window Counter':<28} {'O(1) — 2 counters':<22} {'~99% accurate':<20} {'Approximate — safe'}

  When to use each:
  ┌─────────────────────────────────────────────────────────────────┐
  │ Token Bucket        → APIs that allow short bursts (most common) │
  │ Sliding Window Log  → High-value ops where accuracy is critical   │
  │ Fixed Window        → Simple internal quotas (not user-facing)    │
  │ Sliding Window Ctr  → High-traffic APIs (Nginx, Redis rate limit) │
  └─────────────────────────────────────────────────────────────────┘

  Distributed rate limiting:
  • Single Redis node: all 4 work, ~1ms overhead per check
  • Redis Cluster: use key hashing to keep one user on one shard
  • Lua scripts: make token_bucket atomic (EVALSHA)
  • Redis 7 built-in: SET key INCR EX — use for simple fixed window
  • Nginx limit_req_zone: sliding window counter in shared memory

  Next: ../10-cdn-and-edge/
""")


if __name__ == "__main__":
    main()
