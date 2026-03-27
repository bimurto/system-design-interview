# Message Queues — Fundamentals

**Prerequisites:** `../../01-foundations/14-failure-modes-reliability/`
**Next:** `../06-message-queues-kafka/`

---

## Concept

A message queue is a durable buffer that decouples producers from consumers in time and failure. A producer sends a message and moves on — it does not wait for the consumer to process it. The consumer reads from the queue at its own pace, even if it was unavailable when the message was sent. This **temporal decoupling** is the central value of a message queue.

Compare this to a direct synchronous call: if the payment service calls the email service directly to send a receipt, and the email service is down, the payment fails. With a queue between them, the payment service puts "send receipt" into the queue and returns success. The email service picks it up when it recovers. Two systems with different availability profiles are insulated from each other.

### Why Not Just Use a Database?

A relational table can store pending work rows, and a worker can poll for new rows. This pattern is called "transactional outbox" or "job queue on Postgres," and it works at modest scale. The limitations appear at high throughput: polling adds latency (work sits in the table until the next poll); concurrent workers cause row-level lock contention; the table grows until workers clean it up; and the database takes on workloads it wasn't designed for. A purpose-built queue handles millions of messages per second with push delivery (no polling), horizontal consumer scaling, and built-in retention and dead-letter handling.

### Core Guarantees

Message queues offer three delivery guarantee levels:

**At-most-once:** the broker delivers a message at most once. If the consumer crashes before acknowledging, the message is lost. Used for high-volume telemetry and metrics where occasional loss is acceptable and redelivery overhead is not worth it.

**At-least-once:** the broker delivers a message until it receives an acknowledgement. If the consumer crashes before acking, the message is redelivered (possibly multiple times). The consumer must handle duplicates — either by being idempotent (processing the same message twice has the same effect as once) or by deduplicating using the message ID. This is the most common default in production queues.

**Exactly-once:** the message is processed exactly once, even across crashes and retries. This requires coordination between the broker and consumer storage (e.g., Kafka transactions + consumer state in a Kafka Streams store, or two-phase commit). Expensive and complex; use only when duplicates are genuinely unacceptable (financial transactions, inventory decrements).

### Queue Models

**Point-to-point (competing consumers):** one producer, multiple consumers sharing a single queue. Each message is delivered to exactly one consumer. Consumers compete for messages — this is natural work distribution. Adding consumers scales throughput linearly. Used for background job processing, task queues, email sending. Examples: RabbitMQ queues, SQS standard queues, Celery workers.

**Publish-subscribe (fan-out):** one producer publishes to a topic; multiple subscribers each receive a copy of every message independently. Used for event broadcasting — "order placed" event goes to the inventory service, the analytics service, and the notification service simultaneously. Each subscriber gets its own copy. Examples: RabbitMQ exchanges with bindings, SNS topics, Redis Pub/Sub.

**Hybrid (durable pub/sub):** Kafka and similar log-based queues combine both models. Multiple consumer groups can subscribe to the same topic (pub/sub fan-out), but within each consumer group consumers share partitions (point-to-point work distribution). This makes Kafka both a broadcast bus and a work queue simultaneously.

### Dead-Letter Queues

When a consumer consistently fails to process a message (e.g., malformed payload, downstream dependency unavailable, bug in consumer code), the message is redelivered repeatedly, blocking the queue or wasting resources. A dead-letter queue (DLQ) receives messages after a configurable number of failed delivery attempts. This prevents one bad message from blocking the queue indefinitely. Engineers monitor the DLQ and investigate messages there — they represent bugs or data issues that need manual intervention.

Always configure a DLQ for production queues. A queue without a DLQ allows a single poison pill message to infinite-loop and block all consumer throughput.

### Acknowledgement and Visibility Timeout

In most queues (SQS, RabbitMQ), when a consumer receives a message, the message becomes invisible to other consumers for a **visibility timeout** period. If the consumer acknowledges (deletes) the message within the timeout, it's gone. If the consumer crashes or the timeout expires without an ack, the message reappears in the queue for another consumer to retry. This is the mechanism behind at-least-once delivery.

The visibility timeout must be longer than the expected processing time. If processing takes 30 seconds and the visibility timeout is 10 seconds, messages are redelivered while still being processed, causing duplicates. A rule of thumb: set visibility timeout to 6× the average processing time.

The `redelivered` flag set by the broker is a hint, not a guarantee of duplication. A consumer may have processed the message successfully and crashed before sending the ack. From the broker's perspective, these are identical situations — both result in redelivery. Do not use `redelivered=True` as the sole trigger for deduplication logic; always use the message ID.

### Idempotent Consumers

At-least-once delivery is the production default. Duplicates will happen: on consumer crash, on network partition, on broker failover. Your consumer must be designed for this from the start — not added as an afterthought.

Three idempotency strategies in increasing order of complexity:

**Natural idempotency:** Design the operation so that applying it twice produces the same result as applying it once. `UPDATE accounts SET status = 'verified' WHERE id = 123` is naturally idempotent — running it twice doesn't change the outcome. `INSERT INTO emails_sent VALUES (...)` is not.

**Message ID deduplication:** Assign a stable, unique ID to each logical message (e.g., `order_id + event_type`). Before processing, check if the ID exists in a seen-set (Redis SETNX, DB unique constraint). If seen, skip processing but still ack to remove from queue. If not seen, process and add to seen-set atomically. Use a TTL on the seen-set matching your message retention window.

**Transactional outbox / idempotency keys:** For financial operations, wrap message processing and business state update in a single DB transaction. Insert a row into a `processed_messages(msg_id, processed_at)` table with a unique constraint on `msg_id` in the same transaction as the business update. Duplicate processing will hit the unique constraint and roll back cleanly.

### Ordering

Most queues provide best-effort ordering — messages are generally delivered in the order they were sent, but delivery order is not guaranteed under failure conditions (retries, node failures). For strict ordering, you need either a single-consumer queue (no parallelism) or a log-based queue like Kafka where ordering is guaranteed within a partition. Partition your messages by a key so that related messages (e.g., all events for the same user) land in the same partition and are processed in order.

A subtle ordering hazard with at-least-once delivery: if message A is redelivered (while message B has already been delivered and processed), the consumer sees B before A — partial reorder at the application level even when the broker preserves queue order. Design state machines around events, not sequence numbers, to handle this.

### Backpressure

If producers send faster than consumers process, the queue grows unboundedly. Left unchecked, this consumes all broker disk/memory and crashes the system. Solutions: (1) **scale consumers** — add more consumer instances to increase throughput; (2) **producer rate limiting** — slow or reject producers when the queue depth exceeds a threshold; (3) **message TTL** — expire messages after a time limit so stale work doesn't accumulate; (4) **queue depth alerts** — page on-call when depth exceeds normal operating range. A growing queue is an early warning signal of consumer performance degradation.

### Producer Flow Control and Backpressure at the Broker

Backpressure can be enforced at the broker layer, not just by scaling consumers. RabbitMQ uses a **credit-based flow control** mechanism: each connection is given a credit budget; when a queue exceeds its memory/disk high-watermark threshold, the broker suspends publishing connections (they block at the TCP write call) rather than dropping messages or crashing. This is transparent to producers — they appear to slow down naturally.

SQS has no built-in backpressure — it accepts messages indefinitely. Backpressure must be implemented at the application level (reject requests, return 429) or via CloudWatch queue-depth alarms that trigger autoscaling of consumers.

### Message Schema Evolution

In production, producers and consumers are deployed independently. A producer may start sending a new field before consumers are updated. Strategies:

**Backward-compatible schema changes:** add fields with defaults, never remove required fields. Consumers that don't know the field ignore it. This works with JSON but offers no enforcement.

**Schema registry (Avro / Protobuf):** producers serialize with a versioned schema registered centrally (Confluent Schema Registry). Consumers check the schema ID in the message header and deserialize against the registered version. The registry can enforce compatibility modes (backward, forward, full). This is the Kafka ecosystem standard for large-scale deployments.

**Envelope versioning:** include a `schema_version` field in every message. Consumers branch on version to handle both old and new formats during the migration window.

### Trade-offs

| Concern | Message Queue | Direct RPC |
|---------|--------------|-----------|
| Temporal coupling | None — sender doesn't wait | Tight — sender waits for response |
| Throughput | Buffered — handles bursts | Unbuffered — consumer must keep up |
| Latency | Higher (queue hop) | Lower (direct call) |
| Delivery guarantee | Configurable (at-least-once) | At-most-once (no retry by default) |
| Ordering | Approximate (exact with partitioning) | Request order preserved |
| Idempotency required | Yes — duplicates are normal | No — each call is unique |
| Observability | Queue depth, age, DLQ depth | Latency, error rate |
| Schema evolution | Requires coordination / registry | In-process, easier to refactor |
| Complexity | Broker to operate; DLQ to monitor | Simpler dependency graph |

### When to Use a Message Queue

Use a queue when:
- Producer and consumer have different scaling needs (spike in orders shouldn't spike the email service)
- Processing can be async — the user doesn't need to wait for the result
- Multiple downstream systems need to react to the same event
- Work must survive consumer crashes without data loss
- You need to rate-limit work into a slow downstream

Don't use a queue when:
- You need a synchronous response (user is waiting for a search result)
- The system is simple enough that direct calls are more readable
- You need strict global ordering across all messages (queues make this hard)
- The consumer must process in real time with no queuing latency

## Interview Talking Points

- "A message queue decouples producers from consumers in time and failure — the producer doesn't wait; the consumer processes at its own pace"
- "At-least-once is the safe default — messages are redelivered until acked. Consumers must be idempotent or deduplicate by message ID. I'd use Redis SETNX with a TTL matching message retention, or a DB unique constraint on the message ID inside the same transaction as the business write"
- "Always configure a dead-letter queue — without it, one poison pill message loops forever and blocks the entire consumer group. Track retry counts via broker-populated x-death headers, not in-process counters — the counter resets on crash"
- "The visibility timeout must exceed expected processing time — if it's too short, messages are redelivered while still being processed, causing duplicates. Rule of thumb: 6× average processing time"
- "A growing queue depth is a canary — it means consumers can't keep up. Scale consumers first; if that doesn't help, look at consumer throughput per instance (prefetch_count, concurrency) before adding more broker resources"
- "Point-to-point distributes work; pub/sub broadcasts events. Kafka combines both: consumer groups give you pub/sub across groups, competing consumers within a group. The number of consumers that can parallelize is bounded by partition count"
- "At-least-once redelivery can cause out-of-order delivery at the application level even when the broker preserves queue order — a redelivered message A arrives after message B that was delivered after it. Design state transitions to be order-independent where possible"
- "For schema evolution, a schema registry (Avro + Confluent) enforces compatibility between producer and consumer versions at publish time — not at consume time when it's too late"

## Hands-on Lab

**Time:** ~25 minutes
**Services:** RabbitMQ (with management UI on port 15672)

### Setup

```bash
cd system-design-interview/02-advanced/05-message-queues-fundamentals/
docker compose up -d
# Wait ~15 seconds for RabbitMQ to be ready
```

### Experiment

```bash
python experiment.py
```

The script demonstrates: (1) basic produce/consume with acknowledgement; (2) at-least-once delivery — a consumer crashes mid-processing, and the message is redelivered to another consumer; (3) dead-letter queue — retry count tracked via broker-populated x-death headers (crash-safe); (4) competing consumers — 3 consumers share a queue, showing work distribution across instances; (5) pub/sub fan-out — one message published to an exchange, received by two independent subscriber queues; (6) idempotent consumer — deduplication by message ID prevents double-processing of redelivered messages.

You can also open the RabbitMQ management UI at `http://localhost:15672` (guest/guest) to watch queue depths in real time.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **Amazon SQS at scale:** SQS processes trillions of messages per month. Amazon's own internal services use SQS to decouple order processing from fulfillment — the order service publishes to SQS; fulfillment, inventory, and notification services each have their own consumers. SQS's visibility timeout + DLQ pattern handles consumer failures transparently — source: AWS re:Invent, "Amazon SQS Deep Dive" (2019).
- **WhatsApp message queuing:** WhatsApp stores undelivered messages in a per-user queue on their servers. When the recipient's device comes online, it receives all queued messages in order and acknowledges them. The server deletes acknowledged messages. This is at-least-once delivery with client-side deduplication by message ID — source: WhatsApp Engineering Blog, "WhatsApp's Architecture" (2012).
- **Shopify's job queue (Resque/Sidekiq):** Shopify processes millions of background jobs per day — email sending, webhook delivery, report generation — using Redis-backed Sidekiq queues. During Black Friday traffic spikes, their queue depth grows as consumers can't keep up with producers. They scale consumer worker count dynamically based on queue depth — source: Shopify Engineering Blog, "Surviving Black Friday: The Shopify Approach" (2018).

## Common Mistakes

- **No dead-letter queue.** A single malformed message that fails processing loops forever, consuming consumer resources and blocking other messages in the same partition or queue. Always configure a DLQ with a max-delivery-attempts threshold.
- **Visibility timeout shorter than processing time.** The message reappears while still being processed, causing another consumer to pick it up simultaneously. Both consumers process the same message — phantom duplicate work. Set timeout to 6× average processing time.
- **Not handling duplicates.** At-least-once delivery means your consumer will receive duplicates under failure conditions. If you charge a credit card or send an email on every delivery without deduplication, users get double-charged or spammed. Design consumer logic to be idempotent.
- **Synchronous consumer design.** A consumer that processes one message at a time, waits for each to complete, then fetches the next is leaving throughput on the table. Use prefetch/batch size settings and process messages concurrently within each consumer instance.
- **Ignoring queue depth metrics.** Queue depth is the most important operational metric for a message queue. A sustained increase means consumers are falling behind. Without an alert, this goes unnoticed until the broker runs out of disk and crashes.
- **In-process retry counter.** Counting retry attempts in a variable inside the consumer resets on crash. Use broker-provided x-death headers (RabbitMQ) or ApproximateReceiveCount (SQS) to track attempt counts durably across consumer restarts.
- **Nacking duplicates instead of acking them.** When an idempotent consumer detects a duplicate, nacking with requeue=True causes infinite redelivery of the message you intended to discard. Always ack duplicates — the goal is to remove them from the queue, not to retry them.
- **Fanout to unbound queues.** In RabbitMQ, if a subscriber queue is not declared and bound to the exchange before the event is published, the event is silently dropped. Unlike Kafka (where a log retains messages for replay), RabbitMQ fanout only routes to currently bound queues. Late-joining consumers miss historical events.
