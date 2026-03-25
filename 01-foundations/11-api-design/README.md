# 11 — API Design

**Prerequisites:** [10 — Networking Basics](../10-networking-basics/)
**Next:** [12 — Blob/Object Storage](../12-blob-object-storage/)

---

## Concept

An API is a contract between systems, and contracts are expensive to break. Once external clients — mobile apps, third-party integrators, partner services — depend on your API's shape, changing it requires coordinated migrations, versioning strategies, and often years of maintaining deprecated endpoints in parallel. This is why API design decisions made early in a system's life have outsized consequences: a poorly chosen URL structure, a non-idiomatic use of HTTP verbs, or a missing pagination strategy will be load-bearing technical debt for years. Designing APIs defensively — with clear contracts, explicit versioning, and forward-compatible schemas — is cheaper than retrofitting later.

REST (Representational State Transfer) remains the dominant style for external and public APIs. Its constraints — statelessness, uniform interface, cacheability — align naturally with the web's infrastructure. Statelessness means every request carries its own authentication context (a JWT or API key in the header), enabling horizontal scaling without sticky sessions. The uniform interface (GET for reads, POST for creates, PUT/PATCH for updates, DELETE for deletes, with standard HTTP status codes) provides predictability that every developer already knows. Cacheability is REST's underrated superpower: GET responses can be cached by browsers, CDNs, and reverse proxies with no application-layer involvement.

GraphQL emerged at Facebook in 2012 to solve REST's structural problems at mobile scale. REST suffers from over-fetching (a `/users/1` endpoint returns 20 fields when the mobile app needs only the name) and under-fetching (displaying a user's posts requires a second request to `/users/1/posts`, creating waterfall latency). At Facebook's scale — hundreds of millions of mobile users on slow networks — these inefficiencies were significant. GraphQL lets the client declare exactly the fields and relationships it needs in a single request; the server resolves the entire graph. The tradeoff is caching complexity: GraphQL uses POST requests with dynamic bodies, which HTTP caches cannot key on without application-level workarounds like persisted queries.

gRPC is the dominant choice for internal service-to-service communication. It uses Protocol Buffers (protobuf) — a binary serialization format that is 3–10x more compact than JSON and schema-enforced, catching type mismatches at compile time rather than in production. Built on HTTP/2, gRPC supports four communication modes: unary (standard request/response), server streaming, client streaming, and bidirectional streaming. The strongly-typed `.proto` contract generates client and server stubs in any language, making cross-language internal services type-safe. The main limitation is debuggability: binary frames cannot be inspected with curl or browser DevTools without a grpc-web proxy.

---

## How It Works

### REST — Richardson Maturity Model

| Level | Name                  | Description                                               | Example                                                        |
|-------|-----------------------|-----------------------------------------------------------|----------------------------------------------------------------|
| 0     | The Swamp of POX      | HTTP as a transport tunnel. One endpoint, everything POST | `POST /api` with action in body                               |
| 1     | Resources             | Separate URLs per resource                                | `POST /users`, `POST /users/1`                                |
| 2     | HTTP Verbs            | Use GET/POST/PUT/DELETE correctly with status codes       | `GET /users/1 → 200`, `DELETE → 204`                         |
| 3     | Hypermedia (HATEOAS)  | Responses include links to next actions                   | `{"user": {...}, "_links": {"posts": "/users/1/posts"}}`      |

Most real-world REST APIs operate at Level 2. Level 3 (HATEOAS) is rarely implemented despite being the "true" REST ideal.

**REST constraints (Fielding's dissertation):** client-server, stateless, cacheable, uniform interface, layered system, code on demand (optional). "Stateless" is critical: no server-side session state; each request is self-contained (token in header).

### GraphQL

#### N+1 Problem

```
# REST — N+1 requests
GET /users            → 10 users
GET /users/1/posts    → posts for user 1
GET /users/2/posts    → posts for user 2
...                   → 11 total requests

# GraphQL — 1 request
POST /graphql
{ users { id name posts { title } } }
→ 1 request, server resolves everything
```

The N+1 problem also exists at the **database layer** inside GraphQL resolvers. Each `user.posts` resolver fires a separate SQL query. Fix with the **DataLoader pattern**: batch all user IDs in the same event loop tick → one `WHERE user_id IN (1,2,3...)` query.

#### Over-fetching and Under-fetching

- **Over-fetching:** REST returns the whole user object (20 fields) when you only need the name. Wastes bandwidth, especially on mobile.
- **Under-fetching:** REST `/users/1` doesn't include posts → need a second request. Causes waterfall latency.

GraphQL solves both: the client specifies exactly the fields needed, and can nest related data in a single query.

#### Caching Challenge

REST GET requests are cacheable by URL — CDNs and browsers cache `/users/1` automatically. GraphQL uses POST requests with dynamic bodies, which HTTP caches ignore. Workarounds:
- **Persisted queries:** hash the query, send `GET /graphql?id=abc123`
- **Apollo Client cache:** client-side normalized cache by type+id
- **CDN:** only for public, cacheable queries

#### When to Use GraphQL

- Mobile clients (minimize data transfer)
- Complex nested data models (social graph, product catalog)
- Multiple client types needing different shapes (web vs mobile)
- Rapid frontend iteration without backend deployments

### gRPC

- Uses **Protocol Buffers** (protobuf) — binary serialization, ~3–10x smaller than JSON
- Strongly typed: `.proto` schema generates client/server stubs in any language
- Built on HTTP/2: multiplexed streams, bidirectional streaming
- **Four modes:** unary, server streaming, client streaming, bidirectional streaming
- **When to use:** internal microservice communication, high-throughput, real-time streaming
- **Downsides:** binary (harder to debug with curl), browser support requires grpc-web proxy

```protobuf
service UserService {
  rpc GetUser (GetUserRequest) returns (User);
  rpc ListUsers (ListRequest) returns (stream User);  // server streaming
}
```

### API Versioning

| Strategy         | Example                               | Pros/Cons                                                   |
|------------------|---------------------------------------|-------------------------------------------------------------|
| URI versioning   | `/v1/users`, `/v2/users`              | Simple, visible, easy to route. Clutters URLs.             |
| Header versioning| `Accept: application/vnd.api+v2+json` | Clean URLs. Hard to test in browser, less discoverable.    |
| Query param      | `/users?version=2`                    | Simple. Version in logs. Not RESTful purists' preference.  |
| No versioning    | Evolve schema, never break            | GraphQL approach: add fields, deprecate old ones.          |

**Semantic versioning for APIs:** Major = breaking change. Minor = additive. Patch = bug fix.
Rule: **never remove or rename fields without a major version bump**.

### Pagination

#### Offset Pagination

```
GET /posts?offset=20&limit=10
SELECT * FROM posts LIMIT 10 OFFSET 20;
```

- Simple to implement
- **Problem:** if a row is inserted/deleted between pages, you get duplicates or skip rows
- **Problem:** `OFFSET 10000` is slow — DB scans and discards 10,000 rows

#### Cursor Pagination

```
GET /posts?after=cursor_abc123&limit=10
SELECT * FROM posts WHERE id > 'cursor_abc123' ORDER BY id LIMIT 10;
```

- Cursor = opaque token encoding the last seen position (often base64-encoded `id` or `created_at`)
- **Stable:** insertions/deletions don't cause duplicates or skips
- **Fast:** index seek instead of full scan
- **Downside:** can't jump to page 5; must walk pages sequentially
- **Use cursor** for real-time feeds, infinite scroll, high-volume tables

### Idempotency Keys

For non-idempotent operations (POST = create), clients can send an `Idempotency-Key: <uuid>` header. The server caches the response for that key (e.g., 24h in Redis). If the client retries due to a network timeout, the server returns the cached response instead of creating a duplicate.

```
POST /payments
Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000
{ "amount": 100, "to": "user_456" }
```

Critical for payment, order creation, and any operation with real-world side effects.

### Rate Limiting Headers

Standard headers (IETF draft):
```
X-RateLimit-Limit: 1000        # requests allowed per window
X-RateLimit-Remaining: 842     # requests left in current window
X-RateLimit-Reset: 1704067200  # Unix timestamp when window resets
Retry-After: 60                # seconds to wait (on 429 response)
```

Common algorithms: token bucket (bursty), fixed window (simple), sliding window log (accurate), leaky bucket (smooth). Token bucket is most common in production (allows bursts up to bucket size).

### Trade-offs

| Approach  | Pros                                              | Cons                                                     |
|-----------|---------------------------------------------------|----------------------------------------------------------|
| REST      | Cacheable, familiar, tooling everywhere           | Over/under-fetching, N+1 risk                           |
| GraphQL   | Precise data fetching, single request             | Caching hard, complexity attacks, resolver N+1          |
| gRPC      | Compact binary, streaming, type-safe              | Binary debugging, browser needs proxy                   |
| Webhooks  | Push-based (no polling), event-driven             | Client must expose endpoint; retry/ordering complexity  |

### Failure Modes

**N+1 query problem in REST:** A client fetches a list of 5 users, then for each user fetches their posts — 1 + 5 = 6 requests where 1 should suffice. At the database layer, this translates to N separate SQL queries per list response. Fix server-side with eager loading (`JOIN` or `IN` query) and expose compound endpoints or GraphQL where clients can request nested data in one shot.

**GraphQL complexity attacks:** A malicious or buggy client can send a deeply nested query — users with posts, each post with comments, each comment with its author, each author with their posts... — that causes exponential database load on a single HTTP request. A single query can DoS your server. Mitigations: query depth limits, query complexity scoring (assign a cost to each field), and rate limiting by complexity score rather than request count.

**API versioning drift:** Maintaining v1, v2, and v3 in parallel means every bug fix must be applied to all active versions, every new feature must decide which versions receive it, and the test surface triples. Teams often underestimate the long-term maintenance burden. Prefer additive changes (new fields, new optional parameters) that don't require version bumps, and set firm deprecation timelines with migration guides.

**Missing idempotency on payment/charge endpoints:** Networks are unreliable. A client sends a charge request, the network times out, the client retries — but the first request succeeded. Without an idempotency key, the customer is charged twice. This is not a theoretical risk: it happens in production under normal network conditions. Every endpoint with financial or irreversible side effects must accept and honor idempotency keys.

---

## Interview Talking Points

- "REST for external/public APIs (familiar, cacheable, browser-native), gRPC for internal service-to-service communication (efficient binary protocol, streaming, compile-time type safety)."
- "Cursor pagination for any real-time feed — offset pagination breaks when data is inserted between pages, causing users to skip or see duplicate items."
- "Every mutating endpoint that clients might retry needs an idempotency key — especially payments. The network is unreliable; retries are normal; duplicates are not acceptable."
- "GraphQL solves over-fetching but makes HTTP caching harder — REST GET responses are cacheable by default by CDNs and browsers. GraphQL requires persisted queries or client-side caching to compensate."
- "URL versioning (/v1/) for external APIs (visible, easy to route, survives copy-pasted URLs), header versioning for internal APIs (cleaner URLs, fine since clients are controlled)."

---

## Hands-on Lab

**Time:** ~20–30 minutes
**Services:** rest-api (Flask), graphql-api (Strawberry/Flask), redis (idempotency store)

### Setup

```bash
docker compose up -d
```

Wait ~30 seconds for pip installs inside containers to complete before running the experiment.

### Experiment

```bash
python experiment.py
```

The experiment runs three phases demonstrating core API design tradeoffs:

1. **N+1 demo (REST)** — fetches 5 users then their posts individually: 6 total HTTP requests, showing the N+1 pattern explicitly with timing
2. **GraphQL comparison** — fetches the same users+posts data in a single GraphQL query: 1 request, demonstrating the reduction in round trips
3. **Over-fetching demo** — compares full REST user object payload vs a GraphQL query selecting only `{id, name}`, showing payload size difference

### Break It

Stop the GraphQL service while the experiment is running to observe graceful degradation:

```bash
docker compose stop graphql-api
```

Re-run `python experiment.py`. Phase 2 (GraphQL) will fail with a connection error while Phase 1 (REST) continues to work, demonstrating why service isolation matters.

### Observe

- Request counts: REST makes 6 requests to retrieve 5 users with posts; GraphQL makes 1
- Payload size: REST over-fetches full user objects; GraphQL returns only requested fields
- Timing: GraphQL single request is not always faster per se — the win is fewer round trips and less client-side waterfall

### Teardown

```bash
docker compose down -v
```

---

## Real-World Examples

- **Facebook:** Invented GraphQL in 2012 to solve the N+1 problem and over-fetching in their mobile news feed, where each screen needed data from multiple REST endpoints and mobile bandwidth was limited. Opened source in 2015. Source: Facebook Engineering blog, "GraphQL: A data query language" (2015)
- **Netflix:** Uses gRPC internally between microservices for streaming data pipelines and cross-language type safety across their polyglot microservice architecture. Source: Netflix Tech Blog
- **Stripe:** Uses idempotency keys for all payment endpoints to make retries safe, storing the result in Redis keyed by the idempotency token for 24 hours. This is now considered industry standard for payment APIs. Source: Stripe API documentation

---

## Common Mistakes

- **Using POST for all operations** — violates REST semantics. GET requests should be idempotent and safe (no side effects), enabling caching by browsers and CDNs. Using POST everywhere disables HTTP-level caching entirely and confuses clients about retry safety.
- **Offset pagination on high-traffic feeds** — items shift when new data arrives between pages. A user paginating through a feed with `offset=20&limit=10` will miss items or see duplicates if new posts are inserted while they page. Use cursor pagination for any feed with real-time writes.
- **No idempotency key on charge/payment endpoints** — results in double charges when clients retry on network timeout. The original request may have succeeded before the timeout; without an idempotency key, the server has no way to detect the duplicate. This is a correctness bug, not just a performance issue.
- **Exposing internal database IDs in URLs** — `/users/12345` leaks your primary key sequence (competitors can enumerate your user count), complicates database migrations (changing ID type or sharding strategy requires URL changes), and creates a coupling between your storage layer and your API contract.
