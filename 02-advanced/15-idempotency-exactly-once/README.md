# Idempotency & Exactly-Once

**Prerequisites:** `../13-service-discovery-coordination/`
**Next:** `../15-probabilistic-data-structures/`

---

## Concept

Every distributed system must deal with partial failures: a client sends a request, the server processes it, and then the network fails before the response reaches the client. The client does not know whether the request succeeded. Should it retry? If it does not retry, it risks losing the operation (at-most-once delivery). If it retries, it risks executing the operation twice (at-least-once delivery). For read operations this is harmless, but for writes — charging a credit card, sending an email, provisioning infrastructure — duplicate execution has real consequences.

The three delivery semantics define how message passing systems handle this trade-off. At-most-once is fire-and-forget: send the message once, never retry. Messages may be lost but never duplicated — acceptable for metrics, logs, or user activity events where loss is tolerable. At-least-once guarantees delivery by retrying until acknowledged, but the consumer may process the same message multiple times. This is the default in Kafka, SQS, and most message brokers. Exactly-once would be ideal but is impossible to guarantee end-to-end in a distributed system without cooperation from both the producer and consumer — the only practical approach is at-least-once delivery combined with idempotent consumers.

An idempotent operation produces the same result when applied multiple times as when applied once. `PUT /users/123 {name: "Alice"}` is naturally idempotent — applying it ten times leaves the user with name "Alice." `POST /charges {amount: 100}` is not — applying it ten times creates ten charges. The idempotency key pattern makes non-idempotent operations safe to retry: the client generates a unique key (UUID) for each logical operation and includes it with every attempt. The server stores the key and the result after the first execution; on retry, it recognises the key and returns the original result without re-executing.

Stripe popularised the idempotency key pattern in 2015 for their payment API. Every `POST` request to Stripe must include an `Idempotency-Key` header. Stripe stores the key and the response for 24 hours. A client that receives a network error, a timeout, or a 500 response can safely retry with the same key — if the charge succeeded on the first attempt, Stripe returns the original response without charging again. This design shifted the burden of exactly-once from the unreliable network layer to the application layer, where it can be solved with a unique constraint in a database.

Kafka's producer-side idempotence (enabled with `enable.idempotence=true`) assigns each producer a PID and a sequence number to each message. Brokers deduplicate messages within a session. Kafka transactions extend this to atomic writes across multiple partitions — a batch of messages is either all committed or all aborted, providing transactional exactly-once semantics for stream processors. This powers Kafka Streams' exactly-once processing mode, but adds latency and reduces throughput compared to at-least-once.

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

**Atomic insert pattern (Postgres):**

```sql
INSERT INTO charges (idempotency_key, amount, currency, response_body)
VALUES ($1, $2, $3, $4)
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING id;
```

If two concurrent requests arrive with the same key, only one INSERT succeeds. The other sees `RETURNING id` return NULL and falls back to SELECT to retrieve the winner's response. The `UNIQUE` constraint on `idempotency_key` is the database-level guarantee.

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

| Approach | Delivery | Duplicates | Latency | Use Case |
|---|---|---|---|---|
| At-most-once | May lose | Never | Lowest | Metrics, activity logs |
| At-least-once | Guaranteed | Possible | Low | Most message queues |
| Idempotency key | Guaranteed | Prevented | Low+DB | Write APIs (payments) |
| Kafka exactly-once | Guaranteed | Prevented | ~2x | Stream processing |
| 2PC (Two-Phase Commit) | Guaranteed | Prevented | Highest | Cross-DB transactions |

### Failure Modes

**Idempotency key collision (UUID birthday problem):** UUID v4 has 2^122 possible values. At 1 billion requests per day, the probability of a collision in a year is approximately 10^-18 — negligible in practice. If using shorter keys (8 characters), collision probability rises dramatically — use UUID v4 or a content hash.

**Response stored but charge not actually processed:** The server inserts the idempotency key and response before calling the payment processor, which then fails. All retries return "success" but no charge was created. Mitigation: store the response only after the payment processor confirms success, or use a two-phase approach (mark as "processing", then update to "success").

**Idempotency key reuse across different operations:** A client accidentally reuses a UUID for a different charge (different amount or customer). The server returns the original response for the first charge, silently ignoring the new request. Mitigation: include the key parameters (amount, customer_id) in the deduplication check and return an error if they differ from the stored values.

**Deduplication window too short for client retry logic:** If the deduplication window is 1 hour but the client implements exponential back-off with a maximum retry duration of 2 hours, retries after the window re-create the operation. Align deduplication window to the maximum client retry duration with margin.

## Interview Talking Points

- "At-least-once + idempotent consumers is the practical way to achieve effectively-exactly-once in distributed systems. True exactly-once requires coordination between both ends."
- "The idempotency key pattern: client generates a UUID per logical operation, sends it with every retry. Server uses a UNIQUE constraint on the key to prevent duplicate processing and stores the response for replay."
- "Every write endpoint that clients might retry needs an idempotency key. Charges, emails, order creation, infrastructure provisioning — any non-idempotent write that has real consequences."
- "Stripe stores idempotency keys for 24 hours. After that window, the same key may trigger a new charge. Clients must not retry after the deduplication window expires."
- "Kafka producer idempotence (PID + sequence numbers) prevents duplicate messages within a session. Kafka transactions extend this to atomic cross-partition writes — the foundation for Kafka Streams exactly-once processing."
- "Natural idempotency: GET is always idempotent. PUT (replace) is idempotent. DELETE is idempotent (deleting something already deleted succeeds). POST (create) is not naturally idempotent — requires an idempotency key."

## Hands-on Lab

**Time:** ~2 minutes
**Services:** Postgres 15 + Python payment API

### Setup

```bash
cd system-design-interview/02-advanced/14-idempotency-exactly-once/
docker compose up
```

### Experiment

The script runs five phases automatically:

1. Makes a POST `/charge` with a UUID idempotency key — gets HTTP 201 (new charge created).
2. Retries the same request three times with the same key — gets HTTP 200 with `X-Idempotent-Replayed: true` each time. The charge ID in the response is identical to Phase 1.
3. Makes a new charge with a different UUID key — gets HTTP 201 (separate charge created).
4. Launches 10 concurrent threads all sending the same charge with the same idempotency key simultaneously. Exactly 1 returns 201; the others return 200. Only one row is in the database.
5. Shows the full charges table in Postgres: each row has its idempotency key, amount, customer, and stored response body.

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

Phase 4 shows that even with 10 concurrent requests hitting the database simultaneously, the `ON CONFLICT (idempotency_key) DO NOTHING` clause ensures exactly one row is inserted. The Postgres unique constraint is the atomic guarantee — no application-level locking required.

### Teardown

```bash
docker compose down
```

## Real-World Examples

- **Stripe Idempotency Keys:** Stripe introduced required idempotency keys for all POST requests in 2015. The key must be included in a header (`Idempotency-Key`) and Stripe stores the key and response for 24 hours. If the response is still being processed (a "loading" state), Stripe returns HTTP 409 Conflict to indicate the request is in flight — preventing the client from treating a slow response as a failure and retrying. Source: Stripe Engineering, "Idempotency in the API," Stripe documentation.
- **Kafka Producer Idempotence and Transactions:** Apache Kafka 0.11 (2017) introduced idempotent producers (each message gets a sequence number; brokers deduplicate) and transactional producers (atomic writes across partitions). This enabled Kafka Streams to offer exactly-once processing semantics for stream transformations — each input record produces exactly one output record, even with broker failures. Source: Confluent, "Exactly Once Semantics Are Possible: Here's How Kafka Does It," 2017.
- **AWS SQS and Lambda deduplication:** AWS SQS FIFO queues provide a MessageDeduplicationId field that deduplicates messages within a 5-minute window. When combined with Lambda, if the same SQS message is delivered twice (at-least-once), the Lambda function may execute twice. Making Lambda handlers idempotent — checking a DynamoDB conditional write before processing — is the standard pattern for exactly-once processing in the AWS serverless stack. Source: AWS documentation, "Using Lambda with Amazon SQS," best practices section.

## Common Mistakes

- **Using sequential IDs as idempotency keys.** If the client uses `retry_attempt=1` or a timestamp as the key, different retry attempts get different keys and all succeed — defeating the purpose. The key must be the same across all retries of the same logical operation. Generate a UUID once before the first attempt and reuse it for all retries.
- **Not handling the case where key parameters conflict.** A client sends `key=uuid1, amount=100` and then (by bug) sends `key=uuid1, amount=200`. The server should detect that the amount changed and return HTTP 422 Unprocessable Entity, not silently return the $100 charge response for the $200 request.
- **Only storing the idempotency key without the full response.** If you store only the key but re-execute the operation each time, concurrent retries during processing will race. Store the key atomically with the response body so retries always get the exact same response.
- **Setting the deduplication window shorter than the retry timeout.** If clients retry for up to 30 minutes with exponential back-off, but you only store keys for 10 minutes, a late retry after 15 minutes re-executes the operation. The deduplication window must be longer than the maximum possible retry duration.
