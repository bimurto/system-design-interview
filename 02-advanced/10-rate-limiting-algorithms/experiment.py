#!/usr/bin/env python3
"""
Rate Limiting Algorithms Lab — Redis-backed implementations of all 4 algorithms.

What this demonstrates:
  1. Token Bucket     — atomic Lua-based burst-tolerant rate limiting
  2. Sliding Window Log — most accurate, O(requests) memory
  3. Fixed Window Counter — simplest, has boundary burst problem
  4. Sliding Window Counter — approximation balancing accuracy vs memory
  5. Fixed window boundary burst exploit (shows 2x allowed)
  6. Token bucket burst fill (idle → fire 15 requests)
  7. Sliding window log memory cost vs fixed-window O(1)
  8. Atomic vs non-atomic token bucket (race condition demo)
  9. Comparison table: memory, accuracy, burst handling
"""

import os
import redis
import time
import threading

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
r = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)


# ── Lua script for atomic token bucket ─────────────────────────────────────
# Using EVALSHA ensures the read-modify-write is a single atomic operation.
# Without this, two concurrent requests can both read tokens=1, both allow
# themselves, and both write tokens=0 — consuming only 1 token for 2 requests.
TOKEN_BUCKET_LUA = """
local key        = KEYS[1]
local rate       = tonumber(ARGV[1])
local capacity   = tonumber(ARGV[2])
local now        = tonumber(ARGV[3])

local bucket     = redis.call('HGETALL', key)
local tokens     = capacity
local last_refill = now

if #bucket > 0 then
    for i = 1, #bucket, 2 do
        if bucket[i] == 'tokens'      then tokens      = tonumber(bucket[i+1]) end
        if bucket[i] == 'last_refill' then last_refill = tonumber(bucket[i+1]) end
    end
end

local elapsed = now - last_refill
tokens = math.min(capacity, tokens + elapsed * rate)

if tokens >= 1 then
    tokens = tokens - 1
    redis.call('HSET', key, 'tokens', tokens, 'last_refill', now)
    redis.call('EXPIRE', key, math.ceil(capacity / rate) + 1)
    return 1
else
    redis.call('HSET', key, 'tokens', tokens, 'last_refill', now)
    redis.call('EXPIRE', key, math.ceil(capacity / rate) + 1)
    return 0
end
"""

# Register the Lua script once and reuse via SHA for efficiency
_tb_sha = None

def _get_tb_sha():
    global _tb_sha
    if _tb_sha is None:
        _tb_sha = r.script_load(TOKEN_BUCKET_LUA)
    return _tb_sha


# ── Algorithm Implementations ──────────────────────────────────────────────

def token_bucket(key, rate, capacity):
    """
    Atomic token bucket via Lua script.
    rate:     tokens added per second
    capacity: max tokens (burst size)
    The Lua script runs atomically in Redis — no race conditions.
    """
    now = time.time()
    sha = _get_tb_sha()
    result = r.evalsha(sha, 1, f"tb:{key}", rate, capacity, now)
    return result == 1


def token_bucket_racy(key, rate, capacity):
    """
    NON-ATOMIC token bucket — intentionally broken for the race demo.
    Separate read and write allow concurrent requests to both see tokens=1
    and both grant themselves access, bypassing the rate limit.
    """
    now = time.time()
    bucket = r.hgetall(f"tb_racy:{key}")
    tokens = float(bucket.get("tokens", capacity))
    last_refill = float(bucket.get("last_refill", now))
    elapsed = now - last_refill
    tokens = min(capacity, tokens + elapsed * rate)
    time.sleep(0.002)  # simulate processing gap — widens the race window
    if tokens >= 1:
        tokens -= 1
        r.hset(f"tb_racy:{key}", mapping={"tokens": tokens, "last_refill": now})
        return True
    r.hset(f"tb_racy:{key}", mapping={"tokens": tokens, "last_refill": now})
    return False


def sliding_window_log(key, window_seconds, max_requests):
    """
    Most accurate. Stores timestamp of every request in a Redis sorted set.
    Implemented as a Lua script so the trim+add+count is atomic.
    Memory: O(requests in window) per key — grows linearly with traffic.
    """
    now = time.time()
    lua = """
local key      = KEYS[1]
local cutoff   = tonumber(ARGV[1])
local now      = tonumber(ARGV[2])
local member   = ARGV[3]
local limit    = tonumber(ARGV[4])
local ttl      = tonumber(ARGV[5])
redis.call('ZREMRANGEBYSCORE', key, 0, cutoff)
redis.call('ZADD', key, now, member)
local count = redis.call('ZCARD', key)
redis.call('EXPIRE', key, ttl)
if count <= limit then return 1 else return 0 end
"""
    member = f"{now}:{threading.get_ident()}"
    result = r.eval(lua, 1, f"swl:{key}",
                    now - window_seconds, now, member,
                    max_requests, int(window_seconds) + 1)
    return result == 1


def fixed_window(key, window_seconds, max_requests):
    """
    Simplest. Resets counter at fixed time boundaries.
    Vulnerability: a client can send 2x the limit by straddling a boundary.
    Implementation: single INCR + EXPIRE — inherently atomic per request.
    """
    window_key = f"fw:{key}:{int(time.time() / window_seconds)}"
    pipe = r.pipeline()
    pipe.incr(window_key)
    pipe.expire(window_key, int(window_seconds * 2))
    results = pipe.execute()
    count = results[0]
    return count <= max_requests


def sliding_window_counter(key, window_seconds, max_requests):
    """
    Approximation using two fixed windows + weighted estimate.
    Memory: O(1) per key — just two counters.

    The weighted formula is:
        estimate = prev_count × (1 − elapsed/window) + current_count

    This is ~99% accurate because the linear interpolation slightly
    over- or under-estimates depending on request distribution within
    the previous window.
    """
    now = time.time()
    current_window = int(now / window_seconds)
    prev_window = current_window - 1
    elapsed_in_window = now - (current_window * window_seconds)
    weight = 1.0 - (elapsed_in_window / window_seconds)

    lua = """
local prev_key     = KEYS[1]
local current_key  = KEYS[2]
local weight       = tonumber(ARGV[1])
local limit        = tonumber(ARGV[2])
local ttl          = tonumber(ARGV[3])
local prev_count   = tonumber(redis.call('GET', prev_key) or 0)
local current_count = redis.call('INCR', current_key)
redis.call('EXPIRE', current_key, ttl)
local estimated = prev_count * weight + current_count
if estimated <= limit then return 1 else return 0 end
"""
    result = r.eval(lua, 2,
                    f"swc:{key}:{prev_window}",
                    f"swc:{key}:{current_window}",
                    weight, max_requests, int(window_seconds * 2))
    return result == 1


# ── Helpers ────────────────────────────────────────────────────────────────

def section(title):
    print(f"\n{'=' * 65}")
    print(f"  {title}")
    print("=" * 65)


def flush_keys(*prefixes):
    """Delete all Redis keys matching any of the given prefixes."""
    for prefix in prefixes:
        keys = r.keys(f"{prefix}:*")
        if keys:
            r.delete(*keys)


def send_requests(algo_fn, algo_key, count, delay_between, **kwargs):
    """Send `count` requests sequentially, return (allowed, rejected, elapsed)."""
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

  Token Bucket              Fixed Window Counter
  ┌──────────────────┐      ┌────────────────────┐
  │ ●●●●●○○○○○       │      │ count: 7 / 10      │
  │ tokens=5, cap=10 │      │ resets at t=10s    │
  └──────────────────┘      └────────────────────┘

  Sliding Window Log        Sliding Window Counter
  ┌──────────────────┐      ┌──────────────────────────────┐
  │ zset: [t1,t2,t3] │      │ prev×(1−elapsed/w) + current │
  │ every timestamp  │      │ two counters only            │
  └──────────────────┘      └──────────────────────────────┘

  Key: all checks are atomic (Lua scripts) to prevent race conditions.
""")

    RATE_LIMIT = 10    # requests per second
    BURST_CAP  = 10    # token bucket capacity
    WINDOW     = 1     # 1-second window
    TOTAL_REQS = 50    # requests to send per algorithm

    # 2x rate limit: one request every 50ms = 20 req/s against limit of 10/s
    DELAY = 0.05

    # ── Phase 1: All 4 algorithms at 2x rate limit ─────────────────
    section("Phase 1: 50 Requests at 2x Rate Limit (20 req/s vs limit 10/s)")
    print(f"  Sending {TOTAL_REQS} requests at ~20 req/s  |  limit: {RATE_LIMIT}/s\n")
    print(f"  {'Algorithm':<28} {'Allowed':>8} {'Rejected':>9} {'Eff. rate':>12}")
    print(f"  {'─'*28} {'─'*8} {'─'*9} {'─'*12}")

    flush_keys("tb", "swl", "fw", "swc")

    scenarios = [
        ("Token Bucket",           token_bucket,           "p1_tb",
            {"rate": RATE_LIMIT, "capacity": BURST_CAP}),
        ("Sliding Window Log",     sliding_window_log,     "p1_swl",
            {"window_seconds": WINDOW, "max_requests": RATE_LIMIT}),
        ("Fixed Window Counter",   fixed_window,           "p1_fw",
            {"window_seconds": WINDOW, "max_requests": RATE_LIMIT}),
        ("Sliding Window Counter", sliding_window_counter, "p1_swc",
            {"window_seconds": WINDOW, "max_requests": RATE_LIMIT}),
    ]

    for name, fn, key, kwargs in scenarios:
        allowed, rejected, elapsed = send_requests(fn, key, TOTAL_REQS, DELAY, **kwargs)
        effective_rate = allowed / elapsed
        print(f"  {name:<28} {allowed:>8}  {rejected:>8}  {effective_rate:>10.1f}/s")

    print(f"""
  Expected: ~{RATE_LIMIT} req/s effective throughput for all algorithms.
  Token bucket may allow a brief initial burst; fixed window may show
  slightly higher allowed count due to boundary alignment.
""")

    # ── Phase 2: Fixed window boundary burst problem ───────────────
    section("Phase 2: Fixed Window Boundary Burst Exploit")
    print("""
  Fixed window resets at exact second boundaries.
  Strategy: send max requests near END of window, then max again
  at START of next window → 2x the limit passes in ~200ms.

  Timeline (limit=10/s):
    t=0.85s  [■■■■■■■■■■] ← 10 requests, window 0  (count reaches 10)
    t=1.00s  window resets — counter goes to 0
    t=1.05s  [■■■■■■■■■■] ← 10 more, window 1  (count reaches 10)

  Result: 20 requests in ~200ms — 2x the advertised limit.
  Sliding window log/counter would cap this at ≤10 in any 1s span.
""")

    # Align to window boundary
    now = time.time()
    window_size = 1.0
    next_boundary = (int(now / window_size) + 1) * window_size
    wait_time = next_boundary - now - 0.15  # start 150ms before boundary

    if wait_time > 0:
        print(f"  Aligning to window boundary (waiting {wait_time:.2f}s)...")
        time.sleep(wait_time)

    flush_keys("fw:boundary_test")
    burst_allowed = 0
    burst_times = []

    # Batch 1: before boundary
    for _ in range(10):
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

    # Batch 2: after boundary
    for _ in range(10):
        if fixed_window("boundary_test", 1, 10):
            burst_allowed += 1
        burst_times.append(time.time())
        time.sleep(0.01)

    span = burst_times[-1] - burst_times[0]
    print(f"  Sent 20 requests spanning window boundary over {span:.3f}s")
    print(f"  Allowed: {burst_allowed}/20  {'(boundary burst confirmed!)' if burst_allowed >= 18 else '(partial — boundary missed, rerun)'}")
    print(f"  At limit=10/s, the true maximum over any 200ms should be ≤2.")

    # ── Phase 3: Token bucket burst fill ───────────────────────────
    section("Phase 3: Token Bucket — Burst Up to Bucket Capacity")
    print(f"""
  Token bucket (rate=5/s, capacity={BURST_CAP}):
  • Idle for 2 seconds → bucket fills to {BURST_CAP} tokens (= capacity ceiling).
  • Fire 15 requests instantly:
    - First {BURST_CAP} consume stored tokens → allowed (burst absorbed).
    - Remaining 5 hit empty bucket → rejected.
  • This is by design: APIs tolerate short client bursts without penalising
    well-behaved clients that have been idle.
""")

    # Reset state for this key
    r.delete("tb:burst_demo")
    time.sleep(2.1)  # fill the bucket

    burst_results = []
    for _ in range(15):
        result = token_bucket("burst_demo", rate=5, capacity=BURST_CAP)
        burst_results.append(result)

    burst_ok = sum(burst_results)
    burst_no = len(burst_results) - burst_ok
    symbols = "".join("✓" if x else "✗" for x in burst_results)
    print(f"  15 immediate requests after 2s idle:")
    print(f"  [{symbols}]")
    print(f"  Allowed: {burst_ok}  Rejected: {burst_no}")
    if burst_ok == BURST_CAP:
        print(f"  → Exactly {BURST_CAP} burst tokens consumed, then rate limited.")
    else:
        print(f"  → {burst_ok} tokens consumed (may vary slightly with timing).")

    # ── Phase 4: Sliding window log memory cost ────────────────────
    section("Phase 4: Sliding Window Log — Accuracy at O(N) Memory Cost")
    print("""
  Sliding window log stores one sorted-set entry per request.
  Memory scales with (window_size × request_rate) — not just counters.

  Sending 30 requests at 25 req/s (limit 10/s, window 1s)...
""")
    flush_keys("swl:accuracy_test")
    acc_allowed = 0
    acc_rejected = 0
    for _ in range(30):
        if sliding_window_log("accuracy_test", 1, 10):
            acc_allowed += 1
        else:
            acc_rejected += 1
        time.sleep(0.04)  # 25 req/s

    card = r.zcard("swl:accuracy_test")
    print(f"  30 requests at 25 req/s  |  limit 10/s:")
    print(f"  Allowed: {acc_allowed}  Rejected: {acc_rejected}")
    print(f"  Entries still in sorted set: {card}  (those within last 1s window)")
    print(f"""
  Memory extrapolation:
    10 req/s × 1s window  =   10 entries/user
    1M users              =   10M Redis sorted-set entries
    Each entry ~64 bytes  =  ~640 MB just for rate-limit state
    → Impractical at scale. Use sliding window counter instead.
""")

    # ── Phase 5: Atomic vs non-atomic race condition demo ──────────
    section("Phase 5: Race Condition — Atomic vs Non-Atomic Token Bucket")
    print("""
  The rate limit check must be atomic. If read and write are separate
  Redis operations, two concurrent requests can both see tokens=1,
  both decide to allow themselves, and both write tokens=0 — consuming
  only 1 token for 2 requests. This is a classic TOCTOU bug.

  We set tokens=1 (only 1 request should pass) then fire 20 threads
  concurrently against the racy (non-atomic) implementation.

  Atomic (Lua):     exactly 1 allowed — the script is a single Redis op.
  Non-atomic (racy): likely >1 allowed — threads race through the gap.
""")

    # --- Atomic (Lua) ---
    r.delete("tb:atomic_test")
    r.hset("tb:atomic_test", mapping={"tokens": 1.0, "last_refill": time.time()})
    # Pre-register script
    _get_tb_sha()

    atomic_results = []
    atomic_lock = threading.Lock()

    def atomic_worker():
        res = token_bucket("atomic_test", rate=1, capacity=1)
        with atomic_lock:
            atomic_results.append(res)

    # Reset to exactly 1 token right before firing threads
    r.delete("tb:atomic_test")
    r.hset("tb:atomic_test", mapping={"tokens": 1.0, "last_refill": str(time.time() - 100)})

    threads = [threading.Thread(target=atomic_worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    atomic_allowed = sum(atomic_results)
    print(f"  Atomic (Lua script)  — 20 threads, tokens=1:")
    print(f"  Allowed: {atomic_allowed}/20  {'✓ correct (exactly 1)' if atomic_allowed == 1 else '✗ incorrect — unexpected result'}")

    # --- Non-atomic (racy) ---
    r.delete("tb_racy:race_test")
    r.hset("tb_racy:race_test", mapping={"tokens": 1.0, "last_refill": str(time.time() - 100)})

    racy_results = []
    racy_lock = threading.Lock()

    def racy_worker():
        res = token_bucket_racy("race_test", rate=1, capacity=1)
        with racy_lock:
            racy_results.append(res)

    threads = [threading.Thread(target=racy_worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    racy_allowed = sum(racy_results)
    print(f"\n  Non-atomic (racy)    — 20 threads, tokens=1:")
    print(f"  Allowed: {racy_allowed}/20  {'✗ race condition — more than 1 slipped through!' if racy_allowed > 1 else '(race not triggered this run — try again)'}")
    print(f"""
  Fix: use Redis Lua scripts (EVALSHA) or Redis pipeline with WATCH/MULTI/EXEC.
  Production implementations (redis-cell, Envoy, Kong) always use Lua or
  dedicated atomic commands for this reason.
""")

    # ── Phase 6: Comparison table ──────────────────────────────────
    section("Phase 6: Algorithm Comparison Table")
    print(f"""
  {'Algorithm':<28} {'Memory':<22} {'Accuracy':<22} {'Burst'}
  {'─'*28} {'─'*22} {'─'*22} {'─'*20}
  {'Token Bucket':<28} {'O(1) — 2 fields':<22} {'Good':<22} {'Allows up to capacity'}
  {'Sliding Window Log':<28} {'O(N) — N=reqs/window':<22} {'Exact':<22} {'No bursts allowed'}
  {'Fixed Window Counter':<28} {'O(1) — 1 counter':<22} {'Poor at boundary':<22} {'2x burst at boundary'}
  {'Sliding Window Counter':<28} {'O(1) — 2 counters':<22} {'~99% accurate':<22} {'Approximate — safe'}

  When to use each:
  ┌─────────────────────────────────────────────────────────────────────┐
  │ Token Bucket        → APIs that allow short bursts (most common)    │
  │ Sliding Window Log  → High-value ops where accuracy is critical     │
  │ Fixed Window        → Internal quotas, billing periods (not edge)   │
  │ Sliding Window Ctr  → High-traffic APIs needing O(1) + accuracy     │
  └─────────────────────────────────────────────────────────────────────┘

  Distributed rate limiting in production:
  ┌─────────────────────────────────────────────────────────────────────┐
  │ Single Redis node    all 4 work, ~0.5-2ms overhead per check        │
  │ Redis Cluster        hash-tag keys to pin one user to one shard     │
  │ Lua / EVALSHA        makes token bucket and SWL atomic              │
  │ redis-cell module    CL.THROTTLE implements GCRA in one command     │
  │ Nginx limit_req_zone sliding window counter in shared memory (~1µs) │
  │ Envoy / Kong         token bucket with per-route config             │
  │ Fail-open vs closed  Redis down → allow all (availability) or       │
  │                      deny all (safety). Most choose fail-open.      │
  └─────────────────────────────────────────────────────────────────────┘

  Next: ../11-cdn-and-edge/
""")


if __name__ == "__main__":
    main()
