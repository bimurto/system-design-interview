# Service Discovery & Coordination

**Prerequisites:** `../12-security-at-scale/`
**Next:** `../14-idempotency-exactly-once/`

---

## Concept

In a dynamic distributed system, services start and stop constantly — new instances spin up under load, crash and restart, or are replaced during deployments. Hard-coding service addresses (`api.internal:8080`) breaks the moment that service moves. Service discovery is the mechanism by which clients find the current network locations of the services they need to communicate with. At small scale this is solved with DNS; at microservices scale it requires a dedicated service registry that tracks live instances in real time.

Service registries fall into two categories by consistency model. CP registries (Consistent + Partition Tolerant, in CAP terms) — etcd, ZooKeeper, Consul in default mode — prioritise correctness. During a network partition, the minority side rejects writes and may reject reads, but the returned data is always accurate. AP registries (Available + Partition Tolerant) — Eureka, some Consul configurations — prioritise availability. Under partition, stale data may be returned, but the service always responds. For service discovery the CP model is usually preferred: better to be unable to discover a service than to be routed to a dead one.

Heartbeats and TTL leases are the mechanism that makes registries self-healing. A service registers itself with a TTL (time-to-live), then sends heartbeat renewals before the TTL expires. If the service crashes, it stops sending heartbeats, the TTL expires, and the registry automatically removes the entry — no explicit deregistration needed. Watchers (etcd) or long-polling subscribers (Consul) detect the deletion event and immediately update their local routing tables. The end-to-end detection latency is bounded by the TTL: a crashed service takes at most TTL seconds to disappear from the registry.

Leader election is a coordination primitive needed when exactly one instance of a service must perform a task — sending scheduled jobs, writing to a single-writer database, or acting as a shard coordinator. The canonical implementation uses a distributed key-value store's compare-and-swap (CAS) operation: write a key only if it does not exist. Exactly one candidate wins this atomic race; the winner becomes the leader and heartbeats the key to maintain ownership. Losers watch the key and campaign again when they detect the leader's key disappear.

Configuration management is a natural extension of service registries. etcd and Consul are commonly used to store application configuration (feature flags, database connection strings, rate limit parameters) alongside service registration data. Services watch configuration keys and receive change notifications in milliseconds, enabling zero-downtime configuration updates without service restarts. Kubernetes stores its entire cluster state — pod specs, service definitions, ConfigMaps — in etcd, making etcd the single source of truth for the entire cluster.

## How It Works

**Service Registration and Discovery via etcd:**
1. Service instance starts and creates an **etcd lease** with a TTL (e.g., 10 seconds) — the lease is a time-bounded handle that will auto-expire if not renewed
2. Service registers itself by writing a key tied to the lease (e.g., `/services/api/instance-1 = "10.0.0.1:8080"`); the key disappears automatically when the lease expires
3. Service starts a background heartbeat thread that calls `lease.keepalive()` every ~3 seconds, preventing the lease from expiring while the service is healthy
4. Clients (or the load balancer) query etcd for all keys under the `/services/api/` prefix to discover the current set of live instances
5. Clients set up a **watch** on the prefix; etcd streams real-time notifications (PUT = new instance, DELETE = removed instance) so the client's routing table stays current
6. If a service crashes, heartbeats stop; after the TTL expires, etcd automatically deletes the registration key
7. Watchers receive the DELETE event and remove the dead instance from their local routing table — no manual deregistration required

**Client-side vs server-side discovery:**

```
  Client-side discovery:                Server-side discovery:
  ┌────────┐  query  ┌──────────┐       ┌────────┐  request ┌────────┐
  │ Client │ ──────→ │ Registry │       │ Client │ ────────→ │   LB   │
  └────────┘         └──────────┘       └────────┘          └────────┘
      │  address         │                                       │
      │ ←────────────────┘                    query             │
      ↓ direct call                    ┌──────────────┐         │
  ┌─────────┐                          │   Registry   │ ←───────┘
  │ Service │                          └──────────────┘
  └─────────┘
  + No LB bottleneck             + Simple clients (any language)
  + Client can load balance      + LB may be an SPOF
  - Discovery logic in client    - Extra network hop
```

**etcd lease and watch (TTL heartbeat pattern):**

```
  t=0   Service A: client.lease(ttl=10) → lease_id=abc
        client.put("/services/api-1", "10.0.0.1:8080", lease=abc)

  t=3   Heartbeat: abc.refresh()  (renew before 10s expires)
  t=6   Heartbeat: abc.refresh()
  t=9   Heartbeat: abc.refresh()

  t=12  Service A CRASHES — no more heartbeats
  t=22  etcd: lease abc expired → DELETE /services/api-1
        Watcher receives DELETE event → removes api-1 from routing table
```

**Leader election with fencing tokens:**

```
  Etcd transaction (atomic CAS):
    IF version(/election/leader) == 0  ← key does not exist
    THEN put(/election/leader, "node-A", lease=abc)
    ELSE nothing

  Winner gets a unique, monotonically increasing "revision" number
  from etcd — this is the fencing token.

  Any write to a storage system must include the fencing token.
  The storage system rejects writes with a token ≤ the last accepted token.
  This prevents a slow zombie leader from overwriting new leader's data.
```

**Consul vs etcd vs ZooKeeper comparison:**

```
  Feature              etcd          Consul         ZooKeeper
  ─────────────────── ─────────────  ─────────────  ──────────────
  Consensus           Raft           Raft           ZAB (like Paxos)
  Health checks       External       Built-in       External
  DNS interface       No             Yes            No
  KV store            Yes            Yes            Yes (znodes)
  Multi-DC            No             Yes            Manual
  Language            Go             Go             Java
  Used by             Kubernetes     HashiCorp      Kafka, Hadoop
  Watch semantics     Prefix watch   Blocking GET   ZNode watch
```

### Trade-offs

| Registry | Consistency | Availability under partition | Health checks | Complexity |
|---|---|---|---|---|
| etcd | CP (Raft) | Minority partition rejects writes | Manual TTL | Low |
| Consul | CP by default | Configurable (AP mode) | Built-in | Medium |
| ZooKeeper | CP (ZAB) | Minority unavailable | Manual | High |
| Eureka (Netflix) | AP | Always available, may serve stale | Client-reported | Low |

### Failure Modes

**Network partition — etcd minority becomes unavailable:** etcd requires quorum (N/2+1 nodes) for writes and consistent reads. If 2 of 3 nodes are partitioned away from the third, the isolated node rejects all writes. Services in the isolated network segment cannot register or update their entries. Mitigation: ensure clients can fall back to cached service locations, or use an AP registry for non-critical discovery.

**Split-brain from stale watch events:** A watcher receives a DELETE event for a service key and removes it from the local routing table. But the DELETE was spurious — etcd re-election caused a temporary key loss that was immediately re-written. The client now thinks the service is down and stops routing to it. Mitigation: after receiving DELETE, do a GET to confirm the service is actually gone before removing from the routing table.

**Leader liveness false positive — GC pauses and the Redlock problem:** A leader holds a lease with a 10-second TTL. A JVM GC pause causes the leader process to freeze for 12 seconds. The lease expires; another node wins the election. The GC resumes; the old leader thinks it is still leader and continues writing. Both nodes believe they are the leader simultaneously. Mitigation: use fencing tokens (etcd's revision-based approach) so storage systems can reject writes from the old leader.

## Interview Talking Points

- "Service discovery solves the problem of locating service instances in a dynamic environment where IPs and ports change. The registry is a live directory; services register with TTL leases and heartbeat to stay registered."
- "etcd is CP — during a partition, the minority partition becomes read-only or unavailable. For Kubernetes, correctness is more important than availability: better to pause scheduling than to make inconsistent decisions."
- "Leader election uses compare-and-swap: write a key only if it doesn't exist. Exactly one candidate wins. The winner heartbeats the key with a lease; on crash the lease expires and a new election starts automatically."
- "Consul has built-in health checks (HTTP, TCP, script) unlike etcd which requires application-level TTL heartbeats. For service discovery specifically, Consul's health check model is more operationally convenient."
- "Distributed locks are harder than they look — the Redlock controversy (Kleppmann vs Antirez, 2016) shows that clock skew and GC pauses can cause two processes to simultaneously believe they hold a lock. Fencing tokens solve this by making storage systems reject stale writes."
- "Kubernetes stores all cluster state in etcd. Every pod spec, service definition, and ConfigMap is an etcd key. This is why etcd availability is critical — an etcd outage means the Kubernetes API server is down and no scheduling decisions can be made."

## Hands-on Lab

**Time:** ~3 minutes
**Services:** 3-node etcd cluster + Python experiment container (4 containers)

### Setup

```bash
cd system-design-interview/02-advanced/13-service-discovery-coordination/
docker compose up
```

### Experiment

The script runs six phases automatically:

1. Registers 3 services (`api-server-1`, `-2`, `-3`) with TTL=8s leases. Services 1 and 2 start heartbeat threads; service 3 does not.
2. Sets up a prefix watcher on `/services/api/` that prints events as they arrive. Immediately deregisters service 3 (lease revoked).
3. Stops heartbeat for service 2 and waits 10 seconds — the lease expires naturally and the watch fires a DELETE event.
4. Three election candidates race to write `/election/leader` using an atomic etcd transaction. Exactly one wins; the others see the key already set.
5. The current leader revokes its lease. The other two candidates are watching the key — one detects the DELETE and wins the new election.
6. Prints a summary table comparing etcd, Consul, and ZooKeeper, and explains the Redlock controversy.

### Break It

Simulate split-brain by pausing an etcd node and checking quorum behaviour:

```bash
# Pause etcd1 (simulates crash or network partition)
docker compose pause etcd1

# Try to write from the Python container — should still work (2/3 nodes up)
docker compose exec experiment python -c "
import etcd3
c = etcd3.client(host='etcd0', port=2379)
c.put('/test/key', 'value')
print('Write succeeded with 2/3 nodes')
"

# Now pause etcd2 — quorum lost (1/3 nodes)
docker compose pause etcd2
# Writes will now fail — etcd0 alone cannot achieve quorum
docker compose unpause etcd1 etcd2  # restore
```

### Observe

With 2 of 3 nodes running, all writes succeed — Raft requires only majority quorum. With only 1 of 3, writes fail with a connection error. The lease expiry in Phase 3 demonstrates that service deregistration is automatic — no explicit cleanup is needed when services crash.

### Teardown

```bash
docker compose down
```

## Real-World Examples

- **Kubernetes and etcd:** Kubernetes stores its entire cluster state in a 3- or 5-node etcd cluster. Every API server write (create pod, update service) is an etcd write. etcd's watch mechanism powers the Kubernetes controller pattern: controllers watch for state changes and reconcile the actual state toward the desired state. An etcd failure immediately takes down the Kubernetes API server. Source: Kubernetes documentation, "Operating etcd clusters for Kubernetes."
- **Netflix Eureka (AP service registry):** Netflix built Eureka, an AP service registry, for their microservices. Unlike etcd, Eureka prioritises availability: during a network partition, all Eureka nodes continue serving stale data rather than rejecting requests. Netflix's philosophy (informed by the "Chaos Monkey" experiments) is that stale routing is better than no routing — a request to a potentially dead service at least has a chance of succeeding. Source: Netflix Tech Blog, "Netflix's Eureka: Building a Service Registry," 2012.
- **HashiCorp Consul at scale:** Consul is used extensively at scale for service mesh, configuration management, and leader election. Its built-in health checks (HTTP, TCP, TTL) mean services don't need to implement their own heartbeat logic. Consul's DNS interface allows any service that can resolve DNS to discover registered services without a Consul client library. Source: HashiCorp, "Consul: Service Mesh Solution," product documentation.

## Common Mistakes

- **Setting TTL too short.** A TTL of 1 second with a heartbeat every 500ms leaves no margin for network hiccups. A brief network blip causes premature deregistration, triggering false alerts and client errors. Set TTL to 3-5x the heartbeat interval — if heartbeating every 3 seconds, use TTL=15s.
- **Not handling the discovery service going down.** If your service discovery layer is unavailable, services must still be able to route requests using a cached copy of the last known service list. Always cache discovered addresses locally and fall back to the cache on registry unavailability.
- **Using ZooKeeper for new projects.** ZooKeeper is mature but complex to operate — it requires a separate JVM process, uses a custom binary protocol, and has complex failure semantics. For new systems, etcd (simpler, Kubernetes-native) or Consul (richer features) are better choices.
- **Conflating service discovery with load balancing.** Service discovery tells you which instances exist and are healthy. Load balancing decides which instance to send a given request to. These are separate concerns — discovery gives you the pool, load balancing picks from the pool using round-robin, least-connections, or weighted policies.
