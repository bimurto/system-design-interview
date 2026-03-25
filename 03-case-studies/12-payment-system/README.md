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

## Requirements

### Functional
- Charge a customer's payment method and credit the merchant
- Support idempotent retries: same idempotency_key → same result
- Maintain a double-entry ledger for all money movements
- Issue refunds (with their own idempotency)
- Query transaction status by ID

### Non-Functional
- Exactly-once charge semantics (no duplicates, no lost charges)
- ACID transactions: partial state must never persist
- Ledger sum must always equal zero (accounting invariant)
- Sub-500ms P99 for charge endpoint
- 99.99% availability

### Capacity Estimation

| Metric | Calculation | Result |
|---|---|---|
| Transactions/second | 100M/day / 86,400s | ~1,160 TPS |
| Peak TPS | 3× average | ~3,500 TPS |
| Ledger entries/TPS | 2 per transaction (debit + credit) | ~7,000 writes/s |
| Postgres write throughput | 7K rows/s | 1 node (handles ~50K writes/s) |
| Idempotency key store | 100M keys/day × 100B each | ~10GB/day (pruned daily) |
| Transaction table size | 1B rows × 500B/row | ~500GB/year |

---

## High-Level Architecture

```
  Client (Merchant SDK)
       │  POST /charge  {amount, currency, customer_id, idempotency_key}
       ▼
  ┌────────────────────────────────────────────────────────────┐
  │              Payment Service                                │
  │                                                            │
  │  1. Validate request (amount > 0, valid currency, etc.)    │
  │  2. Check idempotency_key (Redis cache → Postgres)         │
  │     → If duplicate: return original response               │
  │     → If new: proceed                                      │
  │  3. BEGIN TRANSACTION                                       │
  │     a. INSERT transactions (id, idem_key, amount, status)  │
  │     b. INSERT ledger_entries (debit customer)              │
  │     c. INSERT ledger_entries (credit merchant)             │
  │     d. [optional] Call card network (Stripe → Visa/MC)     │
  │  4. COMMIT                                                  │
  │  5. Cache idempotency_key in Redis (24h TTL)               │
  │  6. Return transaction result                               │
  └───────────────────────────────┬────────────────────────────┘
                                  │
                    ┌─────────────▼──────────────┐
                    │         Postgres            │
                    │  transactions table         │
                    │  ledger_entries table       │
                    │  (ACID, UNIQUE constraints) │
                    └─────────────────────────────┘
```

**Postgres** is the source of truth. ACID transactions ensure that either all three operations (insert transaction + 2 ledger entries) succeed, or none do. No partial state can persist.

**UNIQUE constraint** on `idempotency_key` in the transactions table is the last-line defense against concurrent duplicates. If two requests race with the same key, Postgres's unique index ensures only one INSERT succeeds. The loser gets a `UniqueViolation` exception, fetches and returns the winner's result.

**Redis** caches idempotency keys for fast duplicate detection on the hot path (< 1ms vs ~5ms for a Postgres SELECT). Redis is a performance optimization, not correctness guarantee — Postgres is authoritative.

---

## Deep Dives

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

### 5. Fraud Detection (Brief Overview)

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
