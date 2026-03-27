#!/usr/bin/env python3
"""
API Design Lab — REST vs GraphQL

Prerequisites: docker compose up -d  (wait ~40s for pip installs)

Demonstrates:
  1. N+1 query problem with REST (HTTP layer)
  2. GraphQL solving HTTP-layer N+1 in one request
  3. GraphQL N+1 at the RESOLVER layer — still fires N DB queries
  4. Over-fetching: REST returns full object vs needed fields only
  5. Idempotency keys: safe retries for non-idempotent operations
  6. REST eager-loading (?include=posts) — server-side N+1 fix
  7. REST vs GraphQL comparison table
"""

import json
import time
import uuid
import urllib.request
import urllib.error

REST_BASE    = "http://localhost:8001"
GRAPHQL_BASE = "http://localhost:8002"


def section(title):
    print(f"\n{'=' * 68}")
    print(f"  {title}")
    print("=" * 68)


def subsection(title):
    print(f"\n  -- {title} --")


def http_get(url: str) -> tuple[int, bytes]:
    """Returns (status_code, body_bytes)."""
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def http_post(url: str, body: dict, headers: dict | None = None) -> tuple[int, dict]:
    """POST JSON body, return (status_code, response_dict)."""
    payload = json.dumps(body).encode()
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=payload, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def graphql_query(query: str) -> dict:
    _, result = http_post(f"{GRAPHQL_BASE}/graphql", {"query": query})
    return result


def wait_for_services():
    print("  Waiting for services to be ready...")
    endpoints = [
        ("REST API",    f"{REST_BASE}/health"),
        ("GraphQL API", f"{GRAPHQL_BASE}/health"),
    ]
    for name, url in endpoints:
        for attempt in range(24):
            try:
                status, _ = http_get(url)
                if status == 200:
                    print(f"  {name} is ready.")
                    break
            except Exception:
                pass
            print(f"    {name}: attempt {attempt + 1}/24, retrying in 5s...")
            time.sleep(5)
        else:
            print(f"  ERROR: {name} never became ready. Run: docker compose up -d")
            raise SystemExit(1)


# ── Phase 1: N+1 Problem (HTTP layer) ─────────────────────────────────────────

def phase_n_plus_1():
    section("Phase 1: N+1 Query Problem with REST (HTTP Layer)")
    print("""
  Scenario: Fetch 5 users AND their posts.

  Naive REST approach:
    1. GET /users         → get user list
    2. GET /users/1/posts → posts for user 1
    3. GET /users/2/posts → posts for user 2
    ... and so on.

  Total HTTP requests = 1 + N  (the "N+1 problem")
""")

    request_count = 0
    t0 = time.perf_counter()

    status, body = http_get(f"{REST_BASE}/users")
    request_count += 1
    users = json.loads(body)[:5]
    print(f"  Request 1: GET /users  → {len(users)} users")

    for user in users:
        uid = user["id"]
        status, body = http_get(f"{REST_BASE}/users/{uid}/posts")
        request_count += 1
        posts = json.loads(body)
        print(f"  Request {request_count}: GET /users/{uid}/posts → {len(posts)} posts")

    elapsed = (time.perf_counter() - t0) * 1000

    print(f"""
  Total HTTP requests: {request_count}
  Wall-clock time:     {elapsed:.1f}ms  (sequential — each waits for previous)

  At scale:
    100 users on a page → 101 HTTP requests per page load
    Each request ~5ms intra-DC → 500ms+ added latency just from round trips
    Plus TCP connection overhead if not using HTTP keep-alive / connection pool

  Solutions (covered in later phases):
    a) REST eager-loading:  GET /users?include=posts  (Phase 6)
    b) GraphQL single query (Phase 2) — solves HTTP layer, not DB layer
    c) DataLoader pattern   — batches DB queries in the same event-loop tick
    d) Batch endpoint:      POST /users/batch  {{user_ids: [1,2,3,...]}}
""")
    return elapsed


# ── Phase 2: GraphQL — One HTTP Request ───────────────────────────────────────

def phase_graphql():
    section("Phase 2: GraphQL — One HTTP Request for Users + Posts")
    print("""
  Same data, one GraphQL query:
    query {
      users {
        id name email
        posts { id title likes }
      }
    }
""")

    # Reset resolver stats before the query so counts are clean
    http_get(f"{GRAPHQL_BASE}/resolver-stats")

    query = """
    {
      users {
        id
        name
        email
        posts {
          id
          title
          likes
        }
      }
    }
    """

    t0 = time.perf_counter()
    result = graphql_query(query)
    elapsed = (time.perf_counter() - t0) * 1000

    users = result.get("data", {}).get("users", [])
    total_posts = sum(len(u.get("posts", [])) for u in users)

    # Fetch resolver call count recorded server-side during this query
    _, stats_bytes = http_get(f"{GRAPHQL_BASE}/resolver-stats")
    stats_dict = json.loads(stats_bytes) if stats_bytes else {}
    resolver_calls = stats_dict.get("posts_resolver_calls", "?")

    print(f"  Result: {len(users)} users, {total_posts} total posts")
    print(f"  HTTP requests made:          1  (vs {1 + len(users)} with naive REST)")
    print(f"  Round-trip time:             {elapsed:.1f}ms")
    print(f"  posts resolver calls fired:  {resolver_calls}  ← see Phase 3")
    print(f"  Sample: {users[0]['name']} — {len(users[0]['posts'])} posts")
    print("""
  GraphQL wins at the HTTP layer:
    - Client declares exactly the fields it needs
    - Server resolves everything in one HTTP round trip
    - No waterfall: web/mobile client does not orchestrate multiple requests
    - Schema is self-documenting (introspection endpoint built-in)
    - Strongly typed: schema violations caught at query-parse time, not runtime
""")
    return len(users)


# ── Phase 3: GraphQL N+1 at the Resolver Layer ────────────────────────────────

def phase_graphql_resolver_n_plus_1(num_users: int):
    section("Phase 3: GraphQL N+1 at the RESOLVER Layer (DB Layer)")
    print(f"""
  GraphQL solved HTTP-layer N+1 (1 request instead of {1 + num_users}).
  But INSIDE the server, the posts resolver still fires once per user.

  What happens server-side for `users {{ posts {{ ... }} }}`:
    Resolver for users() → returns {num_users} User objects
    For each User, Strawberry calls user.posts() resolver:
      → SELECT * FROM posts WHERE user_id = 1   (resolver call 1)
      → SELECT * FROM posts WHERE user_id = 2   (resolver call 2)
      ...
      → SELECT * FROM posts WHERE user_id = {num_users}   (resolver call {num_users})

  The GraphQL server just moved the N+1 from HTTP to the database layer.
  The client doesn't see it, but the DB does — {num_users} queries instead of 1.

  Fix: DataLoader pattern
    DataLoader collects all user_id arguments from concurrent resolver calls
    within the same event-loop tick, then fires ONE batched query:
      SELECT * FROM posts WHERE user_id IN (1, 2, 3, ..., {num_users})
    Results are distributed back to each waiting resolver.

  DataLoader pseudo-code (Python async):
    posts_loader = DataLoader(batch_fn=lambda ids:
        db.query("SELECT * FROM posts WHERE user_id = ANY(%s)", [ids])
    )
    # In User.posts resolver:
    return await posts_loader.load(self.id)

  GraphQL N+1 at the DB layer is a production correctness issue, not just
  performance. At 100 concurrent requests × 100 users each = 10,000 DB queries
  per second where 100 would suffice — this saturates connection pools.
""")


# ── Phase 4: Over-fetching ─────────────────────────────────────────────────────

def phase_over_fetching():
    section("Phase 4: Over-fetching — REST Returns More Than You Need")
    print("""
  Use case: Display a list of user names only.
  REST returns the entire user object regardless of what you need.
""")

    status, body = http_get(f"{REST_BASE}/users/1")
    full_object = json.loads(body)
    full_bytes = len(body)

    name_only = {"name": full_object["name"]}
    name_bytes = len(json.dumps(name_only).encode())

    print(f"  REST GET /users/1:")
    print(f"    Response size:   {full_bytes} bytes")
    print(f"    Fields returned: {list(full_object.keys())}")
    print(f"    Fields needed:   ['name']")
    print(f"    Needed size:     {name_bytes} bytes")
    print(f"    Over-fetch ratio: {full_bytes / name_bytes:.1f}x more data than needed")

    result = graphql_query('{ user(id: "1") { name } }')
    gql_user = result.get("data", {}).get("user", {})
    gql_response_bytes = len(json.dumps({"data": {"user": gql_user}}).encode())

    print(f"\n  GraphQL {{ user(id: \"1\") {{ name }} }}:")
    print(f"    Response size:   {gql_response_bytes} bytes")
    print(f"    Content:         {gql_user}")

    rps = 10_000
    rest_mbps = rps * full_bytes / 1_000_000
    gql_mbps  = rps * gql_response_bytes / 1_000_000
    print(f"""
  Bandwidth impact at {rps:,} req/s:
    REST:    {rest_mbps:.1f} MB/s
    GraphQL: {gql_mbps:.1f} MB/s
    Saved:   {rest_mbps - gql_mbps:.1f} MB/s of unnecessary transfer

  Under-fetching (the opposite problem):
    REST:    display user + their posts + comment count → 3 separate requests (waterfall)
    GraphQL: one query nests all of it — zero extra round trips
""")


# ── Phase 5: Idempotency Keys ─────────────────────────────────────────────────

def phase_idempotency():
    section("Phase 5: Idempotency Keys — Safe Retries for Non-Idempotent Ops")
    print("""
  Problem: Networks are unreliable. A POST /payments request times out.
  Did it succeed before the timeout? The client doesn't know.
  Without idempotency, retrying creates a DUPLICATE CHARGE.

  Solution: Client sends a UUID in Idempotency-Key header.
  Server checks Redis before processing:
    - Key not seen → process payment, cache result under key (24h TTL)
    - Key already seen → return cached result, skip processing entirely

  This makes the operation safe to retry any number of times.
""")

    payment_body = {"amount": 99, "to": "user_456"}
    idempotency_key = str(uuid.uuid4())

    subsection(f"Attempt 1 — First request (key: {idempotency_key[:8]}...)")
    t0 = time.perf_counter()
    status1, resp1 = http_post(
        f"{REST_BASE}/payments",
        payment_body,
        headers={"Idempotency-Key": idempotency_key},
    )
    t1 = (time.perf_counter() - t0) * 1000
    print(f"    HTTP {status1} in {t1:.0f}ms")
    print(f"    payment_id:         {resp1.get('payment_id')}")
    print(f"    idempotency_status: {resp1.get('idempotency_status')}")

    subsection("Attempt 2 — Retry with SAME key (simulates client retry after timeout)")
    t0 = time.perf_counter()
    status2, resp2 = http_post(
        f"{REST_BASE}/payments",
        payment_body,
        headers={"Idempotency-Key": idempotency_key},
    )
    t2 = (time.perf_counter() - t0) * 1000
    print(f"    HTTP {status2} in {t2:.0f}ms")
    print(f"    payment_id:         {resp2.get('payment_id')}")
    print(f"    idempotency_status: {resp2.get('idempotency_status')}")

    subsection("Attempt 3 — No idempotency key (DANGEROUS — double-charge risk)")
    t0 = time.perf_counter()
    status3, resp3 = http_post(f"{REST_BASE}/payments", payment_body)
    t2 = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    status4, resp4 = http_post(f"{REST_BASE}/payments", payment_body)
    t3 = (time.perf_counter() - t0) * 1000
    print(f"    First  call: HTTP {status3} → payment_id: {resp3.get('payment_id')}")
    print(f"    Retry  call: HTTP {status4} → payment_id: {resp4.get('payment_id')}")
    ids_differ = resp3.get("payment_id") != resp4.get("payment_id")
    print(f"    payment_ids differ: {ids_differ}  ← {'DOUBLE CHARGE OCCURRED' if ids_differ else 'same (lucky)'}")

    print(f"""
  Key observations:
    Attempt 1 → NEW_PAYMENT_PROCESSED  (took ~50ms to "process")
    Attempt 2 → DUPLICATE_RETURNED_FROM_CACHE  (same payment_id, fast cache hit)
    Attempt 3 → two DIFFERENT payment_ids — customer charged twice

  Production rules:
    - Every endpoint with financial or irreversible side effects MUST accept
      Idempotency-Key and store results for at least 24h (Stripe's standard)
    - Key storage: Redis with TTL (fast read, automatic expiry)
    - Key scope: per-customer or per-session, not global, to prevent collisions
    - HTTP verbs: GET/DELETE/PUT are naturally idempotent; POST/PATCH are not
    - Idempotency key ≠ deduplication key — they serve different purposes
""")


# ── Phase 6: REST Eager-Loading (Server-side N+1 fix) ─────────────────────────

def phase_eager_loading():
    section("Phase 6: REST Eager-Loading — Server-Side N+1 Fix")
    print("""
  Phase 1 showed N+1: 6 HTTP requests to get 5 users with posts.
  GraphQL (Phase 2) solves it at the HTTP layer with 1 request.
  REST can also solve it server-side with an ?include= query parameter.

  GET /users?include=posts
    → Server fetches users + posts in one pass
    → Returns combined payload in one HTTP response
    → Client makes 1 request, not N+1
""")

    t0 = time.perf_counter()
    status, body = http_get(f"{REST_BASE}/users?include=posts")
    elapsed = (time.perf_counter() - t0) * 1000
    users = json.loads(body)[:5]
    total_posts = sum(len(u.get("posts", [])) for u in users)

    print(f"  GET /users?include=posts")
    print(f"    HTTP requests made: 1")
    print(f"    Round-trip time:    {elapsed:.1f}ms")
    print(f"    Users returned:     {len(users)}")
    print(f"    Total posts:        {total_posts}")
    print("""
  Trade-offs vs GraphQL:
    REST eager-loading: explicit, cacheable by URL if public, simple to implement
    REST eager-loading: requires server changes per new include combination
    GraphQL:            client controls what's fetched, no server change needed
    GraphQL:            harder to cache, higher learning curve, more attack surface

  In practice: REST ?include= works well for internal APIs with few combos.
  GraphQL shines when many client types need different data shapes.
""")


# ── Phase 7: Comparison Table ─────────────────────────────────────────────────

def phase_comparison_table():
    section("Phase 7: REST vs GraphQL — Full Comparison")
    print("""
  ┌──────────────────────┬──────────────────────────────┬──────────────────────────────────┐
  │ Aspect               │ REST                         │ GraphQL                          │
  ├──────────────────────┼──────────────────────────────┼──────────────────────────────────┤
  │ Data fetching        │ Fixed endpoints              │ Client-defined queries           │
  │ Over-fetching        │ Common (returns whole obj)   │ None (request exactly what's     │
  │                      │                              │ needed)                          │
  │ Under-fetching       │ N+1 HTTP requests            │ Nest in one query                │
  │ DB-layer N+1         │ Server responsibility        │ DataLoader required per resolver  │
  │ Versioning           │ /v1/, /v2/ in URL            │ Schema evolution + deprecations  │
  │ HTTP caching         │ Native (GET cacheable by URL)│ Hard (POST body, dynamic)        │
  │ Error handling       │ HTTP status codes            │ Always 200; errors in body       │
  │ Type safety          │ Via OpenAPI/Swagger           │ Built-in schema + introspection  │
  │ Complexity attacks   │ N/A                          │ Depth/cost limits required       │
  │ File upload          │ Native multipart             │ Awkward (spec extension)         │
  │ Real-time            │ SSE / WebSocket separate     │ Subscriptions built-in           │
  │ Idempotency          │ Via Idempotency-Key header   │ Via Idempotency-Key header       │
  │ Best for             │ Public APIs, CRUD, caching   │ Complex/nested data, mobile      │
  └──────────────────────┴──────────────────────────────┴──────────────────────────────────┘

  GraphQL caching strategies (since HTTP caches can't key on POST bodies):
    1. Persisted queries:  hash query → GET /graphql?queryId=abc123  (CDN-cacheable)
    2. Apollo Client:      normalized in-memory cache keyed by type+id
    3. HTTP GET for reads: GET /graphql?query={...}  (URL-encodable for simple queries)
    4. Response caching:   cache entire operation results at app layer (Redis)

  API paradigm selection guide for FAANG interviews:
    External / public API  → REST   (familiar, cacheable, stable contract)
    Internal microservices → gRPC   (binary, streaming, compile-time type safety)
    Mobile / multi-client  → GraphQL (precise fetching, bandwidth savings)
    Event notifications    → Webhooks + WebSockets (push, not poll)

  Next: ../12-blob-object-storage/
""")


def main():
    section("API DESIGN LAB — REST vs GraphQL")
    print("""
  Services:
    REST API    → http://localhost:8001
    GraphQL API → http://localhost:8002/graphql

  Run: docker compose up -d  (wait ~40s for pip installs)
""")

    wait_for_services()
    phase_n_plus_1()
    num_users = phase_graphql()
    phase_graphql_resolver_n_plus_1(num_users)
    phase_over_fetching()
    phase_idempotency()
    phase_eager_loading()
    phase_comparison_table()


if __name__ == "__main__":
    main()
