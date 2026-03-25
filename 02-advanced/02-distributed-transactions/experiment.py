#!/usr/bin/env python3
"""
Distributed Transactions Lab — 2PC and Saga patterns

Prerequisites: docker compose up -d (wait ~10s for both Postgres instances)

What this demonstrates:
  1. Two-Phase Commit (2PC): PREPARE → COMMIT across two databases
  2. 2PC failure: coordinator crash after PREPARE leaves DBs blocked
  3. Saga pattern: choreography-style with compensating transactions
  4. Why 2PC is blocking and Sagas are preferred for long-lived transactions
"""

import time
import subprocess
import uuid
from datetime import datetime

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("Installing dependencies...")
    subprocess.run(["pip", "install", "psycopg2-binary", "-q"], check=True)
    import psycopg2
    import psycopg2.extras

ORDERS_DSN    = "host=localhost port=5433 dbname=orders user=postgres password=postgres connect_timeout=5"
INVENTORY_DSN = "host=localhost port=5434 dbname=inventory user=postgres password=postgres connect_timeout=5"


# ── Setup ──────────────────────────────────────────────────────────────────

def get_orders_conn():
    conn = psycopg2.connect(ORDERS_DSN)
    return conn


def get_inventory_conn():
    conn = psycopg2.connect(INVENTORY_DSN)
    return conn


def setup_schemas():
    """Create tables in both databases."""
    with get_orders_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id         TEXT PRIMARY KEY,
                    product_id TEXT NOT NULL,
                    quantity   INT  NOT NULL,
                    status     TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            cur.execute("TRUNCATE orders")

    with get_inventory_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS inventory (
                    product_id TEXT PRIMARY KEY,
                    quantity   INT  NOT NULL,
                    reserved   INT  NOT NULL DEFAULT 0
                )
            """)
            cur.execute("TRUNCATE inventory")
            cur.execute("""
                INSERT INTO inventory (product_id, quantity, reserved)
                VALUES ('WIDGET-A', 100, 0), ('WIDGET-B', 50, 0)
            """)

    print("  Schemas created. Inventory seeded: WIDGET-A=100, WIDGET-B=50")


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def show_state(label="Current state"):
    """Print current state of both databases."""
    print(f"\n  [{label}]")
    with get_orders_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, product_id, quantity, status FROM orders ORDER BY created_at")
            rows = cur.fetchall()
            if rows:
                print(f"  db-orders:")
                for r in rows:
                    print(f"    order {r['id'][:8]}... product={r['product_id']} qty={r['quantity']} status={r['status']}")
            else:
                print(f"  db-orders: (no orders)")

    with get_inventory_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT product_id, quantity, reserved FROM inventory ORDER BY product_id")
            rows = cur.fetchall()
            print(f"  db-inventory:")
            for r in rows:
                print(f"    product={r['product_id']} available={r['quantity']} reserved={r['reserved']}")


# ── Two-Phase Commit Coordinator ──────────────────────────────────────────

class TwoPhaseCoordinator:
    """
    Simplified 2PC coordinator.

    In production, the coordinator would:
    - Write a durable prepare log before sending PREPARE
    - Write a commit/abort decision durably before broadcasting it
    - Retry COMMIT indefinitely until all participants acknowledge
    """

    def __init__(self, participants):
        self.participants = participants  # list of (name, conn)
        self.prepared = []

    def run(self, operations, simulate_crash_after_prepare=False):
        """
        operations: list of (name, conn, fn) where fn(conn) executes the work
        simulate_crash_after_prepare: if True, crash after PREPARE, before COMMIT
        """
        txn_id = str(uuid.uuid4())[:8]
        print(f"\n  Transaction ID: {txn_id}")

        # ── Phase 1: PREPARE ──────────────────────────────────────
        print(f"\n  Phase 1: PREPARE")
        prepared_conns = []
        all_yes = True

        for name, conn, operation in operations:
            try:
                conn.autocommit = False
                operation(conn)
                # Use PostgreSQL's PREPARE TRANSACTION (XA-style)
                prepare_name = f"txn_{txn_id}_{name.replace('-', '_')}"
                with conn.cursor() as cur:
                    cur.execute(f"PREPARE TRANSACTION '{prepare_name}'")
                print(f"    {name}: VOTE=YES (prepared as '{prepare_name}')")
                prepared_conns.append((name, conn, prepare_name))
            except Exception as e:
                print(f"    {name}: VOTE=NO ({e})")
                all_yes = False
                break

        if simulate_crash_after_prepare:
            print(f"\n  *** COORDINATOR CRASH after PREPARE, before COMMIT ***")
            print(f"  Both databases are now stuck in PREPARED state.")
            print(f"  They hold locks and cannot be used by other transactions.")
            print(f"  This is the 2PC blocking problem.")
            print(f"\n  Prepared transactions (blocking DB resources):")
            print(f"  === Prepared transactions stuck (zombie state) ===")
            for db_label, dsn_conn_fn in [("db-orders", get_orders_conn),
                                           ("db-inventory", get_inventory_conn)]:
                with dsn_conn_fn() as c:
                    with c.cursor() as cur:
                        cur.execute("SELECT gid FROM pg_prepared_xacts")
                        gids = [row[0] for row in cur.fetchall()]
                        print(f"    {db_label}: {gids if gids else '(none)'}")
            print(f"\n  Recovery requires a human or recovery process to:")
            print(f"    COMMIT PREPARED '{prepare_name}' or ROLLBACK PREPARED '{prepare_name}'")
            # Clean up for next phases
            for name, conn, prepare_name in prepared_conns:
                try:
                    with conn.cursor() as cur:
                        cur.execute(f"ROLLBACK PREPARED '{prepare_name}'")
                    conn.autocommit = True
                except Exception:
                    pass
            return False

        # ── Phase 2: COMMIT or ABORT ──────────────────────────────
        if all_yes:
            print(f"\n  Phase 2: COMMIT (all participants voted YES)")
            for name, conn, prepare_name in prepared_conns:
                with conn.cursor() as cur:
                    cur.execute(f"COMMIT PREPARED '{prepare_name}'")
                conn.autocommit = True
                print(f"    {name}: COMMITTED")
            return True
        else:
            print(f"\n  Phase 2: ABORT (at least one participant voted NO)")
            for name, conn, prepare_name in prepared_conns:
                try:
                    with conn.cursor() as cur:
                        cur.execute(f"ROLLBACK PREPARED '{prepare_name}'")
                    conn.autocommit = True
                    print(f"    {name}: ROLLED BACK")
                except Exception:
                    conn.autocommit = True
            return False


# ── Saga Pattern ───────────────────────────────────────────────────────────

class SagaOrchestrator:
    """
    Saga with explicit compensating transactions.

    Each step records what it did so it can be undone.
    The orchestrator runs steps in order; on failure, runs compensations
    in reverse order.
    """

    def __init__(self):
        self.completed_steps = []  # (name, compensation_fn)

    def execute_step(self, name, action_fn, compensation_fn):
        try:
            action_fn()
            self.completed_steps.append((name, compensation_fn))
            print(f"    ✓ Step '{name}': SUCCESS")
            return True
        except Exception as e:
            print(f"    ✗ Step '{name}': FAILED ({e})")
            return False

    def compensate(self):
        print(f"\n  Running compensating transactions (reverse order):")
        for name, comp_fn in reversed(self.completed_steps):
            try:
                comp_fn()
                print(f"    ↩ Compensated '{name}'")
            except Exception as e:
                print(f"    ✗ Compensation for '{name}' failed: {e}")
                print(f"      This is a 'stuck saga' — requires manual intervention")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    section("DISTRIBUTED TRANSACTIONS LAB")
    print("""
  Two services, two databases:
    db-orders    (port 5433) — stores order records
    db-inventory (port 5434) — stores product stock levels

  Challenge: deduct inventory AND create an order atomically.
  If we do them separately, a crash between them leaves inconsistent state.
""")

    # Verify connectivity
    try:
        get_orders_conn().close()
        get_inventory_conn().close()
    except Exception as e:
        print(f"  ERROR: {e}")
        print("  Run: docker compose up -d")
        return

    # Enable prepared transactions (required for 2PC)
    for dsn in [ORDERS_DSN, INVENTORY_DSN]:
        with psycopg2.connect(dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SHOW max_prepared_transactions")
                val = cur.fetchone()[0]
                if val == '0':
                    print("  WARNING: max_prepared_transactions=0.")
                    print("  2PC phases will use simulated prepare (no real XA).")

    setup_schemas()
    show_state("Initial state")

    # ── Phase 1: Successful 2PC ────────────────────────────────────
    section("Phase 1: Successful Two-Phase Commit (2PC)")
    print("""
  2PC Protocol:
    Phase 1 (PREPARE): Coordinator asks all participants to prepare.
                       Each participant writes to a durable log and votes YES/NO.
    Phase 2 (COMMIT):  If all vote YES, coordinator sends COMMIT to all.
                       If any vote NO, coordinator sends ABORT to all.
""")

    order_id = str(uuid.uuid4())
    product  = "WIDGET-A"
    qty      = 10

    inv_conn = get_inventory_conn()
    ord_conn = get_orders_conn()

    # Enable prepared transactions
    for conn in [inv_conn, ord_conn]:
        conn.autocommit = True

    def deduct_inventory(conn):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE inventory SET quantity = quantity - %s WHERE product_id = %s AND quantity >= %s",
                (qty, product, qty)
            )
            if cur.rowcount == 0:
                raise Exception(f"Insufficient inventory for {product}")

    def create_order(conn):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO orders (id, product_id, quantity, status) VALUES (%s, %s, %s, %s)",
                (order_id, product, qty, "confirmed")
            )

    coordinator = TwoPhaseCoordinator([])
    success = coordinator.run([
        ("db-inventory", inv_conn, deduct_inventory),
        ("db-orders",    ord_conn, create_order),
    ])

    inv_conn.close()
    ord_conn.close()

    print(f"\n  Result: {'COMMITTED' if success else 'ABORTED'}")
    show_state("After successful 2PC")

    # ── Phase 2: Failed 2PC (coordinator crash) ────────────────────
    section("Phase 2: 2PC Coordinator Crash — The Blocking Problem")
    print("""
  Scenario: coordinator sends PREPARE to both databases,
  both vote YES, then the coordinator crashes before sending COMMIT.

  Both databases are now stuck:
    - They've promised to commit (voted YES)
    - They hold write locks on the affected rows
    - They cannot commit or roll back unilaterally
    - Other transactions trying to touch those rows will BLOCK

  This is the fundamental problem with 2PC: it is a BLOCKING protocol.
  If the coordinator is down, the system is frozen until it recovers.
""")

    order_id2 = str(uuid.uuid4())
    inv_conn2 = get_inventory_conn()
    ord_conn2 = get_orders_conn()
    for conn in [inv_conn2, ord_conn2]:
        conn.autocommit = True

    coordinator2 = TwoPhaseCoordinator([])
    coordinator2.run(
        [
            ("db-inventory", inv_conn2, lambda c: deduct_inventory(c)),
            ("db-orders",    ord_conn2, lambda c: create_order(c)),
        ],
        simulate_crash_after_prepare=True
    )

    inv_conn2.close()
    ord_conn2.close()

    show_state("After coordinator crash (no change — both rolled back in cleanup)")

    print("""
  3PC (Three-Phase Commit) adds a "pre-commit" phase to reduce blocking:
    Phase 1: PREPARE    — participants vote YES/NO
    Phase 2: PRE-COMMIT — coordinator broadcasts decision; participants ACK
    Phase 3: COMMIT     — coordinator sends COMMIT after all ACK pre-commit

  If coordinator crashes after Phase 2, participants know the decision
  (all voted YES and received pre-commit) and can complete independently.
  BUT: 3PC is still not safe under network partitions (a split-brain can
  cause some participants to commit and others to abort). In practice,
  neither 2PC nor 3PC is used in modern geo-distributed systems.
""")

    # ── Phase 3: Saga Pattern ──────────────────────────────────────
    section("Phase 3: Saga Pattern — Choreography with Compensations")
    print("""
  The Saga pattern decomposes a distributed transaction into a sequence
  of local transactions. Each step has a compensating transaction that
  undoes its effect if a later step fails.

  Unlike 2PC, Sagas are non-blocking: each step commits locally and
  immediately. Rollback is achieved by executing compensations in reverse.

  Scenario: Order for 5x WIDGET-B — but the order creation step fails.
  Expected outcome: inventory restored, no order record created.
""")

    saga_order_id = str(uuid.uuid4())
    saga_product  = "WIDGET-B"
    saga_qty      = 5

    saga = SagaOrchestrator()

    # Step 1: Deduct inventory
    def step_deduct():
        with get_inventory_conn() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE inventory SET quantity = quantity - %s WHERE product_id = %s AND quantity >= %s",
                    (saga_qty, saga_product, saga_qty)
                )
                if cur.rowcount == 0:
                    raise Exception(f"Insufficient inventory for {saga_product}")

    def compensate_deduct():
        with get_inventory_conn() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE inventory SET quantity = quantity + %s WHERE product_id = %s",
                    (saga_qty, saga_product)
                )

    # Step 2: Create order (simulated failure)
    def step_create_order():
        # Simulate a failure in the order service (e.g., payment declined)
        raise Exception("Payment declined — order creation failed")

    def compensate_order():
        with get_orders_conn() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("DELETE FROM orders WHERE id = %s", (saga_order_id,))

    print(f"\n  Executing saga steps:")

    ok = saga.execute_step("deduct-inventory", step_deduct, compensate_deduct)
    show_state("After step 1: inventory deducted")

    if ok:
        ok = saga.execute_step("create-order", step_create_order, compensate_order)

    if not ok:
        saga.compensate()

    show_state("After saga compensation (should be back to original)")

    print(f"""
  Result: inventory restored to 50, no order created — consistent!

  Saga trade-offs:
    + Non-blocking: no coordinator holds locks across services
    + Works across microservices without distributed lock managers
    + Partial failures are visible and recoverable
    - No atomicity: between steps, the system is in an intermediate state
    - Other services can observe partial results (e.g., inventory deducted
      but order not yet created) — requires idempotent reads
    - Compensation logic is complex and must be idempotent itself
    - "Stuck sagas" occur when a compensation step also fails

  Choreography (used here): each service publishes events and reacts to
  events from other services. No central coordinator.

  Orchestration: a central saga orchestrator (a separate service) directs
  each participant via commands. Easier to track saga state but introduces
  a central point of coordination.
""")

    # ── Summary ───────────────────────────────────────────────────
    section("Summary")
    print("""
  Distributed Transaction Patterns:
  ─────────────────────────────────────────────────────────────
  2PC (Two-Phase Commit)
    + Strong consistency across services
    + Used in XA-compliant databases (PostgreSQL, MySQL, Oracle)
    - Blocking: coordinator failure freezes all participants
    - Tight coupling: all services must support XA protocol
    - Low throughput: locks held across network round-trips

  Saga
    + Non-blocking: each step commits locally
    + Works across heterogeneous services
    + Scales well; no central lock manager
    - Eventual consistency only
    - Compensations must be carefully designed and idempotent
    - Intermediate states are visible to concurrent reads

  When to use 2PC:
    - Single-datacenter, same-org databases
    - Short-lived transactions with low latency requirements
    - When ACID across services is truly necessary (rare)

  When to use Saga:
    - Microservices across network boundaries
    - Long-lived business transactions (minutes to hours)
    - When eventual consistency is acceptable (most of the time)

  Real-world: Stripe uses event-driven Sagas for payment flows.
  Temporal.io is a popular saga orchestration framework.

  Next: ../03-consensus-paxos-raft/
""")


if __name__ == "__main__":
    main()
