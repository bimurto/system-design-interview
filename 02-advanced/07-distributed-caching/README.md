# Distributed Caching

**Prerequisites:** `../06-stream-processing/`
**Next:** `../08-search-systems/`

---

## Concept

A single Redis instance can serve hundreds of thousands of requests per second from a single server. For many applications, this is sufficient. But as data grows beyond memory capacity, or as write throughput exceeds what a single server can handle, a distributed cache is needed. Distributed caching partitions the keyspace across multiple cache nodes, allowing the cache to grow horizontally: add more nodes, and the cache capacity and throughput scale proportionally.

Redis Cluster is Redis's built-in distributed mode. Rather than using consistent hashing (as Cassandra or Memcached do), Redis Cluster pre-divides the keyspace into exactly **16,384 hash slots**. The slot for a key is computed as `CRC16(key) % 16384`. Each cluster node is assigned ownership of a contiguous range of slots. With 3 nodes, each owns roughly 5,461 slots. Adding a new node involves migrating some slot ranges to it — Redis supports live slot migration with the `CLUSTER MIGRATE` command, using `MOVED` and `ASK` redirections to route clients during the migration.

Redis Sentinel provides a different solution: not sharding, but **high availability for a single master**. Sentinel processes run alongside Redis instances and continuously monitor the master. If the master fails to respond to enough Sentinel processes within a timeout, Sentinel elects one of the replicas as the new master and notifies clients via its own discovery mechanism. Sentinel is appropriate when the dataset fits on a single Redis server but you need automatic failover — simpler to operate than Cluster but unable to scale beyond one master's write capacity.

The hardest distributed caching problem is not technical — it's the **hot key**. Consistent hashing or hash slots ensure that on average, each node handles an equal share of the keyspace. But if one key is accessed millions of times per second (a viral tweet's like counter, a global leaderboard, a feature flag checked on every request), all those requests converge on a single node. No amount of horizontal scaling helps — you cannot spread one key across multiple nodes in a standard Redis setup. The solutions involve either splitting the key (shard the counter into N sub-counters), caching at the application layer (local in-process cache), or adding read replicas specifically for the hot key.

Cache warm-up is a related operational challenge. When a new cache cluster starts or all keys expire simultaneously (cache avalanche), the backing database receives every request directly. In a high-traffic system, this cold-start thundering herd can overwhelm the database before the cache has a chance to warm up. Mitigation strategies include pre-populating the cache before traffic arrives (eager warm-up from a DB dump), staggering expiry times with random jitter to avoid mass expiry, and using distributed locks so only one server repopulates each key while others wait.

## How It Works

**Hash slot computation:**
```python
def crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = (crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc

slot = crc16(key.encode()) % 16384
```

**Redis Cluster slot assignment (3 nodes):**
```
  Node 1 (port 7001): slots    0 – 5460    (5461 slots)
  Node 2 (port 7002): slots 5461 – 10922   (5462 slots)
  Node 3 (port 7003): slots 10923 – 16383  (5461 slots)

  Key "user:profile:42": CRC16("user:profile:42") % 16384 = 8790
    → assigned to Node 2 (slots 5461–10922)

  Client flow:
    1. Client computes slot = 8790
    2. Checks its cached slot map: 8790 → Node 2
    3. Sends SET command directly to Node 2
    4. If Node 2 has migrated slot 8790 to Node 3: MOVED 8790 localhost:7003
    5. Client updates slot map, retries to Node 3
```

**MOVED vs ASK redirections:**
```
  MOVED: slot permanently moved to new node. Client must update its
         slot map and use the new node for all future requests.

  ASK:   slot is currently being migrated (CLUSTER MIGRATE in progress).
         Client should send to new node for THIS request only, but
         NOT update its slot map (migration might not be complete).
```

**Hash tags for multi-key operations:**
```
  Multi-key operations (MGET, MSET, transactions) require all keys
  to be in the same slot. Use {tag} to force co-location:

  {order:123}:items     → slot = CRC16("order:123") % 16384
  {order:123}:metadata  → slot = CRC16("order:123") % 16384
  {order:123}:timeline  → slot = CRC16("order:123") % 16384

  All three land on the same node → MGET works without cross-slot error.
  Without hash tags: keys end up on different nodes → MGET fails in Cluster.
```

**Redis Sentinel topology:**
```
  Sentinel-1  Sentinel-2  Sentinel-3
      │            │            │
      └────────────┴────────────┘
               monitors
                  │
          Redis Master ◄── Clients
                  │
          Redis Replica-1
          Redis Replica-2

  If master is unreachable by quorum (≥ majority) of Sentinels:
    1. Sentinel leader elected (Raft-like vote)
    2. Best replica selected (by replication lag)
    3. Replica promoted to master (SLAVEOF NO ONE)
    4. Other replicas reconfigured to follow new master
    5. Clients notified via Sentinel API / pub/sub
```

### Trade-offs

| Feature | Redis Cluster | Redis Sentinel | Single Redis |
|---|---|---|---|
| Scale | Horizontal (N masters) | Vertical (one master) | Vertical |
| HA | Built-in (replicas per master) | Yes (auto-failover) | No |
| Multi-key ops | Only with hash tags | Yes | Yes |
| Complexity | High | Medium | Low |
| Max data | N × RAM | 1 × RAM | 1 × RAM |
| Throughput | N × ops/s | 1 × ops/s | 1 × ops/s |
| Use when | Large datasets, high write throughput | Medium datasets, need HA | Development, small data |

### Failure Modes

**Split-brain in Redis Cluster:** if the network partitions and nodes can't communicate, nodes on each side may believe they are masters. Redis Cluster uses a quorum-based failure detection: a node is considered failed only if a majority of masters agree it's unreachable. However, if the partition is between two equal halves (e.g., 3 nodes on each side of a 6-node cluster), neither half has quorum and both halves may reject writes — this is the safe behavior. With `cluster-require-full-coverage yes` (default), writes are rejected when any slot has no master. With `no`, writes continue for slots that do have a master.

**Hot key saturation:** a single key receiving millions of requests per second will saturate its node's CPU and network bandwidth. No horizontal scaling helps because the key always maps to one slot → one node. Detected by Redis `MONITOR` command or slow log. Solved by key splitting (shard counters), local process-level caching, or read replicas.

**Cluster bus network saturation:** Redis Cluster nodes gossip with each other via a separate cluster bus (port = data port + 10000). In a large cluster (hundreds of nodes), gossip traffic can become significant. Each node sends gossip messages to a random subset of nodes periodically. Monitor cluster bus traffic separately from client traffic.

## Interview Talking Points

- "Redis Cluster uses 16,384 hash slots, not consistent hashing. `slot = CRC16(key) % 16384`. Slots are explicitly assigned to masters. Adding a node requires migrating slot ranges — no automatic rebalancing."
- "MOVED redirect: if a client asks the wrong node for a key, the node responds with `MOVED slot ip:port`. The client updates its slot map and retries. This is transparent to application code using a cluster-aware client like redis-py."
- "Hot keys are the hardest distributed cache problem. A key accessed a million times/second always lands on one node. Solutions: shard the key (counter → N sub-counters), local in-process cache, or read replicas."
- "Redis Sentinel provides high availability without sharding — auto-failover when the master dies. Use Cluster when data exceeds one node's RAM or write throughput exceeds one node's capacity. Use Sentinel for simpler HA when data fits in one master."
- "Hash tags allow co-locating related keys on the same slot: `{user:42}:profile` and `{user:42}:settings` both hash on `user:42`. This enables multi-key operations (MGET, transactions) in a cluster, which otherwise require all keys to be on the same node."
- "Cache warm-up is an operational problem: when a new cluster starts cold, every request is a cache miss and hits the database. Pre-populate before traffic, stagger TTLs to avoid avalanche, and use a distributed lock (Redis SETNX) to prevent stampede."

## Hands-on Lab

**Time:** ~20-30 minutes
**Services:** 3-node Redis Cluster (ports 7001-7003), init container

### Setup

```bash
cd system-design-interview/02-advanced/07-distributed-caching/
docker compose up -d
# Wait ~15 seconds for cluster to initialize
```

### Experiment

```bash
pip install redis
python experiment.py
```

The script sets 1000 keys and reports their distribution across the 3 nodes (should be ~333 each), demonstrates MOVED-transparent routing, hammers a hot key from 20 threads to show all traffic hitting one node, implements key splitting into 10 shards showing distributed load, and demonstrates the 5 main Redis data structures (STRING, HASH, SET, SORTED SET, LIST) for different caching patterns.

### Break It

```bash
# Demonstrate that multi-key operations fail without hash tags
python -c "
from redis.cluster import RedisCluster, ClusterNode

rc = RedisCluster(
    startup_nodes=[ClusterNode('localhost', 7001)],
    decode_responses=True,
    skip_full_coverage_check=True,
)

# This will likely fail with CROSSSLOT error
# because the keys land on different slots
try:
    rc.mset({'user:1': 'alice', 'user:2': 'bob', 'user:3': 'carol'})
    print('MSET succeeded (keys happened to be on same slot)')
except Exception as e:
    print(f'MSET failed: {e}')
    print('Fix: use hash tags: {user}:1, {user}:2, {user}:3')

# Now with hash tags — all keys in same slot
try:
    rc.mset({'{user}:1': 'alice', '{user}:2': 'bob', '{user}:3': 'carol'})
    vals = rc.mget('{user}:1', '{user}:2', '{user}:3')
    print(f'MSET with hash tags succeeded: {vals}')
except Exception as e:
    print(f'Error: {e}')
"
```

### Observe

Phase 1 shows the hash slot distribution is nearly uniform across the 3 nodes (~333 keys each). Phase 3 shows all 5000 hot-key writes hitting one specific node (the one that owns the slot for `leaderboard:global`). Phase 4 shows the 10 shard keys distributed across all 3 nodes. The data structures demo shows Redis's built-in types eliminating the need for application-level serialization for structured data.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **Twitter's Manhattan and Redis usage:** Twitter uses Redis Cluster for timeline caching, session storage, and rate limiting. Their "Twemproxy" (proxy for Redis/Memcached clusters) was widely used before Redis Cluster became production-ready. Their hot-tweet problem (a celebrity tweet gets millions of likes in seconds) is solved by local in-process caching in each API server for the hottest keys. Source: Twitter Engineering, "Caching with Twemproxy," 2012.
- **Snapchat's Mustache:** Snapchat built "Mustache," a distributed cache layer on top of Redis Cluster, to handle their high-cardinality user data (each user's snap count, story views, etc.). Their hot key problem arises when a celebrity's story goes viral; their solution is per-key read replicas that can be spun up dynamically. Source: Snapchat Engineering Blog, "Scaling Snapchat," 2018.
- **GitHub's use of Redis Cluster:** GitHub uses Redis Cluster for repository statistics, pull request state, and CI/CD pipeline state. They found that `cluster-require-full-coverage no` was essential for operating in degraded mode during partial cluster failures — rather than rejecting all writes, they accept writes for available slots and alert on the degraded slot ranges. Source: GitHub Engineering Blog, "How GitHub uses GitHub," 2021.

## Common Mistakes

- **Forgetting hash tags for multi-key operations.** In Redis Cluster, `MGET user:1 user:2` will fail if those keys are on different slots. Use `{user}:1` and `{user}:2` to co-locate them. This is the most common Redis Cluster migration gotcha when moving from standalone Redis.
- **Treating Redis as a database.** Redis is an in-memory cache with optional persistence. Its persistence (AOF, RDB) is not as durable as a true database (Postgres, MySQL). Power loss between an AOF flush can cause data loss. Never use Redis as the primary store for data you can't afford to lose.
- **Not planning for key expiry and eviction.** If Redis memory fills up and no TTLs are set, writes fail (with `allkeys-lru` policy, old keys are evicted). Without a clear expiry strategy, Redis becomes a memory black hole. Set TTLs on all keys, and configure an appropriate eviction policy (`allkeys-lru` for pure cache, `volatile-lru` for mixed cache/persistent).
- **Using Cluster for all use cases.** Redis Cluster adds operational complexity: slot management, CROSSSLOT errors, hash tag requirements, cluster bus monitoring. If your dataset fits in 100GB (well within a single server's RAM), Redis Sentinel or single Redis is simpler, cheaper, and easier to operate. Choose Cluster when you need horizontal write scaling, not just HA.
