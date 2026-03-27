#!/usr/bin/env python3
"""
Message Queues Fundamentals Lab — RabbitMQ

Prerequisites: docker compose up -d (wait ~15s for RabbitMQ to be ready)

What this demonstrates:
  1. Basic produce/consume with manual acknowledgement
  2. At-least-once delivery: consumer crash → message redelivered
  3. Dead-letter queue (DLQ): rejected messages go to DLQ — tracked via x-death headers
  4. Competing consumers: work distributed across multiple consumer instances
  5. Pub/Sub fan-out: one message → multiple independent subscriber queues
  6. Idempotent consumer: deduplication by message ID prevents double-processing
"""

import json
import time
import threading
import subprocess
from collections import Counter

try:
    import pika
except ImportError:
    print("Installing dependencies...")
    subprocess.run(["pip", "install", "pika", "-q"], check=True)
    import pika

AMQP_URL = "amqp://guest:guest@localhost:5672/"


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def connect(retries=20, delay=2):
    for i in range(retries):
        try:
            conn = pika.BlockingConnection(pika.URLParameters(AMQP_URL))
            return conn
        except Exception:
            if i == 0:
                print("  Waiting for RabbitMQ... (this can take ~15s)")
            time.sleep(delay)
    raise RuntimeError("RabbitMQ not ready. Run: docker compose up -d")


# ── Phase 1: Basic produce/consume ────────────────────────────

def phase1_basic():
    section("Phase 1: Basic Produce / Consume with Manual Ack")
    print("""  Producer sends 5 messages to a durable queue.
  Consumer receives and acknowledges each one.
  Manual ack = at-least-once delivery guarantee.
""")

    conn = connect()
    ch   = conn.channel()

    # Durable queue survives broker restart
    ch.queue_declare(queue="basic_demo", durable=True)

    # Publish 5 messages
    for i in range(1, 6):
        body = json.dumps({"job_id": i, "task": f"process_order_{i}"})
        ch.basic_publish(
            exchange="",
            routing_key="basic_demo",
            body=body,
            properties=pika.BasicProperties(delivery_mode=2),  # persistent
        )
    print("  Published 5 messages.")

    # Consume with manual ack
    received = []

    def on_message(ch, method, props, body):
        data = json.loads(body)
        received.append(data["job_id"])
        print(f"  Consumed: job_id={data['job_id']} task={data['task']}")
        ch.basic_ack(delivery_tag=method.delivery_tag)  # acknowledge
        if len(received) == 5:
            ch.stop_consuming()

    ch.basic_qos(prefetch_count=1)  # one message at a time per consumer
    ch.basic_consume(queue="basic_demo", on_message_callback=on_message)
    ch.start_consuming()

    print(f"\n  Consumed {len(received)} messages in order: {received}")
    print("""
  Key: basic_ack() removes the message from the queue.
  Without ack, the message stays in "unacknowledged" state and
  is redelivered when the consumer disconnects.

  prefetch_count=1 means the broker won't send the next message
  until the current one is acked — essential for fair dispatch
  across competing consumers with variable processing times.
""")
    ch.queue_delete(queue="basic_demo")
    conn.close()


# ── Phase 2: At-least-once delivery (crash simulation) ────────

def phase2_at_least_once():
    section("Phase 2: At-Least-Once Delivery — Consumer Crash Simulation")
    print("""  Producer sends 3 messages.
  Consumer-1 processes message 1 (acks), then crashes on message 2 (no ack).
  RabbitMQ detects the connection drop and requeues message 2.
  Consumer-2 picks up message 2 (redelivered=True) and message 3.
""")

    conn1 = connect()
    ch1   = conn1.channel()
    ch1.queue_declare(queue="alo_demo", durable=True)

    for i in range(1, 4):
        ch1.basic_publish(
            exchange="",
            routing_key="alo_demo",
            body=json.dumps({"msg": i}),
            properties=pika.BasicProperties(delivery_mode=2),
        )
    print("  Published 3 messages (msg 1, 2, 3).")

    # First consumer: acks msg 1, simulates crash on msg 2 (closes connection without ack)
    acked_by_consumer1 = []
    crash_on = 2
    stop_after_first_ack = threading.Event()

    def crashing_consumer(ch, method, props, body):
        data = json.loads(body)
        msg_num = data["msg"]
        if msg_num == crash_on:
            print(f"  Consumer-1 received msg {msg_num} — simulating crash (closing connection without ack)")
            # Intentionally do not ack — close connection to trigger requeue
            conn1.close()
            stop_after_first_ack.set()
            return
        ch.basic_ack(delivery_tag=method.delivery_tag)
        acked_by_consumer1.append(msg_num)
        print(f"  Consumer-1 processed msg {msg_num} (acked)")
        # Stop after first ack so we fall through to msg 2 (the crash case)

    ch1.basic_qos(prefetch_count=1)
    ch1.basic_consume(queue="alo_demo", on_message_callback=crashing_consumer)
    try:
        ch1.start_consuming()
    except Exception:
        pass  # connection closed during crash simulation

    time.sleep(0.5)  # allow RabbitMQ to detect disconnect and requeue msg 2

    # Second consumer: picks up redelivered msg 2 and original msg 3
    conn2 = connect()
    ch2   = conn2.channel()
    ch2.queue_declare(queue="alo_demo", durable=True)

    received_second = []
    redelivered_flags = []

    def recovery_consumer(ch, method, props, body):
        data = json.loads(body)
        msg_num = data["msg"]
        redelivered = method.redelivered
        redelivered_flags.append(redelivered)
        received_second.append(msg_num)
        ch.basic_ack(delivery_tag=method.delivery_tag)
        flag = "REDELIVERED" if redelivered else "new"
        print(f"  Consumer-2 processed msg {msg_num} [{flag}] (acked)")
        if len(received_second) >= 2:
            ch.stop_consuming()

    ch2.basic_qos(prefetch_count=1)
    ch2.basic_consume(queue="alo_demo", on_message_callback=recovery_consumer)
    ch2.start_consuming()

    print(f"""
  Summary:
    Consumer-1 acked msgs : {acked_by_consumer1}
    Consumer-2 received   : {received_second}
    Redelivered flags     : {redelivered_flags}

  Msg {crash_on} was redelivered (redelivered=True).
  RabbitMQ detected the TCP connection drop and put the
  unacknowledged message back in the queue automatically.

  IMPORTANT: redelivered=True is a hint, not a proof of duplication.
  A message can be redelivered even if the consumer processed it
  successfully but crashed before sending the ack. Your consumer
  must be idempotent — see Phase 6 for the deduplication pattern.
""")
    ch2.queue_delete(queue="alo_demo")
    conn2.close()


# ── Phase 3: Dead-Letter Queue ────────────────────────────────

def phase3_dlq():
    section("Phase 3: Dead-Letter Queue (DLQ) — x-death Header Tracking")
    print("""  A poison pill message is rejected (nack, requeue=False) and routed
  to the dead-letter queue (DLQ). The broker attaches x-death headers
  so operators can see exactly how many times and where a message failed.

  Production pattern: application code checks x-death count to decide
  whether to retry (requeue=True) or dead-letter (requeue=False).
  This is more reliable than an in-process counter, which resets on crash.
""")

    conn = connect()
    ch   = conn.channel()

    # Declare DLQ first
    ch.queue_declare(queue="dead_letters", durable=True)

    # Declare main queue pointing failed messages at the DLQ
    ch.queue_declare(
        queue="dlq_demo",
        durable=True,
        arguments={
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": "dead_letters",
        },
    )

    # Publish one "poison pill" and two normal messages
    for payload in [
        {"type": "poison", "data": "bad_payload", "msg_id": "poison-001"},
        {"type": "normal", "data": "a",            "msg_id": "normal-001"},
        {"type": "normal", "data": "b",            "msg_id": "normal-002"},
    ]:
        ch.basic_publish(
            exchange="",
            routing_key="dlq_demo",
            body=json.dumps(payload),
            properties=pika.BasicProperties(
                delivery_mode=2,
                message_id=payload["msg_id"],  # used for deduplication
            ),
        )
    print("  Published 1 poison pill + 2 normal messages.")

    processed = []
    dlq_sent  = []
    MAX_ATTEMPTS = 3

    def consumer(ch, method, props, body):
        data = json.loads(body)
        msg_type = data.get("type")

        # Read retry count from x-death headers (broker-populated, crash-safe)
        x_death = (props.headers or {}).get("x-death", [])
        # x-death is a list of death records; sum the counts across all entries
        previous_deaths = sum(entry.get("count", 0) for entry in x_death) if x_death else 0
        attempt = previous_deaths + 1  # current attempt

        if msg_type == "poison":
            if attempt < MAX_ATTEMPTS:
                print(f"  Poison msg (attempt {attempt}/{MAX_ATTEMPTS}) — nack, requeue for retry")
                # Requeue=True: goes back to the head of the queue
                # In production, use a separate retry queue with per-attempt TTL
                # to avoid blocking other messages. Shown here simplified.
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                time.sleep(0.2)  # avoid tight spin
            else:
                print(f"  Poison msg (attempt {attempt}/{MAX_ATTEMPTS}) — max retries exceeded, dead-lettering")
                dlq_sent.append(data["msg_id"])
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        else:
            processed.append(data["data"])
            print(f"  Normal msg processed: data={data['data']} msg_id={data['msg_id']} (acked)")
            ch.basic_ack(delivery_tag=method.delivery_tag)

        if len(processed) == 2 and len(dlq_sent) == 1:
            ch.stop_consuming()

    ch.basic_qos(prefetch_count=1)
    ch.basic_consume(queue="dlq_demo", on_message_callback=consumer)
    ch.start_consuming()

    # Inspect the DLQ — broker attaches x-death headers with full failure history
    method, props, body = ch.basic_get(queue="dead_letters", auto_ack=True)
    if body:
        dlq_data = json.loads(body)
        x_death_headers = (props.headers or {}).get("x-death", [])
        total_deaths = sum(e.get("count", 0) for e in x_death_headers)
        print(f"""
  DLQ contents:
    Message    : {dlq_data}
    x-death count (broker-tracked retries): {total_deaths}
    Source queue: {x_death_headers[0].get("queue") if x_death_headers else "n/a"}

  x-death headers are set by the broker, not the consumer.
  They survive consumer crashes — reliable retry counting in production.
""")

    print(f"""  Normal messages processed : {processed}
  Dead-lettered msg_ids    : {dlq_sent}

  Production notes:
  - Monitor DLQ depth as a separate alert — it means bugs or bad data.
  - Never silently drop DLQ messages. Replay or manually inspect.
  - Use per-attempt TTL retry queues (x-message-ttl) for exponential backoff.
  - Without a DLQ, the poison pill loops forever blocking all consumers.
""")
    ch.queue_delete(queue="dlq_demo")
    ch.queue_delete(queue="dead_letters")
    conn.close()


# ── Phase 4: Competing consumers ──────────────────────────────

def phase4_competing_consumers():
    section("Phase 4: Competing Consumers — Work Distribution")
    print("""  10 jobs published to one queue.
  3 consumers compete — each picks up available messages.
  Work is distributed (not replicated) across consumers.
  prefetch_count=1 ensures fair dispatch regardless of processing speed.
""")

    conn_prod = connect()
    ch_prod   = conn_prod.channel()
    ch_prod.queue_declare(queue="work_queue", durable=True)

    for i in range(1, 11):
        ch_prod.basic_publish(
            exchange="",
            routing_key="work_queue",
            body=json.dumps({"job": i}),
            properties=pika.BasicProperties(delivery_mode=2),
        )
    conn_prod.close()
    print("  Published 10 jobs.")

    consumer_counts = Counter()
    lock = threading.Lock()
    done = threading.Event()

    def make_consumer(consumer_id):
        conn = connect()
        ch   = conn.channel()
        ch.queue_declare(queue="work_queue", durable=True)
        ch.basic_qos(prefetch_count=1)  # critical: without this, one consumer hogs all messages

        def on_msg(ch, method, props, body):
            data = json.loads(body)
            time.sleep(0.05)  # simulate work
            ch.basic_ack(delivery_tag=method.delivery_tag)
            with lock:
                consumer_counts[f"consumer-{consumer_id}"] += 1
                total = sum(consumer_counts.values())
                if total == 10:
                    done.set()

        ch.basic_consume(queue="work_queue", on_message_callback=on_msg)
        try:
            while not done.is_set():
                conn.process_data_events(time_limit=0.1)
        except Exception:
            pass
        conn.close()

    threads = [threading.Thread(target=make_consumer, args=(i,), daemon=True) for i in range(1, 4)]
    for t in threads:
        t.start()

    done.wait(timeout=15)

    print(f"\n  Work distribution (prefetch_count=1, fair round-robin):")
    for consumer, count in sorted(consumer_counts.items()):
        bar = "#" * count
        print(f"    {consumer}: {count} jobs  {bar}")

    total = sum(consumer_counts.values())
    print(f"""
  Total processed: {total}/10 (each job to exactly ONE consumer)

  Scaling insight:
    Adding consumers increases throughput nearly linearly up to the
    number of queue partitions (in RabbitMQ: limited by queue count;
    in Kafka: limited by partition count). Beyond that, extra consumers
    sit idle. Plan partition count = max expected consumer count.
""")

    conn_cleanup = connect()
    ch_cleanup = conn_cleanup.channel()
    ch_cleanup.queue_delete(queue="work_queue")
    conn_cleanup.close()


# ── Phase 5: Pub/Sub fan-out ──────────────────────────────────

def phase5_pubsub():
    section("Phase 5: Pub/Sub Fan-Out")
    print("""  One "order.placed" event published to a fanout exchange.
  Two independent subscribers each receive their own copy:
    - inventory-service queue
    - notification-service queue

  Neither service knows the other exists — loose coupling.
  Adding a new subscriber (e.g., analytics-service) requires zero
  changes to the producer or existing consumers.
""")

    conn = connect()
    ch   = conn.channel()

    # Fanout exchange broadcasts to all bound queues
    ch.exchange_declare(exchange="order_events", exchange_type="fanout", durable=True)

    # Two subscriber queues — each independently durable
    for q in ["inventory-service", "notification-service"]:
        ch.queue_declare(queue=q, durable=True)
        ch.queue_bind(queue=q, exchange="order_events")

    # Publish one event
    event = {"event": "order.placed", "order_id": "ORD-999", "amount": 49.99}
    ch.basic_publish(
        exchange="order_events",
        routing_key="",  # ignored for fanout; exchange routes to all bindings
        body=json.dumps(event),
        properties=pika.BasicProperties(delivery_mode=2),
    )
    print(f"  Published: {event}")

    time.sleep(0.2)  # let exchange route to bound queues

    # Each subscriber reads independently
    for subscriber in ["inventory-service", "notification-service"]:
        method, props, body = ch.basic_get(queue=subscriber, auto_ack=True)
        if body:
            data = json.loads(body)
            print(f"  {subscriber:25s} received: order_id={data['order_id']} amount={data['amount']}")
        else:
            print(f"  {subscriber:25s} received: (none — missed the event)")

    print("""
  If a subscriber queue is not declared before the event is published,
  it misses the message. This is the key difference between fanout
  (durable queues catch messages while consumer is offline) and Redis
  Pub/Sub (subscribers miss events when disconnected — no buffering).

  Point-to-point vs Pub/Sub vs Hybrid:
    Point-to-point  — 1 message → 1 consumer (work queue, task distribution)
    Pub/Sub fan-out — 1 message → ALL subscribers (event bus, broadcasting)
    Kafka hybrid    — per consumer group (pub/sub) + per partition (competing)
""")

    for q in ["inventory-service", "notification-service"]:
        ch.queue_delete(queue=q)
    ch.exchange_delete(exchange="order_events")
    conn.close()


# ── Phase 6: Idempotent consumer (deduplication by message ID) ──

def phase6_idempotent_consumer():
    section("Phase 6: Idempotent Consumer — Deduplication by Message ID")
    print("""  At-least-once delivery means duplicates WILL occur under failure.
  An idempotent consumer must handle receiving the same message twice
  without double-processing (e.g., double-charging, duplicate emails).

  Strategy: track processed message IDs in a seen-set.
  In production: use Redis SETNX or a DB unique constraint as the seen-set.
  Here we use an in-memory set to illustrate the pattern.

  Scenario: producer sends 5 messages; 2 are "redelivered duplicates"
  (simulating what happens after a consumer crash and broker redeliver).
""")

    conn = connect()
    ch   = conn.channel()
    ch.queue_declare(queue="idem_demo", durable=True)

    # Publish 5 unique messages, then simulate redelivery by publishing msg-2 and msg-4 again
    base_messages = [
        {"msg_id": f"msg-{i}", "payload": f"order_{i}"} for i in range(1, 6)
    ]
    duplicate_messages = [base_messages[1], base_messages[3]]  # msg-2, msg-4 duplicated

    all_to_publish = base_messages + duplicate_messages
    for msg in all_to_publish:
        ch.basic_publish(
            exchange="",
            routing_key="idem_demo",
            body=json.dumps(msg),
            properties=pika.BasicProperties(
                delivery_mode=2,
                message_id=msg["msg_id"],  # stable ID across redeliveries
            ),
        )
    print(f"  Published {len(base_messages)} original + {len(duplicate_messages)} duplicate messages = {len(all_to_publish)} total in queue.")

    seen_ids = set()       # in production: Redis SETNX or DB unique constraint
    processed = []
    duplicates_skipped = []
    total_received = [0]

    def idempotent_handler(ch, method, props, body):
        data   = json.loads(body)
        msg_id = props.message_id or data.get("msg_id")
        total_received[0] += 1

        if msg_id in seen_ids:
            duplicates_skipped.append(msg_id)
            print(f"  DUPLICATE  msg_id={msg_id} — already processed, skipping (ack to remove from queue)")
            ch.basic_ack(delivery_tag=method.delivery_tag)  # still ack to clear queue
        else:
            seen_ids.add(msg_id)
            processed.append(msg_id)
            print(f"  PROCESSED  msg_id={msg_id} payload={data['payload']}")
            ch.basic_ack(delivery_tag=method.delivery_tag)

        if total_received[0] == len(all_to_publish):
            ch.stop_consuming()

    ch.basic_qos(prefetch_count=1)
    ch.basic_consume(queue="idem_demo", on_message_callback=idempotent_handler)
    ch.start_consuming()

    print(f"""
  Results:
    Total received   : {total_received[0]} (originals + duplicates)
    Unique processed : {len(processed)} → {processed}
    Duplicates skipped: {len(duplicates_skipped)} → {duplicates_skipped}

  The processing count matches the unique message count — correct behavior.

  Production deduplication patterns:
    1. Redis SETNX(msg_id, expiry=TTL) — fast, O(1), TTL matches retention
    2. DB INSERT ... ON CONFLICT DO NOTHING — durable, works across restarts
    3. Idempotent operation design — e.g., UPDATE SET status='sent' WHERE status!='sent'
       (no separate seen-set needed if the operation itself is naturally idempotent)

  Key insight: always ack duplicates. Nacking a duplicate requeues it —
  causing infinite redelivery of a message you intentionally want to discard.
""")
    ch.queue_delete(queue="idem_demo")
    conn.close()


# ── Main ──────────────────────────────────────────────────────

def main():
    section("MESSAGE QUEUES FUNDAMENTALS LAB — RabbitMQ")
    print("""
  Architecture:
    Producers → [Queue / Exchange] → Consumers

  A queue decouples producers from consumers in time and failure.
  The producer doesn't wait. The consumer processes at its own pace.

  Open the RabbitMQ management UI to watch queue depths live:
    http://localhost:15672  (guest / guest)
    → Queues tab: depth, unacknowledged count, message rates
    → Connections tab: see consumer connections drop in Phase 2
""")

    print("  Connecting to RabbitMQ...")
    connect()  # blocks until ready
    print()

    phase1_basic()
    phase2_at_least_once()
    phase3_dlq()
    phase4_competing_consumers()
    phase5_pubsub()
    phase6_idempotent_consumer()

    section("Summary")
    print("""
  Message Queue Concepts Demonstrated:
  ─────────────────────────────────────────────────────────────
  Temporal decoupling   — producer sends; consumer processes later
  Durable queue         — survives broker restart (delivery_mode=2 + durable=True)
  Manual ack            — message stays until consumer confirms success
  prefetch_count=1      — fair dispatch; broker waits for ack before next msg
  At-least-once         — redelivered on crash; redelivered=True flag is a hint
  Dead-letter queue     — park messages after N failed attempts; x-death tracks retries
  Competing consumers   — scale throughput by adding consumers to the same queue
  Pub/Sub fan-out       — one message → all subscribers; each gets independent copy
  Idempotent consumer   — deduplicate by message_id; always ack duplicates

  Delivery Guarantees:
    At-most-once   — may lose messages (fire-and-forget telemetry, low-value events)
    At-least-once  — may duplicate (production default; handle with idempotent consumers)
    Exactly-once   — complex; requires broker+consumer coordination (Kafka transactions)

  Non-negotiable production requirements:
    1. DLQ configured — without it, one poison pill blocks the queue forever
    2. Idempotent consumers — duplicates WILL happen; design for them from day one
    3. Queue depth alerting — sustained growth = consumers falling behind
    4. x-death header tracking — crash-safe retry counting, not in-process counters

  Common failure modes:
    - Visibility timeout < processing time → phantom duplicate work
    - No DLQ → poison pill infinite loop starves all consumers
    - Not acking duplicates → infinite redelivery of intentionally skipped messages
    - prefetch_count too high → slow consumers hoard messages, blocking fast ones

  Next steps: ../06-message-queues-kafka/
""")


if __name__ == "__main__":
    main()
