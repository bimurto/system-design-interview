# Event-Driven Architecture

**Prerequisites:** `../03-consensus-paxos-raft/`
**Next:** `../05-message-queues-kafka/`

---

## Concept

In a traditional CRUD system, a service handles a request by reading and writing to a database, then returning a response. This is synchronous, request-driven, and tightly coupled: the caller waits for the entire operation to complete before proceeding, and each service must know which other services to call. Event-driven architecture (EDA) inverts this: services communicate by publishing and consuming events — immutable records of things that happened. A service publishes an event when something occurs and moves on; downstream services react to those events independently, in their own time.

An **event** is fundamentally different from a command or a query. A command says "do this" and may be rejected ("place order #123" might fail if the item is out of stock). A query asks "what is the state?" with no side effects. An event says "this happened" — it is an immutable fact about the past that cannot be refused. "Order #123 was placed at 14:32:01" is an event. Events are named in the past tense: `order.created`, `payment.completed`, `user.registered`. This naming convention signals that the event represents something that has already occurred and cannot be undone.

**Event sourcing** takes this idea further: instead of storing the current state of an entity in a database row that gets updated in place, you store the sequence of events that led to the current state. The database row is never updated — only new events are appended. Current state is derived by replaying events from the beginning (or from a recent snapshot). This provides a complete, immutable audit trail; the ability to reconstruct state at any point in time; and the ability to define new read models by replaying the same event history.

**CQRS** (Command Query Responsibility Segregation) is a natural companion to event sourcing. The write side accepts commands and emits events (append-only). The read side maintains one or more projections — queryable views of the data — built by consuming the event stream. These projections can be in different databases (e.g., Elasticsearch for full-text search, Redis for a leaderboard, Postgres for complex queries) and can be optimized independently for their access patterns. If a projection has a bug, delete it and rebuild from the event log.

The coupling implications of EDA are profound. In a synchronous system, Service A calls Service B directly — it must know B's API, it fails if B is down, and it blocks while B processes. In an event-driven system, Service A publishes an event to a topic; B, C, and D consume it independently. A doesn't know who consumes its events, B doesn't know what system produced the event, and neither blocks the other. Services can be added, removed, or replaced without changing the others — as long as the event schema remains compatible.

## How It Works

**Event Sourcing — Write Path:**
1. Client sends a command (e.g., "place order #123") to the command handler
2. Command handler loads current aggregate state by replaying past events from the event store (or from a snapshot + recent events)
3. Handler validates the command against current state; if invalid, returns an error without writing anything
4. Handler creates a new immutable event (e.g., `order.placed {order_id, items, timestamp}`) and appends it to the event store
5. Projection consumers (subscribed to the event store) read the new event and update their read-model databases (Postgres for reports, Redis for leaderboards, Elasticsearch for search)

**Event Sourcing — Read Path:**
1. Client sends a query to the query handler (CQRS read side)
2. Query handler reads from the projection database — not the event store — returning a pre-computed, denormalized view
3. Result is eventually consistent with the event store (lag = time for projections to consume and apply the latest events)

**Event structure:** an event should be self-describing and include all the information needed by consumers without requiring a follow-up query. A minimal event contains: event type, event ID (for deduplication), aggregate ID (the entity it relates to), timestamp, and a payload with event-specific data.

**Event sourcing state machine:**
```
  Append-only event log (Kafka topic / event store):

  offset: 0         1          2           3            4
          ┌──────────┬──────────┬───────────┬────────────┐
  ORD-001 │ created  │  paid    │  shipped  │ delivered  │
          └──────────┴──────────┴───────────┴────────────┘
                                                         ▲
                                              current    │
                                              state is   │
                                              the result │
                                              of replaying
                                              all events │

  Projection (read model):
  {"order_id":"ORD-001", "status":"delivered", "tracking":"TRACK-XYZ"}
```

**CQRS flow:**
```
  Client ──► Command Handler ──► Validates ──► Appends Event ──► Kafka
                                                                     │
  Client ◄── Query Handler  ◄── Read DB   ◄── Projection Consumer ◄─┘
```

**Event schema evolution:** events are immutable once published. Adding new optional fields is backward-compatible (old consumers ignore them). Removing required fields is a breaking change. Renaming fields requires a new event type (e.g., `order.created.v2`) or an **upcaster** — a function that transforms old event payloads to the new shape on read, so the projection code doesn't need to handle both versions. For strict schema contracts across teams, use Apache Avro or Protobuf with a Schema Registry — the registry enforces compatibility rules (`BACKWARD`, `FORWARD`, `FULL`) and prevents a producer from deploying a breaking schema change without all consumers being ready.

**The dual-write problem:** the most insidious failure mode in EDA. A service that writes to a database AND publishes an event in two separate operations has no atomicity guarantee. If the process crashes after the DB write but before the Kafka publish, the state is updated but the event is never emitted — downstream services never learn of the change. Conversely, if Kafka is flushed before the DB commit rolls back, you've emitted an event for a state that doesn't exist.

**Transactional Outbox pattern** solves this atomically: instead of publishing to Kafka directly, the service writes both its business entity update AND a new row in an `outbox` table in the same local database transaction. A separate relay process (e.g., Debezium using Change Data Capture) tails the `outbox` table and publishes events to Kafka. If the relay crashes, it retries — the outbox row persists until the event is confirmed published. This gives you atomicity between the DB write and the event publish without distributed transactions.

```
  Without outbox (dual-write risk):
    Service ──► DB commit ──► [crash here] ──► Kafka publish (never happens)

  With Transactional Outbox:
    Service ──► BEGIN TX ──► UPDATE orders SET ... ──► INSERT INTO outbox ──► COMMIT
                                                              │
    Debezium (CDC) ──────────────────────────────────────────►│ tails outbox table
                                                              ▼
                                                         Kafka publish
                                                         (at-least-once; outbox row deleted)
```

**Saga pattern:** long-running business transactions that span multiple services cannot use a single database transaction. A saga is a sequence of local transactions, each publishing an event that triggers the next step. If a step fails, the saga executes **compensating transactions** — each step has a defined undo operation.

```
  Order saga (choreography):
    1. OrderService       → order.created        → InventoryService reserves stock
    2. InventoryService   → inventory.reserved   → PaymentService charges card
    3. PaymentService     → payment.failed       → InventoryService releases stock (compensate)
                                                  → OrderService cancels order (compensate)
```

If payment fails, compensating events flow backwards to undo the inventory reservation and cancel the order. No distributed lock needed — each service handles its own local rollback.

**Choreography:**
```
  inventory-service  publishes → inventory.reserved
  payment-service    consumes ←  inventory.reserved
                     publishes → payment.completed
  shipping-service   consumes ←  payment.completed
                     publishes → shipment.created
```

**Orchestration:**
```
  saga-orchestrator  sends → RESERVE_INVENTORY → inventory-service
  saga-orchestrator  sends → CHARGE_PAYMENT    → payment-service
  saga-orchestrator  sends → CREATE_SHIPMENT   → shipping-service
```

### Trade-offs

| Aspect | Event-Driven | Request-Driven (REST/gRPC) |
|---|---|---|
| Coupling | Loose (services know events, not each other) | Tight (caller knows callee's API) |
| Availability | High (producer doesn't wait for consumer) | Lower (call fails if downstream is down) |
| Consistency | Eventual | Can be synchronous/strong |
| Debuggability | Hard (distributed trace across many services) | Easy (request → response, simple trace) |
| Ordering | Guaranteed **within a partition** only; no global ordering across partitions | N/A |
| Latency | Higher (async) | Lower for simple request-response |
| Replay | Yes (reprocess historical events) | No |

### Failure Modes

**Poison pill events:** a malformed event that causes the consumer to crash repeatedly, blocking the consumption of all subsequent events in the partition. Mitigation: implement a dead-letter topic (DLT) where events that fail processing N times are routed, with alerting, so the bad event doesn't block the queue indefinitely. Critically, the consumer must commit the offset of the bad event before routing to DLT — otherwise it will loop forever.

**Consumer falling behind (lag buildup):** a slow consumer accumulates lag, and if Kafka's retention period is shorter than the time needed to catch up, the consumer will miss events permanently. Mitigation: monitor consumer lag continuously (lag AND lag growth rate); scale out consumer instances (up to the partition count limit); increase retention period; use Kafka's log compaction for state-based topics to keep the log bounded.

**Event schema mismatch:** a producer publishes events in a new format that old consumers can't deserialize, causing crashes. Mitigation: use a Schema Registry with compatibility checks (e.g., `BACKWARD` mode ensures new schemas can read old messages). Never deploy a schema-breaking change without coordinating with all consumers.

**The dual-write problem:** a service writes to its database and publishes an event in two separate, non-atomic operations. A crash between the two leaves the system inconsistent — state changed but no event emitted, or event emitted for a state that was never persisted. Mitigation: use the Transactional Outbox pattern (write to DB and outbox table in one transaction; a separate relay publishes from outbox to Kafka).

**Projection drift:** a bug in the projection consumer produces incorrect read model state for months before being discovered. Because the event log is immutable and the bug is in the consumer code, you cannot "undo" the bad projections. Mitigation: fix the consumer code and replay the entire event log from offset 0 to rebuild the projection from scratch. This is the event sourcing superpower — the event log is the ground truth, not the derived projection.

## Interview Talking Points

- "In event-driven architecture, services communicate via events — immutable facts about the past. A service publishes events and doesn't know who consumes them. This decouples producers from consumers in time, space, and failure."
- "Event sourcing stores the history of events as the source of truth, not the current state. Current state is derived by replaying events. This gives you a complete audit log, time-travel queries, and the ability to rebuild projections from scratch."
- "CQRS separates the write model (commands/events) from the read model (projections). Each read model can be in a different database, optimized for its query pattern. Elasticsearch for search, Redis for leaderboards, Postgres for reports. Rebuilding a broken projection is as simple as deleting it and replaying the event log."
- "Choreography vs orchestration: choreography has services react to events independently — loose coupling, hard to observe. Orchestration has a central saga that directs each step — easier to observe, but introduces a central coupling point. I default to choreography for simple flows and orchestration when I need explicit failure handling and compensation logic."
- "At-least-once delivery means events may be delivered more than once. Consumers must be idempotent — processing the same event twice must produce the same result. The practical implementation is a unique constraint on event_id in the projection's database — INSERT ... ON CONFLICT DO NOTHING."
- "The dual-write problem is the most common production failure in EDA: if you write to your database and publish to Kafka in two separate operations, a crash between them leaves the system inconsistent. The fix is the Transactional Outbox pattern — write both to the same database transaction, then a CDC relay like Debezium publishes from the outbox to Kafka."
- "Ordering in Kafka is guaranteed only within a single partition. For global ordering across partitions, you'd need a single partition — which kills throughput — or application-level sequencing. The typical solution is to use the aggregate ID (e.g., order_id) as the partition key, so all events for one aggregate are ordered, which is the only ordering that matters for state reconstruction."
- "The hardest part of EDA is schema evolution. Events are immutable once published — you can never change historical events. Design events to be backward-compatible: add optional fields, never remove required ones. For breaking changes, introduce a new event type (order.created.v2) or use an upcaster in the consumer."

## Hands-on Lab

**Time:** ~25-35 minutes
**Services:** Zookeeper (2181), Kafka (9092)

### Setup

```bash
cd system-design-interview/02-advanced/04-event-driven-architecture/
docker compose up -d
# Wait ~30 seconds for Kafka to be healthy
```

### Experiment

```bash
pip install kafka-python-ng
python experiment.py
```

The script runs five phases:
1. Publishes the full lifecycle of 5 orders (created → paid → shipped → delivered, with one cancellation)
2. Consumes all events and rebuilds current state via a CQRS projection
3. Produces 5 more events while a consumer is "offline" and measures real consumer lag (committed offset vs. log end offset)
4. Simulates at-least-once delivery by re-feeding events twice, then shows how an idempotency check using event_id eliminates duplicates
5. Replays all events from offset 0 with a fresh consumer group to show full state reconstruction

### Break It

```bash
# Simulate a poison pill event
python -c "
from kafka import KafkaProducer, KafkaConsumer
import json

# Produce a malformed event (missing required fields)
producer = KafkaProducer(
    bootstrap_servers='localhost:9092',
    value_serializer=lambda v: v if isinstance(v, bytes) else str(v).encode()
)
producer.send('order-events', value=b'THIS IS NOT JSON {{{')
producer.flush()
print('Produced malformed event (poison pill)')

# Try to consume it
consumer = KafkaConsumer(
    'order-events',
    bootstrap_servers='localhost:9092',
    group_id='poison-pill-test',
    auto_offset_reset='latest',
    consumer_timeout_ms=3000,
)
for msg in consumer:
    try:
        data = json.loads(msg.value)
        print(f'Consumed: {data.get(\"event_type\", \"unknown\")}')
    except json.JSONDecodeError as e:
        print(f'POISON PILL at offset {msg.offset}: {e}')
        print('In production: route to dead-letter topic and alert!')
consumer.close()
"
```

### Observe

The state rebuild in Phase 2 shows that ORD-0003 ends up in `cancelled` status even though it was previously `delivered` — the last event wins in the projection. Phase 3 consumer lag shows the difference between committed offset and log end offset; the lag is exactly the number of events produced while the consumer was offline. Phase 4 shows that without an idempotency check, duplicate events inflate the event chain; with the check, duplicates are silently discarded and the state is identical. Phase 5 replay produces the exact same final state as Phase 2 — this is the core guarantee of event sourcing.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **LinkedIn's event streaming:** LinkedIn publishes hundreds of billions of events per day through Kafka, including user activity events, infrastructure metrics, and data pipeline events. Their "unified log" architecture (described by Jay Kreps in "The Log: What every software engineer should know about real-time data's unifying abstraction," 2013) treats the log as the integration point for the entire data ecosystem — all services publish to and consume from the same log.
- **Axon Framework / event sourcing in banking:** many banking and insurance companies use event sourcing for their core systems. The immutable audit trail is not just a design choice but a regulatory requirement. Axon Framework (Java) provides an event sourcing framework used in financial services. Source: Axon documentation, "Event Sourcing," 2023.
- **Netflix event-driven microservices:** Netflix uses an event-driven architecture for its content delivery pipeline. When a video is uploaded, an `upload.completed` event triggers a fan-out to encoding services (each resolution/codec is a separate consumer), metadata extraction, DRM processing, and CDN distribution — all in parallel, all decoupled. Source: Netflix Tech Blog, "Scaling Event Sourcing for Netflix Downloads," 2017.

## Common Mistakes

- **The dual-write problem (most dangerous).** Writing to a database and publishing to Kafka in two separate operations has no atomicity. A crash or network error between the two leaves state and events inconsistent. This is not a theoretical edge case — in any system with decent traffic, processes crash, and eventually you will hit this. Use the Transactional Outbox pattern: write both to the same DB transaction, have a relay (Debezium/CDC) publish from the outbox to Kafka.
- **Making events too coarse-grained.** An `order.updated` event with the full new order state is not an event — it's a snapshot. Events should describe what specifically changed: `order.item_added`, `order.shipping_address_changed`. Coarse events lose the semantic meaning of what happened and make it harder for consumers to react appropriately.
- **Putting commands in the event log.** Publishing `create_order` as an event conflates commands (which may fail) with events (which represent completed facts). If you publish a command and it fails, you've polluted the event log with a non-event. Separate command processing from event publishing.
- **Ignoring idempotency.** At-least-once delivery is the Kafka default. If your consumer doesn't handle duplicate events, you'll double-process: double-charges, double-emails, double-inventory-deductions. Use the event_id as a deduplication key stored in a database unique constraint or Redis set.
- **Using Kafka as a traditional message queue.** Kafka retains all messages for a configurable period, regardless of consumption. Deleting a message after consumption is not how Kafka works. If you need traditional queue semantics (process-once, auto-delete), use RabbitMQ or SQS instead.
- **Forgetting snapshot strategy for long-lived aggregates.** An aggregate that has existed for years may have thousands of events. Replaying all of them on every command is O(n) and gets worse over time. In production, persist a snapshot of the aggregate state every N events and replay only from the last snapshot. Snapshots are an optimisation, not a source of truth — the event log remains the ground truth.
