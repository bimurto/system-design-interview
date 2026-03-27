# Message Queues & Kafka

**Prerequisites:** `../05-message-queues-fundamentals/`
**Next:** `../07-stream-processing/`

---

## Concept

Message queues decouple producers from consumers in time and failure: a producer can send a message even when the consumer is down, and the queue persists the message until the consumer recovers. This pattern underpins virtually every high-scale system — payment processors, notification systems, data pipelines, and microservice choreography all rely on reliable message passing. The fundamental guarantee of a message queue is delivery: a message placed in the queue will eventually be delivered to a consumer, even across failures.

Traditional message queues (RabbitMQ, ActiveMQ, SQS) treat messages like tasks: once a consumer acknowledges receipt, the message is deleted. This is the "queue" model — first in, first out, consumed once. It works well for work dispatch (processing images, sending emails, running background jobs), but it has a critical limitation: once a message is consumed, it's gone. There's no way to replay it, no way to add a second consumer to see the same messages, and no audit trail.

Apache Kafka takes a fundamentally different approach. Rather than a queue, Kafka is a **distributed, partitioned, replicated commit log**. Messages are appended to the end of a topic partition and retained for a configurable period (days or weeks), regardless of whether they've been consumed. Consumers maintain their own offset — a pointer into the log — and can read messages at any pace, replay from any offset, and be added or removed without affecting other consumers. Multiple consumer groups can each independently consume the same topic with their own offsets.

The partition is Kafka's unit of parallelism. Each topic has N partitions, and each partition is an independent ordered log. Producers route messages to partitions via a key (hash-based) or round-robin. Within a partition, messages are strictly ordered by offset. Across partitions, there is no ordering guarantee. This means Kafka provides per-key ordering (all messages with the same key go to the same partition) but not global ordering.

Consumer groups provide horizontal scalability for consumption. Each consumer in a group is assigned a subset of partitions — typically one partition per consumer for maximum parallelism. Adding more consumers scales consumption throughput up to the number of partitions. If you have 12 partitions and start with 3 consumers, each handles 4 partitions. Scale to 12 consumers and each handles 1. Add a 13th and it sits idle — you can't have more active consumers than partitions.

## How It Works

**Kafka Produce-to-Consume Pipeline:**
1. Producer serializes the message and computes the target partition: `hash(message_key) % num_partitions` (or round-robin if no key is set)
2. Producer batches messages in memory (for throughput) and sends the batch to the partition's **leader broker**
3. Leader broker appends the batch to the partition's commit log on disk — a sequential write to the end of a segment file
4. Follower replicas in the ISR (In-Sync Replicas) set pull the new messages from the leader and append to their own logs
5. Once all ISR replicas acknowledge receipt, the messages are **committed** (visible to consumers); the leader updates the high-water mark
6. Consumer polls the partition leader for new messages, receiving a batch starting from its last committed offset
7. Consumer processes each message and commits its new offset — either automatically (at-least-once risk) or manually after processing (preferred for exactly-once guarantee)

**Topic anatomy:**
```
  Topic "orders" with 3 partitions, replication factor 2:

  Partition 0: [msg0, msg3, msg6, msg9, ...]  Leader: Broker-1, Replica: Broker-2
  Partition 1: [msg1, msg4, msg7, msg10, ...] Leader: Broker-2, Replica: Broker-3
  Partition 2: [msg2, msg5, msg8, msg11, ...] Leader: Broker-3, Replica: Broker-1

  Each message has:
    offset   — monotonically increasing integer within the partition
    key      — optional; determines partition routing
    value    — the payload (bytes)
    timestamp — producer or broker timestamp
    headers  — optional key-value metadata
```

**Consumer group offset tracking:**
```
  Partition 0 (10 messages):  [0][1][2][3][4][5][6][7][8][9]
  group-A committed offset:                         ↑ 6 (consumer read up to 6)
  group-B committed offset:            ↑ 3
  group-C committed offset:   ↑ 0 (hasn't started yet)

  All three groups independently track their position.
  Producing a new message (offset 10) doesn't affect any group's offset.
```

**Throughput design — why Kafka is fast:**
1. **Sequential disk writes:** Kafka never modifies existing data. All writes are appends to the end of segment files. Sequential I/O is 100-1000x faster than random I/O on spinning disks and saturates SSDs.
2. **Zero-copy reads:** When a consumer reads, Kafka uses Linux `sendfile(2)` to transfer data from the page cache directly to the network socket without copying through user space.
3. **Batching:** producers buffer messages in memory and send them as batches, amortizing network round-trips. Consumers also fetch in batches.
4. **Page cache:** Kafka relies on the OS page cache rather than a JVM heap. On a modern server with 32GB RAM, the page cache effectively makes reads from "disk" as fast as reads from memory for recently written data.

**Replication:**
- Each partition has one leader and N-1 followers (replicas).
- All reads and writes go through the leader.
- Followers asynchronously replicate from the leader.
- ISR (In-Sync Replicas): the set of replicas that are fully caught up. When `acks=all`, the producer waits for all ISR replicas to acknowledge.
- If the leader fails, the Kafka controller elects a new leader from the ISR set.

### Trade-offs

| Feature | Kafka | RabbitMQ | AWS SQS | AWS SNS |
|---|---|---|---|---|
| Message model | Pull (log) | Push (queue) | Pull (queue) | Push (pub/sub) |
| Retention after consume | Yes (configurable) | No (deleted) | No (deleted) | No |
| Replay | Yes (seek to any offset) | No | No | No |
| Throughput | 1M+ msg/s | ~50k msg/s | ~10k msg/s | High |
| Ordering | Per-partition | Per-queue | No (FIFO queue: yes) | No |
| Consumer model | Consumer groups | Competing consumers | Competing consumers | Push to subscribers |
| Complexity | High (ops-heavy) | Medium | Low (managed) | Low (managed) |
| Best for | Event streaming, data pipelines | Task queues, RPC-style | Simple task queues | Fan-out notifications |

### Failure Modes

**Consumer lag spiraling:** if a consumer falls behind the produce rate (due to slow processing, a bug, or resource constraints), lag accumulates. If lag grows faster than the consumer can catch up, it will never recover without scaling out. Eventually the broker may purge old data (log retention), causing the consumer to miss messages.

**Rebalance storm:** in high-throughput consumer groups, consumer heartbeats may time out during a long GC pause (Java consumers) or a slow poll loop, triggering a rebalance. If rebalances are frequent, consumers spend more time paused during rebalancing than processing messages. Mitigation: tune `session.timeout.ms`, `max.poll.interval.ms`, and `max.poll.records`; use Kafka 2.4+ cooperative rebalancing.

**Log compaction race:** for compacted topics (used for change-data capture), the compaction lag (time between original write and compaction) can be large. A consumer that reads rarely may see many old versions of a key before the compacted "latest" version. Compaction also requires careful configuration of `min.cleanable.dirty.ratio` and `segment.ms` to run frequently enough.

## Interview Talking Points

- "Kafka is a distributed commit log, not a traditional queue. Messages are retained after consumption, consumers track their own offsets, and multiple consumer groups independently read the same topic. This is fundamentally different from RabbitMQ or SQS."
- "Partition count determines max parallelism. With 12 partitions, you can have at most 12 active consumers in a group. More consumers than partitions means idle consumers. Choose partition count at topic creation — decreasing partitions later breaks key-based ordering."
- "Kafka achieves high throughput through sequential writes (append-only log), zero-copy sendfile, batching, and OS page cache. 1 million messages/second is achievable on commodity hardware."
- "Exactly-once semantics require both an idempotent producer (deduplicates retries) and transactional API (atomic multi-partition writes). Most systems use at-least-once with idempotent consumers (deduplicate by message key/offset)."
- "Consumer group rebalancing is Kafka's Achilles' heel at scale: during a rebalance, all consumers in the group pause. Cooperative (incremental) rebalancing in Kafka 2.4+ reduces this by only reassigning the moved partitions."
- "Log compaction is an alternative to time-based retention: instead of deleting old messages, compact the log to keep only the last message for each key. Useful for materializing a changelog into a key-value store."

## Hands-on Lab

**Time:** ~25-35 minutes
**Services:** Zookeeper (2181), Kafka (9092)

### Setup

```bash
cd system-design-interview/02-advanced/05-message-queues-kafka/
docker compose up -d
# Wait ~30 seconds for Kafka to be ready
```

### Experiment

```bash
pip install kafka-python-ng
python experiment.py
```

The script creates a 3-partition topic, produces 300 messages with user-id keys showing even distribution, starts 3 consumers showing 1-partition-each assignment, then 2 consumers showing rebalancing, then 4 consumers showing one idle consumer, and finally proves ordering by showing all events for a single key arrive in order on a single partition.

### Break It

```bash
# Show what happens with no key (round-robin) — no ordering
python -c "
from kafka import KafkaProducer, KafkaConsumer
import json, time

producer = KafkaProducer(
    bootstrap_servers='localhost:9092',
    value_serializer=lambda v: json.dumps(v).encode()
)

# Send 30 messages for 'user-999' with NO key (round-robin)
for i in range(30):
    producer.send('orders', key=None, value={'seq': i, 'user': 'user-999'})
producer.flush()
producer.close()
print('Sent 30 messages with key=None (round-robin, no ordering)')

time.sleep(1)

# Now read them back — they will NOT be in order across partitions
consumer = KafkaConsumer(
    'orders',
    bootstrap_servers='localhost:9092',
    group_id='order-test-nokey',
    auto_offset_reset='latest',
    consumer_timeout_ms=5000,
)
msgs = []
for msg in consumer:
    val = json.loads(msg.value)
    if val.get('user') == 'user-999':
        msgs.append((msg.partition, msg.offset, val['seq']))
consumer.close()

print(f'Received {len(msgs)} messages:')
for p, o, s in msgs[:15]:
    print(f'  partition={p} offset={o} seq={s}')
parts = set(p for p, _, _ in msgs)
print(f'Spread across {len(parts)} partition(s) — ordering not guaranteed!')
"
```

### Observe

Phase 1 shows roughly equal distribution (100 messages per partition), confirming that hash-based routing distributes keys evenly. Phase 4 shows clearly that one consumer receives zero messages and zero partitions — it's completely idle. Phase 5 confirms all 20 messages for `user-777` land on the same partition in sequence order 0-19.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **LinkedIn's Kafka origin:** Kafka was created at LinkedIn in 2011 to handle activity stream data (page views, search queries, clicks) that traditional databases couldn't absorb. LinkedIn processes over 7 trillion messages per day through Kafka clusters. Source: Kreps, Narkhede, Rao, "Kafka: A Distributed Messaging System for Log Processing," NetDB 2011.
- **Uber's real-time data pipeline:** Uber processes 1 trillion events per day through Kafka. They use Kafka for ride event streams, driver location updates, and fraud detection. Their "uReplicator" system replicates Kafka topics across data centers for disaster recovery. Source: Uber Engineering Blog, "Introducing uReplicator," 2018.
- **Cloudflare's logging pipeline:** Kafka serves as the backbone for Cloudflare's logging pipeline, ingesting HTTP request logs from 200+ data centers worldwide. Each PoP produces to a local Kafka cluster; a Kafka MirrorMaker replicates to a central cluster for analytics. Source: Cloudflare Blog, "Logs without Borders," 2020.

## Common Mistakes

- **Using Kafka as a traditional task queue.** Kafka's consumer-group model means that once a message is consumed by a group, it's marked at an offset — but it's still in the log. If you want "process-once-and-delete" semantics, use RabbitMQ or SQS. Kafka shines when multiple consumers need the same data, when replay is needed, or when data volume is high.
- **Not planning partition count.** You cannot decrease partitions without recreating the topic. If you start with 3 partitions and later need 30 consumers for high throughput, you're stuck unless you recreate the topic (which requires data migration). Plan partition count for your expected consumer scale.
- **Treating consumer group rebalancing as free.** Every time a consumer joins or leaves the group, all consumers pause. In a 100-consumer group, a single consumer dying causes all 99 others to pause while partitions are reassigned. Use Kafka 2.4+ cooperative rebalancing and tune session timeouts to minimize impact.
- **Ignoring ISR configuration.** `acks=1` (default) means the producer only waits for the leader to write — a leader failure before replication means data loss. `acks=all` (or `-1`) waits for all ISR replicas. For critical data, always use `acks=all` and `min.insync.replicas=2`.
