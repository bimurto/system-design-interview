#!/usr/bin/env python3
"""
Message Queues Fundamentals Lab — RabbitMQ

Prerequisites: docker compose up -d (wait ~15s for RabbitMQ to be ready)

What this demonstrates:
  1. Basic produce/consume with manual acknowledgement
  2. At-least-once delivery: consumer crash → message redelivered
  3. Dead-letter queue (DLQ): rejected messages go to DLQ after max attempts
  4. Competing consumers: work distributed across multiple consumer instances
  5. Pub/Sub fan-out: one message → multiple independent subscriber queues
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
        except Exception as e:
            if i == 0:
                print(f"  Waiting for RabbitMQ... (this can take ~15s)")
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
""")
    ch.queue_delete(queue="basic_demo")
    conn.close()


# ── Phase 2: At-least-once delivery (crash simulation) ────────

def phase2_at_least_once():
    section("Phase 2: At-Least-Once Delivery — Consumer Crash Simulation")
    print("""  Producer sends 3 messages.
  Consumer processes message 1 and acks it.
  Consumer "crashes" on message 2 (no ack, connection closed).
  Message 2 is redelivered to a new consumer.
  Message 3 is processed normally.
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
    print("  Published 3 messages.")

    # First consumer: processes 1, crashes on 2
    received_first = []
    crash_on = 2

    def crashing_consumer(ch, method, props, body):
        data = json.loads(body)
        msg_num = data["msg"]
        if msg_num == crash_on:
            print(f"  Consumer-1 received msg {msg_num} — simulating crash (no ack, closing connection)")
            # Don't ack — just close the connection
            conn1.close()
            return
        ch.basic_ack(delivery_tag=method.delivery_tag)
        received_first.append(msg_num)
        print(f"  Consumer-1 processed msg {msg_num} (acked)")
        if len(received_first) >= 1:
            ch.stop_consuming()

    ch1.basic_qos(prefetch_count=1)
    ch1.basic_consume(queue="alo_demo", on_message_callback=crashing_consumer)
    try:
        ch1.start_consuming()
    except Exception:
        pass  # connection closed during crash simulation

    time.sleep(0.5)  # allow RabbitMQ to redeliver

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
    Consumer-1 acked: {received_first}
    Consumer-2 received: {received_second}
    Redelivered flags: {redelivered_flags}

  Message {crash_on} was redelivered (redelivered=True) — RabbitMQ detected
  the connection drop and put the unacknowledged message back in the queue.

  This is at-least-once delivery. Consumer-2 must check for duplicates.
""")
    ch2.queue_delete(queue="alo_demo")
    conn2.close()


# ── Phase 3: Dead-Letter Queue ────────────────────────────────

def phase3_dlq():
    section("Phase 3: Dead-Letter Queue (DLQ)")
    print("""  A message rejected 3 times goes to the DLQ (dead_letters).
  This prevents one bad message from blocking the queue forever.
""")

    conn = connect()
    ch   = conn.channel()

    # Declare DLQ
    ch.queue_declare(queue="dead_letters", durable=True)

    # Declare main queue with DLQ routing and max-length for demo
    ch.queue_declare(
        queue="dlq_demo",
        durable=True,
        arguments={
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": "dead_letters",
            "x-message-ttl": 5000,  # 5s TTL as additional DLQ trigger
        },
    )

    # Publish one "poison pill" and two normal messages
    for payload in [{"type": "poison", "data": "bad_data"}, {"type": "normal", "data": "a"}, {"type": "normal", "data": "b"}]:
        ch.basic_publish(
            exchange="",
            routing_key="dlq_demo",
            body=json.dumps(payload),
            properties=pika.BasicProperties(delivery_mode=2),
        )
    print("  Published 1 poison pill + 2 normal messages.")

    processed = []
    dlq_sent  = []

    attempt_counts = Counter()

    def consumer(ch, method, props, body):
        data = json.loads(body)
        msg_type = data.get("type")
        attempt_counts[json.dumps(data)] += 1
        attempts = attempt_counts[json.dumps(data)]

        if msg_type == "poison":
            if attempts < 3:
                print(f"  Processing poison message — attempt {attempts}/3 — NACK (requeue)")
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                time.sleep(0.3)
            else:
                print(f"  Processing poison message — attempt {attempts}/3 — NACK (dead-letter)")
                dlq_sent.append(data)
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        else:
            processed.append(data["data"])
            print(f"  Processed normal message: {data['data']} (acked)")
            ch.basic_ack(delivery_tag=method.delivery_tag)

        if len(processed) == 2 and len(dlq_sent) == 1:
            ch.stop_consuming()

    ch.basic_qos(prefetch_count=1)
    ch.basic_consume(queue="dlq_demo", on_message_callback=consumer)
    ch.start_consuming()

    # Check DLQ
    method, props, body = ch.basic_get(queue="dead_letters", auto_ack=True)
    dlq_message = json.loads(body) if body else None

    print(f"""
  Normal messages processed: {processed}
  Poison message in DLQ: {dlq_message}

  Without a DLQ, the poison pill loops forever — blocking all consumers.
  With a DLQ, it's parked after 3 attempts for manual investigation.
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
        ch.basic_qos(prefetch_count=1)

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

    print(f"\n  Work distribution:")
    for consumer, count in sorted(consumer_counts.items()):
        bar = "#" * count
        print(f"    {consumer}: {count} jobs  {bar}")

    total = sum(consumer_counts.values())
    print(f"\n  Total processed: {total}/10 (each job processed by exactly one consumer)")
    print("""
  Adding more consumers increases throughput linearly.
  Each message goes to exactly ONE consumer — this is work distribution, not broadcast.
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
""")

    conn = connect()
    ch   = conn.channel()

    # Fanout exchange broadcasts to all bound queues
    ch.exchange_declare(exchange="order_events", exchange_type="fanout", durable=True)

    # Two subscriber queues
    for q in ["inventory-service", "notification-service"]:
        ch.queue_declare(queue=q, durable=True)
        ch.queue_bind(queue=q, exchange="order_events")

    # Publish one event
    event = {"event": "order.placed", "order_id": "ORD-999", "amount": 49.99}
    ch.basic_publish(
        exchange="order_events",
        routing_key="",  # ignored for fanout
        body=json.dumps(event),
        properties=pika.BasicProperties(delivery_mode=2),
    )
    print(f"  Published: {event}")

    time.sleep(0.2)  # let exchange route

    # Each subscriber reads independently
    for subscriber in ["inventory-service", "notification-service"]:
        method, props, body = ch.basic_get(queue=subscriber, auto_ack=True)
        if body:
            data = json.loads(body)
            print(f"  {subscriber:25s} received: order_id={data['order_id']}")
        else:
            print(f"  {subscriber:25s} received: (none)")

    print("""
  Each subscriber gets its own copy — independent processing.
  Inventory decrements stock; notification sends email.
  Neither knows the other exists.

  Point-to-point vs Pub/Sub:
    Point-to-point  — message goes to ONE consumer (work queue)
    Pub/Sub fan-out — message goes to ALL subscribers (event bus)
""")

    for q in ["inventory-service", "notification-service"]:
        ch.queue_delete(queue=q)
    ch.exchange_delete(exchange="order_events")
    conn.close()


# ── Main ──────────────────────────────────────────────────────

def main():
    section("MESSAGE QUEUES FUNDAMENTALS LAB — RabbitMQ")
    print("""
  Architecture:
    Producers → [Queue / Exchange] → Consumers

  A queue decouples producers from consumers in time and failure.
  The producer doesn't wait. The consumer processes at its own pace.

  Open the RabbitMQ management UI to watch queue depths:
    http://localhost:15672  (guest / guest)
""")

    print("  Connecting to RabbitMQ...")
    connect()  # blocks until ready
    print()

    phase1_basic()
    phase2_at_least_once()
    phase3_dlq()
    phase4_competing_consumers()
    phase5_pubsub()

    section("Summary")
    print("""
  Message Queue Concepts:
  ─────────────────────────────────────────────────────────────
  Temporal decoupling   — producer sends; consumer processes later
  Durable queue         — survives broker restart
  Manual ack            — message stays until consumer confirms success
  At-least-once         — redelivered on crash; consumer must be idempotent
  Dead-letter queue     — park messages after N failed attempts
  Competing consumers   — scale throughput by adding consumers
  Pub/Sub fan-out       — one message → all subscribers (each gets a copy)
  Visibility timeout    — message invisible during processing; reappears on crash

  Delivery Guarantees:
    At-most-once   — may lose messages (fire-and-forget telemetry)
    At-least-once  — may duplicate (production default; handle duplicates)
    Exactly-once   — complex; requires broker+consumer coordination (Kafka transactions)

  DLQ is non-negotiable in production.
  Without it, one poison pill blocks the entire queue indefinitely.

  Queue depth is your most important operational metric.
  Sustained growth means consumers are falling behind.

  Next steps: ../06-message-queues-kafka/
""")


if __name__ == "__main__":
    main()
