# Partitioning & Sharding

**Prerequisites:** `../04-replication/`
**Next:** `../06-caching/`

---

## Concept

Partitioning is the division of a large dataset into smaller, more manageable pieces called partitions (or shards). When partitioning is done horizontally — splitting rows across multiple database instances — it is called **sharding**. Each shard holds a disjoint subset of the data and operates as an independent database. Sharding is the primary mechanism for scaling writes and storage beyond the capacity of a single machine, but it introduces substantial complexity and should only be reached for when simpler approaches (vertical scaling, read replicas, caching) have been exhausted.

**Vertical partitioning** splits a table by columns: the frequently accessed "hot" columns (id, name, email) live in one table, while "cold" columns (large JSON blobs, rarely read fields) live in another, possibly on a slower storage tier. This reduces row width, fits more rows per page into buffer cache, and can dramatically improve query performance on wide tables. It does not require multiple database instances.

**Horizontal partitioning (sharding)** splits a table by rows. A shard key (a column or combination of columns) determines which shard a given row belongs to. Every write and read must first be routed to the correct shard. If the routing logic is wrong, data ends up on the wrong shard and queries return incorrect results. The sharding scheme must be chosen carefully because changing it later is extraordinarily painful — it requires migrating all data to new shards while the system is live.

The right time to shard is when you have genuinely exhausted all other options. Read-heavy workloads should use read replicas and caching first. Write-heavy workloads should examine whether the writes can be batched, queued, or restructured before sharding. Most companies don't need sharding until they have tens of millions of users and terabytes of data. Instagram ran on a single PostgreSQL instance until it had 5 million users; they only added sharding when their single Postgres instance approached its write limits.

## How It Works

**Query Routing to the Correct Shard (Hash-Based):**
1. Client sends a query containing a shard key (e.g., `user_id = 42`)
2. Application layer (or routing proxy) computes the target shard: `shard = hash(user_id) % N` where N is the number of shards
3. Router consults its shard map to identify which database host owns that shard number
4. Query is forwarded directly to the identified shard host — no other shards are involved
5. Shard executes the query against its local data subset and returns the result
6. **Cross-shard query (non-shard-key filter):** router sends the query to ALL N shards in parallel (scatter), waits for all responses, merges and sorts results in the application (gather) — expensive at scale

**Hash-based sharding** applies a deterministic hash function to the shard key and takes it modulo N (the number of shards): `shard = hash(user_id) % N`. This distributes rows evenly across shards when user IDs are numerous and random. It has no concept of range: consecutive user IDs land on different shards based on their hash values, so a query like "find users with IDs 100-200" must query all N shards.

Critical: the hash function must be deterministic across all processes and languages. Python's built-in `hash()` is randomized per-process via `PYTHONHASHSEED` and cannot be used for shard routing — different restarts would route the same key to different shards. Production systems use SHA-256, MurmurHash, or xxHash. Hash sharding is the default for most NoSQL systems (MongoDB's hashed sharding, Redis Cluster's CRC16 hash slot approach).

**Range-based sharding** assigns contiguous key ranges to shards: shard-0 holds user_id 1–10M, shard-1 holds 10M–20M, etc. Range queries are efficient — they hit only the shards whose range overlaps the query. The danger is hotspots: if user IDs are assigned sequentially (auto-increment primary keys), all new writes go to the last shard. This shard becomes a hotspot while all earlier shards are idle. Solutions include salting the key (prepend a random prefix), using UUIDs, or pre-splitting ranges.

**Consistent hashing** is a hash-based approach designed to minimize data movement during resharding. Rather than `hash(key) % N`, keys and shards are both placed on a conceptual ring (the integer range [0, 2^256)). Each key is assigned to the nearest shard clockwise on the ring. When a shard is added or removed, only the keys adjacent to it on the ring are reassigned — about 1/N keys total. This is a major improvement over naive hash sharding, where adding one shard invalidates routing for ~(N-1)/N keys — nearly all of them at scale.

**Virtual nodes (vnodes)** improve balance in consistent hashing. With a small number of physical shards (e.g., 3), placing only one ring position per shard results in uneven arcs — one shard might own 50% of the ring, another only 20%. Virtual nodes solve this: each physical shard is assigned many ring positions (typically 100–300). The many positions distribute load more evenly, similar to how more coin flips converge to 50/50. Apache Cassandra defaults to 256 vnodes per node.

**Directory-based sharding** maintains a lookup table mapping key ranges or specific keys to shards. This is maximally flexible — you can move individual tenants between shards, create shards of different sizes, and change the mapping without resharding data. The cost is the lookup overhead on every operation and the directory becoming a single point of failure. Used in Facebook's TAO and various multi-tenant SaaS databases.

**Logical shards vs. physical shards** is a production pattern that decouples the routing key space from the physical host count. Instead of routing to `hash(key) % 3_hosts`, you route to `hash(key) % 1024_logical_shards`, and then a separate mapping table routes each logical shard to a physical host. Resharding becomes a metadata operation: update the logical-to-physical mapping to redistribute logical shards across more hosts. No key rehashing required. Used by Discord (Cassandra), DynamoDB, and Redis Cluster (16384 hash slots).

### Trade-offs

| Sharding Approach | Range Query Efficiency | Balance | Resharding Cost | Complexity |
|-------------------|------------------------|---------|-----------------|------------|
| Hash-based (modulo) | All shards required | Excellent | Very High (~(N-1)/N keys move) | Low |
| Range-based | One shard only | Poor (hotspots on sequential keys) | Medium | Low |
| Consistent hash | All shards required | Good (improves with vnodes) | Low (~1/N keys move) | Medium |
| Consistent hash + vnodes | All shards required | Excellent | Low (~1/N keys move) | Medium-High |
| Directory-based | Depends on index | Flexible | Low | High |
| Logical shards | All logical shards in range | Excellent | Very Low (metadata only) | High |

### Failure Modes

**Cross-shard query explosion:** a query that filters on a non-shard-key column (e.g., `WHERE name = 'Alice'`) must be sent to all shards (scatter) and results merged in the application (gather). At 100 shards, a cross-shard query generates 100 parallel DB queries — and parallel latency equals the latency of the *slowest* shard. A single slow or overloaded shard stalls the entire scatter-gather operation. This is often the biggest operational surprise after deploying sharding.

**Hotspot creation:** even with hash sharding, certain keys can become hot if the access pattern is skewed. A viral post, a celebrity user, or a frequently updated configuration record can overwhelm a single shard. Solutions: split the hot key across multiple shards (key + replica suffix), use application-level caching, or use a specialized storage tier for the hot key.

**Resharding nightmare:** changing the number of shards with naive hash sharding requires migrating nearly all data. A 3-shard to 4-shard migration means that `hash(id) % 4` produces different shard assignments for almost every key compared to `hash(id) % 3` — empirically around 75% of keys move. The migration must be done live (double-write to old and new shards, then switch over) or with downtime. This is why consistent hashing or logical shards are preferred for systems that must scale incrementally.

**Cross-shard transactions:** sharding breaks ACID transactions that span multiple shards. Two-phase commit (2PC) can restore atomicity across shards, but 2PC is slow (two round-trips + coordinator log flush), blocks resources during the commit phase, and fails ungracefully if the coordinator crashes during the commit window. Most sharded systems avoid cross-shard writes entirely by design, or use sagas with compensating transactions.

**Shard key immutability:** the shard key determines which shard a row lives on. If a row's shard key value changes (e.g., a user changes their username and username is the shard key), the row must be deleted from the old shard and inserted into the new shard — not updated in-place. Failing to handle this results in the row living on the wrong shard, causing query misses or duplicate data.

## Interview Talking Points

- "Don't shard until you have to — read replicas + caching handle most workloads, and sharding adds enormous operational complexity. A well-tuned single Postgres instance handles tens of thousands of writes per second."
- "Hash sharding distributes evenly but prevents efficient range queries; range sharding enables range queries but creates write hotspots with sequential keys like auto-increment IDs."
- "Cross-shard queries require scatter-gather — every shard must be queried, results merged in the application. Parallel scatter latency equals max(shard latencies), not the average. One slow shard stalls everything."
- "Consistent hashing minimizes data movement during resharding — only 1/N keys move when adding a shard, vs ~(N-1)/N with naive modulo hashing. Virtual nodes improve balance with few physical shards."
- "The shard key must be chosen for access pattern alignment — if 90% of queries filter by user_id, shard by user_id to avoid cross-shard queries. Low-cardinality shard keys (country, status) create imbalanced shards."
- "Cross-shard ACID transactions require 2-phase commit, which is slow and fragile. Design data models so transactional boundaries stay within a single shard — shard by the transaction aggregate root."
- "Logical shards decouple the key space from the physical host count. Route to `hash(key) % 1024` logical shards, then map logical to physical. Resharding becomes a metadata update — no key rehashing."
- "The shard key must be immutable. If it changes, the row moves to a different shard — this is a delete + insert, not an update. Keys like email or username are dangerous shard keys for this reason."

## Hands-on Lab

**Time:** ~25-35 minutes
**Services:** shard-0 (5432), shard-1 (5433), shard-2 (5434)

### Setup

```bash
cd system-design-interview/01-foundations/05-partitioning-sharding/
docker compose up -d
# Wait ~15 seconds for all 3 shard instances to be ready
```

### Experiment

```bash
python experiment.py
```

The script runs six phases:
1. **Hash sharding** 1000 users using SHA-256 (not Python's `hash()`) — shows why deterministic hashing matters
2. **Single-key lookup** — shows O(1) routing to exactly one shard
3. **Range sharding** the same dataset — shows exact distribution with static data
4. **Hotspot demonstration** — inserts new sequential IDs and shows they all go to shard-2
5. **Cross-shard scatter-gather** — compares sequential vs parallel query execution and measures the speedup
6. **Resharding cost comparison** — measures how many of 10,000 keys change shard when going from 3 to 4 shards, comparing modulo hashing vs consistent hashing with varying vnode counts

### Break It

```bash
# Stop shard-1 and attempt a write that routes to it
docker compose stop shard-1

python -c "
import psycopg2

# Connect to the two remaining shards
shard0 = psycopg2.connect('host=localhost port=5432 dbname=shard0 user=postgres password=postgres')
shard0.autocommit = True
shard2 = psycopg2.connect('host=localhost port=5434 dbname=shard2 user=postgres password=postgres')
shard2.autocommit = True

print('What happens to writes destined for shard-1?')
print('Options:')
print('  1. Fail the write (CP behavior — consistent but unavailable)')
print('  2. Write to a different shard (breaks routing invariant)')
print('  3. Buffer the write and retry when shard-1 recovers (queue-based)')
print('  4. Use consistent hashing to reroute to adjacent shard')
"

docker compose start shard-1
```

### Observe

- Phase 1 distribution should show roughly equal distribution (within ~5%) across shards when using SHA-256 hash routing.
- Phase 3 range routing will show exactly 333/333/334 for IDs 1-1000.
- Phase 4 hotspot will show all new users (IDs 1001+) routing to shard-2.
- Phase 5 parallel scatter-gather should be measurably faster than sequential.
- Phase 6 modulo hashing will show ~75% of keys move when adding a 4th shard; consistent hashing will show ~25%.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **Instagram:** Started on a single PostgreSQL instance, then sharded by user_id when approaching limits. Their shard count grew to thousands. They documented that 95%+ of queries are by user_id, making user_id an excellent shard key — source: Instagram Engineering, "Sharding & IDs at Instagram" (2012).
- **Vitess (YouTube/PlanetScale):** A MySQL sharding middleware that handles shard routing, resharding, and cross-shard queries transparently. Powers YouTube's MySQL infrastructure. Cross-shard queries are detected at parse time and executed as scatter-gather — source: Sugu Sougoumarane, "Vitess: Scaling MySQL" (YouTube Engineering).
- **MongoDB:** Supports both hashed and range-based sharding. The config server (a replicated metadata store) holds the chunk map (shard key ranges → shard). The `mongos` router queries the config server to route each operation — source: MongoDB Docs, "Sharding Architecture."
- **Discord:** Uses Cassandra with consistent hashing for message storage. Channel IDs are the partition key; messages within a channel are clustered by timestamp. A single channel is always on one shard, avoiding cross-shard queries for the most common access pattern (fetch latest messages in channel).
- **Redis Cluster:** Uses 16384 hash slots as logical shards. `CRC16(key) % 16384` gives the slot; the cluster maps slot ranges to nodes. Resharding is a slot migration — data is moved slot by slot between nodes with no downtime. Clients cache the slot map and update it when they get MOVED errors.

## Common Mistakes

- **Using a non-deterministic hash function.** Python's `hash()` is randomized per-process (PYTHONHASHSEED). Different processes or restarts route the same key to different shards. Use SHA-256, MurmurHash, or CRC32 — anything deterministic and language-portable.
- **Choosing a low-cardinality shard key.** Sharding by `country` with 3 shards means the US shard gets far more traffic than the Liechtenstein shard. Choose high-cardinality keys (user_id, order_id) that distribute load evenly.
- **Sharding too early.** Sharding before you need it is pure complexity cost with no benefit. Benchmark your single-instance PostgreSQL first — a well-tuned single Postgres instance can handle tens of thousands of writes/second. Don't shard until benchmarks show you've hit the limit.
- **Forgetting cross-shard transactions.** Sharding makes ACID transactions that span multiple shards extremely difficult — they require two-phase commit (2PC), which is slow and error-prone. Design the data model so that transactional operations stay within one shard.
- **Using naive modulo hashing when incremental scaling is expected.** If you plan to add shards as you grow, use consistent hashing or logical shards from the start. Migrating from modulo hash to consistent hash later requires rehashing all data.
- **Mutable shard keys.** If the shard key value can change (email, username, status), the row must be physically moved between shards on update. Use stable, immutable keys (surrogate IDs, UUIDs) as shard keys to avoid this class of bug.
- **Ignoring the logical shard layer.** Jumping straight from "3 physical hosts" to "hash % 3" locks you in. Starting with "1024 logical shards → 3 physical hosts" makes future rebalancing a metadata change rather than a data migration.
