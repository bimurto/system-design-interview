# CDN & Edge

**Prerequisites:** `../09-rate-limiting-algorithms/`
**Next:** `../12-observability/`

---

## Concept

A Content Delivery Network (CDN) is a geographically distributed system of proxy servers called Points of Presence (
PoPs) placed close to end users. When a user in Frankfurt requests a video file, their request routes to a Frankfurt PoP
rather than an origin server in Virginia. If the PoP has the file cached, it responds immediately from local storage —
typically in 5-20ms instead of 150-300ms. If not, the PoP fetches from the origin, caches the response, and serves
future requests locally. This pattern — cache at the edge, fetch from origin on miss — is called origin pull.

CDNs solve two distinct problems: latency and load. Latency is reduced by geographic proximity: the speed of light
limits how fast data can travel across an ocean. A 150ms round-trip to a US origin becomes 10ms to a local PoP. Load on
the origin is reduced because cache hits never reach it — a popular YouTube video might be cached at thousands of PoPs
worldwide, with the origin serving only the original copies. In 2024, Cloudflare reported that its CDN absorbs roughly
40% of all internet HTTP traffic globally, shielding origins from the vast majority of requests.

Cache behaviour is controlled by HTTP `Cache-Control` headers sent by the origin. `max-age=3600` tells the CDN to cache
for one hour. `s-maxage=3600` is CDN-specific: it overrides `max-age` for shared caches while the browser may use a
different duration. `no-cache` means the CDN must revalidate with the origin before serving (it can still cache
conditionally using ETags). `no-store` means never cache anywhere. `public` explicitly permits CDN caching. `private`
means only the browser may cache it — CDNs must not store user-specific responses like shopping cart data or auth
tokens.

Cache invalidation is one of the hardest problems in CDN architecture. The naive approach — call the CDN's purge API
when you deploy — requires an API call per URL per PoP and takes 1-30 seconds to propagate globally. The practical
solution is URL versioning: include the content hash or version number in the URL (`/static/app.abc123.js`). Because the
URL changes on every deploy, old cached content simply expires via TTL while new content is fetched fresh. Old URLs 404
gracefully once the files are removed from origin. This pattern makes CDN invalidation a non-problem for static assets.

Edge computing extends CDNs beyond caching to execute application logic at PoPs. Cloudflare Workers and Fastly
Compute@Edge run JavaScript or WebAssembly at CDN nodes worldwide. This enables: A/B testing at the edge (personalize
content before caching), authentication checks that block requests before they reach origin, request routing based on
geography or headers, and real-time personalization without origin round-trips. Edge functions have cold-start times
of ~0ms (V8 isolates, not containers) and run in ~50ms globally — closer to the CDN caching model than the traditional
serverless model.

## How It Works

**CDN Origin-Pull Request Flow:**

1. Client's DNS resolver queries for the domain; the CDN's DNS system (via Anycast or GeoDNS) returns the IP of the
   nearest Point of Presence (PoP)
2. Client opens an HTTPS connection to the PoP; TLS handshake terminates at the PoP — no TLS round trip to the origin
3. PoP computes a cache key from the request (scheme + host + URI path + relevant query parameters)
4. **Cache HIT:** PoP serves the cached response immediately (5–20ms latency); request never reaches the origin
5. **Cache MISS:** PoP opens (or reuses) a persistent connection to the origin server and fetches the resource
6. Origin responds with the resource and `Cache-Control` headers (e.g., `max-age=3600, public`)
7. PoP stores the response in its edge cache according to the `Cache-Control` directives and returns it to the client;
   subsequent requests from nearby users hit the cache

**Origin Pull vs Push:**

```
  Origin Pull (most common):
  Client → PoP → cache HIT → response (5ms)
               → cache MISS → origin → cache → response (200ms)

  Origin Push (for known-popular content):
  Deploy → push content to all PoPs → Client always hits cache
  Used for: video releases, game patches, scheduled content
```

**Cache key:** By default, Nginx (and most CDNs) cache by `scheme + host + URI` including query string. `?v=1` and
`?v=2` are different cache keys. The `Vary` header makes the CDN cache per header value — `Vary: Accept-Language`
creates separate cache entries per language, which can fragment the cache dramatically.

**Anycast routing:** CDNs use BGP anycast to route clients to the nearest PoP automatically. The same IP address is
announced from multiple PoPs; routers direct packets to the topologically closest announcement. This is transparent to
clients — they always send to the same IP, but the network delivers to different physical servers based on location.

**CDN for dynamic content:** CDNs can accelerate non-cacheable dynamic requests via persistent TCP connections. Without
a CDN, every request incurs TLS handshake (~100ms) + TCP slow start + network RTT to the origin. With a CDN, clients
TLS-terminate at the nearby PoP, which maintains a persistent connection to the origin. Even a cache MISS is faster
because the PoP-to-origin leg uses an optimised, already-warm connection.

**Stale-while-revalidate:** `Cache-Control: stale-while-revalidate=60` tells the CDN to serve stale content immediately
while fetching a fresh copy in the background. Users never wait for revalidation; at worst they get content that is up
to 60 seconds stale. This is the right trade-off for most non-financial content.

```
  Without stale-while-revalidate:
    t=30s: TTL expires
    t=30s+: next client waits for origin fetch (~200ms)

  With stale-while-revalidate=60:
    t=30s: TTL expires
    t=30s+: next client gets stale response immediately (0ms wait)
           CDN fetches fresh copy in background
    t=30s+200ms: cache updated for next client
```

### Trade-offs

| Strategy                  | Freshness         | Origin Load             | Implementation                    |
|---------------------------|-------------------|-------------------------|-----------------------------------|
| Long TTL + URL versioning | Instant on deploy | Very low                | Deploy pipeline must version URLs |
| Short TTL (30-300s)       | Near-real-time    | Moderate                | Simple, slight staleness          |
| CDN purge API             | Instant on demand | Low after purge         | Complex, propagation delay        |
| Stale-while-revalidate    | Near-real-time    | Low                     | Best UX, slight staleness         |
| no-cache (ETag)           | Always fresh      | High (conditional GETs) | Complex, saves bandwidth not RTT  |

### Failure Modes

**Origin overload on CDN failure:** If a PoP goes offline, traffic falls back to the origin. If the origin was sized for
only cache-miss traffic (10% of total), it will be overwhelmed by 100% of traffic. Mitigation: origin must be sized for
full traffic, or use CDN failover to another PoP.

**Cache poisoning:** An attacker causes the CDN to cache a malicious response by crafting a request with unusual
headers. If the CDN varies cache keys on a header that the origin ignores, a crafted request can poison the cache for
all users. Mitigation: normalize input at the CDN layer, use `Vary` sparingly, validate `Cache-Control` headers from
origin.

**Privacy leakage via CDN caching personalised content:** If a response containing user-specific data (e.g.,
`Authorization: Bearer` response) is accidentally cached at the CDN, the next user's request may receive another user's
data. Mitigation: always set `Cache-Control: private, no-store` on authenticated responses; test with
`Surrogate-Control` headers to separate CDN from browser cache policy.

**Thundering herd on popular item cache miss (cache stampede):** When a cached item expires, many concurrent requests
may all simultaneously attempt to fetch from the origin. With 10,000 concurrent users, this can send 10,000 requests to
the origin in one second. Mitigation: CDN request coalescing (Nginx `proxy_cache_lock on`), stale-while-revalidate, or
probabilistic early expiry (jitter on TTL).

**Vary header cache fragmentation:** `Vary: Accept-Language` instructs the CDN to maintain a separate cache entry per
language. With 20 languages, a single URL consumes 20 cache slots and the effective hit rate per slot drops by 20x.
`Vary: Cookie` or `Vary: Authorization` is even worse — it creates per-user cache entries, consuming unbounded storage
and effectively disabling CDN caching. Mitigation: use `Vary: Accept-Encoding` only (safe, bounded variants); serve
language variants at distinct URLs (e.g., `/en/page`, `/fr/page`) instead of relying on `Vary`.

**CDN misconfiguration exposes origin:** Misconfigured CDN rules (e.g., a wildcard passthrough route or a miscategorised
path) can accidentally bypass caching and route all traffic to the origin. A CDN that is supposed to absorb 90% of
traffic suddenly passes 100% through — origin overloads without the traffic ever appearing to increase from the CDN
metrics. Mitigation: monitor origin request rate independently of CDN hit rate; alert on abnormal origin traffic
increases.

## Interview Talking Points

- "A CDN is a distributed cache at the network edge. The cache key is the URL; the value is the HTTP response. Cache
  hits never touch the origin."
- "Cache-Control is the contract between origin and CDN. `s-maxage` overrides `max-age` for CDNs. `private` means CDNs
  must not cache it — critical for auth responses."
- "The best cache invalidation strategy is not invalidating at all — use URL versioning. Hash the file content into the
  filename; when content changes, the URL changes."
- "CDN latency advantage comes from both proximity (shorter RTT) and persistent connections (no TLS handshake per
  request). Even cache misses are faster."
- "Edge computing runs application logic at CDN PoPs — Cloudflare Workers, Fastly Compute@Edge. Zero cold start, global
  distribution, ~1ms execution for simple logic."
- "Anycast routing: CDNs advertise the same IP from multiple PoPs using BGP. The internet routes your packet to the
  topologically nearest PoP automatically."
- "stale-while-revalidate is the right default for most non-financial content. It eliminates the latency spike at TTL
  expiry boundaries by serving stale immediately and refreshing in the background. Without it, the first post-expiry
  request blocks for the full origin RTT — a predictable p99 problem under load."
- "Vary header is a double-edged sword. `Vary: Accept-Encoding` is safe (2-3 variants). `Vary: Accept-Language`
  fragments the cache by language count. `Vary: Cookie` or `Vary: Authorization` effectively disables CDN caching and
  can leak personalised data between users. Never set Vary on auth-gated responses — use `Cache-Control: private`
  instead."
- "Origin must be sized for full traffic, not just cache-miss traffic. If a CDN PoP fails or is bypassed, 100% of
  requests suddenly hit the origin. Sizing only for 10% traffic (the usual miss rate) guarantees a cascade failure on
  any CDN incident."

## Hands-on Lab

**Time:** ~2 minutes
**Services:** Nginx (edge proxy with cache) + Python Flask origin server

### Setup

```bash
cd system-design-interview/02-advanced/11-cdn-and-edge/
docker compose up
```

### Experiment

The script runs eight phases automatically:

1. **MISS → HIT:** Requests `/content/image.jpg` three times — first is a MISS (~200ms), subsequent are HITs (<5ms). The
   `X-Origin-Hit` counter confirms the origin is only contacted once.
2. **no-store:** Requests `/nocache/data.json` three times — all BYPASS because the origin sends
   `Cache-Control: no-store`. The origin hit counter increments on every request.
3. **TTL expiry:** Requests `/short-ttl/report.pdf` (5s TTL), waits 6 seconds, shows the cache expires and re-fetches
   from origin. Then HITs again on the next request.
4. **URL versioning:** Requests `/content/bundle.js?v=1` then `?v=2` — different query strings are different cache keys,
   so `?v=2` always starts with a MISS.
5. **stale-while-revalidate:** Requests `/swr/data.json` (max-age=5, swr=10), sleeps 6 seconds, then shows a STALE
   response served immediately while a background fetch refreshes the cache — no latency spike at TTL expiry.
6. **Vary header fragmentation:** Requests `/vary/page.html` with `Accept-Language: en-US`, `fr-FR`, `de-DE`, then
   `en-US` again — each language is a separate cache entry (MISS) but the second `en-US` is a HIT.
7. **Private content:** Requests `/private/profile.json` as different users — all BYPASS regardless of user; the origin
   is contacted every time, confirming auth responses never reach the CDN cache.
8. **Performance summary:** Compares MISS vs HIT latency across 5 samples and prints total origin hit counts per
   endpoint to show how many requests actually escaped the CDN.

### Break It: Cache Stampede

Test the thundering-herd / cache stampede failure mode by temporarily disabling `proxy_cache_lock` in `nginx.conf`:

```nginx
# In nginx.conf, comment out or remove from the /content/ location:
# proxy_cache_lock on;
```

Then send 20 concurrent requests to a cold URL:

```bash
# Run from your host machine while docker compose is up:
python3 -c "
import threading, requests, time, collections

results = []
lock = threading.Lock()

def req(i):
    url = 'http://localhost:8080/content/stampede_test.jpg'
    t = time.time()
    r = requests.get(url)
    elapsed = (time.time() - t) * 1000
    with lock:
        results.append((r.headers.get('X-Cache-Status'), elapsed,
                         r.headers.get('X-Origin-Hit', '?')))

threads = [threading.Thread(target=req, args=(i,)) for i in range(20)]
[t.start() for t in threads]
[t.join() for t in threads]

status_counts = collections.Counter(s for s, _, _ in results)
origin_hits = [h for _, _, h in results]
print(f'Results: {dict(status_counts)}')
print(f'Origin hit numbers seen: {sorted(set(origin_hits))}')
print('With cache_lock ON:  only 1 MISS; 19 requests wait and get HIT')
print('With cache_lock OFF: up to 20 MISSes; 20 concurrent origin requests')
"
```

### Observe

With `proxy_cache_lock on` (the default in this lab), only one request fetches from the origin; the remaining 19 wait
and are served from the just-populated cache entry. All `X-Origin-Hit` values will be `1`.

Without `proxy_cache_lock`, all 20 requests arrive at the origin simultaneously. Under real load (10,000 RPS hitting a
short-TTL endpoint) this can briefly send thousands of requests to an origin sized for only cache-miss traffic — a
leading cause of origin overload cascades.

`stale-while-revalidate` is complementary: it prevents the stampede at TTL expiry time by serving the stale entry while
exactly one background request refreshes the cache.

### Teardown

```bash
docker compose down
```

## Real-World Examples

- **Cloudflare CDN (2024):** Cloudflare operates 300+ PoPs globally, processes ~45 million HTTP requests per second, and
  caches roughly 40% of all internet requests. They use a tiered caching architecture: regional PoPs check upper-tier
  PoPs before going to origin, reducing origin load further. Source: Cloudflare, "Cloudflare's Impact on the Internet,"
  2024 Year in Review.
- **Netflix Open Connect:** Netflix built its own CDN called Open Connect, deploying custom appliances directly inside
  ISPs' data centres (not colocation facilities). These appliances pre-populate overnight with that region's expected
  popular content via origin push. During peak hours, 95%+ of Netflix traffic is served from these appliances without
  touching Netflix's cloud origin. Source: Netflix Tech Blog, "How Netflix Works with ISPs Around the Globe," 2016.
- **Akamai Image Manager:** Akamai's edge nodes perform real-time image transformation — resizing, format conversion (
  WebP/AVIF), quality adjustment — based on the requesting device's User-Agent. A single origin stores one
  high-resolution master; the CDN edge generates and caches device-optimised variants. This reduces origin storage
  complexity while improving cache hit rate per device type. Source: Akamai, "Image & Video Manager," product
  documentation.

## Common Mistakes

- **Caching authenticated responses.** If `Authorization` or `Set-Cookie` headers appear in a response and the origin
  forgets `Cache-Control: private`, the CDN may cache and serve that response to all users. Always test authenticated
  endpoints to confirm they return `Cache-Control: private, no-store`.
- **Using `no-cache` to mean "don't cache."** `Cache-Control: no-cache` does NOT mean "don't cache" — it means "cache
  but always revalidate." Use `no-store` to prevent caching entirely. This is one of the most common HTTP caching
  misunderstandings.
- **Not including query strings in cache keys for APIs.** `/api/items?page=1` and `/api/items?page=2` must be separate
  cache entries. Some CDN configurations strip query strings by default for "cleaner" URLs, accidentally collapsing
  paginated responses onto the same cache entry.
- **Forgetting to set a TTL on cache entries.** An origin that returns `200 OK` without a `Cache-Control` header gets a
  default CDN cache time — often 0s (no caching) or a CDN-defined default (Cloudflare defaults to 4 hours for some
  content types). Always be explicit about your caching intent.
- **Over-relying on CDN purge APIs for deployment.** Purge APIs have eventual consistency — propagation across all PoPs
  can take 10-60 seconds. If your deploy process relies on instant purge, some users will see old content during the
  propagation window. URL versioning eliminates this risk entirely.
