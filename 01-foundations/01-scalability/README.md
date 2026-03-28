# Scalability

**Prerequisites:** none (this is the first topic)
**Next:** `../02-cap-theorem/`

---

## Concept

Scalability is a system's ability to handle growing load — more users, more data, more requests — without degrading. A
system that works for 100 users often breaks at 100,000 not because of bugs, but because the assumptions baked into its
architecture no longer hold. Understanding scalability means understanding *which* resource becomes exhausted first and
*what options* exist for relieving that pressure.

**Vertical scaling** (scale-up) means giving a single machine more resources: more CPUs, more RAM, faster disks. It is
the first solution most engineers reach for because it requires no code changes. A query that takes 2 seconds on a
4-core machine might take 0.5 seconds on a 32-core machine. The problem is that vertical scaling has hard limits — there
is a largest machine money can buy — and the cost curve is nonlinear: doubling CPU often more than doubles the price.
Beyond a certain point, vertical scaling is economically and physically impossible.

**Horizontal scaling** (scale-out) means adding more machines. Instead of one powerful server, you run many commodity
servers behind a load balancer. This is how large-scale systems actually operate. The theoretical upper bound is much
higher, and each incremental unit of capacity is roughly linear in cost. The catch is that your application must be
designed to run as multiple identical instances, which requires statelessness: no instance should hold data in local
memory that another instance needs.

**Statelessness** is the enabling property of horizontal scaling. A stateless service treats every request as
self-contained — all needed context is either in the request itself or in a shared external store (database, cache,
object storage). When a request arrives at any of 10 identical app servers, each server can handle it correctly.
Sessions are stored in Redis, not in the app server's memory. Uploaded files go to object storage, not to the local
filesystem. Once you achieve statelessness, adding capacity is as simple as starting another container.

**The bottleneck almost always shifts to the database.** Once you scale app servers horizontally, the single writable
database becomes the choke point. The first remedy is read replicas: one primary handles all writes, while N replicas
serve reads. Most applications read far more than they write, so this buys substantial headroom. When reads and writes
both exceed a single machine's capacity, sharding (horizontal partitioning) becomes necessary — but sharding introduces
significant complexity and should be deferred as long as possible.

## How It Works

**Request Flow Through a Horizontally Scaled System:**

1. Client sends an HTTP request to the load balancer's public IP (or DNS name)
2. Load balancer selects a healthy backend using its scheduling algorithm (round-robin, least-connections, etc.)
3. Stateless app server receives the request; session/user state is fetched from an external store (Redis, DB) — not
   local memory
4. App server queries the database: reads route to a read replica, writes route to the primary
5. Response travels back through the load balancer to the client
6. If CPU or queue-depth exceeds a threshold, the auto-scaler launches additional app server instances (1–5 min warm-up
   for VMs, 30–60s for containers)
7. New instances register with the load balancer and begin receiving traffic after passing health checks

A load balancer sits in front of your app servers and distributes incoming requests. Nginx's `upstream` block defines
the backend pool; the default algorithm is **round-robin**: request 1 → server A, request 2 → server B, request 3 →
server C, request 4 → server A, and so on. Other algorithms include:

- **Least connections:** send to whichever backend has fewest active connections (better for long-lived requests)
- **IP hash:** hash the client IP to always route a given client to the same backend (useful for sticky sessions)
- **Weighted round-robin:** send more traffic to more-capable backends
- **Consistent hashing:** distribute based on a hash of the request key (URL, user ID) — useful for cache locality where
  you want the same key to always hit the same backend

**Active vs Passive Health Checks** is a critical distinction for senior engineers:

- **Passive (Nginx open-source default):** the load balancer only marks a backend down after a real client request
  fails. The first request to a dead backend returns an error to the client before failover occurs. After `max_fails`
  failures within `fail_timeout` seconds, the backend is temporarily removed.
- **Active (Nginx Plus, HAProxy, Envoy):** the load balancer sends periodic probe requests (e.g., `GET /health`)
  independently of client traffic. Failures are detected before clients are affected, at the cost of additional probe
  traffic.

**Connection Draining (Graceful Scale-In):** when removing an instance during a scale-in event or deployment, you must
allow in-flight requests to complete before the instance is terminated. Without draining, long-lived requests (file
uploads, streaming responses) are dropped mid-flight. AWS ALB and GCP LB support configurable drain timeouts (typically
30–300s). Kubernetes uses `preStop` hooks and `terminationGracePeriodSeconds` to achieve the same effect.

**Auto-scaling** extends horizontal scaling dynamically: a controller monitors metrics (CPU utilization, request queue
depth, memory) and adds or removes instances in response. AWS Auto Scaling Groups, Kubernetes Horizontal Pod
Autoscaler (HPA), and GCP Managed Instance Groups all implement this. A common trigger is: "if average CPU > 70% for 3
minutes, add 2 instances; if CPU < 30% for 10 minutes, remove 1 instance." The lag between detecting load and having new
instances ready (typically 1–5 minutes for VMs, 30–60 seconds for containers) means you must provision ahead of expected
peaks.

### Trade-offs

| Approach            | Pros                                                           | Cons                                                                                      |
|---------------------|----------------------------------------------------------------|-------------------------------------------------------------------------------------------|
| Vertical scaling    | No code changes; simple ops; low latency within single machine | Hard limits; nonlinear cost curve; single point of failure; planned downtime for upgrades |
| Horizontal scaling  | Theoretically unlimited; fault-tolerant; cost-linear           | Requires statelessness; more complex ops; needs load balancer                             |
| Load balancer       | Enables horizontal scaling; health checking; SSL termination   | Is itself a SPOF unless made redundant (Anycast VIP, managed LB, VRRP)                    |
| Read replicas       | Easy first step; no app changes beyond read routing            | Replication lag; doesn't help write-heavy workloads; replica divergence on failover       |
| Sharding            | Scales writes and storage linearly                             | Complex queries; resharding pain; cross-shard joins; distributed transactions             |
| Auto-scaling        | Handles variable load; cost-efficient                          | Warm-up lag; can oscillate ("flapping"); stateful services are hard to drain              |
| Connection draining | Zero dropped requests during scale-in                          | Adds latency to deployments; drain timeout must be tuned per workload                     |

### Failure Modes

**Session affinity breaks:** if sessions are stored in app server memory and the load balancer routes a user to a
different server, the session is lost. Fix: externalize session storage to Redis.

**Thundering herd on scale-out:** when a new instance starts, it has an empty local cache. If 10 new instances start
simultaneously under load, all of them miss the cache and hammer the database simultaneously. Fix: pre-warm caches on
startup, use circuit breakers, and stagger instance launches.

**Uneven load distribution:** if backends have different response times (e.g., one is running a slow background job),
round-robin will queue requests behind the slow server. Fix: use least-connections algorithm.

**Database write bottleneck:** adding more app servers increases read load proportionally, but also increases write
load. If writes are the bottleneck, read replicas don't help. Fix: sharding, CQRS, or write queuing.

**Load balancer as single point of failure:** adding multiple app servers while running a single Nginx instance just
moves the SPOF. Fix: use DNS round-robin, an Anycast VIP, a managed load balancer service (AWS ALB, GCP LB), or a
VRRP-paired failover pair (Keepalived).

**Passive health check gap:** Nginx open-source only detects a dead backend when a real client request fails. The first
request to a crashed instance returns a 502 to the client before failover completes. Fix: use active health checks (
Nginx Plus, HAProxy, Envoy), or keep `proxy_next_upstream` enabled so Nginx immediately retries on a different backend —
hiding single failures from the client.

**Auto-scaler oscillation (flapping):** if the scale-out threshold and scale-in threshold are too close together, the
system repeatedly adds and removes instances. Fix: use a cooldown period and separate thresholds (e.g., scale out at 70%
CPU, scale in only at 30% CPU with a 10-minute sustained window).

**Connection draining timeout mismatch:** if the drain timeout is shorter than the longest in-flight request, long-lived
connections are forcibly terminated. Fix: tune drain timeout to exceed your 99th-percentile request duration.

## Interview Talking Points

- "Stateless services scale horizontally; stateful services require careful partitioning — sessions go in Redis, files
  go in object storage. The moment you store anything in app server memory, you've created an implicit coupling."
- "The database is almost always the bottleneck — add read replicas before sharding; sharding is a last resort because
  it eliminates cross-shard joins and complicates transactions."
- "Vertical scaling has hard limits (the biggest machine money can buy); horizontal scaling is theoretically unlimited
  but requires stateless design. In practice, I'd exhaust vertical scaling and add caching before touching sharding."
- "Auto-scaling helps with variable load but has warm-up lag — you must provision ahead of expected peaks, not react to
  them. For predictable spikes (Monday morning traffic, Black Friday), use scheduled scaling."
- "Round-robin works when all backends are equivalent; least-connections is better when request durations vary —
  otherwise round-robin can pile up requests on a slow backend."
- "Active health checks (HAProxy, Envoy) detect failures before clients see them; Nginx open-source passive checks let
  one request fail first. In a staff-level design, I'd choose the LB technology based on this distinction."
- "The load balancer itself is a SPOF — use a managed LB (AWS ALB, GCP LB) or a VRRP pair. DNS round-robin is a last
  resort because TTL-based failover is slow."
- "Connection draining is non-negotiable for zero-downtime deployments — without it, in-flight requests are dropped on
  every deploy. Tune the drain timeout to your p99 request latency."

## Hands-on Lab

**Time:** ~25-35 minutes
**Services:** nginx (load balancer), app1, app2, app3 (Python HTTP servers)

### Setup

```bash
cd system-design-interview/01-foundations/01-scalability/
docker compose up -d
# Wait ~15 seconds for all health checks to pass
docker compose ps   # all services should show "healthy"
```

### Experiment

```bash
python experiment.py
```

The script runs four phases:

1. **Baseline:** 30 requests distributed evenly across 3 healthy backends — confirms round-robin
2. **Backend failure:** stops `app2`, demonstrates passive health check failover (first request may error)
3. **Total outage:** stops all backends, shows 502 errors and the LB-as-SPOF problem
4. **Recovery:** restarts all backends, confirms self-healing without manual intervention

### Break It Manually

Observe weighted load by making one backend "slow" via environment variable:

```bash
# Stop app2 and relaunch it with artificial latency
docker compose stop app2
docker compose run -d -e HOSTNAME=app2 -e SIMULATE_LATENCY_MS=300 --name slow-app2 app2
# Send traffic — round-robin still sends 1/3 of requests to the slow backend
python -c "
import urllib.request, time
for i in range(9):
    t0 = time.monotonic()
    r = urllib.request.urlopen('http://localhost:8080', timeout=5)
    ms = int((time.monotonic() - t0) * 1000)
    print(f'{r.read().decode().strip()}  ({ms}ms)')
"
# Observe that round-robin blindly routes to the slow backend,
# while least_conn would avoid it after the first slow response.
docker stop slow-app2 && docker rm slow-app2
docker compose start app2
```

### Observe

Expected output from `experiment.py`:

```
=================================================================
  SCALABILITY LAB: Horizontal Scaling with Nginx
=================================================================

[Phase 1] Baseline — all 3 backends are running.
...
--- Round-robin across 3 backends (30 requests) ---
  Handled by: app1              ########## (10)
  Handled by: app2              ########## (10)
  Handled by: app3              ########## (10)

[Phase 2] Simulate backend failure — stopping app2.
...
--- After stopping app2 — 2 backends remain (12 requests) ---
  Handled by: app1              ###### (6)
  Handled by: app3              ###### (6)

[Phase 3] Total outage — stopping ALL backends.
...
--- All backends stopped — expected 502/errors (5 requests) ---
  ERROR: ...                    ##### (5)

[Phase 4] Recovery — restarting all backends.
...
--- After recovery — all backends back (15 requests) ---
  Handled by: app1              ##### (5)
  Handled by: app2              ##### (5)
  Handled by: app3              ##### (5)
```

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **Netflix:** Moved from a monolith to hundreds of stateless microservices, each horizontally scaled independently.
  Their Chaos Monkey deliberately kills instances to verify fault tolerance — source: Netflix Tech Blog, "5 Lessons
  We've Learned Using AWS" (2010).
- **Twitter:** At peak, the "Fail Whale" appeared because the write database couldn't scale vertically fast enough. They
  eventually moved to a distributed architecture with separate read/write paths — source: Twitter Engineering Blog, "The
  Architecture Twitter Uses to Deal with 150M Active Users" (2013).
- **Stack Overflow:** Famously runs on a very small number of heavily optimized servers rather than many small ones,
  demonstrating that vertical scaling + aggressive caching can be cost-effective at medium scale — source: Nick Craver's
  blog series on Stack Overflow's architecture.
- **Google:** Uses weighted least-connections internally (via Maglev, their software load balancer) and combines active
  health checks with real-time traffic signals to remove unhealthy backends within seconds — source: Maglev paper, NSDI
  2016.

## Common Mistakes

- **Storing state in app server memory.** User sessions, rate-limit counters, and file uploads stored locally break the
  moment a second instance is added. Always externalize state.
- **Sharding too early.** Sharding introduces enormous operational complexity — cross-shard queries, resharding pain,
  distributed transactions. Exhaust vertical scaling, read replicas, and caching before considering sharding.
- **Ignoring the load balancer as a single point of failure.** Adding multiple app servers while running a single Nginx
  instance just moves the SPOF. Use DNS round-robin, an Anycast VIP, or a managed load balancer service.
- **Not accounting for auto-scaling warm-up time.** Reacting to a traffic spike by launching new instances means you're
  already degraded during the warm-up period. Use predictive scaling for known patterns (e.g., Monday morning load).
- **Skipping connection draining.** Terminating instances immediately during a scale-in event or rolling deploy drops
  in-flight requests. Always configure a drain timeout and verify it covers your p99 request latency.
- **Choosing the wrong health check model.** Using passive health checks in latency-sensitive systems means one client
  absorbs each backend failure. For user-facing APIs, use active probes so failures are detected proactively.
