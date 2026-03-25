#!/usr/bin/env python3
"""
Payment API with idempotency key support.
POST /charge  — idempotent payment; X-Idempotency-Key header required
GET  /charges — list all charges
GET  /health
"""
import os
import time
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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS charges (
            id              SERIAL PRIMARY KEY,
            idempotency_key TEXT UNIQUE NOT NULL,
            amount          INTEGER NOT NULL,
            currency        TEXT NOT NULL,
            customer_id     TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'success',
            response_body   TEXT NOT NULL,
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
        # Check if we've seen this idempotency key before
        cur.execute(
            "SELECT response_body FROM charges WHERE idempotency_key = %s",
            (idem_key,)
        )
        existing = cur.fetchone()
        if existing:
            # Return the original response — idempotent replay
            conn.close()
            return Response(
                existing["response_body"],
                status=200,
                content_type="application/json",
                headers={"X-Idempotent-Replayed": "true"}
            )

        # Process the charge (in production: call payment processor here)
        time.sleep(0.05)  # simulate processing
        charge_id = f"ch_{int(time.time() * 1000)}"
        response_data = {
            "id":           charge_id,
            "amount":       amount,
            "currency":     currency,
            "customer_id":  customer_id,
            "status":       "success",
            "idempotency_key": idem_key,
        }
        response_body = json.dumps(response_data)

        # Store the idempotency key + response atomically
        cur.execute(
            """
            INSERT INTO charges (idempotency_key, amount, currency, customer_id, status, response_body)
            VALUES (%s, %s, %s, %s, 'success', %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            (idem_key, amount, currency, customer_id, response_body)
        )
        inserted = cur.fetchone()
        conn.commit()

        if inserted is None:
            # Race condition: another request inserted first — return their response
            cur.execute(
                "SELECT response_body FROM charges WHERE idempotency_key = %s",
                (idem_key,)
            )
            row = cur.fetchone()
            conn.close()
            return Response(
                row["response_body"],
                status=200,
                content_type="application/json",
                headers={"X-Idempotent-Replayed": "true", "X-Race-Won": "false"}
            )

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
    cur.execute("SELECT id, idempotency_key, amount, currency, customer_id, status, created_at FROM charges ORDER BY id")
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


if __name__ == "__main__":
    init_db()
    print("Payment API starting on :8003")
    app.run(host="0.0.0.0", port=8003, threaded=True)
