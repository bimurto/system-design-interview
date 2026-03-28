# Case Study: Distributed Rate Limiter

**Prerequisites:** `../../02-advanced/09-rate-limiting-algorithms/`, `../../02-advanced/07-distributed-caching/`

---

## The Problem at Scale

Stripe processes millions of API calls per day from thousands of merchants, each with different rate limits. A single
global rate limiter must enforce per-API-key, per-endpoint, and per-IP limits across hundreds of API servers —
consistently, without a central bottleneck.

| Metric                   | Value                                      |
|--------------------------|--------------------------------------------|
| API servers              | 100s of instances                          |
| Unique API keys          | Millions                                   |
| API calls/day            | Hundreds of millions                       |
| Rate limit check latency | < 1ms (Redis in-memory)                    |
| Limit granularity        | Per API key, per endpoint, per IP          |
| Window types             | Fixed window, sliding window, token bucket |

The core challenge: if each of 100 API servers has its own in-memory counter, a client can send 100× the intended rate (
10 req/s per server = 1,000 req/s globally). The counter must be globally shared.

---

## Requirements

### Functional

- Enforce rate limits per API key, per IP, per endpoint
- Support multiple limit tiers (trial, standard, premium)
- Return 429 Too Many Requests with informative headers
- Allow configuring different limits without code deployments

### Non-Functional

- Rate limit check must add < 1ms to request latency
- Globally consistent: same API key seen by any server consumes the same quota
- Fault tolerant: if rate limit store is unavailable, define clear fail behavior
- No double-counting: concurrent requests from multiple servers must not bypass limits

### Capacity Estimation

Baseline: Stripe-scale — 100K API RPS, 1M active API keys, 10s fixed window.

| Metric                     | Calculation                                                | Result        |
|----------------------------|------------------------------------------------------------|---------------|
| Redis INCR ops/second      | 100K API RPS × 1 Lua call per request                      | ~100K ops/s   |
| Redis memory per key       | int64 counter (8B) + key string ~40B + Redis overhead ~50B | ~100B/key     |
| Active keys in window      | 1M API keys × 1 counter + 1M IP keys × 1 counter           | ~200MB        |
| Sliding window overhead    | 2× fixed-window (prev + current counter per key)           | ~400MB        |
| Redis throughput headroom  | single node handles ~500K ops/s (pipelining, Lua)          | fits one node |
| Redis cluster needed at    | > 500K API RPS or > 2GB active key space                   | 2–3 shards    |
| Network overhead per check | 1 Lua round-trip ≈ 100–200µs intra-DC                      | adds <0.5ms   |
| 429 response payload       | ~200B JSON + headers                                       | negligible    |

**Scaling trigger:** Stripe reports ~500M API calls/day ≈ 5,800 RPS average, with peak 3–5× = ~30K RPS — well within a
single Redis node. Cloudflare at 55M RPS uses distributed local counting rather than a single Redis, because the network
cost of a central INCR per request exceeds the budget.

**Key expiry math:** with a 10s window, each counter TTL is 10s. Redis uses lazy expiry + background sweep. At 1M active
keys × 100B = 100MB — comfortably below typical Redis instance RAM (8–64GB). No cleanup job needed.

---

## High-Level Architecture

```
  Client Request
       │
       ▼
  ┌────────────┐
  │  Nginx LB  │  (round-robin across API servers)
  └─────┬──────┘
        │
   ┌────┴─────┬──────────┐
   ▼          ▼          ▼
  API1       API2       API3    ...  (N servers, stateless)
   │          │          │
   └──────────┴──────────┘
              │
              ▼ INCR (Lua script, atomic)
        ┌──────────┐
        │  Redis   │  (single shared counter store)
        │          │  Key: ratelimit:{api_key}:{window_id}
        └──────────┘
              │
       allowed / denied
              │
   ┌──────────────────────┐      ┌───────────────────────────┐
   │  200 OK              │  or  │  429 Too Many Requests    │
   │  X-RateLimit-*       │      │  Retry-After header       │
   └──────────────────────┘      └───────────────────────────┘
```

**Redis** holds the shared counters. Each counter is a Redis string key: `ratelimit:{api_key}:{window_number}`. The key
auto-expires after the window duration, requiring no cleanup job.

**Lua script** ensures atomicity: INCR and EXPIRE happen in a single Redis server-side execution. No client-side race
condition between "read counter" and "increment counter".

**Nginx** distributes requests across API servers. Since rate limit state is in Redis (not per-server), the load
balancing algorithm doesn't affect correctness.

---

## Deep Dives

### 1. Why Local Rate Limiting Fails at Scale

**Per-server counter (wrong approach):**

```
Server 1: counter["api-key-X"] = 0  → allow → counter = 1
Server 2: counter["api-key-X"] = 0  → allow → counter = 1
...
Server N: counter["api-key-X"] = 0  → allow → counter = 1
```

With N=100 servers and a 10 req/s limit per server, a client can send 1,000 req/s and every request is "allowed" (each
server sees only 10/s).

**Global Redis counter (correct approach):**

```
All servers → Redis INCR ratelimit:api-key-X:window_42
Redis returns count=7 → server checks: 7 < 10 → allow
Redis returns count=11 → server checks: 11 > 10 → deny 429
```

All servers share one counter. Total across all servers ≤ limit. No coordination needed between servers.

### 2. Redis Lua Script: Atomic Check-and-Increment

A naive (broken) implementation:

```python
count = redis.GET(key)           # read
if count < limit:
    redis.INCR(key)              # write
    return "allowed"
return "denied"
```

Race condition: two concurrent requests both read count=9, both see 9 < 10, both INCR → final count=11 > limit. Both
were allowed, but the limit was 10.

**Fix: Lua script runs atomically on the Redis server:**

```lua
local count = redis.call("INCR", full_key)
if count == 1 then
    redis.call("EXPIRE", full_key, window)
end
if count > limit then
    return {0, count, limit}  -- denied
else
    return {1, count, limit}  -- allowed
end
```

INCR is a single atomic operation. The Lua script is executed atomically (Redis is single-threaded for script
execution). No race window. The count returned by INCR is the authoritative value.

**Why not MULTI/EXEC (Redis transactions)?** Transactions don't support conditional logic (if count > limit). Lua
scripts support full conditional logic and execute atomically.

### 3. Failure Modes: Fail Open vs Fail Closed

When Redis is unavailable, every API server faces a binary choice:

**Fail Open (allow all):**

- All requests pass through
- Rate limit is effectively suspended
- Risk: abusive clients can send unlimited requests during the outage
- Benefit: no user-visible errors from a Redis blip
- Used by: most public APIs (Twitter, GitHub) — user experience > security

**Fail Closed (deny all):**

- All requests get 429 or 503
- Rate limit is enforced conservatively
- Risk: legitimate traffic is blocked during outage
- Benefit: no abuse possible during outage
- Used by: financial APIs (Stripe for certain endpoints), security-critical paths

**Hybrid (local fallback):**

- Each server has an in-memory counter as fallback
- During Redis outage: each server enforces limit/N (N = number of servers)
- Approximate global enforcement without full outage
- Complexity: N must be known and consistent

**Automatic recovery:** On each request while in fail-open mode, the server probes Redis. When Redis comes back, rate
limiting resumes immediately with no operator action. An alternative is a background thread that polls Redis every few
seconds and re-registers the Lua script on reconnect.

### 4. Multi-Tier Rate Limiting

Production systems layer multiple rate limits:

```
Incoming request
    │
    ├─ IP rate limit:         10 req/s per IP (DDoS protection)
    │
    ├─ API key rate limit:    varies by tier (trial/standard/premium)
    │
    └─ Endpoint rate limit:   POST /charge → 100/min (expensive ops)
                              GET /balance → 1000/min (cheap reads)
```

Implementation: check multiple Redis keys per request. If ANY check fails → 429. Return the most restrictive
`X-RateLimit-Remaining` header.

Redis key hierarchy:

```
ratelimit:ip:{ip}:{window}           → IP-level
ratelimit:key:{api_key}:{window}     → API key-level
ratelimit:key:{api_key}:charge:{win} → endpoint-level
```

### 5. Sliding Window vs Fixed Window (Distributed)

**Fixed window boundary problem:** A client sends 10 requests at t=9.9s (window 0) and 10 requests at t=10.1s (window
1). Both windows see 10 requests → both allow. In 0.2 seconds, 20 requests passed through — double the intended rate.

**Sliding window counter (Cloudflare's approach):**

```
rate = prev_window_count × (seconds_since_window_start / window_size)
     + current_window_count
```

Uses 2 Redis keys per limit (current and previous window). Redis memory: 2× fixed window. Error rate vs a true sliding
window: < 0.003% in practice. Used by Cloudflare for their global rate limiter (published 2023 blog post).

**Token bucket (Stripe's approach):** more flexible for burst handling. A key accumulates tokens at rate R/second up to
a bucket capacity B. A request consumes 1 token. Allows short bursts (B tokens) while enforcing an average rate (
R/second). Implemented in Redis with two fields: `tokens` (current bucket fill level) and `last_refill` (Unix timestamp
of last refill). On each request, compute `elapsed = now - last_refill`, add `elapsed × R` tokens (capped at B), then
consume 1 token if available.

### 6. Token Bucket in Redis (Implementation Detail)

The token bucket requires reading two fields, computing new state, and writing back — all atomically. A Lua script
handles this:

```lua
local tokens_key = KEYS[1]
local last_key   = KEYS[2]
local rate       = tonumber(ARGV[1])  -- tokens per second
local capacity   = tonumber(ARGV[2])  -- max bucket size
local now        = tonumber(ARGV[3])

local last_refill = tonumber(redis.call("GET", last_key) or now)
local tokens      = tonumber(redis.call("GET", tokens_key) or capacity)

local elapsed  = now - last_refill
local refill   = elapsed * rate
tokens = math.min(capacity, tokens + refill)

if tokens >= 1 then
    tokens = tokens - 1
    redis.call("SET", tokens_key, tokens, "EX", 3600)
    redis.call("SET", last_key,   now,    "EX", 3600)
    return 1  -- allowed
else
    return 0  -- denied
end
```

This uses two Redis keys per rate-limited entity and a Lua script for atomicity — the same pattern as fixed-window, but
computing time-based refill instead of window-id rollover.

---

## How It Actually Works

**Stripe's rate limiting** (Engineering Blog, "Scaling your API with Rate Limiters", 2015): Stripe uses token bucket per
API key, implemented with Redis. They describe the same fail-open strategy: if Redis becomes unavailable, requests are
allowed through with a warning log. They also describe the per-endpoint limits: some expensive endpoints (creating
charges) have stricter limits than read endpoints.

**Cloudflare's distributed rate limiting** (Blog, 2023): Cloudflare processes ~55 million HTTP requests per second.
Their rate limiter uses an approximate sliding window counter across their distributed PoP (points of presence). Each
PoP does local counting; counts are aggregated across PoPs with a ~10-second lag. This is acceptable for rate limiting (
brief overcount is acceptable) but not for billing.

Key insight from both: **exact global rate limiting at very high RPS requires a centralized store (Redis), which becomes
a bottleneck**. Production systems accept approximate counts at scale, using local estimation + periodic synchronization
rather than a single global counter.

Source: Stripe Engineering Blog "Scaling your API with Rate Limiters" (2015); Cloudflare Blog "How we built rate
limiting capable of scaling to millions of domains" (2023); Redis documentation on Lua scripting.

---

## Hands-on Lab

**Time:** ~15 minutes
**Services:** `redis`, `api1`, `api2`, `nginx`

### Setup

```bash
cd system-design-interview/03-case-studies/10-rate-limiter/
docker compose up -d
# Wait ~60s for all services to be healthy (pip install runs on first start)
docker compose ps
```

### Experiment

```bash
# Run from host (no extra pip installs needed — uses stdlib only)
python experiment.py
```

The script runs 7 phases:

1. **Global limit:** 15 requests through Nginx → 10 pass, 5 blocked (429)
2. **Local vs global:** same key hits api1 and api2 directly → global counter still enforces
3. **429 headers:** show X-RateLimit-Limit and X-RateLimit-Remaining per request
4. **Fail-open + auto-recovery:** stop Redis, observe fail-open, restart Redis, observe automatic resumption
5. **Per-key limits:** trial=3, standard=10, premium=50 — verified
6. **Boundary attack:** live demo of fixed-window double-rate exploit at window rollover
7. **Sliding window formula:** O(1) approximation with live numbers and algorithm comparison table

### Break It

**Observe the boundary problem:**

```bash
# Send 10 requests just before window rolls over
# then 10 requests right after — all 20 should pass with fixed window
python3 -c "
import time, urllib.request, json
now = int(time.time())
window = 10
wait = (window - now % window) - 0.5
print(f'Waiting {wait:.1f}s to hit window boundary...')
time.sleep(wait)
for i in range(20):
    with urllib.request.urlopen('http://localhost:8001/api/data?key=boundary-test') as r:
        b = json.loads(r.read())
        print(f'  req {i+1}: count={b[\"request_count\"]}')
    time.sleep(0.05)
"
```

**Kill Redis to see fail-open:**

```bash
docker compose stop redis
# Now send requests — they should all pass with X-Redis-Available: False
curl http://localhost:8001/api/data?key=test
# Restart — rate limiting resumes automatically, no server restart required
docker compose start redis
```

### Observe

```bash
# Check Redis keys directly
docker compose exec redis redis-cli keys "ratelimit:*"

# Watch a counter in real time
watch -n1 "docker compose exec redis redis-cli get 'ratelimit:anonymous:$(python3 -c \"import time; print(int(time.time())//10)\")'"

# See all active counters and TTLs via the stats endpoint
curl -s http://localhost:8001/stats | python3 -m json.tool
```

### Teardown

```bash
docker compose down -v
```

---

## Interview Checklist

1. **Q: Why can't each API server have its own rate limit counter?**
   A: With N servers each allowing L requests, the effective global limit is N×L. A client routing requests across all
   servers sees N times the intended limit. With 100 servers and a 10 req/s limit, a client can send 1,000 req/s. Shared
   Redis counter ensures all servers increment the same value — total across all servers stays ≤ L.

2. **Q: Why use a Lua script instead of just INCR + GET?**
   A: INCR and GET are separate commands — a race condition exists between them. Two concurrent requests both read
   count=9, both check 9 < 10, both proceed to INCR → count becomes 11. Lua scripts execute atomically on the Redis
   server: no other command runs between the INCR and the limit check. Redis is single-threaded for script execution.

3. **Q: What happens when Redis is down? Should you fail open or closed?**
   A: Depends on the API's security requirements. Fail-open (allow all traffic) preserves availability — a Redis blip
   doesn't cause user-visible errors. Used by most public APIs. Fail-closed (block all traffic) prevents abuse during
   outages — required for financial or security-critical endpoints. A hybrid approach uses a local in-memory counter as
   a fallback (each server enforces limit/N). With automatic recovery, the server probes Redis on every fail-open
   request and resumes rate limiting the moment Redis comes back.

4. **Q: What is the fixed window boundary problem?**
   A: A client can send 2× the rate limit in a short period by making L requests at the end of one window and L requests
   at the start of the next. Both windows see exactly L requests and allow them — but in the 2× window_size seconds
   between them, 2L requests passed. Sliding window counter fixes this by weighting the previous window's count by the
   fraction of time elapsed.

5. **Q: How does a token bucket differ from a fixed window counter?**
   A: Token bucket allows controlled bursting. A bucket holds up to B tokens, refilled at rate R tokens/second. Each
   request consumes 1 token. A client that's been idle accumulates B tokens and can burst at full speed before being
   throttled. Fixed window is binary: all L requests can happen in the first second, then nothing until the window
   resets. Token bucket is more natural for bursty-but-bounded API usage patterns.

6. **Q: How do you support millions of different API keys without Redis running out of memory?**
   A: Each rate limit key auto-expires after the window duration (EXPIRE set on first INCR). Only keys active in the
   current window exist in Redis. At any moment, only ~1M active keys × 54B per key = ~54MB — tiny. Inactive clients'
   keys expire automatically. No manual cleanup needed.

7. **Q: How do you implement per-endpoint rate limits on top of per-key limits?**
   A: Use a composite key: `ratelimit:{api_key}:{endpoint}:{window}`. Each endpoint gets its own counter. On each
   request, check multiple Redis keys: IP-level, key-level, endpoint-level. If any check fails → 429. Return the minimum
   `X-RateLimit-Remaining` across all applicable limits.

8. **Q: How does Cloudflare rate limit at 55M RPS without a single Redis bottleneck?**
   A: Approximate sliding window with local counting per PoP (point of presence). Each PoP counts locally and
   periodically synchronizes with a central store (~10s lag). This means a client could exceed the limit briefly during
   the sync window, but the approximation is acceptable for rate limiting (brief over-counting is not catastrophic).
   True global consistency at 55M RPS would require prohibitively expensive coordination.

9. **Q: What headers should a rate-limited response include?**
   A: `X-RateLimit-Limit` (total allowed), `X-RateLimit-Remaining` (remaining in window), `X-RateLimit-Reset` (Unix
   timestamp when window resets), `Retry-After` (seconds until the client can retry). These allow clients to implement
   smart backoff: wait exactly `Retry-After` seconds rather than using exponential backoff guessing.

10. **Q: Walk me through a Stripe API request that gets rate limited.**
    A: (1) Client sends POST /v1/charges with API key sk_live_xxx. (2) API server extracts key from Authorization
    header. (3) Server executes Lua script: INCR ratelimit:sk_live_xxx:{window}. (4) Redis returns count=101. Limit for
    this key is 100. (5) Server returns 429 with headers: X-RateLimit-Limit=100, X-RateLimit-Remaining=0,
    Retry-After=7 (seconds until window resets). (6) Client SDK reads Retry-After, sleeps 7 seconds, retries. (7) Next
    window: count=1 → allowed.

11. **Q: How do you implement a token bucket in Redis atomically?**
    A: Store two fields per key: `tokens` (current fill level) and `last_refill` (Unix timestamp). On each request, run
    a Lua script that reads both, computes elapsed time, refills tokens at rate R up to capacity B, then consumes 1
    token if available and writes back — all atomically. The Lua script prevents the TOCTOU race between reading the
    current token count and writing the decremented value.

12. **Q: How do sliding window counter and token bucket compare for a burstable API?**
    A: Both use O(1) Redis memory. Sliding window counter approximates a rolling sum over the last N seconds — it
    smooths boundary spikes but does not explicitly model burst capacity. Token bucket explicitly allows bursting up to
    B tokens accumulated during idle periods, then enforces average rate R. For an API where clients legitimately batch
    requests occasionally, token bucket is more permissive and feels more natural. Sliding window counter is simpler to
    implement and reason about at scale (Cloudflare's choice).
