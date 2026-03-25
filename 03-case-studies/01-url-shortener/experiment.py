#!/usr/bin/env python3
"""
URL Shortener Lab — experiment.py

What this demonstrates:
  1. Create 1,000 short URLs and measure throughput
  2. Time redirect with vs. without cache — show 100x+ speedup
  3. Base62 encoding: show how sequential IDs produce short codes
  4. Cache hit-rate warming (cold → warm)
  5. Hash vs. sequential vs. Snowflake-style ID generation
  6. Birthday paradox: collision probability with hash-based IDs

Run:
  docker compose up -d
  # wait ~15s for services
  python experiment.py
"""

import hashlib
import math
import random
import string
import time
import urllib.error
import urllib.parse
import urllib.request
import json

BASE_URL = "http://localhost:5001"
BASE62 = string.digits + string.ascii_letters


# ── Helpers ─────────────────────────────────────────────────────────────────

def section(title):
    print(f"\n{'=' * 62}")
    print(f"  {title}")
    print("=" * 62)


def wait_for_service(url, max_wait=60):
    print(f"  Waiting for service at {url} ...")
    for i in range(max_wait):
        try:
            urllib.request.urlopen(f"{url}/health", timeout=3)
            print(f"  Service ready after {i+1}s")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError(f"Service did not start within {max_wait}s")


def post_json(path, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def get_json(path):
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=10) as resp:
        return json.loads(resp.read())


def time_redirect(code, follow=False):
    """Time a single redirect request (no follow = measure server response)."""
    url = f"{BASE_URL}/{code}"
    req = urllib.request.Request(url, method="GET")
    start = time.perf_counter()
    try:
        if follow:
            urllib.request.urlopen(req, timeout=10)
        else:
            # Don't follow redirect — measure server-side latency only
            opener = urllib.request.build_opener(urllib.request.HTTPErrorProcessor())
            opener.open(req, timeout=10)
    except urllib.error.HTTPError:
        pass
    except Exception:
        pass
    return (time.perf_counter() - start) * 1000  # ms


# ── Base62 utilities (mirror of app.py) ─────────────────────────────────────

def base62_encode(num: int) -> str:
    if num == 0:
        return BASE62[0]
    chars = []
    while num:
        chars.append(BASE62[num % 62])
        num //= 62
    return "".join(reversed(chars))


def base62_decode(s: str) -> int:
    result = 0
    for ch in s:
        result = result * 62 + BASE62.index(ch)
    return result


# ── Phase 1: Bulk URL creation ───────────────────────────────────────────────

def phase1_bulk_create():
    section("Phase 1: Create 1,000 Short URLs")
    domains = ["https://example.com", "https://github.com", "https://news.ycombinator.com"]
    codes = []
    start = time.perf_counter()
    for i in range(1000):
        url = f"{random.choice(domains)}/path/{i}/{''.join(random.choices(string.ascii_lowercase, k=6))}"
        result = post_json("/shorten", {"url": url})
        codes.append(result["short_code"])
    elapsed = time.perf_counter() - start

    print(f"\n  Created 1,000 URLs in {elapsed:.2f}s  ({1000/elapsed:.0f} req/s)")
    print(f"\n  Sample codes (first 10):")
    print(f"  {'ID (approx)':>12}  {'Short Code':>12}  {'URL length':>10}")
    print(f"  {'-'*12}  {'-'*12}  {'-'*10}")
    for i, code in enumerate(codes[:10], start=1):
        decoded = base62_decode(code)
        print(f"  {decoded:>12}  {code:>12}  {len(code):>10} chars")

    print(f"\n  Base62 property: 6 chars cover 62^6 = {62**6:,} unique URLs")
    print(f"  At 1,000 writes/day, 6-char codes last {62**6 // 1000 // 365:,} years")
    return codes


# ── Phase 2: Cache warm-up and latency comparison ───────────────────────────

def phase2_cache_latency(codes):
    section("Phase 2: Redirect Latency — Cache Cold vs. Warm")

    sample = codes[:20]

    # Cold: first request hits Postgres
    cold_times = []
    for code in sample:
        ms = time_redirect(code)
        cold_times.append(ms)
    avg_cold = sum(cold_times) / len(cold_times)

    # Warm: second request hits Redis
    warm_times = []
    for code in sample:
        ms = time_redirect(code)
        warm_times.append(ms)
    avg_warm = sum(warm_times) / len(warm_times)

    speedup = avg_cold / avg_warm if avg_warm > 0 else float("inf")

    print(f"\n  Testing 20 URLs — each requested twice (cold then warm):\n")
    print(f"  {'Metric':<20}  {'Cold (DB)':>12}  {'Warm (Redis)':>14}")
    print(f"  {'-'*20}  {'-'*12}  {'-'*14}")
    print(f"  {'Avg latency':<20}  {avg_cold:>10.1f}ms  {avg_warm:>12.1f}ms")
    print(f"  {'Min latency':<20}  {min(cold_times):>10.1f}ms  {min(warm_times):>12.1f}ms")
    print(f"  {'Max latency':<20}  {max(cold_times):>10.1f}ms  {max(warm_times):>12.1f}ms")
    print(f"\n  Speedup: {speedup:.1f}x  (Redis vs Postgres round-trip)")

    stats = get_json("/stats")
    print(f"\n  Cache stats after warm-up:")
    print(f"    Hits:     {stats['cache_hits']}")
    print(f"    Misses:   {stats['cache_misses']}")
    print(f"    Hit rate: {stats['hit_rate_pct']}%")


# ── Phase 3: Base62 encoding demo ───────────────────────────────────────────

def phase3_base62_demo():
    section("Phase 3: Base62 Encoding — Sequential IDs → Short Codes")

    print(f"\n  Base62 alphabet: {BASE62}")
    print(f"\n  How sequential auto-increment IDs encode:\n")
    print(f"  {'ID':>12}  {'Base62 Code':>12}  {'Code Length':>12}  {'Max IDs at length':>18}")
    print(f"  {'-'*12}  {'-'*12}  {'-'*12}  {'-'*18}")

    ids = [1, 10, 100, 1_000, 10_000, 100_000, 1_000_000, 10_000_000, 100_000_000]
    for i in ids:
        code = base62_encode(i)
        max_at_len = 62 ** len(code)
        print(f"  {i:>12,}  {code:>12}  {len(code):>12}  {max_at_len:>18,}")

    print(f"""
  Key insight: base62 is compact and URL-safe.
  - 6 chars: up to {62**6:,} URLs  (enough for years at typical scale)
  - 7 chars: up to {62**7:,} URLs  (global scale, decades)
  - Sequential IDs are predictable (crawlable) — consider adding
    a random offset or using Snowflake IDs for privacy.
""")


# ── Phase 4: Cache hit rate warming ─────────────────────────────────────────

def phase4_cache_warming(codes):
    section("Phase 4: Cache Hit Rate — Warming Curve")

    # Reset stats by requesting all codes fresh (all already warm from Phase 2)
    # Use a fresh batch not previously fetched
    fresh_codes = codes[20:120]  # 100 codes not yet warmed

    print(f"\n  Simulating 500 requests to 100 URLs (Zipf distribution):")
    print(f"  First requests = cache misses, repeats = cache hits\n")

    # Zipf-like: code[0] is 10x more popular than code[99]
    weights = [1 / (i + 1) for i in range(len(fresh_codes))]
    total_w = sum(weights)
    weights = [w / total_w for w in weights]

    hits_over_time = []
    running_hits = 0
    running_total = 0
    seen = set()

    for i in range(500):
        code = random.choices(fresh_codes, weights=weights)[0]
        time_redirect(code)
        running_total += 1
        if code in seen:
            running_hits += 1
        else:
            seen.add(code)
        if (i + 1) % 50 == 0:
            rate = running_hits / running_total * 100
            hits_over_time.append((i + 1, rate))

    print(f"  {'Requests':>10}  {'Hit Rate':>10}  {'Bar'}")
    print(f"  {'-'*10}  {'-'*10}  {'-'*30}")
    for req_count, rate in hits_over_time:
        bar = "#" * int(rate / 2)
        print(f"  {req_count:>10}  {rate:>9.1f}%  {bar}")

    print(f"""
  Cache hit rate rises as popular URLs get cached.
  Real systems use TTL (24h) + LRU eviction on Redis max-memory.
  Hot URLs (viral links) benefit most — they're requested millions
  of times but cached after the very first miss.
""")


# ── Phase 5: ID generation strategies ───────────────────────────────────────

def phase5_id_generation():
    section("Phase 5: ID Generation Strategies — Trade-offs")

    print(f"""
  Three approaches to generating short codes:

  ┌─────────────────────┬──────────────────┬───────────────────┬──────────────────┐
  │ Strategy            │ Collision Risk   │ Predictable?      │ Distributed OK?  │
  ├─────────────────────┼──────────────────┼───────────────────┼──────────────────┤
  │ Hash (MD5 truncated)│ Yes (birthday)   │ No (good)         │ Yes              │
  │ Sequential auto-inc │ None             │ Yes (bad for UX)  │ No (single DB)   │
  │ Snowflake-style     │ None             │ No (time-ordered) │ Yes              │
  └─────────────────────┴──────────────────┴───────────────────┴──────────────────┘
""")

    # Demonstrate hash-based ID generation
    print("  Hash-based: take first 6 chars of MD5(url)")
    sample_urls = [
        "https://example.com/article/1",
        "https://example.com/article/2",
        "https://github.com/user/repo",
    ]
    for url in sample_urls:
        h = hashlib.md5(url.encode()).hexdigest()
        code = h[:7]  # 7 hex chars = 28 bits
        print(f"    {url[:40]:<40} → {code}")

    # Demonstrate Snowflake-style ID
    print(f"\n  Snowflake-style: timestamp_ms | machine_id | sequence")
    machine_id = 1
    seq = 0
    print(f"\n  {'Timestamp ms':>14}  {'Machine':>8}  {'Seq':>5}  {'Snowflake ID':>20}  {'Base62':>8}")
    print(f"  {'-'*14}  {'-'*8}  {'-'*5}  {'-'*20}  {'-'*8}")
    base_ts = int(time.time() * 1000) - 1_700_000_000_000  # epoch offset
    for i in range(5):
        ts = base_ts + i * 100
        snow_id = (ts << 12) | (machine_id << 6) | (seq + i)
        code = base62_encode(snow_id)
        print(f"  {ts:>14}  {machine_id:>8}  {seq+i:>5}  {snow_id:>20}  {code:>8}")

    print(f"""
  Snowflake advantages:
  - No coordination needed (machine_id assigned at startup)
  - Time-sortable (useful for analytics)
  - No collisions by construction
  - Used by: Twitter/X (tweet IDs), Instagram, Discord
""")


# ── Phase 6: Birthday paradox collision probability ─────────────────────────

def phase6_birthday_paradox():
    section("Phase 6: Birthday Paradox — Hash Collision Probability")

    print(f"""
  When using hash(url)[:N] as short code, what is the probability
  of a collision after K URLs?  (Birthday problem)

  P(collision) ≈ 1 - e^(-K²/2N)  where N = total possible codes

  For hex characters (16 choices per char):
""")

    print(f"  {'Code Chars':>10}  {'Total Codes':>18}  {'1% collision at':>18}  {'50% at':>14}")
    print(f"  {'-'*10}  {'-'*18}  {'-'*18}  {'-'*14}")

    for n_chars in [5, 6, 7, 8, 10]:
        total = 16 ** n_chars  # hex
        # P = 0.01: K ≈ sqrt(-2 * N * ln(0.99))
        k_1pct  = int(math.sqrt(-2 * total * math.log(0.99)))
        k_50pct = int(math.sqrt(-2 * total * math.log(0.5)))
        print(f"  {n_chars:>10}  {total:>18,}  {k_1pct:>18,}  {k_50pct:>14,}")

    print(f"""
  With 6 hex chars (~16M codes) and 100M URLs stored: very high collision risk.
  With 8 hex chars (~4B codes): 1% collision at ~9M URLs — acceptable short-term.

  This is why production systems prefer:
  1. Sequential IDs (no collisions, but predictable)
  2. Snowflake IDs (no collisions, distributed, time-sortable)
  3. Hash with collision detection + retry loop (expensive at scale)

  Base62 (vs hex) gives MORE codes per character:
  6 base62 chars = 62^6 = {62**6:,} vs 16^6 = {16**6:,} (hex)
  → base62 is 10x more space-efficient per character length
""")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    section("URL SHORTENER LAB")
    print("""
  Architecture:
    Client → Flask (base62 encode) → Postgres (persistent store)
                                  ↗
                             Redis (cache-aside, TTL 24h)

  Cache-aside pattern:
    Read:  check Redis → if miss, query Postgres → store in Redis
    Write: write to Postgres only (cache populated on first read)
""")

    wait_for_service(BASE_URL)

    codes = phase1_bulk_create()
    phase2_cache_latency(codes)
    phase3_base62_demo()
    phase4_cache_warming(codes)
    phase5_id_generation()
    phase6_birthday_paradox()

    section("Lab Complete")
    print("""
  Summary:
  • Cache-aside cuts redirect latency 10-100x (Postgres → Redis)
  • Base62 encoding: 6 chars covers 56 billion URLs
  • Sequential IDs: no collisions, but predictable (security risk)
  • Hash IDs: unpredictable, but birthday paradox causes collisions
  • Snowflake IDs: best of both — distributed, no collisions, sortable

  Next: 02-twitter-timeline/ — fan-out on write vs. pull at read time
""")


if __name__ == "__main__":
    main()
