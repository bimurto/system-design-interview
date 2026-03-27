#!/usr/bin/env python3
"""
Proxies & Reverse Proxies Lab — nginx as a reverse proxy in front of two backends

Prerequisites: docker compose up -d  (services become ready automatically via health checks)

What this demonstrates:
  1. Round-robin load balancing: requests alternate between backend-1 and backend-2
  2. Header forwarding: X-Forwarded-For and X-Real-IP show the original client IP
  3. Header spoofing: nginx overwrites XFF so clients cannot fake their IP
  4. Backend transparency: clients only see nginx (port 8080); backends are hidden
  5. Path-based routing: /api/users/* → backend-1 only, /api/orders/* → backend-2 only
  6. Passive failover: stop one backend, nginx routes all traffic to the survivor
"""

import json
import subprocess
import time
from collections import Counter

try:
    import requests
except ImportError:
    print("Installing dependencies...")
    subprocess.run(["pip", "install", "requests", "-q"], check=True)
    import requests

PROXY_URL    = "http://localhost:8080"
BACKEND1_URL = "http://localhost:8081"
BACKEND2_URL = "http://localhost:8082"


def section(title):
    print(f"\n{'=' * 65}")
    print(f"  {title}")
    print("=" * 65)


def wait_ready(url, label, timeout=30):
    """Poll until the endpoint responds or timeout is reached."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(url + "/health", timeout=2)
            if r.status_code == 200:
                print(f"  {label} ready.")
                return True
        except Exception:
            pass
        time.sleep(1)
    print(f"  ERROR: {label} not ready at {url}. Run: docker compose up -d")
    return False


def main():
    section("PROXIES & REVERSE PROXIES LAB")
    print("""
  Architecture:
    client → nginx:8080 (reverse proxy) → backend-1:8081
                                         → backend-2:8082

  The client only knows about port 8080.
  Backends are on the Docker-internal network — not meant for
  direct client access. In production, firewall rules enforce this.
""")

    # Wait for all three services — checking only nginx is not enough;
    # if backends are slow, Phase 5's direct-hit comparison would fail.
    if not wait_ready(PROXY_URL, "nginx proxy"):
        return
    if not wait_ready(BACKEND1_URL, "backend-1"):
        return
    if not wait_ready(BACKEND2_URL, "backend-2"):
        return

    # ── Phase 1: Round-robin load balancing ───────────────────────────
    section("Phase 1: Round-Robin Load Balancing")
    print("""  Sending 10 requests to nginx. nginx has two backends in its
  upstream pool and uses the default round-robin algorithm.
""")

    counts = Counter()
    for i in range(10):
        resp = requests.get(f"{PROXY_URL}/request/{i}")
        data = resp.json()
        backend = data["backend"]
        counts[backend] += 1
        marker = "<-- backend-1" if backend == "backend-1" else "<-- backend-2"
        print(f"  Request {i+1:2d} → {backend}  {marker}")

    print(f"\n  Distribution: {dict(counts)}")
    if len(counts) == 2:
        print("  Both backends received traffic — round-robin is working.")
    else:
        print("  NOTE: only one backend responded; check if both containers are up.")
    print("""
  Round-robin is stateless: each new request goes to the next server
  in sequence regardless of current load. It works well when requests
  are roughly equal in cost. Use least-connections when request
  duration varies significantly (e.g., mixed fast/slow API calls).
""")

    # ── Phase 2: Header forwarding ─────────────────────────────────────
    section("Phase 2: Header Forwarding (X-Forwarded-For, X-Real-IP)")
    print("""  nginx adds X-Real-IP and X-Forwarded-For so backends know the
  original client IP, even though the TCP connection came from nginx.

  nginx.conf sets:
    proxy_set_header X-Real-IP       $remote_addr;
    proxy_set_header X-Forwarded-For $remote_addr;  ← overwrite, not append
""")

    resp = requests.get(f"{PROXY_URL}/headers-demo")
    data = resp.json()

    print(f"  Backend received:")
    print(f"    X-Real-IP:       {data['x_real_ip'] or '(empty)'}  ← set by nginx from TCP socket")
    print(f"    X-Forwarded-For: {data['x_forwarded_for'] or '(empty)'}  ← set by nginx (overwrite)")
    print(f"    Host:            {data['host']}")
    print("""
  The backend never sees your direct connection — only nginx's IP
  appears in the TCP socket. X-Real-IP is how the backend learns
  the actual client IP that nginx observed.
""")

    # ── Phase 3: Header spoofing ────────────────────────────────────────
    section("Phase 3: Header Spoofing — Why X-Forwarded-For Alone Is Untrustworthy")
    print("""  A client can send a fake X-Forwarded-For header to claim a
  different IP. Two nginx behaviors exist:

  UNSAFE:  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    → appends the real client IP: "<spoofed-value>, <real-client-ip>"
    → backend trusting the FIRST XFF entry is fooled

  SAFE:    proxy_set_header X-Forwarded-For $remote_addr;   (our config)
    → OVERWRITES with only what nginx saw on the TCP connection
    → client's spoofed value is discarded entirely

  Sending a spoofed XFF header now:
""")

    spoofed_resp = requests.get(
        f"{PROXY_URL}/headers-demo",
        headers={"X-Forwarded-For": "1.2.3.4"}   # attacker-supplied fake IP
    )
    spoofed_data = spoofed_resp.json()

    print(f"  Client sent:      X-Forwarded-For: 1.2.3.4  (spoofed)")
    print(f"  Backend received: X-Forwarded-For: {spoofed_data['x_forwarded_for']}")
    print(f"  Backend received: X-Real-IP:       {spoofed_data['x_real_ip']}")

    xff = spoofed_data['x_forwarded_for']
    if xff and "1.2.3.4" not in xff:
        print("""
  The spoofed value was discarded. nginx overwrote XFF with the real
  client IP it observed on the TCP socket. The backend cannot be tricked.
""")
    else:
        print("""
  NOTE: If you see '1.2.3.4' in XFF, the nginx config is using
  $proxy_add_x_forwarded_for (append mode) — the unsafe setting.
""")

    print("""  Interview rule:
    Use X-Real-IP for rate limiting and access control — it is set
    by the first trusted proxy and cannot be forged by clients.
    If you must use XFF (e.g., multi-hop proxies), trust only the
    LAST entry added by a proxy you control, not the first.
""")

    # ── Phase 4: Path-based routing ─────────────────────────────────────
    section("Phase 4: Path-Based Routing — Foundation of API Gateway Routing")
    print("""  nginx routes URL prefixes to dedicated upstream pools:

    nginx.conf:
      location /api/users/   { proxy_pass http://users_backend; }   → backend-1 only
      location /api/orders/  { proxy_pass http://orders_backend; }  → backend-2 only
      location /             { proxy_pass http://backends; }         → round-robin

  In a real system each upstream would point to a different service.
  The proxy presents a single hostname to clients; routing is internal.
""")

    routing_cases = [
        ("/api/users/42",     "users_backend",   "should hit backend-1 only"),
        ("/api/users/99",     "users_backend",   "should hit backend-1 only"),
        ("/api/orders/17",    "orders_backend",  "should hit backend-2 only"),
        ("/api/orders/88",    "orders_backend",  "should hit backend-2 only"),
        ("/request/1",        "backends",        "round-robin pool"),
        ("/request/2",        "backends",        "round-robin pool"),
    ]

    for path, expected_pool, note in routing_cases:
        resp = requests.get(f"{PROXY_URL}{path}")
        data = resp.json()
        backend = data.get("backend", "?")
        print(f"  GET {path:25s} → {backend:12s}  ({note})")

    print("""
  This is the pattern behind AWS ALB path-based routing rules,
  Kong route objects, and Kubernetes Ingress resources. The reverse
  proxy is the single entry point; internal topology stays hidden.
""")

    # ── Phase 5: Backend transparency ───────────────────────────────────
    section("Phase 5: Backend Transparency — Why Direct Access Is Dangerous")
    print("""  In production, only the proxy's port is exposed to the internet.
  Backends run on a private network with no public IP.

  If a backend is directly reachable, all proxy-enforced controls
  (auth, rate limiting, header sanitization, WAF) are bypassed.

  Here the backend ports are exposed for demo purposes. Notice the
  difference between going via proxy vs hitting a backend directly:
""")

    proxy_resp   = requests.get(f"{PROXY_URL}/test")
    backend_resp = requests.get(f"{BACKEND1_URL}/test")

    print(f"  Via proxy   → port 8080: backend={proxy_resp.json()['backend']}, "
          f"x_real_ip={proxy_resp.json()['x_real_ip'] or '(set by nginx)'}")
    print(f"  Direct hit  → port 8081: backend={backend_resp.json()['backend']}, "
          f"x_real_ip={backend_resp.json()['x_real_ip'] or '(empty — no proxy headers!)'}")

    print("""
  The direct request has no X-Real-IP, no X-Forwarded-For.
  Any backend logic that depends on those headers (rate limiting,
  geo-restriction, audit logging) is silently skipped.

  Production rule: firewall inbound 443 to the proxy IPs only.
  Backends accept connections from the proxy's private IP only.
""")

    # ── Phase 6: Passive failover ────────────────────────────────────────
    section("Phase 6: Passive Failover — nginx Upstream Health Checking")
    print("""  nginx.conf sets max_fails=3 fail_timeout=30s on each upstream server.
  After 3 consecutive failures in 30 s, nginx marks the server down
  and stops routing to it until the fail_timeout expires.

  This is *passive* health checking — nginx learns about failures
  from real requests, not probes. (Active probes require nginx Plus.)

  Simulating backend-2 going down by hitting it on the internal port
  (we cannot stop the container without docker, but we can observe
  nginx's behavior when one backend is slower or returns errors).

  Sending 6 requests while both backends are healthy:
""")

    counts_before = Counter()
    for i in range(6):
        resp = requests.get(f"{PROXY_URL}/failover-test/{i}", timeout=5)
        data = resp.json()
        counts_before[data["backend"]] += 1

    print(f"  Distribution (both up): {dict(counts_before)}")

    print("""
  In a real scenario: stop backend2 with `docker compose stop backend2`,
  then re-run this script. nginx will initially try backend2 (and fail),
  then mark it down and route all traffic to backend1.

  The key insight: nginx's upstream failover is automatic and
  transparent to clients. The retry happens within the same request
  (proxy_next_upstream on error/timeout), so clients see a small
  latency spike but not an error.

  proxy_next_upstream directive controls which failure types trigger
  a retry: error, timeout, http_500, http_502, http_503, http_504.
  Be careful with non-idempotent requests (POST, PATCH) — retrying
  them can cause duplicate side effects.
""")

    # ── Summary ─────────────────────────────────────────────────────────
    section("Summary")
    print("""
  Reverse Proxy Responsibilities (shown in this lab):
  ─────────────────────────────────────────────────────────────────
  Load balancing        — round-robin across backend pool (Phase 1)
  Header enrichment     — X-Real-IP, X-Forwarded-For (Phase 2)
  Security enforcement  — overwrite XFF to prevent IP spoofing (Phase 3)
  Path routing          — URL prefix → dedicated backend pool (Phase 4)
  Backend hiding        — single public entry point (Phase 5)
  Passive failover      — automatic retry on upstream failure (Phase 6)

  Also handled by a reverse proxy (not shown here):
    SSL/TLS termination — one cert at proxy; plain HTTP internally
    Caching             — serve cached responses without hitting backend
    Compression         — gzip/brotli at proxy; uncompressed upstream
    Rate limiting       — per-client limits at the edge (nginx limit_req)
    Connection pooling  — keepalive to backends; mux many clients over few conns

  Proxy → API Gateway → Service Mesh:
    Reverse proxy   — infrastructure concerns (SSL, LB, caching, routing)
    API gateway     — application concerns (JWT auth, per-client rate limits,
                      request transformation, API versioning)
    Service mesh    — east-west concerns (mTLS between services, circuit
                      breaking, distributed tracing via sidecar proxies)

  Key Security Rules:
    - Use X-Real-IP (not first XFF entry) for rate limiting / access control
    - Overwrite XFF at the trusted proxy boundary ($remote_addr, not append)
    - Backends must be unreachable directly (private network + firewall)
    - The proxy tier itself must be redundant (2 instances + VIP failover)
    - Watch proxy_next_upstream on non-idempotent endpoints — retry = duplicate

  Next: ../14-failure-modes-reliability/
""")


if __name__ == "__main__":
    main()
