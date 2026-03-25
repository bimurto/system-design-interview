# Case Study: Twitter Timeline

**Prerequisites:** `../../01-foundations/04-replication/`, `../../01-foundations/06-caching/`, `../../02-advanced/05-message-queues-kafka/`, `../../02-advanced/07-distributed-caching/`

---

## The Problem at Scale

Twitter's home timeline shows the N most recent tweets from people you follow, sorted chronologically. The naive implementation — query all tweets from all users you follow, sort, paginate — does not survive at scale:

| Metric | Value |
|---|---|
| Total users | 300 million |
| Active users (DAU) | 100 million |
| Tweets per day | 600 million |
| Timeline reads per day | 30 billion |
| Peak read RPS | 300,000 |
| Avg followers per user | 200 |
| Max followers (celebrities) | 100+ million |

A naive `SELECT` at 300K RPS, each joining across millions of follow relationships, would require thousands of database servers. The solution is to pre-compute timelines and store them in Redis.

---

## Requirements

### Functional
- Home timeline: see tweets from people you follow, most recent first
- Post a tweet: visible in followers' timelines within seconds
- Follow/unfollow: timeline updates accordingly
- Infinite scroll: paginate backwards in time

### Non-Functional
- Timeline read latency P99 < 100ms
- Tweet visible to followers within 5 seconds of posting
- 99.9% availability (missing a tweet for a few seconds is acceptable)
- Timeline stores last 800 tweets per user (older entries evicted)

### Capacity Estimation

| Metric | Calculation | Result |
|---|---|---|
| Fan-out writes/day | 600M tweets × 200 avg followers | 120 billion Redis writes |
| Redis storage/timeline | 800 tweets × 8B (tweet ID + score) | ~6.4 KB per user |
| Total Redis for timelines | 100M active users × 6.4 KB | 640 GB |
| Kafka throughput | 600M tweets / 86400s | ~7,000 messages/s |
| Fan-out worker throughput | 120B writes / 86400s | ~1.4M Redis writes/s |

---

## High-Level Architecture

```
                          ┌─────────────────────────────────────┐
  POST /tweet             │           Write Path                 │
       │                  │                                      │
       ▼                  │  Flask API → Postgres (store tweet)  │
  Load Balancer           │           → Kafka (publish event)    │
       │                  │                                      │
       │                  │  Fan-out workers (Kafka consumers):  │
       │                  │   per follower: ZADD timeline:{id}   │
       │                  └─────────────────────────────────────┘
       │
  GET /timeline           ┌─────────────────────────────────────┐
       │                  │           Read Path                  │
       ▼                  │                                      │
  Load Balancer ──────────►  Flask API                           │
                          │    1. ZREVRANGE timeline:{user_id}   │
                          │       → Redis (pre-built timeline)   │
                          │    2. SELECT tweets FROM celebs      │
                          │       → Postgres (celebrity tweets)  │
                          │    3. Merge + sort + return          │
                          └─────────────────────────────────────┘

  ┌──────────────┐  ┌──────────────────────┐  ┌───────────────────┐
  │   Postgres   │  │    Kafka             │  │      Redis        │
  │  (tweets,    │  │  topic: tweets       │  │  sorted sets      │
  │   follows,   │  │  partitioned by      │  │  timeline:{uid}   │
  │   users)     │  │  user_id             │  │  score=timestamp  │
  └──────────────┘  └──────────────────────┘  └───────────────────┘
```

**Postgres** stores the authoritative copy of all tweets, users, and follow relationships. It is only read directly for: (a) timeline construction for new/inactive users whose Redis timeline has expired, and (b) fetching celebrity tweets at read time (hybrid approach).

**Kafka** decouples the tweet write from the fan-out operation. Without Kafka, a single tweet from a user with 1M followers would block the API for minutes. With Kafka, the API writes the tweet and publishes an event in <10ms. Fan-out workers process the event asynchronously, usually within seconds.

**Redis sorted sets** are the timeline store. `ZADD timeline:{user_id} {timestamp} {tweet_id}` pushes a tweet ID into the user's timeline at O(log n). `ZREVRANGE timeline:{user_id} 0 19` reads the 20 most recent tweet IDs in O(log n + 20). Tweet content is then hydrated from Postgres or a tweet cache. The sorted set is trimmed to 800 entries to bound memory usage.

---

## Deep Dives

### 1. Fan-out on Write vs Fan-out on Read

**Fan-out on write (push model):** when a tweet is posted, immediately copy the tweet ID into every follower's Redis timeline. Reads are O(1) — just a Redis range query. Writes are O(followers) per tweet. This is the dominant approach for users with typical follower counts.

*Problem:* Katy Perry had 108M Twitter followers. If she posts a tweet, the fan-out worker must execute 108M Redis writes. At 1M writes/second, this takes 108 seconds — and she might tweet 10 times/day. During that period, followers' timelines are temporarily stale.

**Fan-out on read (pull model):** when a user requests their timeline, fetch the N most recent tweets from each person they follow and merge. The timeline is always fresh. But at 300K reads/second, each needing to query 200+ followed-user timelines, this is 60M Postgres queries/second — completely infeasible.

**Twitter's hybrid approach:** use fan-out on write for regular users (< ~1M followers). For celebrities (≥ ~1M followers), skip the fan-out entirely. At timeline read time, the app takes the pre-built Redis timeline and appends any recent tweets from celebrities the user follows (a small Postgres query). The result is merged and sorted. This bounds the worst-case fan-out write time while keeping reads fast.

### 2. Redis Sorted Sets for Timeline Storage

A Redis sorted set (`ZSET`) stores members (tweet IDs as strings) with float scores (Unix timestamps). Key operations:

- `ZADD timeline:123 1700000000.0 "tweet_456"` — O(log n) insert
- `ZREVRANGE timeline:123 0 19` — O(log n + 20) read, newest-first
- `ZCARD timeline:123` — O(1) count
- `ZREMRANGEBYRANK timeline:123 0 -801` — O(log n + k) trim to 800 entries

The sorted set is **memory-efficient**: storing 800 tweet IDs (8 bytes each) with scores (8 bytes) ≈ 12.8 KB per user timeline. For 100M active users: ~1.3 TB. This is the primary Redis memory cost for Twitter's timeline system.

Why not a plain list? Sorted sets allow arbitrary insertion at any position (an older tweet could be inserted late, e.g., after network delay) and range queries by time window. Lists only support head/tail operations efficiently.

### 3. Timeline Delivery and Consistency

After a tweet is posted, how quickly does it appear in followers' timelines? The latency depends on the Kafka consumer lag and fan-out write throughput:

1. API writes tweet to Postgres + publishes to Kafka: ~5ms
2. Fan-out worker picks up the Kafka message: ~10–100ms (consumer polling interval)
3. Fan-out writes to Redis sorted sets: ~1ms per 1000 followers (pipeline)
4. Next timeline read picks up the new tweet: immediate

For a user with 200 followers, end-to-end tweet visibility ≈ 200ms. For a user with 100K followers: ~500ms. For a celebrity bypassed by the hybrid approach: followers see the tweet on their next read immediately (Postgres query at read time).

The consistency model is **eventual consistency**: timelines are slightly stale immediately after a tweet is posted, converging within seconds.

---

## How It Actually Works

Twitter's engineering blog describes their timeline system in detail in the 2012 QCon London talk "Timelines at Scale" by Raffi Krikorian. Key details:

- Tweet IDs are 64-bit Snowflake IDs, not sequential integers. This allows distributed ID generation across multiple datacenters.
- Timelines are stored in Redis as sorted sets (exactly as described here). Twitter called this the "Timeline Service."
- The celebrity threshold was approximately 1M followers in 2012. Users above this threshold were placed on a "celebrity list" and their tweets were fetched from a separate "Social Graph Service" at read time.
- Fan-out workers were written in Scala and deployed as Finagle services. The Kafka topic was partitioned by user_id to ensure all tweets from one user were processed by the same worker (ordering guarantee).
- Twitter migrated from Ruby on Rails to this architecture between 2010 and 2012, driven by the 2011 "Fail Whale" outages during high-traffic events (World Cup, elections).

Source: Raffi Krikorian, "Timelines at Scale," QCon London 2012; Twitter Engineering Blog, "The Infrastructure Behind Twitter: Scale" (2017).

---

## Hands-on Lab

**Time:** ~20–25 minutes
**Services:** `db` (Postgres 15), `cache` (Redis 7), `zookeeper` (CP ZooKeeper 7.4), `kafka` (CP Kafka 7.4)

### Setup

```bash
cd system-design-interview/03-case-studies/02-twitter-timeline/
docker compose up -d
# Kafka takes ~30-45s to fully start
docker compose logs -f kafka  # wait for "started (kafka.server.KafkaServer)"
```

### Experiment

```bash
python experiment.py
```

The script installs `kafka-python` automatically, then runs six phases:

1. **Seed data:** 50 regular users + 3 celebrities, follow relationships in Postgres
2. **Post tweets:** 15 tweets published to Kafka topic `tweets`
3. **Fan-out worker:** consume from Kafka, push tweet IDs to follower Redis timelines, measure per-follower latency
4. **Read timeline:** ZREVRANGE from Redis, hydrate content from Postgres
5. **Celebrity problem:** simulate fan-out to 10–1000 followers, plot latency growth
6. **Hybrid approach:** demonstrate merge of Redis timeline + celebrity Postgres query at read time

### Break It

**Simulate backpressure on the fan-out queue:**

```bash
# Pause the fan-out consumer and post many tweets
docker compose exec kafka kafka-console-producer \
  --bootstrap-server localhost:9092 --topic tweets &
# (type some fake JSON messages)

# Meanwhile check Kafka consumer lag
docker compose exec kafka kafka-consumer-groups \
  --bootstrap-server localhost:9092 \
  --describe --group fanout-worker
```

With consumer lag growing, timelines become stale. This is the real-world tradeoff: during traffic spikes (breaking news, major events), fan-out workers fall behind and timelines lag by minutes. Twitter addressed this with auto-scaling fan-out workers and prioritising fan-out for highly-followed users.

### Observe

After running phase 5, observe the linear relationship between follower count and fan-out time. At 1000 followers, fan-out takes only a few milliseconds (Redis pipeline is efficient). The problem at 100M followers is not just raw latency — it is the total write amplification load on Redis at peak hours.

```bash
# Check Redis memory usage after all phases
docker compose exec cache redis-cli INFO memory | grep used_memory_human
# Count timeline entries for a user
docker compose exec cache redis-cli ZCARD timeline:1
```

### Teardown

```bash
docker compose down -v
```

---

## Interview Checklist

1. **Q: Why can't you just query Postgres for the home timeline on every request?**
   A: At 300K RPS, each query would need to join tweets with the follow graph for that user (up to 1000+ followed users), sort by timestamp, and paginate. A single query might scan millions of rows. Even with indexes, the aggregate load (300K such queries/second) would require thousands of DB replicas. Pre-computing timelines in Redis converts this to an O(1) sorted set range query.

2. **Q: How do you handle the fan-out for a celebrity with 100M followers?**
   A: The hybrid approach: skip fan-out for users above a follower threshold (~1M). At timeline read time, fetch the celebrity's recent tweets from Postgres (or a tweet cache) and merge with the pre-built Redis timeline. This bounds the worst-case fan-out write time while adding a small read-time query.

3. **Q: What is the Kafka topic partitioning strategy and why does it matter?**
   A: Partition by `user_id` of the tweeter. This ensures all tweets from the same user are processed by the same consumer, guaranteeing ordering of tweets in follower timelines. Without this, two tweets from the same user could be processed out of order, violating chronological sort.

4. **Q: How much Redis memory does the timeline system require?**
   A: 100M active users × 800 tweet IDs × ~16 bytes (ID + score) ≈ 1.3 TB. This is spread across a Redis cluster (Cluster mode or multiple shards). Tweet IDs are the unit of storage — actual tweet content is hydrated separately from a tweet cache or Postgres.

5. **Q: What happens to a user's timeline when they follow someone new?**
   A: Two approaches: (1) Eager backfill: immediately copy the new followee's last N tweet IDs into the follower's Redis timeline. Simple but expensive if the new followee has thousands of tweets. (2) Lazy merge: on the next timeline read, merge the followee's recent tweets with the existing timeline and populate the cache. Twitter used eager backfill for the immediate update, bounded to the last 800 tweets.

6. **Q: How do you handle retweets and quote tweets in the fan-out model?**
   A: A retweet is stored as a new tweet with a reference to the original tweet ID. The fan-out pushes the retweet's own ID (not the original's) into follower timelines. At hydration time, the client sees the retweet wrapper and fetches the original tweet content. This keeps the fan-out logic identical for all tweet types.

7. **Q: What is the consistency model for timelines? Is it acceptable?**
   A: Eventual consistency, typically converging within 1–5 seconds. For a social media feed, this is completely acceptable — users don't notice a 2-second delay before a tweet appears. The alternative (synchronous fan-out in the write path) would either slow down tweet posting or require massive write throughput in the hot path.

8. **Q: How would you handle a user who unfollows someone — stale tweets in timeline?**
   A: On unfollow, trigger a background job to remove all tweet IDs from the unfollowed user's Redis sorted set from the follower's timeline. This is a `ZREM` for each tweet ID. In practice, this is bounded by the timeline size (max 800 entries), so it's a bounded operation. Timeline TTL (24h) also naturally expires stale entries.

9. **Q: How does Twitter ensure tweet delivery during a datacenter outage?**
   A: Kafka provides durability (messages are replicated across brokers). If a fan-out worker dies, Kafka consumer group rebalancing assigns the partition to a healthy worker, which continues from the last committed offset. Tweets are never lost, but timelines may lag during the failover window (seconds to minutes).

10. **Q: Walk me through the complete write path for a tweet.**
    A: (1) User posts tweet → API server validates, writes to Postgres tweets table, returns tweet ID to client (<50ms). (2) API publishes `{tweet_id, user_id, content, timestamp}` to Kafka topic partitioned by user_id. (3) Fan-out consumer picks up event, queries Postgres for all followers. (4) For each follower (excluding celebrities), executes `ZADD timeline:{follower_id} {timestamp} {tweet_id}` via Redis pipeline. (5) Trims timeline to 800 entries. Total fan-out time: ~200ms for avg user (200 followers), up to minutes for major celebrities (handled by hybrid approach).
