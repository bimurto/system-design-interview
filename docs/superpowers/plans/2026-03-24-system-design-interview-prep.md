# System Design Interview Prep — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a comprehensive, hands-on system design interview preparation resource as 39 topic folders + root README, each with deep markdown content, working Docker Compose labs, and Python experiment scripts.

**Architecture:** Three-tier hierarchy (foundations → advanced → case studies), each topic in its own folder with `README.md`, `docker-compose.yml`, and `experiment.py`. Content follows a strict template. Labs are self-contained, runnable in ~20-30 minutes, max 5 containers.

**Tech Stack:** Docker Compose, Python 3.10+, Redis, PostgreSQL, Kafka, Zookeeper, Nginx, Prometheus, Grafana, etcd, Elasticsearch

**Spec:** `docs/superpowers/specs/2026-03-24-system-design-interview-prep-design.md`

---

## File Map

```
system-design-interview/
├── README.md
├── 01-foundations/
│   ├── 01-scalability/          README.md + docker-compose.yml + experiment.py
│   ├── 02-cap-theorem/
│   ├── 03-consistency-models/
│   ├── 04-replication/
│   ├── 05-partitioning-sharding/
│   ├── 06-caching/
│   ├── 07-load-balancing/
│   ├── 08-databases-sql-vs-nosql/
│   ├── 09-indexes/
│   ├── 10-networking-basics/
│   ├── 11-api-design/
│   └── 12-blob-object-storage/
├── 02-advanced/
│   ├── 01-consistent-hashing/
│   ├── 02-distributed-transactions/
│   ├── 03-consensus-paxos-raft/
│   ├── 04-event-driven-architecture/
│   ├── 05-message-queues-kafka/
│   ├── 06-stream-processing/
│   ├── 07-distributed-caching/
│   ├── 08-search-systems/
│   ├── 09-rate-limiting-algorithms/
│   ├── 10-cdn-and-edge/
│   ├── 11-observability/
│   ├── 12-security-at-scale/
│   ├── 13-service-discovery-coordination/
│   ├── 14-idempotency-exactly-once/
│   └── 15-probabilistic-data-structures/
└── 03-case-studies/
    ├── 01-url-shortener/
    ├── 02-twitter-timeline/
    ├── 03-youtube/
    ├── 04-uber/
    ├── 05-whatsapp/
    ├── 06-google-drive/
    ├── 07-web-crawler/
    ├── 08-search-engine/
    ├── 09-notification-system/
    ├── 10-rate-limiter/
    ├── 11-distributed-cache/
    └── 12-payment-system/
```

---

## Content Templates

### README.md Template — Foundations & Advanced

```markdown
# [Topic Name]

**Prerequisites:** [links to earlier topics]
**Next:** [link to next topic]

---

## Concept

[Deep explanation — what it is, why it matters, core idea]

## How It Works

[Internals — algorithms, data structures, protocols]

### Trade-offs

| Approach | Pros | Cons |
|----------|------|------|

### Failure Modes

[What breaks, when, and why]

## Interview Talking Points

- [Key phrase interviewers want to hear]
- [The trade-off to always mention]
- [The follow-up question you'll get]

## Hands-on Lab

**Time:** ~20-30 minutes
**Services:** [list]

### Setup

```bash
cd system-design-interview/[path]/
docker compose up -d
```

### Experiment

```bash
python experiment.py
```

[Explain what it demonstrates]

### Break It

```bash
docker compose stop [service]
# or
docker network disconnect [network] [container]
```

[What to observe]

### Observe

[What logs/output confirms the concept]

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **[Company]:** [how they use this — source: link to engineering blog/paper]

## Common Mistakes

- [Mistake 1 and why it's wrong]
- [Mistake 2]
```

### README.md Template — Case Studies

```markdown
# Case Study: [System Name]

**Prerequisites:** [links to foundations/advanced topics]

---

## The Problem at Scale

[Real numbers: QPS, storage, users, bandwidth]

## Requirements

### Functional
- [requirement 1]

### Non-Functional
- [availability, latency, consistency targets]

### Capacity Estimation

| Metric | Calculation | Result |
|--------|-------------|--------|

## High-Level Architecture

```
[ASCII diagram]
```

### Component Breakdown

[Each component: what it does, why this choice]

## Deep Dives

### [Hard Problem 1]
[Trade-offs, solution, alternatives considered]

### [Hard Problem 2]

### [Hard Problem 3]

## How It Actually Works

[Compare to real system — sourced from engineering blog/paper/talk]

## Hands-on Lab

**Time:** ~20-30 minutes
**Services:** [list — max 5]

### Setup
```bash
docker compose up -d
```

### Experiment
```bash
python experiment.py
```

### Break It
[failure scenario]

### Observe
[what to look for]

### Teardown
```bash
docker compose down -v
```

## Interview Checklist

1. Q: [question] — A: [answer]
...10 total
```

---

## Task 0: Scaffold + Root README

**Files:**
- Create: `system-design-interview/README.md`
- Create all topic directories (empty, with `.gitkeep`)

- [ ] **Step 1: Create directory scaffold**

```bash
mkdir -p system-design-interview
cd system-design-interview

# Foundations
for d in 01-scalability 02-cap-theorem 03-consistency-models 04-replication \
  05-partitioning-sharding 06-caching 07-load-balancing 08-databases-sql-vs-nosql \
  09-indexes 10-networking-basics 11-api-design 12-blob-object-storage; do
  mkdir -p 01-foundations/$d
done

# Advanced
for d in 01-consistent-hashing 02-distributed-transactions 03-consensus-paxos-raft \
  04-event-driven-architecture 05-message-queues-kafka 06-stream-processing \
  07-distributed-caching 08-search-systems 09-rate-limiting-algorithms \
  10-cdn-and-edge 11-observability 12-security-at-scale \
  13-service-discovery-coordination 14-idempotency-exactly-once \
  15-probabilistic-data-structures; do
  mkdir -p 02-advanced/$d
done

# Case Studies
for d in 01-url-shortener 02-twitter-timeline 03-youtube 04-uber 05-whatsapp \
  06-google-drive 07-web-crawler 08-search-engine 09-notification-system \
  10-rate-limiter 11-distributed-cache 12-payment-system; do
  mkdir -p 03-case-studies/$d
done
```

- [ ] **Step 2: Write root README.md**

`system-design-interview/README.md` — include:
- Environment prerequisites (Docker, Python 3.10+, pip packages: `redis psycopg2-binary kafka-python requests flask strawberry-graphql grpcio faust`)
- Recommended reading order (numbered, with estimated time per topic)
- How to use the labs (standard `docker compose up -d` → `python experiment.py` → `docker compose down -v` pattern)
- Quick-reference table linking all 39 topics with one-line descriptions

- [ ] **Step 3: Commit scaffold**

```bash
git add system-design-interview/
git commit -m "chore: scaffold system design interview prep structure"
```

---

## Task 1: F01 — Scalability

**Folder:** `system-design-interview/01-foundations/01-scalability/`
**Lab:** Nginx load balancer + 3 Python Flask app replicas. Demonstrate vertical vs horizontal scaling. Show how adding replicas increases throughput.
**Docker images:** `nginx:alpine`, `python:3.11-slim`
**Key content:** Vertical vs horizontal scaling, stateless vs stateful services, scale-up limits, auto-scaling concepts, load balancer types, CAP implications of scaling.

- [ ] **Step 1: Write README.md** following the Foundations template. Cover: vertical vs horizontal scaling, stateless design, database scaling bottlenecks, the "shared nothing" architecture. Interview talking points: always mention stateless services, database as bottleneck, read replicas before sharding.

- [ ] **Step 2: Write docker-compose.yml**

```yaml
version: '3.8'
services:
  app1:
    image: python:3.11-slim
    command: python -c "import http.server, socketserver; socketserver.TCPServer(('', 8080), http.server.BaseHTTPRequestHandler).serve_forever()"
    # (use a proper Flask app in the actual file)
  app2:
    image: python:3.11-slim
  app3:
    image: python:3.11-slim
  nginx:
    image: nginx:alpine
    ports: ["8080:80"]
    depends_on: [app1, app2, app3]
```

Use a proper nginx.conf with upstream round-robin to all 3 app containers.

- [ ] **Step 3: Write experiment.py**

Send 100 requests to nginx, print which backend handled each (use a `/whoami` endpoint returning container hostname). Stop one container mid-test and show requests redistribute.

- [ ] **Step 4: Verify lab**

```bash
cd system-design-interview/01-foundations/01-scalability
docker compose up -d && python experiment.py && docker compose down -v
```

Expected: requests distributed across 3 backends, then 2 after stopping one.

- [ ] **Step 5: Commit**

```bash
git add system-design-interview/01-foundations/01-scalability/
git commit -m "feat: add scalability foundation topic with horizontal scaling lab"
```

---

## Task 2: F02 — CAP Theorem

**Folder:** `system-design-interview/01-foundations/02-cap-theorem/`
**Lab:** Two PostgreSQL nodes with replication. Pause replication (simulating partition), write to primary, show replica is stale (consistency vs availability trade-off).
**Docker images:** `postgres:15`
**Key content:** CAP theorem, partition tolerance as non-negotiable, CP vs AP systems, real-world examples (Zookeeper=CP, Cassandra=AP, DynamoDB tunable). PACELC extension.

- [ ] **Step 1: Write README.md** — deep explanation of CAP, why P is always present in distributed systems, CP vs AP decision framework, PACELC. Interview talking points: "partition tolerance is non-negotiable in real distributed systems", always name concrete examples of CP/AP systems.

- [ ] **Step 2: Write docker-compose.yml** — primary + replica Postgres. Replica uses `pg_basebackup` for streaming replication.

- [ ] **Step 3: Write experiment.py** — write to primary, read from replica (shows data). Pause replica container. Write again to primary. Try to read from replica (shows stale data). Resume replica, show it catches up. Illustrates consistency vs availability under partition.

- [ ] **Step 4: Verify lab** — `docker compose up -d && python experiment.py && docker compose down -v`

- [ ] **Step 5: Commit** — `feat: add CAP theorem foundation topic with replication partition lab`

---

## Task 3: F03 — Consistency Models

**Folder:** `system-design-interview/01-foundations/03-consistency-models/`
**Lab:** Redis with Python clients demonstrating strong vs eventual consistency patterns. Use Redis replication with `WAIT` command for strong consistency vs fire-and-forget for eventual.
**Docker images:** `redis:7`
**Key content:** Strong, sequential, causal, eventual consistency. Linearizability. Read-your-writes. Monotonic reads. Real-world: DynamoDB eventual vs strong read options.

- [ ] **Step 1: Write README.md** — consistency spectrum, definitions with examples, when to use each. Interview talking points: "read-your-writes is often enough", "eventual consistency is a business decision not just technical".

- [ ] **Step 2: Write docker-compose.yml** — Redis primary + 2 replicas.

- [ ] **Step 3: Write experiment.py** — demonstrate: (1) write to primary, immediately read from replica → stale. (2) write to primary with `WAIT 1 0` (sync replication) → replica has data. Shows consistency levels.

- [ ] **Step 4: Verify lab** — `docker compose up -d && python experiment.py && docker compose down -v`

- [ ] **Step 5: Commit** — `feat: add consistency models foundation topic`

---

## Task 4: F04 — Replication

**Folder:** `system-design-interview/01-foundations/04-replication/`
**Lab:** PostgreSQL primary + 2 replicas (streaming replication). Demonstrate read scaling, failover, replication lag.
**Docker images:** `postgres:15`
**Key content:** Synchronous vs asynchronous replication, leader-follower vs leaderless, replication lag, failover, split-brain, write amplification.

- [ ] **Step 1: Write README.md** — replication strategies, use cases, failure modes, how Postgres streaming replication works internally.

- [ ] **Step 2: Write docker-compose.yml** — 1 primary + 2 replica Postgres with streaming replication configured.

- [ ] **Step 3: Write experiment.py** — write to primary, read from replicas (shows scaling). Measure replication lag with `pg_stat_replication`. Stop primary, show replicas still serve reads.

- [ ] **Step 4: Verify + Commit** — `feat: add replication foundation topic`

---

## Task 5: F05 — Partitioning & Sharding

**Folder:** `system-design-interview/01-foundations/05-partitioning-sharding/`
**Lab:** 3 PostgreSQL instances as manual shards. Python router using hash-based sharding to route writes/reads. Demonstrate hotspot problem with range-based sharding.
**Docker images:** `postgres:15`
**Key content:** Range vs hash vs directory-based partitioning, hotspots, cross-shard queries, resharding pain, consistent hashing preview.

- [ ] **Step 1: Write README.md** — partitioning strategies, when to shard, cross-shard query problem, resharding cost. Interview talking points: "don't shard until you have to", "consistent hashing solves the resharding problem".

- [ ] **Step 2: Write docker-compose.yml** — 3 Postgres containers (shard-0, shard-1, shard-2).

- [ ] **Step 3: Write experiment.py** — implement a `ShardRouter` class in Python that uses `hash(user_id) % 3` to route writes. Insert 1000 records, show even distribution. Then show range-based routing causes hotspot (all new users go to shard-2).

- [ ] **Step 4: Verify + Commit** — `feat: add partitioning and sharding foundation topic`

---

## Task 6: F06 — Caching

**Folder:** `system-design-interview/01-foundations/06-caching/`
**Lab:** Redis + PostgreSQL. Implement cache-aside pattern in Python. Measure latency with/without cache. Demonstrate cache invalidation problem.
**Docker images:** `redis:7`, `postgres:15`
**Key content:** Cache-aside, write-through, write-behind, read-through. Eviction policies (LRU, LFU). Cache stampede. TTL. Cache invalidation — "the hardest problem in CS".

- [ ] **Step 1: Write README.md** — all caching patterns with code examples, eviction policies, cache stampede and the mutex lock solution, when NOT to cache.

- [ ] **Step 2: Write docker-compose.yml** — Redis + Postgres.

- [ ] **Step 3: Write experiment.py** — (1) measure DB query latency without cache. (2) implement cache-aside with Redis. (3) measure latency with cache (show 10x+ improvement). (4) demonstrate cache invalidation: update DB, show cache is stale. (5) demonstrate cache stampede: expire key, send 10 concurrent requests.

- [ ] **Step 4: Verify + Commit** — `feat: add caching foundation topic`

---

## Task 7: F07 — Load Balancing

**Folder:** `system-design-interview/01-foundations/07-load-balancing/`
**Lab:** Nginx with multiple Flask backends. Demonstrate round-robin, least-connections, IP-hash strategies. Show health checks removing a dead backend.
**Docker images:** `nginx:alpine`, `python:3.11-slim`
**Key content:** L4 vs L7 load balancing, algorithms (round-robin, least-connections, consistent hash), health checks, session affinity, SSL termination.

- [ ] **Step 1: Write README.md** — LB types, algorithms, health check internals, sticky sessions trade-offs. Interview talking points: "stateless services don't need sticky sessions".

- [ ] **Step 2: Write docker-compose.yml** — Nginx + 4 Flask backends (different artificial latencies to show least-connections behavior).

- [ ] **Step 3: Write experiment.py** — send requests and record which backend responds + latency. Switch nginx config to least-connections, show requests favor faster backends. Kill one backend, show health check removes it.

- [ ] **Step 4: Verify + Commit** — `feat: add load balancing foundation topic`

---

## Task 8: F08 — Databases: SQL vs NoSQL

**Folder:** `system-design-interview/01-foundations/08-databases-sql-vs-nosql/`
**Lab:** PostgreSQL (relational) + Redis (key-value) + MongoDB (document) side by side. Same data model implemented in each, compare query patterns.
**Docker images:** `postgres:15`, `redis:7`, `mongo:6`
**Key content:** ACID vs BASE, relational vs document vs key-value vs columnar vs graph. When to use which. Normalization. Schema flexibility. Query patterns.

- [ ] **Step 1: Write README.md** — full taxonomy of database types with use cases, ACID vs BASE, denormalization patterns for NoSQL, polyglot persistence.

- [ ] **Step 2: Write docker-compose.yml** — Postgres + Redis + MongoDB.

- [ ] **Step 3: Write experiment.py** — model a "user with posts" in all 3 DBs. Show join query in SQL, embedded document in Mongo, hash in Redis. Measure write/read latency for each.

- [ ] **Step 4: Verify + Commit** — `feat: add SQL vs NoSQL databases foundation topic`

---

## Task 9: F09 — Indexes

**Folder:** `system-design-interview/01-foundations/09-indexes/`
**Lab:** PostgreSQL with 1M row table. Demonstrate query performance without index vs with B-tree, composite, partial, and covering indexes. Show index write overhead.
**Docker images:** `postgres:15`
**Key content:** B-tree, hash, GIN, BRIN indexes. Composite index column order. Covering indexes. Index selectivity. Write amplification. When indexes hurt.

- [ ] **Step 1: Write README.md** — index internals (B-tree structure), how the query planner uses them, index types and when to use each, the "left-most prefix" rule for composite indexes.

- [ ] **Step 2: Write docker-compose.yml** — single Postgres container.

- [ ] **Step 3: Write experiment.py** — seed 1M rows. Run `EXPLAIN ANALYZE` on queries (1) no index: seq scan. (2) B-tree on single col. (3) composite index wrong order. (4) composite index right order. Print query time for each. Show write latency increases with more indexes.

- [ ] **Step 4: Verify + Commit** — `feat: add indexes foundation topic`

---

## Task 10: F10 — Networking Basics

**Folder:** `system-design-interview/01-foundations/10-networking-basics/`
**Lab:** Python scripts demonstrating TCP connection lifecycle, HTTP/1.1 vs HTTP/2 multiplexing, DNS resolution timing, and connection pooling.
**Docker images:** `python:3.11-slim`, `nginx:alpine`
**Key content:** TCP/IP layers, TCP handshake, HTTP/1.1 vs HTTP/2 vs HTTP/3, DNS, CDN basics, latency numbers every engineer should know, bandwidth vs latency.

- [ ] **Step 1: Write README.md** — include the famous "latency numbers every engineer should know" table. Cover TCP, UDP, HTTP versions, DNS lookup chain, what happens when you type a URL.

- [ ] **Step 2: Write docker-compose.yml** — nginx (HTTP/1.1 + HTTP/2) + Python client.

- [ ] **Step 3: Write experiment.py** — (1) measure DNS lookup latency. (2) compare HTTP/1.1 serial requests vs HTTP/2 multiplexed (show speedup). (3) demonstrate TCP connection reuse with connection pool vs new connections.

- [ ] **Step 4: Verify + Commit** — `feat: add networking basics foundation topic`

---

## Task 11: F11 — API Design

**Folder:** `system-design-interview/01-foundations/11-api-design/`
**Lab:** Python Flask app implementing REST, GraphQL (with Strawberry), and a simple gRPC service. Compare for a "get user with posts" use case.
**Docker images:** `python:3.11-slim`
**Key content:** REST (Richardson maturity model), GraphQL (over-fetching/under-fetching), gRPC (protobuf, streaming), API versioning, rate limiting headers, pagination (cursor vs offset), idempotency keys.

- [ ] **Step 1: Write README.md** — REST principles, when GraphQL wins, when gRPC wins, API versioning strategies, pagination trade-offs. Interview talking points: "cursor pagination for real-time data, offset for admin pages".

- [ ] **Step 2: Write docker-compose.yml** — Flask REST + GraphQL + gRPC server.

- [ ] **Step 3: Write experiment.py** — demonstrate the N+1 query problem in REST, how GraphQL solves it, and how gRPC handles streaming updates.

- [ ] **Step 4: Verify + Commit** — `feat: add API design foundation topic`

---

## Task 12: F12 — Blob/Object Storage

**Folder:** `system-design-interview/01-foundations/12-blob-object-storage/`
**Lab:** MinIO (S3-compatible) local object store. Upload, download, multipart upload for large files, presigned URLs, metadata.
**Docker images:** `minio/minio`
**Key content:** Object vs block vs file storage, content-addressable storage, multipart upload, presigned URLs, CDN integration, consistency model (S3 strong consistency), storage classes (hot/warm/cold).

- [ ] **Step 1: Write README.md** — storage types compared, S3 data model (bucket/key/object), eventual vs strong consistency history, multipart upload algorithm, how S3 achieves 11 9s durability (erasure coding).

- [ ] **Step 2: Write docker-compose.yml** — single MinIO container with console on port 9001.

- [ ] **Step 3: Write experiment.py** — (1) upload small file. (2) multipart upload 100MB file (5MB chunks). (3) generate presigned URL, download via URL. (4) list objects, show metadata. (5) time sequential vs parallel multipart upload.

- [ ] **Step 4: Verify + Commit** — `feat: add blob object storage foundation topic`

---

## Task 13: A01 — Consistent Hashing

**Folder:** `system-design-interview/02-advanced/01-consistent-hashing/`
**Lab:** Python implementation of a consistent hash ring with virtual nodes. Add/remove nodes and show minimal key remapping vs naive modulo hashing.
**Docker images:** `python:3.11-slim` (pure Python, no external services needed)
**Key content:** Hash ring, virtual nodes, why modulo hashing causes massive remapping, vnodes for load balance, real use in Cassandra/DynamoDB/Redis Cluster.

- [ ] **Step 1: Write README.md** — full consistent hashing algorithm, virtual nodes, hotspot prevention, how Cassandra uses it. Interview talking points: "without virtual nodes, non-uniform distribution is a problem".

- [ ] **Step 2: Write docker-compose.yml** — minimal Python container.

- [ ] **Step 3: Write experiment.py** — implement `ConsistentHashRing` class. (1) Add 3 nodes, distribute 10K keys. (2) Add a 4th node — show only ~25% of keys remapped. (3) Same with naive `hash(key) % n` — show ~75% remapped. (4) Show virtual nodes improve distribution (print std dev of key counts).

- [ ] **Step 4: Verify + Commit** — `feat: add consistent hashing advanced topic`

---

## Task 14: A02 — Distributed Transactions

**Folder:** `system-design-interview/02-advanced/02-distributed-transactions/`
**Lab:** Two PostgreSQL databases. Python implements 2-phase commit (2PC). Demonstrate commit success and rollback on coordinator failure.
**Docker images:** `postgres:15`
**Key content:** 2PC (prepare/commit phases), coordinator failure problem, 3PC, Saga pattern (choreography vs orchestration), compensating transactions, when to avoid distributed transactions.

- [ ] **Step 1: Write README.md** — 2PC protocol in detail, why coordinator failure creates blocking, Saga pattern as the practical alternative, real-world use (Stripe uses Sagas). Interview talking points: "avoid distributed transactions; use Sagas with compensating transactions instead".

- [ ] **Step 2: Write docker-compose.yml** — 2 Postgres containers (db-orders, db-inventory).

- [ ] **Step 3: Write experiment.py** — implement 2PC coordinator in Python. (1) successful commit across both DBs. (2) simulate coordinator crash after prepare → show blocked transaction. (3) implement simple Saga: deduct inventory → create order → if order fails → compensate inventory.

- [ ] **Step 4: Verify + Commit** — `feat: add distributed transactions advanced topic`

---

## Task 15: A03 — Consensus: Paxos & Raft

**Folder:** `system-design-interview/02-advanced/03-consensus-paxos-raft/`
**Lab:** 3-node etcd cluster (Raft-based). Demonstrate leader election, leader failover, split-brain prevention, log replication.
**Docker images:** `quay.io/etcd-io/etcd:v3.5.9`
**Key content:** Why consensus is hard, Paxos conceptually, Raft in detail (leader election, log replication, safety), etcd as practical example, quorum math (N/2+1).

- [ ] **Step 1: Write README.md** — Raft explained with state machine diagrams in ASCII. Leader election algorithm, log replication guarantee, why you need 2f+1 nodes for f failures. Compare Paxos vs Raft (Raft is more understandable, equivalent safety).

- [ ] **Step 2: Write docker-compose.yml** — 3-node etcd cluster with proper peer URLs.

- [ ] **Step 3: Write experiment.py** — (1) write key to leader. (2) read from any node (show replication). (3) kill leader → observe election timeout → new leader elected. (4) kill 2 of 3 nodes → writes fail (no quorum). (5) restore one node → writes succeed.

- [ ] **Step 4: Verify + Commit** — `feat: add consensus Paxos Raft advanced topic`

---

## Task 16: A04 — Event-Driven Architecture

**Folder:** `system-design-interview/02-advanced/04-event-driven-architecture/`
**Lab:** Kafka + Python producer/consumer. Implement event sourcing for an order system. Show event replay to rebuild state.
**Docker images:** `confluentinc/cp-kafka:7.4.0`, `confluentinc/cp-zookeeper:7.4.0`
**Key content:** Events vs commands vs queries, event sourcing, CQRS, choreography vs orchestration, at-least-once vs exactly-once delivery, event schema evolution.

- [ ] **Step 1: Write README.md** — event-driven patterns with diagrams, event sourcing vs CRUD, CQRS read/write separation, when choreography becomes chaos. Interview talking points: "event sourcing gives you audit log for free, but replay gets slow — use snapshots".

- [ ] **Step 2: Write docker-compose.yml** — Zookeeper + Kafka + Python producer + Python consumer.

- [ ] **Step 3: Write experiment.py** — (1) produce order events (created, paid, shipped, delivered). (2) consume and build state. (3) stop consumer, produce more events. (4) restart consumer from offset 0 — show full state rebuild from events. (5) show consumer group lag.

- [ ] **Step 4: Verify + Commit** — `feat: add event-driven architecture advanced topic`

---

## Task 17: A05 — Message Queues & Kafka

**Folder:** `system-design-interview/02-advanced/05-message-queues-kafka/`
**Lab:** Kafka with multiple partitions and consumer groups. Demonstrate partition-based parallelism, consumer group rebalancing, retention vs deletion.
**Docker images:** `confluentinc/cp-kafka:7.4.0`, `confluentinc/cp-zookeeper:7.4.0`
**Key content:** Kafka architecture (topics, partitions, offsets, consumer groups, brokers), log retention, compaction, throughput math, Kafka vs RabbitMQ vs SQS comparison, exactly-once semantics.

- [ ] **Step 1: Write README.md** — Kafka internals deep dive: how partitions enable parallelism, how consumer groups work, why Kafka is fast (sequential writes, sendfile). Interview talking points: "partition count limits consumer parallelism", "consumer group rebalancing is the source of latency spikes".

- [ ] **Step 2: Write docker-compose.yml** — Zookeeper + Kafka with 3-partition topic.

- [ ] **Step 3: Write experiment.py** — (1) produce 1000 messages across 3 partitions. (2) run 3 consumers in a group — show each gets 1 partition. (3) kill one consumer — show rebalancing. (4) run 4 consumers — one is idle (more consumers than partitions). (5) show message ordering guaranteed within partition, not across.

- [ ] **Step 4: Verify + Commit** — `feat: add message queues Kafka advanced topic`

---

## Task 18: A06 — Stream Processing

**Folder:** `system-design-interview/02-advanced/06-stream-processing/`
**Lab:** Kafka + Python Faust (stream processing library). Implement real-time windowed aggregation (count events per user per minute). Demonstrate late-arriving events.
**Docker images:** `confluentinc/cp-kafka:7.4.0`, `confluentinc/cp-zookeeper:7.4.0`, `python:3.11-slim`
**Key content:** Stream vs batch processing, windowing (tumbling, sliding, session), watermarks, late events, stateful stream processing, Lambda vs Kappa architecture.

- [ ] **Step 1: Write README.md** — stream processing concepts, windowing types with ASCII timelines, watermarks and late event handling, Lambda architecture (batch + speed layers), Kappa architecture (stream only). Interview talking points: "Lambda is operationally complex — Kappa if your stream processor can handle reprocessing".

- [ ] **Step 2: Write docker-compose.yml** — Zookeeper + Kafka + Faust Python worker.

- [ ] **Step 3: Write experiment.py** — produce click events with timestamps. Faust worker counts clicks per user in 1-minute tumbling windows. Produce late-arriving event (backdated timestamp). Show how watermark handles it.

- [ ] **Step 4: Verify + Commit** — `feat: add stream processing advanced topic`

---

## Task 19: A07 — Distributed Caching

**Folder:** `system-design-interview/02-advanced/07-distributed-caching/`
**Lab:** Redis Cluster (3 masters, no dedicated replicas — `cluster-require-full-coverage no`). Demonstrate hash slot distribution, failover, MOVED redirection. Compare with Redis Sentinel.
**Docker images:** `redis:7`
**Key content:** Redis Cluster (hash slots, MOVED/ASK redirections), Redis Sentinel (HA without clustering), consistent hashing in Redis, cache warm-up, thundering herd, hot key problem, Redis data structures for caching patterns.

- [ ] **Step 1: Write README.md** — Redis Cluster internals (16384 hash slots), how failover works, hot key problem and solutions (local replica, key splitting). Interview talking points: "Redis Cluster vs Sentinel: Cluster for scale, Sentinel for HA without sharding".

- [ ] **Step 2: Write docker-compose.yml** — 3-node Redis Cluster (3 masters, `cluster-require-full-coverage no`). Use `redis-cli --cluster create` in init container. 3 nodes is within the 5-container limit and sufficient to demonstrate hash slots and MOVED redirections.

- [ ] **Step 3: Write experiment.py** — (1) write keys across cluster, observe MOVED redirections. (2) kill one master, show replica promoted. (3) demonstrate hot key: flood one key and show it lands on one node — then use key splitting to distribute.

- [ ] **Step 4: Verify + Commit** — `feat: add distributed caching advanced topic`

---

## Task 20: A08 — Search Systems

**Folder:** `system-design-interview/02-advanced/08-search-systems/`
**Lab:** Elasticsearch single node. Index documents, demonstrate full-text search, relevance scoring, faceted search, geo-search.
**Docker images:** `elasticsearch:8.8.0`, `kibana:8.8.0`
**Key content:** Inverted index, TF-IDF and BM25 scoring, tokenization, analyzers, sharding and routing in ES, near real-time search (NRT), index vs search latency trade-off.

- [ ] **Step 1: Write README.md** — inverted index data structure, how BM25 scoring works, ES architecture (primary/replica shards, routing), NRT indexing (refresh interval), when to use ES vs Postgres full-text.

- [ ] **Step 2: Write docker-compose.yml** — Elasticsearch + Kibana.

- [ ] **Step 3: Write experiment.py** — (1) index 10K product documents. (2) full-text search with relevance scoring. (3) faceted search (filter by category + price range + rating). (4) geo-search (find products near coordinates). (5) show how custom analyzer affects tokenization.

- [ ] **Step 4: Verify + Commit** — `feat: add search systems advanced topic`

---

## Task 21: A09 — Rate Limiting Algorithms

**Folder:** `system-design-interview/02-advanced/09-rate-limiting-algorithms/`
**Lab:** Python implementations of all 4 algorithms. Single-process demo (Redis for token bucket state). Benchmark each algorithm's accuracy and memory usage.
**Docker images:** `redis:7`, `python:3.11-slim`
**Key content:** Token bucket, leaky bucket, fixed window counter, sliding window log, sliding window counter. Trade-offs: memory, accuracy, burstiness handling.

- [ ] **Step 1: Write README.md** — each algorithm with ASCII diagrams showing how tokens/requests flow. Comparison table (memory, accuracy, burst handling). Interview talking points: "token bucket allows bursts up to bucket size; sliding window log is most accurate but memory-intensive".

- [ ] **Step 2: Write docker-compose.yml** — Redis + Python container.

- [ ] **Step 3: Write experiment.py** — implement all 4 algorithms against Redis. Send 200 requests at 2x the rate limit. Show (1) fixed window allows burst at window boundary. (2) token bucket smooths bursts. (3) sliding window log is most accurate. Print allowed/rejected counts and rate limit accuracy for each.

- [ ] **Step 4: Verify + Commit** — `feat: add rate limiting algorithms advanced topic`

---

## Task 22: A10 — CDN & Edge

**Folder:** `system-design-interview/02-advanced/10-cdn-and-edge/`
**Lab:** Nginx as "CDN edge" with caching in front of an origin server. Demonstrate cache-control headers, cache hit/miss, cache invalidation (purge), and geographic routing simulation.
**Docker images:** `nginx:alpine`, `python:3.11-slim`
**Key content:** CDN architecture (PoP, origin pull, push CDN), cache-control headers, ETags, cache invalidation (versioning vs purge), edge computing, anycast routing concept.

- [ ] **Step 1: Write README.md** — CDN architecture, how origin pull works, cache-control headers (max-age, s-maxage, no-cache vs no-store), cache invalidation strategies (versioned URLs are best). Interview talking points: "CDN is not just for static content — API responses can be cached too with the right headers".

- [ ] **Step 2: Write docker-compose.yml** — Nginx proxy cache + Flask origin with artificial 200ms latency.

- [ ] **Step 3: Write experiment.py** — (1) first request → cache miss, slow (200ms). (2) repeat → cache hit, fast (<5ms). (3) set short TTL, show expiry. (4) add `Cache-Control: no-cache` header, show Nginx revalidates.

- [ ] **Step 4: Verify + Commit** — `feat: add CDN and edge advanced topic`

---

## Task 23: A11 — Observability

**Folder:** `system-design-interview/02-advanced/11-observability/`
**Lab:** Flask app + Prometheus (metrics) + Grafana (dashboards) + structured JSON logging. Demonstrate the three pillars of observability.
**Docker images:** `prom/prometheus`, `grafana/grafana`, `python:3.11-slim`
**Key content:** Metrics (counters, gauges, histograms), distributed tracing (spans, trace context propagation), structured logging, alerting, SLO/SLI/SLA definitions, RED method (Rate, Errors, Duration).

- [ ] **Step 1: Write README.md** — three pillars (metrics, traces, logs), RED method, USE method, SLO/SLI/SLA with concrete examples. Interview talking points: "SLO is a target, SLA is a contract — always design with SLO headroom".

- [ ] **Step 2: Write docker-compose.yml** — Flask app (with prometheus_client) + Prometheus + Grafana.

- [ ] **Step 3: Write experiment.py** — generate synthetic traffic with varying error rates and latencies. Print Prometheus query results showing p50/p95/p99 latency, error rate, RPS. Include a pre-built Grafana dashboard JSON.

- [ ] **Step 4: Verify + Commit** — `feat: add observability advanced topic`

---

## Task 24: A12 — Security at Scale

**Folder:** `system-design-interview/02-advanced/12-security-at-scale/`
**Lab:** Nginx with JWT auth middleware (Python). Demonstrate token signing/verification, HTTPS termination, CORS, secrets management with environment variables.
**Docker images:** `nginx:alpine`, `python:3.11-slim`
**Key content:** AuthN vs AuthZ, JWT (structure, signing, expiry, rotation), OAuth2/OIDC flow, API keys vs session tokens, HTTPS (TLS handshake, cert pinning), secrets management, OWASP top 10 at scale.

- [ ] **Step 1: Write README.md** — auth patterns, JWT anatomy, OAuth2 flows (auth code, client credentials), when to use API keys vs JWT vs sessions. Interview talking points: "JWT is stateless but can't be revoked — use short TTLs + refresh tokens".

- [ ] **Step 2: Write docker-compose.yml** — Flask auth service + Nginx proxy with auth_request.

- [ ] **Step 3: Write experiment.py** — (1) request without token → 401. (2) get JWT from auth service. (3) request with valid JWT → 200. (4) tamper with JWT payload → 401. (5) expire token → 401. (6) use refresh token → new access token.

- [ ] **Step 4: Verify + Commit** — `feat: add security at scale advanced topic`

---

## Task 25: A13 — Service Discovery & Coordination

**Folder:** `system-design-interview/02-advanced/13-service-discovery-coordination/`
**Lab:** etcd cluster. Python service registers itself on startup, deregisters on shutdown. Client discovers services via etcd watch. Demonstrate leader election using etcd leases.
**Docker images:** `quay.io/etcd-io/etcd:v3.5.9`, `python:3.11-slim`
**Key content:** Client-side vs server-side discovery, service registry (etcd, Consul, Zookeeper), heartbeats and TTL leases, distributed locks, leader election, configuration management.

- [ ] **Step 1: Write README.md** — discovery patterns, Consul vs etcd vs Zookeeper comparison, how Kubernetes service discovery works (DNS + kube-proxy), distributed locking algorithms (Redlock controversy). Interview talking points: "etcd is CP — during partition, services may not be discoverable; design for this".

- [ ] **Step 2: Write docker-compose.yml** — etcd cluster + 3 Python "service" containers.

- [ ] **Step 3: Write experiment.py** — (1) 3 services register with TTL leases. (2) client watches for changes and maintains list. (3) kill one service → lease expires → client sees deregistration. (4) implement leader election: all 3 services compete, one wins, others watch.

- [ ] **Step 4: Verify + Commit** — `feat: add service discovery and coordination advanced topic`

---

## Task 26: A14 — Idempotency & Exactly-Once Semantics

**Folder:** `system-design-interview/02-advanced/14-idempotency-exactly-once/`
**Lab:** Flask payment API with idempotency keys backed by PostgreSQL. Demonstrate duplicate request detection. Kafka producer with exactly-once semantics.
**Docker images:** `postgres:15`, `python:3.11-slim`, `confluentinc/cp-kafka:7.4.0`
**Key content:** At-most-once, at-least-once, exactly-once. Idempotency keys (UUID, content hash). Deduplication window. Kafka transactions (exactly-once producer). Database-level idempotency with UNIQUE constraints. Distributed systems retry storms.

- [ ] **Step 1: Write README.md** — delivery semantics with examples, why exactly-once is hard in distributed systems, idempotency key pattern (Stripe uses this), how Kafka achieves exactly-once with producer transactions. Interview talking points: "at-least-once + idempotent consumers = effectively exactly-once".

- [ ] **Step 2: Write docker-compose.yml** — Postgres + Flask API + Kafka.

- [ ] **Step 3: Write experiment.py** — (1) POST payment with idempotency key → succeeds. (2) retry same request (same key) → returns same response, no duplicate charge. (3) new key → new charge. (4) Kafka: produce with `enable.idempotence=True`, kill and restart producer, show no duplicates in topic.

- [ ] **Step 4: Verify + Commit** — `feat: add idempotency and exactly-once advanced topic`

---

## Task 27: A15 — Probabilistic Data Structures

**Folder:** `system-design-interview/02-advanced/15-probabilistic-data-structures/`
**Lab:** Python implementations of Bloom filter, HyperLogLog, Count-Min Sketch. Demonstrate memory usage vs exact data structures at scale.
**Docker images:** `redis/redis-stack-server:7.2.0-v9` (includes Bloom filter, TopK, HyperLogLog modules)
**Key content:** Bloom filter (membership, false positive rate, no false negatives), HyperLogLog (cardinality estimation), Count-Min Sketch (frequency estimation), MinHash (similarity). Trade-off: memory vs accuracy.

- [ ] **Step 1: Write README.md** — each data structure with the math (false positive formula for Bloom filter), real use cases (Cassandra uses Bloom filters for SSTable lookup, Redis HyperLogLog for unique visitor count, LinkedIn uses MinHash for "People You May Know"). Interview talking points: "Bloom filter: definitely not there OR probably there — great for read amplification reduction".

- [ ] **Step 2: Write docker-compose.yml** — Redis with RedisBloom module.

- [ ] **Step 3: Write experiment.py** — (1) Bloom filter: add 1M URLs, test membership, measure false positive rate vs theoretical. (2) Compare memory: set of 1M strings vs Bloom filter. (3) HyperLogLog: count unique visitors in 10M events — compare memory to exact `set`. (4) Count-Min Sketch: top-K frequent items from stream.

- [ ] **Step 4: Verify + Commit** — `feat: add probabilistic data structures advanced topic`

---

## Task 28: CS01 — URL Shortener

**Folder:** `system-design-interview/03-case-studies/01-url-shortener/`
**Lab:** Full URL shortener: Flask API + PostgreSQL (URL store) + Redis (cache). Base62 encoding, collision handling, analytics counters.
**Docker images:** `postgres:15`, `redis:7`, `python:3.11-slim`
**Prerequisites:** F05 (sharding), F06 (caching), F09 (indexes)
**Scale:** 100M URLs, 10B redirects/day (115K RPS reads, 1K RPS writes)
**Hard problems:** ID generation without collisions, redirect latency <10ms, analytics at scale

- [ ] **Step 1: Write README.md** — full case study format. Deep dives: (1) ID generation strategies (hash vs auto-increment vs Snowflake ID — hash has collisions, why Snowflake is better). (2) redirect latency (cache in Redis, serve from edge). (3) analytics (counter in Redis with periodic flush to DB — not counting in DB on every click). Compare to bit.ly's actual architecture.

- [ ] **Step 2: Write docker-compose.yml** — Postgres + Redis + Flask app.

- [ ] **Step 3: Write experiment.py** — (1) create 1000 short URLs. (2) time redirect with/without Redis cache. (3) show base62 encoding. (4) increment click counter atomically in Redis. (5) flush counters to Postgres batch.

- [ ] **Step 4: Verify + Commit** — `feat: add URL shortener case study`

---

## Task 29: CS02 — Twitter Timeline

**Folder:** `system-design-interview/03-case-studies/02-twitter-timeline/`
**Lab:** Fan-out service: Kafka + Redis + PostgreSQL. Write to Kafka → fan-out worker pushes to follower timelines in Redis. Demonstrate celebrity problem (no fan-out for high-follower accounts).
**Docker images:** `postgres:15`, `redis:7`, `confluentinc/cp-kafka:7.4.0`, `confluentinc/cp-zookeeper:7.4.0`, `python:3.11-slim`
**Prerequisites:** F04 (replication), F06 (caching), A05 (Kafka), A07 (distributed caching)
**Scale:** 300M users, 600M tweets/day, 300K RPS reads
**Hard problems:** Fan-out on write vs read, celebrity accounts, timeline cache invalidation

- [ ] **Step 1: Write README.md** — deep dives: (1) fan-out on write (push to all followers' timelines at write time — fast reads, slow writes, celebrity problem). (2) fan-out on read (pull and merge at read time — slow reads, no celebrity problem). (3) Twitter's hybrid: fan-out on write for non-celebrities (<1M followers), pull for celebrities at read time. (4) timeline cache in Redis sorted sets. Compare to Twitter's actual "Timelines at Scale" engineering blog post.

- [ ] **Step 2: Write docker-compose.yml** — Postgres + Redis + Kafka + Zookeeper + fan-out worker.

- [ ] **Step 3: Write experiment.py** — (1) create users + follow graph. (2) post tweet → Kafka. (3) fan-out worker consumes → pushes to Redis sorted set timelines. (4) fetch timeline from Redis. (5) add a "celebrity" user (10K followers) — show fan-out worker timing.

- [ ] **Step 4: Verify + Commit** — `feat: add Twitter timeline case study`

---

## Task 30: CS03 — YouTube

**Folder:** `system-design-interview/03-case-studies/03-youtube/`
**Lab:** MinIO (video storage) + PostgreSQL (metadata) + Redis (view counts) + Nginx (simulated CDN). Upload → store → serve with range requests (video seeking).
**Docker images:** `minio/minio`, `postgres:15`, `redis:7`, `nginx:alpine`
**Prerequisites:** F06 (caching), F10 (networking), F12 (blob storage), A10 (CDN)
**Scale:** 500 hours video uploaded/minute, 1B hours watched/day
**Hard problems:** Video transcoding pipeline, adaptive bitrate streaming, view count at scale

- [ ] **Step 1: Write README.md** — deep dives: (1) transcoding pipeline (upload → queue → transcode workers → multiple resolutions/formats → store to object storage). (2) adaptive bitrate streaming (HLS/DASH — client switches quality based on bandwidth). (3) view count at scale (Redis counter with periodic flush, not DB write per view). Compare to YouTube's actual architecture (known from engineering talks).

- [ ] **Step 2: Write docker-compose.yml** — MinIO + Postgres + Redis + Nginx.

- [ ] **Step 3: Write experiment.py** — (1) upload video to MinIO. (2) store metadata in Postgres. (3) serve video with byte-range requests (seek simulation). (4) increment view count in Redis. (5) show CDN cache headers for video segments.

- [ ] **Step 4: Verify + Commit** — `feat: add YouTube case study`

---

## Task 31: CS04 — Uber

**Folder:** `system-design-interview/03-case-studies/04-uber/`
**Lab:** PostGIS (geospatial Postgres) + Redis (driver location cache with geo commands) + Flask API. Demonstrate driver location updates, nearest driver query, geofencing.
**Docker images:** `postgis/postgis:15-3.3`, `redis:7`, `python:3.11-slim`
**Prerequisites:** F05 (sharding), F06 (caching), F08 (databases), A07 (distributed caching)
**Scale:** 5M rides/day, 4M active drivers globally, 100K location updates/second
**Hard problems:** Real-time geo-indexing for nearest driver, matching algorithm, surge pricing

- [ ] **Step 1: Write README.md** — deep dives: (1) geo-indexing (Redis GEOADD/GEORADIUS vs PostGIS — Redis for real-time, PostGIS for historical). (2) matching algorithm (minimize total ETA, not just nearest driver). (3) location update pipeline (drivers push every 4s → Redis geo index updated). (4) surge pricing (ML model + supply/demand ratio per geohash cell). Compare to Uber's actual architecture.

- [ ] **Step 2: Write docker-compose.yml** — PostGIS + Redis + Flask API.

- [ ] **Step 3: Write experiment.py** — (1) seed 1000 driver locations via `GEOADD`. (2) find 5 nearest drivers to a location using `GEORADIUS`. (3) simulate 100 location updates/second. (4) geofence check (is this coordinate in the city boundary?).

- [ ] **Step 4: Verify + Commit** — `feat: add Uber case study`

---

## Task 32: CS05 — WhatsApp

**Folder:** `system-design-interview/03-case-studies/05-whatsapp/`
**Lab:** WebSocket server (Python aiohttp) + Redis pub/sub (message routing) + PostgreSQL (message store). Demonstrate online/offline delivery, message acknowledgment (sent/delivered/read receipts).
**Docker images:** `redis:7`, `postgres:15`, `python:3.11-slim`
**Prerequisites:** F04 (replication), F06 (caching), A04 (event-driven), A14 (idempotency)
**Scale:** 2B users, 100B messages/day, 1M connections per server
**Hard problems:** Message delivery guarantee, presence system, end-to-end encryption architecture

- [ ] **Step 1: Write README.md** — deep dives: (1) connection management (WebSocket persistent connections, server selection via consistent hashing). (2) message delivery guarantee (sender → server → ACK1, server → recipient → ACK2, recipient reads → ACK3 = read receipt). (3) presence system (online status via heartbeat, fan-out to contacts). (4) offline delivery (queue messages, deliver on reconnect). Compare to WhatsApp's actual architecture (known from engineering talks + paper).

- [ ] **Step 2: Write docker-compose.yml** — Redis + Postgres + WebSocket server.

- [ ] **Step 3: Write experiment.py** — (1) connect 3 clients via WebSocket. (2) send message: A→B (B online → immediate delivery). (3) disconnect B. (4) send message: A→B (B offline → queued). (5) reconnect B → queued messages delivered. (6) show sent/delivered/read receipt states.

- [ ] **Step 4: Verify + Commit** — `feat: add WhatsApp case study`

---

## Task 33: CS06 — Google Drive

**Folder:** `system-design-interview/03-case-studies/06-google-drive/`
**Lab:** MinIO (file storage) + PostgreSQL (metadata + version history) + Redis (upload session cache). Implement chunked upload, deduplication via content hash, conflict resolution.
**Docker images:** `minio/minio`, `postgres:15`, `redis:7`
**Prerequisites:** F12 (blob storage), F04 (replication), A14 (idempotency)
**Scale:** 1B users, 2 trillion files, 15GB free per user
**Hard problems:** File sync conflict resolution, delta sync (only changed blocks), storage deduplication

- [ ] **Step 1: Write README.md** — deep dives: (1) chunked upload (split file into 4MB blocks, upload individually, resume on failure). (2) delta sync (rsync-style — only upload changed blocks using rolling hash). (3) conflict resolution (last-write-wins vs conflict copy — Google Drive creates conflict copy). (4) storage deduplication (SHA-256 per block, store once). Compare to Dropbox's "Magic Pocket" and Google Drive internals.

- [ ] **Step 2: Write docker-compose.yml** — MinIO + Postgres + Redis.

- [ ] **Step 3: Write experiment.py** — (1) upload file in chunks. (2) modify one byte of file, upload again — show only 1 changed chunk is re-uploaded (delta sync). (3) two clients upload same file — show deduplication (same hash, no duplicate storage). (4) simulate conflict: both clients modify same file → conflict copy created.

- [ ] **Step 4: Verify + Commit** — `feat: add Google Drive case study`

---

## Task 34: CS07 — Web Crawler

**Folder:** `system-design-interview/03-case-studies/07-web-crawler/`
**Lab:** Python crawler + Redis (URL frontier/visited set using Bloom filter) + PostgreSQL (crawled content). Implement politeness (robots.txt, rate limiting per domain), BFS vs priority queue.
**Docker images:** `redis:7`, `postgres:15`, `python:3.11-slim`
**Prerequisites:** F05 (sharding), A09 (rate limiting), A15 (probabilistic data structures)
**Scale:** 5B web pages, re-crawl every 2 weeks, 400M pages/day
**Hard problems:** URL deduplication at scale, politeness, crawler traps, distributed coordination

- [ ] **Step 1: Write README.md** — deep dives: (1) URL frontier (priority queue by PageRank/freshness). (2) deduplication (Bloom filter for visited URLs — can't store 5B URLs in a set). (3) politeness (per-domain rate limiting + robots.txt). (4) distributed crawling (partition URL space by domain hash). (5) crawler traps (infinite URLs like calendars — detect cycles).

- [ ] **Step 2: Write docker-compose.yml** — Redis + Postgres + crawler worker.

- [ ] **Step 3: Write experiment.py** — crawl a set of seed URLs (use a local mock HTTP server to avoid real internet). Show BFS traversal, URL deduplication with Bloom filter, robots.txt respect, per-domain rate limiting.

- [ ] **Step 4: Verify + Commit** — `feat: add web crawler case study`

---

## Task 35: CS08 — Search Engine

**Folder:** `system-design-interview/03-case-studies/08-search-engine/`
**Lab:** Elasticsearch + Python indexing pipeline. Build an inverted index manually in Python for illustration, then use Elasticsearch for the real demo. Implement TF-IDF ranking, query parsing, snippet extraction.
**Docker images:** `elasticsearch:8.8.0`, `python:3.11-slim`
**Prerequisites:** A08 (search systems), F09 (indexes)
**Scale:** Trillion documents, sub-100ms query latency, continuous indexing
**Hard problems:** Inverted index at scale, ranking, indexing pipeline, query understanding

- [ ] **Step 1: Write README.md** — deep dives: (1) inverted index construction and compression (delta encoding, variable-byte encoding for posting lists). (2) ranking (TF-IDF → BM25 → learning-to-rank). (3) indexing pipeline (crawl → parse → tokenize → index → serve — BigTable for index storage at Google). (4) query understanding (spell correction, query expansion, entity recognition).

- [ ] **Step 2: Write docker-compose.yml** — Elasticsearch + Python indexer.

- [ ] **Step 3: Write experiment.py** — (1) manually build inverted index in Python for 1000 docs (shows the algorithm). (2) index same docs in Elasticsearch. (3) compare query results and relevance scores. (4) show how analyzer affects tokenization (stemming, stop words). (5) snippet extraction (highlight matching terms).

- [ ] **Step 4: Verify + Commit** — `feat: add search engine case study`

---

## Task 36: CS09 — Notification System

**Folder:** `system-design-interview/03-case-studies/09-notification-system/`
**Lab:** Kafka (notification events) + Python workers (push/email/SMS dispatchers) + PostgreSQL (notification log). Implement fan-out, priority queues, deduplication, user preferences.
**Docker images:** `confluentinc/cp-kafka:7.4.0`, `confluentinc/cp-zookeeper:7.4.0`, `postgres:15`, `python:3.11-slim`
**Prerequisites:** A05 (Kafka), A04 (event-driven), A14 (idempotency)
**Scale:** Facebook: 1B notifications/day across push, email, SMS, in-app
**Hard problems:** Multi-channel delivery, user preference filtering, deduplication, failure retry

- [ ] **Step 1: Write README.md** — deep dives: (1) notification pipeline (event → Kafka → channel workers → external providers). (2) user preferences (per-channel, per-type opt-out — store in Redis for fast lookup). (3) deduplication (idempotency key per notification — prevent double-send on retry). (4) priority queues (transactional > social > marketing). (5) retry with exponential backoff.

- [ ] **Step 2: Write docker-compose.yml** — Kafka + Zookeeper + Postgres + 3 channel workers.

- [ ] **Step 3: Write experiment.py** — (1) produce notification events. (2) workers consume and simulate delivery (print to stdout as mock push/email/SMS). (3) simulate worker failure → retry with backoff. (4) user has email disabled → worker skips email channel. (5) duplicate events → deduplication via idempotency key.

- [ ] **Step 4: Verify + Commit** — `feat: add notification system case study`

---

## Task 37: CS10 — Distributed Rate Limiter

**Folder:** `system-design-interview/03-case-studies/10-rate-limiter/`
**Lab:** Nginx + Redis (token bucket state) + Flask API + Python load generator. Implement distributed rate limiter enforced across multiple API server instances.
**Docker images:** `nginx:alpine`, `redis:7`, `python:3.11-slim`
**Prerequisites:** A09 (rate limiting algorithms), A07 (distributed caching)
**Scale:** Stripe handles millions of API keys, each with different limits, across hundreds of servers
**Hard problems:** Distributed enforcement (shared state in Redis), race conditions, Redis failure mode

- [ ] **Step 1: Write README.md** — deep dives: (1) why local rate limiting fails (server A allows, server B allows = 2x rate). (2) Redis-backed sliding window counter with Lua script (atomic check-and-increment). (3) race conditions (why Lua script is needed — INCR + EXPIRE is not atomic). (4) Redis failure mode (fail open vs fail closed — Stripe fails open with local fallback). (5) multi-tier rate limiting (global per key + per IP + per endpoint).

- [ ] **Step 2: Write docker-compose.yml** — Nginx + 3 Flask API instances + Redis.

- [ ] **Step 3: Write experiment.py** — (1) send requests to Nginx (distributes across 3 Flask instances). (2) each Flask instance checks Redis for rate limit. (3) exceed rate limit — show 429 responses. (4) kill Redis — show fail-open behavior (requests pass through). (5) compare: local rate limiting (each server allows full rate) vs distributed (enforced globally).

- [ ] **Step 4: Verify + Commit** — `feat: add distributed rate limiter case study`

---

## Task 38: CS11 — Distributed Cache (Redis Internals)

**Folder:** `system-design-interview/03-case-studies/11-distributed-cache/`
**Lab:** Redis Cluster + Python client. Demonstrate eviction policies under memory pressure, pipeline batching, pub/sub, Lua scripting, and persistence (RDB vs AOF).
**Docker images:** `redis:7`
**Prerequisites:** F06 (caching), A07 (distributed caching), A01 (consistent hashing)
**Scale:** Twitter uses Redis for 40TB of data, Discord serves 5M messages/second through Redis
**Hard problems:** Memory management + eviction, persistence trade-offs, hot keys, cache warming

- [ ] **Step 1: Write README.md** — deep dives: (1) Redis single-threaded model and why it's fast. (2) eviction policies (LRU vs LFU vs allkeys-lru — when to use each). (3) persistence (RDB snapshots vs AOF logging — trade-off: RDB faster restart, AOF less data loss). (4) Redis data structures and their use cases (sorted sets for leaderboards, streams for event log, HyperLogLog for unique counts). (5) cluster resharding.

- [ ] **Step 2: Write docker-compose.yml** — Redis Cluster (3 masters, `cluster-require-full-coverage no`) — stays within 5-container limit.

- [ ] **Step 3: Write experiment.py** — (1) fill cache to memory limit, observe LRU eviction. (2) pipeline 1000 commands vs 1000 individual commands — show latency difference. (3) pub/sub: publisher + 3 subscribers. (4) Lua script for atomic check-and-set. (5) trigger RDB snapshot, measure pause time.

- [ ] **Step 4: Verify + Commit** — `feat: add distributed cache case study`

---

## Task 39: CS12 — Payment System

**Folder:** `system-design-interview/03-case-studies/12-payment-system/`
**Lab:** Flask payment API + PostgreSQL (double-entry ledger) + Redis (idempotency store). Implement idempotent charge, double-entry bookkeeping, reconciliation.
**Docker images:** `postgres:15`, `redis:7`, `python:3.11-slim`
**Prerequisites:** A02 (distributed transactions), A14 (idempotency), F08 (databases)
**Scale:** Stripe processes $640B+ annually, 99.99% uptime requirement
**Hard problems:** Exactly-once payments, double-entry bookkeeping, reconciliation, fraud detection

- [ ] **Step 1: Write README.md** — deep dives: (1) idempotency (every payment has an idempotency key — retry is safe). (2) double-entry bookkeeping (every debit has a matching credit — immutable ledger). (3) distributed transaction problem (charge card AND credit merchant — what if one fails?). (4) reconciliation (periodic check: sum of all ledger entries = 0). (5) fraud detection (velocity checks, ML scoring, 3D Secure). Compare to Stripe's architecture (public engineering blog).

- [ ] **Step 2: Write docker-compose.yml** — Postgres + Redis + Flask.

- [ ] **Step 3: Write experiment.py** — (1) charge $100: debit customer account, credit merchant account (atomic DB transaction). (2) retry same charge with same idempotency key — no duplicate. (3) simulate failed charge (card declined) — show no ledger entry created. (4) run reconciliation: sum all entries should be zero. (5) simulate network timeout: charge succeeds server-side but client never gets response — retry with same key is safe.

- [ ] **Step 4: Verify + Commit** — `feat: add payment system case study`

---

## Final Task: Integration Check

- [ ] **Step 1: Verify all folders exist**

```bash
find system-design-interview -name "README.md" | wc -l
# Expected: 40 (1 root + 39 topics)
find system-design-interview -name "docker-compose.yml" | wc -l
# Expected: 39
find system-design-interview -name "experiment.py" | wc -l
# Expected: 39
```

- [ ] **Step 2: Verify each lab starts cleanly**

```bash
for dir in system-design-interview/01-foundations/*/; do
  echo "Testing: $dir"
  cd "$dir" && docker compose config --quiet && cd -
done
```

- [ ] **Step 3: Final commit**

```bash
git add system-design-interview/
git commit -m "feat: complete system design interview prep resource (39 topics)"
```
