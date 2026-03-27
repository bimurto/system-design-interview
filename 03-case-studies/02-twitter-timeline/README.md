# Case Study: Twitter Timeline

**Prerequisites:** `../../01-foundations/04-replication/`, `../../01-foundations/06-caching/`, `../../02-advanced/05-message-queues-kafka/`, `../../02-advanced/07-distributed-caching/`

---

## The Problem at Scale

Twitter's home timeline shows the N most recent tweets from people you follow, sorted chronologically. The naive implementation — query all tweets from all users you follow, sort, paginate — does not survive at scale:

| Metric | Value |
|---|---|
| Total registered users | 350 million |
| Daily active users (DAU) | 100 million |
| Tweets per day | 500 million |
| Timeline reads per day | 28 billion (~280× read/write ratio) |
| Peak read RPS | ~300,000 |
| Peak write RPS (tweets) | ~6,000 |
| Avg followers per user | 200 |
| Max followers (celebrities) | 100+ million |

A naive `SELECT` at 300K RPS, each joining across millions of follow relationships, would require thousands of database servers. The solution is to pre-compute timelines and store them in Redis.

---

## Step 1: Clarify Requirements (3–5 min)

Before designing, drive the scope conversation with the interviewer. Key questions:

**Functional scope:**
- Home timeline only (followed users), or also notifications/mentions?
- Should tweets appear strictly chronologically, or is ranking/ML scoring in scope?
- Do we need to support retweets and quote tweets in the same pipeline?
- What is the follow graph scale? (avg following count, max following count)
- Should unfollow immediately remove tweets from the timeline?

**Non-functional parameters:**
- What is the acceptable end-to-end tweet visibility latency? (seconds vs sub-second)
- What is the timeline read latency SLA? P99 < 100ms? P50 < 10ms?
- What is the availability target? (99.9% = 8.7 hrs downtime/year; 99.99% = 52 min)
- Is it acceptable to show a slightly stale timeline (eventual consistency) or must it be strongly consistent?
- How far back should timeline history go? (last 800 tweets is Twitter's actual bound)

**Answers to assume (if not specified):**
- Home timeline, chronological sort, retweets included
- Tweet visibility within 5 seconds of posting for most users
- Read latency P99 < 100ms
- 99.9% availability; eventual consistency is acceptable
- Timeline stores last 800 tweet IDs per user

### Functional Requirements
- Home timeline: see tweets from people you follow, most recent first
- Post a tweet: visible in followers' timelines within seconds
- Follow/unfollow: timeline updates accordingly
- Infinite scroll: paginate backwards in time

### Non-Functional Requirements
- Timeline read latency P99 < 100ms
- Tweet visible to followers within 5 seconds of posting
- 99.9% availability (missing a tweet for a few seconds is acceptable)
- Timeline stores last 800 tweets per user (older entries evicted)

---

## Step 2: Capacity Estimation (3–5 min)

Work through these numbers out loud to anchor design decisions.

| Metric | Calculation | Result |
|---|---|---|
| Tweets/second (avg) | 500M / 86,400s | ~5,800 tweets/s |
| Tweets/second (peak, 3× avg) | 5,800 × 3 | ~17,000 tweets/s |
| Fan-out writes/day | 500M tweets × 200 avg followers | 100 billion Redis writes |
| Fan-out writes/second (avg) | 100B / 86,400 | ~1.2M Redis writes/s |
| Fan-out writes/second (peak) | 1.2M × 3 | ~3.5M Redis writes/s |
| Redis storage per timeline | 800 tweet IDs × 8B + 800 scores × 8B | ~12.8 KB per user |
| Total Redis for active timelines | 100M users × 12.8 KB | ~1.3 TB |
| Kafka throughput | 500M tweets / 86,400s | ~5,800 messages/s |
| Tweet storage (Postgres) | 500M tweets/day × 365 × ~200 bytes | ~36 TB/year |

**Key insight:** The read/write ratio is ~280:1. This strongly favors pre-computing (denormalizing) timelines to optimize for reads at the cost of write amplification.

**Redis cluster sizing:** 1.3 TB raw + 2× replication = ~2.6 TB Redis memory. At 64 GB per node, that's ~41 Redis nodes. In practice, use Redis Cluster with sharding by `user_id`.

---

## Step 3: High-Level Design (10 min)

```
                          ┌─────────────────────────────────────┐
  POST /tweet             │           Write Path                 │
       │                  │                                      │
       ▼                  │  API Server                          │
  Load Balancer           │   1. Write tweet to Postgres         │
       │                  │   2. Publish event to Kafka          │
       │                  │   (returns to client after step 2)   │
       │                  │                                      │
       │                  │  Fan-out Workers (Kafka consumers):  │
       │                  │   - Skip celebrities (≥1M followers) │
       │                  │   - ZADD timeline:{follower_id}      │
       │                  │     for each regular follower        │
       │                  │   - ZREMRANGEBYRANK (trim to 800)    │
       │                  └─────────────────────────────────────┘

  GET /timeline           ┌─────────────────────────────────────┐
       │                  │           Read Path                  │
       ▼                  │                                      │
  Load Balancer ─────────►│  API Server                          │
                          │   1. ZREVRANGE timeline:{user_id}    │
                          │      from Redis (pre-built timeline) │
                          │   2. Fetch recent tweets from        │
                          │      celebrities user follows        │
                          │      (Postgres or tweet cache)       │
                          │   3. Merge + sort + hydrate content  │
                          │   4. Return top 20 tweets            │
                          └─────────────────────────────────────┘

  ┌──────────────┐  ┌──────────────────────┐  ┌───────────────────┐
  │   Postgres   │  │    Kafka             │  │      Redis        │
  │  (tweets,    │  │  topic: tweets       │  │  sorted sets      │
  │   follows,   │  │  partitioned by      │  │  timeline:{uid}   │
  │   users)     │  │  user_id             │  │  score=timestamp  │
  └──────────────┘  └──────────────────────┘  └───────────────────┘
```

**Postgres** stores the authoritative copy of all tweets, users, and follow relationships. It is only read directly for: (a) timeline cold-start reconstruction for new/inactive users whose Redis key has expired, and (b) fetching celebrity tweets at read time (hybrid approach).

**Kafka** decouples the tweet write from the fan-out operation. Without Kafka, a single tweet from a user with 1M followers would block the API for minutes. With Kafka, the API writes the tweet and publishes an event in <10ms. Fan-out workers process the event asynchronously, typically within seconds. Kafka also provides durability: if a worker crashes mid-fan-out, the message is not acknowledged, and a rebalanced worker replays it.

**Redis sorted sets** are the timeline store. `ZADD timeline:{user_id} {timestamp} {tweet_id}` pushes a tweet ID at O(log n). `ZREVRANGE timeline:{user_id} 0 19` reads the 20 most recent tweet IDs at O(log n + 20). Tweet content is hydrated from Postgres or a tweet content cache. The sorted set is trimmed to 800 entries on every write.

---

## Step 4: Deep Dives (15–20 min)

### 4.1 Fan-out on Write vs Fan-out on Read

**Fan-out on write (push model):** when a tweet is posted, immediately copy the tweet ID into every follower's Redis timeline. Reads are O(1) — just a Redis range query. Writes are O(followers) per tweet. This is the dominant approach for users with typical follower counts.

*Problem:* Katy Perry had 108M Twitter followers. If she posts a tweet, the fan-out worker must execute 108M Redis writes. At 1M writes/second (pipelined), this takes 108 seconds — and she might tweet 10 times/day. During that window, followers' timelines are temporarily stale, and the fan-out queue backs up for everyone.

**Fan-out on read (pull model):** when a user requests their timeline, fetch the N most recent tweets from each person they follow and merge. The timeline is always fresh. But at 300K reads/second, each needing to query 200+ followed-user timelines, this is 60M Postgres queries/second — completely infeasible, even with aggressive caching.

**Twitter's hybrid approach:** use fan-out on write for regular users (< ~1M followers). For celebrities (≥ ~1M followers), skip the fan-out entirely. At timeline read time, fetch the celebrity's recent tweets from Postgres (or a tweet cache) and merge with the pre-built Redis timeline. The result is sorted in the API layer. This bounds the worst-case fan-out write time while keeping reads fast. Most users follow at most a handful of celebrities, so the extra read-time DB query is small.

### 4.2 Redis Sorted Sets for Timeline Storage

A Redis sorted set (`ZSET`) stores members (tweet IDs as strings) with float scores (Unix timestamps). Key operations:

- `ZADD timeline:123 1700000000.0 "tweet_456"` — O(log n) insert
- `ZREVRANGE timeline:123 0 19` — O(log n + 20) read, newest-first
- `ZREVRANGEBYSCORE timeline:123 {max_ts} {min_ts} LIMIT 0 20` — paginate by timestamp window (for cursor-based infinite scroll)
- `ZCARD timeline:123` — O(1) count
- `ZREMRANGEBYRANK timeline:123 0 -801` — O(log n + k) trim to 800 entries

The sorted set is **memory-efficient**: 800 tweet IDs (8 bytes each as integers stored as strings) + 800 scores (8 bytes each as floats) ≈ 12.8 KB per user timeline. For 100M active users: ~1.3 TB across the Redis cluster.

**Why not a plain list?** Sorted sets allow arbitrary insertion at any score position (an older tweet inserted late due to network delay will land at the right position) and range queries by time window. Lists only support O(1) head/tail operations; insertion at an arbitrary position is O(n).

**Why not store tweet content directly?** Storing full tweet text in Redis would balloon memory. Instead, the sorted set stores only tweet IDs (8 bytes). Content hydration is a separate cache lookup or DB read. This separation also simplifies deletion: if a tweet is deleted, you remove it from one content store, not from 100M Redis timeline entries.

### 4.3 Timeline Delivery and Consistency

After a tweet is posted, how quickly does it appear in followers' timelines?

1. API writes tweet to Postgres + publishes to Kafka: ~5–10ms
2. Fan-out worker picks up the Kafka message: ~10–100ms (Kafka consumer poll interval)
3. Fan-out writes to Redis sorted sets via pipeline: ~1ms per 1,000 followers
4. Next timeline read picks up the new tweet: immediate

For a user with 200 followers, end-to-end tweet visibility ≈ 200ms. For a user with 100K followers: ~500ms. For a celebrity bypassed by the hybrid approach: followers see the tweet on their next read immediately (Postgres query at read time).

The consistency model is **eventual consistency**: timelines are slightly stale immediately after a tweet is posted, converging within seconds. This is acceptable for a social media feed — users tolerate a 1–5 second delay.

### 4.4 Timeline Cold Start and Cache Eviction

Redis key TTL is typically set to 24 hours. When a user has not accessed Twitter in >24 hours, their timeline key expires. On their next login:

1. Redis `ZREVRANGE` returns empty.
2. API falls back to Postgres: fetch the user's follow list, then query the N most recent tweets from each followee, merge, sort.
3. Write the reconstructed timeline back into Redis (backfill).

This cold-start reconstruction is expensive for users who follow thousands of accounts. In production, Twitter pre-warmed timelines for users predicted to return (based on past activity patterns) before they logged in.

**New follow backfill:** when user A follows user B, immediately copy the last N tweet IDs from B's Postgres tweet history into A's Redis timeline. Twitter bounded this backfill to the last 800 entries. Without backfill, new follows would not appear until B posts a new tweet.

### 4.5 Failure Modes and Mitigations

| Failure | Symptom | Mitigation |
|---|---|---|
| Redis node failure | Timeline reads fall back to Postgres (slow) | Redis Cluster: 3 primaries, 1 replica each; automatic failover in <30s |
| Kafka broker failure | Fan-out lag grows; tweets not pushed | Kafka replication factor ≥ 2; fan-out consumer retries with offset replay |
| Partial fan-out (worker crash mid-tweet) | Some followers see tweet, others don't | Kafka at-least-once delivery; worker commits offset only after all ZADD succeed; idempotent ZADD is safe to replay |
| Fan-out queue lag during traffic spike | Timelines stale by minutes | Fan-out worker horizontal auto-scaling; celebrity hybrid reduces peak write load |
| Postgres follower-list query bottleneck | Slow fan-out as follower count grows | Cache follow lists in Redis; update cache on follow/unfollow events |
| Snowflake ID service failure | No new tweet IDs can be generated | Snowflake is stateless and horizontally scalable; deploy 3+ instances with fallback |
| Timeline cold-start thundering herd | Many users log in simultaneously after outage | Stagger backfill using queue; use Postgres read replicas for hydration |

### 4.6 Partitioning Strategy

**Kafka partitioning:** partition by `user_id` of the tweeter. This ensures all tweets from the same user are processed by the same consumer, guaranteeing ordering. Without this, two tweets from the same user could be processed out of order, inserting an older tweet into timelines after a newer one (though ZADD with score=timestamp self-heals this).

**Redis sharding:** shard by `user_id` using consistent hashing. Redis Cluster uses 16,384 hash slots; timeline keys for a given user always land on the same shard. Celebrity users' follower timelines are spread evenly across all shards — no hotspot.

**Postgres read replicas:** the follow graph table is read-heavy during fan-out. Direct fan-out workers to read replicas; writes (new follows, unfollows) go to the primary.

---

## How It Actually Works

Twitter's engineering blog describes their timeline system in detail in the 2012 QCon London talk "Timelines at Scale" by Raffi Krikorian. Key details:

- Tweet IDs are 64-bit Snowflake IDs, not sequential integers. This allows distributed ID generation across multiple datacenters without coordination. The top 41 bits are millisecond timestamp, enabling time-ordering by ID alone — scores in the sorted set can be the tweet ID itself rather than a separate timestamp.
- Timelines are stored in Redis as sorted sets (exactly as described here). Twitter called this the "Timeline Service."
- The celebrity threshold was approximately 1M followers in 2012. Users above this threshold were placed on a "celebrity list" and their tweets were fetched from a separate "Social Graph Service" at read time.
- Fan-out workers were written in Scala and deployed as Finagle services. The Kafka topic was partitioned by user_id to ensure all tweets from one user were processed by the same worker (ordering guarantee).
- Twitter migrated from Ruby on Rails to this architecture between 2010 and 2012, driven by the 2011 "Fail Whale" outages during high-traffic events (World Cup, elections).
- In 2022 Twitter (now X) open-sourced parts of the recommendation algorithm, revealing that timelines now incorporate ML ranking signals on top of the chronological feed — the fan-out infrastructure remains, but scores can be ML relevance scores rather than raw timestamps.

Source: Raffi Krikorian, "Timelines at Scale," QCon London 2012; Twitter Engineering Blog, "The Infrastructure Behind Twitter: Scale" (2017).

---

## Hands-on Lab

**Time:** ~25–30 minutes
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

The script installs `kafka-python`, `psycopg2-binary`, and `redis` automatically, then runs seven phases:

1. **Seed data:** 50 regular users + 3 celebrities, follow relationships in Postgres
2. **Post tweets:** 15 tweets published to Kafka topic `tweets`
3. **Fan-out worker:** consume from Kafka, push tweet IDs to follower Redis timelines, measure per-follower latency
4. **Read timeline:** ZREVRANGE from Redis, hydrate content from Postgres
5. **Celebrity problem:** simulate fan-out to 10–1,000 followers, demonstrate linear scaling
6. **Hybrid approach:** demonstrate merge of Redis timeline + celebrity Postgres query at read time
7. **Cold start:** expire a timeline key and show reconstruction from Postgres

### Break It

**Simulate backpressure on the fan-out queue:**

```bash
# Check Kafka consumer lag after running the experiment
docker compose exec kafka kafka-consumer-groups \
  --bootstrap-server localhost:9092 \
  --describe --group fanout-worker
```

With consumer lag growing, timelines become stale. This is the real-world tradeoff: during traffic spikes (breaking news, major events), fan-out workers fall behind and timelines lag by minutes. Twitter addressed this with auto-scaling fan-out workers and the celebrity hybrid approach reducing peak write volume.

**Inspect Redis directly:**

```bash
# Check Redis memory usage after all phases
docker compose exec cache redis-cli INFO memory | grep used_memory_human

# Count timeline entries for user ID 1
docker compose exec cache redis-cli ZCARD timeline:1

# Read the timeline for user 1 (tweet IDs, newest first)
docker compose exec cache redis-cli ZREVRANGE timeline:1 0 9 WITHSCORES

# Inspect all timeline keys
docker compose exec cache redis-cli KEYS "timeline:*" | wc -l
```

**Inspect Postgres:**

```bash
# Connect to Postgres and explore the schema
docker compose exec db psql -U app -d twitter -c "
  SELECT u.username, COUNT(f.follower_id) AS followers
  FROM users u
  LEFT JOIN follows f ON f.followee_id = u.id
  GROUP BY u.username
  ORDER BY followers DESC
  LIMIT 10;
"
```

### Observe

After running phase 5, observe the linear relationship between follower count and fan-out time. At 1,000 followers, fan-out takes only a few milliseconds (Redis pipeline is efficient). The problem at 100M followers is not just raw latency — it is the total write amplification load on Redis at peak hours across all concurrent celebrity tweets.

After phase 7, observe the cold-start latency. Reconstructing a timeline from Postgres takes 10–50ms even in this local lab — at production scale with a user following 1,000 accounts, the join can easily exceed 500ms, justifying the pre-computation approach.

### Teardown

```bash
docker compose down -v
```

---

## Interview Checklist

1. **Q: Why can't you just query Postgres for the home timeline on every request?**
   A: At 300K RPS, each query would need to join tweets with the follow graph for that user (up to 1,000+ followed users), sort by timestamp, and paginate. A single query might scan millions of rows. Even with indexes and read replicas, the aggregate load (300K such queries/second) would require thousands of DB replicas. Pre-computing timelines in Redis converts this to an O(log n) sorted set range query at ~1ms latency.

2. **Q: How do you handle the fan-out for a celebrity with 100M followers?**
   A: The hybrid approach: skip fan-out for users above a follower threshold (~1M). At timeline read time, fetch the celebrity's recent tweets from Postgres (or a tweet cache) and merge with the pre-built Redis timeline. This bounds the worst-case fan-out write time while adding a small read-time query. Most users follow at most a few celebrities, so the extra read latency is bounded.

3. **Q: What is the Kafka topic partitioning strategy and why does it matter?**
   A: Partition by `user_id` of the tweeter. This ensures all tweets from the same user are processed by the same consumer, guaranteeing ordering of tweets in follower timelines. Without this, two tweets from the same user could be processed out of order — though since ZADD uses timestamp as the score, the sorted set would self-heal, but the ordering guarantee simplifies reasoning.

4. **Q: How much Redis memory does the timeline system require?**
   A: 100M active users × 800 tweet IDs × ~16 bytes (8B ID + 8B score) ≈ 1.3 TB raw. With replication (1 replica per shard), ~2.6 TB total Redis memory. This is spread across a Redis Cluster sharded by user_id. Tweet content is not stored in Redis — only IDs are; content is hydrated from a tweet cache or Postgres.

5. **Q: What happens to a user's timeline when they follow someone new?**
   A: Eager backfill: immediately copy the new followee's last N tweet IDs into the follower's Redis timeline via ZADD with historical timestamps. Twitter bounded this to the last 800 entries. The sorted set's score-based ordering means the newly backfilled tweets land at the correct positions chronologically. Without backfill, the new followee would not appear in the timeline until they post a new tweet.

6. **Q: How do you handle retweets and quote tweets in the fan-out model?**
   A: A retweet is stored as a new tweet record with a reference to the original tweet ID (`retweeted_tweet_id` column). The fan-out pushes the retweet's own ID into follower timelines. At hydration time, the API fetches the retweet record (which contains the original ID) and the original tweet content in a single batched query. This keeps fan-out logic identical for all tweet types.

7. **Q: What is the consistency model for timelines? Is it acceptable?**
   A: Eventual consistency, typically converging within 1–5 seconds. For a social media feed, this is completely acceptable — users do not notice a 2-second delay before a tweet appears. The alternative (synchronous fan-out in the write path) would either slow down tweet posting (P99 write latency becomes P99 of the slowest Redis write across all followers) or require massive write throughput in the hot path with no decoupling.

8. **Q: How would you handle a user who unfollows someone — stale tweets in timeline?**
   A: Trigger a background job to scan the follower's Redis sorted set and remove tweet IDs that belong to the unfollowed user. This requires knowing which tweet IDs in the sorted set belong to that user — either by storing metadata alongside the IDs, or by querying Postgres for the unfollowed user's tweet IDs and calling `ZREM` for each. The operation is bounded by the timeline size (max 800 entries). Alternatively, at read time, filter out tweets from users the reader no longer follows (lazy cleanup) — simpler but shows stale content briefly.

9. **Q: How does Twitter ensure tweet delivery during a datacenter outage?**
   A: Kafka provides durability (messages are replicated across brokers, typically replication factor 3). If a fan-out worker dies mid-processing, the Kafka offset is not committed; on rebalance, the same message is re-delivered to a healthy worker. Since ZADD is idempotent (adding the same member with the same score is a no-op), replaying messages is safe. Redis replicas (promoted to primary on failure) ensure timeline data survives a single node loss.

10. **Q: Walk me through the complete write path for a tweet.**
    A: (1) User posts tweet → API validates, writes to Postgres tweets table, returns tweet ID to client in <50ms. (2) API publishes `{tweet_id, user_id, content, timestamp}` to Kafka topic partitioned by user_id. (3) Fan-out consumer picks up event, queries follow list from cache (or Postgres read replica). (4) For each regular follower: `ZADD timeline:{follower_id} {timestamp} {tweet_id}` via Redis pipeline; `ZREMRANGEBYRANK` to trim to 800 entries. (5) Celebrity followers are skipped — they pull at read time. Total fan-out: ~200ms for avg user (200 followers), bounded for celebrities by the hybrid approach.

11. **Q: What happens if the Kafka publish step fails after the DB write succeeds?**
    A: The tweet is durably stored in Postgres but never fanned out. Options: (a) Outbox pattern — write the tweet and an outbox record to Postgres in a single transaction; a separate poller reads the outbox and publishes to Kafka, deleting the record after successful publish. This guarantees at-least-once delivery to Kafka. (b) Dual-write with retry — the API retries the Kafka publish with exponential backoff. This is simpler but risks losing the event if the API server crashes before the retry succeeds. Twitter likely uses option (a) or an equivalent CDC (Change Data Capture) mechanism.

12. **Q: How do you handle tweet deletion? Must it be immediate?**
    A: Soft delete in Postgres: set `deleted_at` timestamp. At hydration time, filter out deleted tweet IDs. For immediate removal from all timelines: publish a deletion event to Kafka, have workers call `ZREM timeline:{follower_id} {tweet_id}` for all followers — same fan-out cost as posting. Twitter chose the soft-delete approach: deleted tweets are hidden at hydration time rather than eagerly removed from 100M Redis sorted sets. The Redis entries naturally expire when the sorted set is trimmed or the key TTLs.
