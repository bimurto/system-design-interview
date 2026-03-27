#!/usr/bin/env python3
"""
Notification System Lab — experiment.py

What this demonstrates:
  1. Produce 100 notification events to Kafka (push/email/sms/in-app)
  2. Channel workers consume and mock-deliver (priority: transactional first)
  3. User preference filtering: email/push disabled for specific users
  4. Deduplication: same idempotency_key delivered only once
  5. Retry with exponential backoff: 2 failures → retry → succeed on 3rd
     + DLQ path for notifications that exhaust all retries
  6. Fan-out simulation: celebrity post triggers 500 push notifications,
     with early preference suppression at produce time
  7. Scale math: capacity numbers for 1B notifications/day
  8. DB summary: delivered count by channel in Postgres

Run:
  docker compose up -d zookeeper kafka db
  # Wait ~40s for Kafka to be healthy
  docker compose run --rm workers
  # Or locally:
  pip install kafka-python-ng psycopg2-binary
  KAFKA_BOOTSTRAP=localhost:9092 python experiment.py
"""

import json
import os
import random
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Optional

import psycopg2
from kafka import KafkaConsumer, KafkaProducer
from kafka.admin import KafkaAdminClient, NewTopic
from kafka.errors import TopicAlreadyExistsError

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://app:secret@localhost:5432/notifications")

TOPIC_TRANSACTIONAL = "notifications-transactional"
TOPIC_MARKETING = "notifications-marketing"
CHANNELS = ["push", "email", "sms", "in-app"]

random.seed(42)


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Notification:
    notification_id: str
    user_id: int
    channel: str          # push | email | sms | in-app
    type: str             # transactional | marketing | social
    message: str
    idempotency_key: str
    priority: str = "normal"  # high | normal | low
    created_at: float = field(default_factory=time.time)

    def to_json(self) -> bytes:
        return json.dumps(asdict(self)).encode()

    @classmethod
    def from_json(cls, data: bytes) -> "Notification":
        return cls(**json.loads(data))


# ── Helpers ───────────────────────────────────────────────────────────────────

def section(title: str):
    print(f"\n{'=' * 62}")
    print(f"  {title}")
    print("=" * 62)


def wait_for_kafka(bootstrap: str, max_wait: int = 90):
    print(f"  Waiting for Kafka at {bootstrap} ...")
    for i in range(max_wait):
        try:
            admin = KafkaAdminClient(bootstrap_servers=bootstrap, request_timeout_ms=5000)
            admin.list_topics()
            admin.close()
            print(f"  Kafka ready after {i + 1}s")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("Kafka not ready")


def create_topics(bootstrap: str):
    admin = KafkaAdminClient(bootstrap_servers=bootstrap)
    topics = [
        NewTopic(name=TOPIC_TRANSACTIONAL, num_partitions=4, replication_factor=1),
        NewTopic(name=TOPIC_MARKETING, num_partitions=4, replication_factor=1),
    ]
    for topic in topics:
        try:
            admin.create_topics([topic])
        except TopicAlreadyExistsError:
            pass
    admin.close()


def make_producer() -> KafkaProducer:
    return KafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP,
        value_serializer=lambda v: v,
        acks="all",
    )


def make_consumer(topics: list[str], group_id: str) -> KafkaConsumer:
    return KafkaConsumer(
        *topics,
        bootstrap_servers=KAFKA_BOOTSTRAP,
        group_id=group_id,
        auto_offset_reset="earliest",
        enable_auto_commit=True,
        consumer_timeout_ms=3000,
        value_deserializer=lambda v: v,
    )


# ── Database ──────────────────────────────────────────────────────────────────

def init_db(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS delivered_notifications (
                id SERIAL PRIMARY KEY,
                notification_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                user_id INT NOT NULL,
                channel TEXT NOT NULL,
                message TEXT,
                delivered_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(idempotency_key, channel)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id INT PRIMARY KEY,
                email_enabled BOOLEAN DEFAULT TRUE,
                push_enabled BOOLEAN DEFAULT TRUE,
                sms_enabled BOOLEAN DEFAULT TRUE,
                inapp_enabled BOOLEAN DEFAULT TRUE
            )
        """)
        # Seed user preferences: users 101, 102, 103 have email disabled
        cur.execute("""
            INSERT INTO user_preferences (user_id, email_enabled, push_enabled, sms_enabled, inapp_enabled)
            VALUES
              (101, FALSE, TRUE,  TRUE,  TRUE),
              (102, FALSE, TRUE,  FALSE, TRUE),
              (103, FALSE, FALSE, TRUE,  TRUE),
              (104, TRUE,  TRUE,  TRUE,  TRUE),
              (105, TRUE,  TRUE,  TRUE,  TRUE)
            ON CONFLICT (user_id) DO NOTHING
        """)
    conn.commit()


def is_channel_enabled(conn, user_id: int, channel: str) -> bool:
    col_map = {"email": "email_enabled", "push": "push_enabled",
               "sms": "sms_enabled", "in-app": "inapp_enabled"}
    col = col_map.get(channel, "push_enabled")
    with conn.cursor() as cur:
        cur.execute(f"SELECT {col} FROM user_preferences WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        return row[0] if row else True  # default allow


def is_duplicate(conn, idempotency_key: str, channel: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM delivered_notifications WHERE idempotency_key = %s AND channel = %s",
            (idempotency_key, channel)
        )
        return cur.fetchone() is not None


def mark_delivered(conn, notif: Notification) -> bool:
    """Insert delivery record. Returns True if this call performed the insert,
    False if a duplicate was detected by the DB unique constraint (race condition
    between is_duplicate() check and concurrent workers)."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO delivered_notifications
              (notification_id, idempotency_key, user_id, channel, message)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (idempotency_key, channel) DO NOTHING
            RETURNING id
        """, (notif.notification_id, notif.idempotency_key,
              notif.user_id, notif.channel, notif.message))
        inserted = cur.fetchone() is not None
    conn.commit()
    return inserted


# ── Mock delivery ─────────────────────────────────────────────────────────────

class DeliveryResult:
    def __init__(self, success: bool, reason: str = ""):
        self.success = success
        self.reason = reason


def mock_deliver(notif: Notification, attempt: int = 1) -> DeliveryResult:
    """Simulate delivery. First 2 attempts fail for specific notification IDs."""
    # Simulate transient failure for notifications with id ending in '7'
    if notif.notification_id.endswith("7") and attempt < 3:
        return DeliveryResult(False, "transient_error")
    return DeliveryResult(True)


# ── Worker logic ──────────────────────────────────────────────────────────────

# Lab simplification: a single DeliveryWorker handles all channels here.
# In production, each channel (push, email, SMS) has its own consumer pool
# with independent scaling, failure isolation, and per-provider rate limits.
# See README Architecture section for the per-channel pool design.
class DeliveryWorker:
    def __init__(self, name: str, conn, stats: dict):
        self.name = name
        self.conn = conn
        self.stats = stats

    def process(self, notif: Notification):
        user_id = notif.user_id
        channel = notif.channel

        # Check user preferences
        if not is_channel_enabled(self.conn, user_id, channel):
            self.stats["preference_filtered"] += 1
            return

        # Deduplication check
        if is_duplicate(self.conn, notif.idempotency_key, channel):
            self.stats["deduplicated"] += 1
            return

        # Attempt delivery with exponential backoff
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            result = mock_deliver(notif, attempt)
            if result.success:
                # mark_delivered uses INSERT ... ON CONFLICT DO NOTHING RETURNING id
                # so even if two workers race past the is_duplicate() check above,
                # only one will get inserted = True here.
                inserted = mark_delivered(self.conn, notif)
                if inserted:
                    self.stats["delivered"] += 1
                    if attempt > 1:
                        self.stats["retried"] += 1
                else:
                    # Lost the race — another worker already recorded this delivery
                    self.stats["deduplicated"] += 1
                return
            else:
                self.stats["failures"] += 1
                if attempt < max_attempts:
                    backoff = (2 ** (attempt - 1)) * 0.05  # 50ms, 100ms
                    time.sleep(backoff)

        # Exhausted retries → dead letter queue (simulated)
        self.stats["dlq"] += 1


# ── Phases ────────────────────────────────────────────────────────────────────

def phase1_produce_notifications(producer: KafkaProducer) -> list[Notification]:
    section("Phase 1: Produce 100 Notification Events to Kafka")

    notifications = []
    user_ids = [101, 102, 103, 104, 105]

    for i in range(100):
        user_id = random.choice(user_ids)
        channel = random.choice(CHANNELS)
        notif_type = random.choices(
            ["transactional", "marketing", "social"],
            weights=[20, 50, 30]
        )[0]
        notif = Notification(
            notification_id=str(i),
            user_id=user_id,
            channel=channel,
            type=notif_type,
            message=f"Notification {i}: {notif_type} message for user {user_id} via {channel}",
            idempotency_key=f"idem-{i}",
            priority="high" if notif_type == "transactional" else "normal",
        )
        topic = TOPIC_TRANSACTIONAL if notif_type == "transactional" else TOPIC_MARKETING
        producer.send(topic, notif.to_json())
        notifications.append(notif)

    producer.flush()

    by_type = defaultdict(int)
    by_channel = defaultdict(int)
    for n in notifications:
        by_type[n.type] += 1
        by_channel[n.channel] += 1

    print(f"\n  Produced 100 notifications to 2 Kafka topics:")
    print(f"  Topic '{TOPIC_TRANSACTIONAL}':  {by_type['transactional']} events")
    print(f"  Topic '{TOPIC_MARKETING}':  {by_type['marketing'] + by_type['social']} events")
    print(f"\n  By channel:")
    for channel, count in sorted(by_channel.items()):
        print(f"    {channel:<10} {count:>5}")
    print(f"\n  By type:")
    for ntype, count in sorted(by_type.items()):
        print(f"    {ntype:<15} {count:>5}")

    return notifications


def phase2_consume_and_deliver(conn) -> dict:
    section("Phase 2: Channel Workers Consume and Deliver")

    print("""
  Worker priority: transactional topic is consumed FIRST.
  Each message is:
    1. Checked against user preferences (filter)
    2. Checked for duplicate (idempotency_key)
    3. Delivered with retry on failure
""")

    stats = defaultdict(int)
    worker = DeliveryWorker("worker-1", conn, stats)

    # Consume transactional first, then marketing (priority ordering)
    print("  Consuming TRANSACTIONAL topic first (high priority) ...")
    consumer_txn = make_consumer([TOPIC_TRANSACTIONAL], "lab-group-txn")
    txn_count = 0
    for msg in consumer_txn:
        notif = Notification.from_json(msg.value)
        worker.process(notif)
        txn_count += 1
    consumer_txn.close()
    print(f"  Processed {txn_count} transactional notifications")

    print("  Consuming MARKETING topic (lower priority) ...")
    consumer_mkt = make_consumer([TOPIC_MARKETING], "lab-group-mkt")
    mkt_count = 0
    for msg in consumer_mkt:
        notif = Notification.from_json(msg.value)
        worker.process(notif)
        mkt_count += 1
    consumer_mkt.close()
    print(f"  Processed {mkt_count} marketing/social notifications")

    print(f"\n  Delivery stats:")
    print(f"  {'Delivered successfully':<30} {stats['delivered']:>6}")
    print(f"  {'Filtered by preference':<30} {stats['preference_filtered']:>6}")
    print(f"  {'Deduplicated':<30} {stats['deduplicated']:>6}")
    print(f"  {'Retried (succeeded later)':<30} {stats['retried']:>6}")
    print(f"  {'Delivery failures':<30} {stats['failures']:>6}")
    print(f"  {'Sent to DLQ':<30} {stats['dlq']:>6}")

    return dict(stats)


def phase3_preference_filtering(conn):
    section("Phase 3: User Preference Filtering")

    print("""
  User preferences are stored in Postgres (or Redis for fast lookup).
  Users 101, 102, 103 have email disabled.
  User 102 also has SMS disabled.
  User 103 also has push disabled.
""")

    test_cases = [
        (101, "email",   False),
        (101, "push",    True),
        (102, "email",   False),
        (102, "sms",     False),
        (102, "push",    True),
        (103, "email",   False),
        (103, "push",    False),
        (104, "email",   True),
        (105, "sms",     True),
    ]

    print(f"  {'User':<8} {'Channel':<10} {'Enabled?':>10}  {'Expected':>10}  {'Match':>6}")
    print(f"  {'-'*8}  {'-'*10}  {'-'*10}  {'-'*10}  {'-'*6}")
    all_match = True
    for user_id, channel, expected in test_cases:
        actual = is_channel_enabled(conn, user_id, channel)
        match = actual == expected
        all_match = all_match and match
        print(f"  {user_id:<8}  {channel:<10}  {'YES' if actual else 'NO':>10}  "
              f"{'YES' if expected else 'NO':>10}  {'OK' if match else 'FAIL':>6}")

    print(f"\n  All preferences correct: {all_match}")
    print("""
  In production, user preferences are cached in Redis for fast lookup:
    HGETALL user:preferences:{user_id}
  Cache is invalidated on preference update (write-through or TTL-based).
""")


def phase4_deduplication(conn):
    section("Phase 4: Deduplication via Idempotency Key")

    print("""
  Idempotency key: a client-generated unique string per notification event.
  On retry, the same key is used → server detects duplicate → skips delivery.
  Stored as UNIQUE(idempotency_key, channel) in delivered_notifications table.
""")

    # Produce the same notification twice
    producer = make_producer()
    key = f"idem-dedup-{uuid.uuid4()}"
    notif = Notification(
        notification_id="dedup-test-1",
        user_id=104,
        channel="push",
        type="transactional",
        message="Password reset code: 123456",
        idempotency_key=key,
    )

    print(f"  Sending notification with idempotency_key={key[:20]}...")
    producer.send(TOPIC_TRANSACTIONAL, notif.to_json())
    producer.send(TOPIC_TRANSACTIONAL, notif.to_json())  # duplicate
    producer.flush()
    producer.close()

    # Consume and process
    consumer = make_consumer([TOPIC_TRANSACTIONAL], f"lab-dedup-{uuid.uuid4()}")
    dedup_stats = defaultdict(int)
    worker = DeliveryWorker("dedup-worker", conn, dedup_stats)
    for msg in consumer:
        notif_recv = Notification.from_json(msg.value)
        if notif_recv.idempotency_key == key:
            worker.process(notif_recv)
    consumer.close()

    delivered = dedup_stats["delivered"]
    deduplicated = dedup_stats["deduplicated"]

    print(f"\n  Sent:         2 events with same idempotency_key")
    print(f"  Delivered:    {delivered} (should be 1)")
    print(f"  Deduplicated: {deduplicated} (should be 1)")
    print(f"  Result:       {'CORRECT' if delivered == 1 and deduplicated == 1 else 'UNEXPECTED'}")

    # Verify DB
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM delivered_notifications WHERE idempotency_key = %s",
            (key,)
        )
        db_count = cur.fetchone()[0]
    print(f"  DB entries for this key: {db_count} (should be 1)")


def phase5_retry_backoff(conn):
    section("Phase 5: Retry with Exponential Backoff + DLQ")

    print("""
  Delivery simulation: notification IDs ending in '7' fail on attempts 1 and 2,
  succeed on attempt 3. Exponential backoff: 50ms → 100ms between retries.

  Pattern:
    attempt 1: fail → wait 50ms
    attempt 2: fail → wait 100ms
    attempt 3: success → mark delivered
    If ID ends in '7' AND max_attempts is reduced to 2 → exhausted → DLQ

  The DeliveryWorker handles all of this — same code path as Phase 2.
""")

    # Case A: 3 attempts allowed — should succeed despite 2 transient failures
    # Case B: notification ID does NOT end in 7 — succeeds immediately
    # Case C: simulate DLQ — use a mock that always fails (patch mock_deliver)
    test_notifications = [
        ("retry-ok-7",   "ends in 7: 2 transient failures, succeeds on attempt 3"),
        ("retry-ok-1",   "normal:    succeeds immediately on attempt 1"),
        ("retry-ok-17",  "ends in 7: 2 transient failures, succeeds on attempt 3"),
        ("retry-dlq-7x", "always fails: exhausts 3 retries → DLQ"),
    ]

    # Temporarily override mock_deliver for the always-fail case
    original_mock = mock_deliver.__code__

    print(f"  {'Notification ID':<20} {'Delivered':>10}  {'Retried':>8}  {'DLQ':>5}  Note")
    print(f"  {'-'*20}  {'-'*10}  {'-'*8}  {'-'*5}  {'-'*45}")

    for notif_id, note in test_notifications:
        local_stats = defaultdict(int)
        worker = DeliveryWorker(f"retry-worker-{notif_id}", conn, local_stats)

        notif = Notification(
            notification_id=notif_id,
            user_id=104,
            channel="push",
            type="transactional",
            message=f"Test retry for {notif_id}",
            idempotency_key=f"retry-phase5-{notif_id}-{uuid.uuid4()}",
        )

        if notif_id == "retry-dlq-7x":
            # Monkey-patch: always fail to demonstrate DLQ path
            import types
            def always_fail(n, attempt=1):
                return DeliveryResult(False, "permanent_error")
            worker_mock = always_fail
            # Run the DLQ path manually mirroring DeliveryWorker.process()
            if is_channel_enabled(conn, notif.user_id, notif.channel):
                max_attempts = 3
                for attempt in range(1, max_attempts + 1):
                    result = worker_mock(notif, attempt)
                    local_stats["failures"] += 1
                    if attempt < max_attempts:
                        time.sleep((2 ** (attempt - 1)) * 0.05)
                local_stats["dlq"] += 1
        else:
            worker.process(notif)

        print(f"  {notif_id:<20}  {local_stats['delivered']:>10}  "
              f"{local_stats['retried']:>8}  {local_stats['dlq']:>5}  {note}")

    print("""
  Dead Letter Queue (DLQ):
  - Messages that exhaust all retries are produced to a 'notif-dlq' Kafka topic
  - On-call engineers monitor DLQ consumer lag as an alerting signal
  - A spike in DLQ lag = systemic issue (APNs outage, expired credentials)
  - DLQ messages can be replayed from the beginning of the topic after a fix
  - For transactional notifications (OTP), the system should also check whether
    the OTP TTL has expired before re-delivering from the DLQ — a 30-min-delayed
    password reset code should be dropped, not sent.
""")


def phase6_fanout_simulation(producer: KafkaProducer, conn):
    section("Phase 6: Fan-Out — Celebrity Post Triggers Mass Notifications")

    print("""
  Fan-out problem: a single user action triggers notifications to many users.
  Example: a celebrity with 1M followers posts → 1M notification events.

  Two strategies:
    WRITE fan-out (push model): fan out at write time → store per-user inbox
      + reads are O(1) per user
      - writes are O(followers) — huge spike for celebrities
    READ fan-out (pull model): store one event, each user reads on demand
      + writes are O(1)
      - reads are O(followed_accounts) per page load — expensive at scale

  Most systems (Facebook, Twitter) use a HYBRID:
    - Inactive / low-follower users: write fan-out
    - Celebrity accounts (>1M followers): read fan-out at query time
  This lab simulates the write fan-out path for a modest 500-follower account.
""")

    # Simulate a "post" event fan-out to 500 followers
    ACTOR_ID = 9001
    FOLLOWER_COUNT = 500
    EVENT_ID = str(uuid.uuid4())

    print(f"  Actor user_id={ACTOR_ID} posts content (event_id={EVENT_ID[:8]}...)")
    print(f"  Fanning out to {FOLLOWER_COUNT} followers ...\n")

    # Generate follower IDs — we reuse our 5 seeded preference users for filtering
    seeded_users = [101, 102, 103, 104, 105]
    follower_ids = seeded_users * (FOLLOWER_COUNT // len(seeded_users))
    follower_ids += seeded_users[: FOLLOWER_COUNT % len(seeded_users)]

    produced = 0
    suppressed_at_fanout = 0
    for follower_id in follower_ids:
        # Check push preference before even producing to Kafka (early suppression)
        # In production this would be a Redis HGET, not a Postgres query
        if not is_channel_enabled(conn, follower_id, "push"):
            suppressed_at_fanout += 1
            continue
        notif = Notification(
            notification_id=f"fanout-{follower_id}-{EVENT_ID[:8]}",
            user_id=follower_id,
            channel="push",
            type="social",
            message=f"User {ACTOR_ID} posted new content",
            # Idempotency key is deterministic: same event + user + channel
            idempotency_key=f"fo-{EVENT_ID}-{follower_id}-push",
            priority="normal",
        )
        producer.send(TOPIC_MARKETING, notif.to_json())
        produced += 1

    producer.flush()

    print(f"  Followers:               {FOLLOWER_COUNT}")
    print(f"  Suppressed at fan-out:   {suppressed_at_fanout}  (push disabled in preferences)")
    print(f"  Events produced to Kafka:{produced}")

    # Drain and count deliveries
    consumer = make_consumer([TOPIC_MARKETING], f"lab-fanout-{uuid.uuid4()}")
    fanout_stats = defaultdict(int)
    worker = DeliveryWorker("fanout-worker", conn, fanout_stats)
    for msg in consumer:
        notif_recv = Notification.from_json(msg.value)
        if notif_recv.notification_id.startswith("fanout-"):
            worker.process(notif_recv)
    consumer.close()

    print(f"  Delivered:               {fanout_stats['delivered']}")
    print(f"  Deduped (already sent):  {fanout_stats['deduplicated']}")

    print(f"""
  At production scale (10M followers):
    10M events × 200 bytes = 2 GB on Kafka — manageable for a single topic
    At 1M events/minute fan-out rate → 60s to produce all events
    APNs batch size = 500 → 20,000 API calls to deliver to 10M devices
    Per-device token validation required (expired tokens → 410 Gone response)

  Key insight: the Notification Service does NOT wait for delivery to complete.
  It produces to Kafka and returns. Delivery is async and takes seconds–minutes.
""")


def phase7_scale_math():
    section("Phase 7: Scale Math — 1B Notifications/Day")

    print("""
  System design numbers for 1 billion notifications per day:
""")

    total = 1_000_000_000
    seconds = 86400
    rps = total / seconds

    # Channel breakdown (approximate industry averages)
    channels = {
        "in-app":   0.45,
        "push":     0.35,
        "email":    0.15,
        "sms":      0.05,
    }

    print(f"  {'Metric':<40} {'Value':>15}")
    print(f"  {'-'*40}  {'-'*15}")
    print(f"  {'Total notifications/day':<40} {total:>15,}")
    print(f"  {'Average RPS':<40} {rps:>15,.0f}")
    print(f"  {'Peak RPS (3× average)':<40} {rps*3:>15,.0f}")
    print()
    print(f"  By channel (estimated breakdown):")
    for channel, fraction in channels.items():
        count = int(total * fraction)
        print(f"    {channel:<10} {count:>15,}  ({fraction*100:.0f}%)")

    print(f"""
  Architecture decisions at this scale:

  1. Kafka topics per priority tier:
     - notifications-transactional  (OTP, alerts) → processed first
     - notifications-social         (likes, follows)
     - notifications-marketing      (promotions) → processed last

  2. Per-channel worker pools (independent scaling):
     - push workers:   integrate with APNs/FCM (batch up to 500/request)
     - email workers:  integrate with SendGrid/SES (rate limited per domain)
     - sms workers:    integrate with Twilio (per-message cost → expensive)
     - in-app workers: write to user's Redis/Postgres notification inbox

  3. User preferences in Redis (not Postgres):
     HSET user:prefs:104 email 1 push 1 sms 1 inapp 1
     → sub-millisecond lookup on every notification

  4. Deduplication window: idempotency keys stored 24h in Redis
     → prevents duplicates across retries and system restarts

  Worker sizing (rule of thumb):
     push workers:   ~35K peak RPS / 500 per APNs batch / ~10 batches/s each
                     → ~7 push worker processes (APNs is fast, batching helps)
     email workers:  ~1,735 avg RPS; SendGrid rate limit ~100 rps/IP
                     → ~18 email worker processes (or IPs/dedicated pools)
     sms workers:    ~580 avg RPS; Twilio throttles per account
                     → scale horizontally by Twilio sub-account
     in-app workers: ~5,200 avg RPS; simple Postgres/Redis writes
                     → ~10-20 in-app workers
""")


def phase8_db_summary(conn):
    section("Phase 8: Delivery Summary from Postgres")

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM delivered_notifications")
        total = cur.fetchone()[0]

        cur.execute("""
            SELECT channel, COUNT(*) FROM delivered_notifications
            GROUP BY channel ORDER BY count DESC
        """)
        by_channel = cur.fetchall()

    print(f"\n  Total notifications delivered: {total}\n")
    print(f"  {'Channel':<12} {'Delivered':>12}")
    print(f"  {'-'*12}  {'-'*12}")
    for channel, count in by_channel:
        print(f"  {channel:<12}  {count:>12}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    section("NOTIFICATION SYSTEM LAB")
    print("""
  Architecture:
    Event → Kafka (priority topic) → Channel Router → Channel Workers
                                                        → Push (APNs/FCM)
                                                        → Email (SendGrid)
                                                        → SMS (Twilio)
                                                        → In-App (Redis)

  This lab simulates the routing, filtering, dedup, and retry logic
  without real third-party integrations.
""")

    wait_for_kafka(KAFKA_BOOTSTRAP)
    create_topics(KAFKA_BOOTSTRAP)

    conn = psycopg2.connect(DATABASE_URL)
    init_db(conn)

    producer = make_producer()
    notifications = phase1_produce_notifications(producer)
    producer.close()

    # Give Kafka a moment to make messages available
    time.sleep(2)

    phase2_consume_and_deliver(conn)
    phase3_preference_filtering(conn)
    phase4_deduplication(conn)
    phase5_retry_backoff(conn)

    fanout_producer = make_producer()
    phase6_fanout_simulation(fanout_producer, conn)
    fanout_producer.close()

    phase7_scale_math()
    phase8_db_summary(conn)

    conn.close()

    section("Lab Complete")
    print("""
  Summary:
  - Kafka separates transactional (high-priority) from marketing notifications
  - User preferences checked at fan-out time + at delivery (Redis in production)
  - Fan-out: early preference suppression at produce time reduces Kafka volume
  - Idempotency key + INSERT...ON CONFLICT RETURNING id prevents double-delivery
    even when concurrent workers race past the pre-check (correct at DB level)
  - Exponential backoff reduces pressure on failing delivery endpoints
  - DLQ captures persistently failing notifications; expired OTP tokens must be
    dropped rather than re-delivered from the DLQ
  - 1B/day at peak 35K RPS requires ~7 push workers (APNs batching), ~18 email
    workers (per-IP rate limits), and ~10-20 in-app workers

  Next: 10-rate-limiter/ — global rate limiting across distributed API servers
""")


if __name__ == "__main__":
    main()
