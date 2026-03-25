# Scalability

**Prerequisites:** none (this is the first topic)
**Next:** `../02-cap-theorem/`

---

## Concept

Scalability is a system's ability to handle growing load — more users, more data, more requests — without degrading. A system that works for 100 users often breaks at 100,000 not because of bugs, but because the assumptions baked into its architecture no longer hold. Understanding scalability means understanding *which* resource becomes exhausted first and *what options* exist for relieving that pressure.

**Vertical scaling** (scale-up) means giving a single machine more resources: more CPUs, more RAM, faster disks. It is the first solution most engineers reach for because it requires no code changes. A query that takes 2 seconds on a 4-core machine might take 0.5 seconds on a 32-core machine. The problem is that vertical scaling has hard limits — there is a largest machine money can buy — and the cost curve is nonlinear: doubling CPU often more than doubles the price. Beyond a certain point, vertical scaling is economically and physically impossible.

**Horizontal scaling** (scale-out) means adding more machines. Instead of one powerful server, you run many commodity servers behind a load balancer. This is how large-scale systems actually operate. The theoretical upper bound is much higher, and each incremental unit of capacity is roughly linear in cost. The catch is that your application must be designed to run as multiple identical instances, which requires statelessness: no instance should hold data in local memory that another instance needs.

**Statelessness** is the enabling property of horizontal scaling. A stateless service treats every request as self-contained — all needed context is either in the request itself or in a shared external store (database, cache, object storage). When a request arrives at any of 10 identical app servers, each server can handle it correctly. Sessions are stored in Redis, not in the app server's memory. Uploaded files go to object storage, not to the local filesystem. Once you achieve statelessness, adding capacity is as simple as starting another container.

**The bottleneck almost always shifts to the database.** Once you scale app servers horizontally, the single writable database becomes the choke point. The first remedy is read replicas: one primary handles all writes, while N replicas serve reads. Most applications read far more than they write, so this buys substantial headroom. When reads and writes both exceed a single machine's capacity, sharding (horizontal partitioning) becomes necessary — but sharding introduces significant complexity and should be deferred as long as possible.

## How It Works

A load balancer sits in front of your app servers and distributes incoming requests. Nginx's `upstream` block defines the backend pool; the default algorithm is **round-robin**: request 1 → server A, request 2 → server B, request 3 → server C, request 4 → server A, and so on. Other algorithms include:

- **Least connections:** send to whichever backend has fewest active connections (better for long-lived requests)
- **IP hash:** hash the client IP to always route a given client to the same backend (useful for sticky sessions)
- **Weighted round-robin:** send more traffic to more-capable backends

**Auto-scaling** extends horizontal scaling dynamically: a controller monitors metrics (CPU utilization, request queue depth, memory) and adds or removes instances in response. AWS Auto Scaling Groups, Kubernetes Horizontal Pod Autoscaler (HPA), and GCP Managed Instance Groups all implement this. A common trigger is: "if average CPU > 70% for 3 minutes, add 2 instances; if CPU < 30% for 10 minutes, remove 1 instance." The lag between detecting load and having new instances ready (typically 1–5 minutes for VMs, 30–60 seconds for containers) means you must provision ahead of expected peaks.

### Trade-offs

| Approach | Pros | Cons |
|----------|------|------|
| Vertical scaling | No code changes; simple ops | Hard limits; nonlinear cost curve; single point of failure |
| Horizontal scaling | Theoretically unlimited; fault-tolerant; cost-linear | Requires statelessness; more complex ops; needs load balancer |
| Read replicas | Easy first step; no app changes beyond routing reads | Replication lag; doesn't help write-heavy workloads |
| Sharding | Scales writes and storage | Complex queries; resharding pain; cross-shard joins |
| Auto-scaling | Handles variable load; cost-efficient | Warm-up lag; can oscillate; stateful services are hard |

### Failure Modes

**Session affinity breaks:** if sessions are stored in app server memory and the load balancer routes a user to a different server, the session is lost. Fix: externalize session storage to Redis.

**Thundering herd on scale-out:** when a new instance starts, it has an empty local cache. If 10 new instances start simultaneously under load, all of them miss the cache and hammer the database simultaneously. Fix: pre-warm caches on startup, use circuit breakers.

**Uneven load distribution:** if backends have different response times (e.g., one is running a slow background job), round-robin will queue requests behind the slow server. Fix: use least-connections algorithm.

**Database write bottleneck:** adding more app servers increases read load proportionally, but also increases write load. If writes are the bottleneck, read replicas don't help. Fix: sharding, CQRS, or write queuing.

## Interview Talking Points

- "Stateless services scale horizontally; stateful services require careful partitioning — sessions go in Redis, files go in object storage"
- "The database is almost always the bottleneck — add read replicas before sharding; sharding is a last resort"
- "Vertical scaling has hard limits (the biggest machine money can buy); horizontal scaling is theoretically unlimited but requires stateless design"
- "Auto-scaling helps with variable load but has warm-up lag — you must provision ahead of expected peaks, not react to them"
- "Round-robin works when all backends are equivalent; least-connections is better when request durations vary"

## Hands-on Lab

**Time:** ~20-30 minutes
**Services:** nginx (load balancer), app1, app2, app3 (Python HTTP servers)

### Setup

```bash
cd system-design-interview/01-foundations/01-scalability/
docker compose up -d
# Wait ~5 seconds for containers to initialize
```

### Experiment

```bash
python experiment.py
```

The script sends 30 GET requests to Nginx on `http://localhost:8080`. Each backend returns its hostname, so you can see the round-robin distribution. The script then stops `app2` and sends 10 more requests, demonstrating that traffic automatically routes to the remaining two backends.

### Break It

Manually kill two of three backends and observe behavior:

```bash
docker compose stop app2 app3
# Now send requests — all go to app1 only
python -c "
import urllib.request
for i in range(5):
    r = urllib.request.urlopen('http://localhost:8080')
    print(r.read().decode().strip())
"
# Stop the last one too
docker compose stop app1
# Now all requests fail — total outage
python -c "
import urllib.request, urllib.error
try:
    urllib.request.urlopen('http://localhost:8080')
except Exception as e:
    print(f'Expected failure: {e}')
"
```

### Observe

Expected output from `experiment.py`:

```
--- Round-robin across 3 backends (30 requests) ---
  Handled by: app1              ########## (10)
  Handled by: app2              ########## (10)
  Handled by: app3              ########## (10)

--- After stopping app2 — 2 backends remain (10 requests) ---
  Handled by: app1              ##### (5)
  Handled by: app3              ##### (5)
```

Perfect round-robin means equal distribution. After stopping app2, Nginx detects it is unavailable and stops routing to it (passive health check on first failure).

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **Netflix:** Moved from a monolith to hundreds of stateless microservices, each horizontally scaled independently. Their Chaos Monkey deliberately kills instances to verify fault tolerance — source: Netflix Tech Blog, "5 Lessons We've Learned Using AWS" (2010).
- **Twitter:** At peak, the "Fail Whale" appeared because the write database couldn't scale vertically fast enough. They eventually moved to a distributed architecture with separate read/write paths — source: Twitter Engineering Blog, "The Architecture Twitter Uses to Deal with 150M Active Users" (2013).
- **Stack Overflow:** Famously runs on a very small number of heavily optimized servers rather than many small ones, demonstrating that vertical scaling + aggressive caching can be cost-effective at medium scale — source: Nick Craver's blog series on Stack Overflow's architecture.

## Common Mistakes

- **Storing state in app server memory.** User sessions, rate-limit counters, and file uploads stored locally break the moment a second instance is added. Always externalize state.
- **Sharding too early.** Sharding introduces enormous operational complexity — cross-shard queries, resharding pain, distributed transactions. Exhaust vertical scaling, read replicas, and caching before considering sharding.
- **Ignoring the load balancer as a single point of failure.** Adding multiple app servers while running a single Nginx instance just moves the SPOF. Use DNS round-robin, an Anycast VIP, or a managed load balancer service.
- **Not accounting for auto-scaling warm-up time.** Reacting to a traffic spike by launching new instances means you're already degraded during the warm-up period. Use predictive scaling for known patterns (e.g., Monday morning load).
