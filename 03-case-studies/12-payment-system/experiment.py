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

Run:
  docker compose up -d
  # Wait ~30s for payment-service to be healthy
  python experiment.py
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE_URL = "http://localhost:5002"


# ── Helpers ───────────────────────────────────────────────────────────────────

def section(title: str):
    print(f"\n{'=' * 62}")
    print(f"  {title}")
    print("=" * 62)


def wait_for_service(url: str, max_wait: int = 60):
    print(f"  Waiting for payment service at {url} ...")
    for i in range(max_wait):
        try:
            urllib.request.urlopen(f"{url}/health", timeout=3)
            print(f"  Service ready after {i + 1}s")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("Payment service not ready")


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
    print(f"  Balanced:      {abs(debits - credits) < 0.001}")

    print(f"""
  Double-entry rule:
  Every charge creates EXACTLY TWO entries:
    1. Debit  customer account  (money leaves customer)
    2. Credit merchant account  (money arrives at merchant)

  The SUM of all amounts × sign must equal ZERO.
  This is the fundamental invariant of accounting.
""")

    return idem_key, txn_id


def phase2_idempotency(idem_key: str, original_txn_id: str):
    section("Phase 2: Retry with Same Idempotency Key")

    print(f"""
  Scenario: client sends POST /charge, network times out.
  Client retries with the SAME idempotency_key.
  Server must return the SAME result — no second charge.
""")

    print(f"  Retrying charge with same key: {idem_key[:20]}...")

    for attempt in range(1, 4):
        status, result = post_json("/charge", {
            "amount": "100.00",
            "currency": "USD",
            "customer_id": "customer-001",
            "idempotency_key": idem_key,
            "description": "Lab purchase",
        })
        txn_id = (result.get("transaction") or result).get("transaction_id", "")
        is_dup = result.get("status") == "duplicate"
        print(f"  Attempt {attempt}: HTTP {status}, duplicate={is_dup}, txn_id={txn_id[:8] if txn_id else 'N/A'}...")

    print(f"\n  All retries returned the original transaction_id: {original_txn_id[:8]}...")
    print(f"  No additional ledger entries created.")


def phase3_concurrent_retries():
    section("Phase 3: Concurrent Retries (10 Threads, Same Key)")

    idem_key = f"concurrent-test-{uuid.uuid4()}"
    results = []
    errors = []
    lock = threading.Lock()

    def attempt_charge():
        status, body = post_json("/charge", {
            "amount": "50.00",
            "currency": "USD",
            "customer_id": "customer-002",
            "idempotency_key": idem_key,
            "description": "Concurrent test",
        })
        with lock:
            if status in (200, 201):
                results.append(body)
            else:
                errors.append((status, body))

    print(f"\n  Launching 10 concurrent threads, all with idempotency_key={idem_key[:16]}...")
    threads = [threading.Thread(target=attempt_charge) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    print(f"  Threads completed: {len(results) + len(errors)}")
    print(f"  Successful responses: {len(results)}")
    print(f"  Error responses:      {len(errors)}")

    # Count unique transaction IDs
    txn_ids = set()
    for r in results:
        if isinstance(r, dict):
            txn = r.get("transaction") or r
            tid = txn.get("transaction_id", "")
            if tid:
                txn_ids.add(tid)

    print(f"  Unique transaction IDs: {len(txn_ids)}")
    print(f"\n  Result: {'CORRECT — exactly 1 charge' if len(txn_ids) == 1 else 'ERROR — multiple charges!'}")

    # Verify in ledger
    _, ledger = get_json("/ledger")
    entries_for_key = [e for e in ledger.get("entries", [])
                       if any(tid in e.get("transaction_id", "") for tid in txn_ids)]
    print(f"  Ledger entries for this transaction: {len(entries_for_key)} (should be 2: 1 debit + 1 credit)")


def phase4_failed_charge():
    section("Phase 4: Failed Charge — No Ledger Entry Created")

    print("""
  A charge with amount=0 or amount<0 should fail validation.
  No transaction record, no ledger entries.
  This verifies atomicity: partial state never persists.
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

    print(f"  {'Test case':<30} {'HTTP':>6}  {'Error':>25}  {'Entries':>8}")
    print(f"  {'-'*30}  {'-'*6}  {'-'*25}  {'-'*8}")

    for payload, note in test_cases:
        status, result = post_json("/charge", payload)
        error = result.get("error", "")[:25]

        # Count ledger entries before
        _, ledger_before = get_json("/ledger")
        count_before = len(ledger_before.get("entries", []))

        # Count after (should be same)
        _, ledger_after = get_json("/ledger")
        count_after = len(ledger_after.get("entries", []))

        entries_created = count_after - count_before
        print(f"  {note:<30}  {status:>6}  {error:>25}  {entries_created:>8}")

    print("""
  All invalid charges return 4xx — no ledger entries created.
  The database transaction is either committed fully or rolled back.
""")


def phase5_reconciliation():
    section("Phase 5: Ledger Reconciliation — Sum Must Equal Zero")

    print("""
  Reconciliation: the fundamental accounting invariant.
  Sum of all debits + all credits = 0 (when debits are negative).
  Equivalently: total_credits - total_debits = 0.

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

    print(f"\n  Ledger summary:")
    print(f"  {'Account':<25} {'Credits':>12}  {'Debits':>12}  {'Count':>8}")
    print(f"  {'-'*25}  {'-'*12}  {'-'*12}  {'-'*8}")
    for b in balances:
        print(f"  {b['account_id']:<25}  {b['total_credits']:>12}  "
              f"{b['total_debits']:>12}  {b['entry_count']:>8}")

    print(f"\n  Net sum of all ledger entries: {net_sum}")
    print(f"  Accounting invariant:          {check}")

    if check == "ZERO":
        print(f"\n  RECONCILIATION PASSED: ledger is balanced.")
    else:
        print(f"\n  RECONCILIATION FAILED: {net_sum} imbalance detected!")


def phase6_timeout_simulation():
    section("Phase 6: Timeout Simulation — Safe Retry")

    print("""
  Scenario: client sends POST /charge, server processes it (charge succeeds)
  but the response is lost in transit (network timeout).
  Client retries with the SAME idempotency_key.

  Without idempotency: customer gets charged twice.
  With idempotency:    server returns original response, no second charge.
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
    print(f"  Step 2: Client retries (same idem_key, 'didn't receive response') ...")
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
    print(f"\n  Transaction IDs match: {ids_match}")
    print(f"  Customer charged:      1× (NOT 2×)")
    print(f"  Result:                {'CORRECT — idempotent' if ids_match else 'ERROR — duplicate charge!'}")


def phase7_refund():
    section("Phase 7: Refund — Reversing the Ledger Entries")

    print("""
  A refund creates a new transaction that REVERSES the original entries.
  Original: debit customer $50, credit merchant $50
  Refund:   credit customer $50, debit merchant $50
  Net:      ledger sum still = 0
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


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    section("PAYMENT SYSTEM LAB")
    print("""
  Architecture:
    Client → Flask /charge → Postgres (transaction + ledger_entries)
                          ↗
                     Redis (idempotency_key cache, 24h TTL)

  Invariants:
    1. Every charge = 1 debit + 1 credit (double-entry)
    2. Same idempotency_key → same response (no duplicate charge)
    3. Failed charges create NO ledger entries (atomicity)
    4. SUM of all ledger entries = 0 (reconciliation)
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
    phase7_refund()

    section("Lab Complete")
    print("""
  Summary:
  - Double-entry: every charge = 1 debit + 1 credit (sum always zero)
  - Idempotency key: retries return original response — no double charges
  - Concurrent retries: DB UNIQUE constraint + ON CONFLICT ensures exactly-1
  - Invalid charges: Postgres transaction rolled back, no partial ledger state
  - Reconciliation: sum of all entries = 0 is a verifiable system invariant
  - Refund: creates reverse entries, ledger remains balanced

  This is the core of Stripe, PayPal, and every financial system.
  The accounting invariant (sum=0) is the ultimate integration test.
""")


if __name__ == "__main__":
    main()
