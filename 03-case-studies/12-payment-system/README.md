# Case Study: Payment System

**Prerequisites:** `../../02-advanced/02-distributed-transactions/`, `../../02-advanced/14-idempotency-exactly-once/`, `../../01-foundations/08-databases-sql-vs-nosql/`

---

## The Problem at Scale

Stripe processes over $640 billion annually with 99.99% uptime. Every payment must happen exactly once — duplicates cost real money; dropped payments lose revenue. The system must handle millions of API calls per day from thousands of merchants, each potentially retrying failed requests.

| Metric | Value |
|---|---|
| Annual volume | $640B+ |
| API calls/day | Hundreds of millions |
| Uptime target | 99.99% (~52 min downtime/year) |
| Transaction latency P99 | < 500ms |
| Idempotency window | 24 hours |
| Ledger records/year | Billions |

The core challenge: payment systems must be **exactly-once** in the face of network failures, timeouts, and retries. A charge that happens twice is catastrophic. A charge that never completes loses a customer.

---

## Step 1 — Clarify Requirements (3–5 min)

Before drawing anything, lock down scope with the interviewer. Ambiguity kills payment system designs.

**Functional scope questions:**
- Are we building the full payment stack (card network integration, ACH settlement) or just the merchant-facing API layer? *(This lab: API layer + ledger)*
- Do we support authorization-only (pre-auth) or only immediate capture?
- Is partial refund required, or only full refunds?
- Multi-currency? Which currencies, and who owns FX conversion?
- Recurring/subscription billing, or only one-time charges?
- Do merchants get per-transaction webhooks on state changes?

**Non-functional scope questions:**
- Global or single-region?
- PCI-DSS scope? Are raw card numbers ever stored, or do we use tokens from a vault (e.g., Stripe Elements)?
- Consistency requirement on balance reads: strong (always see latest) or eventual (lag acceptable)?
- What is the acceptable fraud loss rate? (Affects fraud scoring latency budget)

**Stated requirements for this design:**
- Merchant API: POST /charge, POST /refund, GET /transaction
- Idempotent retries: same `idempotency_key` → same response
- Double-entry ledger: immutable, append-only, sum always = 0
- No raw card storage — cards pre-tokenized by payment vault
- Single-region, active-passive failover
- Exactly-once semantics; 99.99% uptime; P99 charge latency < 500ms

---

## Step 2 — Capacity Estimation (3–5 min)

### Baseline Numbers

| Metric | Calculation | Result |
|---|---|---|
| Transactions/second | 100M charges/day ÷ 86,400 s | ~1,160 TPS avg |
| Peak TPS | 3× average (holiday, sale events) | ~3,500 TPS |
| Read:write ratio | Status checks, reporting, reconciliation | ~10:1 |
| Read QPS peak | 10 × 3,500 | ~35,000 QPS |

### Write Path Sizing

| Resource | Calculation | Result |
|---|---|---|
| Ledger writes/s | 2 entries × 3,500 TPS | 7,000 rows/s |
| Transaction writes/s | 1 row × 3,500 TPS | 3,500 rows/s |
| Total DB writes | ledger + transactions + indexes | ~15,000 write ops/s |
| Single Postgres node capacity | NVMe SSD, synchronous commit | ~50,000 writes/s — 1 node suffices for now |
| Idempotency key store (Redis) | 100M keys/day × 150 B each | ~15 GB/day; pruned at 24h TTL |

### Storage Growth

| Table | Row size | Annual rows | Annual size |
|---|---|---|---|
| transactions | ~500 B | 36.5 B (100M/day × 365) | ~18 TB |
| ledger_entries | ~300 B | 73 B (2 per transaction) | ~22 TB |

**Implication:** at full Stripe scale, Postgres on a single node does not scale for storage — sharding by `account_id` is necessary after ~2 years of growth. For the interview, mention this as the inflection point.

### Network Bandwidth

| Direction | Calculation | Result |
|---|---|---|
| Inbound requests | 3,500 req/s × 1 KB avg payload | ~3.5 MB/s |
| Card network calls | 3,500 × ~800 B (auth request/response) | ~2.8 MB/s |
| Outbound webhooks | 3,500 × 1 KB | ~3.5 MB/s |

All well within a single region's commodity network capacity.

---

## Step 3 — High-Level Design (10 min)

```
  Client (Merchant SDK)
       │  POST /charge  {amount, currency, customer_id, idempotency_key}
       ▼
  ┌────────────────────────────────────────────────────────────────┐
  │              API Gateway / Load Balancer (L7)                  │
  │   TLS termination, rate limiting per API key, routing          │
  └───────────────────────────┬────────────────────────────────────┘
                              │
  ┌───────────────────────────▼────────────────────────────────────┐
  │                    Payment Service                              │
  │                                                                │
  │  1. Validate request (amount > 0, valid currency, etc.)        │
  │  2. Check idempotency_key                                      │
  │       → Redis L1 cache (< 1ms): return cached response        │
  │       → Postgres UNIQUE index (fallback): return stored row    │
  │       → New key: proceed to step 3                             │
  │  3. BEGIN TRANSACTION                                          │
  │     a. INSERT transactions (id, idem_key, amount, status)      │
  │     b. INSERT ledger_entries DEBIT  customer_id                │
  │     c. INSERT ledger_entries CREDIT merchant_id                │
  │  4. COMMIT  ← atomic: all succeed or none persist             │
  │  5. [async] Call card network (authorize → capture)            │
  │  6. Cache response in Redis (24h TTL)                          │
  │  7. [async] Publish PaymentCreated event → webhook queue       │
  │  8. Return 201 with transaction object                         │
  └──────────┬──────────────────────────────────────┬─────────────┘
             │                                      │
  ┌──────────▼───────────┐              ┌───────────▼────────────┐
  │       Postgres        │              │    Redis               │
  │  transactions table   │              │  idem:{key} → response │
  │  ledger_entries table │              │  24h TTL, LRU eviction │
  │  ACID, UNIQUE idem_key│              └────────────────────────┘
  │  append-only ledger   │
  └───────────────────────┘
             │
  ┌──────────▼───────────┐
  │  Reconciliation Job   │
  │  (nightly/hourly)     │
  │  Verify SUM(ledger)=0 │
  │  Cross-check vs Visa/ │
  │  Mastercard files     │
  └───────────────────────┘
```

**Postgres** is the source of truth. ACID transactions ensure that either all three operations (insert transaction + 2 ledger entries) succeed, or none do. No partial state can persist.

**UNIQUE constraint** on `idempotency_key` in the transactions table is the last-line defense against concurrent duplicates. If two requests race with the same key, Postgres's unique index ensures only one INSERT succeeds. The loser gets a `UniqueViolation` exception, fetches and returns the winner's result.

**Redis** caches idempotency keys for fast duplicate detection on the hot path (< 1ms vs ~5ms for a Postgres SELECT). Redis is a performance optimization, not a correctness guarantee — Postgres is authoritative.

**TOCTOU warning (important for interviews):** a naive implementation checks `SELECT WHERE idempotency_key = ?` and then `INSERT`. Between those two statements, a concurrent request with the same key can slip through the SELECT (both see "not found") and both attempt an INSERT. The Postgres UNIQUE constraint is the atomic guard: only one INSERT wins, the other throws `UniqueViolation`. The pre-check SELECT is an optimization only — never rely on it for correctness.

---

## Step 4 — Deep Dives

### 1. Idempotency: Every Payment Endpoint Needs It

**The problem:** A merchant's server sends `POST /charge`, Stripe processes the charge (money moves), but the response is lost in transit (network timeout). The merchant's retry logic sends the exact same request again.

Without idempotency: customer gets charged twice. Stripe issues a refund. Customer leaves. Trust is destroyed.

**The solution:** clients send a unique `idempotency_key` with every request (UUID generated client-side). The server stores the response keyed by `(idempotency_key, endpoint)`.

```
First call:  POST /charge {amount: 100, idem_key: "abc123"}
             → Process charge → Store {abc123 → {txn_id: xyz, amount: 100}}
             → Return {txn_id: xyz, amount: 100}

Retry call:  POST /charge {amount: 100, idem_key: "abc123"}
             → Look up abc123 → Found!
             → Return {txn_id: xyz, amount: 100}  (NO second charge)
```

**Key properties:**
- `idempotency_key` must be unique per payment attempt (new idempotency_key = new charge)
- Keys are scoped to the API key (merchant A's key-123 ≠ merchant B's key-123)
- Expiry window: 24 hours (older keys can be pruned; retry after 24h = new charge)
- Stripe uses UUIDs; recommended format: `pay_{uuid4}`

**Implementation detail:** Postgres UNIQUE constraint on `idempotency_key` column + `ON CONFLICT DO NOTHING RETURNING id`. If RETURNING is empty, the row already exists → fetch and return it. Atomic at the database level.

### 2. Double-Entry Bookkeeping: The Immutable Ledger

Double-entry bookkeeping is a 500-year-old accounting principle. Every money movement creates two entries that cancel out:

```
Charge $100:
  DEBIT  customer-001  $100.00  (money leaves customer)
  CREDIT merchant-001  $100.00  (money arrives at merchant)
  Net: $0.00

Refund $100:
  CREDIT customer-001  $100.00  (money returns to customer)
  DEBIT  merchant-001  $100.00  (money leaves merchant)
  Net: $0.00

Total ledger sum: ALWAYS $0.00
```

**Why immutable?** Ledger entries are never updated or deleted. A refund is a new set of entries that reverses the original. This creates a complete audit trail: every state the ledger has ever been in is recoverable by replaying entries in order.

**The invariant:** `SELECT SUM(CASE WHEN entry_type='credit' THEN amount ELSE -amount END) FROM ledger_entries` must always equal 0. Running this query is the simplest and most powerful integration test for a payment system.

**Postgres schema design:**
```sql
CREATE TABLE ledger_entries (
    id BIGSERIAL PRIMARY KEY,          -- monotonically increasing (order matters)
    transaction_id TEXT NOT NULL,      -- which transaction created this entry
    account_id TEXT NOT NULL,          -- customer-001, merchant-001, etc.
    amount NUMERIC(18, 2) NOT NULL,    -- always positive (sign is in entry_type)
    entry_type TEXT CHECK (entry_type IN ('debit', 'credit')),
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

`NUMERIC(18, 2)` avoids floating-point errors. `18` digits handles $10 quadrillion; `2` decimal places for cents. Never use `FLOAT` for money.

### 3. Distributed Transaction Problem: Saga Pattern

**Problem:** A payment involves two separate systems:
1. Charge the customer's credit card (Visa network)
2. Credit the merchant's bank account (ACH network)

These are separate external systems — you can't wrap them in a Postgres ACID transaction.

**Saga pattern:** break the distributed transaction into a sequence of local transactions, each with a compensating transaction on failure:

```
Step 1: Authorize card (Visa) → success
Step 2: Reserve funds in escrow (Postgres) → success
Step 3: Submit ACH credit to merchant (ACH) → FAILS

Compensation:
Step 3 fail → reverse Step 2 (release escrow)
Step 2 reversed → reverse Step 1 (void authorization)
```

**Orchestration vs choreography:**
- **Orchestration:** a central Payment Saga Orchestrator drives each step and handles compensations. Easier to reason about, single point of failure.
- **Choreography:** each service publishes events, other services react. More resilient, harder to debug.

Stripe uses an orchestration model: their Charge object transitions through states (authorized → captured → settled → refunded), and a saga orchestrator drives each transition.

### 4. Reconciliation: Periodic Batch Integrity Check

**Problem:** even with ACID transactions, bugs can introduce inconsistencies over time. How do you detect a bug that double-charged 100 customers last Tuesday?

**Reconciliation:** a batch job (runs nightly or hourly) that verifies:
1. `SELECT SUM(...)` on ledger = 0 (double-entry invariant)
2. Every transaction has exactly 2 ledger entries
3. Gross volume matches what payment networks report (external reconciliation)
4. Settlement totals match bank statements

**External reconciliation:** Visa/Mastercard provide daily settlement files. Compare your internal ledger against the settlement file line by line. Any discrepancy (Visa shows $1,000 charged, you show $900) triggers an alert and investigation.

**Stripe's approach:** continuous reconciliation running in the background, not just nightly. A streaming job (Flink) reads the ledger event stream and computes running totals per account. Any imbalance triggers an immediate PagerDuty alert.

### 5. Authorization vs Capture: The Two-Step Card Flow

Most card payments are not a single atomic operation — they are two distinct network calls:

```
Step 1 — Authorization (instant, ~100ms)
  Merchant → Card Network → Issuing Bank
  "Can this customer spend $200?"
  Bank response: authorized (reservation placed on customer's limit)
  Ledger: DEBIT customer-escrow $200, CREDIT escrow-001 $200
  Transaction status: "authorized"

Step 2 — Capture (batch, T+1 or T+2)
  Merchant → Card Network → Settlement
  "Actually charge the $200 we authorized yesterday."
  Money moves from bank reserves to merchant settlement account.
  Ledger: DEBIT escrow-001 $200, CREDIT merchant-001 $200
  Transaction status: "captured" → "settled"
```

**Why two steps?**
- Hotel/car rental: authorize on check-in, capture on check-out (amount may differ slightly)
- E-commerce: authorize at order time, capture only when shipped
- Reduces fraud: merchant can void authorization if order is cancelled, no money moved

**Partial capture:** you can capture less than the authorized amount (e.g., only 2 of 3 items shipped). The remainder of the authorization is automatically released.

**Authorization expiry:** most issuers hold an authorization for 5–7 days. After that, it expires and the reserved funds return to the customer. Merchants must capture within this window.

**In the ledger:** Authorization creates entries against an escrow account. Capture moves from escrow to merchant. If voided, the escrow entries are reversed. The invariant (sum=0) holds at every step.

### 6. Currency, Precision, and FX

**Never use floating-point for money.** IEEE 754 cannot represent 0.10 exactly:
```python
>>> 0.1 + 0.2
0.30000000000000004
```

**Use `NUMERIC(18, 2)` in Postgres.** Or store amounts as integer minor units (cents): $9.99 → 999 cents. Integer arithmetic is exact.

**Multi-currency design:**
- Store `(amount, currency)` together — never just amount
- All internal ledger entries use the presentment currency
- FX conversion creates two separate transactions: sell USD, buy EUR
- FX rate is locked at authorization time (settlement may differ slightly — FX risk)
- Settlement always happens in the local currency of the merchant's bank

**Stripe's approach:** charges are in the presentment currency. Payouts to the merchant's bank account are in the bank's local currency. Stripe absorbs the FX spread as margin.

**Interview tip:** if asked about multi-currency at scale, mention the complexity of FX reconciliation — your internal ledger is in multiple currencies, and the sum-to-zero invariant must hold per-currency, not in aggregate.

### 7. Fraud Detection (Brief Overview)

Velocity checks (rule-based):
- > 5 transactions from same IP in 10 minutes → flag
- Transaction from country different from account's usual country → flag
- Amount > 3× historical average for this customer → flag

ML scoring (beyond scope):
- Features: transaction amount, time, merchant category, device fingerprint, historical behavior
- Model: gradient-boosted trees (XGBoost) trained on labeled fraud/non-fraud data
- Applied to every transaction in < 100ms (model inference is fast)

3D Secure (3DS): redirect customer to card issuer's authentication page for high-risk transactions. Shifts fraud liability from merchant to issuer.

---

---

## Failure Modes and Scale Challenges

These are the failure scenarios interviewers expect you to reason through for a senior/staff role:

### 1. Response Lost After Commit (The Idempotency Cliff)
Payment service commits to Postgres, then crashes before returning the HTTP response. The client never sees 201. Client retries with the same `idempotency_key` → hits the UNIQUE constraint → fetches and returns the committed row. **No double charge.** This is why idempotency must be tied to the database commit, not the HTTP response.

### 2. Card Network Call Inside vs Outside the Transaction
If you call Visa/Mastercard *inside* the Postgres transaction:
- Postgres holds row locks for the full duration of the network call (~100–300ms)
- Under load, this exhausts the connection pool and causes cascading failures

Better pattern: commit the `authorized` record to Postgres first, then call the card network asynchronously, then update status to `captured` or `failed`. The transaction can be retried if the card call fails.

### 3. Redis Cache Miss + Postgres Failover
Redis is down. Postgres primary fails mid-request. Client retries hit the replica (which just became primary). The idempotency_key check on the new primary correctly returns the original result — because Postgres replication is synchronous (with `synchronous_commit=on`). If you use async replication, a recent write may be lost, causing the replica to process the charge again → double charge. **Always use synchronous replication for payment-critical data.**

### 4. Partial Refund Leaves Ledger Imbalanced
A bug in the refund service inserts the credit entry but crashes before the debit entry. Postgres rolls back the transaction — both entries are gone. The original charge is untouched. The ledger sum remains zero. The client retries the refund with the same `idempotency_key` → idempotency guard detects no refund exists (rollback wiped it) → processes the refund again correctly.

### 5. Ledger Corruption via Direct DB Update
An engineer runs `UPDATE ledger_entries SET amount = 50 WHERE id = 123` to "fix" a bug. The entry was $100 → $50. The matching entry is still $100 → ledger sum is now -$50. **Solution:** ledger_entries must be immutable in code and at the DB layer (revoke UPDATE/DELETE privileges on the table for the application role). Corrections are new entries that reverse and re-enter.

### 6. Idempotency Key Collision
Two merchants, same `idempotency_key` value (e.g., "order-1"). **Solution:** scope idempotency keys to `(api_key, idempotency_key)`. Stripe's UNIQUE constraint is `UNIQUE (api_key_id, idempotency_key)`, not just `UNIQUE (idempotency_key)`.

### 7. Thundering Herd on Card Network Timeout
Card network (Visa) is slow (P99 = 2s). At 3,500 TPS, you have 7,000 concurrent in-flight card calls. Each holds a DB connection. Connection pool exhausted → new requests fail immediately → cascade. **Solution:** circuit breaker (stop calling Visa if error rate > 5%), bulkhead pattern (separate thread pool / connection pool for card network calls vs. DB calls), and exponential backoff with jitter on retries.

---

## How It Actually Works

**Stripe's Engineering Blog "Designing robust and predictable APIs with idempotency"** (2017): Stripe describes exactly the system implemented in this lab. Key details: idempotency keys stored in Redis with Postgres as fallback; keys scoped to API key; 24-hour retention window. They explicitly describe the "return original response on retry" behavior.

**PayPal's payment architecture:** PayPal uses a similar double-entry ledger but at planetary scale — 430M accounts, 50B transactions/year. Their ledger is sharded by account_id across thousands of Postgres instances. Each shard handles a subset of accounts. Cross-shard transfers (customer on shard 1, merchant on shard 2) use a two-phase commit protocol or Saga.

**Stripe's Ledger architecture** (2023 talk): Stripe's modern ledger is append-only, immutable, and event-sourced. The "current balance" of any account is computed by replaying all entries — but this is too slow at query time, so they maintain a separate read model (materialized view) of balances, updated asynchronously.

Source: Stripe Engineering Blog "Designing robust and predictable APIs with idempotency" (2017); PayPal Technology Blog "Inside the PayPal Ledger" (2018); Stripe Sessions 2023 "The Architecture of Stripe's Ledger".

---

## Hands-on Lab

**Time:** ~20 minutes
**Services:** `db` (Postgres 15), `redis` (Redis 7), `payment-service` (Python/Flask)

### Setup

```bash
cd system-design-interview/03-case-studies/12-payment-system/
docker compose up -d
# Wait ~30s for payment-service to be healthy
docker compose ps
```

### Experiment

```bash
# Run from host
python experiment.py

# Or with curl directly:
curl -X POST http://localhost:5002/charge \
  -H "Content-Type: application/json" \
  -d '{"amount":"100","currency":"USD","customer_id":"cust-1","idempotency_key":"test-001"}'
```

The script runs 7 phases:

1. **Double-entry:** charge $100, inspect 2 ledger entries (1 debit + 1 credit)
2. **Idempotency:** retry same charge 3 times, verify same response each time
3. **Concurrent retries:** 10 threads, same idempotency_key → exactly 1 charge
4. **Failed charges:** amount=0, negative, missing fields → no ledger entries
5. **Reconciliation:** run 5 more charges, verify ledger sum = 0
6. **Timeout simulation:** charge succeeds server-side, client retries → idempotent
7. **Refund:** reverse original charge, verify ledger still balanced

### Break It

**Manually verify the accounting invariant:**

```bash
docker compose exec db psql -U app -d payments -c "
SELECT
    SUM(CASE WHEN entry_type='credit' THEN amount ELSE -amount END) as net
FROM ledger_entries;"
# Should always return 0.00
```

**Try to sneak a partial insert (should fail):**

```bash
# Insert a debit without a matching credit — Postgres rollback prevents this
docker compose exec db psql -U app -d payments -c "
BEGIN;
INSERT INTO transactions VALUES ('test-broken', 'idem-broken', 'cust-x', 50, 'USD', 'completed', 'test', NOW(), NOW());
INSERT INTO ledger_entries (transaction_id, account_id, amount, entry_type) VALUES ('test-broken', 'cust-x', 50, 'debit');
-- Intentionally omit the credit entry
ROLLBACK;
-- Net sum is still 0 because we rolled back
"
```

**Observe idempotency key cache in Redis:**

```bash
docker compose exec redis redis-cli keys "idem:*"
```

### Observe

```bash
# Full ledger
curl http://localhost:5002/ledger | python3 -m json.tool

# Balance per account
curl http://localhost:5002/balance/merchant-001

# Transaction detail
curl http://localhost:5002/transaction/<txn_id_from_experiment>
```

### Teardown

```bash
docker compose down -v
```

---

## Interview Checklist

1. **Q: How do you prevent a customer from being charged twice on a retry?**
   A: Idempotency keys. The client generates a unique UUID per payment attempt and sends it as `idempotency_key`. The server stores the response indexed by this key (in Postgres with a UNIQUE constraint). On retry with the same key, the server returns the stored response without processing a new charge. The UNIQUE constraint handles concurrent race conditions atomically.

2. **Q: What is double-entry bookkeeping and why does a payment system need it?**
   A: Double-entry means every money movement creates two equal and opposite entries: a debit (money leaves an account) and a credit (money arrives). The sum of all entries is always zero. This provides a mathematical correctness invariant: `SELECT SUM(amount × sign) FROM ledger = 0`. If this query returns non-zero, there's a bug. It also provides a complete audit trail — every historical state is derivable from the ledger.

3. **Q: What is a Saga and when would you use it for payments?**
   A: A Saga is a pattern for distributed transactions across services that don't share a database. Each step has a local transaction and a compensating transaction on failure. For payments: (1) authorize card → (2) reserve escrow → (3) settle with merchant. If step 3 fails, run compensations: reverse step 2 (release escrow), then reverse step 1 (void authorization). Sagas are eventually consistent, not immediately consistent like ACID transactions.

4. **Q: Why use NUMERIC instead of FLOAT for money in Postgres?**
   A: Floating-point arithmetic has precision errors: `0.1 + 0.2 = 0.30000000000000004` in IEEE 754. This is unacceptable for money. `NUMERIC(18, 2)` stores exact decimal values. 18 digits of precision covers amounts up to $10 quadrillion. 2 decimal places for cents. Never use FLOAT, DOUBLE PRECISION, or REAL for monetary values.

5. **Q: How do you reconcile your internal ledger with what payment networks report?**
   A: External reconciliation: payment networks (Visa, Mastercard) send daily settlement files. A batch job compares each line item in the settlement file against the corresponding ledger entry. Any discrepancy triggers an alert. In practice, this catches bugs (incorrect amounts), timing differences (authorization vs capture), and network errors (charge that never reached the network).

6. **Q: How would you shard the ledger at PayPal scale (50B transactions/year)?**
   A: Shard by `account_id` hash. Each shard handles a subset of accounts. A customer's entire transaction history is on one shard — no cross-shard queries needed for single-account views. Cross-shard transfers (customer on shard 1, merchant on shard 2) use a Saga: debit customer (shard 1), then credit merchant (shard 2). If step 2 fails: compensate by crediting customer back on shard 1.

7. **Q: What is the difference between authorization and capture in a payment?**
   A: Authorization reserves funds on the customer's card without moving money. Capture actually transfers money (typically within 5 days of authorization). Hotels, car rentals, and Stripe use pre-authorization to hold funds. Authorization creates a ledger entry in "escrow"; capture moves it from escrow to merchant. If capture never happens, the authorization expires and the reserved funds return to the customer.

8. **Q: How do you handle a refund 30 days after the original charge?**
   A: A refund is a new transaction with a new idempotency_key that references the original transaction_id. It creates two new ledger entries that reverse the original: credit customer (money returns), debit merchant (money leaves). The original entries are never modified (immutable ledger). The net effect: the original charge + refund sum to zero. The ledger invariant (sum=0) is maintained.

9. **Q: What is your strategy for idempotency key expiry?**
   A: 24-hour window is standard. After 24 hours, the same idempotency_key is treated as a new request. This handles: (1) clients that generate a new key per retry (correct), (2) clients that reuse keys across days (they get a new charge — client bug, not server bug). Keys older than 24 hours are pruned from Redis (TTL) and marked as expired in Postgres (not deleted — audit trail).

10. **Q: Walk me through a Stripe charge from API call to settlement.**
    A: (1) Merchant calls POST /v1/charges with amount, source (card token), idempotency_key. (2) Stripe checks idempotency_key — new request. (3) Stripe validates card token, performs fraud scoring. (4) Stripe sends authorization request to card network (Visa/Mastercard) — ~100ms. (5) Network returns authorized/declined. (6) If authorized: INSERT transaction (pending), INSERT ledger entries (debit customer, credit Stripe escrow). COMMIT. (7) Return charge object to merchant (201). (8) Daily: Stripe captures authorized charges in batch, moves money from escrow to merchant account, sends to ACH. (9) Merchant receives funds in 2-7 business days. (10) Daily settlement file from Visa reconciled against Stripe's ledger.

11. **Q: Two concurrent requests arrive with the same idempotency_key at the exact same millisecond. How does your system handle it?**
    A: The optimistic path (pre-check SELECT) may return "not found" for both. Both attempt an INSERT. Postgres evaluates the UNIQUE constraint atomically within the btree index latch — only one INSERT succeeds. The loser gets a `UniqueViolation` (serialization error). The loser's handler catches it, does a SELECT to find the winner's row, and returns that row as the response. No double charge. This is why the UNIQUE constraint — not the pre-check SELECT — is the correctness guarantee. The pre-check is purely a performance optimization to avoid hitting the UNIQUE path on the common case.

12. **Q: How do you handle multi-currency payments at scale?**
    A: Store `(amount, currency)` together always. Use `NUMERIC(18, 2)` or integer minor units (cents) — never FLOAT. Ledger entries are in the presentment currency; the sum-to-zero invariant holds per-currency independently. FX conversion is a separate pair of transactions: debit in source currency, credit in target currency, with the exchange rate locked at the time of the transaction and stored on the record. For reconciliation, compare per-currency totals, not aggregate across currencies — that aggregate is meaningless. At PayPal scale, shards are often currency-aligned to avoid cross-currency query complexity.

13. **Q: Why should you not call the card network inside the database transaction?**
    A: A card network call takes 100–300ms on average, with tail latency potentially seconds (timeouts). If this call is inside a `BEGIN...COMMIT` block, Postgres holds row locks for the full duration. At 3,500 TPS with 200ms average card latency, you have ~700 concurrent transactions holding locks. Under load spikes this exhausts connection pools and causes cascading failures. The correct pattern: (1) COMMIT the `authorized` record synchronously to Postgres first, (2) call the card network asynchronously or in a follow-up step, (3) UPDATE status to `captured` or `failed`. The idempotency key ensures retries are safe even if the card call fails and must be retried.
