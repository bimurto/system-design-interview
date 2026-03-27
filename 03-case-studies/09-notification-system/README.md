# Case Study: Notification System

**Prerequisites:** `../../02-advanced/05-message-queues-kafka/`, `../../02-advanced/04-event-driven-architecture/`, `../../02-advanced/14-idempotency-exactly-once/`

---

## The Problem at Scale

A notification system for a platform like Facebook or WhatsApp must deliver over 1 billion notifications per day across push, email, SMS, and in-app channels — reliably, quickly, and exactly once.

| Metric | Value |
|---|---|
| Notifications/day | 1 billion |
| Average RPS | ~11,600 |
| Peak RPS | ~35,000 (3× average) |
| Push notifications | ~350M/day |
| Email notifications | ~150M/day |
| SMS notifications | ~50M/day |
| In-app notifications | ~450M/day |
| Delivery latency (transactional) | < 1 second |
| Delivery latency (marketing) | < 10 minutes |

The core challenge is the fan-out problem: a single user action (e.g., a new follower) may trigger notifications to thousands of users. The system must absorb traffic spikes without losing messages or delivering duplicates.

---

## Clarify Requirements (first 3–5 minutes of the interview)

Before designing anything, drive a short requirements conversation. These are the questions that surface critical design forks:

| Question | Why it matters |
|---|---|
| Which channels must we support? (push, email, SMS, in-app) | Each channel has its own rate limits, failure modes, and provider SDKs |
| What notification types exist? (transactional vs marketing) | Determines whether you need priority tiers and separate SLAs |
| What is the delivery guarantee? (at-least-once, exactly-once) | Exactly-once requires idempotency infrastructure; at-least-once is much simpler |
| What is the acceptable delivery latency? (OTP vs newsletter) | Drives whether you need dedicated high-priority consumer groups |
| Do users have notification preferences per channel and per type? | Requires a preferences store; affects fan-out strategy |
| What is the expected scale? (DAU, peak notifications per event) | Determines whether fan-out is sync or async, Kafka partition count |
| Is scheduled delivery required? | Adds a timer/scheduler service to the design |
| Is there a quiet hours / timezone requirement? | Adds significant complexity: delay queues keyed by user timezone |

## Requirements

### Functional
- Deliver notifications via push (APNs/FCM), email, SMS, and in-app
- Respect per-user, per-channel opt-out preferences (e.g., email off, push on)
- Respect per-user, per-notification-type opt-out (e.g., marketing off, transactional always on)
- Support scheduled and immediate delivery
- Guarantee exactly-once delivery (no duplicate notifications)
- Support priority tiers: transactional (OTP, alerts) vs marketing

### Non-Functional
- Transactional notifications delivered in < 1 second P95
- At least 99.9% delivery rate (not lost)
- Horizontal scalability for channel workers
- Retry failed deliveries with backoff (not dropped)
- Dead-letter queue for messages that exhaust retries

### Capacity Estimation

| Metric | Calculation | Result |
|---|---|---|
| Push RPS | 350M / 86,400s | ~4,050 RPS |
| Email RPS | 150M / 86,400s | ~1,735 RPS |
| SMS RPS | 50M / 86,400s | ~580 RPS |
| In-app RPS | 450M / 86,400s | ~5,200 RPS |
| Kafka partition count | ~35K peak RPS / 1K per partition | ~35 partitions |
| User preferences in Redis | 500M users × 50B | ~25 GB |
| Idempotency key store (24h) | 1B keys × 100B | ~100 GB |

---

## High-Level Architecture

```
  User Action (like, follow, purchase, OTP request)
       │
       ▼
  ┌────────────────────────────────────────────────────────┐
  │              Notification Service                       │
  │  1. Determine recipients                               │
  │  2. Look up user preferences (Redis)                   │
  │  3. Assign idempotency key                             │
  │  4. Route to correct Kafka topic by priority           │
  └───────────────────────────┬────────────────────────────┘
                              │
         ┌────────────────────┼────────────────────┐
         ▼                    ▼                    ▼
  Kafka: transactional  Kafka: social      Kafka: marketing
  (OTP, alerts)         (likes, follows)   (promotions)
         │                    │                    │
         └──────────┬─────────┘                    │
                    ▼                              ▼
  ┌─────────────────────────────────────────────────────┐
  │            Channel Workers (per-channel pools)       │
  │                                                     │
  │  Push Workers    → APNs (iOS) / FCM (Android)       │
  │  Email Workers   → SendGrid / Amazon SES             │
  │  SMS Workers     → Twilio / AWS SNS                  │
  │  In-App Workers  → Write to user's notification feed │
  └─────────────────────────────────────────────────────┘
       │ on failure
       ▼
  Dead Letter Queue (Kafka DLQ topic)
```

**Kafka topics by priority:** Transactional messages (password reset, payment alert) go on a separate high-priority topic. Workers drain transactional first. This prevents marketing campaigns from starving time-sensitive alerts.

**Channel workers:** Separate worker pools per channel, each with its own Kafka consumer group. Push workers scale independently from email workers (push is faster; email has rate limits per domain). Workers are stateless — state lives in Redis (preferences) and Postgres (delivered log).

**Idempotency:** Every notification event carries an idempotency_key. Before delivery, the worker checks if this key has already been delivered to this channel. A unique constraint on `(idempotency_key, channel)` in Postgres guarantees exactly-once delivery even with retries or worker restarts.

---

## Deep Dives

### 1. Notification Pipeline: Event to Delivery

```
User action
  → Notification Service: fan-out to recipient list
    → For each recipient:
      1. Fetch user preferences from Redis (O(1))
      2. If channel is enabled:
         a. Assign idempotency_key = hash(event_id + user_id + channel)
         b. Produce to Kafka (transactional/marketing topic)
  → Channel Workers consume from Kafka:
      1. Dedup check (idempotency_key in Redis/Postgres)
      2. Call third-party API (APNs, SendGrid, Twilio)
      3. On success: mark delivered
      4. On failure: retry with backoff → DLQ after N failures
```

**Fan-out at scale:** A celebrity with 10M followers who posts triggers 10M notification events. The Notification Service uses a worker pool to fan out in parallel. Each event is small (~200 bytes). At 10M events × 200B = 2GB — manageable for Kafka.

**Batching:** Push workers can send up to 500 notifications per APNs request, and up to 1000 per FCM request. Workers accumulate messages from Kafka and batch them before calling the API. This reduces API calls from 4,050 RPS to ~10 RPS for push.

### 2. Fan-Out Strategy: Write vs. Read

**The problem:** A celebrity with 10M followers posts new content. How do you trigger 10M notifications without hammering your storage layer?

**Write fan-out (push model):** At post time, the Notification Service enumerates all followers and produces one Kafka event per follower. Workers then deliver to each recipient independently.
- Pros: delivery is O(1) per user — just consume from Kafka
- Cons: 10M Kafka produces happen synchronously on post → huge write spike; follower enumeration can take seconds for mega-celebrities

**Read fan-out (pull model):** Store one post event. Each user's notification feed is assembled at read time by querying who the user follows.
- Pros: posting is O(1) regardless of follower count
- Cons: reading is O(followed_accounts) per page load; hard to enforce delivery guarantees or retry

**Hybrid (industry standard — Facebook, Twitter/X, Instagram):**
- Regular users (< 1M followers): write fan-out — enumerate followers at post time, produce one Kafka event per follower
- Celebrity accounts (> 1M followers): read fan-out — post event stored once; follower notification feeds are materialized lazily when users open the app
- The cutoff threshold is configurable; accounts above it are flagged in the social graph service

**Early suppression at fan-out time:** Before producing a Kafka event for a follower, check the user's push preference in Redis. If push is disabled, skip the produce entirely — this reduces Kafka volume and downstream work. The preference check at delivery time is still needed as a safety net (preferences change between fan-out and delivery).

**Fan-out at scale:** A 10M-follower celebrity post:
- 10M events × 200 bytes = 2 GB on Kafka — manageable for a single topic with 35 partitions
- At 1M events/minute fan-out rate → 60 seconds to produce all events
- APNs batch size = 500 → 20,000 API calls to deliver to 10M devices
- Per-device token validation: APNs returns HTTP 410 for expired tokens — remove from device registry immediately to avoid wasting future sends

### 3. User Preferences: Per-Channel, Per-Type Opt-Out

Every notification lookup needs to check:
- Is this channel enabled for this user? (email on/off)
- Has the user opted out of this notification type? (marketing/social on/off)
- What's the user's quiet hours? (no push after 10pm)

**Storage:** Redis hash per user: `HSET user:prefs:104 email 1 push 1 sms 0 mkt 0`

**Lookup:** `HGETALL user:prefs:{user_id}` — O(1), ~0.1ms in Redis. At 35K RPS, this is 35K Redis reads/second — well within a single Redis instance's capacity (~100K ops/second).

**Cache invalidation:** When a user updates their preferences:
1. Write to Postgres (source of truth)
2. Update Redis key immediately (write-through)
3. Set TTL = 1 hour on the Redis key (belt-and-suspenders expiry)

### 4. Deduplication: Idempotency Key

**Problem:** Network failures cause retries. A push notification sent successfully to APNs, but the ACK was lost, causes the worker to retry → user gets the same notification twice.

**Solution:** Idempotency key = `sha256(event_id + user_id + channel)[:16]`

Before delivery:
```sql
INSERT INTO delivered_notifications (idempotency_key, channel, ...)
VALUES ($1, $2, ...)
ON CONFLICT (idempotency_key, channel) DO NOTHING
RETURNING id
```
If `RETURNING id` is empty → already delivered → skip.

**Dedup window:** Idempotency keys can be cleaned up after 24 hours (notifications older than 24h don't need dedup). A cron job deletes old rows nightly.

**Redis dedup (lower latency):** store `SET dedup:{key}:{channel} 1 EX 86400` — check with `EXISTS`. Use Redis for hot path (sub-ms), Postgres as fallback for cold keys.

### 5. Priority: Transactional Before Marketing

**Why separate topics?** If transactional and marketing share a topic, a large marketing campaign (100M emails) can add 30 minutes of lag to transactional messages (password reset). Separate topics let workers drain transactional completely before touching marketing.

**Priority tiers:**
| Tier | Examples | SLA | Kafka Topic |
|---|---|---|---|
| Transactional | OTP, password reset, payment alert | < 1s | `notif-transactional` |
| Social | New follower, comment, like | < 30s | `notif-social` |
| Marketing | Promotions, newsletters | < 10min | `notif-marketing` |

**Worker allocation:** Transactional workers are always running. Social workers scale with traffic. Marketing workers can be stopped during peak hours to reserve capacity.

### 6. Retry and Dead Letter Queue

**Exponential backoff:**
```
attempt 1: immediate
attempt 2: wait 2^1 × base_delay = 2s
attempt 3: wait 2^2 × base_delay = 4s
attempt 4: wait 2^3 × base_delay = 8s
```

**Max attempts:** 5 for transactional, 3 for marketing. After exhausting retries → produce to `notif-dlq` Kafka topic.

**DLQ monitoring:** On-call engineers monitor the DLQ consumer lag. A spike indicates a systemic delivery failure (APNs outage, invalid credentials). The DLQ preserves the original message for re-processing after the incident.

**Kafka consumer groups for retry:** Use separate consumer groups for each retry tier. Kafka's `consumer_timeout_ms` + sleep-before-retry achieves backoff without blocking the partition for other messages.

---

## Failure Modes and Scale Challenges

These are the failure modes that separate a senior design from a mid-level one. Raise at least two proactively.

### Device Token Churn (APNs/FCM)
Every mobile device has a push token that changes when the user reinstalls the app, resets the device, or revokes notification permission. APNs returns **HTTP 410 Gone** for expired tokens. FCM returns `registration_not_registered`. Workers must:
1. Detect these error codes in the delivery response
2. Delete the stale token from the device registry immediately
3. **Not** retry — retrying a 410 wastes quota and can get the sender flagged

At scale, 1-3% of push tokens are stale daily. For 1B push notifications/day at 35% push share (350M/day), that's ~3-10M 410 responses per day that must be cleaned up.

### Thundering Herd from Scheduled Campaigns
A marketing team schedules a 100M-email campaign to fire at 9:00 AM EST. At 9:00:00, 100M events are produced to Kafka simultaneously. Email workers, which are rate-limited per IP by SendGrid (~100 emails/second per dedicated IP), become the bottleneck. The campaign takes hours to drain — acceptable for marketing — but the spike in Kafka producer load can starve transactional topics if they share a broker.

Mitigation: **time-spread scheduled campaigns** — the campaign scheduler produces to Kafka at a controlled rate (e.g., 1M events/minute over 100 minutes) rather than all at once. This smooths broker load.

### Quiet Hours and Timezone Handling
Users in Tokyo should not receive marketing push notifications at 3 AM Tokyo time, even if the campaign fires at noon EST. Quiet hours require:
1. Knowing each user's timezone (stored in user profile, NOT in notification preferences — different concern)
2. At fan-out time, compute `delivery_not_before = next_morning_9am_in_user_timezone`
3. If `delivery_not_before > now`, produce to a **delay queue** instead of the immediate Kafka topic
4. A scheduler service reads the delay queue and re-publishes at the correct time

This is a significant additional component. Transactional notifications (OTP, alerts) always bypass quiet hours.

### Per-User Notification Rate Limiting
A bug in the Notification Service could cause it to emit 1,000 "new follower" notifications for the same user in one second. Without a rate limit, the user receives 1,000 push notifications and immediately disables all notifications from the app.

Solution: before producing to Kafka, check a Redis counter:
```
INCR notif:ratelimit:{user_id}:{channel}:{window}
EXPIRE notif:ratelimit:{user_id}:{channel}:{window} {window_seconds}
```
If the counter exceeds the limit (e.g., 10 push per minute per user), suppress the notification. This is separate from third-party API rate limits.

### Idempotency Key Collision
`sha256(event_id + user_id + channel)[:16]` has a 1-in-2^64 collision probability per pair. At 1B notifications/day × 365 days = 365B key-days, the birthday paradox gives a ~1-in-50 chance of a collision per year. Use the full 32-character hex (128 bits) to make collisions cryptographically negligible.

---

## How It Actually Works

**Facebook's real-time notification system** (OSDI 2011, "TAO: Facebook's Distributed Data Store for the Social Graph"): Facebook uses a custom fan-out service that reads the social graph to find recipients, then routes to channel-specific queues. Their system handles ~1B notifications/day across push, email, and in-app.

**Airbnb's notification platform:** Airbnb published a blog post (2023) describing their migration from a monolithic notification service to a Kafka-based pipeline. Key insight: separating the "decide what to send" step from the "actually send" step allowed them to scale channel workers independently. They also implemented a suppression layer (don't send marketing during active booking flow) in the router, not in individual channel workers.

**APNs and FCM batch sizes:** Apple Push Notification service accepts up to 500 notifications per HTTP/2 connection, each as a separate request. FCM (Firebase Cloud Messaging) supports batch sends of up to 500 messages in a single HTTP request. Batching is the primary lever for throughput in push notification systems.

Source: Facebook OSDI 2011 "TAO"; Airbnb Engineering Blog "Rearchitecting Airbnb's Notification Platform" (2023); Apple Developer Documentation "Sending Notification Requests to APNs".

---

## Hands-on Lab

**Time:** ~20 minutes
**Services:** `zookeeper`, `kafka`, `db` (Postgres 15), `workers` (Python)

### Setup

```bash
cd system-design-interview/03-case-studies/09-notification-system/
docker compose up -d zookeeper kafka db
# Wait ~40s for Kafka to be healthy
docker compose ps
```

### Experiment

```bash
docker compose run --rm workers
# Or locally:
pip install kafka-python psycopg2-binary
KAFKA_BOOTSTRAP=localhost:9092 python experiment.py
```

The script runs 7 phases:

1. **Produce:** 100 notifications to transactional and marketing Kafka topics
2. **Consume & deliver:** workers drain transactional first (priority), then marketing
3. **Preference filtering:** verify users 101-103 have email suppressed
4. **Deduplication:** same idempotency_key sent twice → delivered exactly once
5. **Retry backoff:** notifications ending in "7" simulate 2 failures then succeed
6. **Scale math:** compute real numbers for 1B/day
7. **DB summary:** delivered count per channel in Postgres

### Break It

**Observe DLQ behavior:** in `mock_deliver`, change the `attempt < 3` condition to `attempt < 10`. Re-run — those notifications will exhaust all retries and go to the DLQ:

```bash
# After modifying mock_deliver threshold, run and watch DLQ stat increment
docker compose run --rm workers
```

**Test preference filtering at scale:** Add 50 notifications for user 103 (push+email disabled) and verify none of the email/push ones are delivered:

```python
# In the produce phase, loop 50 notifications targeting user_id=103 on channel="push"
```

### Observe

```bash
# Check Kafka consumer group lag
docker compose exec kafka kafka-consumer-groups \
  --bootstrap-server localhost:9092 \
  --group lab-group-txn \
  --describe

# Check delivered notifications in Postgres
docker compose exec db psql -U app -d notifications \
  -c "SELECT channel, COUNT(*) FROM delivered_notifications GROUP BY channel;"
```

### Teardown

```bash
docker compose down -v
```

---

## Interview Checklist

1. **Q: Why use Kafka instead of a simple database queue for notifications?**
   A: Kafka provides persistent, replayable, ordered queues with consumer group offsets. If a channel worker crashes mid-batch, Kafka replays the uncommitted messages on restart. A database queue requires polling and row locking — at 35K RPS this creates heavy contention. Kafka also enables multiple independent consumer groups (push workers, email workers) to read from the same topic without coordination.

2. **Q: How do you guarantee a notification is delivered exactly once?**
   A: Idempotency key per (notification_event, user, channel). Before delivery, check `delivered_notifications` table (unique constraint on key+channel). Use `INSERT ... ON CONFLICT DO NOTHING` — if the row already exists, skip delivery. This handles retries from worker crashes, network timeouts, and duplicate Kafka events.

3. **Q: How do you prevent a marketing campaign from delaying password reset notifications?**
   A: Separate Kafka topics per priority tier. Channel workers drain `notif-transactional` completely before consuming from `notif-marketing`. In practice, transactional workers run continuously; marketing workers can be paused during incidents. This is consumer-side priority, not Kafka-side — Kafka itself doesn't have priority queues.

4. **Q: What happens when APNs is down for 30 minutes?**
   A: Retry with exponential backoff up to N attempts. After exhausting retries, produce to the DLQ. When APNs recovers, re-process DLQ messages. The user receives their notification late rather than not at all. For transactional notifications (OTP), a 30-minute delay may invalidate the token. Detection mechanism: the notification event carries an `expires_at` timestamp set by the Auth Service (equal to the OTP's Redis TTL). The DLQ consumer checks `if now() > expires_at: drop`. This requires the original event schema to include expiry metadata — a design decision made at schema definition time, not during an outage.

5. **Q: How do you store and look up user notification preferences efficiently?**
   A: Redis hash per user: `HSET user:prefs:{id} email 1 push 1 sms 0`. Sub-millisecond lookup at any scale. Write-through cache: on preference change, update Postgres first, then Redis. Redis key TTL = 1 hour as a safety net. At 500M users × 50B per hash = ~25GB total — fits in a Redis cluster.

6. **Q: How do you handle a user receiving the same push notification twice?**
   A: The idempotency key prevents re-delivery from our system. However, APNs/FCM may duplicate on their end (rare but documented). The client app should also implement local dedup: if a notification with the same `notification_id` is received twice within 60 seconds, display it only once.

7. **Q: How does the notification service know who to notify for a "new follower" event?**
   A: The event contains `{actor: user_A, action: follow, target: user_B}`. The notification service directly produces one message for user_B — no fan-out needed. For "new post by followed user", the service queries the follower list (from the social graph service) and fans out to each follower. At 10M followers, this fan-out is async and batched in Kafka.

8. **Q: What is a dead letter queue and when do messages go there?**
   A: A DLQ is a Kafka topic that receives messages which have exhausted all delivery retries. A message goes to the DLQ after N failed delivery attempts (N=5 for transactional). On-call engineers monitor the DLQ consumer lag as an alert signal. Messages can be re-processed by replaying the DLQ topic after fixing the underlying issue.

9. **Q: How do you handle 100M email notifications from a single marketing campaign?**
   A: Produce all 100M events to the `notif-marketing` Kafka topic (1-2 minutes to produce at 1M/s). Email workers consume and batch-send via SendGrid/SES. Email providers have rate limits per domain (e.g., 100 emails/second to same domain). Workers implement per-domain rate limiting. The campaign drains over several hours — acceptable for marketing. Transactional email is on a separate dedicated IP pool.

10. **Q: Walk me through what happens when a user requests a password reset.**
    A: (1) User submits email → Auth service generates OTP, stores in Redis with 10-minute TTL, sets `expires_at = now + 10 min`. (2) Auth service produces notification event with `{type: transactional, channel: email, user_id, message: "OTP: 123456", idempotency_key: hash(event_id), expires_at}`. (3) Notification service produces to `notif-transactional` Kafka topic. (4) Email worker picks up within seconds (transactional has dedicated consumer). (5) Worker checks preferences (email enabled?), checks dedup, checks `expires_at` (drop if expired), calls SendGrid API. (6) User receives email in < 5 seconds. On failure, retry up to 5 times with backoff. If all fail → DLQ + alert on-call.

11. **Q: How would you design for a celebrity with 50M followers posting content?**
    A: Use a hybrid fan-out strategy. Flag celebrity accounts (above a threshold, e.g., 1M followers) in the social graph service. For regular users: write fan-out — enumerate followers at post time and produce one Kafka event per follower. For celebrities: read fan-out — store one post event; notification feeds are assembled lazily when followers open the app, pulling recent posts from followed celebrities. This prevents a single post from generating 50M Kafka produces in milliseconds. The threshold is configurable and typically 1–5M followers.

12. **Q: APNs returns HTTP 410 for a push token. What do you do?**
    A: HTTP 410 means the device token is permanently invalid (uninstall, reset, revoked permission). The push worker must: (1) NOT retry — retrying a 410 wastes quota and risks getting the sender IP flagged; (2) immediately delete the stale token from the device token registry (Postgres table: `DELETE FROM device_tokens WHERE token = $1`); (3) increment a metric for token churn rate. At 1B push/day, expect 1-3% stale tokens (~3-10M 410s/day). The device token registry must be purged continuously, not just on delivery failure — some providers also send token invalidation webhooks proactively.

13. **Q: How do you prevent a bug from spamming a user with 1,000 notifications?**
    A: Per-user, per-channel rate limiting at the fan-out layer. Before producing to Kafka, check a Redis counter with a sliding window: `INCR notif:rl:{user_id}:{channel}:{minute}`. If the count exceeds the limit (e.g., 10 push/minute), suppress and drop the notification. This is distinct from third-party API rate limits (which are per-sender, not per-recipient). The limit should be configurable per notification type — transactional OTPs may be exempt.
