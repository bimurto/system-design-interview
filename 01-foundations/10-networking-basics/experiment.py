#!/usr/bin/env python3
"""
Networking Basics Lab — TCP, Keep-Alive, DNS, Latency Numbers

Prerequisites: docker compose up -d (wait ~10s)

What this demonstrates:
  1. The famous latency numbers every engineer should know
  2. TCP connection overhead: new connection per request vs keep-alive
  3. Response time vs payload size (1KB vs 100KB vs 1MB)
  4. DNS resolution timing with socket.getaddrinfo
"""

import socket
import time
import http.client
import urllib.request
import urllib.error
import subprocess

BASE_HOST = "localhost"
BASE_PORT = 8080
BASE_URL  = f"http://{BASE_HOST}:{BASE_PORT}"


def section(title):
    print(f"\n{'=' * 64}")
    print(f"  {title}")
    print("=" * 64)


def check_ready():
    for attempt in range(10):
        try:
            with urllib.request.urlopen(f"{BASE_URL}/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        print(f"  Waiting for nginx... (attempt {attempt + 1}/10)")
        time.sleep(2)
    return False


# ── Phase 1: Latency Numbers ───────────────────────────────────────────────────

def phase_latency_numbers():
    section("Phase 1: Latency Numbers Every Engineer Must Know")
    print("""
  These numbers are approximate but stable over time. The ratios matter
  more than the exact values — commit the ORDER OF MAGNITUDE to memory.

  ┌──────────────────────────────────────┬──────────────┬─────────────────────┐
  │ Operation                            │ Latency      │ Relative to L1      │
  ├──────────────────────────────────────┼──────────────┼─────────────────────┤
  │ L1 cache reference                   │ 0.5 ns       │ 1x                  │
  │ Branch mispredict                    │ 5 ns         │ 10x                 │
  │ L2 cache reference                   │ 7 ns         │ 14x                 │
  │ Mutex lock/unlock                    │ 25 ns        │ 50x                 │
  │ Main memory (RAM) reference          │ 100 ns       │ 200x                │
  │ Compress 1KB with Snappy             │ 3,000 ns     │ 6,000x              │
  │ Send 1KB over 1Gbps network          │ 10,000 ns    │ 20,000x             │
  │ Read 4KB randomly from SSD           │ 150,000 ns   │ 300,000x            │
  │ Read 1MB sequentially from memory    │ 250,000 ns   │ 500,000x            │
  │ Round trip within same datacenter    │ 500,000 ns   │ 1,000,000x          │
  │ Read 1MB sequentially from SSD       │ 1,000,000 ns │ 2,000,000x          │
  │ Disk seek (HDD)                      │ 10,000,000 ns│ 20,000,000x         │
  │ Read 1MB sequentially from HDD       │ 20,000,000 ns│ 40,000,000x         │
  │ Send packet US → Europe → US         │ 150,000,000 ns│ 300,000,000x       │
  └──────────────────────────────────────┴──────────────┴─────────────────────┘

  In human terms (scaled to 1 CPU cycle = 1 second):
    L1 cache    = 1 second
    RAM access  = 3.5 minutes
    SSD read    = 2.5 days
    HDD seek    = 3.9 months
    Cross-US    = 4.8 years

  Key design implications:
    - Network RTT (500µs intra-DC) is 5,000x slower than RAM access
    - HDD seek (10ms) is 100,000x slower than L1 cache
    - Minimize round-trips: batch, pipeline, cache aggressively
    - SSD random I/O (150µs) is fast enough for many caching use cases
    - Cross-continental latency (150ms) is dominated by speed of light
""")


# ── Phase 2: TCP Connection Overhead ──────────────────────────────────────────

def phase_tcp_overhead():
    section("Phase 2: TCP Connection Overhead — New vs Keep-Alive")
    print("""
  TCP 3-way handshake:
    Client → SYN   → Server
    Client ← SYN-ACK ← Server
    Client → ACK   → Server
    (then TLS handshake adds 1-2 more round-trips for HTTPS)

  Each new connection = at least 1 RTT of overhead before first byte.
  Keep-alive reuses the same TCP connection for multiple requests.
""")

    N = 20

    # New connection per request
    new_conn_times = []
    for _ in range(N):
        t0 = time.perf_counter()
        conn = http.client.HTTPConnection(BASE_HOST, BASE_PORT, timeout=5)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        new_conn_times.append((time.perf_counter() - t0) * 1000)

    new_avg = sum(new_conn_times) / len(new_conn_times)
    new_total = sum(new_conn_times)

    # Keep-alive: one connection, many requests
    keepalive_times = []
    conn = http.client.HTTPConnection(BASE_HOST, BASE_PORT, timeout=5)
    for _ in range(N):
        t0 = time.perf_counter()
        conn.request("GET", "/health")
        resp = conn.getresponse()
        resp.read()
        keepalive_times.append((time.perf_counter() - t0) * 1000)
    conn.close()

    ka_avg = sum(keepalive_times) / len(keepalive_times)
    ka_total = sum(keepalive_times)

    print(f"  {N} requests — new connection each time:")
    print(f"    Total: {new_total:.1f}ms   Avg per request: {new_avg:.2f}ms")
    print(f"\n  {N} requests — keep-alive (single connection):")
    print(f"    Total: {ka_total:.1f}ms   Avg per request: {ka_avg:.2f}ms")

    if new_avg > 0 and ka_avg > 0:
        speedup = new_avg / ka_avg
        print(f"\n  Keep-alive speedup: {speedup:.1f}x faster per request")

    print("""
  Why keep-alive wins:
    - Eliminates TCP handshake RTT on every request
    - Avoids TCP slow-start: reuses connection that's already at full window
    - HTTP/1.1 keep-alive is default; HTTP/2 multiplexes many streams on one conn
    - HTTP/3 (QUIC) eliminates TCP handshake entirely — 0-RTT on reconnects

  In production:
    - Connection pools (psycopg2, redis-py) reuse connections to databases
    - HTTP clients (requests, httpx) use session objects for connection reuse
    - Nginx upstream keepalive: reuse connections to backend servers
""")
    return new_avg, ka_avg


# ── Phase 3: Payload Size vs Response Time ────────────────────────────────────

def phase_payload_size():
    section("Phase 3: Response Time vs Payload Size")
    print("""
  Bandwidth vs Latency:
    - Latency = time for first byte to arrive (fixed cost: ~RTT + processing)
    - Bandwidth = how fast subsequent bytes transfer (variable with size)
    - Small payload: dominated by latency (connection + headers)
    - Large payload: dominated by bandwidth (transfer time)
""")

    sizes = [
        ("/health", "health check (~5B)"),
        ("/1kb",    "1KB payload"),
        ("/100kb",  "100KB payload"),
        ("/1mb",    "1MB payload"),
    ]

    conn = http.client.HTTPConnection(BASE_HOST, BASE_PORT, timeout=10)
    results = []

    for path, label in sizes:
        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            conn.request("GET", path)
            resp = conn.getresponse()
            data = resp.read()
            elapsed = (time.perf_counter() - t0) * 1000
            times.append(elapsed)
        conn.close()
        conn = http.client.HTTPConnection(BASE_HOST, BASE_PORT, timeout=10)

        avg = sum(times) / len(times)
        results.append((label, avg, len(data) if times else 0))
        print(f"  {label:<25} avg={avg:.2f}ms  (actual size: {len(data):,} bytes)")

    conn.close()

    print("""
  Observations:
    - Small responses (health check) show the baseline RTT overhead
    - Time grows with payload but not linearly — TCP pipelining helps
    - On localhost, bandwidth is not a bottleneck; latency is nearly zero
    - In production over WAN: 1MB at 10Mbps = 800ms transfer time alone
    - Compression (gzip) can reduce JSON by 60-80%, critical for large API responses

  CDN strategy:
    - Static assets (JS, CSS, images): cache at edge PoPs → ~10ms instead of 150ms
    - Dynamic API responses: can't be cached, but keep them small
    - Streaming responses: use chunked transfer-encoding for large payloads
""")
    return results


# ── Phase 4: DNS Timing ────────────────────────────────────────────────────────

def phase_dns():
    section("Phase 4: DNS Resolution Timing")
    print("""
  DNS lookup chain (recursive resolver):
    1. Check OS cache (nscd / systemd-resolved)
    2. Check local /etc/hosts
    3. Query recursive resolver (ISP or 8.8.8.8)
    4. Resolver queries root nameserver → TLD → authoritative NS
    5. Result cached at each level with TTL

  DNS adds latency to every first connection to a new hostname.
  Typical first lookup: 10-100ms. Cached lookup: <1ms.
""")

    # Localhost — should be in /etc/hosts or instantly resolved
    hosts = [
        ("localhost",       "localhost (/etc/hosts)"),
        ("127.0.0.1",       "127.0.0.1 (IP literal — no DNS)"),
    ]

    for host, label in hosts:
        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            try:
                socket.getaddrinfo(host, 80, socket.AF_INET)
            except Exception:
                pass
            elapsed = (time.perf_counter() - t0) * 1000
            times.append(elapsed)
        avg = sum(times) / len(times)
        print(f"  {label:<40} avg={avg:.3f}ms")

    print("""
  DNS design considerations:
    - Low TTLs (30s): fast failover but high resolver load
    - High TTLs (300s+): low resolver load but slow propagation on change
    - Negative TTL: how long "NXDOMAIN" is cached (prevents re-lookup of missing names)
    - DNS-based load balancing: return multiple A records, client picks one
    - GeoDNS: return different IPs based on client's resolver location

  In microservices:
    - Service discovery (Consul, Kubernetes DNS) uses DNS internally
    - Each service call does a DNS lookup: use connection pools + DNS caching
    - HTTP/2 and HTTP/3 multiplexing reduces the need for many connections
""")


# ── Summary ────────────────────────────────────────────────────────────────────

def phase_summary(new_avg, ka_avg, payload_results):
    section("Summary: Networking Concepts")
    print(f"""
  Lab results:
    TCP new connection avg latency:  {new_avg:.2f}ms
    TCP keep-alive avg latency:      {ka_avg:.2f}ms
    Keep-alive speedup:              {new_avg/ka_avg:.1f}x

  Payload sizes (keep-alive, 5-request avg):""")
    for label, avg, size in payload_results:
        print(f"    {label:<25} {avg:.2f}ms")

    print("""
  Protocol comparison:
  ┌─────────────┬──────────┬──────────┬─────────────────────────────────────┐
  │ Protocol    │ Layer    │ Ordered? │ Use case                            │
  ├─────────────┼──────────┼──────────┼─────────────────────────────────────┤
  │ TCP         │ L4       │ Yes      │ HTTP, databases, file transfer       │
  │ UDP         │ L4       │ No       │ DNS, video streaming, gaming, QUIC  │
  │ HTTP/1.1    │ L7/TCP   │ Yes      │ Most web traffic; keep-alive reuse  │
  │ HTTP/2      │ L7/TCP   │ Yes      │ Multiplexed streams; header compress│
  │ HTTP/3      │ L7/QUIC  │ Yes      │ 0-RTT, no HOL blocking, mobile-friendly│
  │ WebSocket   │ L7/TCP   │ Yes      │ Bidirectional: chat, live updates   │
  │ SSE         │ L7/HTTP  │ Yes      │ Server→client push, simpler than WS │
  │ Long polling│ L7/HTTP  │ Yes      │ Fallback: server holds request open │
  └─────────────┴──────────┴──────────┴─────────────────────────────────────┘

  WebSocket vs SSE vs Long Polling:
    WebSocket: full-duplex, client and server both send. Use for chat, collab.
    SSE: server-push only, built on HTTP. Use for dashboards, notifications.
    Long polling: server holds response until data available. Legacy fallback.

  Bandwidth vs Latency:
    Latency: fixed cost per request — optimize with keep-alive, caching, CDN
    Bandwidth: variable with size — optimize with compression, pagination
    "Fat and slow" pipe: high bandwidth, high latency → satellite internet
    "Thin and fast" pipe: low bandwidth, low latency → local Ethernet

  Next: ../11-api-design/
""")


def main():
    section("NETWORKING BASICS LAB")
    print("""
  Services:
    nginx (port 8080) — serves static payloads of 1KB, 100KB, 1MB

  This lab does NOT require internet. All measurements are localhost.
  Real-world numbers will be larger due to network RTT.
""")

    if not check_ready():
        print("  ERROR: nginx is not responding.")
        print("  Run: docker compose up -d && sleep 10")
        return

    phase_latency_numbers()
    new_avg, ka_avg = phase_tcp_overhead()
    payload_results = phase_payload_size()
    phase_dns()
    phase_summary(new_avg, ka_avg, payload_results)


if __name__ == "__main__":
    main()
