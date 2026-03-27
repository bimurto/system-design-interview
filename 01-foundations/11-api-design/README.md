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

**REST API Request Lifecycle:**
1. Client constructs the request: HTTP verb (GET / POST / PUT / DELETE) + URL path + headers (`Authorization`, `Content-Type`) + request body (for POST/PUT)
2. Request reaches the API gateway or load balancer, which validates the authentication credential (API key, JWT token, OAuth bearer token)
3. Gateway routes the request to the correct backend service based on the URL path prefix (e.g., `/users/*` → user-service)
4. Service validates the request body against its schema (required fields, types, constraints); returns HTTP 400 with error details if invalid
5. Service executes business logic — database query, cache lookup, downstream service call, etc.
6. Service returns an HTTP response with the appropriate status code (200 OK, 201 Created, 400 Bad Request, 404 Not Found, 500 Internal Server Error) and a JSON response body
7. For GET responses with `Cache-Control: public, max-age=3600`, CDN edge nodes and browsers cache the response — subsequent identical requests never reach the origin

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

| Approach        | Pros                                              | Cons                                                          |
|-----------------|---------------------------------------------------|---------------------------------------------------------------|
| REST            | Cacheable, familiar, tooling everywhere           | Over/under-fetching, N+1 risk, rigid field sets              |
| GraphQL         | Precise data fetching, single HTTP request        | Caching hard, complexity attacks, DB-layer resolver N+1      |
| gRPC            | Compact binary, streaming, type-safe              | Binary debugging, browser needs grpc-web proxy               |
| Webhooks        | Push-based (no polling), event-driven             | Client must expose endpoint; retry/ordering/fan-out complexity|
| WebSockets/SSE  | Real-time bidirectional / server push             | Stateful connections, hard to scale horizontally, load balancer config |

**Choosing between WebSockets and SSE:** SSE is unidirectional (server → client), uses standard HTTP, and reconnects automatically — ideal for live feeds, notifications, dashboards. WebSockets are bidirectional and lower overhead per message — ideal for chat, collaborative editing, gaming. SSE is simpler to deploy (no protocol upgrade, no sticky-session requirement when using event IDs for reconnect).

### Failure Modes

**N+1 query problem (HTTP layer — REST):** A client fetches a list of 5 users, then for each user fetches their posts — 1 + 5 = 6 requests where 1 should suffice. Fix server-side with eager loading (`GET /users?include=posts` or a GraphQL query) and expose compound endpoints where clients can request nested data in one shot.

**N+1 query problem (DB layer — GraphQL):** GraphQL solves HTTP-layer N+1 but introduces it at the resolver layer. `users { posts { ... } }` triggers N separate SQL `SELECT * FROM posts WHERE user_id = ?` calls — one per user. Fix with the **DataLoader pattern**: batch all user IDs collected in the same event-loop tick into one `WHERE user_id IN (1,2,3...)` query, then fan results back to each waiting resolver. Without DataLoader, a GraphQL server under load will exhaust DB connection pools.

**GraphQL complexity attacks:** A malicious or buggy client sends a deeply nested query — `users { posts { comments { author { posts { comments { ... } } } } } }` — that causes exponential resolver fan-out on a single HTTP request. One query can DoS the DB. Mitigations (apply all three in production):
- **Query depth limit:** reject queries deeper than N levels (e.g., `max_depth=5`)
- **Query complexity scoring:** assign a cost to each field; reject queries exceeding a budget (e.g., each list field costs 10, each scalar costs 1)
- **Persisted queries only:** in production, only allow pre-registered queries by hash; reject ad-hoc query strings entirely (eliminates the attack surface)

**API versioning drift:** Maintaining v1, v2, and v3 in parallel means every bug fix must be applied to all active versions, every new feature must decide which versions receive it, and the test surface triples. Teams often underestimate the long-term maintenance burden. Prefer additive changes (new fields, new optional parameters) that don't require version bumps, and set firm deprecation timelines with migration guides.

**Missing idempotency on payment/charge endpoints:** Networks are unreliable. A client sends a charge request, the network times out, the client retries — but the first request succeeded. Without an idempotency key, the customer is charged twice. This is not a theoretical risk: it happens in production under normal network conditions. Every endpoint with financial or irreversible side effects must accept and honor idempotency keys.

**Webhook delivery failures:** Webhooks invert the connection direction — your server POSTs to the client's endpoint. Failure modes: (1) client endpoint is down → event lost unless you implement a retry queue with exponential backoff; (2) events arrive out of order (network re-ordering, parallel retries) → client must handle idempotent event processing and sequence numbers; (3) fan-out at scale — 1M subscribers for one event means 1M outbound HTTP requests in a burst, requiring a dedicated async delivery queue (Kafka/SQS) and per-subscriber rate limiting. Always include an event ID and timestamp; clients should deduplicate on the ID.

---

## Interview Talking Points

- "REST for external/public APIs (familiar, cacheable, browser-native), gRPC for internal service-to-service communication (efficient binary protocol, streaming, compile-time type safety). GraphQL for mobile or multi-client scenarios where different clients need different data shapes without backend changes."
- "GraphQL solves HTTP-layer N+1 but introduces resolver-layer N+1 at the DB. You must pair GraphQL with DataLoader: it batches all concurrent resolver calls in the same event-loop tick into one `WHERE id IN (...)` query. Without it, a 100-user query fires 100 separate DB queries."
- "Cursor pagination for any real-time feed — offset pagination breaks when data is inserted between pages, causing users to skip or see duplicate items. Cursor = opaque token encoding the last-seen position; the client can't jump to page 5, but feeds don't need to."
- "Every mutating endpoint that clients might retry needs an idempotency key — especially payments. Cache the response in Redis for 24h keyed by the idempotency UUID. If the same key arrives again, return the cached result; skip processing. This turns unreliable networks into a correctness guarantee."
- "GraphQL solves over-fetching but makes HTTP caching harder — REST GET responses are cacheable by default by CDNs and browsers. GraphQL uses POST with dynamic bodies; fix with persisted queries (hash → GET /graphql?queryId=abc) to make responses CDN-cacheable."
- "URL versioning (/v1/) for external APIs (visible, easy to route, survives copy-pasted URLs), header versioning for internal APIs (cleaner URLs, fine since clients are controlled). In both cases: never remove fields without a major version bump; prefer additive changes."
- "GraphQL in production requires query depth limits AND complexity scoring — a single deeply nested query can DoS the DB. At Facebook/GitHub scale, only persisted queries are allowed in production; ad-hoc query strings are rejected entirely, eliminating the attack surface."

---

## Hands-on Lab

**Time:** ~25–35 minutes
**Services:** rest-api (Flask + Redis), graphql-api (Strawberry/Flask), redis (idempotency store)

### Setup

```bash
docker compose up -d
```

Wait ~30 seconds for pip installs inside containers to complete before running the experiment.

### Experiment

```bash
python experiment.py
```

The experiment runs seven phases demonstrating core API design tradeoffs:

1. **N+1 demo (REST)** — fetches 5 users then their posts individually: 6 total HTTP requests, showing the N+1 pattern with explicit timing
2. **GraphQL single request** — same data in 1 GraphQL query; also surfaces the server-side resolver call count
3. **GraphQL resolver N+1** — explains that GraphQL solves HTTP-layer N+1 but the DB layer still fires N resolver calls without DataLoader
4. **Over-fetching** — compares full REST user object payload vs a GraphQL query selecting only `{name}`, showing payload size and bandwidth difference
5. **Idempotency keys** — sends a payment request twice with the same `Idempotency-Key` (safe retry), then twice without (double-charge)
6. **REST eager-loading** — `GET /users?include=posts` solves N+1 server-side in 1 HTTP request; compares tradeoffs with GraphQL
7. **Comparison table** — REST vs GraphQL across caching, versioning, error handling, complexity attacks, real-time, and use-case selection

### Break It

Stop the GraphQL service while the experiment is running to observe graceful degradation:

```bash
docker compose stop graphql-api
```

Re-run `python experiment.py`. Phases 2–4 (GraphQL) will fail with a connection error while Phase 1 (REST) continues to work, demonstrating why service isolation and graceful fallback matter.

### Observe

- **Request counts:** REST N+1 makes 6 requests; GraphQL makes 1; REST eager-loading also makes 1
- **Resolver calls:** Phase 3 shows GraphQL fires N `posts` resolver calls server-side — the DB-layer N+1
- **Idempotency:** Phase 5 shows same `payment_id` returned on retry (safe), vs two different `payment_id` values without a key (double-charge)
- **Payload size:** REST over-fetches; GraphQL returns only requested fields — visible in MB/s calculation at scale

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
