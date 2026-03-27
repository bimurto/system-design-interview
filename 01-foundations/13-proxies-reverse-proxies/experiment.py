#!/usr/bin/env python3
"""
Proxies & Reverse Proxies Lab — nginx as a reverse proxy in front of two backends

Prerequisites: docker compose up -d (wait ~5s)

What this demonstrates:
  1. Round-robin load balancing: requests alternate between backend-1 and backend-2
  2. Header forwarding: X-Forwarded-For and X-Real-IP show the original client IP
  3. Backend transparency: clients only see nginx (port 8080); backends are hidden
  4. Path-based routing: different paths hit the same proxy but backends echo the path
  5. Header spoofing: showing why X-Forwarded-For alone cannot be trusted
"""

import json
import time
import subprocess
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
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def wait_ready(url, label, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            requests.get(url + "/health", timeout=2)
            print(f"  {label} ready.")
            return True
        except Exception:
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
  Backend ports are internal — not meant for direct client access.
""")

    if not wait_ready(PROXY_URL, "nginx proxy"):
        return

    # ── Phase 1: Round-robin load balancing ───────────────────────
    section("Phase 1: Round-Robin Load Balancing")
    print("""  Sending 10 requests to nginx. Watch which backend handles each.
""")

    counts = Counter()
    for i in range(10):
        resp = requests.get(f"{PROXY_URL}/request/{i}")
        data = resp.json()
        backend = data["backend"]
        counts[backend] += 1
        print(f"  Request {i+1:2d} → handled by {backend}")

    print(f"\n  Distribution: {dict(counts)}")
    print("  Requests alternate between backends — this is round-robin.")

    # ── Phase 2: Header forwarding ─────────────────────────────────
    section("Phase 2: Header Forwarding (X-Forwarded-For, X-Real-IP)")
    print("""  nginx adds X-Real-IP and X-Forwarded-For so backends know
  the original client IP even though the connection came from nginx.
""")

    resp = requests.get(f"{PROXY_URL}/headers-demo")
    data = resp.json()

    print(f"  Backend received:")
    print(f"    X-Real-IP:       {data['x_real_ip'] or '(empty)'}  ← set by nginx to your IP")
    print(f"    X-Forwarded-For: {data['x_forwarded_for'] or '(empty)'}  ← chain of proxies")
    print(f"    Host:            {data['host']}")
    print("""
  The backend never sees your direct connection — only nginx's IP.
  X-Real-IP is the authoritative client IP (set by nginx, not client).
""")

    # ── Phase 3: Header spoofing ───────────────────────────────────
    section("Phase 3: Header Spoofing — Why X-Forwarded-For Is Untrustworthy")
    print("""  A client can send a fake X-Forwarded-For header.
  If nginx appends to it (proxy_add_x_forwarded_for), the backend
  sees: <spoofed-value>, <real-client-ip>

  A backend trusting the FIRST value in XFF for IP-based access
  control can be bypassed.
""")

    # Send a spoofed XFF header
    spoofed_resp = requests.get(
        f"{PROXY_URL}/headers-demo",
        headers={"X-Forwarded-For": "1.2.3.4"}  # attacker spoofs this
    )
    spoofed_data = spoofed_resp.json()

    print(f"  Client sent:       X-Forwarded-For: 1.2.3.4 (spoofed)")
    print(f"  Backend received:  X-Forwarded-For: {spoofed_data['x_forwarded_for']}")
    print(f"  Backend received:  X-Real-IP:       {spoofed_data['x_real_ip']}")
    print("""
  X-Real-IP is set by nginx from the actual TCP connection — cannot be spoofed.
  X-Forwarded-For is appended — the first entry can be forged.

  Rule: use X-Real-IP (or the LAST XFF entry) for access control, not the first.
""")

    # ── Phase 4: Backend transparency ─────────────────────────────
    section("Phase 4: Backend Transparency")
    print("""  Clients only know the proxy address. Backends are hidden.
  Hitting a backend directly bypasses all proxy logic (auth, rate limits, routing).
""")

    proxy_resp   = requests.get(f"{PROXY_URL}/test")
    backend_resp = requests.get(f"{BACKEND1_URL}/test")

    print(f"  Via proxy   (port 8080): backend={proxy_resp.json()['backend']}")
    print(f"  Direct hit  (port 8081): backend={backend_resp.json()['backend']}")
    print("""
  In production, backends should not be publicly reachable.
  Only the proxy's port is exposed. Backends run on private network addresses.

  Firewall rule: allow inbound on 443 (proxy) only.
  The proxy's security headers, rate limits, and auth apply to all traffic.
""")

    # ── Phase 5: Path-based routing ───────────────────────────────
    section("Phase 5: Path Routing — How API Gateways Work")
    print("""  A reverse proxy can route different URL paths to different backend services.
  This is the foundation of API gateway routing.

  Example paths (all hitting the same nginx):
""")

    paths = ["/api/users/42", "/api/orders/99", "/static/image.png", "/health"]
    for path in paths:
        resp = requests.get(f"{PROXY_URL}{path}")
        data = resp.json() if resp.headers.get("Content-Type", "").startswith("application/json") else {"path": path, "note": resp.text.strip()}
        echo_path = data.get("path", path)
        print(f"  GET {path:30s} → backend receives path: {echo_path}")

    print("""
  In a real API gateway:
    /api/users/*   → user-service:3001
    /api/orders/*  → order-service:3002
    /static/*      → S3 / CDN
    /health        → proxy responds directly (no backend needed)
""")

    # ── Summary ───────────────────────────────────────────────────
    section("Summary")
    print("""
  Reverse Proxy Responsibilities:
  ─────────────────────────────────────────────────────────────
  Load balancing      — distribute requests across backend pool
  SSL/TLS termination — one cert at the proxy; HTTP internally
  Header enrichment   — add X-Real-IP, X-Forwarded-For, Host
  Path routing        — route URL prefixes to different services
  Caching             — serve repeated responses without backend
  Compression         — gzip/brotli at the proxy; plain upstream
  Rate limiting       — enforce per-client limits at the edge

  Forward vs Reverse:
    Forward proxy  — in front of clients (VPN, outbound filtering)
    Reverse proxy  — in front of servers (load balancing, SSL)

  Key Security Rules:
    - Do NOT trust X-Forwarded-For first entry (can be spoofed)
    - DO trust X-Real-IP (set by first trusted proxy from TCP conn)
    - Backends should be unreachable directly from the internet
    - The proxy tier must itself be redundant (two instances + VIP)

  Reverse proxy → API gateway → service mesh:
    Each layer adds application awareness. Reverse proxy handles
    infrastructure concerns. API gateway adds auth/rate limits/
    routing. Service mesh adds east-west mTLS and circuit breaking.

  Next steps: ../14-failure-modes-reliability/
""")


if __name__ == "__main__":
    main()
