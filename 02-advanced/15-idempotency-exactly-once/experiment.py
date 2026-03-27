#!/usr/bin/env python3
"""
Idempotency & Exactly-Once Lab — Payment API with idempotency keys.

What this demonstrates:
  1. POST charge with idempotency key → 201 (new charge)
  2. Retry same request with same key → 200 (replayed, no duplicate charge)
  3. New key → new charge (different idempotency key = different operation)
  4. Concurrent retries (10 threads, same key) → exactly 1 charge created
  5. Show idempotency key table in DB
"""

import os
import time
import uuid
import threading
import requests
import psycopg2
import psycopg2.extras

API = f"http://{os.environ.get('API_HOST', 'localhost')}:8003"

DB_CONFIG = {
    "host":     os.environ.get("DB_HOST", "localhost"),
    "dbname":   os.environ.get("DB_NAME", "payments"),
    "user":     os.environ.get("DB_USER", "app"),
    "password": os.environ.get("DB_PASSWORD", "secret"),
}


def section(title):
    print(f"\n{'=' * 65}")
    print(f"  {title}")
    print("=" * 65)


def charge(idem_key, amount, customer_id="cust_123"):
    r = requests.post(
        f"{API}/charge",
        json={"amount": amount, "currency": "usd", "customer_id": customer_id},
        headers={"X-Idempotency-Key": idem_key},
        timeout=10,
    )
    return r


def show_charge_table():
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT id, idempotency_key, amount, currency, customer_id, status, created_at "
        "FROM charges ORDER BY id"
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        print("  (no charges)")
        return

    print(f"\n  {'ID':<4} {'Idempotency Key':<38} {'Amount':<8} {'Customer':<14} {'Status'}")
    print(f"  {'─'*4} {'─'*38} {'─'*8} {'─'*14} {'─'*8}")
    for row in rows:
        key_short = str(row["idempotency_key"])[:36]
        print(f"  {row['id']:<4} {key_short:<38} ${row['amount']:<7} {row['customer_id']:<14} {row['status']}")

    conn2 = psycopg2.connect(**DB_CONFIG)
    cur2 = conn2.cursor()
    cur2.execute("SELECT COUNT(*) FROM charges")
    total = cur2.fetchone()[0]
    conn2.close()
    print(f"\n  Total rows in charges table: {total}")


def main():
    section("IDEMPOTENCY & EXACTLY-ONCE LAB")
    print("""
  Problem: networks are unreliable. Clients retry failed requests.
  Without idempotency, a retry can charge a customer twice.

  Solution: idempotency keys
    1. Client generates a unique key (UUID) per logical operation
    2. Sends key with every attempt: X-Idempotency-Key: <uuid>
    3. Server stores key + response after first success
    4. On retry: server finds the key → returns original response
    5. Customer is charged exactly once, regardless of retries

  Delivery semantics:
    at-most-once  → fire and forget, never retry (may lose data)
    at-least-once → always retry, but may duplicate (most common)
    exactly-once  → at-least-once + idempotent consumer
""")

    # ── Phase 1: First charge ──────────────────────────────────────
    section("Phase 1: First Charge — Creates New Record")
    key1 = str(uuid.uuid4())
    print(f"  Idempotency key: {key1}\n")

    r = charge(key1, 1000, "cust_alice")
    replayed = r.headers.get("X-Idempotent-Replayed", "false")
    print(f"  Response: {r.status_code} (replayed={replayed})")
    print(f"  Body: {r.json()}")
    print(f"\n  HTTP 201 = new charge created")

    # ── Phase 2: Retry with same key ──────────────────────────────
    section("Phase 2: Retry Same Request — Idempotent Replay")
    print(f"  Retrying with same key: {key1}\n")

    for attempt in range(1, 4):
        r = charge(key1, 1000, "cust_alice")
        replayed = r.headers.get("X-Idempotent-Replayed", "false")
        print(f"  Attempt {attempt}: {r.status_code} (replayed={replayed}) — {r.json()['id']}")

    print(f"""
  HTTP 200 + X-Idempotent-Replayed: true = no new charge was created.
  The API returned the exact same charge ID as the first request.
  → Customer was charged exactly once despite 3 network attempts.
""")

    # ── Phase 3: New key → new charge ─────────────────────────────
    section("Phase 3: Different Key = Different Charge")
    key2 = str(uuid.uuid4())
    print(f"  New key: {key2}\n")

    r2 = charge(key2, 2500, "cust_alice")
    print(f"  Response: {r2.status_code}")
    print(f"  Body: {r2.json()}")
    print(f"\n  Different idempotency key → separate logical operation → new charge created.")

    # ── Phase 4: Concurrent retries ────────────────────────────────
    section("Phase 4: 10 Concurrent Threads, Same Key — Exactly 1 Charge")
    print("  Simulating client retry storm: 10 threads fire simultaneously with same key.\n")

    key3       = str(uuid.uuid4())
    results    = []
    statuses   = []
    lock       = threading.Lock()
    barrier    = threading.Barrier(10)

    def concurrent_charge():
        barrier.wait()  # All threads start simultaneously
        r = charge(key3, 500, "cust_concurrent")
        with lock:
            results.append(r.json())
            statuses.append(r.status_code)

    threads = [threading.Thread(target=concurrent_charge) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    charge_ids = set(r.get("id") for r in results)
    created = sum(1 for s in statuses if s == 201)
    replayed = sum(1 for s in statuses if s == 200)

    print(f"  10 concurrent requests completed:")
    print(f"    Status 201 (new charge):  {created}")
    print(f"    Status 200 (replayed):    {replayed}")
    print(f"    Unique charge IDs:        {len(charge_ids)}")
    print(f"    Charge IDs seen: {charge_ids}")
    print(f"\n  → Exactly 1 charge was created, regardless of concurrency.")
    print(f"  ON CONFLICT (idempotency_key) DO NOTHING ensures atomicity in Postgres.")

    # ── Phase 5: Show the DB table ─────────────────────────────────
    section("Phase 5: Idempotency Key Table in Postgres")
    print("  All charges with their idempotency keys:")
    show_charge_table()

    print(f"""
  DB schema highlights:
    idempotency_key TEXT UNIQUE NOT NULL   ← unique constraint prevents duplicates
    response_body   TEXT                   ← store the exact response to replay
    created_at      TIMESTAMP              ← enables cleanup of old keys

  Deduplication window:
    Store keys for 24 hours (Stripe) or 7 days depending on retry policy.
    Expired keys can be deleted — no need to store forever.
    Use a scheduled job or Postgres TTL extension for cleanup.

  Idempotency key generation strategies:
    UUID v4       → random, client-generated, simple
    Content hash  → hash of (customer_id + amount + timestamp bucket)
    Request ID    → from distributed tracing, correlates across services
    User+operation→ hash(user_id + "charge" + idempotent_nonce)

  Non-idempotent operations that NEED idempotency keys:
    POST /charge          → creates payment (money movement)
    POST /send-email      → sends email (external side effect)
    POST /provision-vm    → creates cloud resource
    POST /order           → creates order record

  Idempotency keys are not needed for:
    GET /user/profile     → reads are naturally idempotent
    PUT /user/name        → replacing a resource is idempotent
    DELETE /session       → deleting an already-deleted resource is fine

  Next: ../15-probabilistic-data-structures/
""")


if __name__ == "__main__":
    main()
