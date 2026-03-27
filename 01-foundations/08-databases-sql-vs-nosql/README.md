# Databases — SQL vs NoSQL

**Prerequisites:** `../07-load-balancing/`
**Next:** `../09-indexes/`

---

## Concept

The choice between relational and non-relational databases is one of the most consequential architectural decisions in system design. It shapes how data is stored, queried, scaled, and kept consistent. The decision is not "SQL is better" or "NoSQL is faster" — it is "which storage model fits the access patterns, consistency requirements, and scale of this specific problem?"

**Relational databases** (PostgreSQL, MySQL, Oracle) store data in tables with rows and columns, enforce a strict schema, and support SQL — a declarative query language that can express complex multi-table operations in a single statement. Their defining property is ACID: Atomicity (a transaction either fully commits or fully rolls back), Consistency (constraints like foreign keys and UNIQUE are always enforced), Isolation (concurrent transactions see consistent state), and Durability (committed data survives crashes). These guarantees make relational databases the default choice for financial systems, inventory management, and any domain where correctness is non-negotiable.

**Document databases** (MongoDB, CouchDB, Firestore) store data as self-contained JSON-like documents. A "user with posts" is one document — no JOIN required. This maps naturally to how application code works: an HTTP API that returns a user with their embedded posts can fetch a single document rather than executing a JOIN across two tables. Document databases are schema-flexible: different documents in the same collection can have different fields, enabling schema evolution without migration scripts. The trade-off is relaxed consistency: multi-document transactions are expensive and were an afterthought in most document databases. Referential integrity (foreign keys) must be enforced in application code.

**Key-value stores** (Redis, DynamoDB, Memcached) are the simplest data model: every value has a key, and you look up values by key. There is no query language — you can't ask "find all users where age > 30" without scanning every key. What you get instead is extreme speed: Redis reads and writes in under a millisecond because all data lives in RAM, and the data structures (Hash, List, Set, Sorted Set) are optimized for specific access patterns. Redis is the right tool for caching, session storage, leaderboards, rate limiting, and pub/sub messaging — not for replacing a relational database.

**Wide-column stores** (Cassandra, HBase, Google Bigtable) are optimized for massive write throughput and time-series data. Data is organized by a partition key that determines physical storage location. All reads and writes for a partition key go to the same node — no cross-node coordination, which is why Cassandra can sustain millions of writes per second across a cluster. The price is limited query flexibility: you must know the partition key upfront. "Show all posts from user 42 in the last hour" is fast if partition key is user_id. "Show all posts from all users in city=NYC" requires a full cluster scan unless city is also a partition key.

## How It Works

**SQL Read Path — JOIN Query:**
1. Client issues `SELECT u.name, p.title FROM users u JOIN posts p ON p.user_id = u.id WHERE u.id = 42`
2. Query planner analyses table statistics and chooses a join strategy (nested loop, hash join, or merge join)
3. B-tree index scan on `users.id = 42` retrieves the single user row in O(log n)
4. Index scan on `posts.user_id = 42` retrieves all matching post rows
5. Database assembles the joined result set, applies any ORDER BY / LIMIT, and returns it

**Document (NoSQL) Read Path:**
1. Client issues `db.users.findOne({_id: 42})`
2. Database fetches the single document by ID — a direct key lookup
3. The entire user record including embedded posts is returned in one round trip: `{id: 42, name: "Alice", posts: [{...}, {...}]}`
4. No JOIN needed; trade-off: updating a single nested post requires fetching, modifying, and rewriting the full document

### The Relational Model

A relational schema normalizes data to eliminate redundancy. A "user with posts" becomes two tables: `users` and `posts`, connected by a foreign key. This means each user's name and email is stored exactly once. A JOIN query at read time reconstructs the relationship. The query planner uses indexes, statistics, and cost estimation to find the optimal execution plan. For complex queries touching millions of rows, the query planner's ability to choose between nested loop, hash join, and merge join can be the difference between a 50ms query and a 50-second query.

### The Document Model

A document database stores a user's posts embedded directly in the user document. There is no JOIN — one document fetch returns the complete user+posts tree. This is optimal for read-heavy access where the unit of access matches the unit of storage. The challenge appears on writes: updating a single post requires fetching the entire document, modifying it, and writing it back. For users with thousands of posts, this becomes expensive. The solution is a "referenced" document model (post documents in a separate collection with a user_id field), which re-introduces the JOIN problem — MongoDB calls this `$lookup`.

### ACID vs BASE

ACID is the relational model's consistency guarantee: every transaction is all-or-nothing, and after committing, data is permanently written.

BASE (Basically Available, Soft state, Eventually consistent) is the NoSQL trade-off. Cassandra and DynamoDB prioritize availability over consistency: a write acknowledged by one node will eventually propagate to all replicas, but reads may temporarily see old values. The reward is that writes never block waiting for cross-datacenter coordination, making BASE systems sustain much higher write throughput than ACID systems.

### NewSQL

NewSQL databases (CockroachDB, Google Spanner, YugabyteDB) attempt to provide SQL semantics and ACID transactions with horizontal scalability. CockroachDB uses the Raft consensus algorithm to keep replicas consistent, distributes data across nodes using range-based sharding, and supports standard SQL. Google Spanner uses TrueTime — atomic clocks in every Google datacenter — to provide globally consistent reads without coordination latency. These systems sacrifice some write throughput compared to pure AP systems, but provide ACID guarantees at distributed scale.

### Trade-offs

| Database Type | Consistency | Scale-out | Query Power | Schema | Best For |
|---------------|-------------|-----------|-------------|--------|----------|
| Relational (Postgres) | ACID | Hard (sharding) | Full SQL | Strict | Transactions, complex queries |
| Document (MongoDB) | Eventual (tunable) | Built-in sharding | Limited | Flexible | Hierarchical data, rapid iteration |
| Key-value (Redis) | Eventual (tunable) | Cluster mode | None (key lookup only) | None | Caching, sessions, real-time |
| Wide-column (Cassandra) | Eventual (tunable) | Built-in | Partition key only | Flexible | Time-series, high-write |
| Graph (Neo4j) | ACID | Limited | Cypher traversal | Flexible | Relationships, recommendations |
| Time-series (InfluxDB) | Strong within shard | Built-in | Time-range queries | Strict | Metrics, monitoring |
| NewSQL (CockroachDB) | ACID (distributed) | Built-in | Full SQL | Strict | ACID at scale |

### Failure Modes

**N+1 query problem:** fetching 100 users and then querying posts for each user separately results in 101 queries instead of 1. This is the most common performance problem in relational systems. Solution: JOIN or batch query. In document databases, embedding prevents N+1, but deep nesting creates oversized documents.

**Eventual consistency surprises:** writing a document to MongoDB and immediately reading it back may return the old value if the read goes to a replica that hasn't caught up. Applications must either read from the primary or tolerate briefly stale data. Most NoSQL databases let you tune the consistency level per operation (Cassandra's `QUORUM` read, DynamoDB's `ConsistentRead`).

**Schema migration on large tables:** adding a non-nullable column to a 500M-row Postgres table can lock the table for minutes. Production zero-downtime migrations require: add column as nullable → deploy new code that writes both old and new format → backfill in batches → add NOT NULL constraint. Tools like pglogical and pt-online-schema-change handle this.

**Document growth:** embedded arrays that grow without bound (a user's entire message history embedded in the user document) eventually hit the 16MB document size limit in MongoDB. Design for bounded document size by switching to a referenced model when arrays can grow large.

## Interview Talking Points

- "SQL vs NoSQL is the wrong question. The right question is: what are the access patterns, consistency requirements, and scale of this problem? Financial transactions need ACID → Postgres. Session storage needs speed → Redis. Time-series metrics need write throughput → InfluxDB or Cassandra."
- "Document databases don't eliminate the JOIN problem — they move it to write time (denormalization) vs read time (JOIN). Embedding means no JOIN on read, but expensive writes when embedded data changes."
- "BASE is a trade-off, not a bug. Cassandra's eventual consistency means a write that succeeds on one replica will propagate, but a read might briefly see old data. For most social app features (like counts), this is acceptable. For bank balances, it is not."
- "Polyglot persistence is the norm at scale. A production system uses Postgres for user accounts (ACID), Redis for sessions (speed), S3 for file storage (durability), and Elasticsearch for search (full-text). Use the right tool for each job."
- "NewSQL (CockroachDB, Spanner) provides ACID at horizontal scale, but at higher latency than eventual-consistent NoSQL. The latency cost comes from consensus protocol overhead (Raft/Paxos)."

## Hands-on Lab

**Time:** ~20-30 minutes
**Services:** postgres (5432), redis (6379), mongodb (27017)

### Setup

```bash
cd system-design-interview/01-foundations/08-databases-sql-vs-nosql/
docker compose up -d
# Wait ~15 seconds for all three databases to initialize
```

### Experiment

```bash
python experiment.py
```

The script seeds 100 users with 5 posts each in all three databases, benchmarks read/write latency, and demonstrates schema evolution trade-offs.

### Break It

Demonstrate the N+1 problem in Postgres:

```bash
python -c "
import psycopg2, time

conn = psycopg2.connect('host=localhost port=5432 dbname=dbtest user=postgres password=postgres')
conn.autocommit = True

# Bad: N+1 queries — one per user
start = time.perf_counter()
with conn.cursor() as cur:
    cur.execute('SELECT id FROM users LIMIT 20')
    user_ids = [r[0] for r in cur.fetchall()]
    for uid in user_ids:
        cur.execute('SELECT COUNT(*) FROM posts WHERE user_id = %s', (uid,))
        cur.fetchone()
n1_ms = (time.perf_counter() - start) * 1000

# Good: one JOIN query
start = time.perf_counter()
with conn.cursor() as cur:
    cur.execute('''
        SELECT u.id, COUNT(p.id) as post_count
        FROM users u LEFT JOIN posts p ON p.user_id = u.id
        GROUP BY u.id LIMIT 20
    ''')
    cur.fetchall()
join_ms = (time.perf_counter() - start) * 1000

print(f'N+1 queries (21 round-trips): {n1_ms:.1f}ms')
print(f'JOIN query  (1 round-trip):   {join_ms:.1f}ms')
print(f'JOIN is {n1_ms/join_ms:.1f}x faster')
conn.close()
"
```

### Observe

Expected benchmark output:
```
PostgreSQL write 100 users + 500 posts: ~50-200ms
MongoDB    write 100 embedded docs:     ~30-100ms
Redis      write 100 users + posts:     ~5-30ms

PostgreSQL read (JOIN) x100:     ~10-50ms  (avg ~0.3ms)
MongoDB    read (no JOIN) x100:  ~10-40ms  (avg ~0.3ms)
Redis      read (pipeline) x100: ~3-10ms   (avg ~0.05ms)
```

Redis is fastest because it's in-memory. MongoDB and Postgres perform comparably for simple reads. At scale, query complexity and indexing matter far more than the database choice itself.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **Instagram:** Started on Postgres for user data and Django ORM. As they scaled to hundreds of millions of users, they kept Postgres for the core relational data (users, follows, comments) but added Cassandra for the activity feed (time-series write-heavy), Redis for caching, and HBase for media metadata. Classic polyglot persistence — source: Instagram Engineering Blog, "What Powers Instagram: Hundreds of Instances, Dozens of Technologies" (2011).
- **Airbnb:** Uses MySQL as the primary relational store, with a custom sharding layer (Vitess). Their search uses Elasticsearch. Real-time messaging and notifications use Kafka + Redis. They documented the migration from a single MySQL instance to a globally distributed architecture — source: Airbnb Engineering Blog, "Scaling Airbnb's Payment Platform" (2018).
- **MongoDB Atlas:** MongoDB's own SaaS platform stores configuration and billing data in Postgres (ACID requirements), while operational data uses MongoDB. Even MongoDB uses a relational database where ACID matters — source: MongoDB World conference talk, 2019.

## Common Mistakes

- **Choosing NoSQL because it's "faster."** Redis is faster than Postgres for key lookups because it's in-memory, not because it's NoSQL. Postgres with proper indexes on an in-memory table (shared_buffers) can match MongoDB for simple lookups. The performance difference is usually the access pattern fit, not the database category.
- **Using a document database for relational data.** Modeling a "bank account with transactions" in MongoDB leads to either a growing embedded array (document size limit), or separate collections with app-managed foreign keys (losing referential integrity). Data with complex relationships belongs in a relational database.
- **Ignoring eventual consistency in NoSQL.** Reading from a Cassandra replica immediately after a write may return the old value. Applications must either use tunable consistency (QUORUM reads) for critical paths or be designed to tolerate brief staleness.
- **Not using transactions in Postgres.** Rolling back on error, referential integrity, and atomic multi-table updates are free with Postgres transactions. Developers who write separate INSERT statements without wrapping them in a transaction create partial-write bugs that are hard to diagnose.
- **Storing everything in Redis.** Redis is an in-memory data structure store. Using it as your primary database means all data must fit in RAM, and you lose the durability of disk-backed databases unless you configure AOF persistence with `appendfsync always` — which eliminates the performance advantage.
