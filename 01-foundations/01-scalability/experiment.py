#!/usr/bin/env python3
"""
Scalability Lab — Round-Robin Load Balancing Demo

Prerequisites: docker compose up -d (wait ~5s for containers to start)

What this demonstrates:
  - Nginx distributes requests evenly across 3 backends (round-robin)
  - When one backend is removed, traffic automatically routes to the remaining two
"""

import urllib.request
import urllib.error
import subprocess
import time
from collections import Counter

NGINX_URL = "http://localhost:8080"


def make_request():
    try:
        with urllib.request.urlopen(NGINX_URL, timeout=3) as resp:
            return resp.read().decode().strip()
    except Exception as e:
        return f"ERROR: {e}"


def send_requests(n, label):
    print(f"\n--- {label} ({n} requests) ---")
    results = []
    for _ in range(n):
        results.append(make_request())

    counts = Counter(results)
    for backend, count in sorted(counts.items()):
        bar = "#" * count
        print(f"  {backend:<30} {bar} ({count})")
    return counts


def main():
    print("=" * 60)
    print("  SCALABILITY LAB: Horizontal Scaling with Nginx")
    print("=" * 60)

    # Phase 1: All 3 backends up
    print("\n[Phase 1] All 3 app backends are running.")
    print("Sending 30 requests to Nginx load balancer...")
    counts = send_requests(30, "Round-robin across 3 backends")

    errors = sum(v for k, v in counts.items() if "ERROR" in k)
    if errors:
        print(f"\n  WARNING: {errors} errors. Is 'docker compose up -d' running?")
        return

    unique_backends = len([k for k in counts if "ERROR" not in k])
    print(f"\n  Backends observed: {unique_backends}/3")
    if unique_backends == 3:
        print("  Round-robin confirmed — traffic distributed across all 3 backends.")
    else:
        print("  Some backends may not be ready yet. Try again in a few seconds.")

    # Phase 2: Remove one backend
    print("\n[Phase 2] Stopping app2 to simulate a backend failure...")
    result = subprocess.run(
        ["docker", "compose", "stop", "app2"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        # Try alternate container naming
        subprocess.run(
            ["docker", "stop", "01-scalability-app2-1"],
            capture_output=True, text=True
        )

    print("  Waiting 2s for Nginx to detect the downed backend...")
    time.sleep(2)

    counts2 = send_requests(10, "After stopping app2 — 2 backends remain")
    backends_after = [k for k in counts2 if "ERROR" not in k]
    print(f"\n  Backends observed after failure: {len(backends_after)}")
    if "Handled by: app2" not in backends_after:
        print("  app2 is no longer receiving traffic — failover works!")
    else:
        print("  Note: Nginx may still be routing to app2; it uses passive health checks.")

    # Explanation
    print("\n" + "=" * 60)
    print("  WHAT YOU JUST SAW")
    print("=" * 60)
    print("""
  Horizontal Scaling Key Points:
  --------------------------------
  1. STATELESS SERVICES scale horizontally by adding identical
     instances behind a load balancer. No shared in-memory state
     means any backend can handle any request.

  2. ROUND-ROBIN distributes load evenly when all backends are
     identical in capacity.

  3. PASSIVE HEALTH CHECKS (Nginx default) remove a backend only
     after it fails to respond. Active health checks (Nginx Plus /
     HAProxy) detect failures faster.

  4. THE SCALING BOTTLENECK shifts to the database once you add
     enough app servers. Read replicas come before sharding.

  Next steps: ../02-cap-theorem/
""")

    # Restart app2 so teardown is clean
    subprocess.run(["docker", "compose", "start", "app2"],
                   capture_output=True, text=True)


if __name__ == "__main__":
    main()
