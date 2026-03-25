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

**Hash-based sharding** applies a hash function to the shard key and takes it modulo N (the number of shards): `shard = hash(user_id) % N`. This distributes rows evenly across shards when user IDs are numerous and random. It has no concept of range: consecutive user IDs land on different shards based on their hash values, so a query like "find users with IDs 100-200" must query all N shards. Hash sharding is the default for most NoSQL systems (MongoDB's hashed sharding, Redis Cluster's CRC16 hash slot approach).

**Range-based sharding** assigns contiguous key ranges to shards: shard-0 holds user_id 1–10M, shard-1 holds 10M–20M, etc. Range queries are efficient — they hit only the shards whose range overlaps the query. The danger is hotspots: if user IDs are assigned sequentially (auto-increment primary keys), all new writes go to the last shard. This shard becomes a hotspot while all earlier shards are idle. Solutions include salting the key (prepend a random prefix), using UUIDs, or pre-splitting ranges.

**Consistent hashing** is a hash-based approach designed to minimize data movement during resharding. Rather than `hash(key) % N`, keys and shards are both placed on a conceptual ring (a 360-degree circle). Each key is assigned to the nearest shard clockwise on the ring. When a shard is added or removed, only the keys adjacent to it on the ring are reassigned — about 1/N keys total. This is a major improvement over naive hash sharding, where adding one shard invalidates all routing for all keys. Consistent hashing is used in Amazon DynamoDB, Apache Cassandra, and Redis Cluster (via hash slots, a practical approximation).

**Directory-based sharding** maintains a lookup table mapping key ranges or specific keys to shards. This is maximally flexible — you can move individual tenants between shards, create shards of different sizes, and change the mapping without resharding data. The cost is the lookup overhead on every operation and the directory becoming a single point of failure. Used in Facebook's TAO and various multi-tenant SaaS databases.

### Trade-offs

| Sharding Approach | Query Efficiency | Balance | Resharding Cost | Complexity |
|-------------------|-----------------|---------|-----------------|------------|
| Hash-based | Range queries require all shards | Excellent | High (all keys move) | Low |
| Range-based | Range queries hit 1 shard | Poor (hotspots) | Medium | Low |
| Consistent hash | Range queries require all shards | Good | Low (~1/N keys move) | Medium |
| Directory-based | Any pattern | Flexible | Low | High |

### Failure Modes

**Cross-shard query explosion:** a query that filters on a non-shard-key column (e.g., `WHERE name = 'Alice'`) must be sent to all shards (scatter) and results merged in the application (gather). At 100 shards, a cross-shard query generates 100 parallel DB queries. This is often the biggest operational surprise after deploying sharding.

**Hotspot creation:** even with hash sharding, certain keys can become hot if the access pattern is skewed. A viral post, a celebrity user, or a frequently updated configuration record can overwhelm a single shard. Solutions: split the hot key across multiple shards (key + replica suffix), use application-level caching, or use a specialized storage tier for the hot key.

**Resharding nightmare:** changing the number of shards with naive hash sharding requires migrating nearly all data. A 3-shard to 4-shard migration means that `hash(id) % 4` produces different shard assignments for almost every key compared to `hash(id) % 3`. The migration must be done live (double-write to old and new shards, then switch over) or with downtime. This is why consistent hashing is preferred for systems that must scale incrementally.

## Interview Talking Points

- "Don't shard until you have to — read replicas + caching handle most workloads, and sharding adds enormous operational complexity"
- "Hash sharding distributes evenly but prevents range queries; range sharding enables range queries but creates hotspots with sequential keys"
- "Cross-shard queries require scatter-gather — every shard must be queried in parallel and results merged in the application layer"
- "Consistent hashing minimizes data movement during resharding — only 1/N keys move when adding a shard, vs nearly all keys with naive modulo hashing"
- "The shard key must be chosen for access pattern alignment — if 90% of queries filter by user_id, shard by user_id to avoid cross-shard queries"

## Hands-on Lab

**Time:** ~20-30 minutes
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

The script runs five phases: hash sharding 1000 users and showing distribution uniformity (~333 per shard), direct lookup by user_id through the hash router, range sharding the same dataset, demonstrating the hotspot with sequential new user IDs, and scatter-gather cross-shard query for users named "Alice."

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

The distribution phase should show roughly equal distribution (within ~5%) across shards when using hash routing. The range routing phase will show exactly 333/333/334 for IDs 1-1000. The hotspot phase will show all new users (IDs 1001+) routing to shard-2.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **Instagram:** Started on a single PostgreSQL instance, then sharded by user_id when approaching limits. Their shard count grew to thousands. They documented that 95%+ of queries are by user_id, making user_id an excellent shard key — source: Instagram Engineering, "Sharding & IDs at Instagram" (2012).
- **Vitess (YouTube/PlanetScale):** A MySQL sharding middleware that handles shard routing, resharding, and cross-shard queries transparently. Powers YouTube's MySQL infrastructure. Cross-shard queries are detected at parse time and executed as scatter-gather — source: Sugu Sougoumarane, "Vitess: Scaling MySQL" (YouTube Engineering).
- **MongoDB:** Supports both hashed and range-based sharding. The config server (a replicated metadata store) holds the chunk map (shard key ranges → shard). The `mongos` router queries the config server to route each operation — source: MongoDB Docs, "Sharding Architecture."

## Common Mistakes

- **Choosing a low-cardinality shard key.** Sharding by `country` with 3 shards for 200 countries means most shards get many countries, but a large country like the US may dominate one shard. Choose high-cardinality keys (user_id, order_id) that distribute load evenly.
- **Sharding too early.** Sharding before you need it is pure complexity cost with no benefit. Benchmark your single-instance PostgreSQL first — a well-tuned single Postgres instance can handle tens of thousands of writes/second. Don't shard until benchmarks show you've hit the limit.
- **Forgetting cross-shard transactions.** Sharding makes ACID transactions that span multiple shards extremely difficult — they require two-phase commit (2PC), which is slow and error-prone. Design the data model so that transactional operations stay within one shard.
- **Using naive modulo hashing when incremental scaling is expected.** If you plan to add shards as you grow, use consistent hashing from the start. Migrating from modulo hash to consistent hash later requires rehashing all data.
