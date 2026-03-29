#!/usr/bin/env python3
"""
Payment System Lab — experiment.py

What this demonstrates:
  1. POST charge $100 — verify double-entry (debit -100, credit +100 in ledger)
  2. Retry same charge with same idempotency key → same response, no duplicate
  3. Concurrent retries (10 threads, same key) → exactly 1 charge in DB
  4. Failed charge (amount <= 0) → no ledger entry created
  5. Reconciliation: sum all ledger entries → assert sum = 0
  6. Simulate timeout + retry: same key → safe, idempotent
  7. Refund: reverse original charge, verify ledger still balanced
  8. Double-refund prevention: second refund on same txn → rejected
  9. Reconcile endpoint: server-side invariant verification

Run:
  docker compose up -d
  # Wait ~45s for payment-service to be healthy
  python experiment.py
"""

import json
import threading
import time
import urllib.error
import urllib.request
import uuid

BASE_URL = "http://localhost:5002"


# ── Helpers ───────────────────────────────────────────────────────────────────

def section(title: str):
    print(f"\n{'=' * 62}")
    print(f"  {title}")
    print("=" * 62)


def wait_for_service(url: str, max_wait: int = 90):
    print(f"  Waiting for payment service at {url} ...")
    for i in range(max_wait):
        try:
            urllib.request.urlopen(f"{url}/health", timeout=3)
            print(f"  Service ready after {i + 1}s")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("Payment service not ready after 90s — is docker compose up?")


def post_json(path: str, data: dict) -> tuple[int, dict]:
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = {"error": "unknown"}
        return e.code, body


def get_json(path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


# ── Phase 1: Double-entry bookkeeping ────────────────────────────────────────

def phase1_double_entry():
    section("Phase 1: POST /charge — Double-Entry Bookkeeping")

    idem_key = f"charge-lab-001-{uuid.uuid4()}"
    print(f"\n  Charging customer-001 $100.00 ...")
    status, result = post_json("/charge", {
        "amount": "100.00",
        "currency": "USD",
        "customer_id": "customer-001",
        "idempotency_key": idem_key,
        "description": "Lab purchase",
    })

    print(f"  HTTP {status}")
    if status not in (200, 201):
        print(f"  ERROR: {result}")
        return None

    txn_id = result.get("transaction_id")
    print(f"  Transaction ID: {txn_id}")
    print(f"  Amount:         {result.get('amount')} {result.get('currency')}")
    print(f"  Status:         {result.get('status')}")

    # Fetch transaction details including ledger entries
    _, txn = get_json(f"/transaction/{txn_id}")

    entries = txn.get("ledger_entries", [])
    print(f"\n  Ledger entries for transaction {txn_id[:8]}...:")
    print(f"  {'Account':<25} {'Type':<8} {'Amount':>10}  Description")
    print(f"  {'-'*25}  {'-'*8}  {'-'*10}  {'-'*30}")
    for entry in entries:
        print(f"  {entry['account_id']:<25}  {entry['entry_type']:<8}  "
              f"{entry['amount']:>10}  {entry['description'][:30]}")

    # Verify double-entry: debits == credits
    debits = sum(float(e["amount"]) for e in entries if e["entry_type"] == "debit")
    credits = sum(float(e["amount"]) for e in entries if e["entry_type"] == "credit")
    print(f"\n  Total debits:  ${debits:.2f}")
    print(f"  Total credits: ${credits:.2f}")
    balanced = abs(debits - credits) < 0.001
    print(f"  Balanced:      {balanced}")
    assert balanced, "ERROR: debit/credit totals do not match for this transaction"

    print(f"""
  Double-entry rule:
  Every charge creates EXACTLY TWO ledger entries:
    1. Debit  customer account  (money leaves customer)
    2. Credit merchant account  (money arrives at merchant)

  SUM(credits) - SUM(debits) == 0 for any closed set of entries.
  This is the 500-year-old double-entry bookkeeping invariant.
""")

    return idem_key, txn_id


def phase2_idempotency(idem_key: str, original_txn_id: str):
    section("Phase 2: Retry with Same Idempotency Key")

    print(f"""
  Scenario: client sends POST /charge, network times out.
  Client retries with the SAME idempotency_key.
  Server must return the SAME result — no second charge.

  Implementation:
    Redis L1 cache  →  O(1) lookup, < 1ms
    Postgres UNIQUE →  authoritative fallback, ~5ms
    Both scoped to (customer_id, idempotency_key) to prevent
    cross-merchant key collisions (Failure Mode 6).
""")

    print(f"  Retrying charge with same key: {idem_key[:20]}...")

    all_match = True
    for attempt in range(1, 4):
        status, result = post_json("/charge", {
            "amount": "100.00",
            "currency": "USD",
            "customer_id": "customer-001",
            "idempotency_key": idem_key,
            "description": "Lab purchase",
        })
        # Duplicate responses nest the transaction under "transaction" key
        txn_obj = result.get("transaction") or result
        returned_txn_id = txn_obj.get("transaction_id", "")
        is_dup = result.get("status") == "duplicate"
        id_matches = returned_txn_id == original_txn_id
        if not id_matches:
            all_match = False
        print(f"  Attempt {attempt}: HTTP {status}, duplicate={is_dup}, "
              f"txn_id={'MATCH' if id_matches else 'MISMATCH: ' + returned_txn_id[:8]}...")

    # Verify no extra ledger entries were created
    _, txn_detail = get_json(f"/transaction/{original_txn_id}")
    entry_count = len(txn_detail.get("ledger_entries", []))

    print(f"\n  All retries returned original transaction_id: {all_match}")
    print(f"  Ledger entries for original txn: {entry_count} (expected 2 — no extras created)")
    print(f"  Result: {'CORRECT — idempotent' if all_match and entry_count == 2 else 'ERROR — check above'}")


def phase3_concurrent_retries():
    section("Phase 3: Concurrent Retries (10 Threads, Same Key)")

    print("""
  TOCTOU Race Explained:
  10 threads all arrive simultaneously with the same idempotency_key.
  All 10 do a SELECT and may see "not found" — the pre-check does NOT
  stop them. All 10 attempt an INSERT. Postgres's UNIQUE btree index
  latch ensures ONLY ONE insert commits. The other 9 get UniqueViolation,
  roll back, then fetch and return the winner's row.

  Result: exactly 1 charge in the DB, all 10 threads return the same
  transaction_id. This is why the UNIQUE constraint — not the pre-check
  SELECT — is the correctness guarantee.

  Note: all 10 responses come back as HTTP 200 (some 201 for the winner,
  200 for duplicates) because a duplicate is a successful idempotent
  outcome, not an error.
""")

    idem_key = f"concurrent-test-{uuid.uuid4()}"
    all_responses = []
    lock = threading.Lock()

    def attempt_charge():
        t0 = time.monotonic()
        status, body = post_json("/charge", {
            "amount": "50.00",
            "currency": "USD",
            "customer_id": "customer-002",
            "idempotency_key": idem_key,
            "description": "Concurrent test",
        })
        elapsed_ms = (time.monotonic() - t0) * 1000
        with lock:
            all_responses.append((status, body, elapsed_ms))

    print(f"  Launching 10 concurrent threads, all with idempotency_key={idem_key[:16]}...")
    t_start = time.monotonic()
    threads = [threading.Thread(target=attempt_charge) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    total_ms = (time.monotonic() - t_start) * 1000

    success_responses = [(s, b, ms) for s, b, ms in all_responses if s in (200, 201)]
    error_responses = [(s, b, ms) for s, b, ms in all_responses if s not in (200, 201)]

    print(f"  Threads completed: {len(all_responses)}/10  (wall time: {total_ms:.0f}ms)")
    print(f"  2xx responses (winner + idempotent duplicates): {len(success_responses)}")
    print(f"  Unexpected error responses (4xx/5xx):           {len(error_responses)}")
    if error_responses:
        for s, b, ms in error_responses:
            print(f"    HTTP {s}: {b}")

    # Show per-response breakdown
    print(f"\n  Per-response breakdown:")
    for i, (s, b, ms) in enumerate(all_responses, 1):
        txn_obj = b.get("transaction") or b
        tid = (txn_obj.get("transaction_id") or "")[:8]
        dup = b.get("status") == "duplicate"
        print(f"    Thread {i:2d}: HTTP {s}, duplicate={str(dup):<5}, "
              f"txn_id={tid}..., latency={ms:.0f}ms")

    # Collect all transaction IDs from all responses (including duplicate=True ones)
    txn_ids = set()
    for s, body, _ in success_responses:
        txn_obj = body.get("transaction") or body
        tid = txn_obj.get("transaction_id", "")
        if tid:
            txn_ids.add(tid)

    print(f"\n  Unique transaction IDs across all {len(success_responses)} responses: {len(txn_ids)}")

    # Ground truth: count ledger entries in the DB for this key
    if txn_ids:
        canonical_tid = next(iter(txn_ids))
        _, txn_detail = get_json(f"/transaction/{canonical_tid}")
        db_entry_count = len(txn_detail.get("ledger_entries", []))
        print(f"  Ledger entries in DB for this charge: {db_entry_count} (expected exactly 2)")
        correct = len(txn_ids) == 1 and db_entry_count == 2
    else:
        correct = False
        print(f"  Could not retrieve transaction details.")

    print(f"\n  Result: {'CORRECT — UNIQUE constraint enforced exactly-once' if correct else 'ERROR — check above'}")


def phase4_failed_charge():
    section("Phase 4: Failed Charge — No Ledger Entry Created")

    print("""
  A charge with amount=0, amount<0, or missing required fields must fail.
  No transaction record, no ledger entries are persisted.

  This verifies ATOMICITY: the DB transaction rolls back entirely.
  There is NO partial state (a debit without a matching credit).
""")

    test_cases = [
        ({"amount": "0",    "currency": "USD", "customer_id": "cust-3",
          "idempotency_key": f"fail-test-zero-{uuid.uuid4()}"},
         "amount = 0"),
        ({"amount": "-50",  "currency": "USD", "customer_id": "cust-3",
          "idempotency_key": f"fail-test-neg-{uuid.uuid4()}"},
         "negative amount"),
        ({"amount": "abc",  "currency": "USD", "customer_id": "cust-3",
          "idempotency_key": f"fail-test-str-{uuid.uuid4()}"},
         "non-numeric amount"),
        ({"amount": "25.00", "currency": "USD", "customer_id": "cust-3"},
         "missing idempotency_key"),
    ]

    print(f"  {'Test case':<30} {'HTTP':>6}  {'Expected':>10}  {'Pass':>6}")
    print(f"  {'-'*30}  {'-'*6}  {'-'*10}  {'-'*6}")

    all_passed = True
    for payload, note in test_cases:
        # Capture ledger count BEFORE the POST
        _, ledger_before = get_json("/ledger")
        count_before = len(ledger_before.get("entries", []))

        status, result = post_json("/charge", payload)
        error = result.get("error", "")[:25]

        # Count AFTER — should be identical
        _, ledger_after = get_json("/ledger")
        count_after = len(ledger_after.get("entries", []))

        entries_created = count_after - count_before
        passed = status >= 400 and entries_created == 0
        if not passed:
            all_passed = False
        print(f"  {note:<30}  {status:>6}  {'4xx+no entry':>10}  {'YES' if passed else 'NO':>6}  {error}")

    print(f"""
  All invalid charges return 4xx — no ledger entries created.
  {'ALL PASSED' if all_passed else 'SOME FAILED — check above'}

  Key point: the Postgres transaction (INSERT transactions +
  INSERT ledger_entries) is either fully committed or fully
  rolled back. There is no "half-charged" state possible.
""")


def phase5_reconciliation():
    section("Phase 5: Ledger Reconciliation — Sum Must Equal Zero")

    print("""
  Reconciliation: the fundamental accounting invariant.
  SUM(credits) - SUM(debits) = 0 across ALL entries.
  Equivalently: SUM(amount * sign) = 0 where credit=+1, debit=-1.

  This invariant must hold after every operation:
    - After a charge: +credit cancels -debit
    - After a refund: the reversal entries cancel the originals
    - Always, forever

  Run 5 more charges, then verify the entire ledger sums to zero.
""")

    charges = [
        ("customer-001", "29.99", "Subscription"),
        ("customer-002", "149.00", "Hardware purchase"),
        ("customer-003", "9.99", "Monthly plan"),
        ("customer-004", "499.00", "Enterprise license"),
        ("customer-005", "1.00", "Verification charge"),
    ]

    print(f"  Creating {len(charges)} additional charges ...")
    created = 0
    for customer_id, amount, desc in charges:
        status, result = post_json("/charge", {
            "amount": amount,
            "currency": "USD",
            "customer_id": customer_id,
            "idempotency_key": f"recon-{customer_id}-{uuid.uuid4()}",
            "description": desc,
        })
        if status in (200, 201):
            created += 1
    print(f"  Created {created}/{len(charges)} charges")

    # Fetch full ledger
    _, ledger = get_json("/ledger")
    entries = ledger.get("entries", [])
    balances = ledger.get("balances", [])
    net_sum = ledger.get("net_sum", "?")
    check = ledger.get("accounting_check", "?")

    print(f"\n  Ledger summary (per account):")
    print(f"  {'Account':<25} {'Credits':>12}  {'Debits':>12}  {'Count':>8}")
    print(f"  {'-'*25}  {'-'*12}  {'-'*12}  {'-'*8}")
    for b in balances:
        print(f"  {b['account_id']:<25}  {b['total_credits']:>12}  "
              f"{b['total_debits']:>12}  {b['entry_count']:>8}")

    print(f"\n  Total ledger entries visible: {len(entries)}")
    print(f"  Net sum of all ledger entries: {net_sum}")
    print(f"  Accounting invariant:          {check}")

    if check == "ZERO":
        print(f"\n  RECONCILIATION PASSED: ledger is balanced.")
    else:
        print(f"\n  RECONCILIATION FAILED: {net_sum} imbalance detected!")

    print(f"""
  SQL equivalent (run directly on DB to verify):
    SELECT SUM(amount * CASE WHEN entry_type='credit' THEN 1 ELSE -1 END) AS net
    FROM ledger_entries;
    -- Should always return 0.00
""")


def phase6_timeout_simulation():
    section("Phase 6: Timeout Simulation — Safe Retry")

    print("""
  Scenario: client sends POST /charge, server processes it (charge succeeds,
  DB committed) but the response is lost in transit (network timeout).
  Client retries with the SAME idempotency_key.

  Without idempotency: customer gets charged twice. Catastrophic.
  With idempotency:    server returns the ORIGINAL response. No second charge.

  Key insight: the idempotency guarantee is tied to the DB COMMIT, not the
  HTTP response. Even if the process crashes between COMMIT and HTTP 201,
  the retry correctly finds the committed row and returns it.
""")

    idem_key = f"timeout-sim-{uuid.uuid4()}"

    # Simulate: first call "succeeds server-side" (charge is created)
    print(f"  Step 1: POST /charge (server processes, returns 201) ...")
    status1, result1 = post_json("/charge", {
        "amount": "75.00",
        "currency": "USD",
        "customer_id": "customer-timeout",
        "idempotency_key": idem_key,
        "description": "Timeout test",
    })
    txn_id1 = result1.get("transaction_id", "")
    print(f"          HTTP {status1}, transaction_id={txn_id1[:8] if txn_id1 else 'N/A'}...")

    # Simulate: client didn't receive the response, retries
    print(f"  Step 2: Client retries (same idem_key, simulating lost response) ...")
    status2, result2 = post_json("/charge", {
        "amount": "75.00",
        "currency": "USD",
        "customer_id": "customer-timeout",
        "idempotency_key": idem_key,
        "description": "Timeout test",
    })
    txn = result2.get("transaction") or result2
    txn_id2 = txn.get("transaction_id", "")
    is_dup = result2.get("status") == "duplicate"
    print(f"          HTTP {status2}, duplicate={is_dup}, transaction_id={txn_id2[:8] if txn_id2 else 'N/A'}...")

    ids_match = txn_id1 and txn_id2 and txn_id1 == txn_id2
    print(f"\n  Transaction IDs match: {ids_match}  (same txn, no duplicate)")
    print(f"  Customer charged:      {'1x (CORRECT)' if ids_match else '2x (ERROR!)'}")
    print(f"  Result:                {'CORRECT — idempotent' if ids_match else 'ERROR — duplicate charge!'}")


def phase7_refund():
    section("Phase 7: Refund — Reversing the Ledger Entries")

    print("""
  A refund creates a NEW transaction that REVERSES the original entries.
  The original entries are NEVER modified (immutable ledger).

  Original charge entries:
    DEBIT  customer-refund  $50.00  (money left customer)
    CREDIT merchant-001     $50.00  (money arrived at merchant)

  Refund entries (new transaction):
    CREDIT customer-refund  $50.00  (money returns to customer)
    DEBIT  merchant-001     $50.00  (money leaves merchant)

  Net across all four entries: 0.00
  The accounting invariant holds before AND after the refund.

  The original transaction is marked status='refunded' atomically
  inside the same DB transaction as the refund entries, preventing
  the original from being refunded a second time.
""")

    # Create original charge
    idem_key = f"refund-orig-{uuid.uuid4()}"
    status, result = post_json("/charge", {
        "amount": "50.00",
        "currency": "USD",
        "customer_id": "customer-refund",
        "idempotency_key": idem_key,
        "description": "Refundable purchase",
    })
    txn_id = result.get("transaction_id", "")
    print(f"  Original charge: HTTP {status}, txn={txn_id[:8] if txn_id else 'N/A'}...")

    # Fetch and show ledger before refund
    _, txn_before = get_json(f"/transaction/{txn_id}")
    entries_before = txn_before.get("ledger_entries", [])
    print(f"  Ledger entries before refund: {len(entries_before)}")

    # Issue refund
    refund_key = f"refund-{uuid.uuid4()}"
    status, result = post_json("/refund", {
        "transaction_id": txn_id,
        "idempotency_key": refund_key,
    })
    refund_id = result.get("refund_transaction_id", "")
    print(f"  Refund issued:   HTTP {status}, refund_txn={refund_id[:8] if refund_id else 'N/A'}...")

    # Verify ledger still balances
    _, ledger = get_json("/ledger")
    check = ledger.get("accounting_check", "?")
    net = ledger.get("net_sum", "?")
    print(f"\n  Ledger net sum after refund: {net}")
    print(f"  Accounting check:            {check}")
    print(f"  Result: {'CORRECT — ledger balanced after refund' if check == 'ZERO' else 'ERROR — ledger imbalanced!'}")

    return txn_id, refund_key


def phase8_double_refund_prevention(txn_id: str, original_refund_key: str):
    section("Phase 8: Double-Refund Prevention")

    print("""
  A completed refund must not be applied a second time.
  The original transaction is atomically marked 'refunded' inside
  the same DB transaction that inserts the refund ledger entries.

  Two sub-cases:
    a) Same idempotency_key as the first refund
       → idempotent: return the original refund result, no new entries
    b) Different idempotency_key on an already-refunded transaction
       → rejected with 4xx: transaction status is not 'completed'

  The guard is the status field check BEFORE inserting refund entries,
  all within a single ACID transaction. Concurrent refund attempts
  are serialized by the Postgres row lock on the original transaction row.
""")

    if not txn_id:
        print("  SKIP — no transaction_id from Phase 7.")
        return

    # Case a: Idempotent retry of the same refund (same idem key)
    print(f"  Case a: Retry refund with SAME idempotency_key ...")
    status_a, result_a = post_json("/refund", {
        "transaction_id": txn_id,
        "idempotency_key": original_refund_key,
    })
    is_dup_a = result_a.get("status") in ("duplicate_refund", "refunded")
    print(f"          HTTP {status_a}, status={result_a.get('status')}")
    print(f"          Idempotent (no second refund applied): {is_dup_a}")

    # Case b: New idempotency_key — should be rejected because txn is already refunded
    new_refund_key = f"refund-second-{uuid.uuid4()}"
    print(f"\n  Case b: Second refund attempt with DIFFERENT idempotency_key ...")
    status_b, result_b = post_json("/refund", {
        "transaction_id": txn_id,
        "idempotency_key": new_refund_key,
    })
    rejected = status_b in (400, 409, 422)
    error_msg = result_b.get("error", result_b.get("detail", ""))
    print(f"          HTTP {status_b}, error='{error_msg}'")
    print(f"          Rejected (correct — already refunded): {rejected}")

    overall = is_dup_a and rejected
    print(f"\n  Result: {'CORRECT — double-refund prevented' if overall else 'ERROR — check above'}")


def phase9_reconcile_endpoint():
    section("Phase 9: /reconcile Endpoint — Server-Side Integrity Check")

    print("""
  The /reconcile endpoint runs the full invariant check on the server:
    1. SUM(signed ledger amounts) == 0  (double-entry invariant)
    2. Every transaction has exactly 2 ledger entries
    3. No orphaned ledger entries (entries with no parent transaction)

  In production, this runs as a background job (every hour or continuously
  via a Flink streaming job on the ledger event stream). Any non-zero sum
  triggers an immediate PagerDuty alert — it means money is missing.
""")

    status, result = get_json("/reconcile")
    print(f"  HTTP {status}")
    if status == 200:
        print(f"  Net sum:            {result.get('net_sum', '?')}")
        print(f"  Accounting check:   {result.get('accounting_check', '?')}")
        print(f"  Total entries:      {result.get('total_entries', '?')}")
        print(f"  Total transactions: {result.get('total_transactions', '?')}")
        txns_missing = result.get("transactions_missing_entries", 0)
        print(f"  Txns missing 2 entries: {txns_missing} (expected 0)")
        ok = (result.get("accounting_check") == "ZERO" and txns_missing == 0)
        print(f"\n  Result: {'RECONCILIATION PASSED' if ok else 'RECONCILIATION FAILED — investigate!'}")
    else:
        print(f"  Response: {result}")
        print(f"  (Endpoint may not be implemented — see /ledger for manual check)")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    section("PAYMENT SYSTEM LAB")
    print("""
  Architecture:
    Client -> Flask /charge -> Postgres (transaction + ledger_entries)
                            ^
                       Redis (idempotency_key cache, 24h TTL)

  Invariants demonstrated:
    1. Every charge = 1 debit + 1 credit (double-entry bookkeeping)
    2. Same idempotency_key -> same response (no duplicate charge)
    3. Concurrent duplicates -> DB UNIQUE constraint ensures exactly-once
    4. Failed charges create NO ledger entries (atomicity / rollback)
    5. SUM of all ledger entries = 0 (reconciliation invariant)
    6. A refunded transaction cannot be refunded again (status guard)

  Key design decisions:
    - Postgres UNIQUE (customer_id, idempotency_key) is the correctness guard
    - Redis idempotency cache is a performance optimization, NOT correctness
    - Ledger entries are IMMUTABLE — corrections are new reversal entries
    - All charge operations (txn + 2 ledger entries + outbox) are one ACID txn
""")

    wait_for_service(BASE_URL)

    result = phase1_double_entry()
    if result:
        idem_key, txn_id = result
        phase2_idempotency(idem_key, txn_id)

    phase3_concurrent_retries()
    phase4_failed_charge()
    phase5_reconciliation()
    phase6_timeout_simulation()
    refund_result = phase7_refund()
    if refund_result:
        txn_id, refund_key = refund_result
        phase8_double_refund_prevention(txn_id, refund_key)

    phase9_reconcile_endpoint()

    section("Lab Complete")
    print("""
  Summary:
  - Double-entry: every charge = 1 debit + 1 credit (sum always zero)
  - Idempotency key: retries return original response — no double charges
  - Concurrent retries: Postgres UNIQUE constraint enforces exactly-once
  - Invalid charges: DB transaction rolled back, no partial ledger state
  - Reconciliation: SUM(signed amounts) = 0 is a verifiable invariant
  - Refund: creates reverse entries, ledger remains balanced
  - Double-refund: second refund on same txn is rejected (status guard)

  This is the core of Stripe, PayPal, and every financial system.
  The accounting invariant (sum=0) is the ultimate integration test.

  Manual verification commands:
    # Ledger sum (must be 0.00):
    docker compose exec db psql -U app -d payments -c \\
      "SELECT SUM(amount * CASE WHEN entry_type='credit' THEN 1 ELSE -1 END) FROM ledger_entries;"

    # Idempotency keys in Redis:
    docker compose exec redis redis-cli keys "idem:*"
""")


if __name__ == "__main__":
    main()
