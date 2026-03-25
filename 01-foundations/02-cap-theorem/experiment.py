#!/usr/bin/env python3
"""
CAP Theorem Lab — Consistency vs Availability under Network Partition

Prerequisites: docker compose up -d (wait ~30s for streaming replication to establish)

What this demonstrates:
  - Real PostgreSQL streaming replication (WAL-based, not manual data copy)
  - pg_stat_replication shows live replication state on primary
  - Under normal operation: write to primary propagates automatically to replica
  - During a "partition" (replica stopped): primary stays available (AP choice)
  - If we require consistent reads from replica: we lose availability (CP choice)
  - After heal: replica reconnects and catches up via WAL replay
"""

import subprocess
import time
import json

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("Installing psycopg2-binary...")
    subprocess.run(["pip", "install", "psycopg2-binary", "-q"], check=True)
    import psycopg2
    import psycopg2.extras

PRIMARY_DSN = "host=localhost port=5432 dbname=captest user=postgres password=postgres connect_timeout=3"
REPLICA_DSN = "host=localhost port=5433 dbname=captest user=postgres password=postgres connect_timeout=3"


def connect(dsn, label):
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        return conn
    except Exception as e:
        print(f"  [!] Cannot connect to {label}: {e}")
        return None


def wait_for_replica(dsn, label, max_attempts=15):
    """Wait for replica to become available (basebackup + startup takes time)."""
    for attempt in range(max_attempts):
        conn = connect(dsn, label)
        if conn:
            return conn
        print(f"  Waiting for {label} to be ready... ({attempt + 1}/{max_attempts})")
        time.sleep(2)
    return None


def setup_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                balance INTEGER NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        cur.execute("TRUNCATE accounts")
        cur.execute("INSERT INTO accounts (name, balance) VALUES ('Alice', 1000), ('Bob', 500)")


def read_accounts(conn, label):
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, name, balance FROM accounts ORDER BY id")
            rows = cur.fetchall()
            print(f"  Read from {label}: {[dict(r) for r in rows]}")
            return rows
    except Exception as e:
        print(f"  [!] Read from {label} failed: {e}")
        return None


def write_account(conn, name, balance):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE accounts SET balance = %s, updated_at = now() WHERE name = %s",
            (balance, name)
        )


def show_replication_status(conn):
    """Query pg_stat_replication to show real streaming replication state."""
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    client_addr,
                    state,
                    sent_lsn,
                    write_lsn,
                    flush_lsn,
                    replay_lsn,
                    sync_state,
                    (sent_lsn - replay_lsn) AS replication_lag_bytes
                FROM pg_stat_replication
            """)
            rows = cur.fetchall()
            if rows:
                print(f"  pg_stat_replication ({len(rows)} connected replica(s)):")
                for r in rows:
                    print(f"    client={r['client_addr']}  state={r['state']}  "
                          f"sync={r['sync_state']}  lag_bytes={r['replication_lag_bytes']}")
                    print(f"      sent_lsn={r['sent_lsn']}  replay_lsn={r['replay_lsn']}")
            else:
                print("  pg_stat_replication: no connected replicas")
            return rows
    except Exception as e:
        print(f"  [!] pg_stat_replication query failed: {e}")
        return []


def poll_replica_for_row(replica_conn, expected_balance, timeout=10):
    """Poll replica until it reflects a write (measures real replication lag)."""
    start = time.perf_counter()
    while time.perf_counter() - start < timeout:
        try:
            with replica_conn.cursor() as cur:
                cur.execute("SELECT balance FROM accounts WHERE name = 'Alice'")
                row = cur.fetchone()
                if row and row[0] == expected_balance:
                    return time.perf_counter() - start
        except Exception:
            pass
        time.sleep(0.01)
    return None


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def main():
    section("CAP THEOREM LAB")
    print("""
  CAP Theorem states a distributed system can guarantee at most
  two of three properties during a network partition:
    C — Consistency  (every read sees the latest write)
    A — Availability (every request gets a response)
    P — Partition Tolerance (system works despite dropped messages)

  Since network partitions WILL happen, you must choose: CP or AP.

  This lab uses REAL PostgreSQL streaming replication (WAL-based).
  The replica receives changes automatically — no manual data copy.
""")

    # ── Phase 1: Normal operation ──────────────────────────────────
    section("Phase 1: Normal Operation — Real Streaming Replication")

    primary = connect(PRIMARY_DSN, "primary")
    if not primary:
        print("  ERROR: Primary not reachable. Run 'docker compose up -d' first.")
        return

    print("  Waiting for replica streaming replication to establish...")
    replica = wait_for_replica(REPLICA_DSN, "replica")
    if not replica:
        print("  WARNING: Replica not reachable. Continuing with primary only.")

    print("\n  Checking pg_stat_replication on primary:")
    show_replication_status(primary)

    print("\n  Setting up schema and seed data on PRIMARY only...")
    setup_table(primary)
    print("  (Data will propagate to replica automatically via WAL streaming)")

    # Allow WAL to stream to replica
    time.sleep(1)

    print("\n  [Read] From primary immediately after write:")
    read_accounts(primary, "PRIMARY")

    if replica:
        print("\n  [Read] From replica (receives changes via streaming replication):")
        read_accounts(replica, "REPLICA ")

    # Measure real replication lag
    if replica:
        print("\n  Measuring real replication lag...")
        write_account(primary, "Alice", 800)
        write_account(primary, "Bob", 700)
        lag = poll_replica_for_row(replica, 800)
        if lag is not None:
            print(f"  Replica reflected write in {lag * 1000:.1f}ms (real WAL streaming lag)")
        else:
            print("  Replica did not reflect write within timeout")

        print("\n  [Read] After write — both nodes:")
        read_accounts(primary, "PRIMARY")
        read_accounts(replica, "REPLICA ")

        print("\n  pg_stat_replication after writes:")
        show_replication_status(primary)

    # ── Phase 2: Simulate Network Partition ───────────────────────
    section("Phase 2: Network Partition — Stopping the Replica")

    print("  Stopping postgres-replica container to simulate a network partition...")
    result = subprocess.run(
        ["docker", "compose", "stop", "postgres-replica"],
        capture_output=True, text=True, cwd="."
    )
    if result.returncode != 0:
        subprocess.run(
            ["docker", "stop", "02-cap-theorem-postgres-replica-1"],
            capture_output=True, text=True
        )
    print("  Replica is now unreachable (partition simulated).")

    print("\n  pg_stat_replication now (replica disconnected):")
    show_replication_status(primary)
    time.sleep(1)

    # ── Phase 3: AP choice ─────────────────────────────────────────
    section("Phase 3: AP Choice — Primary Stays Available")

    print("""  AP systems (Cassandra, DynamoDB, CouchDB) choose AVAILABILITY:
  → Accept writes and reads on surviving nodes
  → Return potentially stale data rather than refusing requests
  → Reconcile conflicts when the partition heals
""")
    print("  [Write] Attempting write to PRIMARY during partition...")
    try:
        write_account(primary, "Alice", 600)
        write_account(primary, "Bob", 900)
        print("  Write SUCCEEDED on primary — system is AVAILABLE (AP behavior)")
        read_accounts(primary, "PRIMARY (AP)")
    except Exception as e:
        print(f"  Write failed: {e}")

    print("\n  [Read] Attempting read from REPLICA during partition...")
    replica2 = connect(REPLICA_DSN, "replica")
    if replica2 is None:
        print("  Read from replica FAILED — replica is unreachable.")
        print("  If our app REQUIRES replica reads: we lose AVAILABILITY (CP behavior).")
    else:
        read_accounts(replica2, "REPLICA")

    # ── Phase 4: CP choice explanation ────────────────────────────
    section("Phase 4: CP Choice — Consistency over Availability")

    print("""  CP systems (ZooKeeper, HBase, MongoDB in strong mode) choose CONSISTENCY:
  → Refuse reads/writes if the system cannot guarantee consistency
  → Return an error rather than stale or potentially conflicting data
  → The system is "unavailable" during the partition

  Example: ZooKeeper requires a quorum (majority) of nodes to be
  reachable before it processes any request. During a partition
  where only minority nodes are reachable, ZooKeeper returns errors.

  In our simulation above:
    AP choice → Primary accepted writes; replica was stale/unreachable
    CP choice → Would refuse primary writes until replica confirms
""")

    # ── Phase 5: Heal the partition ───────────────────────────────
    section("Phase 5: Healing the Partition — WAL Catchup")

    print("  Restarting postgres-replica...")
    result = subprocess.run(
        ["docker", "compose", "start", "postgres-replica"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        subprocess.run(
            ["docker", "start", "02-cap-theorem-postgres-replica-1"],
            capture_output=True, text=True
        )

    print("  Waiting for replica to reconnect and replay WAL...")
    replica3 = wait_for_replica(REPLICA_DSN, "replica", max_attempts=20)
    if replica3:
        # Wait for WAL catchup: poll until replica has the latest balance
        print("  Polling replica until it catches up to primary writes...")
        lag = poll_replica_for_row(replica3, 600, timeout=30)
        if lag is not None:
            print(f"  Replica caught up in {lag:.2f}s via WAL replay.")
        else:
            print("  Replica is still catching up (WAL replay in progress).")

        print("\n  [Read] After partition heals — replica auto-synced via streaming:")
        read_accounts(primary, "PRIMARY")
        read_accounts(replica3, "REPLICA (WAL caught up)")

        print("\n  pg_stat_replication after healing:")
        show_replication_status(primary)
    else:
        print("  Replica did not come back in time. Check: docker compose logs postgres-replica")

    # ── Summary ───────────────────────────────────────────────────
    section("Summary")
    print("""
  CAP Theorem Takeaways:
  ─────────────────────
  1. P (Partition Tolerance) is NON-NEGOTIABLE for distributed
     systems. Networks fail. You must decide: CP or AP.

  2. CP systems prioritize CONSISTENCY:
     - Return errors if quorum not reached
     - Examples: ZooKeeper, HBase, etcd, MongoDB (majority writes)

  3. AP systems prioritize AVAILABILITY:
     - Return (possibly stale) data instead of erroring
     - Resolve conflicts after partition heals
     - Examples: Cassandra, DynamoDB, CouchDB

  4. PACELC extends CAP: even without partitions, there is a
     latency vs consistency trade-off. Synchronous replication
     adds latency; async replication risks staleness.

  5. "Consistency" in CAP means LINEARIZABILITY — every read
     reflects the most recent write. This is stronger than
     SQL ACID consistency.

  6. Real streaming replication (WAL-based) means:
     - No manual data copying required
     - pg_stat_replication shows live lag in bytes and LSN
     - Replica reconnects and replays missed WAL automatically

  Next steps: ../03-consistency-models/
""")


if __name__ == "__main__":
    main()
