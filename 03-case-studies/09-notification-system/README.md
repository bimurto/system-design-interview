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

## Requirements

### Functional
- Deliver notifications via push (APNs/FCM), email, SMS, and in-app
- Respect per-user, per-channel opt-out preferences
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

### 2. User Preferences: Per-Channel, Per-Type Opt-Out

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

### 3. Deduplication: Idempotency Key

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

### 4. Priority: Transactional Before Marketing

**Why separate topics?** If transactional and marketing share a topic, a large marketing campaign (100M emails) can add 30 minutes of lag to transactional messages (password reset). Separate topics let workers drain transactional completely before touching marketing.

**Priority tiers:**
| Tier | Examples | SLA | Kafka Topic |
|---|---|---|---|
| Transactional | OTP, password reset, payment alert | < 1s | `notif-transactional` |
| Social | New follower, comment, like | < 30s | `notif-social` |
| Marketing | Promotions, newsletters | < 10min | `notif-marketing` |

**Worker allocation:** Transactional workers are always running. Social workers scale with traffic. Marketing workers can be stopped during peak hours to reserve capacity.

### 5. Retry and Dead Letter Queue

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
   A: Retry with exponential backoff up to N attempts. After exhausting retries, produce to the DLQ. When APNs recovers, re-process DLQ messages. The user receives their notification late rather than not at all. For transactional notifications (OTP), a 30-minute delay may invalidate the token — the system should detect expired OTPs and drop them rather than delivering stale codes.

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
    A: (1) User submits email → Auth service generates OTP, stores in Redis with 10-minute TTL. (2) Auth service produces notification event with `{type: transactional, channel: email, user_id, message: "OTP: 123456", idempotency_key: hash(event_id)}`. (3) Notification service produces to `notif-transactional` Kafka topic. (4) Email worker picks up within seconds (transactional has dedicated consumer). (5) Worker checks preferences (email enabled?), checks dedup, calls SendGrid API. (6) User receives email in < 5 seconds. On failure, retry up to 5 times with backoff. If all fail → DLQ + alert on-call.
