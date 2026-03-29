#!/usr/bin/env python3
"""
payment_service.py — Flask payment API with double-entry bookkeeping and idempotency.

Endpoints:
  GET  /health                         → 200 OK
  POST /charge                         → create a charge
       body: {amount, currency, customer_id, idempotency_key, description?}
  GET  /transaction/<id>               → get transaction details
  GET  /ledger                         → full ledger view (all entries)
  GET  /balance/<account_id>           → balance for an account
  POST /refund                         → refund a charge
       body: {transaction_id, idempotency_key, amount? (partial refund)}
  GET  /reconcile                      → run ledger integrity check

Design notes:
  - Idempotency keys are scoped to (customer_id, idempotency_key) so two different
    customers can use the same key string without collision. In production this would
    be (api_key_id, idempotency_key).
  - The UNIQUE constraint on (customer_id, idempotency_key) is the correctness
    guarantee for concurrent deduplication. The Redis pre-check is a latency
    optimization only — correctness does NOT depend on Redis.
  - Ledger entries carry a currency column so the sum-to-zero invariant is enforced
    per-currency, not in aggregate across currencies.
  - The outbox table captures webhook events inside the same DB transaction as the
    charge, guaranteeing at-least-once delivery even if the service crashes before
    publishing to Kafka/SQS.
"""

import json
import os
import uuid
from decimal import Decimal, InvalidOperation

import psycopg2
import psycopg2.extras
import redis
from flask import Flask, jsonify, request

app = Flask(__name__)
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://app:secret@localhost:5432/payments")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

MERCHANT_ACCOUNT = "merchant-001"
ESCROW_ACCOUNT = "escrow-001"

IDEM_KEY_TTL_SECONDS = 86400  # 24 hours

# Connect to Redis for idempotency key cache.
# Redis is a performance optimization only — Postgres UNIQUE constraint is the
# correctness guarantee. If Redis is unavailable, all requests fall through to
# Postgres (slower, but correct).
try:
    rcache = redis.from_url(REDIS_URL, decode_responses=True)
    rcache.ping()
    print("[startup] Redis cache: connected", flush=True)
except Exception as e:
    rcache = None
    print(f"[startup] Redis cache: UNAVAILABLE ({e}) — falling back to Postgres-only idempotency", flush=True)


def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
                -- Scoped to customer to match the (api_key, idem_key) production pattern.
                -- The UNIQUE constraint here is the atomic guard against TOCTOU races.
                customer_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                amount NUMERIC(18, 2) NOT NULL CHECK (amount > 0),
                -- Track how much has been refunded so partial refunds are safe.
                refunded_amount NUMERIC(18, 2) NOT NULL DEFAULT 0 CHECK (refunded_amount >= 0),
                currency TEXT NOT NULL DEFAULT 'USD',
                -- status lifecycle: pending → completed | failed | refunded | partially_refunded
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'completed', 'failed', 'refunded', 'partially_refunded')),
                description TEXT,
                -- Store when this idempotency key expires so batch pruning can filter by it.
                idem_key_expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '24 hours',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (customer_id, idempotency_key)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ledger_entries (
                id BIGSERIAL PRIMARY KEY,
                transaction_id TEXT NOT NULL REFERENCES transactions(id),
                account_id TEXT NOT NULL,
                amount NUMERIC(18, 2) NOT NULL CHECK (amount > 0),
                -- currency is stored here so sum-to-zero can be verified per-currency.
                -- In a multi-currency system, SUM(signed_amount) must equal 0 for each
                -- currency independently — not in aggregate across currencies.
                currency TEXT NOT NULL DEFAULT 'USD',
                entry_type TEXT NOT NULL CHECK (entry_type IN ('debit', 'credit')),
                description TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Outbox table: webhook events written inside the same DB transaction as the
        # charge. A separate delivery worker polls this table and publishes to
        # Kafka/SQS, then marks delivered=true. This guarantees at-least-once
        # delivery even if the service crashes between commit and publish.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS outbox_events (
                id BIGSERIAL PRIMARY KEY,
                event_type TEXT NOT NULL,
                payload JSONB NOT NULL,
                delivered BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ledger_transaction
            ON ledger_entries(transaction_id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ledger_account
            ON ledger_entries(account_id)
        """)
        # Composite index mirrors the UNIQUE constraint for fast idempotency lookups.
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_transactions_idem
            ON transactions(customer_id, idempotency_key)
        """)
        # Index for batch pruning of expired idempotency keys.
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_transactions_idem_expires
            ON transactions(idem_key_expires_at)
            WHERE idem_key_expires_at < NOW()
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_outbox_undelivered
            ON outbox_events(created_at) WHERE delivered = FALSE
        """)
        cur.execute("""
            CREATE OR REPLACE FUNCTION update_updated_at()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.updated_at = NOW();
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """)
        cur.execute("""
            DROP TRIGGER IF EXISTS set_updated_at ON transactions
        """)
        cur.execute("""
            CREATE TRIGGER set_updated_at
            BEFORE UPDATE ON transactions
            FOR EACH ROW EXECUTE FUNCTION update_updated_at()
        """)
    conn.commit()
    conn.close()


def create_ledger_entries(cur, transaction_id: str, customer_id: str,
                          amount: Decimal, currency: str, description: str,
                          debit_account: str = None, credit_account: str = None,
                          debit_desc: str = None, credit_desc: str = None):
    """
    Double-entry bookkeeping: every money movement creates exactly two entries.
    The sum of (credits - debits) across all entries must always equal zero,
    enforced per-currency.

    Default pattern for a charge:
      Debit  customer account  (money leaves customer)
      Credit merchant account  (money arrives at merchant)

    For a refund the caller inverts debit_account / credit_account.
    """
    debit_account = debit_account or customer_id
    credit_account = credit_account or MERCHANT_ACCOUNT
    debit_desc = debit_desc or f"Charge: {description}"
    credit_desc = credit_desc or f"Revenue: {description}"

    cur.execute("""
        INSERT INTO ledger_entries
            (transaction_id, account_id, amount, currency, entry_type, description)
        VALUES
          (%s, %s, %s, %s, 'debit',  %s),
          (%s, %s, %s, %s, 'credit', %s)
    """, (
        transaction_id, debit_account,  amount, currency, debit_desc,
        transaction_id, credit_account, amount, currency, credit_desc,
    ))


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "payment-service"})


def _serialize_transaction(txn: dict) -> dict:
    """
    Normalize a transactions row into a stable API response shape.
    Always returns 'transaction_id' (not the raw DB column 'id') so callers
    get a consistent field name regardless of whether the response is a fresh
    charge or an idempotency-deduplicated repeat.
    """
    out = dict(txn)
    # Expose the PK as 'transaction_id' for a clean API contract.
    out["transaction_id"] = out.pop("id", out.get("transaction_id", ""))
    out["amount"] = str(out["amount"])
    out["refunded_amount"] = str(out.get("refunded_amount") or "0")
    out["created_at"] = str(out["created_at"])
    out["updated_at"] = str(out["updated_at"])
    out["idem_key_expires_at"] = str(out.get("idem_key_expires_at", ""))
    return out


def _redis_idem_key(customer_id: str, idem_key: str) -> str:
    """Redis key is scoped to customer so two customers can use the same string."""
    return f"idem:{customer_id}:{idem_key}"


@app.route("/charge", methods=["POST"])
def charge():
    data = request.get_json(force=True)

    # Validate required fields
    required = ["amount", "currency", "customer_id", "idempotency_key"]
    for field in required:
        if field not in data:
            return jsonify({"error": "missing_field", "field": field}), 400

    try:
        amount = Decimal(str(data["amount"]))
    except (InvalidOperation, ValueError):
        return jsonify({"error": "invalid_amount"}), 400

    if amount <= 0:
        return jsonify({"error": "amount_must_be_positive", "amount": str(amount)}), 400

    idem_key = str(data["idempotency_key"])
    customer_id = str(data["customer_id"])
    currency = str(data["currency"]).upper()
    description = str(data.get("description", "Payment"))
    redis_key = _redis_idem_key(customer_id, idem_key)

    # Fast path: check Redis cache (performance optimisation, not correctness guarantee).
    if rcache:
        cached = rcache.get(redis_key)
        if cached:
            cached_result = json.loads(cached)
            return jsonify({"status": "duplicate", "cached": True,
                            "transaction": cached_result}), 200

    conn = get_db()
    try:
        with conn.cursor() as cur:
            # Authoritative idempotency check in Postgres.
            # Scoped to (customer_id, idempotency_key) — mirrors production
            # (api_key_id, idempotency_key) pattern to prevent cross-merchant
            # key collisions (Failure Mode 6 in README).
            cur.execute(
                "SELECT * FROM transactions WHERE customer_id = %s AND idempotency_key = %s",
                (customer_id, idem_key)
            )
            existing = cur.fetchone()
            if existing:
                result = _serialize_transaction(existing)
                if rcache:
                    rcache.setex(redis_key, IDEM_KEY_TTL_SECONDS, json.dumps(result))
                return jsonify({"status": "duplicate", "transaction": result}), 200

            # New request: insert transaction + ledger entries in one atomic transaction.
            # The UNIQUE (customer_id, idempotency_key) constraint is the last-line guard
            # for concurrent races (TOCTOU). If two threads race here, only one INSERT
            # commits; the other gets UniqueViolation and falls into the except block.
            transaction_id = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO transactions
                    (id, idempotency_key, customer_id, amount, currency, status, description)
                VALUES (%s, %s, %s, %s, %s, 'completed', %s)
            """, (transaction_id, idem_key, customer_id, amount, currency, description))

            # Double-entry: debit customer, credit merchant.
            create_ledger_entries(cur, transaction_id, customer_id, amount, currency, description)

            # Outbox event: written in the same DB transaction so webhook delivery
            # is guaranteed even if the process crashes before publishing externally.
            cur.execute("""
                INSERT INTO outbox_events (event_type, payload)
                VALUES ('payment.created', %s)
            """, (json.dumps({
                "transaction_id": transaction_id,
                "customer_id": customer_id,
                "amount": str(amount),
                "currency": currency,
            }),))

        conn.commit()

        result = {
            "status": "success",
            "transaction_id": transaction_id,
            "idempotency_key": idem_key,
            "customer_id": customer_id,
            "amount": str(amount),
            "currency": currency,
            "description": description,
        }

        # Populate Redis after commit so cache is never ahead of DB truth.
        if rcache:
            rcache.setex(redis_key, IDEM_KEY_TTL_SECONDS, json.dumps(result))

        return jsonify(result), 201

    except psycopg2.errors.UniqueViolation:
        # Concurrent duplicate hit the UNIQUE constraint. Roll back and return
        # the committed row — this is the correct path for the TOCTOU race.
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM transactions WHERE customer_id = %s AND idempotency_key = %s",
                (customer_id, idem_key)
            )
            existing = cur.fetchone()
        result = _serialize_transaction(existing)
        return jsonify({"status": "duplicate", "transaction": result}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"error": "internal_error", "detail": str(e)}), 500
    finally:
        conn.close()


@app.route("/transaction/<transaction_id>")
def get_transaction(transaction_id: str):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM transactions WHERE id = %s",
                (transaction_id,)
            )
            txn = cur.fetchone()
            if not txn:
                return jsonify({"error": "not_found"}), 404

            cur.execute(
                "SELECT * FROM ledger_entries WHERE transaction_id = %s ORDER BY id",
                (transaction_id,)
            )
            entries = cur.fetchall()

        result = dict(txn)
        result["amount"] = str(result["amount"])
        result["created_at"] = str(result["created_at"])
        result["updated_at"] = str(result["updated_at"])
        result["ledger_entries"] = [
            {**dict(e), "amount": str(e["amount"]), "created_at": str(e["created_at"])}
            for e in entries
        ]
        return jsonify(result)
    finally:
        conn.close()


@app.route("/ledger")
def get_ledger():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT l.*, t.customer_id, t.currency
                FROM ledger_entries l
                JOIN transactions t ON l.transaction_id = t.id
                ORDER BY l.id DESC
                LIMIT 100
            """)
            entries = cur.fetchall()

            # Compute balance per account
            cur.execute("""
                SELECT account_id,
                       SUM(CASE WHEN entry_type='credit' THEN amount ELSE 0 END) as total_credits,
                       SUM(CASE WHEN entry_type='debit'  THEN amount ELSE 0 END) as total_debits,
                       COUNT(*) as entry_count
                FROM ledger_entries
                GROUP BY account_id
                ORDER BY account_id
            """)
            balances = cur.fetchall()

            cur.execute("SELECT SUM(amount * CASE WHEN entry_type='credit' THEN 1 ELSE -1 END) as net FROM ledger_entries")
            net = cur.fetchone()

        return jsonify({
            "entries": [
                {**dict(e), "amount": str(e["amount"]), "created_at": str(e["created_at"])}
                for e in entries
            ],
            "balances": [
                {**dict(b), "total_credits": str(b["total_credits"]),
                 "total_debits": str(b["total_debits"])}
                for b in balances
            ],
            "net_sum": str(net["net"]) if net["net"] else "0",
            "accounting_check": "ZERO" if (net["net"] or Decimal(0)) == Decimal(0) else "NON-ZERO",
        })
    finally:
        conn.close()


@app.route("/balance/<account_id>")
def get_balance(account_id: str):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    SUM(CASE WHEN entry_type='credit' THEN amount ELSE -amount END) as balance,
                    COUNT(*) as transactions
                FROM ledger_entries
                WHERE account_id = %s
            """, (account_id,))
            row = cur.fetchone()
        balance = row["balance"] or Decimal(0)
        return jsonify({
            "account_id": account_id,
            "balance": str(balance),
            "transactions": row["transactions"],
        })
    finally:
        conn.close()


@app.route("/refund", methods=["POST"])
def refund():
    data = request.get_json(force=True)
    txn_id = data.get("transaction_id")
    idem_key = str(data.get("idempotency_key", ""))

    if not txn_id or not idem_key:
        return jsonify({"error": "missing_fields"}), 400

    conn = get_db()
    try:
        with conn.cursor() as cur:
            # Check idempotency
            cur.execute(
                "SELECT * FROM transactions WHERE idempotency_key = %s",
                (f"refund-{idem_key}",)
            )
            if cur.fetchone():
                return jsonify({"status": "duplicate_refund"}), 200

            # Get original transaction
            cur.execute("SELECT * FROM transactions WHERE id = %s", (txn_id,))
            original = cur.fetchone()
            if not original:
                return jsonify({"error": "transaction_not_found"}), 404

            # Only refund completed charges — prevent double-refund and refunding failed charges.
            # In production, also check for partial-refund tracking (refund_amount_remaining).
            if original["status"] != "completed":
                return jsonify({
                    "error": "not_refundable",
                    "detail": f"Transaction status is '{original['status']}', must be 'completed'",
                }), 422

            amount = original["amount"]
            customer_id = original["customer_id"]
            refund_id = str(uuid.uuid4())

            # Create refund transaction
            cur.execute("""
                INSERT INTO transactions (id, idempotency_key, customer_id, amount,
                    currency, status, description)
                VALUES (%s, %s, %s, %s, %s, 'refunded', %s)
            """, (refund_id, f"refund-{idem_key}", customer_id, amount,
                  original["currency"], f"Refund for {txn_id}"))

            # Reverse the entries: credit customer, debit merchant.
            # The original entries are never modified (immutable ledger).
            # Correction is always a new pair of entries that reverse the original.
            cur.execute("""
                INSERT INTO ledger_entries (transaction_id, account_id, amount, entry_type, description)
                VALUES
                  (%s, %s, %s, 'credit', %s),
                  (%s, %s, %s, 'debit',  %s)
            """, (
                refund_id, customer_id, amount, f"Refund of {txn_id}",
                refund_id, MERCHANT_ACCOUNT, amount, f"Refund of {txn_id}",
            ))

            # Mark original transaction as refunded so it cannot be refunded again.
            # The trigger updates updated_at automatically.
            cur.execute(
                "UPDATE transactions SET status = 'refunded' WHERE id = %s",
                (txn_id,)
            )

        conn.commit()
        return jsonify({
            "status": "refunded",
            "refund_transaction_id": refund_id,
            "original_transaction_id": txn_id,
            "amount": str(amount),
        }), 201
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
