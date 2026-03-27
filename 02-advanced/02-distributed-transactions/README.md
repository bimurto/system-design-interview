# Distributed Transactions

**Prerequisites:** `../01-consistent-hashing/`
**Next:** `../03-consensus-paxos-raft/`

---

## Concept

In a single database, transactions are ACID: atomic, consistent, isolated, and durable. The database engine ensures that either all operations in a transaction commit together or none do. But in a modern distributed system — microservices with separate databases, polyglot persistence, services spanning geographic regions — there is no single database engine to provide this guarantee. When an e-commerce checkout must atomically deduct inventory, charge the customer, and create a shipment record across three separate services, you have a distributed transaction problem.

The core challenge is the combination of partial failures and communication delays. In a single machine, if a process crashes mid-transaction, the database recovers to a consistent state using its write-ahead log. In a distributed system, a coordinator sending "COMMIT" to two services might succeed for one and fail for the other because the network partitioned between the two sends. Now one service has committed and the other hasn't — and there's no automatic recovery. This is the fundamental impossibility that makes distributed transactions hard.

Two-Phase Commit (2PC) is the classical solution. A coordinator service orchestrates all participants through two phases: Prepare (vote YES/NO after durably logging intent) and Commit (if all voted YES) or Abort (if any voted NO). 2PC guarantees atomicity as long as the coordinator doesn't crash between phases. The catch is that 2PC is a **blocking protocol**: if the coordinator crashes after collecting YES votes but before sending COMMIT, all participants are stuck waiting indefinitely — they hold write locks and cannot proceed unilaterally. The system is frozen until the coordinator recovers.

The Saga pattern takes the opposite approach: abandon distributed atomicity altogether, and instead decompose the transaction into a sequence of local transactions, each with a corresponding **compensating transaction** that undoes its effect. A saga for "place order" might be: (1) deduct inventory, (2) charge customer, (3) create shipment. If step 3 fails, run compensations in reverse: refund customer, restore inventory. There is no distributed lock — each step commits immediately and durably. The system is eventually consistent, not atomically consistent: between steps, the world is in an intermediate state that other services can observe.

The choice between 2PC and Saga comes down to what kind of consistency you can tolerate. Financial systems processing wire transfers typically require 2PC or equivalent (most major databases use XA, the industry-standard 2PC protocol). E-commerce order flows, user registration pipelines, and most microservice workflows can tolerate the eventual consistency of Sagas — and Sagas are dramatically simpler to scale because they don't require a distributed lock manager.

## How It Works

**2PC — Phase 1 (Prepare):** The coordinator sends a PREPARE message to each participant. Each participant:
1. Writes a durable "prepare" log entry (so it can recover after a crash).
2. Executes the transaction up to but not including commit.
3. Acquires all needed locks.
4. Responds YES (ready to commit) or NO (cannot commit).

**2PC — Phase 2 (Commit or Abort):** If all voted YES:
1. Coordinator writes "commit" durably to its own log.
2. Coordinator sends COMMIT to all participants.
3. Participants commit, release locks, acknowledge.

If any voted NO, coordinator sends ABORT to all. Participants that voted YES roll back.

```
  Coordinator        Participant A      Participant B
      |                   |                  |
      |---- PREPARE ----->|                  |
      |---- PREPARE ----------------------->|
      |<--- YES ----------|                  |
      |<--- YES -----------------------------|
      |                   |                  |
      [coordinator writes "commit" to its own durable log]
      |                   |                  |
      |---- COMMIT ------>|                  |   ← if coordinator crashes HERE
      |---- COMMIT ----------------------->|       Participant A already committed
      |                   |                  |       Participant B is stuck waiting!
      |                   |                  |       (holds write locks indefinitely)
```

The critical window: after the coordinator writes "commit" to its log but before it has sent COMMIT to all participants. Any participant that has not yet received COMMIT is stuck — it cannot commit (only the coordinator decides that) and cannot abort (it voted YES, so aborting would violate the protocol if others committed).

**PostgreSQL XA:** PostgreSQL supports 2PC natively via `PREPARE TRANSACTION 'name'` and `COMMIT PREPARED 'name'`. The prepared transaction is durable — it survives a server crash — and is visible in `pg_prepared_xacts`. This is used by XA-compliant drivers (JDBC, ODBC) for distributed transactions.

**Saga choreography:** Each service listens for events and publishes events. No central coordinator. Example:
- `inventory-service` receives `order.requested` → deducts stock → publishes `inventory.reserved`
- `payment-service` receives `inventory.reserved` → charges card → publishes `payment.completed`
- `shipping-service` receives `payment.completed` → creates shipment → publishes `order.confirmed`

Failure path: if `payment-service` receives `inventory.reserved` but the charge fails, it publishes `payment.failed`. The `inventory-service` listens for `payment.failed` and runs the compensating transaction (restore stock).

**Saga orchestration:** a dedicated Saga Orchestrator service holds the saga state machine and sends commands directly to each participant service. Easier to reason about and observe (the orchestrator knows exactly which step the saga is on), but introduces a central service that must be highly available.

**Saga recovery strategies:** When a saga step fails, there are two recovery paths:
- **Backward recovery (compensate):** run compensating transactions in reverse to undo all completed steps. This is the standard rollback-style recovery. All compensations must be idempotent.
- **Forward recovery (retry):** if the failure is transient (e.g., downstream service temporarily down), retry the failed step until it succeeds. This requires idempotent steps and a persistent record of which step the saga is on (so retries resume from the right point, not from the beginning).

Real systems use both: retry transient failures for a bounded period, then fall back to compensation if retries are exhausted.

**TCC (Try-Confirm/Cancel):** A pattern popular in fintech. Each service exposes three endpoints: `try` (reserve resources tentatively), `confirm` (finalize the reservation), and `cancel` (release the tentative reservation). A coordinator calls `try` on all participants; if all succeed, calls `confirm`; if any fail, calls `cancel`. Unlike pure 2PC, TCC is non-blocking because the `try` phase only *reserves* resources (not locks them at the DB level) and each service can time out and self-cancel without waiting for the coordinator. Unlike Sagas, TCC provides a stronger atomicity window: from the perspective of business logic, either all confirmations succeed or all are cancelled, and the tentative state is never visible to end users. Cost: each service must implement all three operations and handle partial confirm/cancel scenarios.

**Outbox Pattern:** Guarantees exactly-once event publishing from a service even if the service crashes between writing to its database and publishing to the message bus. The service writes the event to an `outbox` table in the *same local database transaction* as its business data change. A separate process (or CDC tool like Debezium) reads the outbox table and publishes to the message bus, then marks rows as published. Because the write to the outbox and the business data change are in the same transaction, they are atomic — either both happen or neither does. This is the standard solution to "dual write" in event-driven sagas.

### Trade-offs

| Approach | Consistency | Blocking | Complexity | Throughput | Use Case |
|---|---|---|---|---|---|
| 2PC (XA) | Strong (ACID) | Yes — coordinator failure blocks all | High (XA drivers, coordinator logic) | Low (distributed locks) | Same-org DB, short txns |
| 3PC | Strong-ish | Reduced (but not zero under partitions) | Very high | Low | Theoretical; rarely used in production |
| Saga (choreography) | Eventual | No | Medium (event contracts, compensations) | High | Microservices, long txns |
| Saga (orchestration) | Eventual | No | Medium-high (orchestrator state machine) | High | Complex workflows, observability required |
| TCC (Try-Confirm/Cancel) | Near-strong | No (self-cancel on timeout) | High (3 endpoints per service) | Medium | Fintech, inventory reservation, hotel booking |
| Outbox pattern | Eventual | No | Low-medium | High | Exactly-once event publishing from any saga step |

### Failure Modes

**Coordinator failure after PREPARE (2PC blocking):** the worst-case failure in 2PC. All participants hold locks waiting for COMMIT or ABORT. The system is blocked until the coordinator restarts and reads its durable log. Duration of block = coordinator downtime, which can be minutes to hours in a real incident.

**Stuck saga — compensation failure:** a compensating transaction itself fails (e.g., the inventory service is down when trying to restore stock). The saga is now stuck in an intermediate state. Recovery requires either a human operator or an automated retry mechanism with idempotent compensations. All sagas should be designed so compensations can be retried safely.

**Dirty reads between saga steps:** because each step commits locally, concurrent transactions can observe intermediate state. Example: a "read current inventory" query between saga steps 1 (inventory deducted) and 3 (inventory restored after compensation) will see the deducted amount. Systems using Sagas must be designed for this — either by using "pending" states that hide intermediate results or by documenting that reads may observe in-progress sagas.

**Idempotency violations:** if a saga step is retried (due to a timeout or at-least-once delivery from a message queue), it must be idempotent — running it twice must have the same effect as running it once. Failing to design idempotent steps leads to double-charges, double-shipments, or double-debits. The standard defense is an idempotency key (a unique ID per logical operation) stored in the target service's database; on retry, the service checks for a prior record with that key before acting.

**Saga semantic vs. ACID rollback:** ACID rollback is as-if-nothing-happened — no other transaction ever observes the partial state. Saga compensation is observable — between the failed step and the completion of compensation, other transactions see intermediate state. This difference is fundamental: a customer might briefly see their inventory reserved (or their account debited) even though the saga will ultimately compensate. Systems must be designed with this in mind, typically by using "pending" or "reserved" states that hide partially-completed sagas from end-user-facing reads.

**Heuristic decisions (2PC edge case):** In 2PC, a participant that has voted YES and received no COMMIT/ABORT for an extended period may take a *heuristic decision* — unilaterally committing or aborting to release locks. XA protocol permits this as a last resort. If the coordinator later arrives with the opposite decision, you have a *heuristic inconsistency*: one participant committed and another aborted. The XA standard allows this but requires manual resolution. This is why distributed transactions across services you don't fully control are extremely dangerous.

## Interview Talking Points

- "2PC provides strong consistency but is a blocking protocol — if the coordinator crashes after collecting YES votes, participants are frozen until recovery. This is unacceptable for services that need high availability."
- "The Saga pattern replaces distributed atomicity with eventual consistency. Each step commits locally; failures trigger compensating transactions in reverse order. You trade strong consistency for availability and scalability — and you must design for the intermediate states that are now visible."
- "Compensating transactions must be idempotent — if a compensation is run twice (e.g., due to a crash during compensation), the system must still end up in a consistent state. Standard technique: check current state before acting, or use a deduplication key table."
- "3PC reduces blocking in failure scenarios but is still unsafe under network partitions — split-brain can cause half the participants to commit and the other half to abort. This is why 2PC is still the practical standard when strong consistency is required and partition tolerance is not the primary concern."
- "In practice, most microservice architectures use the Outbox Pattern with Sagas: the service writes the event to an outbox table in the same local transaction as its database write, and a separate process (or CDC/Debezium) publishes the event to the message bus. This solves the dual-write problem and ensures exactly-once event publishing even if the service crashes."
- "Sagas have two recovery strategies: backward recovery (compensate what was done) and forward recovery (retry the failed step). Real systems use both: retry transient failures for a bounded duration, then compensate. Compensation without idempotent steps is a disaster — you can end up in a worse state than the original failure."
- "TCC (Try-Confirm/Cancel) offers a middle ground between 2PC and Sagas. It avoids database-level distributed locks by reserving resources at the application layer, and it avoids the visibility of intermediate state that Sagas expose. The cost is that every service must implement three operations and the coordinator must track which phase each participant is in. Popular in fintech and hotel/airline booking systems."
- "Stripe uses event-driven sagas for payment processing. Each step publishes an idempotency-keyed event, and Stripe's retry infrastructure ensures every step eventually completes or compensates. The idempotency key is stored in Stripe's database as a deduplication record — the same key always returns the same result."
- "When asked to design a checkout flow across inventory, payment, and shipping services: start by pushing back on whether true atomicity is required. In most e-commerce systems, eventual consistency (with a saga) is acceptable because you can always compensate a failed payment or restore inventory. Reserve 2PC for genuinely ledger-critical flows within a single database cluster."

## Hands-on Lab

**Time:** ~30-40 minutes
**Services:** db-orders (Postgres, port 5433), db-inventory (Postgres, port 5434)

### Setup

```bash
cd system-design-interview/02-advanced/02-distributed-transactions/
docker compose up -d
# Wait ~10 seconds for both Postgres instances to be healthy
```

### Experiment

```bash
python experiment.py
```

The script runs five phases: (1) successful 2PC where both databases commit atomically, (2) simulated coordinator crash after PREPARE showing both databases stuck in prepared state with blocking, (3) a Saga choreography where inventory deduction succeeds but order creation fails, triggering compensation that restores inventory, (4) a "stuck saga" where the compensation step itself is blocked by a concurrent lock, showing the failure mode that requires dead-letter queues and manual recovery, and (5) an idempotent saga step demonstration showing that retrying a step with the same idempotency key does not double-apply the operation.

### Break It

```bash
# Simulate a stuck saga compensation
python -c "
import psycopg2, time

# Connect to inventory DB and check its state
conn = psycopg2.connect('host=localhost port=5434 dbname=inventory user=postgres password=postgres')
conn.autocommit = True
cur = conn.cursor()
cur.execute('SELECT product_id, quantity FROM inventory')
print('Inventory before:', cur.fetchall())

# Start a transaction that holds a lock
conn2 = psycopg2.connect('host=localhost port=5434 dbname=inventory user=postgres password=postgres')
cur2 = conn2.cursor()
cur2.execute('BEGIN')
cur2.execute(\"UPDATE inventory SET quantity = quantity - 5 WHERE product_id = 'WIDGET-A'\")
print('Long-running transaction holding lock on WIDGET-A...')

# Now try to compensate (add back inventory) from another connection
# This will BLOCK because conn2 holds the lock
print('Trying to run compensation (will block for 3 seconds)...')
conn3 = psycopg2.connect('host=localhost port=5434 dbname=inventory user=postgres password=postgres connect_timeout=3')
conn3.autocommit = False
cur3 = conn3.cursor()
try:
    cur3.execute(\"UPDATE inventory SET quantity = quantity + 5 WHERE product_id = 'WIDGET-A'\")
    print('Compensation ran (lock was released)')
except Exception as e:
    print(f'Compensation blocked/failed: {e}')
finally:
    conn2.rollback()
    conn2.close()
    conn3.close()
print('Moral: long-held locks block compensations — keep saga steps fast')
"
```

### Observe

In Phase 1, both databases commit and show consistent state (inventory reduced by 10, order created). In Phase 2, the prepared transactions appear in `pg_prepared_xacts` — this is the "zombie" state that blocks the system. In Phase 3, watch the state after step 1 (inventory deducted) vs after compensation (inventory restored) — this intermediate state is what concurrent readers would observe. In Phase 4, watch the compensation time out because a concurrent lock holder blocks it — exactly the "stuck saga" failure mode. In Phase 5, observe that the second call with the same idempotency key returns "SKIPPED" and does not reduce inventory a second time.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **Stripe payment sagas:** Stripe's payment flow decomposes a charge into multiple idempotent steps (authorize, capture, fulfill). Each step uses an idempotency key to prevent duplicate operations. If any step fails, compensating events are published. The Stripe API's idempotency key mechanism allows safe retries across the full distributed transaction. Source: Stripe Engineering Blog, "Designing robust and predictable APIs with idempotency" (2021).
- **PostgreSQL XA (2PC):** PostgreSQL has supported `PREPARE TRANSACTION` since version 8.1 (2005). The `max_prepared_transactions` config parameter (default 0, meaning disabled) must be set to enable it. It is used by JDBC drivers with `javax.transaction.xa.XAResource` for Java EE distributed transactions. Source: PostgreSQL documentation, "Two-Phase Transactions."
- **Temporal.io workflow engine:** Temporal provides a durable execution framework for implementing sagas as code. Each saga step is a Go or Java function that Temporal persists and retries automatically. On failure, the workflow history is replayed to determine which compensations to run. Companies like Netflix, Uber, and DoorDash use Temporal for multi-service transaction coordination. Source: Temporal documentation, "Workflow as code," 2023.

## Common Mistakes

- **Using 2PC across microservices you don't own.** 2PC requires that all participants support XA and that your coordinator can reliably reach them. If a third-party service doesn't support XA (and most don't), 2PC is not an option. Default to Sagas for cross-service transactions.
- **Forgetting to make compensations idempotent.** A compensation that runs twice (e.g., restores inventory twice) creates a worse inconsistency than the original failure. Always guard compensations with idempotency checks: check current state before acting, or use a deduplication key.
- **Designing saga steps that are too coarse-grained.** If a single saga step takes 30 seconds (e.g., a slow payment API), it holds intermediate state for that long, increasing the window for concurrent dirty reads. Break long operations into smaller steps.
- **Not handling the "outbox" problem for event publishing.** If you publish an event to a message bus in one operation and update your database in another, a crash between the two will cause one without the other. Use the Outbox Pattern: write the event to an outbox table in the same local database transaction, and use a separate process (or CDC/Debezium) to relay it to the message bus.
