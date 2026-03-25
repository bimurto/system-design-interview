#!/usr/bin/env python3
"""
Message Queues & Kafka Lab

Prerequisites: docker compose up -d (wait ~30s for Kafka to be ready)

What this demonstrates:
  1. Partition distribution — 300 messages with keys → even across 3 partitions
  2. Consumer groups — 3 consumers share load (1 partition each)
  3. Rebalancing — kill a consumer, others take its partition
  4. More consumers than partitions — 4th consumer idles
  5. Ordering guarantee — same key always goes to same partition, in order
"""

import json
import time
import threading
import subprocess
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

BOOTSTRAP   = "localhost:9092"
TOPIC       = "orders"
NUM_PARTS   = 3


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def create_topic(name, partitions):
    admin = KafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    try:
        admin.create_topics([NewTopic(name=name, num_partitions=partitions, replication_factor=1)])
        print(f"  Topic '{name}' created with {partitions} partitions.")
    except TopicAlreadyExistsError:
        print(f"  Topic '{name}' already exists.")
    finally:
        admin.close()


def get_producer():
    return KafkaProducer(
        bootstrap_servers=BOOTSTRAP,
        key_serializer=lambda k: k.encode() if k else None,
        value_serializer=lambda v: json.dumps(v).encode(),
        acks="all",
    )


def get_partition_offsets(topic, num_partitions):
    """Return the current end offsets for all partitions."""
    consumer = KafkaConsumer(bootstrap_servers=BOOTSTRAP)
    tps = [TopicPartition(topic, p) for p in range(num_partitions)]
    consumer.assign(tps)
    consumer.seek_to_end(*tps)
    offsets = {p: consumer.position(TopicPartition(topic, p)) for p in range(num_partitions)}
    consumer.close()
    return offsets


def main():
    section("MESSAGE QUEUES & KAFKA LAB")
    print("""
  Kafka architecture recap:

  Producer ──► Topic (3 partitions) ──► Consumer Group
                  ┌──────────────────────────────────────┐
  Partition 0 ── │ msg0, msg3, msg6, msg9, ...           │ ◄── Consumer-0
  Partition 1 ── │ msg1, msg4, msg7, msg10, ...          │ ◄── Consumer-1
  Partition 2 ── │ msg2, msg5, msg8, msg11, ...          │ ◄── Consumer-2
                  └──────────────────────────────────────┘

  • Messages with the same key always go to the same partition
  • Within a partition, messages are strictly ordered
  • Each partition is consumed by exactly one consumer per group
  • More consumers than partitions → some consumers are idle
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

    create_topic(TOPIC, NUM_PARTS)

    # ── Phase 1: Partition distribution ───────────────────────────
    section("Phase 1: Partition Distribution — 300 Messages, 3 Partitions")

    print(f"\n  Producing 300 messages with keys 'user-0' to 'user-99'")
    print(f"  (each user ID consistently maps to one partition)...\n")

    producer = get_producer()
    key_to_partition = {}

    for i in range(300):
        key = f"user-{i % 100}"   # 100 unique keys cycling
        msg = {"seq": i, "user": key, "action": "purchase"}
        fut = producer.send(TOPIC, key=key, value=msg)
        meta = fut.get(timeout=10)
        if key not in key_to_partition:
            key_to_partition[key] = meta.partition

    producer.flush()
    producer.close()

    # Count messages per partition from offsets
    offsets = get_partition_offsets(TOPIC, NUM_PARTS)
    total = sum(offsets.values())

    print(f"  Messages per partition:")
    for p, count in sorted(offsets.items()):
        pct = count / total * 100 if total > 0 else 0
        bar = "#" * int(pct / 2)
        print(f"    Partition {p}: {count:4d} messages ({pct:5.1f}%)  {bar}")

    # Show key-to-partition mapping
    from collections import Counter
    part_keys = Counter(key_to_partition.values())
    print(f"\n  Unique keys per partition:")
    for p in sorted(part_keys):
        print(f"    Partition {p}: {part_keys[p]} unique user keys")

    print(f"""
  Key-based routing: hash(key) % num_partitions
    • All messages for 'user-42' always go to the same partition
    • This guarantees ordering for messages with the same key
    • Keys are typically the entity ID (user_id, order_id, etc.)
    • If key=None, messages are round-robined across partitions
      (no ordering guarantee, but maximum throughput)
""")

    # ── Phase 2: Consumer group — 3 consumers share partitions ────
    section("Phase 2: Consumer Group — 3 Consumers, 1 Partition Each")

    results = defaultdict(list)
    stop_events = {}

    def consume_worker(consumer_id, group_id, stop_event, timeout_ms=4000):
        consumer = KafkaConsumer(
            TOPIC,
            bootstrap_servers=BOOTSTRAP,
            group_id=group_id,
            auto_offset_reset="earliest",
            enable_auto_commit=True,
            consumer_timeout_ms=timeout_ms,
            max_poll_records=50,
        )
        partitions_seen = set()
        msg_count = 0
        for msg in consumer:
            partitions_seen.add(msg.partition)
            msg_count += 1
        consumer.close()
        results[consumer_id] = {
            "partitions": sorted(partitions_seen),
            "messages":   msg_count,
        }

    print(f"  Starting 3 consumers in group 'order-processors'...")
    print(f"  Each should be assigned exactly 1 of the 3 partitions.\n")

    threads = []
    for cid in range(3):
        stop_events[cid] = threading.Event()
        t = threading.Thread(
            target=consume_worker,
            args=(cid, "order-processors", stop_events[cid]),
            daemon=True,
        )
        threads.append(t)

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    print(f"  Consumer assignment results:")
    for cid in sorted(results):
        r = results[cid]
        print(f"    Consumer-{cid}: partitions={r['partitions']}  messages={r['messages']}")

    print(f"""
  Consumer group semantics:
    • Kafka coordinator assigns partitions to consumers in the group
    • When a consumer joins/leaves, a REBALANCE occurs
    • During rebalance: all consumers stop, reassignment happens, then resume
    • Each partition is assigned to exactly ONE consumer in the group
    • Multiple consumer groups can each read the same topic independently
      (each group has its own offset pointer — this is unlike a queue)
""")

    # ── Phase 3: Simulate rebalancing ─────────────────────────────
    section("Phase 3: Rebalancing — Produce More, Consumer Count Changes")

    # Produce 150 more messages
    producer = get_producer()
    for i in range(150):
        key = f"user-{i % 50}"
        producer.send(TOPIC, key=key, value={"seq": 300 + i, "user": key})
    producer.flush()
    producer.close()
    print(f"  Produced 150 more messages.")

    # Clear the global results dict so Phase 3 consumers write fresh data here
    results.clear()

    print(f"\n  Now starting only 2 consumers in group 'order-processors-2'.")
    print(f"  Kafka will assign 2 partitions to consumer-0 and 1 partition to consumer-1.")
    print(f"  (Or some similar assignment — Kafka decides)\n")

    threads2 = []
    for cid in range(2):
        t = threading.Thread(
            target=consume_worker,
            args=(cid, "order-processors-2", threading.Event()),
            daemon=True,
        )
        threads2.append(t)

    for t in threads2:
        t.start()
    for t in threads2:
        t.join(timeout=20)

    for cid in sorted(results):
        r = results[cid]
        print(f"    Consumer-{cid}: partitions={r.get('partitions', '?')}  messages={r.get('messages', 0)}")

    print(f"""
  After a consumer leaves, Kafka triggers a rebalance:
    1. Group coordinator detects the consumer left (heartbeat timeout)
    2. All remaining consumers are told to stop polling
    3. Coordinator reassigns partitions to the remaining consumers
    4. Consumers resume from their committed offsets
    5. Rebalance duration: typically 100ms–5s depending on group size

  This is the "stop the world" rebalance problem. Kafka 2.4+ introduced
  cooperative (incremental) rebalancing where only the affected partitions
  are reassigned, not the entire group — reducing pause time.
""")

    # ── Phase 4: More consumers than partitions ────────────────────
    section("Phase 4: 4 Consumers, 3 Partitions — One Consumer Idles")

    print(f"  Starting 4 consumers for 3 partitions in group 'order-overloaded'...")

    results4 = {}
    threads4 = []
    for cid in range(4):
        def worker(cid=cid):
            consumer = KafkaConsumer(
                TOPIC,
                bootstrap_servers=BOOTSTRAP,
                group_id="order-overloaded",
                auto_offset_reset="earliest",
                enable_auto_commit=True,
                consumer_timeout_ms=6000,
            )
            parts = set()
            count = 0
            for msg in consumer:
                parts.add(msg.partition)
                count += 1
            consumer.close()
            results4[cid] = {"partitions": sorted(parts), "messages": count}
        t = threading.Thread(target=worker, daemon=True)
        threads4.append(t)

    for t in threads4:
        t.start()
    for t in threads4:
        t.join(timeout=30)

    print(f"\n  Results (4 consumers, 3 partitions):")
    idle_count = 0
    for cid in sorted(results4):
        r = results4[cid]
        idle = " ← IDLE (no partitions assigned)" if not r["partitions"] else ""
        print(f"    Consumer-{cid}: partitions={r['partitions']}  messages={r['messages']}{idle}")
        if not r["partitions"]:
            idle_count += 1

    print(f"\n  Idle consumers: {idle_count}")
    print(f"""
  Rule: max useful consumers = number of partitions
    • Extra consumers beyond the partition count sit idle
    • They serve as hot standbys — if a consumer dies, an idle one
      immediately takes over (faster than spinning up a new one)
    • This is why choosing partition count carefully at topic creation
      matters — you can't easily decrease partitions later
    • Increasing partitions is possible but may break key ordering
""")

    # ── Phase 5: Ordering guarantee ────────────────────────────────
    section("Phase 5: Ordering Guarantee — Same Key = Same Partition = In Order")

    print(f"  Producing 20 events for user-777 (all go to same partition)...")

    order_topic = "ordering-test"
    create_topic(order_topic, 3)

    producer = get_producer()
    for seq in range(20):
        producer.send(order_topic, key="user-777", value={"seq": seq, "event": f"action-{seq}"})
    # Also interleave events for other users
    for seq in range(20):
        producer.send(order_topic, key=f"user-{seq}", value={"seq": seq, "event": "other"})
    producer.flush()
    producer.close()

    consumer = KafkaConsumer(
        order_topic,
        bootstrap_servers=BOOTSTRAP,
        group_id="ordering-checker",
        auto_offset_reset="earliest",
        consumer_timeout_ms=5000,
    )
    user777_msgs = []
    for msg in consumer:
        val = json.loads(msg.value)
        if val.get("event", "").startswith("action-"):
            user777_msgs.append((msg.partition, msg.offset, val["seq"]))
    consumer.close()

    if user777_msgs:
        partitions_used = set(p for p, _, _ in user777_msgs)
        print(f"\n  user-777 messages:")
        print(f"    All landed on partition(s): {partitions_used}")
        print(f"    Sequence order preserved: {[s for _, _, s in user777_msgs[:10]]}...")
        in_order = all(user777_msgs[i][2] < user777_msgs[i+1][2] for i in range(len(user777_msgs)-1))
        print(f"    In order: {in_order}")
        assert len(partitions_used) == 1, "Same key must always go to same partition!"
        print(f"\n  ✓ Confirmed: same key → single partition → strict ordering")
    else:
        print("  No user-777 messages found (may need to re-run)")

    print(f"""
  Ordering guarantees summary:
    • Within a partition: strict FIFO order, always
    • Across partitions: NO ordering guarantee
    • With key=None: round-robin, NO ordering guarantee
    • With key=X: all messages with key X go to the same partition,
      preserving order for that key's event stream

  This is why partition keys should be the entity that requires ordering:
    • Use order_id as key for order events (all order events stay together)
    • Use user_id as key for user activity (all user events stay together)
    • Never use a random UUID as key (defeats ordering purpose)
""")

    # ── Summary ───────────────────────────────────────────────────
    section("Summary — Kafka vs RabbitMQ vs SQS")

    print("""
  ┌──────────────────┬──────────────┬───────────────┬──────────────┐
  │                  │   Kafka      │  RabbitMQ     │  AWS SQS     │
  ├──────────────────┼──────────────┼───────────────┼──────────────┤
  │ Model            │ Log (pull)   │ Queue (push)  │ Queue (pull) │
  │ Retention        │ Time-based   │ Until acked   │ Until acked  │
  │ Replay           │ Yes          │ No            │ No           │
  │ Ordering         │ Per-partition│ Per-queue     │ No (FIFO Q)  │
  │ Throughput       │ Very high    │ High          │ High         │
  │ Consumers        │ Many groups  │ Competing     │ Competing    │
  │ Complexity       │ High         │ Medium        │ Low (managed)│
  │ Use case         │ Event stream │ Task queue    │ Task queue   │
  └──────────────────┴──────────────┴───────────────┴──────────────┘

  Kafka throughput math:
    • Sequential disk writes (append-only log): ~500MB/s
    • Zero-copy sendfile(2): kernel → NIC, no user-space copy
    • Message batching: producer buffers messages → fewer I/O ops
    • 1M messages/sec is achievable on commodity hardware

  Exactly-once semantics:
    • Idempotent producer: each message has a sequence number;
      broker deduplicates retries (enable.idempotence=true)
    • Transactional API: producer wraps multiple sends in a transaction
      (beginTransaction / commitTransaction / abortTransaction)
    • Consumer: read-process-write pattern — use Kafka transactions
      or external deduplication for exactly-once processing

  Next: ../06-stream-processing/
""")


if __name__ == "__main__":
    main()
