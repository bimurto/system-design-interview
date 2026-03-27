#!/usr/bin/env python3
"""
Web Crawler Lab — experiment.py

What this demonstrates:
  1. BFS crawl: seed frontier → fetch → extract links → enqueue
  2. Bloom filter deduplication: URLs seen twice are skipped (not re-fetched)
  3. Politeness: per-domain rate limiting (max 1 req/s per domain)
  4. robots.txt respect: /private/* pages are never fetched
  5. Crawler trap detection: ?page=N parameter depth limit
  6. URL normalization: collapse session IDs, tracking params, fragments
  7. SimHash near-duplicate content detection (Hamming distance <= 3)
  8. Scale math: capacity numbers for a 5B-page Google-scale crawler
  9. DB summary: pages stored in Postgres by domain and depth

Run:
  docker compose up -d mock-server redis db
  # Wait ~20s for services to be healthy
  docker compose run --rm crawler
  # Or locally:
  pip install redis psycopg2-binary mmh3
  REDIS_URL=redis://localhost:6379 MOCK_SERVER_URL=http://localhost:5100 python experiment.py
"""

import hashlib
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque

import mmh3
import psycopg2
import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://app:secret@localhost:5432/crawler")
MOCK_SERVER_URL = os.environ.get("MOCK_SERVER_URL", "http://localhost:5100")

MAX_PAGES = 30          # crawl budget for this lab
MAX_DEPTH = 5           # maximum URL depth
POLITENESS_DELAY = 1.0  # seconds between requests to same domain
MAX_TRAP_PARAM = 3      # max value of ?page= parameter before treating as trap


# ── Helpers ──────────────────────────────────────────────────────────────────

def section(title: str):
    print(f"\n{'=' * 62}")
    print(f"  {title}")
    print("=" * 62)


def wait_for_service(url: str, max_wait: int = 60):
    print(f"  Waiting for {url} ...")
    for i in range(max_wait):
        try:
            urllib.request.urlopen(f"{url}/health", timeout=3)
            print(f"  Ready after {i + 1}s")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError(f"Service not ready within {max_wait}s")


def fetch(url: str, timeout: int = 5) -> tuple[int, str]:
    """Return (status_code, body). Returns (0, '') on network error."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def extract_links(base_url: str, html: str) -> list[str]:
    """Extract all href links from HTML, resolve to absolute URLs."""
    links = []
    for href in re.findall(r'href=["\']([^"\']+)["\']', html):
        try:
            absolute = urllib.parse.urljoin(base_url, href)
            parsed = urllib.parse.urlparse(absolute)
            # Keep only http/https links on the same host
            if parsed.scheme in ("http", "https"):
                links.append(absolute)
        except Exception:
            pass
    return links


def get_domain(url: str) -> str:
    return urllib.parse.urlparse(url).netloc


def is_trap_url(url: str) -> bool:
    """Detect URLs with incrementing parameters like ?page=N beyond threshold."""
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    for key in ("page", "p", "offset", "start"):
        if key in params:
            try:
                val = int(params[key][0])
                if val > MAX_TRAP_PARAM:
                    return True
            except ValueError:
                pass
    return False


SESSION_ID_PARAMS = frozenset({
    "jsessionid", "phpsessid", "sid", "sessionid", "aspsessionid",
    "cfid", "cftoken", "utm_source", "utm_medium", "utm_campaign",
})


def normalize_url(url: str) -> str:
    """Normalize URL for deduplication.

    Steps (mirrors production crawlers like Googlebot/CommonCrawl):
      1. Lowercase scheme and host
      2. Strip fragment (#section) — never sent to server
      3. Remove tracking/session ID query parameters
      4. Sort remaining query parameters alphabetically
    """
    parsed = urllib.parse.urlparse(url)
    # Filter out session/tracking params, then sort the rest
    filtered_params = [
        (k, v) for k, v in urllib.parse.parse_qsl(parsed.query)
        if k.lower() not in SESSION_ID_PARAMS
    ]
    query = urllib.parse.urlencode(sorted(filtered_params))
    normalized = urllib.parse.urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path,
        parsed.params,
        query,
        "",  # strip fragment — never part of server-side identity
    ))
    return normalized


# ── Bloom Filter ─────────────────────────────────────────────────────────────

class BloomFilter:
    """
    Simple in-process Bloom filter for URL deduplication.

    Uses k=3 hash functions over a bit array of size m.
    False positive rate ≈ (1 - e^(-kn/m))^k
    For m=1,000,000 bits and n=100,000 URLs: FPR ≈ 0.8%
    """

    def __init__(self, size: int = 1_000_000, num_hashes: int = 3):
        self.size = size
        self.num_hashes = num_hashes
        self.bits = bytearray(size // 8 + 1)
        self.count = 0
        self.fp_simulated = 0  # track simulated false positives for demo

    def _positions(self, item: str) -> list[int]:
        positions = []
        for seed in range(self.num_hashes):
            h = mmh3.hash(item, seed, signed=False)
            positions.append(h % self.size)
        return positions

    def _get_bit(self, pos: int) -> bool:
        byte_idx, bit_idx = divmod(pos, 8)
        return bool(self.bits[byte_idx] & (1 << bit_idx))

    def _set_bit(self, pos: int):
        byte_idx, bit_idx = divmod(pos, 8)
        self.bits[byte_idx] |= (1 << bit_idx)

    def add(self, item: str):
        for pos in self._positions(item):
            self._set_bit(pos)
        self.count += 1

    def __contains__(self, item: str) -> bool:
        return all(self._get_bit(pos) for pos in self._positions(item))

    def memory_bytes(self) -> int:
        return len(self.bits)


# ── SimHash (Near-Duplicate Content Detection) ────────────────────────────────

class SimHash:
    """
    64-bit SimHash for near-duplicate page detection.

    Algorithm (Charikar 2002, used by Google for web dedup):
      1. Tokenise page into shingles (word n-grams)
      2. Hash each shingle to a 64-bit integer
      3. For each bit position, sum +1 if that bit is 1, -1 if 0
      4. Final hash: bit i = 1 if sum[i] > 0, else 0

    Two pages are near-duplicates if Hamming distance <= 3.
    """

    BITS = 64

    @staticmethod
    def _shingles(text: str, k: int = 3) -> list[str]:
        words = re.findall(r'\w+', text.lower())
        return [" ".join(words[i:i + k]) for i in range(len(words) - k + 1)]

    @classmethod
    def compute(cls, text: str) -> int:
        v = [0] * cls.BITS
        for shingle in cls._shingles(text) or [text]:
            h = mmh3.hash64(shingle, signed=False)[0]
            for i in range(cls.BITS):
                v[i] += 1 if (h >> i) & 1 else -1
        fingerprint = 0
        for i in range(cls.BITS):
            if v[i] > 0:
                fingerprint |= (1 << i)
        return fingerprint

    @staticmethod
    def hamming(a: int, b: int) -> int:
        x = a ^ b
        count = 0
        while x:
            count += x & 1
            x >>= 1
        return count


# ── robots.txt Parser ─────────────────────────────────────────────────────────

class RobotsCache:
    """Fetch and cache robots.txt per domain. Honour Disallow rules."""

    def __init__(self):
        self._rules: dict[str, list[str]] = {}  # domain → disallowed prefixes

    def _fetch_rules(self, domain: str, scheme: str) -> list[str]:
        url = f"{scheme}://{domain}/robots.txt"
        try:
            status, body = fetch(url)
            if status != 200:
                return []
            disallowed = []
            in_block = False
            for line in body.splitlines():
                line = line.strip()
                if line.lower().startswith("user-agent:"):
                    agent = line.split(":", 1)[1].strip()
                    in_block = agent in ("*", "Googlebot", "MyCrawler")
                elif in_block and line.lower().startswith("disallow:"):
                    path = line.split(":", 1)[1].strip()
                    if path:
                        disallowed.append(path)
            return disallowed
        except Exception:
            return []

    def is_allowed(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc
        if domain not in self._rules:
            self._rules[domain] = self._fetch_rules(domain, parsed.scheme)
        path = parsed.path
        for prefix in self._rules[domain]:
            if path.startswith(prefix):
                return False
        return True


# ── Politeness tracker ────────────────────────────────────────────────────────

class PolitenessTracker:
    """Enforce minimum delay between requests to the same domain."""

    def __init__(self, delay: float = POLITENESS_DELAY):
        self.delay = delay
        self._last_access: dict[str, float] = {}
        self.waits: list[float] = []

    def wait_if_needed(self, domain: str):
        now = time.time()
        last = self._last_access.get(domain, 0)
        gap = now - last
        if gap < self.delay:
            wait_time = self.delay - gap
            self.waits.append(wait_time)
            time.sleep(wait_time)
        self._last_access[domain] = time.time()


# ── Database ──────────────────────────────────────────────────────────────────

def init_db(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crawled_pages (
                id SERIAL PRIMARY KEY,
                url TEXT UNIQUE NOT NULL,
                status_code INT,
                content_length INT,
                crawled_at TIMESTAMPTZ DEFAULT NOW(),
                depth INT,
                domain TEXT
            )
        """)
    conn.commit()


def save_page(conn, url: str, status: int, body: str, depth: int):
    domain = get_domain(url)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO crawled_pages (url, status_code, content_length, depth, domain)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (url) DO NOTHING
        """, (url, status, len(body), depth, domain))
    conn.commit()


# ── Main Crawler ──────────────────────────────────────────────────────────────

def run_crawler(seed_urls: list[str], bloom: BloomFilter, robots: RobotsCache,
                politeness: PolitenessTracker, conn) -> dict:
    """BFS crawler. Returns stats dict."""
    frontier = deque()
    for url in seed_urls:
        norm = normalize_url(url)
        frontier.append((norm, 0))
        bloom.add(norm)

    stats = {
        "fetched": 0,
        "skipped_bloom": 0,
        "skipped_robots": 0,
        "skipped_trap": 0,
        "errors": 0,
        "links_found": 0,
        "domains": set(),
    }

    while frontier and stats["fetched"] < MAX_PAGES:
        url, depth = frontier.popleft()

        # Depth limit
        if depth > MAX_DEPTH:
            continue

        # Trap detection
        if is_trap_url(url):
            stats["skipped_trap"] += 1
            continue

        # robots.txt check
        if not robots.is_allowed(url):
            stats["skipped_robots"] += 1
            continue

        # Politeness
        domain = get_domain(url)
        politeness.wait_if_needed(domain)
        stats["domains"].add(domain)

        # Fetch
        status, body = fetch(url)
        if status == 0:
            stats["errors"] += 1
            continue

        stats["fetched"] += 1
        save_page(conn, url, status, body, depth)

        # Extract + enqueue links
        links = extract_links(url, body)
        stats["links_found"] += len(links)
        for link in links:
            norm = normalize_url(link)
            if norm not in bloom:
                bloom.add(norm)
                frontier.append((norm, depth + 1))
            else:
                stats["skipped_bloom"] += 1

    return stats


# ── Phases ────────────────────────────────────────────────────────────────────

def phase1_seed_and_crawl(conn):
    section("Phase 1: BFS Crawl with Bloom Filter Deduplication")

    seed_urls = [
        f"{MOCK_SERVER_URL}/",
        f"{MOCK_SERVER_URL}/page/0",
        f"{MOCK_SERVER_URL}/page/10",
        f"{MOCK_SERVER_URL}/page/20",
        f"{MOCK_SERVER_URL}/page/30",
    ]

    bloom = BloomFilter(size=1_000_000, num_hashes=3)
    robots = RobotsCache()
    politeness = PolitenessTracker(delay=0.05)  # 50ms for lab speed

    print(f"\n  Seeding frontier with {len(seed_urls)} URLs:")
    for url in seed_urls:
        print(f"    {url}")

    print(f"\n  Starting BFS crawl (budget: {MAX_PAGES} pages, max depth: {MAX_DEPTH}) ...")
    t0 = time.time()
    stats = run_crawler(seed_urls, bloom, robots, politeness, conn)
    elapsed = time.time() - t0

    print(f"\n  Crawl complete in {elapsed:.1f}s")
    print(f"\n  {'Metric':<30} {'Value':>10}")
    print(f"  {'-'*30}  {'-'*10}")
    print(f"  {'Pages fetched':<30} {stats['fetched']:>10}")
    print(f"  {'Links found':<30} {stats['links_found']:>10}")
    print(f"  {'Bloom filter skips (dupes)':<30} {stats['skipped_bloom']:>10}")
    print(f"  {'robots.txt skips':<30} {stats['skipped_robots']:>10}")
    print(f"  {'Trap URL skips':<30} {stats['skipped_trap']:>10}")
    print(f"  {'Unique domains seen':<30} {len(stats['domains']):>10}")
    print(f"  {'Bloom filter size (bits)':<30} {bloom.size:>10,}")
    print(f"  {'Bloom filter memory':<30} {bloom.memory_bytes():>8} B")
    print(f"  {'URLs in bloom filter':<30} {bloom.count:>10}")

    return bloom, stats


def phase2_bloom_dedup_demo():
    section("Phase 2: Bloom Filter Deduplication")

    bloom = BloomFilter(size=100_000, num_hashes=3)

    print("""
  A Bloom filter is a probabilistic set: it can answer "definitely not seen"
  or "probably seen" — with a tunable false-positive rate.

  At 5B URLs, a plain hash set would need ~320GB RAM (5B × 64B/URL).
  A Bloom filter needs only ~5.7GB for 5B items at 1% FPR.

  Formula: m = -n*ln(p) / (ln2)^2
    n=5,000,000,000  p=0.01  → m ≈ 47.9B bits ≈ 5.7 GB
    n=5,000,000,000  p=0.10  → m ≈ 23.9B bits ≈ 2.9 GB
""")

    test_urls = [
        f"{MOCK_SERVER_URL}/page/1",
        f"{MOCK_SERVER_URL}/page/2",
        f"{MOCK_SERVER_URL}/page/3",
    ]

    print("  Adding 3 URLs to Bloom filter:")
    for url in test_urls:
        bloom.add(url)
        print(f"    Added: {url}")

    print("\n  Checking membership:")
    check_urls = test_urls + [
        f"{MOCK_SERVER_URL}/page/99",
        f"{MOCK_SERVER_URL}/page/100",
    ]
    for url in check_urls:
        in_filter = url in bloom
        truth = url in test_urls
        label = "TRUE POSITIVE" if in_filter and truth else \
                "TRUE NEGATIVE" if not in_filter and not truth else \
                "FALSE POSITIVE"
        print(f"    {'IN' if in_filter else 'NOT IN'} filter: {url}  [{label}]")

    # Demonstrate same URL encountered twice → skipped
    print("\n  Simulating crawler seeing the same URL twice:")
    url = f"{MOCK_SERVER_URL}/page/5"
    bloom.add(url)
    print(f"    First encounter: {url} → not in filter → CRAWL → add to filter")
    if url in bloom:
        print(f"    Second encounter: {url} → IN filter → SKIP (deduplicated)")


def phase3_politeness():
    section("Phase 3: Per-Domain Rate Limiting (Politeness)")

    print("""
  Politeness rules prevent hammering a single server.
  Each domain gets its own "last access" timestamp.
  If we accessed domain X less than 1s ago, we wait.

  Real systems also read Crawl-delay from robots.txt.
""")

    tracker = PolitenessTracker(delay=0.5)
    urls = [
        f"{MOCK_SERVER_URL}/page/1",
        f"{MOCK_SERVER_URL}/page/2",
        f"{MOCK_SERVER_URL}/page/3",
        "http://other-domain.example/page/1",
        "http://other-domain.example/page/2",
        f"{MOCK_SERVER_URL}/page/4",
    ]

    print(f"  {'URL':<45} {'Wait (ms)':>10} {'Domain'}")
    print(f"  {'-'*45}  {'-'*10}  {'-'*25}")
    for url in urls:
        domain = get_domain(url)
        t0 = time.time()
        tracker.wait_if_needed(domain)
        waited_ms = (time.time() - t0) * 1000
        print(f"  {url:<45}  {waited_ms:>8.0f}ms  {domain}")

    print(f"""
  Observations:
  - Same domain (mock-server): enforced {tracker.delay*1000:.0f}ms gap between requests
  - Different domain (other-domain): no wait (separate token bucket)
  - Total waits applied: {len(tracker.waits)}
  - Total time waited: {sum(tracker.waits)*1000:.0f}ms
""")


def phase4_robots_txt():
    section("Phase 4: robots.txt Respect")

    robots = RobotsCache()

    print(f"\n  Fetching robots.txt from {MOCK_SERVER_URL}/robots.txt ...")
    _, body = fetch(f"{MOCK_SERVER_URL}/robots.txt")
    print(f"\n  robots.txt content:")
    for line in body.strip().splitlines():
        print(f"    {line}")

    test_urls = [
        (f"{MOCK_SERVER_URL}/page/1",      "normal page"),
        (f"{MOCK_SERVER_URL}/page/5",      "normal page"),
        (f"{MOCK_SERVER_URL}/private/secret", "disallowed"),
        (f"{MOCK_SERVER_URL}/private/admin",  "disallowed"),
        (f"{MOCK_SERVER_URL}/",            "root page"),
    ]

    print(f"\n  {'URL':<55} {'Allowed?':>10}  Note")
    print(f"  {'-'*55}  {'-'*10}  {'-'*15}")
    for url, note in test_urls:
        allowed = robots.is_allowed(url)
        symbol = "YES" if allowed else "NO (blocked)"
        print(f"  {url:<55}  {symbol:>10}  {note}")

    print("""
  Private pages exist on the server but robots.txt disallows them.
  A well-behaved crawler respects this and never fetches /private/*.
""")


def phase5_trap_detection():
    section("Phase 5: Crawler Trap Detection")

    print(f"""
  Crawler traps: pages that generate infinite link graphs.
  Common patterns:
    - Calendar pages: /calendar?year=2024&month=1 → month=2 → ... → month=999
    - Session IDs:    /page?session=abc123&page=1 → page=2 → ...
    - Infinite pagination: ?page=1 → ?page=2 → ?page=999999

  Detection strategies:
    1. Depth limit (max depth = {MAX_DEPTH})
    2. URL normalization (remove session IDs)
    3. Parameter value threshold (if ?page= > {MAX_TRAP_PARAM}, skip)
""")

    trap_urls = [
        f"{MOCK_SERVER_URL}/trap?page=0",
        f"{MOCK_SERVER_URL}/trap?page=1",
        f"{MOCK_SERVER_URL}/trap?page=3",
        f"{MOCK_SERVER_URL}/trap?page=4",   # exceeds MAX_TRAP_PARAM
        f"{MOCK_SERVER_URL}/trap?page=100",
        f"{MOCK_SERVER_URL}/page/1",         # normal
        f"{MOCK_SERVER_URL}/page/2?ref=homepage",  # normal with extra param
    ]

    print(f"  {'URL':<55} {'Trap?':>8}")
    print(f"  {'-'*55}  {'-'*8}")
    for url in trap_urls:
        trap = is_trap_url(url)
        print(f"  {url:<55}  {'YES' if trap else 'no':>8}")

    # Show BFS would have gone deep into trap without detection
    print(f"""
  Without trap detection, a BFS crawler fetching {MOCK_SERVER_URL}/trap
  would enqueue /trap?page=1 → /trap?page=2 → ... indefinitely.

  With depth limit ({MAX_DEPTH}) AND param threshold ({MAX_TRAP_PARAM}):
  crawler stops adding trap URLs after page={MAX_TRAP_PARAM}.
""")


def phase6_url_normalization():
    section("Phase 6: URL Normalization — Collapsing Duplicate URL Variants")

    print("""
  URL normalization collapses URL variants that point to the same content.
  Without normalization, the Bloom filter cannot detect these as duplicates.

  Rules applied (in order):
    1. Lowercase scheme + host  (HTTP://Example.COM → http://example.com)
    2. Strip fragment           (/page#section → /page)
    3. Remove session/tracking params (jsessionid, phpsessid, utm_*)
    4. Sort query params alphabetically (?b=2&a=1 → ?a=1&b=2)
""")

    test_cases = [
        (
            "Scheme + host case",
            "HTTP://Example.COM/Path",
            "http://example.com/Path",
        ),
        (
            "Fragment removal",
            "http://example.com/page#comments",
            "http://example.com/page",
        ),
        (
            "Session ID stripping",
            "http://example.com/page?jsessionid=abc123&id=5",
            "http://example.com/page?id=5",
        ),
        (
            "UTM tracking removal",
            "http://example.com/article?utm_source=twitter&utm_medium=social&id=9",
            "http://example.com/article?id=9",
        ),
        (
            "Query param sorting",
            "http://example.com/search?z=last&a=first&m=mid",
            "http://example.com/search?a=first&m=mid&z=last",
        ),
        (
            "Combined: session + fragment + sort",
            "HTTP://Example.COM/page?b=2&sid=XYZ&a=1#top",
            "http://example.com/page?a=1&b=2",
        ),
    ]

    print(f"  {'Case':<35} {'Match?':>7}")
    print(f"  {'-'*35}  {'-'*7}")
    all_pass = True
    for label, raw, expected in test_cases:
        result = normalize_url(raw)
        match = result == expected
        if not match:
            all_pass = False
        status = "PASS" if match else "FAIL"
        print(f"  {label:<35}  {status:>7}")
        if not match:
            print(f"    Input:    {raw}")
            print(f"    Expected: {expected}")
            print(f"    Got:      {result}")

    print(f"""
  Normalization ensures these two crawler encounters map to ONE Bloom
  filter entry — avoiding redundant fetches of the same server resource.

  {'All normalization tests passed.' if all_pass else 'WARNING: some tests failed — check normalize_url().'}
""")


def phase7_simhash_content_dedup():
    section("Phase 7: SimHash — Near-Duplicate Content Detection")

    print("""
  URL deduplication (Bloom filter) catches exact URL matches.
  But the same article often lives at multiple URLs:
    - /article/123  and  /print/article/123  (printer-friendly mirror)
    - /2024/01/01/story  and  /story?id=1     (slug vs query-param)
    - Syndicated content on multiple domains

  SimHash (Charikar 2002): a locality-sensitive hash (LSH) where
  near-identical texts produce hashes with small Hamming distance.

  Algorithm:
    1. Tokenise text into 3-word shingles
    2. Hash each shingle to a 64-bit integer (MurmurHash)
    3. For each bit position, accumulate +1 (bit=1) or -1 (bit=0)
    4. Final fingerprint: bit i = 1 iff sum[i] > 0

  Two pages are near-duplicates if Hamming(fp_A, fp_B) <= 3.
  (3 bits differs in 64 ≈ 95% content similarity)
""")

    canonical = (
        "The quick brown fox jumps over the lazy dog near the river bank. "
        "It was a bright sunny afternoon and the fox had been running all day."
    )
    near_dup = (
        "The quick brown fox jumps over the lazy dog near the river. "
        "It was a bright sunny afternoon and the fox had been running all day long."
    )
    different = (
        "Distributed systems require careful consideration of consistency and "
        "availability trade-offs. The CAP theorem states you can only have two "
        "of consistency, availability, and partition tolerance."
    )
    empty = ""

    pages = [
        ("Canonical article",       canonical),
        ("Near-duplicate (2 words changed)", near_dup),
        ("Completely different page",        different),
        ("Empty page",                       empty),
    ]

    fps = {}
    print(f"  Computing SimHash fingerprints:")
    for label, text in pages:
        fp = SimHash.compute(text) if text else 0
        fps[label] = fp
        print(f"    {label:<40}  fp={fp:016x}")

    canonical_fp = fps["Canonical article"]
    print(f"\n  Hamming distances from canonical article (threshold = 3):")
    print(f"  {'Comparison':<40} {'Distance':>10}  {'Near-dup?':>10}")
    print(f"  {'-'*40}  {'-'*10}  {'-'*10}")
    for label, _ in pages[1:]:
        dist = SimHash.hamming(canonical_fp, fps[label])
        is_dup = dist <= 3
        print(f"  {label:<40}  {dist:>10}  {'YES' if is_dup else 'no':>10}")

    print("""
  Production usage (Google/CommonCrawl):
    - Compute SimHash of page body text after stripping HTML tags
    - Store fingerprint in Postgres alongside canonical_url
    - Before indexing: lookup pages with Hamming distance <= 3
    - Only index the canonical URL; redirect near-dups to it
    - Saves ~30% index space (web has massive duplicate content)
""")


def phase8_scale_math():
    section("Phase 8: Scale Math — 5B Pages in 2 Weeks")

    print("""
  System design numbers for a Google-scale crawler:

  Target: 5,000,000,000 pages, re-crawl every 14 days
""")

    total_pages = 5_000_000_000
    recrawl_days = 14
    pages_per_day = total_pages / recrawl_days
    pages_per_sec = pages_per_day / 86400
    avg_page_kb = 50  # average HTML page size
    bandwidth_mbps = pages_per_sec * avg_page_kb * 1024 / 1_000_000

    # Bloom filter sizing for 5B URLs
    n = total_pages
    p = 0.01
    m_bits = -n * math.log(p) / (math.log(2) ** 2)
    k_hashes = math.ceil((m_bits / n) * math.log(2))

    # Storage and operational overhead
    unique_domains = 200_000_000
    robots_cache_ttl_hours = 24
    robots_fetches_per_day = unique_domains / robots_cache_ttl_hours / 3600  # per second
    dns_cache_ttl_s = 300  # 5-minute DNS TTL
    dns_lookups_per_sec = pages_per_sec / (dns_cache_ttl_s * 10)  # ~10 pages/domain hit cache

    raw_html_storage_tb = (total_pages * avg_page_kb * 1024) / 1e12
    storage_cost_monthly = raw_html_storage_tb * 1000 * 0.023  # S3 at $0.023/GB

    # Write IOPS: each page fetch = 1 object store write + 1 Postgres row
    write_iops = pages_per_sec  # Postgres metadata inserts/sec

    print(f"  {'Metric':<52} {'Value':>20}")
    print(f"  {'-'*52}  {'-'*20}")
    print(f"  {'Pages/day':<52} {pages_per_day:>20,.0f}")
    print(f"  {'Pages/second (crawl throughput)':<52} {pages_per_sec:>20,.0f}")
    print(f"  {'Bandwidth @ 50KB/page':<52} {bandwidth_mbps:>18.0f} MB/s")
    print(f"  {'Bloom filter size (1% FPR, 5B URLs)':<52} {m_bits/8/1e9:>17.1f} GB")
    print(f"  {'Optimal hash functions (k)':<52} {k_hashes:>20}")
    print(f"  {'Bloom memory vs hash set (64B/URL)':<52} {'~6GB vs ~320GB':>20}")
    print(f"  {'Raw HTML storage (250TB total)':<52} {raw_html_storage_tb:>17.0f} TB")
    print(f"  {'S3 storage cost/month':<52} ${storage_cost_monthly:>18,.0f}")
    print(f"  {'Postgres write IOPS (metadata)':<52} {write_iops:>18,.0f}/s")
    print(f"  {'robots.txt fetches (200M domains, 24h TTL)':<52} {robots_fetches_per_day:>18.0f}/s")
    print(f"  {'Crawler workers (@ 10 pages/s each)':<52} {pages_per_sec/10:>20,.0f}")

    print(f"""
  Distributed crawler architecture:
  ┌─────────────────────────────────────────────────────────┐
  │  URL Frontier (Kafka, priority tiers)                    │
  │    Topic: priority-high  (news, trending)               │
  │    Topic: priority-med   (known sites)                  │
  │    Topic: priority-low   (new/unknown domains)          │
  └──────────────────┬──────────────────────────────────────┘
                     │ partition by domain hash
          ┌──────────▼──────────┐
          │  Crawler Workers     │  (N workers, each owns domains)
          │  - Fetch HTML        │
          │  - Extract links     │
          │  - Politeness delay  │
          └──────────┬───────────┘
                     │
          ┌──────────▼──────────┐
          │  Bloom Filter        │  (shared, Redis BitSet or Spark)
          │  URL Seen?          │
          └─────────────────────┘
""")


def phase9_db_summary(conn):
    section("Phase 9: Crawl Results in Postgres")

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM crawled_pages")
        total = cur.fetchone()[0]

        cur.execute("""
            SELECT domain, COUNT(*) as pages, AVG(content_length)::INT as avg_bytes
            FROM crawled_pages
            GROUP BY domain
            ORDER BY pages DESC
        """)
        rows = cur.fetchall()

        cur.execute("""
            SELECT depth, COUNT(*) as pages
            FROM crawled_pages
            GROUP BY depth
            ORDER BY depth
        """)
        depth_rows = cur.fetchall()

    print(f"\n  Total pages stored: {total}\n")

    print(f"  By domain:")
    print(f"  {'Domain':<35} {'Pages':>8}  {'Avg bytes':>10}")
    print(f"  {'-'*35}  {'-'*8}  {'-'*10}")
    for domain, pages, avg_bytes in rows:
        print(f"  {domain:<35}  {pages:>8}  {avg_bytes or 0:>10}")

    print(f"\n  By depth:")
    print(f"  {'Depth':>8}  {'Pages':>8}")
    print(f"  {'-'*8}  {'-'*8}")
    for depth, pages in depth_rows:
        bar = "#" * pages
        print(f"  {depth:>8}  {pages:>8}  {bar}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    section("WEB CRAWLER LAB")
    print(f"""
  Architecture:
    Frontier (Redis/Postgres) → Crawler Worker → Fetch HTML
         ↑                           ↓
    Add new links              Extract links
         ↑                           ↓
    Bloom Filter           Normalize + Deduplicate
         ↑                           ↓
    robots.txt check       Politeness delay per domain
""")

    wait_for_service(MOCK_SERVER_URL)

    conn = psycopg2.connect(DATABASE_URL)
    init_db(conn)

    bloom, stats = phase1_seed_and_crawl(conn)
    phase2_bloom_dedup_demo()
    phase3_politeness()
    phase4_robots_txt()
    phase5_trap_detection()
    phase6_url_normalization()
    phase7_simhash_content_dedup()
    phase8_scale_math()
    phase9_db_summary(conn)

    conn.close()

    section("Lab Complete")
    print("""
  Summary:
  - BFS crawl with Bloom filter deduplication prevents re-fetching seen URLs
  - Per-domain rate limiting prevents overloading servers (politeness)
  - robots.txt compliance is mandatory for ethical crawling
  - Crawler traps (infinite pagination) stopped by depth limit + param check
  - URL normalization collapses session IDs, tracking params, fragments
  - SimHash detects near-duplicate content at different URLs (saves ~30% index)
  - At 5B pages: Bloom filter saves ~314GB RAM vs a hash set

  Next: 08-search-engine/ — inverting the crawled content into a searchable index
""")


if __name__ == "__main__":
    main()
