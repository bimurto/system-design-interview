#!/usr/bin/env python3
"""
Event-Driven Architecture Lab — Event Sourcing with Kafka

Prerequisites: docker compose up -d (wait ~30s for Kafka to be ready)

What this demonstrates:
  1. Produce order lifecycle events (event sourcing)
  2. Rebuild current state by replaying the event log
  3. Consumer group lag — events accumulate while consumer is stopped
  4. Replay from offset 0 — full state rebuild from scratch
"""

import json
import time
import uuid
import subprocess
from datetime import datetime, timezone
from collections import defaultdict

try:
    from kafka import KafkaProducer, KafkaConsumer, TopicPartition
    from kafka.admin import KafkaAdminClient, NewTopic
    from kafka.errors import TopicAlreadyExistsError
except ImportError:
    print("Installing dependencies...")
    subprocess.run(["pip", "install", "kafka-python-ng", "-q"], check=True)
    from kafka import KafkaProducer, KafkaConsumer, TopicPartition
    from kafka.admin import KafkaAdminClient, NewTopic
    from kafka.errors import TopicAlreadyExistsError

BOOTSTRAP = "localhost:9092"
TOPIC     = "order-events"


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def make_event(event_type, order_id, payload):
    return {
        "event_id":   str(uuid.uuid4()),
        "event_type": event_type,
        "order_id":   order_id,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "payload":    payload,
    }


def get_producer():
    return KafkaProducer(
        bootstrap_servers=BOOTSTRAP,
        value_serializer=lambda v: json.dumps(v).encode(),
        key_serializer=lambda k: k.encode() if k else None,
        acks="all",
    )


def create_topic():
    admin = KafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    try:
        admin.create_topics([NewTopic(name=TOPIC, num_partitions=1, replication_factor=1)])
        print(f"  Topic '{TOPIC}' created.")
    except TopicAlreadyExistsError:
        print(f"  Topic '{TOPIC}' already exists.")
    finally:
        admin.close()


def rebuild_state(events):
    """Rebuild order state from an ordered list of events."""
    orders = {}
    for ev in events:
        oid  = ev["order_id"]
        etype = ev["event_type"]
        if etype == "order.created":
            orders[oid] = {
                "order_id": oid,
                "status":   "created",
                "items":    ev["payload"].get("items", []),
                "total":    ev["payload"].get("total", 0),
                "events":   [etype],
            }
        elif oid in orders:
            orders[oid]["events"].append(etype)
            if etype == "order.paid":
                orders[oid]["status"] = "paid"
                orders[oid]["payment_ref"] = ev["payload"].get("payment_ref")
            elif etype == "order.shipped":
                orders[oid]["status"] = "shipped"
                orders[oid]["tracking"] = ev["payload"].get("tracking_number")
            elif etype == "order.delivered":
                orders[oid]["status"] = "delivered"
            elif etype == "order.cancelled":
                orders[oid]["status"] = "cancelled"
                orders[oid]["reason"] = ev["payload"].get("reason")
    return orders


def consume_all_events(group_id, from_beginning=False, timeout_ms=5000):
    """Consume all available events from the topic."""
    consumer = KafkaConsumer(
        TOPIC,
        bootstrap_servers=BOOTSTRAP,
        group_id=group_id,
        value_deserializer=lambda v: json.loads(v.decode()),
        auto_offset_reset="earliest" if from_beginning else "latest",
        enable_auto_commit=True,
        consumer_timeout_ms=timeout_ms,
    )
    events = []
    for msg in consumer:
        events.append(msg.value)
    consumer.close()
    return events


def get_consumer_lag(group_id):
    """Return the consumer lag for the group."""
    consumer = KafkaConsumer(
        bootstrap_servers=BOOTSTRAP,
        group_id=f"{group_id}-lag-check",
    )
    tp = TopicPartition(TOPIC, 0)
    consumer.assign([tp])
    consumer.seek_to_end(tp)
    end_offset = consumer.position(tp)

    # Get committed offset for the actual group
    admin = KafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    try:
        offsets = admin.list_consumer_group_offsets(group_id)
        committed = offsets.get(tp)
        committed_offset = committed.offset if committed else 0
    except Exception:
        committed_offset = 0
    finally:
        admin.close()
        consumer.close()

    return end_offset, committed_offset, end_offset - committed_offset


def main():
    section("EVENT-DRIVEN ARCHITECTURE LAB — EVENT SOURCING")
    print("""
  Core concepts:

  Event    — an immutable fact about something that happened
             ("order #123 was placed at 14:32:01")

  Command  — a request for something to happen (may be rejected)
             ("please place order #123")

  Query    — a request for current state (no side effects)
             ("what is the status of order #123?")

  Event Sourcing — the event log IS the source of truth.
    - State = replay of all events
    - Events are append-only and immutable
    - Current state is a derived projection, not stored directly
    - You can always rebuild state from the event log
    - You can replay events to populate a new read model (CQRS)
""")

    # Verify connectivity
    try:
        from kafka import KafkaAdminClient
        admin = KafkaAdminClient(bootstrap_servers=BOOTSTRAP)
        admin.list_topics()
        admin.close()
        print("  Connected to Kafka.")
    except Exception as e:
        print(f"  ERROR: {e}")
        print("  Run: docker compose up -d && sleep 30")
        return

    create_topic()

    # ── Phase 1: Produce order lifecycle events ────────────────────
    section("Phase 1: Producing Order Lifecycle Events")

    producer = get_producer()
    orders_data = [
        {
            "order_id": f"ORD-{i:04d}",
            "items": [f"item-{j}" for j in range(1, 4)],
            "total": 99.99 * i,
        }
        for i in range(1, 6)
    ]

    all_events = []

    print(f"\n  Publishing event lifecycle for 5 orders:\n")
    for od in orders_data:
        oid = od["order_id"]

        # order.created
        ev = make_event("order.created", oid, {"items": od["items"], "total": od["total"]})
        producer.send(TOPIC, key=oid, value=ev)
        all_events.append(ev)

        # order.paid
        ev = make_event("order.paid", oid, {"payment_ref": f"PAY-{uuid.uuid4().hex[:8].upper()}"})
        producer.send(TOPIC, key=oid, value=ev)
        all_events.append(ev)

        # order.shipped
        ev = make_event("order.shipped", oid, {"tracking_number": f"TRACK-{uuid.uuid4().hex[:10].upper()}"})
        producer.send(TOPIC, key=oid, value=ev)
        all_events.append(ev)

        # order.delivered (all except last one)
        if oid != "ORD-0005":
            ev = make_event("order.delivered", oid, {})
            producer.send(TOPIC, key=oid, value=ev)
            all_events.append(ev)

        print(f"    {oid}: created → paid → shipped" + (" → delivered" if oid != "ORD-0005" else " (in transit)"))

    # Cancel order 3
    ev = make_event("order.cancelled", "ORD-0003", {"reason": "customer request"})
    producer.send(TOPIC, key="ORD-0003", value=ev)
    all_events.append(ev)
    print(f"    ORD-0003: also cancelled (customer request)")

    producer.flush()
    producer.close()
    print(f"\n  Total events published: {len(all_events)}")

    print(f"""
  Event schema:
  {{
    "event_id":   "uuid",
    "event_type": "order.created | order.paid | order.shipped | ...",
    "order_id":   "ORD-0001",
    "timestamp":  "2025-01-01T14:32:01Z",
    "payload":    {{ ... event-specific data ... }}
  }}

  Key design decisions:
    • event_type uses dot notation: entity.action (order.created)
    • All events include a timestamp in UTC ISO 8601
    • The event_id is a UUID for deduplication
    • The order_id is the partition key — all events for one order
      go to the same partition and are ordered there
""")

    # ── Phase 2: Consume events, rebuild state ─────────────────────
    section("Phase 2: Consuming Events — Rebuild State from Log")

    print(f"  Consuming all events from topic '{TOPIC}'...")
    time.sleep(1)
    consumed = consume_all_events("group-state-builder", from_beginning=True)
    print(f"  Consumed {len(consumed)} events.")

    state = rebuild_state(consumed)

    print(f"\n  Current order state (projected from {len(consumed)} events):\n")
    print(f"  {'Order':<12} {'Status':<12} {'Items':<20} {'Events'}")
    print(f"  {'-'*12} {'-'*12} {'-'*20} {'-'*30}")
    for oid, order in sorted(state.items()):
        items_str = f"{len(order['items'])} items"
        events_str = " → ".join(e.split(".")[1] for e in order["events"])
        print(f"  {oid:<12} {order['status']:<12} {items_str:<20} {events_str}")

    print(f"""
  This is the CQRS read model:
    Write side: append events to Kafka (append-only log)
    Read side:  a consumer builds a queryable projection
                (could be a database, cache, or in-memory dict)

  The read model can be rebuilt at any time from the event log.
  Multiple read models with different shapes can co-exist,
  each consuming the same event stream.
""")

    # ── Phase 3: Consumer group lag ────────────────────────────────
    section("Phase 3: Consumer Lag — Events Accumulate While Consumer is Stopped")

    print(f"  Consumer 'group-stopped-consumer' has never consumed anything.")
    print(f"  Producing 5 more events while it's 'stopped'...")

    producer = get_producer()
    for i in range(5):
        ev = make_event("order.created", f"ORD-NEW-{i}", {"items": ["item-x"], "total": 9.99})
        producer.send(TOPIC, key=f"ORD-NEW-{i}", value=ev)
    producer.flush()
    producer.close()
    time.sleep(1)

    end_offset, committed, lag = get_consumer_lag("group-stopped-consumer")
    print(f"\n  Consumer group 'group-stopped-consumer':")
    print(f"    Latest offset (end of topic): {end_offset}")
    print(f"    Committed offset:             {committed}")
    print(f"    Lag:                          {lag} messages behind")

    print(f"""
  Consumer lag = (end offset) - (committed offset)
    • Lag=0: consumer is caught up
    • Lag>0: consumer is behind — check for slow consumer or high produce rate
    • Kafka retains messages per retention policy (default: 7 days)
      regardless of whether consumers have read them
    • This is unlike traditional MQ (RabbitMQ): in Kafka, consuming
      an event does NOT delete it from the log
""")

    # ── Phase 4: Replay from offset 0 ─────────────────────────────
    section("Phase 4: Replay from Offset 0 — Full State Rebuild")

    print(f"  Consumer 'group-replayer' will read ALL events from the beginning...")
    print(f"  (This is a fresh consumer with auto_offset_reset='earliest')")
    time.sleep(1)

    all_consumed = consume_all_events("group-replayer", from_beginning=True, timeout_ms=8000)
    rebuilt = rebuild_state(all_consumed)

    print(f"\n  Replayed {len(all_consumed)} total events.")
    print(f"  Rebuilt state for {len(rebuilt)} orders:")

    for oid, order in sorted(rebuilt.items()):
        print(f"    {oid}: status={order['status']}")

    print(f"""
  Why replay is powerful:
    • Bug fix: if your projection had a bug, fix the code and
      replay all events — the state is rebuilt correctly
    • New feature: add a new read model (e.g., "sales by category")
      without touching existing data — just replay the event log
    • Audit trail: the event log is a complete, immutable history
    • Time travel: replay events up to timestamp T to see past state

  Event schema evolution:
    • Backward compatible: add optional fields (consumers ignore them)
    • Forward compatible: remove fields (producers still send them)
    • Breaking change: requires a new event type or a migration
    • Use Avro/Protobuf schemas + a Schema Registry for strict contracts
""")

    # ── Summary ───────────────────────────────────────────────────
    section("Summary — Event-Driven Architecture")

    print("""
  Patterns in event-driven systems:
  ─────────────────────────────────────────────────────────────
  Event Sourcing
    Store events as the source of truth (not current state)
    + Complete audit trail
    + Temporal queries (state at any point in time)
    + Rebuild projections freely
    - Eventual consistency for reads
    - Event schema evolution is tricky
    - High event volume can make replay slow (use snapshots)

  CQRS (Command Query Responsibility Segregation)
    Separate write model (commands/events) from read model (queries)
    + Optimise reads and writes independently
    + Multiple read models for different query patterns
    - Two models to keep in sync
    - More complex infrastructure

  Choreography vs Orchestration:
    Choreography: services react to events independently (no coordinator)
      + Loose coupling; each service only knows about events
      - Hard to see overall flow; debugging is harder
    Orchestration: one service directs others via commands
      + Easy to see flow; central place to handle errors
      - Central coordinator is a coupling point and SPOF

  Delivery guarantees:
    At-most-once:   messages may be lost (fire-and-forget)
    At-least-once:  messages may be duplicated (most systems default)
    Exactly-once:   Kafka with idempotent producer + transactions

  Next: ../05-message-queues-kafka/
""")


if __name__ == "__main__":
    main()
