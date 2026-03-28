# System Design Interview Preparation

A hands-on, lab-driven resource for experienced software engineers preparing for FAANG-level system design interviews.
Every topic includes a runnable local experiment using Docker Compose and Python — no cloud account required.

## Who This Is For

This resource targets senior/staff engineers with 3+ years of experience who need to systematically close gaps before
FAANG system design rounds. It assumes comfort with basic programming and data structures. The focus is on **depth over
breadth**: each topic explains the *why*, walks through trade-offs, and gives you something to run and observe locally.

## How to Use This Resource

Work through topics in the recommended order below. Each topic folder contains:

- `README.md` — concept explanation, trade-offs, and interview angles
- `docker-compose.yml` — local infrastructure (Redis, Postgres, Kafka, etc.)
- `experiment.py` — runnable experiment that makes the concept tangible
- `solutions/` — reference implementations and discussion notes

Read the topic README first, then run the experiment, then return to the README to review what you observed.

---

## Environment Prerequisites

Install these once before starting any labs:

- **Docker Desktop** (macOS/Windows) or **Docker Engine + Docker Compose plugin** (Linux)
- **Python 3.10+**
- Python packages:

```bash
pip install redis psycopg2-binary kafka-python-ng requests flask strawberry-graphql grpcio faust-streaming
```

- No cloud account required — all infrastructure runs locally via Docker Compose.

---

## How to Run Any Lab

Every topic follows the same pattern:

```bash
cd system-design-interview/<topic-folder>/

# Start local infrastructure
docker compose up -d

# Run the experiment
python experiment.py

# Follow the topic README.md for guided observations and exercises

# Tear down (remove containers and volumes)
docker compose down -v
```

---

## Recommended Reading Order

Work through all 39 topics in sequence. Earlier topics build vocabulary and intuition that later topics depend on.

| #  | Topic                         | Section      | Est. Time | Key Concept                                           |
|----|-------------------------------|--------------|-----------|-------------------------------------------------------|
| 1  | Scalability                   | Foundations  | 45 min    | Horizontal vs vertical scaling                        |
| 2  | CAP Theorem                   | Foundations  | 45 min    | Consistency vs availability trade-off                 |
| 3  | Consistency Models            | Foundations  | 45 min    | Strong, eventual, causal consistency                  |
| 4  | Replication                   | Foundations  | 45 min    | Leader-follower, sync vs async                        |
| 5  | Partitioning & Sharding       | Foundations  | 45 min    | Hash vs range partitioning                            |
| 6  | Caching                       | Foundations  | 45 min    | Cache-aside, write-through, eviction                  |
| 7  | Load Balancing                | Foundations  | 45 min    | Round-robin, least-connections, health checks         |
| 8  | Databases: SQL vs NoSQL       | Foundations  | 45 min    | ACID vs BASE, when to use which                       |
| 9  | Indexes                       | Foundations  | 45 min    | B-tree, composite, covering indexes                   |
| 10 | Networking Basics             | Foundations  | 45 min    | TCP, HTTP versions, latency numbers                   |
| 11 | API Design                    | Foundations  | 45 min    | REST, GraphQL, gRPC trade-offs                        |
| 12 | Blob/Object Storage           | Foundations  | 45 min    | S3 model, multipart upload, CDN                       |
| 13 | Proxies & Reverse Proxies     | Foundations  | 45 min    | Reverse proxy, SSL termination, API gateway           |
| 14 | Failure Modes & Reliability   | Foundations  | 45 min    | Timeouts, retries, circuit breaker, cascading failure |
| 15 | Consistent Hashing            | Advanced     | 60 min    | Hash ring, virtual nodes                              |
| 16 | Distributed Transactions      | Advanced     | 60 min    | 2PC, Saga pattern                                     |
| 17 | Consensus: Paxos & Raft       | Advanced     | 60 min    | Leader election, log replication                      |
| 18 | Event-Driven Architecture     | Advanced     | 60 min    | Event sourcing, CQRS                                  |
| 19 | Message Queues — Fundamentals | Advanced     | 60 min    | At-least-once, DLQ, competing consumers, pub/sub      |
| 20 | Message Queues & Kafka        | Advanced     | 60 min    | Partitions, consumer groups, log retention            |
| 21 | Stream Processing             | Advanced     | 60 min    | Windowing, watermarks                                 |
| 22 | Distributed Caching           | Advanced     | 60 min    | Redis Cluster, hash slots                             |
| 23 | Search Systems                | Advanced     | 60 min    | Inverted index, BM25, Elasticsearch                   |
| 24 | Rate Limiting Algorithms      | Advanced     | 45 min    | Token bucket, sliding window                          |
| 25 | CDN & Edge                    | Advanced     | 45 min    | Cache-control, origin pull                            |
| 26 | Observability                 | Advanced     | 60 min    | Metrics, traces, logs, SLO                            |
| 27 | Security at Scale             | Advanced     | 45 min    | JWT, OAuth2, HTTPS                                    |
| 28 | Service Discovery             | Advanced     | 60 min    | etcd, Consul, leader election                         |
| 29 | Idempotency & Exactly-Once    | Advanced     | 60 min    | Idempotency keys, Kafka transactions                  |
| 30 | Probabilistic Data Structures | Advanced     | 60 min    | Bloom filter, HyperLogLog                             |
| 31 | Database Internals            | Advanced     | 60 min    | B-tree, LSM-tree, WAL, MVCC                           |
| 32 | Backpressure & Flow Control   | Advanced     | 60 min    | Queue depth, drop, reject, load shedding              |
| 33 | Multi-Region Architecture     | Advanced     | 60 min    | Active-active, conflict resolution, failover          |
| 34 | URL Shortener                 | Case Studies | 90 min    | ID generation, redirect caching                       |
| 35 | Twitter Timeline              | Case Studies | 90 min    | Fan-out on write vs read                              |
| 36 | YouTube                       | Case Studies | 90 min    | Video pipeline, adaptive bitrate                      |
| 37 | Uber                          | Case Studies | 90 min    | Geo-indexing, real-time matching                      |
| 38 | WhatsApp                      | Case Studies | 90 min    | Message delivery guarantees                           |
| 39 | Google Drive                  | Case Studies | 90 min    | Delta sync, deduplication                             |
| 40 | Web Crawler                   | Case Studies | 90 min    | URL frontier, politeness                              |
| 41 | Search Engine                 | Case Studies | 90 min    | Inverted index, ranking pipeline                      |
| 42 | Notification System           | Case Studies | 90 min    | Multi-channel fan-out                                 |
| 43 | Distributed Rate Limiter      | Case Studies | 90 min    | Redis-backed enforcement                              |
| 44 | Distributed Cache             | Case Studies | 90 min    | Redis internals, eviction                             |
| 45 | Payment System                | Case Studies | 90 min    | Idempotency, double-entry ledger                      |

**Total estimated time:** ~46 hours of focused study across all topics.

---

## Study Schedule

### 4-Week Accelerated Plan (interview in ~1 month)

| Week   | Focus               | Topics                | Daily Commitment |
|--------|---------------------|-----------------------|------------------|
| Week 1 | Foundations, Part 1 | Topics 1–6            | ~45 min/day      |
| Week 2 | Foundations, Part 2 | Topics 7–12           | ~45 min/day      |
| Week 3 | Advanced Topics     | Topics 13–27 (~2/day) | ~2 hrs/day       |
| Week 4 | Case Studies        | Topics 28–39 (~2/day) | ~3 hrs/day       |

### 6-Week Thorough Plan (recommended)

| Week   | Focus                         | Topics                        | Daily Commitment |
|--------|-------------------------------|-------------------------------|------------------|
| Week 1 | Foundations, Part 1           | Topics 1–6 (1/day)            | ~45 min/day      |
| Week 2 | Foundations, Part 2           | Topics 7–12 (1/day)           | ~45 min/day      |
| Week 3 | Advanced, Part 1              | Topics 13–20 (1–2/day)        | ~60 min/day      |
| Week 4 | Advanced, Part 2              | Topics 21–27 (1–2/day)        | ~60 min/day      |
| Week 5 | Case Studies, Part 1          | Topics 28–33 (2 every 3 days) | ~90 min/day      |
| Week 6 | Case Studies, Part 2 + Review | Topics 34–39 + weak spots     | ~90 min/day      |

**Tips for sticking to the schedule:**

- Do the lab *before* re-reading the notes — observation first, explanation second.
- After each case study, practice a 35-minute verbal walkthrough out loud or with a partner.
- Keep a "gaps" list. When an experiment reveals something you don't fully understand, add it and circle back.

---

## Interview Tips

### How to Structure a System Design Answer

A strong answer follows this four-phase structure. Spend time proportional to the phase's weight.

**1. Clarify Requirements (3–5 min)**

Ask before designing. Cover:

- Scale: DAU, QPS, storage volume, read/write ratio
- Functional requirements: what the system must do
- Non-functional requirements: latency targets, availability SLA, consistency guarantees
- Constraints: global vs regional, budget sensitivity

*Why this matters:* The same question ("design a URL shortener") can have wildly different correct answers at 1k vs 1B
requests/day. Interviewers want to see you scope before you build.

**2. Capacity Estimation (3–5 min)**

Back-of-envelope math for:

- Storage: data size per entity × entity count
- Bandwidth: QPS × payload size
- Cache: read QPS × object size × cache hit ratio complement

Use round numbers. Show your reasoning. Being roughly right beats being precisely wrong.

**3. High-Level Design (10 min)**

Draw the major components: clients, load balancers, application servers, databases, caches, queues, CDN. Describe data
flow for the primary use case. This should be a whiteboard diagram, not a wall of text.

**4. Deep Dive (15–20 min)**

Pick 2–3 hard problems your high-level design creates and solve them in detail:

- How does the database handle 100k writes/sec? (sharding, connection pooling)
- How do you guarantee exactly-once delivery? (idempotency keys, deduplication)
- How do you keep a hot cache consistent? (TTL strategy, write-through, cache invalidation)

Let the interviewer guide which component to drill into — follow their interest.

### Common Mistakes to Avoid

- **Jumping to solutions before requirements.** Designing a globally consistent system when "eventual consistency is
  fine" wastes time and signals poor judgment.
- **Single points of failure everywhere.** Every stateful component needs a replication/failover story.
- **Ignoring the data model.** How you store data determines what queries are fast. Sketch the schema early.
- **Hand-waving at scale.** "We'll just add more servers" is not an answer. Explain *how* — consistent hashing, sharding
  key selection, read replicas.
- **Neglecting failure modes.** What happens when the cache is cold? When Kafka consumer lag spikes? When a database
  node goes down mid-transaction?
- **Over-engineering toy systems.** A URL shortener doesn't need Kafka. Propose complexity proportional to actual
  requirements.

### What FAANG Interviewers Actually Look For

| Signal              | What Demonstrates It                                                                               |
|---------------------|----------------------------------------------------------------------------------------------------|
| Communication       | You narrate your thinking; you check in; you handle disagreement gracefully                        |
| Breadth             | You know the standard components and when to use them                                              |
| Depth               | You can go one level deeper than the diagram on any component                                      |
| Judgment            | You make explicit trade-offs; you don't gold-plate simple problems                                 |
| Structured thinking | You move through phases methodically rather than free-associating                                  |
| Adaptability        | When the interviewer changes a constraint, you re-evaluate rather than defend your original design |

---

## Quick Reference: Topic Map by Interview Question

Use this to quickly find relevant topics when practicing a specific type of question.

### Storage & Databases

- "Design a key-value store" → topics 6, 8, 19, 38
- "Design a relational-scale write-heavy system" → topics 4, 5, 8, 9, 14
- "Design blob/file storage" → topics 12, 33

### Caching

- "Design a cache" → topics 6, 19, 38
- "Design a CDN" → topics 6, 12, 22

### Messaging & Streaming

- "Design a messaging system" → topics 5, 17, 32
- "Design a notification system" → topics 17, 36
- "Design a stream processor" → topics 17, 18, 26

### APIs & Services

- "Design an API gateway" → topics 7, 11, 21, 24
- "Design a rate limiter" → topics 21, 37

### Search

- "Design a search engine" → topics 20, 35
- "Design typeahead / autocomplete" → topics 6, 20

### Real-Time & Feeds

- "Design a social feed / timeline" → topics 6, 13, 17, 29
- "Design a ride-sharing system" → topics 11, 31

### Reliability & Coordination

- "Design a distributed lock / leader election" → topics 15, 25
- "Design a payment system" → topics 14, 26, 39
- "Design a web crawler" → topics 5, 13, 34

### Analytics & Scale

- "Estimate uniqueness / cardinality at scale" → topics 20, 27
- "Design an observability/monitoring system" → topics 23

---

## Directory Structure

```
system-design-interview/
├── README.md                          ← You are here
├── 01-foundations/
│   ├── 01-scalability/
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

## Contributing

Each topic folder will be populated by subsequent tasks in this project. The canonical structure for every topic is:

```
<topic-folder>/
├── README.md           # Concept, trade-offs, interview angles
├── docker-compose.yml  # Local infrastructure
├── experiment.py       # Runnable experiment
└── solutions/          # Reference implementations
```

If you find errors, gaps, or have improvements, open an issue or PR against the relevant topic folder.
