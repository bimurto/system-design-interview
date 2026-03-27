#!/usr/bin/env python3
"""
Event-Driven Architecture Lab — Event Sourcing with Kafka

Prerequisites: docker compose up -d (wait ~30s for Kafka to be ready)

What this demonstrates:
  1. Produce order lifecycle events (event sourcing write path)
  2. Rebuild current state by replaying the event log (CQRS projection)
  3. Consumer group lag — what it means and how to measure it correctly
  4. Idempotent consumers — processing duplicate events safely
  5. Replay from offset 0 — full state rebuild (the event sourcing superpower)
"""

import json
import time
import uuid
import subprocess
from datetime import datetime, timezone

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


def make_event(event_type, order_id, payload, event_id=None):
    return {
        "event_id":   event_id or str(uuid.uuid4()),
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
        # acks="all" waits for all in-sync replicas to acknowledge.
        # Combined with enable.idempotence=true on the broker this gives
        # exactly-once producer semantics (no duplicates from retries).
        acks="all",
        # Idempotent producer: Kafka assigns each producer a PID and tracks
        # sequence numbers so retried sends don't produce duplicates.
        enable_idempotence=True,
    )


def create_topic():
    admin = KafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    try:
        admin.create_topics([NewTopic(name=TOPIC, num_partitions=1, replication_factor=1)])
        print(f"  Topic '{TOPIC}' created (1 partition, replication_factor=1).")
    except TopicAlreadyExistsError:
        print(f"  Topic '{TOPIC}' already exists.")
    finally:
        admin.close()


def rebuild_state(events, seen_event_ids=None):
    """
    Rebuild order state from an ordered list of events.

    seen_event_ids: optional set for idempotent processing.
      Pass the same set across multiple calls to skip duplicates.
    """
    orders = {}
    duplicates_skipped = 0
    for ev in events:
        eid  = ev["event_id"]
        oid  = ev["order_id"]
        etype = ev["event_type"]

        # Idempotency check — skip events already processed
        if seen_event_ids is not None:
            if eid in seen_event_ids:
                duplicates_skipped += 1
                continue
            seen_event_ids.add(eid)

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

    return orders, duplicates_skipped


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
    """
    Return (end_offset, committed_offset, lag) for group_id on partition 0.

    Uses a separate admin client to read the committed offset; avoids
    polluting Kafka's group metadata with a spurious consumer group.
    """
    tp = TopicPartition(TOPIC, 0)

    # Get the high-watermark (log end offset) via a temporary consumer
    probe = KafkaConsumer(bootstrap_servers=BOOTSTRAP)
    probe.assign([tp])
    probe.seek_to_end(tp)
    end_offset = probe.position(tp)
    probe.close()

    # Get the committed offset for the actual consumer group
    admin = KafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    try:
        offsets = admin.list_consumer_group_offsets(group_id)
        committed = offsets.get(tp)
        committed_offset = committed.offset if committed else 0
    except Exception:
        committed_offset = 0
    finally:
        admin.close()

    lag = end_offset - committed_offset
    return end_offset, committed_offset, lag


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

        # order.delivered (all except ORD-0005 which stays in transit)
        if oid != "ORD-0005":
            ev = make_event("order.delivered", oid, {})
            producer.send(TOPIC, key=oid, value=ev)
            all_events.append(ev)

        print(f"    {oid}: created → paid → shipped" + (" → delivered" if oid != "ORD-0005" else " (in transit)"))

    # Cancel order 3 after delivery — last event wins in the projection
    ev = make_event("order.cancelled", "ORD-0003", {"reason": "customer request"})
    producer.send(TOPIC, key="ORD-0003", value=ev)
    all_events.append(ev)
    print(f"    ORD-0003: also received order.cancelled (customer request)")

    producer.flush()
    producer.close()
    print(f"\n  Total events published: {len(all_events)}")

    print(f"""
  Event schema:
  {{
    "event_id":   "uuid",            ← deduplication key (idempotency)
    "event_type": "order.created",   ← past-tense; entity.action
    "order_id":   "ORD-0001",        ← partition key (ordering guarantee)
    "timestamp":  "2025-01-01T14:32:01Z",
    "payload":    {{ ... event-specific data ... }}
  }}

  Key design decisions:
    • event_type uses dot notation: entity.action (order.created)
    • All events include a timestamp in UTC ISO 8601
    • The event_id is a UUID — consumers use it as a deduplication key
    • The order_id is the Kafka partition key — all events for one order
      go to the same partition and are strictly ordered there
    • Events are self-describing — consumers need no follow-up query
""")

    # ── Phase 2: Consume events, rebuild state ─────────────────────
    section("Phase 2: Consuming Events — Rebuild State from Log (CQRS Projection)")

    print(f"  Consuming all events from topic '{TOPIC}'...")
    time.sleep(1)
    consumed = consume_all_events("group-state-builder", from_beginning=True)
    print(f"  Consumed {len(consumed)} events.")

    state, _ = rebuild_state(consumed)

    print(f"\n  Current order state (projected from {len(consumed)} events):\n")
    print(f"  {'Order':<12} {'Status':<12} {'Items':<20} {'Event chain'}")
    print(f"  {'-'*12} {'-'*12} {'-'*20} {'-'*35}")
    for oid, order in sorted(state.items()):
        items_str = f"{len(order['items'])} items"
        events_str = " → ".join(e.split(".")[1] for e in order["events"])
        print(f"  {oid:<12} {order['status']:<12} {items_str:<20} {events_str}")

    print(f"""
  Notice: ORD-0003 ends up as 'cancelled' even though it was previously
  'delivered'. The projection applies events in order — the last state
  transition wins. This is correct: the event log preserves the full
  history but the projection shows current state.

  This is the CQRS read model:
    Write side: append events to Kafka (append-only log)
    Read side:  a consumer builds a queryable projection
                (could be Postgres, Redis, Elasticsearch, etc.)

  The read model can be rebuilt at any time from the event log.
  Multiple read models can co-exist, consuming the same event stream.
""")

    # ── Phase 3: Consumer group lag ────────────────────────────────
    section("Phase 3: Consumer Lag — Events Accumulate While Consumer is Stopped")

    print(f"  'group-state-builder' has consumed all {len(consumed)} events so far.")
    print(f"  Now producing 5 more events while that consumer is 'offline'...")
    print()

    producer = get_producer()
    new_event_count = 5
    for i in range(new_event_count):
        ev = make_event("order.created", f"ORD-NEW-{i:04d}", {"items": ["item-x"], "total": 9.99})
        producer.send(TOPIC, key=f"ORD-NEW-{i:04d}", value=ev)
        print(f"    Produced: order.created for ORD-NEW-{i:04d}")
    producer.flush()
    producer.close()
    time.sleep(1)

    end_offset, committed, lag = get_consumer_lag("group-state-builder")
    print(f"""
  Consumer group 'group-state-builder':
    Log end offset (total messages in partition): {end_offset}
    Committed offset (last processed by group):   {committed}
    Lag (messages not yet consumed):              {lag}

  Consumer lag = (log end offset) - (committed offset)
    • Lag = 0  → consumer is caught up
    • Lag > 0  → consumer is behind; {new_event_count} new events published while it was offline
    • Kafka retains ALL events regardless of consumption, up to the
      configured retention period (default: 7 days / KAFKA_LOG_RETENTION_MS)
    • This is unlike traditional MQ (RabbitMQ/SQS): consuming an event
      does NOT delete it from the Kafka log

  Production alert rule: alarm when lag > threshold AND is growing.
  A steady non-zero lag (processing at produce rate) is fine; a growing
  lag means the consumer can't keep up and will eventually miss events if
  retention expires before it catches up.
""")

    # ── Phase 4: Idempotent consumers ──────────────────────────────
    section("Phase 4: Idempotent Consumers — Handling Duplicate Events")

    print("""  At-least-once delivery is the Kafka default.
  A consumer crash after processing but before committing its offset
  causes it to re-read and re-process the same event on restart.
  Consumers MUST be idempotent — processing the same event twice must
  produce exactly the same result as processing it once.
""")

    # Use ORD-0001's full lifecycle (4 events: created, paid, shipped, delivered).
    # Inject a duplicate of the order.paid event to simulate a consumer crash
    # after processing but before committing the Kafka offset.
    # real-world risk: double-charge, double-email, double-inventory-deduction.
    ord1_events = all_events[:4]  # created, paid, shipped, delivered for ORD-0001
    pay_event   = all_events[1]   # the order.paid event specifically
    # Insert the duplicate paid event after the original paid (offset 1→dup at pos 2)
    duplicated = [ord1_events[0], ord1_events[1], pay_event,
                  ord1_events[2], ord1_events[3]]

    print(f"  Simulating at-least-once delivery for ORD-0001:")
    print(f"    Stream: created → paid → [DUPLICATE paid] → shipped → delivered")
    print(f"    This happens when consumer crashes after processing 'paid' but before")
    print(f"    committing the Kafka offset — Kafka redelivers 'paid' on restart.")
    print()

    # WITHOUT idempotency
    state_no_idem, _ = rebuild_state(duplicated, seen_event_ids=None)
    print(f"  Without idempotency check:")
    for oid, order in sorted(state_no_idem.items()):
        chain = " → ".join(e.split(".")[1] for e in order["events"])
        print(f"    {oid}: status={order['status']!r:12s}  event chain: {chain}")
    print(f"  ^^^ BUG: 'paid' appears twice in the event chain.")
    print(f"      In a payment system this means a double-charge.")
    print(f"      In email: duplicate confirmation sent. In inventory: double-deduction.")

    print()

    # WITH idempotency — pass a set to track seen event_ids
    seen = set()
    state_idem, dupes_skipped = rebuild_state(duplicated, seen_event_ids=seen)
    print(f"  With idempotency check:    {dupes_skipped} duplicate(s) skipped")
    for oid, order in sorted(state_idem.items()):
        chain = " → ".join(e.split(".")[1] for e in order["events"])
        print(f"    {oid}: status={order['status']!r:12s}  event chain: {chain}")
    print(f"  ^^^ CORRECT: event chain is clean, duplicate 'paid' discarded via event_id.")

    print(f"""
  The idempotent version produces the same state regardless of how many
  times each event is delivered — duplicates are discarded via event_id.

  Implementation options:
    1. In-memory set (lost on restart — only works for session dedup)
    2. Redis SET with TTL (fast, distributed, survives restarts)
    3. Database UNIQUE constraint on (event_id) in your projection table
       → INSERT INTO processed_events (event_id) ON CONFLICT DO NOTHING
       → If the insert succeeds, process the event; if it fails, skip it

  Exactly-once semantics (Kafka transactions):
    With enable.idempotence=true (producer) + transactional consumer,
    Kafka can guarantee exactly-once end-to-end within the Kafka
    ecosystem. Outside Kafka (e.g., writing to Postgres), you still
    need application-level idempotency as shown above.
""")

    # ── Phase 5: Replay from offset 0 ─────────────────────────────
    section("Phase 5: Replay from Offset 0 — Full State Rebuild")

    print(f"  Consumer 'group-replayer' reads ALL events from offset 0...")
    print(f"  (auto_offset_reset='earliest', fresh consumer group)\n")
    time.sleep(1)

    all_consumed = consume_all_events("group-replayer", from_beginning=True, timeout_ms=8000)
    seen_replay = set()
    rebuilt, _ = rebuild_state(all_consumed, seen_event_ids=seen_replay)

    print(f"  Replayed {len(all_consumed)} total events.")
    print(f"  Rebuilt state for {len(rebuilt)} orders:\n")
    print(f"  {'Order':<14} {'Status':<12} {'Distinct events seen'}")
    print(f"  {'-'*14} {'-'*12} {'-'*30}")
    for oid, order in sorted(rebuilt.items()):
        events_str = " → ".join(e.split(".")[1] for e in order["events"])
        print(f"  {oid:<14} {order['status']:<12} {events_str}")

    print(f"""
  The replay produced the exact same final state as Phase 2.
  This is the core guarantee of event sourcing.

  Why replay is powerful:
    • Bug fix: projection had a bug? Fix the code and replay —
      state is rebuilt correctly from the immutable source of truth
    • New feature: add a new read model (e.g. "revenue by day")
      without touching existing data — just replay the event log
    • Audit trail: the event log is a complete, immutable history
    • Time travel: replay events up to timestamp T to recover past state
    • Snapshot optimisation: for very long-lived aggregates, store a
      snapshot every N events and replay only from the last snapshot

  Event schema evolution:
    • Backward compatible: add optional fields (consumers ignore them)
    • Breaking change: requires a new event type (e.g. order.created.v2)
      or an upcaster that transforms old events on read
    • Use Avro/Protobuf schemas + a Schema Registry for strict contracts
      and automated compatibility enforcement
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
    + Rebuild projections freely (fix bugs, add read models)
    - Eventual consistency for reads (projection lag)
    - Event schema evolution requires care
    - Long-lived aggregates need snapshots to avoid slow replay

  CQRS (Command Query Responsibility Segregation)
    Separate write model (commands/events) from read model (queries)
    + Optimise reads and writes independently
    + Multiple read models for different query patterns
    - Two models to maintain
    - More complex infrastructure

  Choreography vs Orchestration (Sagas):
    Choreography: services react to events independently
      + Loose coupling; each service only knows about events
      - Hard to trace overall flow; debugging requires distributed tracing
    Orchestration: one saga service directs others via commands
      + Easy to observe flow; central error handling
      - Central coordinator is a coupling point and potential SPOF

  Delivery guarantees:
    At-most-once:   messages may be lost (fire-and-forget)
    At-least-once:  messages may be duplicated → consumers must be idempotent
    Exactly-once:   Kafka idempotent producer + transactions (Kafka-internal)

  Critical failure modes:
    Poison pill:    malformed event crashes consumer → dead-letter topic
    Consumer lag:   slow consumer misses events when retention expires
    Dual-write:     writing to DB and publishing event non-atomically →
                    use the Transactional Outbox pattern instead

  Next: ../05-message-queues-kafka/
""")


if __name__ == "__main__":
    main()
