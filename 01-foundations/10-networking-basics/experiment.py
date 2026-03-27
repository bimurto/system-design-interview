#!/usr/bin/env python3
"""
Networking Basics Lab — TCP, Keep-Alive, DNS, Bandwidth vs Latency

Prerequisites: docker compose up -d (wait ~10s for health checks to pass)

What this demonstrates:
  1. The famous latency numbers every engineer should know (reference table)
  2. TCP connection overhead: new connection per request vs keep-alive
  3. Bandwidth vs latency crossover: 1KB → 100KB → 1MB → 10MB payloads
  4. DNS resolution timing: first lookup vs cached vs IP literal
  5. TCP TIME_WAIT accumulation: why connection churn causes port exhaustion
"""

import http.client
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE_HOST = "localhost"
BASE_PORT = 8080
BASE_URL  = f"http://{BASE_HOST}:{BASE_PORT}"


def section(title: str) -> None:
    print(f"\n{'=' * 68}")
    print(f"  {title}")
    print("=" * 68)


def subsection(title: str) -> None:
    print(f"\n  -- {title} --")


def ensure_static_files() -> None:
    """Generate payload files if missing. Idempotent."""
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    required = {
        "1kb.bin":   1024,
        "100kb.bin": 100 * 1024,
        "1mb.bin":   1024 * 1024,
        "10mb.bin":  10 * 1024 * 1024,
    }
    missing = [f for f in required if not os.path.exists(os.path.join(static_dir, f))]
    if missing:
        print(f"  Generating missing static files: {missing}")
        subprocess.run([sys.executable, "create_static.py"], check=True)


def check_ready() -> bool:
    """Poll /health until nginx responds or give up after 20 attempts."""
    for attempt in range(20):
        try:
            with urllib.request.urlopen(f"{BASE_URL}/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        print(f"  Waiting for nginx... (attempt {attempt + 1}/20)")
        time.sleep(2)
    return False


# ── Phase 1: Latency Numbers ──────────────────────────────────────────────────

def phase_latency_numbers() -> None:
    section("Phase 1: Latency Numbers Every Engineer Must Know")
    print("""
  These numbers are approximate but stable over decades. The RATIOS matter
  more than exact values — commit the ORDER OF MAGNITUDE to memory.

  ┌──────────────────────────────────────┬───────────────┬──────────────────────┐
  │ Operation                            │ Latency       │ Relative to L1 cache │
  ├──────────────────────────────────────┼───────────────┼──────────────────────┤
  │ L1 cache reference                   │ 0.5 ns        │ 1x                   │
  │ Branch mispredict                    │ 5 ns          │ 10x                  │
  │ L2 cache reference                   │ 7 ns          │ 14x                  │
  │ Mutex lock/unlock                    │ 25 ns         │ 50x                  │
  │ Main memory (RAM) reference          │ 100 ns        │ 200x                 │
  │ Compress 1KB with Snappy             │ 3,000 ns      │ 6,000x               │
  │ Send 1KB over 1Gbps network          │ 10,000 ns     │ 20,000x              │
  │ Read 4KB randomly from SSD           │ 150,000 ns    │ 300,000x             │
  │ Read 1MB sequentially from memory    │ 250,000 ns    │ 500,000x             │
  │ Round trip within same datacenter    │ 500,000 ns    │ 1,000,000x           │
  │ Read 1MB sequentially from SSD       │ 1,000,000 ns  │ 2,000,000x           │
  │ HDD disk seek                        │ 10,000,000 ns │ 20,000,000x          │
  │ Read 1MB sequentially from HDD       │ 20,000,000 ns │ 40,000,000x          │
  │ Send packet US → Europe → US         │ 150,000,000 ns│ 300,000,000x         │
  └──────────────────────────────────────┴───────────────┴──────────────────────┘

  Scaled to human time (1 CPU cycle = 1 second):
    L1 cache    = 1 second       │ Mutex lock = 50 seconds
    RAM access  = 3.5 minutes    │ SSD random = 2.5 days
    HDD seek    = 3.9 months     │ Cross-US   = 4.8 years

  Design implications:
    • Network RTT (500µs intra-DC) is 5,000× slower than RAM access →
      minimize round-trips: batch, pipeline, cache aggressively
    • SSD random I/O (150µs) is fast enough for many caching use cases →
      RocksDB, LevelDB, and similar LSM stores exploit this
    • Cross-continental latency (150ms) is dominated by speed of light →
      the only fix is geographic distribution (CDN, multi-region)
    • HDD seek (10ms) costs as much as 20 intra-DC round trips →
      avoid random HDD reads in hot paths; sequential I/O is 2,000× faster
""")


# ── Phase 2: TCP Connection Overhead ─────────────────────────────────────────

def phase_tcp_overhead() -> tuple[float, float]:
    section("Phase 2: TCP Connection Overhead — New vs Keep-Alive")
    print("""
  TCP 3-way handshake:
    Client → SYN      → Server   (half-RTT)
    Client ← SYN-ACK  ← Server   (half-RTT)
    Client → ACK      → Server
    ─── connection established; now first request can be sent ───
    (TLS 1.2 adds 2 more RTTs on top; TLS 1.3 adds 1; QUIC 0-RTT = zero)

  Each new TCP connection costs at least 1 full RTT before a byte is sent.
  Keep-alive reuses the same TCP connection for multiple requests,
  eliminating that overhead AND avoiding TCP slow-start re-entry.
""")

    N = 20

    # ── New connection per request ────────────────────────────────────────────
    subsection("New TCP connection for each of the 20 requests")
    new_conn_times: list[float] = []
    for i in range(N):
        t0 = time.perf_counter()
        conn = http.client.HTTPConnection(BASE_HOST, BASE_PORT, timeout=5)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        elapsed_ms = (time.perf_counter() - t0) * 1000
        new_conn_times.append(elapsed_ms)
        print(f"    req {i+1:>2}: {elapsed_ms:.2f}ms", end="  ")
        if (i + 1) % 5 == 0:
            print()

    new_avg   = sum(new_conn_times) / len(new_conn_times)
    new_total = sum(new_conn_times)
    new_min   = min(new_conn_times)
    new_max   = max(new_conn_times)
    print(f"\n    Total={new_total:.1f}ms  avg={new_avg:.2f}ms  "
          f"min={new_min:.2f}ms  max={new_max:.2f}ms")

    # ── Keep-alive: one connection, many requests ─────────────────────────────
    subsection("Keep-alive: single TCP connection reused for all 20 requests")
    keepalive_times: list[float] = []
    # NOTE: the connection object is created ONCE outside the loop.
    # http.client.HTTPConnection uses HTTP/1.1 persistent connections by
    # default; the server must also support keep-alive (nginx does by default).
    ka_conn = http.client.HTTPConnection(BASE_HOST, BASE_PORT, timeout=5)
    for i in range(N):
        t0 = time.perf_counter()
        ka_conn.request("GET", "/health")
        resp = ka_conn.getresponse()
        resp.read()
        elapsed_ms = (time.perf_counter() - t0) * 1000
        keepalive_times.append(elapsed_ms)
        print(f"    req {i+1:>2}: {elapsed_ms:.2f}ms", end="  ")
        if (i + 1) % 5 == 0:
            print()
    ka_conn.close()

    ka_avg   = sum(keepalive_times) / len(keepalive_times)
    ka_total = sum(keepalive_times)
    ka_min   = min(keepalive_times)
    ka_max   = max(keepalive_times)
    print(f"\n    Total={ka_total:.1f}ms  avg={ka_avg:.2f}ms  "
          f"min={ka_min:.2f}ms  max={ka_max:.2f}ms")

    # ── Comparison ────────────────────────────────────────────────────────────
    print(f"""
  Results:
    New connection avg:   {new_avg:.2f}ms
    Keep-alive avg:       {ka_avg:.2f}ms
    Speedup:              {new_avg / ka_avg:.1f}x  (keep-alive is faster)
    Time saved over {N} requests: {new_total - ka_total:.1f}ms

  Why keep-alive wins:
    • Eliminates TCP handshake RTT on every request
    • Avoids TCP slow-start: reused connection is already at full cwnd
    • HTTP/1.1 keep-alive is the default; HTTP/2 multiplexes many streams
      on one connection (even better); HTTP/3/QUIC also persists across
      IP changes (e.g. switching WiFi → cellular)

  In production:
    • DB connection pools (psycopg2, HikariCP) reuse TCP connections
    • HTTP client session objects (requests.Session, httpx.Client) do this
    • Nginx `upstream keepalive N` directive reuses backend connections
    • Load balancer → backend keep-alive is critical at high RPS
""")
    return new_avg, ka_avg


# ── Phase 3: Bandwidth vs Latency Crossover ───────────────────────────────────

def phase_payload_size() -> list[tuple[str, float, int]]:
    section("Phase 3: Bandwidth vs Latency Crossover (1KB → 10MB)")
    print("""
  Transfer time = fixed_latency + (payload_size / bandwidth)

  On localhost the "bandwidth" is loopback speed (~10Gbps+) and
  latency is sub-millisecond, so the crossover point is at much larger
  payloads than on a real WAN link. The SHAPE of the curve is what matters.

  Theoretical WAN comparison (50ms RTT, 10Mbps link):
    1KB   = 50ms + 0.8ms   =  50.8ms  (latency-dominated)
    100KB = 50ms + 80ms    = 130ms    (transitional)
    1MB   = 50ms + 800ms   = 850ms    (bandwidth-dominated)
    10MB  = 50ms + 8,000ms = 8,050ms  (bandwidth-dominated)
""")

    sizes = [
        ("/health", "health check  (~5B)"),
        ("/1kb",    "1KB  payload       "),
        ("/100kb",  "100KB payload      "),
        ("/1mb",    "1MB  payload       "),
        ("/10mb",   "10MB payload       "),
    ]

    results: list[tuple[str, float, int]] = []

    # Use a fresh connection per payload size so we measure cold-start for each.
    # This isolates the per-transfer time from keep-alive amortization effects.
    for path, label in sizes:
        times: list[float] = []
        actual_size = 0
        for _ in range(5):
            conn = http.client.HTTPConnection(BASE_HOST, BASE_PORT, timeout=30)
            t0 = time.perf_counter()
            conn.request("GET", path)
            resp = conn.getresponse()
            data = resp.read()
            elapsed_ms = (time.perf_counter() - t0) * 1000
            conn.close()
            times.append(elapsed_ms)
            actual_size = len(data)

        avg = sum(times) / len(times)
        lo  = min(times)
        hi  = max(times)
        results.append((label, avg, actual_size))

        throughput_mbps = (actual_size * 8) / (avg / 1000) / 1_000_000
        print(f"  {label}  avg={avg:8.2f}ms  "
              f"[{lo:.2f}–{hi:.2f}ms]  "
              f"size={actual_size:>11,}B  "
              f"throughput={throughput_mbps:6.1f} Mbps")

    print("""
  Observations:
    • Health check and 1KB are nearly identical on localhost — both
      latency-dominated (loopback is so fast the transfer is free)
    • 10MB shows a clear time increase — bandwidth-dominated on localhost
      (loopback is ~10Gbps; over 10Mbps WAN that 10MB = 8+ seconds)
    • The throughput column shows Mbps; on WAN it would be much lower and
      would be the dominant term in the transfer-time equation

  Practical implications:
    • API responses should target < 1KB for latency-sensitive paths
    • Large payloads: use compression (gzip cuts JSON by 60–80%),
      pagination (never return unbounded lists), or streaming
    • Static assets (JS bundles, images): cache at CDN edge — moves the
      "server" from 150ms away to 10ms away, turning bandwidth into latency
    • Chunked transfer-encoding: lets clients begin processing before the
      full body arrives (important for large search results, exports)
""")
    return results


# ── Phase 4: DNS Resolution Timing ───────────────────────────────────────────

def phase_dns() -> None:
    section("Phase 4: DNS Resolution Timing")
    print("""
  DNS lookup chain (recursive resolver):
    1. OS cache (nscd / systemd-resolved) or /etc/hosts   → sub-ms
    2. Recursive resolver (ISP or 8.8.8.8 / 1.1.1.1)     → 10–50ms
    3. Resolver queries root NS → TLD NS → auth NS         → 100–300ms
    4. Result cached at resolver + OS with TTL

  Every first connection to a new hostname pays this cost.
  Subsequent connections within the TTL window pay ~0ms.
""")

    # We measure five rounds for each host to observe caching effects.
    # Round 1 may be a cold resolver lookup; rounds 2–5 should be cached.
    test_cases = [
        ("127.0.0.1",  "IP literal (no DNS at all)          "),
        ("localhost",  "localhost (/etc/hosts or mDNS)      "),
    ]

    for host, label in test_cases:
        times: list[float] = []
        for i in range(5):
            t0 = time.perf_counter()
            try:
                socket.getaddrinfo(host, 80, socket.AF_INET, socket.SOCK_STREAM)
            except Exception:
                pass
            elapsed_ms = (time.perf_counter() - t0) * 1000
            times.append(elapsed_ms)

        avg = sum(times) / len(times)
        per_round = "  ".join(f"r{i+1}={t:.3f}ms" for i, t in enumerate(times))
        print(f"  {label}  avg={avg:.3f}ms")
        print(f"    {per_round}")

    print("""
  Expected pattern:
    • IP literal: essentially 0ms — the OS skips DNS entirely
    • localhost: <1ms — resolved from /etc/hosts without any network query
    • External hostname (e.g. api.github.com) first call: 10–100ms;
      subsequent calls: <1ms once the OS resolver cache is warm

  DNS design considerations for distributed systems:
    ┌─────────────────┬─────────────────────────────────────────────────────┐
    │ Short TTL (30s) │ Fast failover, but high resolver load               │
    │ Long TTL (300s) │ Low resolver load, but slow propagation on change   │
    │ Negative TTL    │ How long NXDOMAIN is cached (prevent re-lookup)     │
    │ DNS-based LB    │ Multiple A records; client picks. No session sticky │
    │ GeoDNS          │ Return different IPs by resolver geography          │
    └─────────────────┴─────────────────────────────────────────────────────┘

  Incident gotcha: TTL too high
    If your TTL is 3600s and you need to cut traffic to a new IP (DDoS,
    region failover), clients will continue hitting the old address for
    up to 1 hour. Best practice: pre-lower TTL to 60–300s before planned
    migrations; restore it after the change stabilises.

  Kubernetes / microservices:
    • Every pod has a DNS stub resolver (CoreDNS)
    • Each service-to-service call may resolve a new hostname
    • Use connection pools to amortize DNS + TCP setup across many requests
    • ndots:5 in k8s means short names get 5 search-domain suffixes tried
      before the literal name — this can cause 5× DNS round-trips on miss
""")


# ── Phase 5: TCP TIME_WAIT — Connection Churn ─────────────────────────────────

def phase_time_wait() -> None:
    section("Phase 5: TCP TIME_WAIT — What Connection Churn Costs")
    print("""
  When a TCP connection is closed by the active closer (usually the client),
  the socket enters TIME_WAIT for 2×MSL (Maximum Segment Lifetime), typically
  60–120 seconds on Linux. The port cannot be reused during this window.

  A server making 1,000 req/s using new connections per request accumulates
  up to 60,000–120,000 sockets in TIME_WAIT simultaneously. With a default
  ephemeral port range of ~28,000 ports, port exhaustion follows.

  This experiment creates rapid short-lived connections and counts
  TIME_WAIT sockets that accumulate.
""")

    N = 40
    print(f"  Opening and closing {N} connections rapidly...")
    for _ in range(N):
        conn = http.client.HTTPConnection(BASE_HOST, BASE_PORT, timeout=5)
        conn.request("GET", "/health")
        conn.getresponse().read()
        conn.close()

    # On macOS the command is `netstat -an`; on Linux it may be `ss -tan`
    time_wait_count = 0
    try:
        if sys.platform == "darwin":
            result = subprocess.run(
                ["netstat", "-an"],
                capture_output=True, text=True, timeout=5
            )
            lines = result.stdout.splitlines()
        else:
            result = subprocess.run(
                ["ss", "-tan"],
                capture_output=True, text=True, timeout=5
            )
            lines = result.stdout.splitlines()

        time_wait_lines = [l for l in lines if "TIME_WAIT" in l and str(BASE_PORT) in l]
        time_wait_count = len(time_wait_lines)
        print(f"  TIME_WAIT sockets on port {BASE_PORT}: {time_wait_count}")
        if time_wait_lines:
            print(f"  Sample (first 5):")
            for l in time_wait_lines[:5]:
                print(f"    {l.strip()}")
    except Exception as e:
        print(f"  (Could not query socket state: {e})")

    print(f"""
  Interpretation:
    • Each closed connection enters TIME_WAIT for ~60s on the host OS
    • After {N} rapid connections you may see up to {N} TIME_WAIT entries
    • At 1,000 req/s with new connections: 60,000+ TIME_WAIT sockets
    • Default ephemeral port range (Linux: 32768–60999 = ~28k ports) is
      exhausted → new connection attempts fail with EADDRNOTAVAIL

  Mitigations:
    • Connection pooling (keep-alive) — the PRIMARY solution
    • SO_REUSEADDR / SO_REUSEPORT — allow port reuse in TIME_WAIT
    • net.ipv4.tcp_tw_reuse=1 (Linux) — safe reuse with timestamps
    • Avoid net.ipv4.tcp_tw_recycle — broken in NAT environments (removed
      from Linux 4.12+); DO NOT use this in production
    • Increase ephemeral range: sysctl net.ipv4.ip_local_port_range
""")


# ── Summary ───────────────────────────────────────────────────────────────────

def phase_summary(
    new_avg: float,
    ka_avg: float,
    payload_results: list[tuple[str, float, int]],
) -> None:
    section("Summary: Key Takeaways")
    speedup = (new_avg / ka_avg) if ka_avg > 0 else float("inf")
    print(f"""
  Lab measurements (localhost, loopback):
    TCP new-connection avg latency:  {new_avg:.2f}ms
    TCP keep-alive avg latency:      {ka_avg:.2f}ms
    Keep-alive speedup:              {speedup:.1f}x

  Payload transfer times (5-request avg, new connection each):""")
    for label, avg, size in payload_results:
        print(f"    {label}  {avg:8.2f}ms  ({size:>12,} bytes)")

    print("""
  Protocol comparison:
  ┌─────────────┬──────────┬───────────────────────────────────────────────┐
  │ Protocol    │ Layer    │ Key characteristic / use case                 │
  ├─────────────┼──────────┼───────────────────────────────────────────────┤
  │ TCP         │ L4       │ Reliable, ordered. HTTP, DB, file transfer    │
  │ UDP         │ L4       │ Best-effort. DNS, video, gaming, QUIC         │
  │ HTTP/1.1    │ L7/TCP   │ Persistent conn; HOL blocking per connection  │
  │ HTTP/2      │ L7/TCP   │ Stream mux; fixes app-layer HOL; TCP HOL stays│
  │ HTTP/3/QUIC │ L7/UDP   │ Per-stream reliability; 0-RTT; no TCP HOL    │
  │ WebSocket   │ L7/TCP   │ Full-duplex: chat, collaborative editing      │
  │ SSE         │ L7/HTTP  │ Server→client push only; HTTP-native, simpler │
  │ Long polling│ L7/HTTP  │ Legacy fallback; holds connection open        │
  └─────────────┴──────────┴───────────────────────────────────────────────┘

  Core mental models:
    1. Latency = physics (speed of light). Bandwidth = money (buy more capacity).
       Buying bandwidth does NOT reduce latency. The fix for latency is
       geographic distribution: CDN, multi-region, edge compute.

    2. Every round-trip costs. Reduce them: batch requests, pipeline,
       use connection pools, cache aggressively (RAM > SSD > HDD > network).

    3. DNS TTL = cache. During incidents, a high TTL blocks rapid IP failover.
       Pre-lower TTL before planned changes; keep production TTL at 60–300s.

    4. HTTP/2 ≠ no HOL blocking. It fixes the APPLICATION layer but TCP
       still blocks all streams on a single dropped segment. HTTP/3/QUIC
       fixes this at the transport layer by isolating loss to one stream.

    5. Connection churn → TIME_WAIT accumulation → port exhaustion.
       Connection pooling is the primary mitigation; it also eliminates
       TCP slow-start re-entry and amortizes TLS handshake cost.

  Next: ../11-api-design/
""")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    section("NETWORKING BASICS LAB")
    print("""
  Infrastructure: nginx on port 8080 (static payloads: 1KB, 100KB, 1MB, 10MB)
  All measurements run on localhost/loopback — no internet required.
  Real-world numbers over WAN will be significantly higher.
""")

    ensure_static_files()

    if not check_ready():
        print("\n  ERROR: nginx is not responding on port 8080.")
        print("  Run: docker compose up -d")
        print("  Then wait ~15s for health checks to pass, and re-run.")
        sys.exit(1)

    phase_latency_numbers()
    new_avg, ka_avg = phase_tcp_overhead()
    payload_results  = phase_payload_size()
    phase_dns()
    phase_time_wait()
    phase_summary(new_avg, ka_avg, payload_results)


if __name__ == "__main__":
    main()
