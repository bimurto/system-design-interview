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
       body: {transaction_id, idempotency_key}
"""

import json
import os
import time
import uuid
from decimal import Decimal

import psycopg2
import psycopg2.extras
import redis
from flask import Flask, jsonify, request

app = Flask(__name__)
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://app:secret@localhost:5432/payments")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

MERCHANT_ACCOUNT = "merchant-001"
REVENUE_ACCOUNT = "revenue-001"
ESCROW_ACCOUNT = "escrow-001"

# Connect to Redis for idempotency key cache
try:
    rcache = redis.from_url(REDIS_URL, decode_responses=True)
    rcache.ping()
except Exception:
    rcache = None


def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
                idempotency_key TEXT UNIQUE NOT NULL,
                customer_id TEXT NOT NULL,
                amount NUMERIC(18, 2) NOT NULL CHECK (amount > 0),
                currency TEXT NOT NULL DEFAULT 'USD',
                status TEXT NOT NULL DEFAULT 'pending',
                description TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ledger_entries (
                id BIGSERIAL PRIMARY KEY,
                transaction_id TEXT NOT NULL REFERENCES transactions(id),
                account_id TEXT NOT NULL,
                amount NUMERIC(18, 2) NOT NULL,
                entry_type TEXT NOT NULL CHECK (entry_type IN ('debit', 'credit')),
                description TEXT,
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
    conn.commit()
    conn.close()


def create_ledger_entries(cur, transaction_id: str, customer_id: str,
                          amount: Decimal, description: str):
    """
    Double-entry bookkeeping: every charge creates two entries.
    Customer account: debit (money leaves customer)
    Merchant account: credit (money arrives at merchant)
    The sum of all ledger entries must always be zero.
    """
    cur.execute("""
        INSERT INTO ledger_entries (transaction_id, account_id, amount, entry_type, description)
        VALUES
          (%s, %s, %s, 'debit',  %s),
          (%s, %s, %s, 'credit', %s)
    """, (
        transaction_id, customer_id, amount, f"Charge: {description}",
        transaction_id, MERCHANT_ACCOUNT, amount, f"Revenue: {description}",
    ))


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "payment-service"})


@app.route("/charge", methods=["POST"])
def charge():
    data = request.get_json(force=True)

    # Validate required fields
    required = ["amount", "currency", "customer_id", "idempotency_key"]
    for field in required:
        if field not in data:
            return jsonify({"error": f"missing_field", "field": field}), 400

    try:
        amount = Decimal(str(data["amount"]))
    except Exception:
        return jsonify({"error": "invalid_amount"}), 400

    if amount <= 0:
        return jsonify({"error": "amount_must_be_positive", "amount": str(amount)}), 400

    idem_key = str(data["idempotency_key"])
    customer_id = str(data["customer_id"])
    currency = str(data["currency"]).upper()
    description = str(data.get("description", "Payment"))

    # Check Redis idempotency cache first (fast path)
    if rcache:
        cached = rcache.get(f"idem:{idem_key}")
        if cached:
            return jsonify({"status": "duplicate", "cached": True,
                            "transaction": json.loads(cached)}), 200

    conn = get_db()
    try:
        with conn.cursor() as cur:
            # Check idempotency in DB (authoritative)
            cur.execute(
                "SELECT * FROM transactions WHERE idempotency_key = %s",
                (idem_key,)
            )
            existing = cur.fetchone()
            if existing:
                txn = dict(existing)
                txn["amount"] = str(txn["amount"])
                txn["created_at"] = str(txn["created_at"])
                txn["updated_at"] = str(txn["updated_at"])
                # Cache it for future fast lookups
                if rcache:
                    rcache.setex(f"idem:{idem_key}", 86400, json.dumps(txn))
                return jsonify({"status": "duplicate", "transaction": txn}), 200

            # Create transaction record
            transaction_id = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO transactions (id, idempotency_key, customer_id, amount,
                    currency, status, description)
                VALUES (%s, %s, %s, %s, %s, 'completed', %s)
            """, (transaction_id, idem_key, customer_id, amount, currency, description))

            # Create double-entry ledger entries
            create_ledger_entries(cur, transaction_id, customer_id, amount, description)

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

        # Cache in Redis for 24h
        if rcache:
            rcache.setex(f"idem:{idem_key}", 86400, json.dumps(result))

        return jsonify(result), 201

    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        # Concurrent duplicate — fetch and return existing
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM transactions WHERE idempotency_key = %s",
                (idem_key,)
            )
            existing = cur.fetchone()
        txn = dict(existing)
        txn["amount"] = str(txn["amount"])
        txn["created_at"] = str(txn["created_at"])
        txn["updated_at"] = str(txn["updated_at"])
        return jsonify({"status": "duplicate", "transaction": txn}), 200
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

            # Reverse the entries: credit customer, debit merchant
            cur.execute("""
                INSERT INTO ledger_entries (transaction_id, account_id, amount, entry_type, description)
                VALUES
                  (%s, %s, %s, 'credit', %s),
                  (%s, %s, %s, 'debit',  %s)
            """, (
                refund_id, customer_id, amount, f"Refund of {txn_id}",
                refund_id, MERCHANT_ACCOUNT, amount, f"Refund of {txn_id}",
            ))

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
