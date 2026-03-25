#!/usr/bin/env python3
"""
CDN & Edge Lab — Nginx as a CDN proxy in front of a Python origin server.

What this demonstrates:
  1. First request → cache MISS (slow, ~200ms origin latency)
  2. Repeated requests → cache HIT (fast, <5ms from Nginx cache)
  3. Cache-Control: no-store → never stored in cache at all
  4. TTL expiry → cache MISS after TTL, then HIT again
  5. URL versioning (?v=2) → cache miss, fresh content
"""

import os
import time
import requests

NGINX = f"http://{os.environ.get('NGINX_HOST', 'localhost')}:80"


def section(title):
    print(f"\n{'=' * 65}")
    print(f"  {title}")
    print("=" * 65)


def get(path, **kwargs):
    """GET through nginx, return (response, elapsed_ms, cache_status)."""
    start = time.time()
    r = requests.get(f"{NGINX}{path}", **kwargs)
    elapsed = (time.time() - start) * 1000
    status = r.headers.get("X-Cache-Status", "N/A")
    return r, elapsed, status


def main():
    section("CDN & EDGE LAB")
    print("""
  Architecture:

  [Client] ──→ [Nginx Edge/CDN cache] ──→ [Origin Server]
                    ↑                          ↑
               nginx_cache               200ms latency
               (30s TTL)                 (simulated)

  Nginx acts as the CDN PoP (Point of Presence).
  Origin serves content with Cache-Control headers.
  Nginx caches based on those headers and exposes X-Cache-Status.
""")

    # ── Phase 1: Cache MISS then HIT ──────────────────────────────
    section("Phase 1: Cache MISS → HIT")
    print("  Requesting /content/image.jpg twice:\n")

    r1, ms1, status1 = get("/content/image.jpg")
    print(f"  Request 1: {status1:<8}  {ms1:6.1f}ms  '{r1.text[:50]}'")

    r2, ms2, status2 = get("/content/image.jpg")
    print(f"  Request 2: {status2:<8}  {ms2:6.1f}ms  '{r2.text[:50]}'")

    r3, ms3, status3 = get("/content/image.jpg")
    print(f"  Request 3: {status3:<8}  {ms3:6.1f}ms  (same cached response)")

    speedup = ms1 / ms2 if ms2 > 0 else 999
    print(f"\n  Cache speedup: {speedup:.0f}x faster on HIT vs MISS")
    print(f"  MISS={ms1:.1f}ms (round-trip to origin) vs HIT={ms2:.1f}ms (from cache)")
    print(f"""
  Cache-Control flow:
    Origin response: Cache-Control: public, max-age=30
         ↓
    Nginx: cache for 30s, serve from memory on repeated requests
         ↓
    Client sees: X-Cache-Status: MISS (first) → HIT (subsequent)
""")

    # ── Phase 2: no-store never stored in cache ───────────────────
    # Note: no-store = never cache this at all (our /nocache endpoint)
    #       no-cache = revalidate with origin before serving from cache
    section("Phase 2: Cache-Control: no-store — Never Stored in Cache")
    print("  Requesting /nocache/data.json (Cache-Control: no-store):\n")

    for i in range(1, 4):
        r, ms, status = get("/nocache/data.json")
        print(f"  Request {i}: {status:<8}  {ms:6.1f}ms  (always hits origin)")

    print(f"""
  Cache-Control directives explained:
    no-cache   → must revalidate with origin before serving (still caches)
    no-store   → never cache at all (our /nocache endpoint uses both)
    public     → CDN/proxy may cache this response
    private    → only browser cache, not CDN (e.g., personalised content)
    max-age=N  → cache for N seconds
    s-maxage=N → CDN-specific override of max-age (browser ignores s-maxage)
""")

    # ── Phase 3: TTL expiry ────────────────────────────────────────
    section("Phase 3: TTL Expiry — Cache Goes Stale")
    print("  /short-ttl/report.pdf has Cache-Control: max-age=5 (5s TTL)\n")

    r, ms, status = get("/short-ttl/report.pdf")
    print(f"  Request 1: {status:<8}  {ms:6.1f}ms  (MISS — fetches from origin)")

    r, ms, status = get("/short-ttl/report.pdf")
    print(f"  Request 2: {status:<8}  {ms:6.1f}ms  (HIT — served from cache)")

    print("  Waiting 6 seconds for TTL to expire...")
    time.sleep(6)

    r, ms, status = get("/short-ttl/report.pdf")
    print(f"  Request 3: {status:<8}  {ms:6.1f}ms  (cache expired — MISS again)")

    r, ms, status = get("/short-ttl/report.pdf")
    print(f"  Request 4: {status:<8}  {ms:6.1f}ms  (re-cached — HIT)")

    print(f"""
  TTL strategy:
    Static assets (JS, CSS, images): max-age=31536000 (1 year) + URL versioning
    API responses: max-age=30 to 300 seconds depending on freshness requirements
    HTML pages: max-age=0 with ETag/Last-Modified for conditional requests
    User data: private, no-store (never cache in CDN)
""")

    # ── Phase 4: URL versioning for cache invalidation ─────────────
    section("Phase 4: Cache Invalidation via URL Versioning")
    print("""  Best practice: instead of purging cache, version the URL.
  Same content path, different ?v= parameter = different cache key.
""")

    r_v1, ms_v1, st_v1 = get("/content/bundle.js?v=1")
    print(f"  ?v=1 Request 1: {st_v1:<8}  {ms_v1:6.1f}ms  (MISS — cold)")

    r_v1, ms_v1, st_v1 = get("/content/bundle.js?v=1")
    print(f"  ?v=1 Request 2: {st_v1:<8}  {ms_v1:6.1f}ms  (HIT — cached)")

    # Deploy new version — different URL = different cache key
    r_v2, ms_v2, st_v2 = get("/content/bundle.js?v=2")
    print(f"  ?v=2 Request 1: {st_v2:<8}  {ms_v2:6.1f}ms  (MISS — new version, fresh from origin)")

    r_v2, ms_v2, st_v2 = get("/content/bundle.js?v=2")
    print(f"  ?v=2 Request 2: {st_v2:<8}  {ms_v2:6.1f}ms  (HIT — v2 now cached)")

    print(f"""
  Cache invalidation strategies:
  ┌──────────────────────────────────────────────────────────────┐
  │ URL versioning  → Change URL on deploy (/static/app.v2.js)   │
  │                   Instant, no purge needed. RECOMMENDED.      │
  │ TTL expiry      → Wait for max-age to expire. Eventual.       │
  │ CDN purge API   → Explicit invalidation (Cloudflare, Fastly)  │
  │                   Requires CDN API, propagation takes 1-30s   │
  │ Cache-Control   → Vary header (per Accept-Language etc.)      │
  │ Surrogate keys  → Tag responses, purge by tag (Varnish/Fastly)│
  └──────────────────────────────────────────────────────────────┘

  Phil Karlton: "There are only two hard things in CS: cache invalidation
  and naming things." URL versioning sidesteps the invalidation problem.
""")

    # ── Phase 5: Summary ──────────────────────────────────────────
    section("Phase 5: Performance Summary")

    # Collect 5 hits and 1 miss for stats
    times_hit = []
    for _ in range(5):
        _, ms, _ = get("/content/image.jpg")
        times_hit.append(ms)

    # Force a miss with a fresh URL
    _, ms_miss, _ = get(f"/content/unique_{int(time.time())}.jpg")

    avg_hit = sum(times_hit) / len(times_hit)
    print(f"""
  Performance results:
    Origin latency (MISS):  ~{ms_miss:.1f}ms  (200ms artificial + network)
    Cache latency  (HIT):   ~{avg_hit:.1f}ms  (Nginx in-memory cache)
    Speedup ratio:          ~{ms_miss/avg_hit:.0f}x

  Real CDN numbers:
    Origin (US-East):       ~150-300ms from Europe
    CDN PoP (Europe):       ~5-20ms from European clients
    Speedup:                10-60x for cacheable content

  Cache hit ratio targets:
    Static assets:  >95% hit rate (long TTL + URL versioning)
    API responses:  50-80% hit rate (short TTL, high reuse)
    Personalised:   0% (must not be cached at CDN layer)

  Next: ../11-observability/
""")


if __name__ == "__main__":
    main()
