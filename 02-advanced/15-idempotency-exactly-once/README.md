# Idempotency & Exactly-Once

**Prerequisites:** `../13-service-discovery-coordination/`
**Next:** `../15-probabilistic-data-structures/`

---

## Concept

Every distributed system must deal with partial failures: a client sends a request, the server processes it, and then
the network fails before the response reaches the client. The client does not know whether the request succeeded. Should
it retry? If it does not retry, it risks losing the operation (at-most-once delivery). If it retries, it risks executing
the operation twice (at-least-once delivery). For read operations this is harmless, but for writes — charging a credit
card, sending an email, provisioning infrastructure — duplicate execution has real consequences.

The three delivery semantics define how message passing systems handle this trade-off. At-most-once is fire-and-forget:
send the message once, never retry. Messages may be lost but never duplicated — acceptable for metrics, logs, or user
activity events where loss is tolerable. At-least-once guarantees delivery by retrying until acknowledged, but the
consumer may process the same message multiple times. This is the default in Kafka, SQS, and most message brokers.
Exactly-once would be ideal but is impossible to guarantee end-to-end in a distributed system without cooperation from
both the producer and consumer — the only practical approach is at-least-once delivery combined with idempotent
consumers.

An idempotent operation produces the same result when applied multiple times as when applied once.
`PUT /users/123 {name: "Alice"}` is naturally idempotent — applying it ten times leaves the user with name "Alice."
`POST /charges {amount: 100}` is not — applying it ten times creates ten charges. The idempotency key pattern makes
non-idempotent operations safe to retry: the client generates a unique key (UUID) for each logical operation and
includes it with every attempt. The server stores the key and the result after the first execution; on retry, it
recognises the key and returns the original result without re-executing.

Stripe popularised the idempotency key pattern in 2015 for their payment API. Every `POST` request to Stripe must
include an `Idempotency-Key` header. Stripe stores the key and the response for 24 hours. A client that receives a
network error, a timeout, or a 500 response can safely retry with the same key — if the charge succeeded on the first
attempt, Stripe returns the original response without charging again. This design shifted the burden of exactly-once
from the unreliable network layer to the application layer, where it can be solved with a unique constraint in a
database.

Kafka's producer-side idempotence (enabled with `enable.idempotence=true`) assigns each producer a PID and a sequence
number to each message. Brokers deduplicate messages within a session. Kafka transactions extend this to atomic writes
across multiple partitions — a batch of messages is either all committed or all aborted, providing transactional
exactly-once semantics for stream processors. This powers Kafka Streams' exactly-once processing mode, but adds latency
and reduces throughput compared to at-least-once.

## How It Works

**Idempotency key flow:**

```
  Client generates key:   key = "550e8400-e29b-41d4-a716-446655440000"

  Attempt 1 (network timeout after server processes):
    POST /charge {amount: 100}
    Header: X-Idempotency-Key: 550e8400...
    Server: INSERT INTO charges (idempotency_key=..., amount=100, ...)
            Charge processor: ch_abc123 created
            Response body saved to DB
    Network: TIMEOUT before response reaches client

  Attempt 2 (client retries):
    POST /charge {amount: 100}
    Header: X-Idempotency-Key: 550e8400...
    Server: SELECT FROM charges WHERE idempotency_key = '550e8400...'
            → Found! Return saved response body
    Client: receives {id: "ch_abc123", status: "success"}
    → No duplicate charge created.
```

**Two-phase atomic pattern (Postgres):**

A naive implementation does SELECT → if not found → INSERT. This has a TOCTOU (time-of-check-time-of-use) race: two
concurrent requests both see no row, both attempt INSERT, and one fails at the DB constraint. The loser needs a second
SELECT to retrieve the winner's result. More importantly, there is no way to distinguish "request is still in flight"
from "request never started" — which matters when the payment processor call is slow or fails mid-flight.

The production-grade approach uses a two-phase row lifecycle:

```sql
-- Phase 1: claim the key atomically (no SELECT before this)
INSERT INTO charges (idempotency_key, amount, currency, customer_id, status)
VALUES ($1, $2, $3, $4, 'processing')
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING id;

-- If RETURNING id IS NULL, the key already exists — fetch its current state:
SELECT amount, currency, customer_id, status, response_body
FROM charges WHERE idempotency_key = $1;

-- status='processing' → another request is in flight → HTTP 409 (retry later)
-- status='success'    → idempotent replay → HTTP 200 + X-Idempotent-Replayed: true

-- Phase 2: after payment processor confirms, commit the result
UPDATE charges
SET status='success', response_body=$2
WHERE idempotency_key=$1;
```

The `UNIQUE` constraint on `idempotency_key` is the atomic gate — no application-level locking required. Only one INSERT
wins regardless of concurrency.

**Why two-phase matters:**

```
  Single-INSERT approach (fragile):
    1. Build response body (includes charge ID, status=success)
    2. INSERT (idempotency_key, response_body='{"status":"success",...}')
    3. Call payment processor
       → If processor FAILS: row says "success" but no charge was created.
          All retries return a "success" response for a charge that never happened.

  Two-phase approach (correct):
    1. INSERT status='processing'          ← atomic claim
    2. Call payment processor
       → If processor FAILS: row stays 'processing'. Next retry gets HTTP 409,
          backs off, retries later. The claim row can be expired or reset.
       → If processor SUCCEEDS:
    3. UPDATE status='success', response_body='{...}'
          Future retries get HTTP 200 (idempotent replay).
```

**Parameter fingerprinting:**

A client bug can reuse an idempotency key with different parameters (different amount or customer). Silently returning
the original response would be dangerous — a $100 response served for a $999 request. The correct behaviour is to
compare stored parameters against incoming parameters and return HTTP 422 if they differ:

```
  Key: "uuid-abc", stored: amount=100, customer=alice
  Retry: key="uuid-abc", amount=999, customer=alice
  → HTTP 422: idempotency_key reused with different parameters
    stored:   { amount: 100, customer: alice }
    received: { amount: 999, customer: alice }
```

**Deduplication window:**

```
  Key retention policy (Stripe: 24 hours):
    t=0    Charge created, key stored
    t=1hr  Client retries → key found, original response returned
    t=25hr Key expired → if client retries now, it creates a NEW charge
           (client should not retry after the deduplication window)

  For financial operations: 24-72 hours is standard
  For infrastructure provisioning: 7-30 days
  Cleanup: scheduled job DELETE FROM charges WHERE created_at < NOW() - INTERVAL '24h'
```

**Kafka exactly-once:**

```
  Producer:
    enable.idempotence=true  → assigns PID, sequence numbers
    transactional.id=X       → enables cross-partition transactions

  Consumer (stream processor):
    isolation.level=read_committed  → only read committed transactions

  Combined:
    1. Producer begins transaction
    2. Writes to input offset topic + output topic atomically
    3. Either both commit or both abort
    4. Consumer sees output only after commit
    → Each input record produces exactly one output record
    Cost: ~2x latency, ~20% throughput reduction vs at-least-once
```

### Trade-offs

| Approach               | Delivery   | Duplicates | Latency | Use Case               |
|------------------------|------------|------------|---------|------------------------|
| At-most-once           | May lose   | Never      | Lowest  | Metrics, activity logs |
| At-least-once          | Guaranteed | Possible   | Low     | Most message queues    |
| Idempotency key        | Guaranteed | Prevented  | Low+DB  | Write APIs (payments)  |
| Kafka exactly-once     | Guaranteed | Prevented  | ~2x     | Stream processing      |
| 2PC (Two-Phase Commit) | Guaranteed | Prevented  | Highest | Cross-DB transactions  |

### Failure Modes

**Idempotency key collision (UUID birthday problem):** UUID v4 has 2^122 possible values. At 1 billion requests per day,
the probability of a collision in a year is approximately 10^-18 — negligible in practice. If using shorter keys (8
characters), collision probability rises dramatically — use UUID v4 or a content hash.

**Response stored before charge is confirmed (single-phase anti-pattern):** If the server inserts the idempotency key
and a "success" response in a single step before calling the payment processor, a processor failure leaves the row
claiming success for a charge that never happened. All subsequent retries return the cached "success" without
re-attempting the charge. Use the two-phase approach: claim with `status='processing'`, commit `status='success'` only
after the processor confirms.

**In-flight request with no status tracking:** Without a `'processing'` state, when a request is mid-flight and a
concurrent retry arrives, the server either races to re-execute or uses application-level locking. Stripe returns HTTP
409 Conflict for any retry that arrives while the original request is still being processed — this requires explicitly
storing the "in flight" status. Without 409, a slow payment processor call will cause concurrent retries to execute the
charge multiple times.

**Idempotency key reuse across different operations:** A client accidentally reuses a UUID for a different charge (
different amount or customer). Without parameter fingerprinting, the server silently returns the original response.
Mitigation: store and compare (amount, currency, customer_id) — return HTTP 422 if the incoming parameters differ from
stored values.

**Deduplication window too short for client retry logic:** If the deduplication window is 1 hour but the client
implements exponential back-off with a maximum retry duration of 2 hours, retries after the window expiry re-execute the
operation. The deduplication window must exceed the maximum possible client retry duration with margin.

**Stale 'processing' rows after processor failure:** If the payment processor call fails (the process crashes
mid-flight), the row stays in `status='processing'` forever. Clients retrying will always get HTTP 409. Mitigation: a
background job resets rows stuck in `'processing'` for longer than the maximum expected processor latency (e.g., 30
seconds). Alternatively, the claim INSERT includes an `expires_at` column and the SELECT checks for expiry.

## Interview Talking Points

- "At-least-once delivery plus idempotent consumers is the practical path to effectively-exactly-once semantics. True
  end-to-end exactly-once requires coordination at both the producer and consumer — impossible to guarantee at the
  network layer alone."
- "The idempotency key pattern: client generates a UUID once per logical operation and includes it in every retry. The
  server uses a `UNIQUE` constraint on the key as the atomic gate. The database does the deduplication — no
  application-level locking needed."
- "Use a two-phase row lifecycle, not a single INSERT with the response body. Claim first with `status='processing'`,
  call the payment processor, then commit `status='success'`. This prevents the failure mode where a cached 'success'
  response is stored before the external call is confirmed — a single-phase INSERT bakes in the response before knowing
  if the charge worked."
- "Stripe returns HTTP 409 Conflict if a request with the same key is still in flight. This requires explicitly tracking
  the 'processing' state. Without it, a slow processor call plus an impatient client retry causes the charge to execute
  twice."
- "Parameter fingerprinting: store the key parameters (amount, currency, customer_id) alongside the idempotency key. If
  a retry arrives with the same key but different parameters, return HTTP 422 — it is a client bug and must be surfaced,
  not silently served the wrong cached response."
- "Kafka producer idempotence (PID + sequence numbers) prevents duplicate messages within a producer session. Kafka
  transactions extend this to atomic cross-partition writes — the basis for Kafka Streams exactly-once. Cost: roughly 2x
  latency, ~20% throughput reduction versus at-least-once."
- "Natural idempotency: GET is always idempotent. PUT (replace semantics) is idempotent. DELETE is idempotent — deleting
  an absent resource returns 404 or 200, both acceptable. POST (create) is not — it requires an idempotency key."
- "Stale 'processing' rows are a hidden production issue: if the server crashes between claiming the key and committing
  the result, the row stays 'processing' forever and all retries get 409. Fix with a background sweep that resets rows
  stuck in 'processing' beyond the max expected processor latency."

## Hands-on Lab

**Time:** ~2 minutes
**Services:** Postgres 15 + Python payment API

### Setup

```bash
cd system-design-interview/02-advanced/15-idempotency-exactly-once/
docker compose up
```

### Experiment

The script runs six phases automatically:

1. Makes a POST `/charge` with a UUID idempotency key — gets HTTP 201 (new charge created).
2. Retries the same request three times with the same key — gets HTTP 200 with `X-Idempotent-Replayed: true` each time.
   The charge ID in the response is identical to Phase 1.
3. Makes a new charge with a different UUID key — gets HTTP 201 (separate charge created).
4. Launches 10 concurrent threads all sending the same charge with the same idempotency key simultaneously. Exactly 1
   returns 201; the others return 200 (replay) or 409 (in flight). Only one row reaches `status='success'` in the
   database.
5. Demonstrates parameter fingerprinting: reuses an existing key with a different amount — gets HTTP 422 showing the
   stored vs received parameters.
6. Shows the full charges table in Postgres: each row has its idempotency key, amount, customer, and status lifecycle.

### Break It

Demonstrate what happens without idempotency keys — double charges:

```bash
docker compose exec experiment python -c "
import requests, os
API = 'http://payment-api:8003'
# Make 5 requests without idempotency key validation
# (temporarily patch the API to skip the check — see payment_api.py)
# Instead, simulate by using a different key every time:
import uuid
for i in range(5):
    r = requests.post(f'{API}/charge',
        json={'amount': 100, 'currency': 'usd', 'customer_id': 'cust_bad'},
        headers={'X-Idempotency-Key': str(uuid.uuid4())})  # new key each time!
    print(f'Attempt {i+1}: {r.status_code} {r.json()[\"id\"]}')
print('5 different keys = 5 charges (customer charged 5x!)')
"
```

### Observe

Phase 4 shows that with 10 concurrent requests hitting the database simultaneously, the
`ON CONFLICT (idempotency_key) DO NOTHING` clause ensures exactly one row is inserted. The Postgres unique constraint is
the atomic guarantee — no application-level locking required. Concurrent losers that arrive before the winner commits
the response receive HTTP 409 (in-flight state); those that arrive after receive HTTP 200 (idempotent replay).

Phase 5 shows parameter fingerprinting: the API detects when a key is reused with a different amount and returns HTTP
422 rather than silently serving the wrong cached response.

### Teardown

```bash
docker compose down
```

## Real-World Examples

- **Stripe Idempotency Keys:** Stripe introduced required idempotency keys for all POST requests in 2015. The key must
  be included in a header (`Idempotency-Key`) and Stripe stores the key and response for 24 hours. If the response is
  still being processed (a "loading" state), Stripe returns HTTP 409 Conflict to indicate the request is in flight —
  preventing the client from treating a slow response as a failure and retrying. Source: Stripe Engineering, "
  Idempotency in the API," Stripe documentation.
- **Kafka Producer Idempotence and Transactions:** Apache Kafka 0.11 (2017) introduced idempotent producers (each
  message gets a sequence number; brokers deduplicate) and transactional producers (atomic writes across partitions).
  This enabled Kafka Streams to offer exactly-once processing semantics for stream transformations — each input record
  produces exactly one output record, even with broker failures. Source: Confluent, "Exactly Once Semantics Are
  Possible: Here's How Kafka Does It," 2017.
- **AWS SQS and Lambda deduplication:** AWS SQS FIFO queues provide a MessageDeduplicationId field that deduplicates
  messages within a 5-minute window. When combined with Lambda, if the same SQS message is delivered twice (
  at-least-once), the Lambda function may execute twice. Making Lambda handlers idempotent — checking a DynamoDB
  conditional write before processing — is the standard pattern for exactly-once processing in the AWS serverless stack.
  Source: AWS documentation, "Using Lambda with Amazon SQS," best practices section.

## Common Mistakes

- **Using sequential IDs as idempotency keys.** If the client uses `retry_attempt=1` or a timestamp as the key,
  different retry attempts get different keys and all succeed — defeating the purpose. The key must be the same across
  all retries of the same logical operation. Generate a UUID once before the first attempt and reuse it for all retries.
- **Not handling the case where key parameters conflict.** A client sends `key=uuid1, amount=100` and then (by bug)
  sends `key=uuid1, amount=200`. The server should detect that the amount changed and return HTTP 422 Unprocessable
  Entity, not silently return the $100 charge response for the $200 request.
- **Only storing the idempotency key without the full response.** If you store only the key but re-execute the operation
  each time, concurrent retries during processing will race. Store the key atomically with the response body so retries
  always get the exact same response.
- **Setting the deduplication window shorter than the retry timeout.** If clients retry for up to 30 minutes with
  exponential back-off, but you only store keys for 10 minutes, a late retry after 15 minutes re-executes the operation.
  The deduplication window must be longer than the maximum possible retry duration.
