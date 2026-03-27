#!/usr/bin/env python3
"""
CDN & Edge Lab — Nginx as a CDN proxy in front of a Python origin server.

What this demonstrates:
  1. Cache MISS → HIT  — first request fetches from origin; subsequent served from cache
  2. Cache-Control: no-store  — origin instructs CDN to never store the response
  3. TTL expiry  — short TTL (5s) causes re-fetch after expiry
  4. URL versioning  — ?v=2 creates a new cache key; old version stays cached separately
  5. stale-while-revalidate  — serve stale immediately, refresh origin in background
  6. Vary header fragmentation  — Accept-Language creates separate cache entries per language
  7. Private content guardrail  — Cache-Control: private prevents CDN from caching auth data
  8. Origin hit counters  — confirm how many requests actually reached the origin

Architecture:
  [Client] ──→ [Nginx (CDN PoP)] ──→ [Python Origin]
                  ↑ cache zone              ↑ 200ms latency
               /tmp/nginx_cache          (simulated)
"""

import os
import time
import requests

NGINX  = f"http://{os.environ.get('NGINX_HOST', 'localhost')}:80"
ORIGIN = f"http://{os.environ.get('ORIGIN_HOST', 'localhost')}:8000"


def section(title: str) -> None:
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print("=" * 70)


def get(path: str, headers: dict | None = None) -> tuple[requests.Response, float, str]:
    """GET through nginx; return (response, elapsed_ms, X-Cache-Status)."""
    start = time.time()
    r = requests.get(f"{NGINX}{path}", headers=headers or {})
    elapsed = (time.time() - start) * 1000
    status = r.headers.get("X-Cache-Status", "N/A")
    return r, elapsed, status


def origin_hits() -> dict:
    """Query the origin health endpoint for its hit counter totals."""
    try:
        r = requests.get(f"{ORIGIN}/health", timeout=2)
        return r.json().get("hit_counts", {})
    except Exception:
        return {}


def main() -> None:
    section("CDN & EDGE LAB")
    print("""
  Architecture:

    [Client] ──────→ [Nginx CDN PoP] ─── HIT ──→ return cached response (5ms)
                           │
                           └─── MISS ──→ [Python Origin Server]
                                              200ms latency (simulated)

  Nginx models a CDN Point of Presence (PoP).
  The origin controls caching behaviour via Cache-Control response headers.
  X-Cache-Status header reports: MISS / HIT / EXPIRED / BYPASS / STALE
  X-Origin-Hit header shows the origin's own request counter per endpoint.
""")

    # ── Phase 1: Cache MISS → HIT ─────────────────────────────────────────────
    section("Phase 1: Cache MISS → HIT  (origin pull model)")
    print("""  /content/image.jpg — Cache-Control: public, max-age=30
  First request: cache cold → Nginx fetches from origin (MISS, ~200ms)
  Subsequent requests: served from Nginx cache (HIT, <5ms)
""")

    r1, ms1, st1 = get("/content/image.jpg")
    print(f"  Request 1: {st1:<8}  {ms1:6.1f}ms  origin_hit={r1.headers.get('X-Origin-Hit', '?')}")

    r2, ms2, st2 = get("/content/image.jpg")
    print(f"  Request 2: {st2:<8}  {ms2:6.1f}ms  origin_hit={r2.headers.get('X-Origin-Hit', '?')}")

    r3, ms3, st3 = get("/content/image.jpg")
    print(f"  Request 3: {st3:<8}  {ms3:6.1f}ms  origin_hit={r3.headers.get('X-Origin-Hit', '?')}")

    speedup = ms1 / ms2 if ms2 > 0 else 999
    print(f"""
  Cache speedup: {speedup:.0f}x  (MISS={ms1:.0f}ms vs HIT={ms2:.1f}ms)
  X-Origin-Hit stays at '1' for requests 2 and 3 — origin was never contacted.

  Cache-Control flow:
    Origin sends: Cache-Control: public, max-age=30
         ↓
    Nginx stores response in cdn_cache zone for 30 seconds
         ↓
    Client sees X-Cache-Status: MISS (first) → HIT (subsequent)
""")

    # ── Phase 2: no-store — never cached ──────────────────────────────────────
    section("Phase 2: Cache-Control: no-store — Never Stored in Cache")
    print("""  /nocache/data.json — Cache-Control: no-cache, no-store
  All three requests show BYPASS: every request reaches the origin.
  X-Origin-Hit increments on every request, confirming no cache storage.
""")

    for i in range(1, 4):
        r, ms, status = get("/nocache/data.json")
        print(f"  Request {i}: {status:<8}  {ms:6.1f}ms  origin_hit={r.headers.get('X-Origin-Hit', '?')}")

    print("""
  Cache-Control directive semantics (a common interview trip-wire):
    no-store   → never store in any cache (CDN or browser).  Use this for secrets.
    no-cache   → may cache, but MUST revalidate with origin before serving.
                 Sends a conditional GET (If-None-Match / If-Modified-Since).
                 Origin can reply 304 Not Modified — bandwidth saved, but RTT not.
    private    → only the end-user's browser may cache; CDN/proxy must not store.
    public     → any cache (CDN, proxy, browser) may store the response.
    s-maxage=N → CDN-specific TTL override; browsers ignore s-maxage.
    max-age=N  → both browser and CDN TTL (unless s-maxage overrides for CDN).
""")

    # ── Phase 3: TTL expiry ────────────────────────────────────────────────────
    section("Phase 3: TTL Expiry — Cache Goes Stale")
    print("""  /short-ttl/report.pdf — Cache-Control: public, max-age=5
  TTL is 5 seconds.  After sleeping 6s the cache entry expires and
  the next request is a MISS (origin contacted again).
""")

    r, ms, status = get("/short-ttl/report.pdf")
    print(f"  Request 1: {status:<8}  {ms:6.1f}ms  (MISS — cold)")

    r, ms, status = get("/short-ttl/report.pdf")
    print(f"  Request 2: {status:<8}  {ms:6.1f}ms  (HIT — warm)")

    print("  Sleeping 6s for TTL to expire...")
    time.sleep(6)

    r, ms, status = get("/short-ttl/report.pdf")
    print(f"  Request 3: {status:<8}  {ms:6.1f}ms  (EXPIRED/MISS — re-fetches origin)")

    r, ms, status = get("/short-ttl/report.pdf")
    print(f"  Request 4: {status:<8}  {ms:6.1f}ms  (HIT — re-cached)")

    print("""
  TTL selection guide (for the interview whiteboard):
    Static assets (JS/CSS/images): max-age=31536000 (1 year) + URL versioning
    API responses (read-heavy):     max-age=30 to 300s depending on freshness SLA
    HTML pages:                     max-age=0, ETag for conditional requests
    Authenticated/user data:        Cache-Control: private, no-store
""")

    # ── Phase 4: URL versioning ────────────────────────────────────────────────
    section("Phase 4: Cache Invalidation via URL Versioning")
    print("""  Best practice: avoid CDN purge APIs entirely by versioning the URL.
  ?v=1 and ?v=2 are different cache keys — deploying v2 never touches v1's cache.
  Old version expires naturally via TTL; new version always starts with a MISS.
""")

    r_v1, ms_v1, st_v1 = get("/content/bundle.js?v=1")
    print(f"  bundle.js?v=1  Request 1: {st_v1:<8}  {ms_v1:6.1f}ms  (MISS — cold)")

    r_v1, ms_v1, st_v1 = get("/content/bundle.js?v=1")
    print(f"  bundle.js?v=1  Request 2: {st_v1:<8}  {ms_v1:6.1f}ms  (HIT  — warm)")

    r_v2, ms_v2, st_v2 = get("/content/bundle.js?v=2")
    print(f"  bundle.js?v=2  Request 1: {st_v2:<8}  {ms_v2:6.1f}ms  (MISS — new key)")

    r_v2, ms_v2, st_v2 = get("/content/bundle.js?v=2")
    print(f"  bundle.js?v=2  Request 2: {st_v2:<8}  {ms_v2:6.1f}ms  (HIT  — warm)")

    print("""
  Cache invalidation strategies compared:
  ┌─────────────────────┬─────────────┬──────────────┬─────────────────────────┐
  │ Strategy            │ Freshness   │ Origin load  │ Operational cost        │
  ├─────────────────────┼─────────────┼──────────────┼─────────────────────────┤
  │ URL versioning      │ Instant     │ Very low     │ Build pipeline change   │
  │ TTL expiry          │ Eventual    │ Moderate     │ None                    │
  │ CDN purge API       │ ~1-30s lag  │ Low post-    │ API call per URL per PoP│
  │                     │             │ purge        │ Eventual consistency     │
  │ Surrogate-Key tags  │ Instant     │ Low post-    │ Tag all responses        │
  │ (Fastly/Varnish)    │             │ purge        │ Requires CDN support     │
  │ stale-while-reval.  │ Near-real   │ Low          │ Accept bounded staleness│
  └─────────────────────┴─────────────┴──────────────┴─────────────────────────┘

  Phil Karlton: "There are only two hard things in CS: cache invalidation and
  naming things." URL versioning sidesteps the problem entirely for static assets.
""")

    # ── Phase 5: stale-while-revalidate ───────────────────────────────────────
    section("Phase 5: stale-while-revalidate — Serve Stale, Refresh in Background")
    print("""  /swr/data.json — Cache-Control: public, s-maxage=5, stale-while-revalidate=10
  Nginx: proxy_cache_use_stale updating + proxy_cache_background_update on

  Timeline:
    0-5s:   TTL fresh   → HIT  (origin not contacted)
    5-15s:  Stale window → STALE served immediately + background fetch
    >15s:   Must revalidate → MISS (blocking wait for origin)

  Without SWR: every TTL expiry causes the NEXT user to block for ~200ms.
  With SWR:    that user gets the stale response in <5ms; origin updates async.
""")

    r, ms, status = get("/swr/data.json")
    print(f"  Request 1: {status:<8}  {ms:6.1f}ms  (MISS — cache cold)")

    r, ms, status = get("/swr/data.json")
    print(f"  Request 2: {status:<8}  {ms:6.1f}ms  (HIT  — within fresh window)")

    print("  Sleeping 6s to move into the stale-while-revalidate window...")
    time.sleep(6)

    r, ms, status = get("/swr/data.json")
    # Status will be STALE or HIT depending on Nginx version/config
    print(f"  Request 3: {status:<8}  {ms:6.1f}ms  (STALE or HIT — stale served, bg refresh)")

    time.sleep(0.5)  # allow background update to complete
    r, ms, status = get("/swr/data.json")
    print(f"  Request 4: {status:<8}  {ms:6.1f}ms  (HIT  — fresh after bg update)")

    print("""
  Key insight: with stale-while-revalidate, p99 latency stays flat even at TTL
  expiry boundaries.  Without it, the first post-expiry request incurs full
  origin RTT — a predictable but spiky latency pattern under load.
""")

    # ── Phase 6: Vary header cache fragmentation ───────────────────────────────
    section("Phase 6: Vary Header — Cache Fragmentation per Accept-Language")
    print("""  /vary/page.html — Origin sets: Vary: Accept-Language
  The CDN creates a SEPARATE cache entry for each distinct Accept-Language value.
  Same URL → multiple cache keys → lower effective hit rate.
""")

    for lang in ["en-US", "fr-FR", "de-DE", "en-US"]:
        r, ms, status = get("/vary/page.html", headers={"Accept-Language": lang})
        origin_hit = r.headers.get("X-Origin-Hit", "?")
        print(f"  Accept-Language: {lang:<8}  {status:<8}  {ms:6.1f}ms  origin_hit={origin_hit}")

    print("""
  The en-US request on the 4th call is a HIT because that entry was already cached.
  fr-FR and de-DE were each MISSes — separate cache slots for each language.

  Vary gotchas (important for interviews):
    Vary: *              → effectively disables caching (every request unique)
    Vary: Cookie         → per-user cache entries (cache explosion)
    Vary: Authorization  → should never be set; use Cache-Control: private instead
    Vary: Accept-Encoding → safe; only 2-3 variants (gzip, br, identity)
    Vary: Accept-Language → manageable if language count is bounded (~10-20 langs)

  Cloudflare strips Vary: Cookie from cached responses to prevent accidental
  personal data leakage across users — a hard safety override.
""")

    # ── Phase 7: Private content must not be CDN-cached ───────────────────────
    section("Phase 7: Private Content — CDN Must Not Cache Authenticated Responses")
    print("""  /private/profile.json — Cache-Control: private, no-store
  Nginx /private/ location forces proxy_no_cache + proxy_cache_bypass as
  defence-in-depth, even if the origin forgets to set the header.

  This models the critical mistake: forgetting Cache-Control: private on an
  auth-gated response causes the CDN to cache user A's data and serve it to
  user B on the next request for the same URL.
""")

    for user_id in ["user-alice", "user-bob", "user-alice"]:
        r, ms, status = get("/private/profile.json", headers={"X-User-Id": user_id})
        origin_hit = r.headers.get("X-Origin-Hit", "?")
        body_preview = r.text[:60]
        print(f"  user={user_id:<12}  {status:<8}  {ms:6.1f}ms  hit={origin_hit}  body='{body_preview}'")

    print("""
  All three requests are BYPASS — origin_hit increments every time.
  Even the second alice request does NOT serve bob's cached response.

  Defence-in-depth for auth responses:
    1. Origin sets Cache-Control: private, no-store
    2. CDN configured to never cache paths matching /api/user/* or /private/*
    3. CDN layer strips Set-Cookie from cached responses
    4. Surrogate-Control header lets you send different directives to CDN vs browser
""")

    # ── Phase 8: Performance Summary ──────────────────────────────────────────
    section("Phase 8: Performance Summary")

    # Collect several HIT timings for the well-warmed image.jpg cache entry
    times_hit = []
    for _ in range(5):
        _, ms, _ = get("/content/image.jpg")
        times_hit.append(ms)

    # Force a MISS with a unique URL never seen before
    _, ms_miss, _ = get(f"/content/unique_{int(time.time())}.jpg")

    avg_hit = sum(times_hit) / len(times_hit)
    min_hit = min(times_hit)
    max_hit = max(times_hit)

    hits = origin_hits()

    print(f"""
  Local lab measurements:
    Origin fetch (MISS):   ~{ms_miss:.0f}ms  (200ms artificial + Docker overhead)
    Cache serve (HIT):     ~{avg_hit:.1f}ms  avg  (min={min_hit:.1f}ms, max={max_hit:.1f}ms)
    Speedup ratio:         ~{ms_miss/avg_hit:.0f}x

  Origin hit counters (total times origin was actually contacted):
    content endpoint:      {hits.get('content', '?')} hits
    nocache endpoint:      {hits.get('nocache', '?')} hits
    short-ttl endpoint:    {hits.get('short-ttl', '?')} hits
    swr endpoint:          {hits.get('swr', '?')} hits
    vary endpoint:         {hits.get('vary', '?')} hits
    private endpoint:      {hits.get('private', '?')} hits

  Real CDN numbers (for the interview):
    Origin (US-East):      ~150-300ms round-trip from European client
    CDN PoP (Europe):      ~5-20ms from European client
    Speedup:               10-60x for cacheable content

  Cache hit ratio targets:
    Static assets:         >95%  (long TTL + URL versioning)
    Public API responses:  50-80% (short TTL, high reuse)
    Personalised content:  0%    (must not be CDN-cached)

  Next: ../12-observability/
""")


if __name__ == "__main__":
    main()
