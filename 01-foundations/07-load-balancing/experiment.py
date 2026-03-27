#!/usr/bin/env python3
"""
Load Balancing Lab — Round-Robin vs Weighted RR vs Least-Connections vs IP Hash

Prerequisites: docker compose up -d (wait ~60s for all backends to start)

What this demonstrates:
  1. Round-robin: equal distribution regardless of backend speed
  2. The tail latency problem: a slow backend (app4 at 500ms) drives p95 for the whole pool
  3. Least-connections (concurrent): fast backends self-select for more traffic
  4. Weighted round-robin: explicitly bias traffic toward higher-capacity backends
  5. IP-hash: same source IP always routes to same backend (session affinity)
  6. Passive health check: Nginx retries on a different backend after failure

Upstreams (configured in nginx.conf):
  /          → round-robin
  /lc/       → least_conn
  /wrr/      → weighted round-robin (app1×3, app2×2, app3×3, app4×1)
  /iphash/   → ip_hash
"""

import json
import time
import threading
import urllib.request
import urllib.error
from collections import Counter, defaultdict

BASE_URL = "http://localhost:8080"


# ── Helpers ────────────────────────────────────────────────────────────────────

def section(title):
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print("=" * 70)


def fetch(path="/", timeout=6):
    """Fetch a URL, return (data_dict, elapsed_ms) or (None, elapsed_ms) on error."""
    url = f"{BASE_URL}{path}"
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            elapsed = (time.perf_counter() - start) * 1000
            data = json.loads(resp.read().decode())
            return data, elapsed
    except Exception:
        elapsed = (time.perf_counter() - start) * 1000
        return None, elapsed


def check_ready():
    """Verify Nginx is up before running experiments."""
    for attempt in range(15):
        try:
            with urllib.request.urlopen(f"{BASE_URL}/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        print(f"  Waiting for Nginx... (attempt {attempt + 1}/15)")
        time.sleep(3)
    return False


def run_sequential(path, count):
    """Run `count` sequential requests, return (distribution, latencies_by_host)."""
    dist = Counter()
    latencies_by_host = defaultdict(list)
    for _ in range(count):
        data, ms = fetch(path)
        host = data["host"] if data else "error"
        dist[host] += 1
        latencies_by_host[host].append(ms)
    return dist, latencies_by_host


def run_concurrent(path, count):
    """Run `count` concurrent requests, return (distribution, latencies_by_host, wall_ms)."""
    results = []
    lock = threading.Lock()

    def worker():
        data, ms = fetch(path)
        host = data["host"] if data else "error"
        with lock:
            results.append((host, ms))

    threads = [threading.Thread(target=worker) for _ in range(count)]
    wall_start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall_ms = (time.perf_counter() - wall_start) * 1000

    dist = Counter(h for h, _ in results)
    latencies_by_host = defaultdict(list)
    for h, ms in results:
        latencies_by_host[h].append(ms)
    return dist, latencies_by_host, wall_ms


DECLARED_LATENCY = {"app1": 50, "app2": 200, "app3": 50, "app4": 500}
BACKEND_ORDER = ["app1", "app2", "app3", "app4", "error"]


def print_distribution(dist, latencies_by_host, total):
    for b in BACKEND_ORDER:
        count = dist.get(b, 0)
        if count == 0:
            continue
        bar = "#" * count
        pct = count / total * 100
        lats = latencies_by_host.get(b, [0])
        avg_ms = sum(lats) / len(lats)
        declared = DECLARED_LATENCY.get(b, "?")
        print(f"    {b}  ({declared:>3}ms declared)  {bar:<30} {count:>3} reqs  ({pct:4.0f}%)  avg {avg_ms:.0f}ms")


def percentile(sorted_values, p):
    """Return the p-th percentile of a pre-sorted list (0 < p <= 100)."""
    if not sorted_values:
        return 0
    # Linear interpolation (same method as numpy.percentile default)
    idx = (p / 100) * (len(sorted_values) - 1)
    lower = int(idx)
    upper = min(lower + 1, len(sorted_values) - 1)
    frac = idx - lower
    return sorted_values[lower] * (1 - frac) + sorted_values[upper] * frac


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    section("LOAD BALANCING LAB")
    print("""
  Architecture:
    Nginx (port 8080)
      ├─ app1:  50ms latency   (healthy)
      ├─ app2: 200ms latency   (healthy, moderate)
      ├─ app3:  50ms latency   (healthy)
      └─ app4: 500ms latency   (degraded — simulates a GC-paused or I/O-saturated node)

  Nginx upstreams:
    /          → round-robin       (equal distribution)
    /wrr/      → weighted RR       (app1×3, app2×2, app3×3, app4×1)
    /lc/       → least_conn        (fewest active connections wins)
    /iphash/   → ip_hash           (client IP hashed to a fixed backend)
""")

    if not check_ready():
        print("  ERROR: Nginx is not responding. Run: docker compose up -d")
        return

    # ── Phase 1: Round-Robin (sequential) ─────────────────────────────────────
    section("Phase 1: Round-Robin — 40 Sequential Requests")
    print("""
  Round-robin sends each request to the next backend in order:
    req1→app1, req2→app2, req3→app3, req4→app4, req5→app1, ...

  With sequential requests (one at a time), every backend gets exactly 25%.
  The total wall time is the sum of all backend latencies — the slow backend
  app4 (500ms) contributes equally to the total time budget.
""")
    dist_rr, lat_rr = run_sequential("/", 40)
    print_distribution(dist_rr, lat_rr, 40)

    all_rr = sorted(ms for lats in lat_rr.values() for ms in lats)
    avg_rr = sum(all_rr) / len(all_rr)
    p50_rr = percentile(all_rr, 50)
    p95_rr = percentile(all_rr, 95)
    total_rr = sum(all_rr)
    print(f"\n  Sequential stats (40 requests): avg={avg_rr:.0f}ms  p50={p50_rr:.0f}ms  p95={p95_rr:.0f}ms")
    print(f"  Total accumulated latency: {total_rr:.0f}ms")
    print(f"""
  Intuition: avg ≈ (50 + 200 + 50 + 500) / 4 = {(50+200+50+500)//4}ms.
  app4 is 10x slower than app1/app3 but receives identical traffic.
""")

    # ── Phase 2: The Tail Latency Problem (concurrent) ────────────────────────
    section("Phase 2: Tail Latency Under Concurrent Round-Robin Load")
    print("""
  With concurrent requests, the wall time is bounded by the SLOWEST backend.
  Even if only 25% of requests hit app4, the entire batch waits for it.

  Real-world impact:
    A page loading 4 parallel API calls routes round-robin.
    Whichever call lands on app4 delays the entire page render.
    At scale: if 1% of backends are slow and you fan out to 100 backends,
    P(hitting the slow one at least once) = 1 - 0.99^100 ≈ 63%.
    You almost certainly pay the slow backend's penalty on every fan-out.
""")
    print("  Sending 40 concurrent requests via round-robin...")
    dist_rr_c, lat_rr_c, wall_rr = run_concurrent("/", 40)
    print_distribution(dist_rr_c, lat_rr_c, 40)

    all_rr_c = sorted(ms for lats in lat_rr_c.values() for ms in lats)
    p50_rr_c = percentile(all_rr_c, 50)
    p95_rr_c = percentile(all_rr_c, 95)
    print(f"\n  Wall time (40 concurrent, round-robin): {wall_rr:.0f}ms")
    print(f"  p50={p50_rr_c:.0f}ms  p95={p95_rr_c:.0f}ms  ← p95 is driven by app4")
    print(f"""
  The wall time approaches app4's latency (~500ms) even though 75% of
  backends are fast. One slow node poisons the entire concurrent batch.
""")

    # ── Phase 3: Weighted Round-Robin ─────────────────────────────────────────
    section("Phase 3: Weighted Round-Robin — Traffic Proportional to Capacity")
    print("""
  Weighted round-robin lets you control traffic share per backend:
    app1: weight=3  →  ~33% of requests
    app2: weight=2  →  ~22% of requests
    app3: weight=3  →  ~33% of requests
    app4: weight=1  →  ~11% of requests  (canary / degraded node)

  Use cases:
    - Backends with different hardware specs (2x CPU → weight=2)
    - Canary deployments: send 10% of traffic to the new version
    - Gradual traffic migration during blue/green deployments

  Compared to equal round-robin, weighted RR reduces how much traffic
  the slow backend receives, but it is still static — it does not adapt
  to real-time backend speed. Least-conn does that dynamically.
""")
    print("  Sending 40 sequential requests via weighted round-robin...")
    dist_wrr, lat_wrr = run_sequential("/wrr/", 40)
    print_distribution(dist_wrr, lat_wrr, 40)

    all_wrr = sorted(ms for lats in lat_wrr.values() for ms in lats)
    avg_wrr = sum(all_wrr) / len(all_wrr)
    p95_wrr = percentile(all_wrr, 95)
    print(f"\n  Weighted RR stats: avg={avg_wrr:.0f}ms  p95={p95_wrr:.0f}ms")
    print(f"  Compare to equal RR: avg={avg_rr:.0f}ms  p95={p95_rr:.0f}ms")
    print(f"""
  Weighted RR lowered average latency by steering fewer requests to app4.
  But the distribution is still static — if app4 degrades further, you
  need to manually adjust weights. Least-conn adapts automatically.
""")

    # ── Phase 4: Least Connections (concurrent) ────────────────────────────────
    section("Phase 4: Least-Connections — Dynamic Routing Under Concurrent Load")
    print("""
  least_conn routes each new request to the backend with the fewest currently
  ACTIVE connections. Fast backends complete requests quickly, freeing their
  connection slot and pulling in more new requests. Slow backends accumulate
  connections and receive fewer new ones.

  Critical distinction:
    Sequential requests (one at a time): least_conn behaves like round-robin.
    Each request finishes before the next starts, so all backends have 0
    active connections at the moment of each new request — the tie is broken
    round-robin-style. The benefit is only visible under concurrent load.

  Sequential least_conn (expect round-robin distribution):
""")
    dist_lc_s, lat_lc_s = run_sequential("/lc/", 20)
    print_distribution(dist_lc_s, lat_lc_s, 20)

    print("""
  Concurrent least_conn (expect fast backends to self-select):
""")
    print("  Sending 40 concurrent requests via least_conn...")
    dist_lc_c, lat_lc_c, wall_lc = run_concurrent("/lc/", 40)
    print_distribution(dist_lc_c, lat_lc_c, 40)

    all_lc_c = sorted(ms for lats in lat_lc_c.values() for ms in lats)
    p50_lc = percentile(all_lc_c, 50)
    p95_lc = percentile(all_lc_c, 95)
    print(f"\n  Wall time (40 concurrent, least_conn):   {wall_lc:.0f}ms")
    print(f"  Wall time (40 concurrent, round-robin):  {wall_rr:.0f}ms")
    print(f"  least_conn p50={p50_lc:.0f}ms  p95={p95_lc:.0f}ms")
    print(f"  round-robin p50={p50_rr_c:.0f}ms  p95={p95_rr_c:.0f}ms")
    print("""
  With least_conn, fast backends (app1, app3 at 50ms) accumulate fewer
  connections and pull in more traffic. App4 at 500ms gets very few new
  connections once it has pending ones queued up.

  Gotcha — thundering herd on backend recovery:
    When app4 recovers (comes back up), it briefly shows 0 active connections.
    Under least_conn, all new requests flood to it until it accumulates load,
    potentially re-crashing a fragile backend. Nginx Plus's `slow_start`
    directive ramps traffic gradually after recovery. Open-source Nginx does
    not support slow_start — use upstream health checks + connection limits
    in application-level circuit breakers (e.g., resilience4j, Hystrix).
""")

    # ── Phase 5: IP Hash (Session Affinity) ───────────────────────────────────
    section("Phase 5: IP Hash — Session Affinity (Sticky Sessions)")
    print("""
  ip_hash hashes the client source IP and consistently maps it to one backend.
  The same client IP always reaches the same backend — even across restarts,
  as long as the backend pool size doesn't change.

  When you need it:
    - Server-side sessions stored in memory (not Redis) — legacy apps
    - WebSocket connections that must reconnect to the same node
    - Local disk cache that must be on a specific server

  When you do NOT need it (correct interview answer):
    - Stateless services where sessions are in Redis / JWT tokens
    - Any 12-factor app — design for statelessness, then any algorithm works

  Problem: when a sticky backend goes down, that user loses their session.
  The fix is to externalize session state so that backend choice is irrelevant.

  Since all requests come from the same IP (localhost → 127.0.0.1),
  all requests will route to the same backend:
""")
    dist_ip, lat_ip = run_sequential("/iphash/", 12)
    print_distribution(dist_ip, lat_ip, 12)
    unique_backends = set(dist_ip.keys()) - {"error"}
    print(f"\n  All 12 requests routed to: {unique_backends}")
    print("  Same source IP = same backend every time (deterministic hashing).")
    print("""
  Uneven distribution risk: if 50,000 users share one corporate NAT IP,
  they all hash to the same backend, overloading it while others sit idle.
  This is the silent failure mode of ip_hash in enterprise environments.
""")

    # ── Phase 6: Algorithm Comparison Summary ─────────────────────────────────
    section("Phase 6: Algorithm Comparison — When to Use What")
    print(f"""
  Results from this lab:

    Round-robin (sequential 40 req):   avg={avg_rr:.0f}ms   p95={p95_rr:.0f}ms
    Weighted RR (sequential 40 req):   avg={avg_wrr:.0f}ms   p95={p95_wrr:.0f}ms
    Round-robin (concurrent 40 req):   wall={wall_rr:.0f}ms   p95={p95_rr_c:.0f}ms
    Least-conn  (concurrent 40 req):   wall={wall_lc:.0f}ms   p95={p95_lc:.0f}ms

  ┌────────────────────────┬─────────────────────────────┬─────────────────────────────┐
  │ Algorithm              │ Best for                    │ Avoid when                  │
  ├────────────────────────┼─────────────────────────────┼─────────────────────────────┤
  │ Round-robin            │ Identical backends, uniform │ Backends have variable      │
  │                        │ request durations           │ latency or capacity         │
  ├────────────────────────┼─────────────────────────────┼─────────────────────────────┤
  │ Weighted round-robin   │ Different capacity backends │ Latency is highly variable; │
  │                        │ Canary / blue-green deploy  │ weights are hard to tune    │
  ├────────────────────────┼─────────────────────────────┼─────────────────────────────┤
  │ Least connections      │ Variable request durations  │ Sequential workloads (acts  │
  │                        │ File upload, long-polling   │ same as RR with no concurr) │
  ├────────────────────────┼─────────────────────────────┼─────────────────────────────┤
  │ IP hash                │ Sticky sessions (legacy)    │ Stateless services — always │
  │                        │                             │ prefer stateless design     │
  ├────────────────────────┼─────────────────────────────┼─────────────────────────────┤
  │ Random / power-of-two  │ Large clusters, minimal     │ Backend capacities differ   │
  │ random choices         │ coordination overhead       │ significantly               │
  └────────────────────────┴─────────────────────────────┴─────────────────────────────┘

  Key interview insight:
    "I would design services to be stateless — sessions externalized to Redis,
     no local disk state — so that sticky sessions are unnecessary. With stateless
     backends, I can use least-conn to automatically route around slow or degraded
     nodes without any manual weight tuning. I would add passive health checks
     (proxy_next_upstream) to retry failed requests on another backend, and
     circuit breakers at the application level to shed load from backends that
     are repeatedly erroring."

  Next: ../08-databases-sql-vs-nosql/
""")


if __name__ == "__main__":
    main()
