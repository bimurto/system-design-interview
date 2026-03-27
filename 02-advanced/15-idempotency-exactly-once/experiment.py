#!/usr/bin/env python3
"""
Idempotency & Exactly-Once Lab — Payment API with idempotency keys.

What this demonstrates:
  1. POST charge with idempotency key → 201 (new charge created)
  2. Retry same request with same key → 200 + X-Idempotent-Replayed: true
  3. Different key → new charge (separate logical operation)
  4. Concurrent retries (10 threads, same key) → exactly 1 charge created
  5. Parameter mismatch on reused key → 422 (client bug detection)
  6. DB table: every row, idempotency key, status lifecycle
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


def charge(idem_key, amount, customer_id="cust_123", currency="usd"):
    r = requests.post(
        f"{API}/charge",
        json={"amount": amount, "currency": currency, "customer_id": customer_id},
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

    print(f"\n  {'ID':<4} {'Idempotency Key':<38} {'Amt':>6} {'Customer':<16} {'Status'}")
    print(f"  {'─'*4} {'─'*38} {'─'*6} {'─'*16} {'─'*10}")
    for row in rows:
        key_short = str(row["idempotency_key"])[:36]
        print(
            f"  {row['id']:<4} {key_short:<38} "
            f"${row['amount']:<5} {row['customer_id']:<16} {row['status']}"
        )

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
    3. Server atomically claims the key (INSERT, status='processing')
    4. After payment processor succeeds, commits response (status='success')
    5. On retry: server finds key with status='success' → replays response
    6. Concurrent in-flight retries see status='processing' → HTTP 409

  Delivery semantics:
    at-most-once  → fire and forget, never retry (may lose data)
    at-least-once → always retry, but may duplicate (most common)
    exactly-once  → at-least-once + idempotent consumer
""")

    # ── Phase 1: First charge ──────────────────────────────────────────────
    section("Phase 1: First Charge — Creates New Record")
    key1 = str(uuid.uuid4())
    print(f"  Idempotency key: {key1}\n")

    r = charge(key1, 1000, "cust_alice")
    replayed = r.headers.get("X-Idempotent-Replayed", "false")
    print(f"  Response: HTTP {r.status_code}  (X-Idempotent-Replayed: {replayed})")
    body = r.json()
    print(f"  Charge ID: {body['id']}")
    print(f"  Amount:    ${body['amount']}")
    print(f"\n  HTTP 201 = new charge created and committed to DB.")

    # ── Phase 2: Retry with same key ──────────────────────────────────────
    section("Phase 2: Retry Same Request — Idempotent Replay")
    print(f"  Retrying with SAME key: {key1}\n")

    original_id = body["id"]
    for attempt in range(1, 4):
        r = charge(key1, 1000, "cust_alice")
        replayed = r.headers.get("X-Idempotent-Replayed", "false")
        returned_id = r.json()["id"]
        match = "SAME" if returned_id == original_id else "DIFFERENT (!)"
        print(
            f"  Attempt {attempt}: HTTP {r.status_code}  "
            f"X-Idempotent-Replayed: {replayed}  "
            f"Charge ID: {returned_id} [{match}]"
        )

    print(f"""
  HTTP 200 + X-Idempotent-Replayed: true = no new charge was created.
  All retries returned the exact same charge ID as the first request.
  → Customer charged exactly once despite 3 additional network round-trips.
""")

    # ── Phase 3: New key → new charge ─────────────────────────────────────
    section("Phase 3: Different Key = Different Charge")
    key2 = str(uuid.uuid4())
    print(f"  New key: {key2}\n")

    r2 = charge(key2, 2500, "cust_alice")
    print(f"  Response: HTTP {r2.status_code}")
    print(f"  Charge ID: {r2.json()['id']}")
    print(f"\n  Different idempotency key → separate logical operation → new charge.")
    print(f"  (This is correct: two different $10 and $25 charges for the same customer.)")

    # ── Phase 4: Concurrent retries ───────────────────────────────────────
    section("Phase 4: 10 Concurrent Threads, Same Key — Exactly 1 Charge")
    print("  Simulating retry storm: 10 threads fire simultaneously with the same key.")
    print("  The DB UNIQUE constraint on idempotency_key is the atomic gate.\n")

    key3     = str(uuid.uuid4())
    results  = []
    lock     = threading.Lock()
    barrier  = threading.Barrier(10)

    def concurrent_charge():
        barrier.wait()  # all threads start at the same instant
        r = charge(key3, 500, "cust_concurrent")
        with lock:
            results.append({
                "status": r.status_code,
                "id":     r.json().get("id"),
                "replayed": r.headers.get("X-Idempotent-Replayed", "false"),
            })

    threads = [threading.Thread(target=concurrent_charge) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses   = [res["status"] for res in results]
    charge_ids = set(res["id"] for res in results if res["id"])
    created    = sum(1 for s in statuses if s == 201)
    replayed   = sum(1 for s in statuses if s == 200)
    in_flight  = sum(1 for s in statuses if s == 409)

    print(f"  10 concurrent requests completed:")
    print(f"    HTTP 201 (new charge created):   {created:>2}")
    print(f"    HTTP 200 (idempotent replay):    {replayed:>2}")
    print(f"    HTTP 409 (request in flight):    {in_flight:>2}")
    print(f"    Unique charge IDs in responses:  {len(charge_ids)}")
    print(f"    Charge IDs seen: {charge_ids}")
    print(f"""
  Exactly 1 charge was created regardless of concurrency.

  Two-phase state machine in the API:
    INSERT status='processing'  ← atomic claim (UNIQUE constraint wins the race)
    UPDATE status='success'     ← committed after payment processor confirms
    Concurrent losers see status='processing' → HTTP 409 (back off and retry)
    Future retries see status='success' → HTTP 200 (idempotent replay)

  ON CONFLICT (idempotency_key) DO NOTHING is the atomic guarantee.
  No application-level locking required — the DB constraint does the work.
""")

    # ── Phase 5: Parameter mismatch on reused key ──────────────────────────
    section("Phase 5: Key Reused with Different Parameters — Client Bug Detection")
    print("  Scenario: client reuses an idempotency key but changes the amount.")
    print("  (Classic bug: retry logic generates the key from the wrong source.)\n")

    key4 = str(uuid.uuid4())
    r_orig = charge(key4, 100, "cust_bob")
    print(f"  Original charge:  HTTP {r_orig.status_code}  amount=$100  key={key4[:18]}...")

    r_mismatch = charge(key4, 999, "cust_bob")  # same key, different amount
    print(f"  Reused key ($999): HTTP {r_mismatch.status_code}")
    if r_mismatch.status_code == 422:
        body = r_mismatch.json()
        print(f"  Error: {body['error']}")
        print(f"  Stored amount:   ${body['stored']['amount']}")
        print(f"  Received amount: ${body['received']['amount']}")

    print(f"""
  HTTP 422 = Unprocessable Entity — the API detected a parameter mismatch.
  Silently serving the $100 response for a $999 request would be dangerous.
  The parameter fingerprint (amount + currency + customer_id) must match.
  This surfaces client bugs immediately rather than hiding them.
""")

    # ── Phase 6: Show the DB table ─────────────────────────────────────────
    section("Phase 6: Idempotency Key Table in Postgres")
    print("  All charges with their idempotency keys and statuses:")
    show_charge_table()

    print(f"""
  DB schema highlights:
    idempotency_key TEXT UNIQUE NOT NULL   ← UNIQUE constraint is the atomic gate
    status          TEXT DEFAULT 'processing' ← two-phase: processing → success
    response_body   TEXT (nullable)        ← NULL until payment processor confirms
    created_at      TIMESTAMP              ← enables TTL cleanup of old keys

  Two-phase lifecycle (mirrors Stripe's implementation):
    1. INSERT status='processing'    ← claim the key atomically
    2. Call payment processor        ← external side effect
    3. UPDATE status='success'       ← commit the response
    Retry during step 2 → HTTP 409 (in flight)
    Retry after step 3  → HTTP 200  (idempotent replay)

  Why two-phase vs single INSERT with response?
    Single-INSERT approach: INSERT (key, response) — requires knowing the
    response before the external call. If the processor call fails, the row
    is already committed with a "success" response that never happened.
    Two-phase: claim first, commit only after processor confirms success.

  Deduplication window:
    Store keys for 24 hours (Stripe), 7-30 days for infrastructure ops.
    Expired keys can be hard-deleted — no need for permanent storage.
    Cleanup: DELETE FROM charges WHERE created_at < NOW() - INTERVAL '24h'

  Idempotency key generation strategies:
    UUID v4       → random, client-generated, collision probability ~10^-18/yr
    Content hash  → hash(customer_id + amount + nonce) — deterministic
    Request ID    → from distributed tracing context, correlates across hops

  Non-idempotent operations that require idempotency keys:
    POST /charge          → money movement
    POST /send-email      → external side effect
    POST /provision-vm    → creates cloud resource
    POST /order           → creates order record

  Naturally idempotent (no key needed):
    GET  /user/profile    → reads have no side effects
    PUT  /user/name       → replace semantics are idempotent
    DELETE /session       → deleting an absent resource is harmless

  Next: ../16-probabilistic-data-structures/
""")


if __name__ == "__main__":
    main()
