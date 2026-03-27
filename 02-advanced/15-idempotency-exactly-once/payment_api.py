#!/usr/bin/env python3
"""
Payment API with idempotency key support.

POST /charge  — idempotent payment; X-Idempotency-Key header required
GET  /charges — list all charges
GET  /health

Design notes
------------
The idempotency implementation uses a two-phase row lifecycle:

  Phase 1 – claim:  INSERT with status='processing', no response_body yet.
                    The UNIQUE constraint on idempotency_key makes this atomic.
                    Concurrent requests that lose the INSERT race see the key
                    already exists and return HTTP 409 (request in flight), just
                    like Stripe does — callers should retry after a short back-off.

  Phase 2 – commit: After the payment processor succeeds, UPDATE the row to
                    status='success' and store the response_body.  On retry the
                    server checks status: 'success' → replay; 'processing' → 409.

Why not SELECT-then-INSERT?
  A plain SELECT + INSERT has a TOCTOU window: two threads can both find no row,
  both try to INSERT, one wins (ON CONFLICT) and the other needs a second SELECT.
  This works, but the race loser sees status='processing' on the first retry and
  must distinguish "someone else is working on it" from "already done".  The
  two-phase approach makes the state machine explicit and mirrors real production
  systems (Stripe, Adyen) that return 409 while a request is in flight.

Parameter fingerprinting
  The stored (amount, currency, customer_id) are compared against every retry.
  A mismatch on a reused key returns HTTP 422 — this is a client bug and must
  not silently serve the wrong response.
"""
import os
import time
import uuid
import json
import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

DB_CONFIG = {
    "host":     os.environ.get("DB_HOST", "localhost"),
    "dbname":   os.environ.get("DB_NAME", "payments"),
    "user":     os.environ.get("DB_USER", "app"),
    "password": os.environ.get("DB_PASSWORD", "secret"),
}


def get_db():
    return psycopg2.connect(**DB_CONFIG)


def init_db():
    conn = get_db()
    cur = conn.cursor()
    # Two-phase schema:
    #   status = 'processing' — claimed, payment not yet confirmed
    #   status = 'success'    — payment confirmed, response_body is populated
    # response_body is nullable to allow the 'processing' phase row.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS charges (
            id              SERIAL PRIMARY KEY,
            idempotency_key TEXT UNIQUE NOT NULL,
            amount          INTEGER NOT NULL,
            currency        TEXT NOT NULL,
            customer_id     TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'processing',
            response_body   TEXT,
            created_at      TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.commit()
    cur.close()
    conn.close()
    print("Database initialised.")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/charge", methods=["POST"])
def charge():
    idem_key = request.headers.get("X-Idempotency-Key")
    if not idem_key:
        return jsonify({"error": "X-Idempotency-Key header is required"}), 400

    data = request.get_json() or {}
    amount      = data.get("amount")
    currency    = data.get("currency", "usd")
    customer_id = data.get("customer_id", "cust_unknown")

    if amount is None:
        return jsonify({"error": "amount is required"}), 400

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        # ── Phase 1: atomically claim the idempotency key ──────────────────
        # INSERT with status='processing'. The UNIQUE constraint ensures only
        # one concurrent request wins. No SELECT before the INSERT — that would
        # introduce a TOCTOU race window.
        cur.execute(
            """
            INSERT INTO charges (idempotency_key, amount, currency, customer_id, status)
            VALUES (%s, %s, %s, %s, 'processing')
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            (idem_key, amount, currency, customer_id)
        )
        inserted = cur.fetchone()
        conn.commit()

        if inserted is None:
            # Key already exists — fetch the current row to determine state
            cur.execute(
                "SELECT amount, currency, customer_id, status, response_body "
                "FROM charges WHERE idempotency_key = %s",
                (idem_key,)
            )
            existing = cur.fetchone()
            conn.close()

            if existing is None:
                # Extremely unlikely: row disappeared between INSERT and SELECT
                return jsonify({"error": "transient conflict, please retry"}), 503

            # ── Parameter fingerprint check ─────────────────────────────────
            # If the caller is reusing the key with different parameters it is
            # a client bug.  Return 422 so the bug is visible rather than
            # silently serving the wrong response.
            if (existing["amount"] != amount
                    or existing["currency"] != currency
                    or existing["customer_id"] != customer_id):
                return jsonify({
                    "error": "idempotency_key reused with different parameters",
                    "stored": {
                        "amount":      existing["amount"],
                        "currency":    existing["currency"],
                        "customer_id": existing["customer_id"],
                    },
                    "received": {
                        "amount":      amount,
                        "currency":    currency,
                        "customer_id": customer_id,
                    }
                }), 422

            if existing["status"] == "processing":
                # Another request is mid-flight for the same key.
                # Return 409 so the client backs off and retries later —
                # identical to Stripe's behaviour for in-flight requests.
                return jsonify({
                    "error": "request in flight, retry after a short delay",
                    "idempotency_key": idem_key
                }), 409

            # status == 'success' — idempotent replay
            return Response(
                existing["response_body"],
                status=200,
                content_type="application/json",
                headers={"X-Idempotent-Replayed": "true"}
            )

        # ── Phase 2: process the charge, then commit the response ───────────
        # In production: call the payment processor (Stripe, Braintree) here.
        # If the processor call fails, we leave the row as 'processing' so
        # the next retry can attempt Phase 1 again (the INSERT will conflict,
        # see the 409 path above — caller retries after back-off, our
        # background job or next retry can reset 'processing' rows that are
        # stale).  For this demo we always succeed.
        time.sleep(0.05)  # simulate network call to payment processor
        charge_id = f"ch_{uuid.uuid4().hex[:12]}"  # UUID-based, no collision risk

        response_data = {
            "id":             charge_id,
            "amount":         amount,
            "currency":       currency,
            "customer_id":    customer_id,
            "status":         "success",
            "idempotency_key": idem_key,
        }
        response_body = json.dumps(response_data)

        # Commit: update status to 'success' and persist the response body.
        # Future retries will hit the 'success' branch above and replay this.
        cur.execute(
            "UPDATE charges SET status='success', response_body=%s "
            "WHERE idempotency_key=%s",
            (response_body, idem_key)
        )
        conn.commit()
        conn.close()

        return Response(response_body, status=201, content_type="application/json")

    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"error": str(e)}), 500


@app.route("/charges")
def list_charges():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT id, idempotency_key, amount, currency, customer_id, status, created_at "
        "FROM charges ORDER BY id"
    )
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


if __name__ == "__main__":
    init_db()
    print("Payment API starting on :8003")
    app.run(host="0.0.0.0", port=8003, threaded=True)
