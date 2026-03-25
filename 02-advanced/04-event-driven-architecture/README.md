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

**Event schema evolution:** events are immutable once published. Adding new optional fields is backward-compatible (old consumers ignore them). Removing required fields is a breaking change. Renaming fields requires a new event type. For strict schema contracts, use Apache Avro or Protobuf with a Schema Registry — the registry enforces compatibility rules and allows consumers to evolve independently.

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
| Ordering | Guaranteed within partition | N/A |
| Latency | Higher (async) | Lower for simple request-response |
| Replay | Yes (reprocess historical events) | No |

### Failure Modes

**Poison pill events:** a malformed event that causes the consumer to crash repeatedly, blocking the consumption of all subsequent events in the partition. Mitigation: implement a dead-letter topic (DLT) where events that fail processing N times are routed, with alerting, so the bad event doesn't block the queue indefinitely.

**Consumer falling behind (lag buildup):** a slow consumer accumulates lag, and if Kafka's retention period is shorter than the time needed to catch up, the consumer will miss events. Mitigation: monitor consumer lag continuously; scale out consumer instances; increase retention period; use Kafka's log compaction for state-based topics.

**Event schema mismatch:** a producer publishes events in a new format that old consumers can't deserialize, causing crashes. Mitigation: use a Schema Registry with compatibility checks (e.g., `BACKWARD` mode, which ensures new schemas can read old messages). Never deploy a schema-breaking change without coordinating with all consumers.

## Interview Talking Points

- "In event-driven architecture, services communicate via events — immutable facts about the past. A service publishes events and doesn't know who consumes them. This decouples producers from consumers in time, space, and failure."
- "Event sourcing stores the history of events as the source of truth, not the current state. Current state is derived by replaying events. This gives you a complete audit log, time-travel queries, and the ability to rebuild projections."
- "CQRS separates the write model (commands/events) from the read model (projections). Each read model can be in a different database, optimized for its query pattern. Elasticsearch for search, Redis for leaderboards, Postgres for reports."
- "Choreography vs orchestration: choreography has services react to events independently — loose coupling, hard to observe. Orchestration has a central saga that directs each step — easier to observe, but introduces a central coupling point."
- "At-least-once delivery means events may be delivered more than once. Consumers must be idempotent — processing the same event twice must produce the same result. Use the event_id as a deduplication key."
- "The hardest part of EDA is schema evolution. Events are immutable once published — you can't change historical events. All schema changes must be backward-compatible, or you need a schema migration strategy."

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

The script publishes the full lifecycle of 5 orders (created → paid → shipped → delivered, with one cancellation), then consumes all events and rebuilds current state by replaying them. It then simulates a stopped consumer, produces 5 more events, and shows the consumer lag. Finally, it replays all events from offset 0 with a fresh consumer group to show full state reconstruction.

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

The state rebuild in Phase 2 shows that ORD-0003 ends up in `cancelled` status even though it was previously `shipped` — the last event wins. The consumer lag section shows that Kafka retains all events regardless of whether they've been consumed. Phase 4 replay produces the exact same final state as Phase 2 — this is the core guarantee of event sourcing.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **LinkedIn's event streaming:** LinkedIn publishes hundreds of billions of events per day through Kafka, including user activity events, infrastructure metrics, and data pipeline events. Their "unified log" architecture (described by Jay Kreps in "The Log: What every software engineer should know about real-time data's unifying abstraction," 2013) treats the log as the integration point for the entire data ecosystem — all services publish to and consume from the same log.
- **Axon Framework / event sourcing in banking:** many banking and insurance companies use event sourcing for their core systems. The immutable audit trail is not just a design choice but a regulatory requirement. Axon Framework (Java) provides an event sourcing framework used in financial services. Source: Axon documentation, "Event Sourcing," 2023.
- **Netflix event-driven microservices:** Netflix uses an event-driven architecture for its content delivery pipeline. When a video is uploaded, an `upload.completed` event triggers a fan-out to encoding services (each resolution/codec is a separate consumer), metadata extraction, DRM processing, and CDN distribution — all in parallel, all decoupled. Source: Netflix Tech Blog, "Scaling Event Sourcing for Netflix Downloads," 2017.

## Common Mistakes

- **Making events too coarse-grained.** An `order.updated` event with the full new order state is not an event — it's a snapshot. Events should describe what specifically changed: `order.item_added`, `order.shipping_address_changed`. Coarse events lose the semantic meaning of what happened and make it harder for consumers to react appropriately.
- **Putting commands in the event log.** Publishing `create_order` as an event conflates commands (which may fail) with events (which represent completed facts). If you publish a command and it fails, you've polluted the event log with a non-event. Separate command processing from event publishing.
- **Ignoring idempotency.** At-least-once delivery is the Kafka default. If your consumer doesn't handle duplicate events, you'll double-process: double-charges, double-emails, double-inventory-deductions. Use the event_id as a deduplication key in a database or Redis set.
- **Using Kafka as a traditional message queue.** Kafka retains all messages for a configurable period, regardless of consumption. Deleting a message after consumption is not how Kafka works. If you need traditional queue semantics (process-once, auto-delete), use RabbitMQ or SQS instead.
