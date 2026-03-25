#!/usr/bin/env python3
"""
Twitter Timeline Lab — experiment.py

What this demonstrates:
  1. Setup: users + follow relationships in Postgres
  2. Post tweet → produce to Kafka topic
  3. Fan-out worker: consume from Kafka → push to each follower's Redis sorted set
  4. Fetch timeline from Redis sorted set (ZREVRANGE)
  5. Celebrity problem: fan-out time grows linearly with follower count
  6. Hybrid approach: skip fan-out for celebrities, merge at read time

Run:
  docker compose up -d
  # Wait ~45s for Kafka to be ready
  python experiment.py
"""

import json
import os
import random
import time

import psycopg2
import psycopg2.extras
import redis

# ── Config ───────────────────────────────────────────────────────────────────

DB_URL    = os.getenv("DATABASE_URL", "postgresql://app:secret@localhost:5432/twitter")
REDIS_URL = os.getenv("REDIS_URL",    "redis://localhost:6379")
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")

CELEBRITY_THRESHOLD = 500  # Lab only: production Twitter uses ~1M followers as threshold


# ── Helpers ───────────────────────────────────────────────────────────────────

def section(title):
    print(f"\n{'=' * 62}")
    print(f"  {title}")
    print("=" * 62)


def get_db():
    return psycopg2.connect(DB_URL)


def get_redis():
    return redis.from_url(REDIS_URL, decode_responses=True)


def wait_for_kafka(bootstrap, max_wait=90):
    print(f"  Waiting for Kafka at {bootstrap} ...")
    for i in range(max_wait):
        try:
            from kafka import KafkaAdminClient
            client = KafkaAdminClient(bootstrap_servers=bootstrap, request_timeout_ms=3000)
            client.list_topics()
            client.close()
            print(f"  Kafka ready after {i+1}s")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("Kafka did not start in time")


def wait_for_postgres(max_wait=30):
    print("  Waiting for Postgres ...")
    for i in range(max_wait):
        try:
            conn = get_db()
            conn.close()
            print(f"  Postgres ready after {i+1}s")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("Postgres did not start in time")


def install_packages():
    import subprocess, sys
    pkgs = ["kafka-python", "psycopg2-binary", "redis"]
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet"] + pkgs
    )


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db(conn):
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id       BIGSERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL
                );
                CREATE TABLE IF NOT EXISTS follows (
                    follower_id  BIGINT NOT NULL,
                    followee_id  BIGINT NOT NULL,
                    PRIMARY KEY (follower_id, followee_id)
                );
                CREATE TABLE IF NOT EXISTS tweets (
                    id         BIGSERIAL PRIMARY KEY,
                    user_id    BIGINT NOT NULL,
                    content    TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_tweets_user ON tweets(user_id);
                CREATE INDEX IF NOT EXISTS idx_follows_followee ON follows(followee_id);
            """)


# ── Phase 1: Seed users and follows ──────────────────────────────────────────

def phase1_setup(conn):
    section("Phase 1: Seed Users + Follow Graph")

    with conn:
        with conn.cursor() as cur:
            # Create 50 regular users
            regular_users = [f"user_{i:03d}" for i in range(1, 51)]
            cur.executemany(
                "INSERT INTO users (username) VALUES (%s) ON CONFLICT DO NOTHING",
                [(u,) for u in regular_users],
            )
            # Create 3 celebrities
            celebrities = ["celeb_popstar", "celeb_politician", "celeb_athlete"]
            cur.executemany(
                "INSERT INTO users (username) VALUES (%s) ON CONFLICT DO NOTHING",
                [(c,) for c in celebrities],
            )
            conn.commit()

            # Get all user IDs
            cur.execute("SELECT id, username FROM users ORDER BY id")
            all_users = {row[1]: row[0] for row in cur.fetchall()}
            regular_ids = [all_users[u] for u in regular_users]
            celeb_ids   = [all_users[c] for c in celebrities]

            # Regular users: each follows 10–20 random others
            follows = set()
            random.seed(42)
            for uid in regular_ids:
                n_follows = random.randint(10, 20)
                targets = random.sample([x for x in regular_ids if x != uid], min(n_follows, len(regular_ids)-1))
                for t in targets:
                    follows.add((uid, t))
                # Also follow one celebrity
                follows.add((uid, random.choice(celeb_ids)))

            # Celebrity: 1000 followers (all regular users)
            big_celeb = celeb_ids[0]
            for uid in regular_ids:
                follows.add((uid, big_celeb))

            cur.executemany(
                "INSERT INTO follows (follower_id, followee_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                list(follows),
            )

    # Count followers per user
    with conn.cursor() as cur:
        cur.execute("""
            SELECT u.username, COUNT(f.follower_id) as followers
            FROM users u
            LEFT JOIN follows f ON f.followee_id = u.id
            GROUP BY u.username
            ORDER BY followers DESC
            LIMIT 10
        """)
        rows = cur.fetchall()

    print(f"\n  Created 53 users, {len(follows)} follow relationships")
    print(f"\n  Top 10 users by follower count:")
    print(f"  {'Username':<25}  {'Followers':>10}")
    print(f"  {'-'*25}  {'-'*10}")
    for username, followers in rows:
        tag = " ← celebrity" if username.startswith("celeb_") else ""
        print(f"  {username:<25}  {followers:>10}{tag}")

    return all_users, regular_ids, celeb_ids


# ── Phase 2: Post tweets → Kafka ─────────────────────────────────────────────

def phase2_post_tweets(conn, all_users, producer):
    section("Phase 2: Post Tweets → Kafka")

    from kafka import KafkaProducer

    tweet_data = []
    with conn:
        with conn.cursor() as cur:
            # 5 regular users each post 3 tweets
            sample_users = list(all_users.items())[:5]
            for username, user_id in sample_users:
                for i in range(3):
                    content = f"Tweet #{i+1} from {username}: {''.join(random.choices('abcdefghijklmnopqrstuvwxyz ', k=30)).strip()}"
                    cur.execute(
                        "INSERT INTO tweets (user_id, content) VALUES (%s, %s) RETURNING id, created_at",
                        (user_id, content),
                    )
                    row = cur.fetchone()
                    tweet = {
                        "tweet_id":   row[0],
                        "user_id":    user_id,
                        "username":   username,
                        "content":    content,
                        "created_at": row[1].timestamp(),
                    }
                    tweet_data.append(tweet)
                    producer.send("tweets", value=json.dumps(tweet).encode())

    producer.flush()
    print(f"\n  Posted {len(tweet_data)} tweets to Kafka topic 'tweets'")
    print(f"\n  Sample (first 3):")
    for t in tweet_data[:3]:
        print(f"    [{t['tweet_id']}] @{t['username']}: {t['content'][:50]}...")
    return tweet_data


# ── Phase 3: Fan-out worker ───────────────────────────────────────────────────

def phase3_fanout(conn, consumer, r):
    section("Phase 3: Fan-out Worker (Kafka → Redis Sorted Sets)")

    print("""
  Fan-out on write:
    When a tweet arrives, push it into each follower's timeline.
    Timeline stored in Redis as sorted set: key = timeline:{user_id}
    Score = tweet timestamp (enables chronological order via ZREVRANGE)
""")

    fanout_times = []
    total_pushes = 0

    with conn.cursor() as cur:
        # Consume up to 15 messages (5 users × 3 tweets)
        for i, msg in enumerate(consumer):
            tweet = json.loads(msg.value.decode())
            user_id = tweet["user_id"]
            tweet_id = tweet["tweet_id"]
            score    = tweet["created_at"]

            # Fetch all followers of this tweeter
            cur.execute(
                "SELECT follower_id FROM follows WHERE followee_id = %s",
                (user_id,),
            )
            followers = [row[0] for row in cur.fetchall()]

            # Fan-out: push tweet_id into each follower's sorted set
            start = time.perf_counter()
            pipe = r.pipeline()
            for follower_id in followers:
                pipe.zadd(f"timeline:{follower_id}", {str(tweet_id): score})
                pipe.zremrangebyrank(f"timeline:{follower_id}", 0, -801)  # keep 800 max
            pipe.execute()
            elapsed_ms = (time.perf_counter() - start) * 1000

            fanout_times.append((len(followers), elapsed_ms))
            total_pushes += len(followers)

            if i < 5:
                print(f"  Tweet {tweet_id} from user {user_id}: "
                      f"fanned out to {len(followers)} followers in {elapsed_ms:.1f}ms")

            if i >= 14:
                break

    print(f"\n  Total: {len(fanout_times)} tweets processed, {total_pushes} sorted-set entries written")
    print(f"\n  Fan-out time vs follower count:")
    print(f"  {'Followers':>10}  {'Fan-out ms':>12}")
    print(f"  {'-'*10}  {'-'*12}")
    for followers, ms in sorted(fanout_times)[:8]:
        print(f"  {followers:>10}  {ms:>12.1f}")

    return fanout_times


# ── Phase 4: Read timeline from Redis ────────────────────────────────────────

def phase4_read_timeline(conn, r, all_users):
    section("Phase 4: Read Timeline from Redis (ZREVRANGE)")

    # Pick a user who follows several others
    sample_user_id = list(all_users.values())[5]
    sample_username = list(all_users.keys())[5]

    start = time.perf_counter()
    tweet_ids = r.zrevrange(f"timeline:{sample_user_id}", 0, 19, withscores=False)
    elapsed_ms = (time.perf_counter() - start) * 1000

    print(f"\n  Reading timeline for @{sample_username} (user_id={sample_user_id})")
    print(f"  ZREVRANGE returned {len(tweet_ids)} tweet IDs in {elapsed_ms:.2f}ms")

    if tweet_ids:
        # Hydrate tweet content from Postgres
        ids_tuple = tuple(int(t) for t in tweet_ids)
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(ids_tuple))
            cur.execute(
                f"SELECT t.id, u.username, t.content, t.created_at "
                f"FROM tweets t JOIN users u ON t.user_id = u.id "
                f"WHERE t.id IN ({placeholders}) ORDER BY t.created_at DESC",
                ids_tuple,
            )
            rows = cur.fetchall()

        print(f"\n  Timeline (most recent first):")
        for tweet_id, username, content, created_at in rows[:5]:
            print(f"    [{tweet_id}] @{username}: {content[:55]}...")
    else:
        print("  (No tweets in timeline yet — try after phase 3)")

    print(f"""
  Redis sorted set key: timeline:{{user_id}}
    Score = tweet timestamp → ZREVRANGE gives chronological order
    Trim to 800 entries on write (users rarely scroll back 800 tweets)
    Read latency: ~{elapsed_ms:.2f}ms (pure Redis, no DB)
""")


# ── Phase 5: Celebrity fan-out problem ────────────────────────────────────────

def phase5_celebrity_problem(conn, r):
    section("Phase 5: Celebrity Problem — Fan-out Time at Scale")

    print("""
  Simulate posting a tweet from a celebrity with many followers.
  Measure how fan-out time scales with follower count.
""")

    # Simulate celebrity with varying follower counts
    results = []
    tweet_score = time.time()

    for n_followers in [10, 50, 100, 250, 500, 1000]:
        fake_followers = [f"fake_follower_{i}" for i in range(n_followers)]

        start = time.perf_counter()
        pipe = r.pipeline()
        for fid in fake_followers:
            pipe.zadd(f"timeline:{fid}", {"celeb_tweet_99999": tweet_score})
        pipe.execute()
        elapsed_ms = (time.perf_counter() - start) * 1000

        results.append((n_followers, elapsed_ms))

    print(f"  {'Followers':>10}  {'Fan-out ms':>12}  {'Bar'}")
    print(f"  {'-'*10}  {'-'*12}  {'-'*30}")
    for n, ms in results:
        bar = "#" * int(ms / 2)
        print(f"  {n:>10}  {ms:>12.1f}  {bar}")

    print(f"""
  Problem: Katy Perry had 108M Twitter followers in 2022.
  At ~1ms per 1000 followers, fanning out 1 tweet takes ~108 seconds.
  Worse: she might tweet during peak hours when the fan-out queue backs up.

  Real numbers from Twitter engineering (2012 QCon talk):
  - Average fan-out: 75 followers → negligible
  - Barack Obama (30M followers at the time): fan-out took ~5 minutes
  - Solution: hybrid approach (Phase 6)
""")
    return results


# ── Phase 6: Hybrid approach ─────────────────────────────────────────────────

def phase6_hybrid(conn, r, all_users, celeb_ids):
    section("Phase 6: Hybrid Fan-out — Skip Celebrities, Merge at Read Time")

    print(f"""
  Hybrid strategy (Twitter's actual approach):
    - Regular users (< {CELEBRITY_THRESHOLD} followers): fan-out on write
    - Celebrities (≥ {CELEBRITY_THRESHOLD} followers): NO fan-out, pull at read time

  At timeline read time, merge two sources:
    1. Pre-built timeline from Redis (regular users' tweets)
    2. Live query: SELECT recent tweets FROM celebrities I follow
""")

    # Simulate reading a hybrid timeline
    reader_id = list(all_users.values())[10]
    reader_username = list(all_users.keys())[10]

    # Step 1: Get pre-built timeline from Redis
    start = time.perf_counter()
    fanout_tweet_ids = r.zrevrange(f"timeline:{reader_id}", 0, 19, withscores=True)
    redis_ms = (time.perf_counter() - start) * 1000

    # Step 2: Get celebrity IDs this user follows
    with conn.cursor() as cur:
        if celeb_ids:
            placeholders = ",".join(["%s"] * len(celeb_ids))
            cur.execute(
                f"SELECT id FROM users WHERE id IN ({placeholders})",
                celeb_ids,
            )
            followed_celebs = [row[0] for row in cur.fetchall()]
        else:
            followed_celebs = []

        # Fetch recent tweets from followed celebrities
        start = time.perf_counter()
        if followed_celebs:
            placeholders = ",".join(["%s"] * len(followed_celebs))
            cur.execute(
                f"SELECT t.id, t.user_id, t.content, t.created_at "
                f"FROM tweets t "
                f"WHERE t.user_id IN ({placeholders}) "
                f"ORDER BY t.created_at DESC LIMIT 20",
                followed_celebs,
            )
            celeb_tweets = cur.fetchall()
        else:
            celeb_tweets = []
        db_ms = (time.perf_counter() - start) * 1000

    # Step 3: Merge and sort
    start = time.perf_counter()
    combined = list(fanout_tweet_ids) + [(str(t[0]), t[3].timestamp()) for t in celeb_tweets]
    combined.sort(key=lambda x: x[1], reverse=True)
    final_timeline = combined[:20]
    merge_ms = (time.perf_counter() - start) * 1000

    total_ms = redis_ms + db_ms + merge_ms

    print(f"  Timeline for @{reader_username}:")
    print(f"    Redis (fan-out timeline):  {len(fanout_tweet_ids)} tweets  ({redis_ms:.2f}ms)")
    print(f"    Postgres (celeb tweets):   {len(celeb_tweets)} tweets  ({db_ms:.2f}ms)")
    print(f"    Merge + sort:              {len(final_timeline)} tweets  ({merge_ms:.2f}ms)")
    print(f"    Total read latency:        {total_ms:.2f}ms")

    print(f"""
  Trade-off comparison:

  ┌─────────────────────┬──────────────────┬──────────────────┐
  │ Strategy            │ Write cost       │ Read cost        │
  ├─────────────────────┼──────────────────┼──────────────────┤
  │ Fan-out on write    │ O(followers)     │ O(1) Redis read  │
  │ Pull on read        │ O(1)             │ O(following) DB  │
  │ Hybrid (Twitter)    │ O(regular fans)  │ O(1) + O(celebs) │
  └─────────────────────┴──────────────────┴──────────────────┘

  Celebrity threshold determines the trade-off:
  - Low threshold (100 followers): many users skip fan-out → reads slower
  - High threshold (1M followers): only true mega-celebrities skip fan-out
  - Twitter used ~1M follower threshold in production
""")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    section("TWITTER TIMELINE LAB")
    print("""
  Architecture:
    Tweet POST → Kafka → Fan-out Worker → Redis Sorted Sets (timelines)
    Timeline GET → Redis ZREVRANGE (+ Postgres for celebrities)

  Key insight: timelines are pre-computed on write, not computed on read.
  This trades write amplification for O(1) read latency.
""")

    install_packages()

    wait_for_postgres()
    wait_for_kafka(KAFKA_BOOTSTRAP)

    from kafka import KafkaProducer, KafkaConsumer

    conn = get_db()
    r    = get_redis()

    init_db(conn)

    producer = KafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP,
        acks="all",
    )

    all_users, regular_ids, celeb_ids = phase1_setup(conn)
    tweet_data = phase2_post_tweets(conn, all_users, producer)

    consumer = KafkaConsumer(
        "tweets",
        bootstrap_servers=KAFKA_BOOTSTRAP,
        auto_offset_reset="earliest",
        consumer_timeout_ms=5000,
        group_id="fanout-worker",
    )

    phase3_fanout(conn, consumer, r)
    consumer.close()

    phase4_read_timeline(conn, r, all_users)
    phase5_celebrity_problem(conn, r)
    phase6_hybrid(conn, r, all_users, celeb_ids)

    conn.close()

    section("Lab Complete")
    print("""
  Summary:
  • Fan-out on write: O(followers) writes per tweet, O(1) timeline reads
  • Redis sorted sets: score=timestamp, ZREVRANGE = chronological feed
  • Celebrity problem: 100M followers × 1 tweet = massive write amplification
  • Hybrid approach: fan-out only for regular users, pull celebs at read time
  • Twitter's actual threshold: ~1M followers triggers celebrity treatment

  Next: 03-youtube/ — video upload, transcoding pipeline, adaptive bitrate
""")


if __name__ == "__main__":
    main()
