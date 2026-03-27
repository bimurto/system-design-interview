#!/usr/bin/env python3
"""
Message Queues & Kafka Lab

Prerequisites: docker compose up -d (wait ~30s for Kafka to be ready)

What this demonstrates:
  1. Partition distribution — 300 messages with keys → even across 3 partitions
  2. Consumer groups — 3 consumers share load (1 partition each)
  3. Rebalancing — live consumer group shrinks from 3 to 2, showing partition takeover
  4. More consumers than partitions — 4th consumer idles
  5. Ordering guarantee — same key always goes to same partition, in order
  6. Consumer lag — producer outpaces consumer; lag is measured and explained
  7. Dead letter topic — poison-pill messages routed to DLT, processing continues
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


def create_topic(name, partitions, cleanup_policy="delete"):
    admin = KafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    try:
        configs = {}
        if cleanup_policy == "compact":
            configs["cleanup.policy"] = "compact"
        admin.create_topics([NewTopic(
            name=name,
            num_partitions=partitions,
            replication_factor=1,
            topic_configs=configs,
        )])
        print(f"  Topic '{name}' created with {partitions} partitions (policy={cleanup_policy}).")
    except TopicAlreadyExistsError:
        print(f"  Topic '{name}' already exists.")
    finally:
        admin.close()


def get_producer(idempotent=False):
    """
    acks='all': wait for all ISR replicas to acknowledge.
    With a single broker (replication_factor=1), the ISR is just the leader,
    so acks='all' behaves like acks='1' here — but the habit is correct for
    production where replication_factor >= 3 and min.insync.replicas >= 2.
    """
    kwargs = dict(
        bootstrap_servers=BOOTSTRAP,
        key_serializer=lambda k: k.encode() if k else None,
        value_serializer=lambda v: json.dumps(v).encode(),
        acks="all",
    )
    if idempotent:
        kwargs["enable_idempotence"] = True
    return KafkaProducer(**kwargs)


def get_partition_offsets(topic, num_partitions):
    """Return the current log-end offsets for all partitions (the latest written offset)."""
    consumer = KafkaConsumer(bootstrap_servers=BOOTSTRAP)
    tps = [TopicPartition(topic, p) for p in range(num_partitions)]
    consumer.assign(tps)
    consumer.seek_to_end(*tps)
    offsets = {p: consumer.position(TopicPartition(topic, p)) for p in range(num_partitions)}
    consumer.close()
    return offsets


def get_consumer_lag(topic, group_id, num_partitions):
    """
    Compute per-partition consumer lag:
      lag = log_end_offset - committed_offset

    This is what `kafka-consumer-groups.sh --describe` computes internally.
    """
    # End offsets (how far the log has grown)
    end_offsets = get_partition_offsets(topic, num_partitions)

    # Committed offsets for the group
    admin = KafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    try:
        offsets_response = admin.list_consumer_group_offsets(group_id)
    except Exception:
        offsets_response = {}
    finally:
        admin.close()

    lag = {}
    for p in range(num_partitions):
        tp = TopicPartition(topic, p)
        committed = offsets_response.get(tp)
        committed_offset = committed.offset if committed else 0
        lag[p] = max(0, end_offsets[p] - committed_offset)
    return lag, end_offsets


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
  • Within a partition, messages are strictly ordered by offset
  • Each partition is consumed by exactly one consumer per group
  • More consumers than partitions → some consumers are idle
  • Multiple consumer GROUPS each get their own independent copy
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
    • HOT PARTITION risk: if a key has much higher traffic than others
      (e.g., a celebrity user), one partition/consumer bears all the load.
      Solution: add a random suffix to the key for high-traffic entities.
""")

    # ── Phase 2: Consumer group — 3 consumers share partitions ────
    section("Phase 2: Consumer Group — 3 Consumers, 1 Partition Each")

    results = defaultdict(list)

    def consume_worker(consumer_id, group_id, timeout_ms=4000):
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
        t = threading.Thread(
            target=consume_worker,
            args=(cid, "order-processors"),
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

    # ── Phase 3: Live rebalance — shrink group from 3 to 2 ────────
    section("Phase 3: Live Rebalancing — 3 Consumers → 2 Consumers")

    # Produce a fresh batch so consumers have something to consume during the demo
    producer = get_producer()
    for i in range(300):
        key = f"user-{i % 100}"
        producer.send(TOPIC, key=key, value={"seq": 300 + i, "user": key})
    producer.flush()
    producer.close()
    print(f"  Produced 300 more messages to consume during rebalance demo.")

    print(f"""
  Starting 3 consumers in group 'order-rebalance-demo'.
  After 3 seconds, consumer-0 is stopped (simulating a crash).
  Kafka detects the missing heartbeat, triggers a rebalance, and
  redistributes consumer-0's partition to the remaining consumers.
""")

    rebalance_results = defaultdict(dict)
    stop_event_c0 = threading.Event()

    def consume_with_stop(consumer_id, group_id, stop_evt=None, timeout_ms=8000):
        """Consumer that can be stopped early via a threading Event."""
        consumer = KafkaConsumer(
            TOPIC,
            bootstrap_servers=BOOTSTRAP,
            group_id=group_id,
            auto_offset_reset="earliest",
            enable_auto_commit=True,
            consumer_timeout_ms=timeout_ms,
            max_poll_records=20,
            # Shorten session timeout so rebalance is detected quickly in the lab
            session_timeout_ms=6000,
            heartbeat_interval_ms=2000,
        )
        partitions_before = set()
        partitions_after = set()
        msg_count = 0
        stopped_early = False

        for msg in consumer:
            assigned = {tp.partition for tp in consumer.assignment()}
            msg_count += 1
            if stop_evt and stop_evt.is_set():
                # This consumer is "crashing" — close without committing final offsets
                stopped_early = True
                break
            partitions_before.update(assigned)

        if not stopped_early:
            # Record final assignment (may differ from initial after rebalance)
            assigned = {tp.partition for tp in consumer.assignment()}
            partitions_after.update(assigned)

        consumer.close()
        rebalance_results[consumer_id] = {
            "partitions_seen": sorted(partitions_before | partitions_after),
            "messages": msg_count,
            "crashed": stopped_early,
        }

    threads3 = []
    # Consumer 0 will be stopped after 3 seconds
    t0 = threading.Thread(target=consume_with_stop,
                           args=(0, "order-rebalance-demo", stop_event_c0), daemon=True)
    t1 = threading.Thread(target=consume_with_stop,
                           args=(1, "order-rebalance-demo"), daemon=True)
    t2 = threading.Thread(target=consume_with_stop,
                           args=(2, "order-rebalance-demo"), daemon=True)
    threads3 = [t0, t1, t2]

    for t in threads3:
        t.start()

    # Let all 3 consumers get established and process some messages
    time.sleep(3)
    print(f"  [t=3s] Stopping consumer-0 (simulating crash)...")
    stop_event_c0.set()
    t0.join(timeout=5)

    print(f"  [t=3s] Kafka will detect consumer-0's heartbeat timeout (~6s) and rebalance.")
    print(f"  [t=3s] Consumers 1 and 2 continue processing; they will absorb partition from consumer-0.")

    for t in threads3[1:]:
        t.join(timeout=20)

    print(f"\n  Results after rebalance:")
    for cid in sorted(rebalance_results):
        r = rebalance_results[cid]
        status = " ← CRASHED (stopped early)" if r.get("crashed") else ""
        print(f"    Consumer-{cid}: partitions={r['partitions_seen']}  messages={r['messages']}{status}")

    print(f"""
  Rebalance mechanics:
    1. Consumer-0's heartbeats stop (session_timeout_ms = 6s)
    2. Group coordinator declares consumer-0 dead, triggers rebalance
    3. All remaining consumers pause consumption (stop-the-world in eager mode)
    4. Coordinator reassigns consumer-0's partition to consumer-1 or consumer-2
    5. Consumers resume from their last committed offsets

  Eager (classic) rebalance: ALL consumers pause during step 3-5 (~100ms–5s).
  Cooperative (incremental) rebalance (Kafka 2.4+): only the partitions that
  MOVE are revoked. Consumers that keep their partitions never stop.
  Use partition.assignment.strategy=CooperativeStickyAssignor in production.
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
    • Increasing partitions is possible but may temporarily break key routing
      for keys that were already in-flight (the hash changes)
""")

    # ── Phase 5: Ordering guarantee ────────────────────────────────
    section("Phase 5: Ordering Guarantee — Same Key = Same Partition = In Order")

    # Use a dedicated topic to avoid offset pollution from prior phases
    order_topic = "ordering-test-isolated"
    create_topic(order_topic, 3)

    print(f"  Producing 20 sequential events for user-777 (all must land on same partition)...")
    print(f"  Interleaving 20 events for other users to prove isolation.\n")

    producer = get_producer()
    for seq in range(20):
        producer.send(order_topic, key="user-777", value={"seq": seq, "event": f"action-{seq}"})
        # Interleave events for other users to prove partitioning doesn't affect ordering
        producer.send(order_topic, key=f"user-{seq % 50}", value={"seq": seq, "event": "other"})
    producer.flush()
    producer.close()

    consumer = KafkaConsumer(
        order_topic,
        bootstrap_servers=BOOTSTRAP,
        group_id="ordering-checker",
        auto_offset_reset="earliest",
        consumer_timeout_ms=5000,
        # Manual offset management to avoid auto-commit side effects
        enable_auto_commit=False,
    )
    user777_msgs = []
    for msg in consumer:
        val = json.loads(msg.value)
        if val.get("event", "").startswith("action-"):
            user777_msgs.append((msg.partition, msg.offset, val["seq"]))
    consumer.close()

    if user777_msgs:
        partitions_used = set(p for p, _, _ in user777_msgs)
        print(f"  user-777 messages received: {len(user777_msgs)}")
        print(f"  All landed on partition(s): {partitions_used}")
        seqs = [s for _, _, s in user777_msgs]
        print(f"  Sequence numbers in order: {seqs[:10]}...")
        in_order = all(seqs[i] < seqs[i+1] for i in range(len(seqs) - 1))
        print(f"  Monotonically increasing (in order): {in_order}")
        assert len(partitions_used) == 1, "Same key must always go to same partition!"
        assert in_order, "Events for a single key must be strictly ordered within a partition!"
        print(f"\n  CONFIRMED: same key -> single partition -> strict ordering")
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
    • Never use a random UUID as key if you need ordering — each message
      will hash to a different partition with no ordering relationship
""")

    # ── Phase 6: Consumer lag ──────────────────────────────────────
    section("Phase 6: Consumer Lag — Measuring How Far Behind a Consumer Is")

    lag_topic = "lag-demo"
    create_topic(lag_topic, NUM_PARTS)

    print(f"  Producing 500 messages to '{lag_topic}'...")
    producer = get_producer()
    for i in range(500):
        key = f"user-{i % 50}"
        producer.send(lag_topic, key=key, value={"seq": i})
    producer.flush()
    producer.close()

    # Measure lag before any consumer has run
    lag, end_offsets = get_consumer_lag(lag_topic, "lag-demo-group", NUM_PARTS)
    total_lag = sum(lag.values())
    print(f"\n  Log-end offsets (messages produced):  {end_offsets}")
    print(f"  Consumer committed offsets (none yet): all 0")
    print(f"  Lag per partition: {lag}")
    print(f"  Total lag: {total_lag} messages")

    # Consume only half the messages
    print(f"\n  Starting consumer, but stopping it after processing ~250 messages...")
    lag_results = {}

    def partial_consumer():
        consumer = KafkaConsumer(
            lag_topic,
            bootstrap_servers=BOOTSTRAP,
            group_id="lag-demo-group",
            auto_offset_reset="earliest",
            enable_auto_commit=True,
            max_poll_records=25,
            consumer_timeout_ms=8000,
        )
        count = 0
        for msg in consumer:
            count += 1
            if count >= 250:
                break
        # Commit current offsets before closing
        consumer.commit()
        consumer.close()
        lag_results["consumed"] = count

    t = threading.Thread(target=partial_consumer, daemon=True)
    t.start()
    t.join(timeout=20)

    time.sleep(1)  # Allow commits to propagate

    lag_after, end_offsets_after = get_consumer_lag(lag_topic, "lag-demo-group", NUM_PARTS)
    total_lag_after = sum(lag_after.values())
    consumed = lag_results.get("consumed", 0)

    print(f"\n  After consuming ~{consumed} messages:")
    print(f"  Log-end offsets (unchanged): {end_offsets_after}")
    print(f"  Lag per partition: {lag_after}")
    print(f"  Total lag: {total_lag_after} messages")

    print(f"""
  Consumer lag is the primary operational health metric for Kafka consumers.

  lag = log_end_offset - committed_consumer_offset

  What lag tells you:
    • Lag = 0: consumer is keeping up with the producer
    • Lag > 0, stable: consumer is behind but not falling further behind
    • Lag > 0, growing: consumer CANNOT keep up — will eventually lose data
      when the retention window rolls past the unconsumed messages

  How to monitor lag:
    # From the command line (inside the Kafka container):
    kafka-consumer-groups.sh \\
      --bootstrap-server localhost:9092 \\
      --group lag-demo-group \\
      --describe

  Alert on: lag growth rate (lag increasing over time), not absolute lag value.
  A stable lag of 100,000 messages is fine if the retention is 7 days.
  A growing lag of 1,000 messages will cause data loss within hours.
""")

    # ── Phase 7: Dead letter topic (DLT) ──────────────────────────
    section("Phase 7: Dead Letter Topic — Handling Poison-Pill Messages")

    good_topic = "payments"
    dlt_topic  = "payments.DLT"
    create_topic(good_topic, NUM_PARTS)
    create_topic(dlt_topic, 1)

    # Produce a mix of good messages and one deliberately malformed message
    producer = get_producer()
    for i in range(10):
        producer.send(good_topic, key=f"pay-{i}",
                      value={"payment_id": i, "amount": 9.99, "valid": True})
    # Inject a poison-pill message: amount is a string instead of a number
    producer.send(good_topic, key="pay-bad",
                  value={"payment_id": -1, "amount": "INVALID", "valid": False})
    for i in range(10, 20):
        producer.send(good_topic, key=f"pay-{i}",
                      value={"payment_id": i, "amount": 9.99, "valid": True})
    producer.flush()
    producer.close()

    print(f"  Produced 20 good messages + 1 poison-pill to '{good_topic}'.")
    print(f"  Consumer processes each message; on failure, routes to '{dlt_topic}'.\n")

    dlt_producer = get_producer()
    consumer = KafkaConsumer(
        good_topic,
        bootstrap_servers=BOOTSTRAP,
        group_id="payment-processor",
        auto_offset_reset="earliest",
        enable_auto_commit=False,
        consumer_timeout_ms=5000,
    )

    processed = 0
    dead_lettered = 0
    MAX_RETRIES = 3

    for msg in consumer:
        val = json.loads(msg.value)
        retries = 0
        success = False

        while retries < MAX_RETRIES:
            try:
                # Simulate processing: fail on invalid amount
                amount = float(val["amount"])  # raises ValueError for "INVALID"
                # Success path
                success = True
                processed += 1
                break
            except (ValueError, KeyError) as e:
                retries += 1
                if retries < MAX_RETRIES:
                    time.sleep(0.05)  # brief backoff

        if not success:
            # Route poison pill to DLT with error metadata in headers
            dlt_producer.send(
                dlt_topic,
                key=msg.key,
                value=json.dumps({
                    "original_topic": good_topic,
                    "original_partition": msg.partition,
                    "original_offset": msg.offset,
                    "payload": val,
                    "error": f"Failed after {MAX_RETRIES} retries: invalid amount",
                }).encode(),
            )
            dlt_producer.flush()
            dead_lettered += 1
            print(f"    [DLT] payment_id={val.get('payment_id')} routed to {dlt_topic}")

        # Commit offset regardless of success/failure — we handled the message
        consumer.commit()

    consumer.close()
    dlt_producer.close()

    print(f"\n  Results:")
    print(f"    Successfully processed: {processed}")
    print(f"    Dead-lettered:          {dead_lettered}")

    # Confirm DLT consumer sees the poison pill
    dlt_consumer = KafkaConsumer(
        dlt_topic,
        bootstrap_servers=BOOTSTRAP,
        group_id="dlt-inspector",
        auto_offset_reset="earliest",
        consumer_timeout_ms=3000,
    )
    dlt_msgs = [json.loads(m.value) for m in dlt_consumer]
    dlt_consumer.close()

    print(f"    Messages in DLT topic:  {len(dlt_msgs)}")
    if dlt_msgs:
        for m in dlt_msgs:
            print(f"      payment_id={m['payload'].get('payment_id')}  "
                  f"error='{m['error']}'  "
                  f"original={m['original_topic']}[{m['original_partition']}]@{m['original_offset']}")

    print(f"""
  Dead letter topic pattern:
    • Without DLT: a poison pill blocks the partition forever (consumer retries
      the same bad message in a tight loop, making no progress)
    • With DLT: after N retries, route the message to a DLT, commit the offset,
      and continue processing the next message
    • The DLT is monitored separately; bad messages are fixed and reprocessed

  DLT payload should always include:
    - original topic, partition, offset (for auditability)
    - original payload (to reprocess after fix)
    - error details (for triage)
    - timestamp and consumer group (for tracing)

  Spring Kafka and AWS EventBridge both have built-in DLT support.
  For custom consumers, implement this pattern explicitly.
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
    • OS page cache: recently written data served from RAM, not disk
    • 1M messages/sec on commodity hardware is achievable

  Exactly-once semantics (two independent layers):
    Layer 1 — Idempotent producer (enable.idempotence=true):
      • Each message gets a producer epoch + sequence number
      • Broker deduplicates retries within the same producer session
      • Prevents duplicates from producer retries on network errors

    Layer 2 — Transactional API (transactional.id):
      • Producer wraps multiple sends in a transaction
        (beginTransaction / commitTransaction / abortTransaction)
      • Consumer reads only committed messages
        (isolation.level=read_committed)
      • Enables atomic multi-partition, multi-topic writes

    In practice: most systems use at-least-once + idempotent consumers.
    True exactly-once is complex and has throughput trade-offs.

  KRaft (Kafka without Zookeeper — Kafka 3.3+, Confluent 7.3+):
    • Kafka brokers form their own Raft metadata quorum
    • Eliminates Zookeeper as a separate component
    • Leader election: minutes → seconds
    • Max partitions per cluster: 200k → 2M+
    • Use KRaft for all new Kafka deployments

  Next: ../07-stream-processing/
""")


if __name__ == "__main__":
    main()
