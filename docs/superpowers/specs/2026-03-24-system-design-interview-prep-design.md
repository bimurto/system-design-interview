# System Design Interview Prep — Design Spec

**Date:** 2026-03-24
**Target:** FAANG/top-tier interviews in 4-6 weeks
**Environment:** Docker + Docker Compose
**Language:** Python for code snippets
**Case study style:** Real-world reverse engineering

---

## Goal

Create a comprehensive, hands-on system design interview preparation resource as a collection of markdown files. The
content covers foundational concepts, advanced distributed systems topics, and real-world case studies — each with
embedded Docker Compose labs and Python code.

---

## Directory Structure

Each topic lives in its own folder containing a `README.md` (the main content) plus any lab files (`docker-compose.yml`,
Python scripts) as actual files alongside it.

```
system-design-interview/
├── README.md                        # Study guide + navigation order + environment setup
│
├── 01-foundations/
│   ├── 01-scalability/
│   │   ├── README.md
│   │   ├── docker-compose.yml
│   │   └── experiment.py
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
│       ├── README.md
│       ├── docker-compose.yml
│       └── experiment.py
│
├── 02-advanced/
│   ├── 01-consistent-hashing/
│   ├── 02-distributed-transactions/
│   ├── 03-consensus-paxos-raft/
│   ├── 04-event-driven-architecture/
│   ├── 05-message-queues-kafka/
│   ├── 06-stream-processing/
│   ├── 07-distributed-caching/
│   ├── 08-search-systems/
│   ├── 09-rate-limiting-algorithms/    # Theory: token bucket, leaky bucket, sliding window
│   ├── 10-cdn-and-edge/
│   ├── 11-observability/
│   ├── 12-security-at-scale/
│   ├── 13-service-discovery-coordination/  # Zookeeper, etcd, Consul
│   ├── 14-idempotency-exactly-once/
│   └── 15-probabilistic-data-structures/   # Bloom filters, HyperLogLog, Count-Min Sketch
│
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
    ├── 10-rate-limiter/                # Distributed enforcement: building a real rate limiter service
    ├── 11-distributed-cache/
    └── 12-payment-system/
```

Each topic folder contains at minimum:

- `README.md` — all written content following the template
- `docker-compose.yml` — lab setup
- `experiment.py` — Python experiment script
- Additional scripts as needed (e.g., `break_it.py`, `load_test.py`)

### Rate Limiting Differentiation

`02-advanced/09-rate-limiting-algorithms.md` covers **theory**: how each algorithm works, trade-offs, when to use which.
It includes a full Docker Compose lab like all other files; the experiment script is a single-process Python demo (no
multi-node setup needed to illustrate the algorithms).

`03-case-studies/10-rate-limiter.md` covers **building a distributed rate limiter service**: multi-node enforcement,
Redis-backed shared state, API gateway integration, and failure behavior under partition.

---

## Content Template Per File

Every file (foundations, advanced, case study) follows this structure:

### Foundations & Advanced Files

1. **Prerequisites** — links to files that should be read first
2. **Concept** — deep explanation of the topic
3. **How it works** — internals, trade-offs, failure modes
4. **Interview talking points** — what interviewers want to hear
5. **Hands-on lab** — Docker Compose setup + Python code (self-contained, ~20-30 min)
6. **Real-world examples** — how major companies use this (sourced from public engineering blogs, papers, or conference
   talks — no speculation)
7. **Common mistakes** — what candidates get wrong

### Case Study Files

1. **Prerequisites** — links to foundation/advanced files to read first
2. **The Problem at Scale** — real numbers and scale context
3. **Requirements** — functional + non-functional + capacity estimation
4. **High-Level Architecture** — ASCII component diagram + rationale
5. **Deep Dives** — 3-4 hardest sub-problems with solutions
6. **How It Actually Works** — comparison to real-world implementation (sourced from public engineering blogs, papers,
   or conference talks)
7. **Hands-on Lab** — simplified but real Docker Compose + Python implementation (~20-30 min, max 5 containers)
8. **Interview Checklist** — 10 interviewer questions with answers

---

## Lab Format

Each hands-on lab lives as real files inside the topic folder. The `README.md` references them with instructions:

- **Setup:** `docker-compose.yml` — run `docker compose up -d` from the topic folder
- **Experiment:** `experiment.py` — Python script demonstrating the concept; run with `python experiment.py`
- **Break it:** Intentional failure scenarios (kill containers, overload, stop replicas). Network partition simulation
  uses `docker network disconnect` where needed — this requires Docker socket access. On macOS with Docker Desktop,
  `tc netem` is unavailable; each affected lab documents a Docker-native fallback.
- **Observe:** `README.md` includes what to look for in logs/metrics to confirm understanding
- **Teardown:** `docker compose down` — documented in `README.md`

**Lab constraints (applied consistently across all files):**

- Time budget: ~20-30 minutes per lab
- Max containers: 5 per lab
- No external internet required — all images pulled from Docker Hub

Labs are fully self-contained — no shared state between labs.

---

## Navigation

The README provides an explicit recommended reading order:

1. All of `01-foundations/` in numbered order
2. All of `02-advanced/` in numbered order
3. All of `03-case-studies/` in numbered order

Each file also links to prerequisite files and follow-up files for non-linear navigation.

---

## Reader Environment Prerequisites

The README will include a setup section covering:

- Docker Desktop (macOS/Windows) or Docker Engine + Docker Compose plugin (Linux)
- Python 3.10+ with `pip`
- Required Python packages: `redis`, `psycopg2-binary`, `kafka-python`, `requests`, `flask`
- No cloud account required

## Timeline Clarification

**4-6 weeks is the reader's study timeline**, not the content build timeline. The content is created first, then used as
a study guide. A reader spending ~2 hours/day can complete the material in 4-6 weeks (1 foundation file/day → 1 advanced
file/day → 1-2 case studies/week).

## Scope

**In scope:**

- 12 foundation topics
- 15 advanced topics
- 12 case studies
- All with Docker Compose labs and Python code
- README with study guide, navigation, and environment setup

**Out of scope:**

- Video content
- Interactive quizzes
- Cloud infrastructure (AWS/GCP) — local Docker only
- Mock interview simulation tooling

---

## Success Criteria

- Every concept can be demonstrated locally with `docker compose up`
- Each case study answers: what are the hard problems, what are the trade-offs, how does the real system solve it
- A reader can go from zero to confident in 4-6 weeks following the numbered order
