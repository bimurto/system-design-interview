#!/usr/bin/env python3
"""
API Design Lab — REST vs GraphQL

Prerequisites: docker compose up -d  (wait ~40s for pip installs)

Demonstrates:
  1. N+1 query problem with REST
  2. GraphQL solving N+1 in one request
  3. Over-fetching: REST returns full object vs needed field only
  4. REST vs GraphQL comparison table
"""

import json
import time
import urllib.request
import urllib.error

REST_BASE    = "http://localhost:8001"
GRAPHQL_BASE = "http://localhost:8002"


def section(title):
    print(f"\n{'=' * 64}")
    print(f"  {title}")
    print("=" * 64)


def http_get(url: str) -> tuple[int, bytes]:
    """Returns (status_code, body_bytes)."""
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def graphql_query(query: str) -> dict:
    payload = json.dumps({"query": query}).encode()
    req = urllib.request.Request(
        f"{GRAPHQL_BASE}/graphql",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def wait_for_services():
    print("  Waiting for services to be ready...")
    for name, url in [("REST API", f"{REST_BASE}/health"), ("GraphQL API", f"{GRAPHQL_BASE}/health")]:
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


# ── Phase 1: N+1 Problem ───────────────────────────────────────────────────────

def phase_n_plus_1():
    section("Phase 1: N+1 Query Problem with REST")
    print("""
  Scenario: Fetch 5 users AND their posts.

  Naive REST approach:
    1. GET /users         → get user list
    2. GET /users/1/posts → get posts for user 1
    3. GET /users/2/posts → get posts for user 2
    ... and so on.

  Total requests = 1 + N  (the "N+1" problem)
""")

    request_count = 0

    # Step 1: get users
    status, body = http_get(f"{REST_BASE}/users")
    request_count += 1
    users = json.loads(body)[:5]  # only first 5
    print(f"  Request 1: GET /users  → got {len(users)} users")

    # Step 2: fetch posts for each user
    all_posts = []
    for user in users:
        uid = user["id"]
        status, body = http_get(f"{REST_BASE}/users/{uid}/posts")
        request_count += 1
        posts = json.loads(body)
        all_posts.append((user["name"], len(posts)))
        print(f"  Request {request_count}: GET /users/{uid}/posts → {len(posts)} posts")

    print(f"""
  Total HTTP requests made: {request_count}
  That's 1 (list) + {len(users)} (individual post fetches) = {request_count} requests

  At scale:
    100 users on a page → 101 requests per page load
    Each request ~5ms intra-DC → 500ms added latency just for posts
    Plus connection overhead if not using connection pooling

  Solutions:
    a) Add GET /users?include=posts  (REST: eager loading via query params)
    b) Use GraphQL (fetch exactly what you need in one request)
    c) Batch endpoint: POST /users/batch-posts  {{user_ids: [...]}}
    d) DataLoader pattern (batches DB queries in same event loop tick)
""")
    return request_count


# ── Phase 2: GraphQL — One Request ────────────────────────────────────────────

def phase_graphql():
    section("Phase 2: GraphQL — One Request for Users + Posts")
    print("""
  Same data, one GraphQL query:
    query {
      users {
        id name email
        posts { id title likes }
      }
    }
""")

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

    print(f"  Result: {len(users)} users, {total_posts} total posts")
    print(f"  HTTP requests made: 1")
    print(f"  Round-trip time: {elapsed:.1f}ms")
    print(f"  Sample user: {users[0]['name']} — {len(users[0]['posts'])} posts")
    print("""
  GraphQL advantages:
    - Client declares exactly what fields it needs
    - Server resolves everything in one request
    - No N+1 at the HTTP layer (though DB N+1 can still occur — use DataLoader)
    - Schema is self-documenting (introspection)
    - Strongly typed: schema violations caught at query parse time
""")


# ── Phase 3: Over-fetching ─────────────────────────────────────────────────────

def phase_over_fetching():
    section("Phase 3: Over-fetching — REST Returns More Than You Need")
    print("""
  Use case: Display a list of user names only.
  REST returns the entire user object regardless.
""")

    status, body = http_get(f"{REST_BASE}/users/1")
    full_object = json.loads(body)
    full_bytes = len(body)

    # What we actually needed
    name_only = {"name": full_object["name"]}
    name_bytes = len(json.dumps(name_only).encode())

    print(f"  REST GET /users/1 response size: {full_bytes} bytes")
    print(f"  Fields returned: {list(full_object.keys())}")
    print(f"  Fields needed for display: ['name']")
    print(f"  Bytes needed: {name_bytes} bytes")
    print(f"  Over-fetching ratio: {full_bytes / name_bytes:.1f}x more data than needed")

    # GraphQL: fetch only name
    result = graphql_query('{ user(id: "1") { name } }')
    gql_user = result.get("data", {}).get("user", {})
    gql_bytes = len(json.dumps(gql_user).encode())

    print(f"\n  GraphQL {{ user(id: \"1\") {{ name }} }} response: {gql_bytes} bytes")
    print(f"  Content: {gql_user}")
    print(f"""
  Over-fetching impact at scale:
    10,000 requests/sec × {full_bytes} bytes = {10000 * full_bytes / 1_000_000:.1f} MB/s vs {10000 * gql_bytes / 1_000_000:.1f} MB/s with GraphQL
    Difference: {10000 * (full_bytes - gql_bytes) / 1_000_000:.1f} MB/s of unnecessary data transfer

  Under-fetching (opposite problem):
    REST: need user + posts + comments → 3 requests (waterfall)
    GraphQL: one query gets all nested data
""")


# ── Phase 4: Comparison Table ─────────────────────────────────────────────────

def phase_comparison_table():
    section("Phase 4: REST vs GraphQL Comparison")
    print("""
  ┌─────────────────────┬──────────────────────────┬──────────────────────────────┐
  │ Aspect              │ REST                     │ GraphQL                      │
  ├─────────────────────┼──────────────────────────┼──────────────────────────────┤
  │ Data fetching       │ Fixed endpoints          │ Client-defined queries       │
  │ Over-fetching       │ Common (whole object)    │ No (request only what's needed)│
  │ Under-fetching      │ Common (N+1 problem)     │ No (nest in one query)       │
  │ Versioning          │ /v1/, /v2/ in URL        │ Schema evolution, deprecation│
  │ Caching             │ Easy (HTTP GET caching)  │ Hard (POST, dynamic queries) │
  │ Error handling      │ HTTP status codes        │ Always 200; errors in body   │
  │ Type safety         │ Via OpenAPI/Swagger       │ Built-in schema + introspect │
  │ Learning curve      │ Low (HTTP + JSON)        │ Medium (query language)      │
  │ Tooling             │ Mature (Postman, curl)   │ GraphiQL, Apollo DevTools    │
  │ File upload         │ Native multipart         │ Awkward (spec extension)     │
  │ Real-time           │ SSE / WebSocket separate │ Subscriptions built-in       │
  │ Best for            │ Public APIs, CRUD        │ Complex/nested data, mobile  │
  └─────────────────────┴──────────────────────────┴──────────────────────────────┘

  GraphQL caching challenge:
    REST: GET /users/1 → cacheable by URL (CDN, browser, proxy)
    GraphQL: POST /graphql with body → not cacheable by standard HTTP caches
    Solutions: persisted queries (hash → query), GET for queries, Apollo Cache

  When to use REST:
    - Public API consumed by many clients (stable contract)
    - Simple CRUD with few relationships
    - Need HTTP-level caching (CDN, CloudFront)
    - Team unfamiliar with GraphQL

  When to use GraphQL:
    - Mobile apps (minimize bandwidth, avoid over-fetching)
    - Complex nested data (social graph, e-commerce catalog)
    - Rapid frontend iteration (no backend changes for new field subsets)
    - Multiple client types needing different data shapes

  Next: ../12-blob-object-storage/
""")


def main():
    section("API DESIGN LAB — REST vs GraphQL")
    print("""
  Services:
    REST API   → http://localhost:8001
    GraphQL API → http://localhost:8002/graphql

  Run: docker compose up -d  (wait ~40s for pip installs)
""")

    wait_for_services()
    phase_n_plus_1()
    phase_graphql()
    phase_over_fetching()
    phase_comparison_table()


if __name__ == "__main__":
    main()
