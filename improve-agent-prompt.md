# Content Improvement Agent Prompt Template

## Template (used per directory)

```
You are reviewing and improving ONE topic folder in a system design interview prep course
targeting senior/staff engineers preparing for FAANG interviews.

**Your target directory:** BASE_DIR

**Context:**
- Audience: engineers with 3+ years experience targeting staff/senior FAANG roles
- Each folder is self-contained with no shared state between folders
- Standard structure: README.md (concept + trade-offs + interview talking points),
  docker-compose.yml (local infra), experiment.py (runnable Python demo)
- Some folders also have: app.py, nginx.conf, or other service files

**Your task:**
1. Read ALL files in the target directory
2. Identify and implement improvements in these areas:
   - README.md: depth of concept explanation, accuracy of trade-offs, quality of
     interview talking points, missing gotchas or failure modes, diagram clarity
   - experiment.py: does the demo clearly illustrate the concept? Are edge cases
     shown? Is output readable and educational?
   - docker-compose.yml: correct services for the concept, proper resource config,
     health checks present?
   - Any other files (app.py, nginx.conf, etc.): correctness and clarity

**Constraints:**
- ONLY modify files inside BASE_DIR — do NOT touch any other folder
- Preserve the existing file structure
- Keep experiments locally runnable with Docker + Python (no cloud dependencies)
- Depth over basics — this audience knows fundamentals
- Do not add new files unless clearly missing (e.g., a missing experiment.py)

**Return:** A 3-5 bullet summary of what you improved and why.
```

## All 39 Directories

01-foundations/01-scalability
01-foundations/02-cap-theorem
01-foundations/03-consistency-models
01-foundations/04-replication
01-foundations/05-partitioning-sharding
01-foundations/06-caching
01-foundations/07-load-balancing
01-foundations/08-databases-sql-vs-nosql
01-foundations/09-indexes
01-foundations/10-networking-basics
01-foundations/11-api-design
01-foundations/12-blob-object-storage
01-foundations/13-proxies-reverse-proxies
01-foundations/14-failure-modes-reliability
02-advanced/01-consistent-hashing
02-advanced/02-distributed-transactions
02-advanced/03-consensus-paxos-raft
02-advanced/04-event-driven-architecture
02-advanced/05-message-queues-fundamentals
02-advanced/06-message-queues-kafka
02-advanced/07-stream-processing
02-advanced/08-distributed-caching
02-advanced/09-search-systems
02-advanced/10-rate-limiting-algorithms
02-advanced/11-cdn-and-edge
02-advanced/12-observability
02-advanced/13-security-at-scale
02-advanced/14-service-discovery-coordination
02-advanced/15-idempotency-exactly-once
02-advanced/16-probabilistic-data-structures
02-advanced/17-database-internals
02-advanced/18-backpressure-flow-control
02-advanced/19-multi-region-architecture
03-case-studies/01-url-shortener
03-case-studies/02-twitter-timeline
03-case-studies/03-youtube
03-case-studies/04-uber
03-case-studies/05-whatsapp
03-case-studies/06-google-drive
03-case-studies/07-web-crawler
03-case-studies/08-search-engine
03-case-studies/09-notification-system
03-case-studies/10-rate-limiter
03-case-studies/11-distributed-cache
03-case-studies/12-payment-system
