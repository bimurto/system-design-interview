#!/usr/bin/env python3
"""
Load Balancing Lab — Round-Robin vs Least-Connections vs IP Hash

Prerequisites: docker compose up -d (wait ~60s for all backends to start)

What this demonstrates:
  1. Round-robin: equal distribution regardless of backend speed
  2. Tail latency problem: slow backend (app4 at 500ms) receives equal traffic
  3. Least-connections: routes to the backend with fewest active connections
  4. IP-hash: same source IP always routes to same backend (session affinity)
  5. Health check behavior: removing a backend from the pool
"""

import json
import time
import threading
import urllib.request
import urllib.error
from collections import Counter, defaultdict

BASE_URL = "http://localhost:8080"


def section(title):
    print(f"\n{'=' * 64}")
    print(f"  {title}")
    print("=" * 64)


def fetch(path="/", timeout=5):
    """Fetch a URL, return (data_dict, elapsed_ms) or (None, elapsed_ms) on error."""
    url = f"{BASE_URL}{path}"
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            elapsed = (time.perf_counter() - start) * 1000
            data = json.loads(resp.read().decode())
            return data, elapsed
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return None, elapsed


def check_ready():
    """Verify Nginx is up before running experiments."""
    for attempt in range(10):
        try:
            with urllib.request.urlopen(f"{BASE_URL}/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        print(f"  Waiting for Nginx... (attempt {attempt + 1}/10)")
        time.sleep(3)
    return False


def run_sequential(path, count, label):
    """Run `count` sequential requests, return (distribution, latencies)."""
    dist = Counter()
    latencies = []
    for _ in range(count):
        data, ms = fetch(path)
        if data:
            dist[data["host"]] += 1
            latencies.append((data["host"], ms))
        else:
            dist["error"] += 1
            latencies.append(("error", ms))
    return dist, latencies


def print_distribution(dist, latencies_by_host, total):
    backends = ["app1", "app2", "app3", "app4", "error"]
    declared_latency = {"app1": 50, "app2": 200, "app3": 50, "app4": 500}
    for b in backends:
        count = dist.get(b, 0)
        if count == 0:
            continue
        bar = "#" * count
        pct = count / total * 100
        avg_ms = sum(latencies_by_host.get(b, [0])) / max(len(latencies_by_host.get(b, [1])), 1)
        declared = declared_latency.get(b, "?")
        print(f"    {b}  ({declared:>3}ms backend)  {bar:<25} {count:>3} reqs  ({pct:.0f}%)  avg {avg_ms:.0f}ms")


def main():
    section("LOAD BALANCING LAB")
    print("""
  Architecture:
    Nginx (port 8080)
      ├─ app1: 50ms artificial latency
      ├─ app2: 200ms artificial latency
      ├─ app3: 50ms artificial latency
      └─ app4: 500ms artificial latency  ← simulates degraded node

  Nginx upstreams configured:
    /          → round-robin (default)
    /lc/       → least_conn
    /iphash/   → ip_hash
""")

    if not check_ready():
        print("  ERROR: Nginx is not responding. Run: docker compose up -d")
        return

    # ── Phase 1: Round-Robin ───────────────────────────────────────
    section("Phase 1: Round-Robin (20 sequential requests)")
    print("""
  Round-robin sends each request to the next backend in order:
  req1→app1, req2→app2, req3→app3, req4→app4, req5→app1, ...

  Every backend gets equal traffic regardless of its speed.
  This means slow backends (app4 at 500ms) receive the same load as fast ones.
""")
    dist, latencies = run_sequential("/", 20, "round-robin")
    latencies_by_host = defaultdict(list)
    for host, ms in latencies:
        latencies_by_host[host].append(ms)

    print_distribution(dist, latencies_by_host, 20)

    all_latencies = [ms for _, ms in latencies]
    all_latencies.sort()
    p50 = all_latencies[len(all_latencies) // 2]
    p99 = all_latencies[int(len(all_latencies) * 0.99)]
    avg = sum(all_latencies) / len(all_latencies)
    total_time = sum(all_latencies)
    print(f"\n  Overall: avg={avg:.0f}ms  p50={p50:.0f}ms  p99={p99:.0f}ms")
    print(f"  Total wall time for 20 requests (sequential): {total_time:.0f}ms")

    # ── Phase 2: The Tail Latency Problem ─────────────────────────
    section("Phase 2: The Tail Latency Problem")
    print("""
  With round-robin, app4 (500ms) gets 25% of requests.
  In a sequential client, one slow backend slows everything.
  In a concurrent system, slow backends accumulate connections.

  Consider: a page makes 4 parallel API calls routed round-robin.
  The slowest call (hitting app4) determines the page load time.
  This is "tail latency" — the 95th/99th percentile request time.

  Real-world impact:
    If 1% of backends are slow and you make 100 requests,
    the probability of hitting the slow backend at least once = 1-(0.99^100) ≈ 63%.
""")
    # Show that with concurrent requests, some threads will always hit app4
    results = []
    lock = threading.Lock()

    def concurrent_fetch():
        data, ms = fetch("/")
        with lock:
            results.append((data["host"] if data else "error", ms))

    print("  Sending 20 concurrent requests (round-robin)...")
    threads = [threading.Thread(target=concurrent_fetch) for _ in range(20)]
    wall_start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall_elapsed = (time.perf_counter() - wall_start) * 1000

    concurrent_dist = Counter(h for h, _ in results)
    concurrent_latencies = defaultdict(list)
    for h, ms in results:
        concurrent_latencies[h].append(ms)

    print_distribution(concurrent_dist, concurrent_latencies, 20)
    all_conc = sorted(ms for _, ms in results)
    p99_conc = all_conc[int(len(all_conc) * 0.95)]
    print(f"\n  Wall time (all 20 in parallel): {wall_elapsed:.0f}ms")
    print(f"  p95 individual latency: {p99_conc:.0f}ms  ← driven by app4")
    print("""
  The wall time is bounded by the slowest backend (app4 at ~500ms).
  Even though 75% of backends are fast, the slow one sets the ceiling.
""")

    # ── Phase 3: Least Connections ─────────────────────────────────
    section("Phase 3: Least Connections (least_conn)")
    print("""
  least_conn routes each new request to the backend with the
  FEWEST active connections. Fast backends complete requests quickly
  and become available sooner, so they naturally receive more traffic.

  Slow backends (app4) accumulate connections and receive fewer new ones.
  This is much better for variable-latency workloads.
""")
    dist_lc, latencies_lc = run_sequential("/lc/", 20, "least_conn")
    lc_by_host = defaultdict(list)
    for host, ms in latencies_lc:
        lc_by_host[host].append(ms)

    print_distribution(dist_lc, lc_by_host, 20)

    all_lc = sorted(ms for _, ms in latencies_lc)
    avg_lc = sum(all_lc) / len(all_lc)
    p99_lc = all_lc[int(len(all_lc) * 0.99)]
    print(f"\n  least_conn: avg={avg_lc:.0f}ms  p99={p99_lc:.0f}ms")
    print("""
  Note: with sequential requests, least_conn behaves like round-robin
  because each request completes before the next starts (0 active connections).
  The benefit of least_conn is most visible under concurrent load.

  Concurrent least_conn demonstration:
""")
    lc_results = []
    lock2 = threading.Lock()

    def concurrent_lc_fetch():
        data, ms = fetch("/lc/")
        with lock2:
            lc_results.append((data["host"] if data else "error", ms))

    threads = [threading.Thread(target=concurrent_lc_fetch) for _ in range(20)]
    wall_start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lc_wall = (time.perf_counter() - wall_start) * 1000

    lc_conc_dist = Counter(h for h, _ in lc_results)
    lc_conc_lat = defaultdict(list)
    for h, ms in lc_results:
        lc_conc_lat[h].append(ms)

    print_distribution(lc_conc_dist, lc_conc_lat, 20)
    print(f"\n  Wall time (20 concurrent, least_conn): {lc_wall:.0f}ms")
    print(f"  Compare to round-robin wall time: {wall_elapsed:.0f}ms")
    print("""
  With least_conn, fast backends (app1, app3 at 50ms) handle more
  requests because they free up faster. App4 at 500ms gets fewer
  new connections once it accumulates pending ones.
""")

    # ── Phase 4: IP Hash (Session Affinity) ────────────────────────
    section("Phase 4: IP Hash (Session Affinity / Sticky Sessions)")
    print("""
  ip_hash computes a hash of the client IP address and always routes
  that client to the same backend. This ensures session affinity:
  the same client always reaches the same server.

  When you need it:
    - Server-side sessions stored in memory (not externalized to Redis)
    - Stateful protocols that require the same server (WebSocket upgrades)
    - Shopping cart stored in server memory (legacy apps)

  When you DON'T need it (the right answer in interviews):
    - Stateless services (sessions in Redis, JWT tokens)
    - Microservices with externalized state
    - Any modern 12-factor app

  Since all our requests come from the same IP (localhost),
  all requests will go to the same backend:
""")
    dist_ip, latencies_ip = run_sequential("/iphash/", 8, "ip_hash")
    ip_by_host = defaultdict(list)
    for host, ms in latencies_ip:
        ip_by_host[host].append(ms)

    print_distribution(dist_ip, ip_by_host, 8)
    unique_backends = set(dist_ip.keys()) - {"error"}
    print(f"\n  All 8 requests went to: {unique_backends}")
    print("  Same source IP = same backend every time.")
    print("""
  The downside: if your sticky backend goes down, that user's session is lost.
  The fix: externalize session state to Redis, then you don't need ip_hash at all.
""")

    # ── Phase 5: Latency Numbers Reference ────────────────────────
    section("Phase 5: Load Balancer Algorithm Comparison")
    print("""
  ┌─────────────────────┬──────────────────────────────┬──────────────────────────────┐
  │ Algorithm           │ Best for                     │ Avoid when                   │
  ├─────────────────────┼──────────────────────────────┼──────────────────────────────┤
  │ Round-robin         │ Identical backends, equal    │ Backends have variable       │
  │                     │ request durations            │ latency or capacity          │
  ├─────────────────────┼──────────────────────────────┼──────────────────────────────┤
  │ Weighted round-robin│ Different-capacity backends  │ Request durations vary       │
  │                     │ (2x CPU → weight=2)          │ significantly                │
  ├─────────────────────┼──────────────────────────────┼──────────────────────────────┤
  │ Least connections   │ Variable request durations   │ Backends have very different │
  │                     │ (long-polling, file upload)  │ per-request overhead         │
  ├─────────────────────┼──────────────────────────────┼──────────────────────────────┤
  │ IP hash             │ Sticky sessions (legacy)     │ Stateless services (always   │
  │                     │                              │ prefer stateless design)     │
  ├─────────────────────┼──────────────────────────────┼──────────────────────────────┤
  │ Random              │ Simple, no state tracking    │ Backend capacities differ    │
  ├─────────────────────┼──────────────────────────────┼──────────────────────────────┤
  │ Power of two choices│ Large clusters, low overhead │ Small clusters (overkill)    │
  └─────────────────────┴──────────────────────────────┴──────────────────────────────┘

  Key insight from this lab:
    - Round-robin gave equal traffic to app4 (500ms) as to app1 (50ms)
    - Least-conn under concurrent load naturally avoids slow backends
    - IP-hash provides sticky sessions but breaks if backend fails

  Interview answer: "I would design stateless services so that sticky sessions
  are not needed. Any instance can handle any request. Load balancer uses
  least-conn to handle variable-latency backends gracefully."

  Next: ../08-databases-sql-vs-nosql/
""")


if __name__ == "__main__":
    main()
