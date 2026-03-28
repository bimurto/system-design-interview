# Proxies & Reverse Proxies

**Prerequisites:** `../11-api-design/`
**Next:** `../14-failure-modes-reliability/`

---

## Concept

A **proxy** is a server that sits between a client and a backend and forwards requests on the client's behalf. The
distinction between a forward proxy and a reverse proxy comes down to whose behalf the proxy acts.

A **forward proxy** sits in front of clients. The client explicitly sends requests to the proxy, which forwards them to
the internet. The backend server sees the proxy's IP, not the client's. Common uses: corporate network filtering,
anonymization (VPNs), caching outbound traffic, bypassing geo-restrictions.

A **reverse proxy** sits in front of servers. The client sends a request to the proxy, which forwards it to one or more
backend servers. The client sees only the proxy's IP; backend servers are hidden. Common uses: load balancing, SSL
termination, caching, compression, routing, request authentication.

The terms matter in interviews because they clarify where in the architecture a component lives and whose identity is
hidden. When someone says "put nginx in front of your API servers," they mean a reverse proxy.

### Why Reverse Proxies Are Everywhere

Every large-scale system uses at least one reverse proxy layer. They concentrate cross-cutting concerns — TLS
termination, rate limiting, authentication, compression, logging, request routing — into a single place so backend
services don't have to implement them individually. This is the **single responsibility principle applied at the
infrastructure level**: backend services handle business logic; the reverse proxy handles traffic management.

**SSL/TLS termination:** HTTPS is terminated at the proxy. Traffic between proxy and backends is plain HTTP on a private
network. This avoids the CPU cost of TLS encryption on every backend server and simplifies certificate management — one
cert at the proxy instead of one per backend. The trade-off is that internal traffic is unencrypted; in regulated
environments (HIPAA, PCI-DSS) you may need end-to-end encryption even on private links (SSL passthrough or re-encryption
mode).

**Load balancing:** the reverse proxy distributes requests across backend instances. Common algorithms:

- **Round-robin:** requests cycle through backends in sequence. Stateless and simple; works well when requests are
  uniform in cost.
- **Least-connections:** route to the backend with fewest active connections. Better when request duration varies (mix
  of fast reads and slow writes).
- **IP hash / sticky sessions:** the same client IP always routes to the same backend. Required when session state lives
  in-process on the backend rather than in a shared store. Avoid if you can — it complicates rolling deploys and causes
  uneven load if a single client is a heavy hitter.
- **Weighted round-robin:** send a larger share of traffic to higher-capacity backends. Useful during canary
  deployments (send 5% to the new version).

**Request routing:** a single reverse proxy can route `api.example.com/v1/users` to the user service, `/v1/orders` to
the order service, and `/static/` to an S3 bucket. This allows an entire microservice architecture to present a single
external hostname.

**Caching:** the proxy can cache backend responses and serve them directly on repeat requests, reducing backend load.
nginx's `proxy_cache`, Varnish, and Cloudflare all implement this. Cache invalidation at the proxy layer is complex —
the proxy must respect `Cache-Control` headers and have a way to purge stale entries when data changes.

**Compression:** gzip or brotli compression can be applied at the proxy so backends return uncompressed responses (
faster to generate) and clients receive compressed ones (faster to transfer).

**Connection pooling:** backends must handle fewer raw TCP connections because the proxy maintains persistent keepalive
connections to backends and multiplexes many client connections over them. In nginx, this requires setting
`proxy_http_version 1.1`, `proxy_set_header Connection ""`, and `keepalive N` in the upstream block. Without this, nginx
degrades to HTTP/1.0 and opens a new TCP connection per request.

### HTTP/2 and HTTP/3 at the Proxy

Modern reverse proxies terminate HTTP/2 (and HTTP/3/QUIC) at the edge and speak HTTP/1.1 to backends. This gives all the
benefits of HTTP/2 multiplexing to clients without requiring backends to implement it. nginx supports `listen 443 http2`
for client-facing H2 and `proxy_http_version 1.1` for the backend leg. Envoy and Caddy can speak H2 on both legs.

HTTP/3 runs over QUIC (UDP), which eliminates head-of-line blocking at the transport layer. At the time of writing,
nginx supports H3 in experimental builds; Cloudflare and AWS CloudFront terminate H3 in production. Worth mentioning in
an interview when discussing latency at the edge.

### API Gateway vs Reverse Proxy

An API gateway is a reverse proxy with added application-layer awareness: request authentication (JWT/OAuth validation),
per-client rate limiting, request transformation (add/strip headers, rewrite paths), API versioning, and usage metering.
AWS API Gateway, Kong, and Apigee are examples. The line between "reverse proxy" and "API gateway" is fuzzy — Kong is
nginx with plugins; nginx with Lua scripts is close to an API gateway.

In interviews, reach for **reverse proxy** when discussing infrastructure-level traffic management (SSL, load balancing,
caching) and **API gateway** when discussing per-client application concerns (auth, rate limits, routing to
microservices).

### Service Mesh

In microservice environments with dozens of services making calls to each other, a service mesh adds a sidecar proxy (
typically Envoy) alongside every service instance. All network traffic in and out of a service passes through its
sidecar. This gives per-service observability (metrics, tracing), mTLS between services, and circuit breaking — without
modifying application code. Istio and Linkerd are service mesh implementations.

Service mesh is the reverse proxy pattern applied to east-west (service-to-service) traffic, just as a traditional
reverse proxy handles north-south (client-to-service) traffic.

## How It Works

**nginx as a reverse proxy:**

```
client → nginx (port 443, TLS termination) → app server pool (port 8080, HTTP)
```

nginx accepts HTTPS connections, terminates TLS, and proxies plain HTTP to a pool of backend servers. The `upstream`
block names the backend pool; `proxy_pass` in the `location` block routes matching requests to it.

**Request lifecycle through a reverse proxy:**

1. Client connects to the proxy (DNS resolves the service hostname to the proxy IP)
2. Proxy terminates TLS, reads the HTTP request
3. Proxy applies rules: authentication check, rate limit check, route matching
4. Proxy selects a backend via the load balancing algorithm
5. Proxy opens or reuses a keepalive connection to the backend
6. Proxy forwards the request, enriching headers:
    - `X-Real-IP: <client-ip>` — set from the TCP socket, cannot be forged
    - `X-Forwarded-For: <client-ip>` — **overwrite** with `$remote_addr` (not append) to prevent spoofing
    - `Host: <original-host>` — so the backend knows the requested hostname
7. Backend responds; proxy may cache the response, compress it, then forward to the client

**Critical nginx config for connection pooling to backends:**

```nginx
upstream backends {
    server backend1:8081 max_fails=3 fail_timeout=30s;
    server backend2:8082 max_fails=3 fail_timeout=30s;
    keepalive 32;                      # keep up to 32 idle connections per worker
}

location / {
    proxy_pass         http://backends;
    proxy_http_version 1.1;            # required for keepalive
    proxy_set_header   Connection "";  # clear "close" from HTTP/1.0 clients
}
```

Without `keepalive`, nginx defaults to HTTP/1.0 on the backend leg and closes the connection after every request —
burning a TCP handshake per request.

### Trade-offs

| Concern           | Forward Proxy                       | Reverse Proxy                   |
|-------------------|-------------------------------------|---------------------------------|
| Acts on behalf of | Clients                             | Servers                         |
| Hides identity of | Clients                             | Servers                         |
| Typical use       | Outbound filtering, VPN             | Load balancing, SSL termination |
| Client awareness  | Client must be configured to use it | Client is unaware               |

| Feature                   | Round-Robin             | Least-Connections       | IP Hash                          |
|---------------------------|-------------------------|-------------------------|----------------------------------|
| Distribution              | Equal                   | Load-weighted           | Client-affinity                  |
| Session state             | Requires external store | Requires external store | Works in-process                 |
| Rolling deploy complexity | Simple                  | Simple                  | Tricky (clients follow backend)  |
| Hot-spot risk             | Low                     | Low                     | High (one client = heavy hitter) |

### Failure Modes

**Proxy becomes a single point of failure:** if a single nginx instance handles all traffic and it goes down, the
service is unavailable. Solution: run two proxy instances in active-passive (with keepalived/VIP failover) or
active-active behind a DNS round-robin or cloud load balancer.

**Proxy becomes a throughput bottleneck:** all traffic passes through the proxy. At very high throughput (millions of
RPS), even a lightweight proxy can saturate CPU or network. Solution: horizontal scaling of the proxy tier; use
connection multiplexing (HTTP/2 to backends) to reduce connection overhead; increase `worker_processes` to match CPU
core count.

**Header injection via X-Forwarded-For:** because the proxy adds `X-Forwarded-For`, a malicious client can send
`X-Forwarded-For: 1.2.3.4` to spoof their IP. If nginx uses `$proxy_add_x_forwarded_for` (append mode), the backend sees
`1.2.3.4, <real-client-ip>` and a backend trusting the first entry is deceived. Solution: the proxy must **overwrite**
the header with `$remote_addr` so only the proxy's observed client IP reaches the backend.

**Proxy timeout misconfiguration:** forgetting to set `proxy_connect_timeout`, `proxy_read_timeout`, and
`proxy_send_timeout` means a hung backend holds the client connection open indefinitely. Under load this exhausts
nginx's worker connections. Set timeouts explicitly and calibrate them to the SLA — a 30 s read timeout on a chat API is
wrong; 5 s may be right.

**SSL certificate expiry:** if the proxy's TLS certificate expires, all HTTPS traffic fails. Solution: automate
renewal (Let's Encrypt + certbot/cert-manager). Monitor expiry date as a metric; alert at 30 days.

**Retrying non-idempotent requests:** nginx's `proxy_next_upstream` can retry a failed request on the next backend. If
the failed request was a POST or PATCH that already wrote to the database, the retry causes a duplicate write. Solution:
restrict `proxy_next_upstream` to `error timeout` and exclude `http_500`; or disable retries on mutation endpoints
entirely.

**Sticky sessions with IP hash during rolling deploy:** if a backend is removed from rotation, all clients hashed to it
are re-distributed. Stateful sessions break. Solution: migrate session state to Redis before deploying; use consistent
hashing if you need affinity.

## Interview Talking Points

- "A reverse proxy sits in front of backends — clients see it as the server. It centralizes SSL termination, load
  balancing, caching, and routing so backends don't need to implement them"
- "SSL termination at the proxy means one cert, one TLS handshake per client — backends get plain HTTP on the private
  network. The trade-off is internal traffic is unencrypted; PCI-DSS or HIPAA may require re-encryption"
- "For connection pooling to work, nginx needs `proxy_http_version 1.1` and `proxy_set_header Connection ''` plus
  `keepalive N` in the upstream block — without this it's HTTP/1.0 with a TCP handshake per request"
- "The proxy must overwrite X-Forwarded-For with `$remote_addr`, not append to it — appending lets clients spoof their
  IP. X-Real-IP is safe because it's always set from the TCP socket"
- "For rate limiting and access control, use X-Real-IP or the *last* proxy-appended XFF entry — never the first, which
  the client controls"
- "Round-robin works when requests are uniform; least-connections is better with mixed workloads; IP hash gives session
  affinity but creates hot spots and complicates deploys"
- "An API gateway is a reverse proxy with application-layer features: JWT validation, per-client rate limiting, request
  transformation. Kong is nginx with plugins; the boundary is fuzzy"
- "Service mesh extends the reverse proxy pattern to east-west traffic via sidecars — same mTLS and observability
  policies without touching app code"
- "The proxy tier itself must be HA — run at least two instances with VIP failover. A single proxy in front of a
  10-instance backend cluster is still a SPOF"
- "proxy_next_upstream enables automatic retry on backend failure, but it must be disabled or scoped carefully for
  non-idempotent endpoints to avoid duplicate writes"

## Hands-on Lab

**Time:** ~20 minutes
**Services:** nginx (8080), two Python backends (8081, 8082)

### Setup

```bash
cd system-design-interview/01-foundations/13-proxies-reverse-proxies/
docker compose up -d
# docker compose uses health checks — nginx starts only after both backends pass
```

### Experiment

```bash
python experiment.py
```

The script demonstrates six concepts in sequence:

1. **Round-robin load balancing** — 10 requests alternate between backend-1 and backend-2
2. **Header forwarding** — backends receive X-Real-IP and X-Forwarded-For set by nginx
3. **Header spoofing defense** — nginx overwrites XFF with `$remote_addr`; the spoofed value is discarded
4. **Path-based routing** — `/api/users/*` always hits backend-1; `/api/orders/*` always hits backend-2
5. **Backend transparency** — direct-hitting a backend port shows empty proxy headers (all security layers bypassed)
6. **Passive failover** — conceptual walkthrough of nginx's `max_fails` / `fail_timeout` behavior

### Observe

- Phase 1: requests alternate cleanly between the two backends
- Phase 3: even when the client sends `X-Forwarded-For: 1.2.3.4`, the backend sees the real IP — the spoofed value is
  gone
- Phase 4: URL prefix controls which backend pool is selected — this is how API gateway routing works
- Phase 5: the direct request arrives at the backend with no X-Real-IP header — proxy security is skipped

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **nginx at Dropbox:** Dropbox runs nginx as the edge reverse proxy handling TLS termination and routing for all user
  traffic. They published details of managing 50,000+ nginx configurations at scale using templating — source: Dropbox
  Tech Blog, "How We Scaled nginx at Dropbox" (2017).
- **Envoy at Lyft:** Lyft developed Envoy as their service mesh sidecar proxy and open-sourced it. Every service runs an
  Envoy sidecar; all cross-service calls go through it. This gave them automatic retries, circuit breaking, and
  distributed tracing across 200+ services without application-level code — source: Matt Klein, "Introducing Lyft's
  Envoy — Open Source Edge and Service Proxy for Cloud-Native Applications" (2016).
- **AWS ALB:** Amazon's Application Load Balancer is a managed reverse proxy that terminates TLS, routes by URL path or
  host header, and integrates with AWS WAF for request filtering. It handles millions of requests per second with no
  single point of failure — source: AWS Documentation, "How AWS Application Load Balancer Works."
- **Cloudflare as global reverse proxy:** Cloudflare operates 300+ PoPs, each running reverse proxies that terminate
  HTTP/3 (QUIC) from clients and speak HTTP/2 or HTTP/1.1 to origin servers. Their anycast network means DNS resolves to
  the nearest PoP — source: Cloudflare Blog, "QUIC is now RFC 9000" (2021).

## Common Mistakes

- **Forgetting the proxy is a SPOF.** A single reverse proxy in front of a highly available backend cluster creates a
  new single point of failure. Always deploy the proxy tier with redundancy.
- **Trusting `X-Forwarded-For` without validation.** Any client can set this header. The proxy must overwrite it with
  `$remote_addr`; backends must use X-Real-IP or the last proxy-appended XFF value for access control.
- **Applying SSL termination but not encrypting the internal leg.** In regulated industries (HIPAA, PCI-DSS) traffic
  must be encrypted end-to-end, even on internal networks. Know when passthrough SSL (proxy doesn't decrypt) is required
  vs termination.
- **Not configuring keepalive to backends.** Omitting `keepalive`, `proxy_http_version 1.1`, and
  `proxy_set_header Connection ""` means nginx opens a new TCP connection per request — a hidden performance cliff at
  high RPS.
- **Retrying non-idempotent requests.** `proxy_next_upstream` can silently duplicate POST or PATCH requests when a
  backend fails mid-response. Scope it to safe failure modes only.
- **Over-centralizing in the proxy.** Business logic does not belong in nginx Lua scripts or API gateway transforms. The
  proxy should handle infrastructure concerns; application logic stays in the service.
- **Using IP hash with scaling events.** Adding or removing a backend changes the hash ring and redistributes all
  sessions. If the backend holds session state, users are logged out. Move session state to an external store (Redis)
  before enabling IP hash.
