#!/usr/bin/env python3
"""
Scalability Lab — Round-Robin Load Balancing Demo

Prerequisites: docker compose up -d  (wait ~15s for all health checks to pass)

What this demonstrates:
  Phase 1 — Round-robin distributes traffic evenly across 3 healthy backends.
  Phase 2 — One backend fails; Nginx's passive health check stops routing to it.
  Phase 3 — All backends fail; the load balancer itself returns errors (total outage).
  Phase 4 — Backend recovers; traffic automatically resumes (self-healing).

Key concepts surfaced:
  - Stateless horizontal scaling: any backend can serve any request
  - Passive vs active health checks and their detection latency difference
  - The load balancer as a remaining single point of failure
  - Connection draining is NOT demonstrated here (requires Nginx Plus / HAProxy)
"""

import urllib.request
import urllib.error
import subprocess
import time
from collections import Counter

NGINX_URL = "http://localhost:8080"
SEPARATOR = "=" * 65


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_request(url=NGINX_URL, timeout=3):
    """Return (backend_label, latency_ms) or ("ERROR: ...", latency_ms)."""
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode().strip()
        return body, int((time.monotonic() - t0) * 1000)
    except Exception as exc:
        return f"ERROR: {exc}", int((time.monotonic() - t0) * 1000)


def send_requests(n, label, show_latency=False):
    """Send n requests, print a histogram of which backends responded."""
    print(f"\n--- {label} ({n} requests) ---")
    results = []
    latencies = []
    for _ in range(n):
        backend, ms = make_request()
        results.append(backend)
        latencies.append(ms)

    counts = Counter(results)
    for backend, count in sorted(counts.items()):
        bar = "#" * count
        print(f"  {backend:<35} {bar} ({count})")

    if show_latency and latencies:
        avg_ms = sum(latencies) / len(latencies)
        p99_ms = sorted(latencies)[int(len(latencies) * 0.99)]
        print(f"  Latency — avg: {avg_ms:.1f}ms  p99: {p99_ms}ms")

    return counts


def docker_compose(*args):
    """Run a docker compose sub-command, return True on success."""
    cmd = ["docker", "compose"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def stop_service(name):
    if not docker_compose("stop", name):
        # Fallback: direct container name used by Docker Compose v2
        subprocess.run(["docker", "stop", f"01-scalability-{name}-1"],
                       capture_output=True, text=True)


def start_service(name):
    if not docker_compose("start", name):
        subprocess.run(["docker", "start", f"01-scalability-{name}-1"],
                       capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(SEPARATOR)
    print("  SCALABILITY LAB: Horizontal Scaling with Nginx")
    print(SEPARATOR)

    # -----------------------------------------------------------------------
    # Phase 1 — Baseline: all 3 backends healthy
    # -----------------------------------------------------------------------
    print("\n[Phase 1] Baseline — all 3 backends are running.")
    print("Sending 30 requests through the Nginx load balancer...")
    counts = send_requests(30, "Round-robin across 3 backends", show_latency=True)

    errors = sum(v for k, v in counts.items() if "ERROR" in k)
    if errors:
        print(f"\n  WARNING: {errors} errors detected.")
        print("  Make sure 'docker compose up -d' has completed and all")
        print("  health checks have passed (check: docker compose ps).")
        return

    unique_backends = len([k for k in counts if "ERROR" not in k])
    print(f"\n  Backends observed: {unique_backends}/3")
    if unique_backends == 3:
        print("  CONFIRMED: Round-robin is distributing traffic across all 3 backends.")
        print("  Each backend receives ~1/3 of requests because they are stateless")
        print("  and identical — any backend can serve any request.")
    else:
        print("  Some backends may not be ready yet. Retry in a few seconds.")
        return

    # -----------------------------------------------------------------------
    # Phase 2 — Passive health check: one backend dies
    # -----------------------------------------------------------------------
    print("\n" + SEPARATOR)
    print("\n[Phase 2] Simulate backend failure — stopping app2.")
    print("  This models an instance crash, OOM kill, or deployment rollout.")
    stop_service("app2")

    print("  Waiting 3s for Nginx passive health check to detect the failure...")
    print("  NOTE: Nginx open-source uses PASSIVE checks — it only marks a")
    print("  backend down after a real request fails. Active health checks")
    print("  (Nginx Plus, HAProxy, Envoy) probe /health periodically and")
    print("  detect failures before client traffic is affected.")
    time.sleep(3)

    counts2 = send_requests(12, "After stopping app2 — 2 backends remain", show_latency=True)
    backends_after = [k for k in counts2 if "ERROR" not in k]
    error_count = sum(v for k, v in counts2.items() if "ERROR" in k)

    print(f"\n  Backends serving traffic: {len(backends_after)}")
    if error_count:
        print(f"  Errors (first probe to dead backend): {error_count}")
        print("  These errors are the cost of passive health checks — the first")
        print("  request to a dead backend fails before Nginx removes it from rotation.")
    if "Handled by: app2" not in backends_after:
        print("  app2 removed from rotation — passive failover succeeded.")
    else:
        print("  Note: app2 still in rotation; increase fail_timeout in nginx.conf.")

    # -----------------------------------------------------------------------
    # Phase 3 — Total outage: all backends down
    # -----------------------------------------------------------------------
    print("\n" + SEPARATOR)
    print("\n[Phase 3] Total outage — stopping ALL backends.")
    print("  This simulates a network partition or a bad deployment that takes")
    print("  every instance offline simultaneously.")
    stop_service("app1")
    stop_service("app3")
    time.sleep(2)

    print("\n  Sending 5 requests with all backends down...")
    counts3 = send_requests(5, "All backends stopped — expected 502/errors")
    all_errors = all("ERROR" in k for k in counts3)
    if all_errors:
        print("\n  CONFIRMED: With zero healthy backends, Nginx returns 502 Bad Gateway.")
        print("  The load balancer itself is now a SINGLE POINT OF FAILURE.")
        print("  In production: use DNS failover, Anycast VIPs, or a managed LB")
        print("  (AWS ALB, GCP LB) that is itself distributed across AZs.")
    else:
        print("\n  Unexpected: some requests succeeded despite all backends being stopped.")

    # -----------------------------------------------------------------------
    # Phase 4 — Self-healing: backends come back online
    # -----------------------------------------------------------------------
    print("\n" + SEPARATOR)
    print("\n[Phase 4] Recovery — restarting all backends.")
    print("  This models auto-scaling launching replacement instances or a")
    print("  deployment completing successfully after a rollback.")
    start_service("app1")
    start_service("app2")
    start_service("app3")

    print("  Waiting 12s for backends to pass their health checks and")
    print("  re-register with Nginx...")
    time.sleep(12)

    counts4 = send_requests(15, "After recovery — all backends back", show_latency=True)
    backends_recovered = len([k for k in counts4 if "ERROR" not in k])
    print(f"\n  Backends serving traffic after recovery: {backends_recovered}/3")
    if backends_recovered == 3:
        print("  CONFIRMED: All 3 backends recovered and traffic redistributed.")
        print("  Self-healing is automatic — no manual intervention required.")
    else:
        print(f"  Only {backends_recovered} backend(s) recovered so far.")
        print("  If fewer than 3, wait a few more seconds and re-run Phase 4 manually.")

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    print("\n" + SEPARATOR)
    print("  WHAT YOU JUST SAW")
    print(SEPARATOR)
    print("""
  1. STATELESS HORIZONTAL SCALING
     All 3 backends are identical and share no in-memory state.
     Any backend can handle any request. Adding a 4th backend
     requires only: start container + health check passes.

  2. PASSIVE vs ACTIVE HEALTH CHECKS
     Nginx open-source: first failed request detects a dead backend
     (client sees one error). Nginx Plus / HAProxy / Envoy: periodic
     /health probes detect failures before client traffic is affected.
     FAANG interviewers expect you to know this distinction.

  3. TOTAL OUTAGE IS STILL POSSIBLE
     The load balancer is itself a single point of failure. In
     production, use a managed LB (AWS ALB, GCP LB) that is
     internally distributed, or pair two LBs with a floating VIP
     (Keepalived / VRRP).

  4. SELF-HEALING AFTER RECOVERY
     When backends restart and pass health checks, Nginx automatically
     resumes sending them traffic. This is the foundation of
     auto-scaling: add instances under load, remove them when idle.

  5. DATABASE BOTTLENECK (not shown here)
     Once app servers scale horizontally, the writable primary DB
     becomes the bottleneck. Remedy order: read replicas → caching
     → sharding. Sharding is a last resort.

  Next topic: ../02-cap-theorem/
""")


if __name__ == "__main__":
    main()
