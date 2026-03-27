# Proxies & Reverse Proxies

**Prerequisites:** `../11-api-design/`
**Next:** `../14-failure-modes-reliability/`

---

## Concept

A **proxy** is a server that sits between a client and a backend and forwards requests on the client's behalf. The distinction between a forward proxy and a reverse proxy comes down to whose behalf the proxy acts.

A **forward proxy** sits in front of clients. The client explicitly sends requests to the proxy, which forwards them to the internet. The backend server sees the proxy's IP, not the client's. Common uses: corporate network filtering, anonymization (VPNs), caching outbound traffic, bypassing geo-restrictions.

A **reverse proxy** sits in front of servers. The client sends a request to the proxy, which forwards it to one or more backend servers. The client sees only the proxy's IP; backend servers are hidden. Common uses: load balancing, SSL termination, caching, compression, routing, request authentication.

The terms matter in interviews because they clarify where in the architecture a component lives and whose identity is hidden. When someone says "put nginx in front of your API servers," they mean a reverse proxy.

### Why Reverse Proxies Are Everywhere

Every large-scale system uses at least one reverse proxy layer. They concentrate cross-cutting concerns — TLS termination, rate limiting, authentication, compression, logging, request routing — into a single place so backend services don't have to implement them individually. This is the **single responsibility principle applied at the infrastructure level**: backend services handle business logic; the reverse proxy handles traffic management.

**SSL/TLS termination:** HTTPS is terminated at the proxy. Traffic between proxy and backends is plain HTTP on a private network. This avoids the CPU cost of TLS encryption on every backend server and simplifies certificate management — one cert at the proxy instead of one per backend.

**Load balancing:** the reverse proxy distributes requests across backend instances using the same algorithms covered in the load balancing topic (round-robin, least-connections, IP hash). The distinction from a dedicated load balancer is mostly one of implementation — nginx, HAProxy, and Envoy can all do both.

**Request routing:** a single reverse proxy can route `api.example.com/v1/users` to the user service, `/v1/orders` to the order service, and `/static/` to an S3 bucket. This allows an entire microservice architecture to present a single external hostname.

**Caching:** the proxy can cache backend responses and serve them directly on repeat requests, reducing backend load. nginx's proxy_cache, Varnish, and Cloudflare all implement this.

**Compression:** gzip or brotli compression can be applied at the proxy so backends return uncompressed responses (faster to generate) and clients receive compressed ones (faster to transfer).

**Connection pooling:** backends must handle fewer raw TCP connections because the proxy maintains persistent connections to backends and multiplexes many client connections over them.

### API Gateway vs Reverse Proxy

An API gateway is a reverse proxy with added application-layer awareness: request authentication (JWT/OAuth validation), per-client rate limiting, request transformation (add/strip headers, rewrite paths), API versioning, and usage metering. AWS API Gateway, Kong, and Apigee are examples. The line between "reverse proxy" and "API gateway" is fuzzy — Kong is nginx with plugins; nginx with Lua scripts is close to an API gateway.

In interviews, reach for **reverse proxy** when discussing infrastructure-level traffic management (SSL, load balancing, caching) and **API gateway** when discussing per-client application concerns (auth, rate limits, routing to microservices).

### Service Mesh

In microservice environments with dozens of services making calls to each other, a service mesh adds a sidecar proxy (typically Envoy) alongside every service instance. All network traffic in and out of a service passes through its sidecar. This gives per-service observability (metrics, tracing), mTLS between services, and circuit breaking — without modifying application code. Istio and Linkerd are service mesh implementations.

Service mesh is the reverse proxy pattern applied to east-west (service-to-service) traffic, just as a traditional reverse proxy handles north-south (client-to-service) traffic.

## How It Works

**nginx as a reverse proxy:**

```
client → nginx (port 443, TLS termination) → app server pool (port 8080, HTTP)
```

nginx accepts HTTPS connections, terminates TLS, and proxies plain HTTP to a pool of backend servers. The `upstream` block names the backend pool; `proxy_pass` in the `location` block routes matching requests to it.

**Request lifecycle through a reverse proxy:**

1. Client connects to the proxy (DNS resolves the service hostname to the proxy IP)
2. Proxy terminates TLS, reads the HTTP request
3. Proxy applies rules: authentication check, rate limit check, route matching
4. Proxy selects a backend via the load balancing algorithm
5. Proxy opens or reuses a pooled connection to the backend
6. Proxy forwards the request (adding `X-Forwarded-For`, `X-Real-IP`, `Host` headers so the backend knows the original client)
7. Backend responds; proxy may cache the response, compress it, then forward to the client

### Trade-offs

| Concern | Forward Proxy | Reverse Proxy |
|---------|--------------|---------------|
| Acts on behalf of | Clients | Servers |
| Hides identity of | Clients | Servers |
| Typical use | Outbound filtering, VPN | Load balancing, SSL termination |
| Client awareness | Client must be configured to use it | Client is unaware |

### Failure Modes

**Proxy becomes a single point of failure:** if a single nginx instance handles all traffic and it goes down, the service is unavailable. Solution: run two proxy instances in active-passive (with keepalived/VIP failover) or active-active behind a DNS round-robin or cloud load balancer.

**Proxy becomes a throughput bottleneck:** all traffic passes through the proxy. At very high throughput (millions of RPS), even a lightweight proxy can saturate CPU or network. Solution: horizontal scaling of the proxy tier; use connection multiplexing (HTTP/2 to backends) to reduce connection overhead.

**Header injection via X-Forwarded-For:** because the proxy adds `X-Forwarded-For: <client-ip>`, a malicious client can send `X-Forwarded-For: 1.2.3.4` to spoof their IP. Backends that trust this header for access control or rate limiting are vulnerable. Solution: the proxy must overwrite or strip the header rather than append to it, so only the proxy's view of the client IP is trusted.

**SSL certificate expiry:** if the proxy's TLS certificate expires, all HTTPS traffic fails. Solution: automate renewal (Let's Encrypt + certbot/cert-manager).

## Interview Talking Points

- "A reverse proxy sits in front of backends — clients see it as the server. It centralizes SSL termination, load balancing, caching, and routing so backends don't need to implement them"
- "SSL termination at the proxy means one cert, one TLS handshake per client — backends get plain HTTP on the private network"
- "The proxy adds `X-Forwarded-For` so backends can see the real client IP — but backends must trust only the rightmost proxy-appended value to prevent spoofing"
- "An API gateway is a reverse proxy with application-layer features: JWT validation, per-client rate limiting, request transformation"
- "Service mesh extends the reverse proxy pattern to east-west service-to-service traffic via sidecars — same observability and policy enforcement without touching app code"
- "The proxy tier itself must be highly available — run at least two instances with VIP failover or sit them behind a cloud load balancer"

## Hands-on Lab

**Time:** ~20 minutes
**Services:** nginx (80), two Flask backends (8081, 8082)

### Setup

```bash
cd system-design-interview/01-foundations/13-proxies-reverse-proxies/
docker compose up -d
# Wait ~5 seconds for services to be ready
```

### Experiment

```bash
python experiment.py
```

The script sends 50 requests to nginx (the reverse proxy) and shows which backend instance handled each request, demonstrating round-robin load balancing. It then shows the `X-Forwarded-For` and `X-Real-IP` headers the backends receive, and demonstrates that changing the upstream pool (simulated by stopping one backend) causes nginx to route all traffic to the remaining instance.

### Observe

Watch the backend distribution in the output — requests alternate between backend-1 and backend-2. Note the headers received by the backend; the original client IP appears in `X-Real-IP` even though the connection came from nginx.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **nginx at Dropbox:** Dropbox runs nginx as the edge reverse proxy handling TLS termination and routing for all user traffic. They published details of managing 50,000+ nginx configurations at scale using templating — source: Dropbox Tech Blog, "How We Scaled nginx at Dropbox" (2017).
- **Envoy at Lyft:** Lyft developed Envoy as their service mesh sidecar proxy and open-sourced it. Every service runs an Envoy sidecar; all cross-service calls go through it. This gave them automatic retries, circuit breaking, and distributed tracing across 200+ services without application-level code — source: Matt Klein, "Introducing Lyft's Envoy — Open Source Edge and Service Proxy for Cloud-Native Applications" (2016).
- **AWS ALB:** Amazon's Application Load Balancer is a managed reverse proxy that terminates TLS, routes by URL path or host header, and integrates with AWS WAF for request filtering. It handles millions of requests per second with no single point of failure — source: AWS Documentation, "How AWS Application Load Balancer Works."

## Common Mistakes

- **Forgetting the proxy is a SPOF.** A single reverse proxy in front of a highly available backend cluster creates a new single point of failure. Always deploy the proxy tier with redundancy.
- **Trusting `X-Forwarded-For` without validation.** Any client can set this header. The proxy must overwrite it; backends must only trust it when it comes from a known proxy IP.
- **Applying SSL termination but not encrypting the internal leg.** In regulated industries (HIPAA, PCI-DSS) traffic must be encrypted end-to-end, even on internal networks. Know when passthrough SSL (proxy doesn't decrypt) is required vs termination.
- **Over-centralizing in the proxy.** Business logic does not belong in nginx Lua scripts or API gateway transforms. The proxy should handle infrastructure concerns; application logic stays in the service.
