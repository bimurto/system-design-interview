# Case Study: Distributed Cache (Redis Internals)

**Prerequisites:** `../../01-foundations/06-caching/`, `../../02-advanced/07-distributed-caching/`, `../../02-advanced/01-consistent-hashing/`

---

## The Problem at Scale

Twitter caches 40TB of data in Redis. Discord handles 5 million messages per second with Redis as their primary hot store. At this scale, understanding Redis internals — not just the API — determines whether your cache survives production.

| Metric | Value |
|---|---|
| Twitter Redis cluster | ~40TB total, thousands of nodes |
| Discord messages/second | 5 million |
| Redis ops/second (single node) | ~500K (simple operations) |
| Redis pipeline throughput | 10-100× higher |
| Typical P99 GET latency | 0.1–0.5ms |
| Memory efficiency | 64B per string key+value |

The challenge: Redis is fast in isolation. At scale, you hit limits — single-threaded bottlenecks, memory pressure, network RTT, cluster routing. Understanding these limits lets you design around them.

---

## Requirements

### Functional
- Serve key-value cache for application data
- Support complex data structures: sorted sets, streams, HyperLogLog, Bloom filter
- Pub/sub messaging for real-time distribution
- Persistence: survive restarts without full cache-cold performance degradation

### Non-Functional
- Sub-millisecond P99 GET/SET latency
- Linear throughput scaling with cluster size
- Memory-efficient storage (minimize overhead per key)
- Cluster-mode: no single point of failure per shard

### Capacity Estimation

| Metric | Calculation | Result |
|---|---|---|
| Memory per cached item | 100B key + 200B value + 64B overhead | ~364B |
| Items per GB | 1GB / 364B | ~2.7M items |
| Redis nodes for 40TB | 40TB / (64GB per node × 0.75 headroom) | ~834 nodes |
| Ops/s per node | 500K simple ops/s | — |
| Nodes for 5M ops/s | 5M / 500K | 10 nodes minimum |

---

## High-Level Architecture

```
  Application Servers
       │
       ▼
  ┌──────────────────────────────────────────────────────────┐
  │                  Redis Cluster Client                     │
  │  - Downloads cluster topology on connect                 │
  │  - Routes each key to correct node (CRC16 % 16384)       │
  │  - Handles MOVED and ASK redirects on slot migration      │
  └───────────┬──────────────────┬──────────────────┬────────┘
              │                  │                  │
       ┌──────▼──────┐   ┌──────▼──────┐   ┌──────▼──────┐
       │  Redis-1    │   │  Redis-2    │   │  Redis-3    │
       │  Master     │   │  Master     │   │  Master     │
       │  Slots 0-   │   │  Slots 5461-│   │  Slots 10923│
       │  5460       │   │  10922      │   │  -16383     │
       └──────┬──────┘   └──────┬──────┘   └──────┬──────┘
              │                  │                  │
       ┌──────▼──────┐   ┌──────▼──────┐   ┌──────▼──────┐
       │  Replica-1  │   │  Replica-2  │   │  Replica-3  │
       │  (standby)  │   │  (standby)  │   │  (standby)  │
       └─────────────┘   └─────────────┘   └─────────────┘
```

**16,384 hash slots** are divided across all master nodes. Each key maps to one slot: `slot = CRC16(key) % 16384`. The cluster client caches this mapping locally. On `MOVED` error (wrong node), client refreshes topology and retries.

**Replication:** each master has one or more replicas. If a master fails, a replica is promoted automatically (within `cluster-node-timeout` milliseconds, default 15s). The lab uses 3 masters without replicas to stay within the 5-container limit.

---

## Deep Dives

### 1. Redis Single-Threaded Model: Why It's Fast

Redis is single-threaded for command processing (as of Redis 6, I/O is multithreaded, but command execution is still single-threaded per instance). This seems like a limitation but is actually a strength:

**No lock contention:** hash tables, sorted sets, and other data structures are accessed from a single goroutine. No mutex, no CAS, no cache-line ping-pong. The data structure operations are pure, fast, and lock-free.

**Predictable latency:** a single-threaded event loop (epoll/kqueue) processes commands in FIFO order. P99 latency is bounded by the longest single command. Avoid O(N) commands (KEYS *, SMEMBERS large-set) which block the event loop.

**Memory locality:** all data is in-memory, accessed from one thread. CPU cache efficiency is maximized.

**What single-threaded means for you:**
- One expensive command (e.g., `SORT` on 1M elements) blocks all other clients for its duration
- Use `SCAN` instead of `KEYS *` (SCAN is O(1) per call, resumes across calls)
- Break large operations into smaller chunks

**Redis 6+ I/O threads:** I/O reading/writing is multithreaded (configurable `io-threads 4`). Command execution remains single-threaded. This improves throughput for workloads with large values or many small connections.

### 2. Eviction Policies: LRU vs LFU vs allkeys-lru

When `maxmemory` is set and Redis is full, it must evict a key before accepting new writes.

**LRU (Least Recently Used):** evict the key that hasn't been accessed for the longest time. Redis uses an *approximate* LRU — it samples a small set of random keys and evicts the least recently used among the sample (default sample size: 5). Configurable via `maxmemory-samples`.

**LFU (Least Frequently Used, Redis 4+):** evict the key accessed the fewest times over a time window. Better for workloads where "recently added but rarely accessed" keys shouldn't displace "older but heavily used" keys. Twitter uses LFU for their timeline cache.

**Key policy selection:**
| Policy | Best For |
|---|---|
| `allkeys-lru` | General cache (most common choice) |
| `allkeys-lfu` | Workloads with hot/cold key patterns |
| `volatile-lru` | Mixed cache+persistent data (only evict TTL-bearing keys) |
| `volatile-ttl` | When you want short-lived keys evicted first |
| `noeviction` | When you CANNOT lose data (use with monitoring) |

**allkeys vs volatile prefix:** `allkeys-*` can evict any key; `volatile-*` only evicts keys with an expiry set. Use `volatile-*` when some keys are critical and should never be evicted (set them without TTL).

### 3. Persistence: RDB vs AOF

**RDB (Redis Database file):**
- `BGSAVE`: fork a child process (copy-on-write, O(1)). Child writes a point-in-time snapshot to `dump.rdb`.
- Parent continues serving requests. Memory pages modified after the fork are copied (copy-on-write). Memory overhead during save: ~10-50% of working set.
- On restart: load `dump.rdb` into memory. Fast startup (binary format, compressed).
- Risk: data written after the last snapshot is lost on crash. Typical config: save every 5 minutes or after 100 writes.

**AOF (Append-Only File):**
- Every write command is appended to `appendonly.aof`.
- On restart: replay AOF to reconstruct state. Slower startup (thousands of RPUSH, SET commands).
- `appendfsync always`: fsync after every command — safest, slowest.
- `appendfsync everysec`: fsync once per second — 1-second data loss risk, fast.
- `appendfsync no`: OS decides — fastest, highest data loss risk.

**AOF Rewrite:** AOF grows unboundedly. `BGREWRITEAOF` creates a compact version (only current state, not history of mutations). Redis auto-triggers this when AOF exceeds a size threshold.

**Production recommendation:** both. RDB for fast restarts (binary, compact). AOF for minimal data loss on crash. `appendfsync everysec` for balance. This is Redis's own default recommendation.

### 4. Data Structures and Use Cases

| Structure | Commands | Use Case |
|---|---|---|
| String | GET/SET/INCR | Simple cache, counters, distributed locks |
| Hash | HGET/HSET/HMGET | User profiles (field-level access) |
| List | LPUSH/RPOP/BLPOP | Work queues, activity feeds (append-only) |
| Set | SADD/SMEMBERS/SINTER | Unique tags, social graph (who follows whom) |
| Sorted Set | ZADD/ZRANGE/ZRANGEBYSCORE | Leaderboards, priority queues, rate limiting |
| Stream | XADD/XREAD/XACK | Durable message log (like Kafka lite) |
| HyperLogLog | PFADD/PFCOUNT | Approximate unique count (1TB of unique IPs → 12KB) |
| Bloom Filter | BF.ADD/BF.EXISTS | "Have we seen this URL?" (probabilistic set) |

**Sorted Set internals:** implemented as a zip list (small sizes) or a skip list + hash table (large sizes). Skip list gives O(log N) for ZADD/ZRANGE. Hash table gives O(1) for ZSCORE. Discord uses sorted sets for their "online members" indicator (score = last-seen timestamp).

**HyperLogLog:** 12KB of memory for any cardinality estimate with < 1% error. Twitter uses HyperLogLog to count unique impressions per tweet without storing 1TB of user IDs.

### 5. Cluster Resharding: Hash Slot Migration with Zero Downtime

When adding a new node to a Redis Cluster, hash slots must be migrated from existing nodes to the new node. Redis does this with zero downtime:

1. **Command:** `redis-cli --cluster reshard <host>:<port>`
2. **Migrate slot:** for each slot being moved, Redis migrates keys one-by-one using `MIGRATE` command
3. **ASK redirect:** during migration, commands for the migrating slot return an `ASK` redirect to the destination node. Client retries on the new node with `ASKING` prefix.
4. **MOVED redirect:** once all keys are migrated, the slot ownership is updated in the cluster config. All future commands for that slot get `MOVED` to the new node.
5. **Client update:** clients cache slot→node mappings and refresh on `MOVED` responses.

Key insight: migration is done key-by-key, not slot-at-a-time. A slot with 1M keys takes minutes to migrate; an empty slot takes milliseconds. During migration, the source node handles commands that arrive before the key is migrated; the destination handles commands after.

---

## How It Actually Works

**Twitter's 40TB Redis** (QCon 2014 talk by Yao Yu): Twitter runs Redis for timeline caching, session storage, and rate limiting. They use `allkeys-lfu` policy for timeline caches (viral tweets are accessed constantly and should never be evicted). They run thousands of Redis instances across multiple data centers, with automatic failover via Zookeeper.

**Discord's "How Discord Stores Billions of Messages"** (blog post, 2017): Discord initially used Redis for message storage. Redis sorted sets stored message IDs per channel (ZADD); message content in Redis hashes. As Discord grew to 100M daily active users, they migrated to Cassandra for long-term message storage while keeping Redis for the "hot" recent messages (last 50 per channel). Their Redis cluster processes 5M messages/second using pipelining and connection pooling.

Key insight from both: Redis's bottleneck at scale is network throughput and connection count, not CPU. Pipelining and connection pooling are the first optimizations to make.

Source: Yao Yu, "Redis at Twitter" (QCon London 2014); Discord Engineering Blog "How Discord Stores Billions of Messages" (2017); Redis official documentation on cluster architecture.

---

## Hands-on Lab

**Time:** ~20 minutes
**Services:** `redis-1`, `redis-2`, `redis-3` (Redis Cluster), `cluster-init` (one-shot setup)

### Setup

```bash
cd system-design-interview/03-case-studies/11-distributed-cache/
docker compose up -d
# Wait ~15s for cluster-init to finish
docker compose logs cluster-init  # should see "All 16384 slots covered"
docker compose ps
```

### Experiment

```bash
# Run from host (requires pip install redis)
pip install redis
python experiment.py

# Or inside a container:
docker run --rm --network 11-distributed-cache_default \
  python:3.11-slim sh -c "pip install redis --quiet && python /experiment.py"
```

The script runs 6 phases:

1. **Pipeline:** 1000 individual GETs vs 1000 pipelined GETs — show speedup
2. **Eviction:** fill redis-1 past maxmemory=2MB → show LRU eviction
3. **Pub/sub:** 3 subscribers, publisher sends 20 messages — all receive all
4. **Distributed lock:** acquire/release/contest with Lua script
5. **RDB snapshot:** BGSAVE timing with 5,000 pre-filled keys
6. **Cluster slots:** show hash slot → node mapping for sample keys

### Break It

**Force a pipeline failure:**

```bash
# Kill redis-1 mid-pipeline
docker compose stop redis-1
# Run a pipeline targeting keys on redis-1's slots
# Observe CLUSTERDOWN or MOVED error
docker compose start redis-1
```

**Observe memory eviction in real time:**

```bash
# Watch evicted_keys counter
watch -n1 "docker compose exec redis-1 redis-cli -p 7001 info stats | grep evicted"
```

**Test cluster reshard:**

```bash
# Check current slot distribution
docker compose exec redis-1 redis-cli -p 7001 cluster nodes

# Count keys per node
docker compose exec redis-1 redis-cli -p 7001 cluster info | grep cluster_stats_messages
```

### Observe

```bash
# Cluster health
docker compose exec redis-1 redis-cli -p 7001 cluster info | grep -E "state|slots|nodes"

# Memory usage per node
for port in 7001 7002 7003; do
  echo "Port $port:"
  docker compose exec redis-1 redis-cli -p $port info memory | grep used_memory_human
done

# Replication info
docker compose exec redis-1 redis-cli -p 7001 info replication
```

### Teardown

```bash
docker compose down -v
```

---

## Interview Checklist

1. **Q: Why is Redis fast despite being single-threaded?**
   A: Single-threaded eliminates lock contention — no mutex, no CAS, no cache-line bouncing between cores. All data structure operations run in a tight event loop (epoll). The bottleneck is rarely CPU; it's network I/O. Redis 6+ added I/O threading to parallelize network reads/writes, while keeping command execution single-threaded for correctness. In-memory access patterns also maximize CPU cache locality.

2. **Q: When would you use LFU over LRU for cache eviction?**
   A: LFU (Least Frequently Used) is better when access patterns have clear hot/cold splits and the hot keys are not necessarily the most recently accessed. Example: a viral tweet from 3 days ago is accessed millions of times — LRU would eventually evict it as newer but less popular keys arrive; LFU keeps it cached because its frequency is high. Twitter uses LFU for timeline caches for exactly this reason.

3. **Q: What is the difference between RDB and AOF persistence? Which should you use?**
   A: RDB is a periodic point-in-time snapshot (compact binary file, fast restart). AOF logs every write command (larger file, slower restart, less data loss). Data loss risk: RDB can lose minutes of data; AOF with `appendfsync everysec` loses at most 1 second. Production recommendation: use both — RDB for fast recovery, AOF for minimal data loss. Redis documentation explicitly recommends this combination.

4. **Q: How does Redis Cluster route a key to the right node?**
   A: `slot = CRC16(key) % 16384`. Each node owns a range of slots. The cluster client downloads the slot→node mapping on connect and caches it locally. Requests go directly to the right node (no proxy). If the node returns `MOVED {slot} {host:port}`, the client refreshes its mapping and retries. If it returns `ASK`, a slot migration is in progress — the client retries with `ASKING` on the destination node.

5. **Q: How does pipelining improve Redis throughput?**
   A: Without pipelining: each command incurs one network round-trip (~0.1–1ms). 1000 commands = 100–1000ms of pure network overhead. With pipelining: all 1000 commands are sent in one TCP packet, Redis processes them sequentially, and returns all responses in one packet. The 1000 round trips become 1, giving 100-1000× throughput improvement. Caveat: pipeline is not transactional (partial execution on failure).

6. **Q: What is a Redis Cluster hash tag and why do you need it?**
   A: `{tag}` in a key name forces the key to hash to the same slot as all other keys with the same tag. Example: `{user:1}.profile` and `{user:1}.settings` both hash the `user:1` part → same slot → same node. This is required for multi-key operations (MGET, MSET, transactions) in cluster mode. Keys on different slots cannot be used in the same pipeline transaction.

7. **Q: How does Redis handle a master failure in cluster mode?**
   A: Each master has one or more replicas. If a master is unreachable for `cluster-node-timeout` milliseconds (default 15s), the cluster starts an election. The replica with the most up-to-date replication offset wins and is promoted. Slots owned by the failed master are now served by the new master. The cluster enters `cluster_state: ok` once the replica takes over. Writes during the failover window (~15s) may be lost.

8. **Q: What is the SCAN command and why is it preferred over KEYS *?**
   A: `KEYS *` scans the entire keyspace in one O(N) command — blocks the Redis event loop for the full duration. On a 10M-key instance this can block all other commands for seconds. `SCAN cursor [MATCH pattern] [COUNT hint]` iterates the keyspace in small increments (default 10 keys per call), returning a cursor for the next iteration. Non-blocking, safe for production. Tradeoff: may return duplicate keys across calls (during resize events).

9. **Q: How would you implement a leaderboard at Twitter scale?**
   A: Redis Sorted Set. `ZADD leaderboard {score} {user_id}`. `ZREVRANGE leaderboard 0 99` returns top 100 users. `ZRANK leaderboard {user_id}` gives the user's rank in O(log N). Sorted sets use a skip list internally — O(log N) for insert and rank lookup. For Twitter scale (100M users), partition into 100 sorted sets by user_id prefix and aggregate the top-K from each.

10. **Q: What is HyperLogLog and when would you use it for caching?**
    A: HyperLogLog is a probabilistic cardinality estimator. `PFADD` adds an item; `PFCOUNT` returns an estimate of unique items seen. Memory: exactly 12KB regardless of cardinality. Error rate: < 1%. Use case: count unique views per video at YouTube scale. Storing 1B distinct user IDs in a set would take ~8GB; HyperLogLog estimates the same count in 12KB. The trade-off is approximation — not suitable for billing, suitable for analytics.
