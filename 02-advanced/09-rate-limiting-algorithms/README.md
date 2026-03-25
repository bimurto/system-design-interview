# Rate Limiting Algorithms

**Prerequisites:** `../08-search-systems/`
**Next:** `../10-cdn-and-edge/`

---

## Concept

Rate limiting is a defensive pattern that restricts how many requests a client can make in a given time window. Without it, a single misbehaving client — or a flood of legitimate traffic — can exhaust your server's CPU, memory, database connections, or downstream API quotas. Every public-facing API, from Twitter's timeline endpoint to Stripe's payment API, implements some form of rate limiting. The algorithm you choose has significant consequences for memory consumption, accuracy, and how the system behaves under burst traffic.

The four canonical algorithms are token bucket, sliding window log, fixed window counter, and sliding window counter. Each makes a different trade-off between simplicity and precision. Token bucket and sliding window counter are the most widely deployed in production systems because they balance accuracy with O(1) memory. Sliding window log is theoretically perfect but stores every request timestamp, making it impractical at high traffic volumes. Fixed window is the simplest to implement but has a well-known boundary burst vulnerability.

In distributed systems, rate limiting is complicated by the fact that multiple servers all handle requests from the same client. A naive per-server limit of 10 req/s across 5 servers actually allows 50 req/s. The canonical solution is centralised state in Redis: every rate-limit check reads and writes to a shared key, giving a global count. Redis pipelines and Lua scripts make these checks atomic so two simultaneous requests cannot both see a count of 9 and both increment to 10 — a race condition that would allow limit bypass.

Rate limiting is distinct from throttling. Rate limiting is binary: the request either passes or returns HTTP 429 Too Many Requests. Throttling is continuous: the server slows down processing proportionally to load. Rate limiting protects against abuse; throttling manages degradation gracefully. Many production systems implement both: rate limiting at the edge (Nginx/API gateway) and throttling deeper in the stack (queue depth, circuit breakers).

The choice of where to enforce rate limits also matters. Client-side rate limiting (in your SDK) reduces unnecessary network round-trips. Edge enforcement (API gateway, Nginx) protects backend servers entirely. Application-level enforcement enables per-user, per-endpoint, and per-tier limits that edge proxies cannot easily express. Most mature platforms use all three layers.

## How It Works

**Token Bucket:** Each client has a "bucket" that holds up to `capacity` tokens. Tokens are added at `rate` tokens/second, up to the capacity ceiling. Each request consumes one token. If the bucket is empty, the request is rejected. The bucket naturally absorbs short bursts (up to `capacity`) while enforcing the average rate over time.

```
  Bucket (capacity=10, rate=5/s):

  t=0   [●●●●●●●●●●]  10 tokens — burst of 10 requests allowed instantly
  t=0+  [          ]  bucket empty — next 5 requests must wait ~1s
  t=1   [●●●●●     ]  5 new tokens — 5 requests can proceed
  t=2   [●●●●●●●●●●]  back to full capacity
```

**Sliding Window Log:** The server stores a sorted set (by timestamp) of every request in the current window. On each new request, it trims entries older than `window_seconds`, appends the new timestamp, counts the total, and allows if the count is within the limit. This is perfectly accurate — it enforces the limit in any rolling window — but the sorted set grows linearly with traffic.

```
  Window=1s, limit=3:

  t=0.1: log=[0.1]         count=1 ALLOW
  t=0.5: log=[0.1, 0.5]    count=2 ALLOW
  t=0.8: log=[0.1,0.5,0.8] count=3 ALLOW
  t=0.9: log=[0.1,0.5,0.8,0.9] count=4 DENY
  t=1.2: trim <0.2 → log=[0.5,0.8,0.9,1.2] count=4 DENY  (0.1 expired)
  t=1.6: trim <0.6 → log=[0.8,0.9,1.2,1.6] count=4 still limited
```

**Fixed Window Counter:** Divide time into fixed windows (e.g., each second from 0-1, 1-2, 2-3). Increment a counter for the current window on each request. If count exceeds the limit, reject. At the window boundary the counter resets. Implementation is a single `INCR` + `EXPIRE` in Redis.

```
  Limit=10/s, fixed windows:

  Window 0 (t=0..1):   [■■■■■■■■■■] 10 requests — full
  Window 1 (t=1..2):   [■■■■■■■■■■] 10 more — allowed (counter reset!)

  Problem: 10 requests at t=0.99 + 10 at t=1.01 = 20 in 20ms!
```

**Sliding Window Counter:** An approximation that combines two fixed windows. The rate estimate is: `prev_count × (1 - elapsed/window) + current_count`. This smooths the boundary burst because the previous window's count is weighted by how much of it still overlaps the current sliding window. It uses only two Redis keys per user.

```
  t=1.7s, window=1s, limit=10:
  prev_window (t=1..2): count=8
  current_window (t=2..3): count=3, elapsed=0.7s
  weight of prev = 1 - 0.7 = 0.3
  estimate = 8×0.3 + 3 = 2.4 + 3 = 5.4 → ALLOW
```

### Trade-offs

| Algorithm | Memory per Key | Accuracy | Burst Handling | Complexity |
|---|---|---|---|---|
| Token Bucket | O(1) — 2 fields | Good | Allows up to capacity | Medium |
| Sliding Window Log | O(N) — N=requests in window | Exact | No bursts | Medium |
| Fixed Window Counter | O(1) — 1 counter | Poor at boundaries | 2x burst at boundary | Low |
| Sliding Window Counter | O(1) — 2 counters | ~99% accurate | Approximate, safe | Low-Medium |

### Failure Modes

**Redis unavailability — fail open vs fail closed:** If Redis goes down, every rate-limit check fails. Failing closed (reject all requests) protects backends but causes an outage. Failing open (allow all requests) maintains availability but removes protection. Most systems choose fail-open with alerting, accepting the risk of temporary overload during Redis failure.

**Clock skew between servers:** The sliding window counter computes `int(now / window_seconds)` to determine the current window. If two servers have clocks drifted by more than a few milliseconds, they will use slightly different window keys, effectively giving each server a separate limit. Solution: use Redis server time (`TIME` command) rather than local clock for window calculations.

**Race condition in token bucket without atomic operations:** The naive read-modify-write in the token bucket (read current tokens, compute new tokens, write back) is not atomic. Two concurrent requests can both read 1 token, both decide to allow, and both write 0 back — only consuming 1 token for 2 requests. Fix: use a Redis Lua script or a Redis pipeline with WATCH/MULTI/EXEC for optimistic locking.

**Thundering herd after rate limit reset:** At the fixed window boundary, all clients that were queued retry simultaneously. This can spike traffic from 0 to `limit × num_clients` in a single moment. Solution: add jitter to client retry logic (`retry after: random(0, window_remaining)`).

**Distributed counters under hot key:** A single Redis key tracking a popular API's global rate limit can become a hot spot, handling millions of operations per second. Solution: shard the counter across multiple keys (`key:shard_0`, `key:shard_1`), sum them at check time, or use Redis Cluster with key hashing.

## Interview Talking Points

- "Token bucket allows bursts up to the bucket capacity, then enforces average rate. It's the most common production choice because APIs need to tolerate bursty clients."
- "Sliding window log is most accurate but O(requests) memory — impractical for high-traffic APIs. Sliding window counter gets you 99% accuracy with O(1) memory."
- "Fixed window has a boundary burst problem: a client can send 2x the limit in 2x the window time by straddling the reset boundary. Sliding window counter eliminates this."
- "Distributed rate limiting requires shared state — typically Redis. The check must be atomic (Lua script or pipeline) to avoid race conditions that let requests slip through."
- "HTTP 429 Too Many Requests should include a `Retry-After` header so clients can back off intelligently rather than hammering in a retry loop."
- "Stripe uses token bucket per API key with separate buckets for read vs write operations. They expose current bucket state in response headers (`X-RateLimit-Remaining`)."
- "Leaky bucket is a variant of token bucket where requests fill a queue and drain at a fixed rate — it smooths output but adds latency. Token bucket allows bursts to pass through; leaky bucket does not."

## Hands-on Lab

**Time:** ~3 minutes
**Services:** Redis 7 + Python container

### Setup

```bash
cd system-design-interview/02-advanced/09-rate-limiting-algorithms/
docker compose up
```

### Experiment

The script runs five phases automatically:

1. Sends 50 requests at 2x the rate limit for all four algorithms, printing allowed/rejected counts and effective throughput.
2. Aligns to a window boundary and demonstrates the fixed window boundary burst — 20 requests allowed when only 10 should be.
3. Demonstrates token bucket bursting: idle for 2 seconds to fill the bucket, then fire 15 requests instantly — first 10 pass, rest are rejected.
4. Shows sliding window log memory behaviour: entry count in the sorted set scales with traffic.
5. Prints a comparison table with memory, accuracy, and burst-handling characteristics.

### Break It

Demonstrate the token bucket race condition by removing atomicity:

```python
import redis, time, threading
r = redis.Redis(decode_responses=True)

# Simulate concurrent requests racing on the same bucket
def racy_check():
    bucket = r.hgetall("tb:race")
    tokens = float(bucket.get("tokens", 10))
    time.sleep(0.001)  # simulate processing gap
    if tokens >= 1:
        r.hset("tb:race", mapping={"tokens": tokens - 1, "last_refill": time.time()})
        return True
    return False

r.hset("tb:race", mapping={"tokens": 1, "last_refill": time.time()})
threads = [threading.Thread(target=racy_check) for _ in range(20)]
[t.start() for t in threads]
[t.join() for t in threads]
print("Tokens remaining:", r.hgetall("tb:race")["tokens"])
# Multiple threads may have all seen tokens=1 and all allowed themselves
```

### Observe

The boundary burst in Phase 2 will show 20 requests allowed across a 1-second window. The token bucket burst in Phase 3 shows exactly 10 allowed then all rejected. The comparison table shows O(N) memory for sliding window log versus O(1) for all other algorithms.

### Teardown

```bash
docker compose down
```

## Real-World Examples

- **Stripe API rate limiting:** Stripe uses token buckets per API key with different capacities for test vs live mode and different rates for read vs write endpoints. Response headers include `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset` so clients can implement adaptive back-off. Source: Stripe API Reference, "Rate Limiting."
- **Nginx `limit_req_zone`:** Nginx implements a variant of the leaky bucket / sliding window counter in shared memory, configured with `limit_req_zone $binary_remote_addr zone=api:10m rate=10r/s`. It handles rate limiting in C at the kernel boundary, with ~microsecond overhead per request. Source: Nginx documentation, "Module ngx_http_limit_req_module."
- **Redis built-in rate limiting (Redis 7.0+):** Redis 7 added `WAIT`, `OBJECT FREQ`, and the `redis-cell` module (`CL.THROTTLE`) implements a Generic Cell Rate Algorithm (GCRA) — a variant of the leaky bucket — directly in Redis as an atomic command. Used by GitHub for API rate limiting. Source: Redis Labs, "redis-cell: A Redis module for rate limiting."

## Common Mistakes

- **Not making the check atomic.** A read-increment-write sequence without a Lua script or pipeline WATCH allows two concurrent requests to both see the limit as not exceeded and both proceed. Always use atomic operations: `INCR` for fixed window, Lua for token bucket and sliding window log.
- **Using fixed window for user-facing APIs.** The boundary burst lets a determined client send 2x the configured limit in a short window. Use sliding window counter or token bucket for anything clients interact with.
- **Not returning `Retry-After`.** When you return 429, include `Retry-After: N` (seconds) or `X-RateLimit-Reset: <timestamp>`. Without it, clients implement exponential back-off with random jitter — or worse, retry immediately in a hot loop.
- **Rate limiting per server instead of globally.** A 10 req/s limit enforced on each of 10 servers is actually a 100 req/s global limit. Always use shared state (Redis) for rate limits, or accept that per-server limits are approximations.
- **Ignoring the cost of the rate-limit check itself.** A Redis round-trip adds 0.5-2ms to every request. For very high-throughput endpoints, move the check to a local counter with periodic Redis sync, or use Nginx's `limit_req_zone` which runs in shared memory without a network hop.
