# Load Balancing

**Prerequisites:** `../06-caching/`
**Next:** `../08-databases-sql-vs-nosql/`

---

## Concept

A load balancer is a component that distributes incoming traffic across a pool of backend servers. Without it, all traffic hits a single server, creating a capacity ceiling and a single point of failure. With it, you can add more backends horizontally, route around unhealthy nodes, and serve traffic during rolling deployments — all transparent to the client.

Load balancers operate at different layers of the OSI model. **L4 (transport layer)** load balancers operate on TCP/UDP packets without inspecting the payload. They make routing decisions based on IP address and port, then forward packets directly. This is extremely fast — the load balancer acts as a pass-through with almost zero latency overhead. AWS Network Load Balancer and hardware appliances like F5 BIG-IP operate at L4. The limitation is blindness to application semantics: an L4 load balancer cannot route `/api/*` requests to one cluster and `/static/*` to another because it never sees the HTTP path.

**L7 (application layer)** load balancers terminate the client connection, inspect the full HTTP/HTTPS request, and make intelligent routing decisions based on URL path, headers, cookies, and even request body content. This enables content-based routing, SSL/TLS termination, HTTP compression, WebSocket upgrades, and cookie-based session affinity. Nginx, HAProxy, and AWS Application Load Balancer operate at L7. The trade-off is higher latency (must fully parse the request) and CPU cost, but modern hardware makes this overhead negligible for most workloads.

**SSL/TLS termination** is a critical L7 function: the load balancer handles the expensive TLS handshake and certificate management, then forwards unencrypted HTTP to backends on a private network. This offloads CPU from backends, centralizes certificate rotation, and enables the load balancer to inspect request content. The private network between load balancer and backends must be trusted — typically an internal VPC with no public access.

**Global load balancing** operates above individual data centers. DNS-based global load balancing returns different IP addresses based on the client's geographic location, routing users to the nearest regional cluster. Anycast assigns the same IP address to servers in multiple locations and routes packets to the nearest one via BGP routing. Anycast is how CDNs and DDoS mitigation services (Cloudflare, Fastly) route traffic — a single IP resolves to hundreds of PoPs worldwide.

## How It Works

**End-to-End Request Flow Through an L7 Load Balancer:**
1. Client initiates a TCP connection to the load balancer's public IP; TLS handshake occurs at the load balancer (TLS termination)
2. Load balancer decrypts the request and parses the full HTTP request — URL path, headers, cookies, body
3. Scheduling algorithm selects a healthy backend from the pool (round-robin, least-connections, IP hash, etc.)
4. Load balancer forwards the request to the selected backend over plain HTTP on the internal private network
5. Backend processes the request and returns the HTTP response to the load balancer
6. Load balancer encrypts the response and forwards it to the client over the original TLS connection
7. If a backend fails its health check, it is removed from the pool; all new requests route to the remaining healthy backends

When a request arrives at a load balancer, the balancer selects a backend using a **scheduling algorithm**:

**Round-robin** cycles through backends in order. Backend list: [A, B, C, D]. Request 1 → A, 2 → B, 3 → C, 4 → D, 5 → A. Equal distribution regardless of backend capacity or current load. Simple, stateless, and effective when all backends are identical and request durations are similar.

**Weighted round-robin** assigns a weight to each backend proportional to its capacity. If A has weight=3 and B has weight=1, A receives 75% of traffic. Useful when backends have different hardware specs, or when one backend is in "canary" deployment receiving 10% of traffic for testing.

**Least connections** routes each new request to the backend with the fewest currently active connections. A backend that is slow (long response times) accumulates connections; a fast backend handles requests quickly and has fewer active connections, receiving more new requests. This naturally adapts to variable backend latency — critical for workloads like file uploads, long-polling, or any endpoint with high p99 latency.

**IP hash** computes a hash of the client's source IP and maps it to a backend consistently. The same client always routes to the same backend. Useful for legacy stateful applications but fundamentally at odds with stateless design. If the sticky backend fails, that client loses their session.

**Random** picks a backend uniformly at random. Statistically approaches round-robin at scale. Some implementations use "power of two random choices" — pick two random backends and send to whichever has fewer connections. This provides most of least-conn's benefit with minimal tracking overhead.

**Health checks** verify that backends are alive and able to serve traffic before routing to them. Active health checks: the load balancer periodically sends probe requests (HTTP GET /health, TCP connect, or a custom application-level ping) and marks backends up or down based on the response. Passive health checks: the load balancer monitors actual request responses and removes backends that return repeated errors or timeouts. A backend returning HTTP 500 five times in 30 seconds gets removed from the pool without waiting for an active probe cycle.

### Trade-offs

| Approach | Pros | Cons |
|----------|------|------|
| Round-robin | Simple, zero state | Ignores backend speed/load differences |
| Weighted round-robin | Accounts for capacity differences | Weights must be tuned manually |
| Least connections | Adapts to variable latency automatically | Requires connection tracking state |
| IP hash | Sticky sessions without app changes | Uneven distribution, session loss on failover |
| L4 load balancing | Extremely fast, low overhead | No application-level routing |
| L7 load balancing | Content routing, TLS termination, cookies | Higher CPU/latency cost |
| Hardware LB (F5) | Line-rate throughput, dedicated hardware | Expensive, inflexible, vendor lock-in |
| Software LB (Nginx/HAProxy) | Flexible, cheap, runs anywhere | Becomes SPOF if not HA-deployed |
| Cloud LB (ALB/NLB) | Managed, auto-scales, integrated with cloud | Cloud vendor lock-in, cost at scale |

### Failure Modes

**Load balancer as single point of failure:** adding multiple backends while running a single load balancer just moves the SPOF. Production deployments run load balancers in active-passive or active-active pairs with a shared VIP (virtual IP). If the primary fails, the VIP migrates to the secondary via VRRP/keepalived within seconds.

**Health checks lagging behind reality:** a health check endpoint returning 200 while the application is actually broken (database connection exhausted, dependent service down) will keep routing traffic to a broken backend. Health checks must validate actual application health, not just "the process is running." A `/health` endpoint should verify it can reach its database, not just return a static 200.

**Slow backend accumulating connections:** with round-robin, a backend that degrades (GC pause, I/O saturation) continues receiving new requests even as existing ones queue up. Connection queues grow until the backend becomes completely unresponsive. Mitigation: circuit breakers that trip when a backend's error rate exceeds a threshold, combined with least-conn to naturally route around slow backends.

**Thundering herd on backend recovery:** when a failed backend comes back online, it momentarily has 0 active connections. Under least-conn, every new request floods to it until it accumulates load — potentially crashing it again immediately. Nginx Plus's `slow_start` directive ramps traffic to a recovering backend gradually. Open-source Nginx does not support `slow_start`; compensate with application-level circuit breakers (resilience4j, Hystrix) or a minimum connection floor in your LB config.

**Passive health check gap:** `proxy_next_upstream` in Nginx retries a failed request on another backend (error, timeout, 5xx). But if a backend is slow rather than failing — returning 200 after 10 seconds — passive checks don't catch it. Active health checks (Nginx Plus `/health_check` or an external checker like consul-template) are needed to remove slow-but-alive backends from the pool.

**Session loss during scale-in:** if sticky sessions are in use and the sticky backend is removed (scale-in event, rolling deploy), all sessions on that backend are lost. Users are dropped mid-session. The correct fix is stateless design: sessions externalized to Redis, so any backend can handle any request.

## Interview Talking Points

- "Stateless services don't need sticky sessions — design for statelessness. Sessions go in Redis, files go in object storage. Then any load balancing algorithm works and you can freely scale in/out."
- "Least connections is better than round-robin when backends have variable latency. Round-robin will give equal traffic to a degraded 500ms backend and a healthy 50ms backend, driving tail latency up. But least-conn only helps under concurrent load — with sequential requests, all backends have 0 active connections at each decision point, so it degrades to round-robin."
- "Weighted round-robin is the right tool for canary deployments and blue/green traffic migration: set the new version to weight=1 (10%), watch metrics, then ramp up. It's also correct when backends have genuinely different hardware capacity."
- "Health checks must match actual application health, not just TCP port open. A health endpoint that checks database connectivity, not just HTTP 200, catches half-failed backends before they affect users."
- "L4 vs L7: L4 is faster but blind to application content. L7 enables content routing, TLS termination, and header manipulation. For most web workloads, L7 overhead is negligible."
- "SSL termination at the load balancer offloads TLS from backends and centralizes certificate management. Backends communicate over HTTP on the internal network, which must be a trusted private subnet."
- "Global load balancing: DNS-based (GeoDNS) adds latency from DNS TTL lag; Anycast routes by BGP and is near-instant. Cloudflare uses Anycast — same IP, routed to nearest PoP."
- "Connection draining (deregistration delay): when removing a backend during a rolling deploy, the load balancer should stop sending new requests but allow in-flight requests to complete. AWS ALB calls this deregistration delay (default 300s). Without it, in-flight requests to a terminating backend get dropped."

## Hands-on Lab

**Time:** ~20-30 minutes
**Services:** nginx (load balancer), app1 (50ms), app2 (200ms), app3 (50ms), app4 (500ms — degraded)

### Setup

```bash
cd system-design-interview/01-foundations/07-load-balancing/
docker compose up -d
# Wait ~60 seconds for all Flask backends to start (pip install inside containers)
```

### Experiment

```bash
python experiment.py
```

The script sends requests through four Nginx upstream configurations, measuring distribution and latency for each algorithm:
- `/` → round-robin
- `/wrr/` → weighted round-robin (app1×3, app2×2, app3×3, app4×1)
- `/lc/` → least_conn
- `/iphash/` → ip_hash

### Break It

Kill the slow backend and observe how the pool adapts:

```bash
# Stop app4 (the 500ms backend)
docker compose stop app4

# Nginx passive health check removes it after first failure
# Round-robin now distributes across 3 backends only
python -c "
import urllib.request, json
for i in range(8):
    with urllib.request.urlopen('http://localhost:8080') as r:
        print(json.loads(r.read()))
"
# You should see app1, app2, app3 only — no app4

# Restart it
docker compose start app4
```

Simulate a backend returning errors (health check failure):

```bash
# Stop all backends — load balancer returns 502 Bad Gateway
docker compose stop app1 app2 app3 app4
python -c "
import urllib.request, urllib.error
try:
    urllib.request.urlopen('http://localhost:8080')
except urllib.error.HTTPError as e:
    print(f'Got {e.code} — expected 502 Bad Gateway when no backends available')
except Exception as e:
    print(f'Connection failed: {e}')
"
docker compose start app1 app2 app3 app4
```

### Observe

Expected output from `experiment.py` (approximate — exact numbers vary with timing):

```
Phase 1 (round-robin, 40 sequential requests):
  app1 ( 50ms declared)  ########## (10 reqs, 25%)  avg  ~55ms
  app2 (200ms declared)  ########## (10 reqs, 25%)  avg ~205ms
  app3 ( 50ms declared)  ########## (10 reqs, 25%)  avg  ~55ms
  app4 (500ms declared)  ########## (10 reqs, 25%)  avg ~505ms  ← equal traffic despite 10x slower

Phase 3 (weighted RR, 40 sequential requests):
  app1 ( 50ms declared)  ############ (13 reqs, 33%)  avg  ~55ms
  app2 (200ms declared)  ######## ( 9 reqs, 22%)      avg ~205ms
  app3 ( 50ms declared)  ############ (13 reqs, 33%)  avg  ~55ms
  app4 (500ms declared)  ##### ( 5 reqs, 11%)         avg ~505ms  ← reduced share

Phase 4 (least_conn, 40 concurrent):
  app1 ( 50ms declared)  ############### (15 reqs, 38%)  ← fast, gets more
  app3 ( 50ms declared)  ############### (15 reqs, 38%)  ← fast, gets more
  app2 (200ms declared)  ###### ( 7 reqs, 18%)
  app4 (500ms declared)  ## ( 3 reqs,  8%)              ← slow, gets almost none
```

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **GitHub:** Uses HAProxy for load balancing across their infrastructure. Their teams have documented eliminating single-load-balancer SPOFs by moving to active-active pairs with shared VIPs via VRRP. HAProxy's stats socket enables zero-downtime backend draining during rolling deploys: mark a backend as MAINT, wait for connections to drain, then take it offline — source: GitHub Engineering Blog.
- **Cloudflare:** Routes traffic using Anycast — the same 104.16.0.0/12 IP range is announced from 300+ data centers. BGP routing sends each packet to the nearest PoP. This means a DDoS targeting Cloudflare is automatically spread across 300 locations, each absorbing a fraction — source: Cloudflare Blog, "How Cloudflare's Global Anycast Network Works."
- **Netflix:** Uses AWS Elastic Load Balancing (ALB) in front of their stateless microservices. Because services are stateless (all state in Cassandra/EVCache), any algorithm works — they use round-robin. Their Zuul API gateway does L7 routing, forwarding requests to different backend clusters based on path — source: Netflix Tech Blog, "Zuul 2: The Netflix Journey to Asynchronous, Non-Blocking Systems" (2016).

## Common Mistakes

- **Running a single load balancer.** One Nginx instance in front of 10 app servers is still a single point of failure. Always deploy load balancers in pairs with VRRP or use a managed cloud load balancer that handles HA transparently.
- **Using IP hash for stateless services.** Modern applications should externalize state to Redis or a database. Once you do, you don't need sticky sessions, and IP hash wastes capacity by distributing unevenly (clients with many users behind one IP overload one backend).
- **Health check endpoint that always returns 200.** A `/health` endpoint that returns 200 regardless of application state lets broken backends receive traffic. The endpoint must check critical dependencies: database connectivity, downstream service reachability, and disk space.
- **Ignoring slow backends with round-robin.** A single degraded backend at 10x normal latency receives the same traffic as healthy backends. Under high load this creates a cascading failure: the slow backend queues up connections, degrading further. Use least-conn or circuit breakers to route around slow backends.
- **Not accounting for connection draining during deploys.** Sending SIGTERM to a backend immediately while in-flight requests are active drops those requests. Proper rolling deploys require a deregistration grace period: remove the backend from the load balancer pool, wait for in-flight requests to complete (ALB's deregistration delay, HAProxy's `maxconn drain`), then shut down. Without this, every deploy causes a brief error spike.
- **Forgetting SSL certificate renewal.** With TLS termination at the load balancer, the certificate lives in one place. Forgetting to renew it takes down HTTPS for all backends simultaneously. Use auto-renewal (Let's Encrypt / ACM) and monitor certificate expiry as a metric.
