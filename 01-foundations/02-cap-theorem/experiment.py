#!/usr/bin/env python3
"""
CAP Theorem Lab — Consistency vs Availability under Network Partition

Prerequisites: docker compose up -d (wait ~20s for streaming replication to establish)

What this demonstrates:
  - Real PostgreSQL streaming replication (WAL-based, not manual data copy)
  - pg_stat_replication shows live replication state on primary
  - Under normal operation: write to primary propagates automatically to replica
  - AP behavior: primary stays available when replica is partitioned away
  - CP behavior: synchronous_commit=remote_write blocks writes until replica acks
  - After heal: replica reconnects and catches up via WAL replay
  - Session guarantees: read-your-writes using LSN comparison

Phases:
  1. Normal operation — streaming replication established
  2. Network partition — replica stopped, divergence begins
  3. AP behavior — primary accepts writes, replica is stale/unreachable
  4. CP behavior — synchronous_commit forces write to block (timeout = partition)
  5. Partition healing — WAL catchup measured live
  6. Session guarantee demo — read-your-writes via LSN
"""

import os
import subprocess
import time

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

# Resolve the directory containing this script so docker compose commands work
# regardless of the caller's cwd.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def connect(dsn, label):
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        return conn
    except Exception as e:
        print(f"  [!] Cannot connect to {label}: {e}")
        return None


def wait_for_postgres(dsn, label, max_attempts=20, delay=2):
    """Poll until the Postgres instance is accepting connections."""
    for attempt in range(max_attempts):
        conn = connect(dsn, label)
        if conn:
            return conn
        print(f"  Waiting for {label} to be ready... ({attempt + 1}/{max_attempts})")
        time.sleep(delay)
    return None


def docker_compose(*args):
    """Run a docker compose command from the script's own directory."""
    cmd = ["docker", "compose"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=SCRIPT_DIR)
    return result


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
            formatted = ", ".join(f"{r['name']}=${r['balance']}" for r in rows)
            print(f"  [{label}] {formatted}")
            return rows
    except Exception as e:
        print(f"  [!] Read from {label} failed: {e}")
        return None


def write_account(conn, name, balance, label="PRIMARY"):
    """Write an account balance and return the LSN of the write."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE accounts SET balance = %s, updated_at = now() WHERE name = %s",
                (balance, name)
            )
            cur.execute("SELECT pg_current_wal_lsn()")
            lsn = cur.fetchone()[0]
        print(f"  [{label}] SET {name} balance={balance}  (WAL LSN: {lsn})")
        return lsn
    except Exception as e:
        print(f"  [!] Write to {label} failed: {e}")
        return None


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
                print("  pg_stat_replication: no connected replicas (partition in effect)")
            return rows
    except Exception as e:
        print(f"  [!] pg_stat_replication query failed: {e}")
        return []


def poll_replica_for_balance(replica_conn, expected_balance, timeout=15):
    """
    Poll replica until it reflects the expected balance.
    Returns elapsed time in seconds, or None on timeout.
    Surfaces connection errors explicitly so they are not silently swallowed.
    """
    start = time.perf_counter()
    while time.perf_counter() - start < timeout:
        try:
            with replica_conn.cursor() as cur:
                cur.execute("SELECT balance FROM accounts WHERE name = 'Alice'")
                row = cur.fetchone()
                if row and row[0] == expected_balance:
                    return time.perf_counter() - start
        except psycopg2.OperationalError as e:
            print(f"  [!] Replica connection error during poll: {e}")
            return None
        except Exception:
            pass
        time.sleep(0.02)
    return None


def replica_has_caught_up(replica_conn, lsn_str):
    """Return True if the replica's replay LSN >= the given LSN string."""
    try:
        with replica_conn.cursor() as cur:
            cur.execute(
                "SELECT pg_last_wal_replay_lsn() >= %s::pg_lsn",
                (lsn_str,)
            )
            row = cur.fetchone()
            return row and row[0]
    except Exception:
        return False


def section(title):
    print(f"\n{'=' * 65}")
    print(f"  {title}")
    print("=" * 65)


def main():
    section("CAP THEOREM LAB — PostgreSQL Streaming Replication")
    print("""
  CAP Theorem: a distributed system can guarantee at most two of:
    C — Consistency     (every read sees the latest write = linearizability)
    A — Availability    (every request to a live node gets a non-error response)
    P — Partition Tol.  (system continues despite dropped network messages)

  Since network partitions WILL happen, the real choice is: CP or AP.

  This lab uses real PostgreSQL streaming replication (WAL-based).
  Writes to the primary flow as WAL records to the replica automatically.
""")

    # ── Phase 1: Normal operation ──────────────────────────────────────────
    section("Phase 1: Normal Operation — Streaming Replication Established")

    primary = connect(PRIMARY_DSN, "primary")
    if not primary:
        print("  ERROR: Primary not reachable. Run 'docker compose up -d' first.")
        return

    print("  Waiting for replica's pg_basebackup + startup to finish...")
    replica = wait_for_postgres(REPLICA_DSN, "replica")
    if not replica:
        print("  WARNING: Replica not reachable after retries. Continuing with primary only.")

    print("\n  pg_stat_replication on primary (shows connected replicas):")
    show_replication_status(primary)

    print("\n  Seeding schema and data on PRIMARY only (WAL will propagate it)...")
    setup_table(primary)
    time.sleep(1)  # let WAL stream

    print("\n  Reads immediately after seed write:")
    read_accounts(primary, "PRIMARY")
    if replica:
        read_accounts(replica, "REPLICA ")

    if replica:
        print("\n  Measuring replication lag for a fresh write...")
        write_account(primary, "Alice", 950, "PRIMARY")
        lag = poll_replica_for_balance(replica, 950)
        if lag is not None:
            print(f"  Replica reflected write in {lag * 1000:.1f} ms  (real WAL streaming lag)")
        else:
            print("  Replica did not reflect write within timeout — check docker compose logs")

        print("\n  Both nodes after write:")
        read_accounts(primary, "PRIMARY")
        read_accounts(replica, "REPLICA ")

        print("\n  pg_stat_replication after writes:")
        show_replication_status(primary)

    # ── Phase 2: Simulate Network Partition ────────────────────────────────
    section("Phase 2: Network Partition — Stopping the Replica Container")
    print("  Stopping postgres-replica to simulate a network split...")
    result = docker_compose("stop", "postgres-replica")
    if result.returncode != 0:
        print(f"  [!] docker compose stop failed: {result.stderr.strip()}")
        # Fallback: try direct container name
        subprocess.run(["docker", "stop", "02-cap-theorem-postgres-replica-1"],
                       capture_output=True, text=True)
    print("  Replica is now unreachable — partition simulated.")

    time.sleep(2)
    print("\n  pg_stat_replication now (replica gone):")
    show_replication_status(primary)

    # ── Phase 3: AP behavior ───────────────────────────────────────────────
    section("Phase 3: AP Choice — Primary Stays Available During Partition")
    print("""  AP systems (Cassandra, DynamoDB, CouchDB) choose AVAILABILITY:
    → Accept reads/writes on surviving nodes regardless of replica state
    → Return potentially stale data rather than refusing requests
    → Reconcile conflicts (LWW, vector clocks, CRDTs) when partition heals
""")
    print("  Writing to PRIMARY during partition (no replica confirmation needed)...")
    ap_lsn = write_account(primary, "Alice", 800, "PRIMARY (AP)")
    write_account(primary, "Bob", 300, "PRIMARY (AP)")
    print()
    read_accounts(primary, "PRIMARY (AP) — current data")

    print("\n  Attempting read from REPLICA during partition...")
    stale_replica = connect(REPLICA_DSN, "replica")
    if stale_replica is None:
        print("  REPLICA is unreachable — connection refused (partition confirmed).")
        print("  AP system behavior: primary wrote successfully; replica is stale/gone.")
        print("  Application must tolerate that reads from replica return old data or fail.")
    else:
        print("  (Replica still connectable — reading stale state:)")
        read_accounts(stale_replica, "REPLICA (stale)")

    # ── Phase 4: CP behavior ───────────────────────────────────────────────
    section("Phase 4: CP Choice — Synchronous Replication Blocks During Partition")
    print("""  CP systems (ZooKeeper, HBase, etcd, PostgreSQL with sync replication)
  choose CONSISTENCY:
    → Refuse or block reads/writes if the system cannot guarantee all nodes agree
    → Return an error rather than serving stale or potentially conflicting data
    → System is "unavailable" during the partition — this is intentional

  PostgreSQL's synchronous_commit=remote_write makes writes wait for the
  replica to confirm before returning. During a partition with no replica
  reachable, this write will block until statement_timeout fires.
""")

    print("  Configuring primary for synchronous CP mode...")
    try:
        with primary.cursor() as cur:
            # synchronous_standby_names must be set to require replica acks.
            # We use FIRST 1 (*) — wait for any one standby.
            cur.execute("ALTER SYSTEM SET synchronous_standby_names = 'FIRST 1 (*)'")
            cur.execute("SELECT pg_reload_conf()")
        print("  synchronous_standby_names = 'FIRST 1 (*)' — primary now waits for replica WAL ack")
        time.sleep(1)

        # Use a short statement_timeout so we don't hang forever
        with primary.cursor() as cur:
            cur.execute("SET statement_timeout = '4s'")
            cur.execute("SET synchronous_commit = 'remote_write'")

        print("  Attempting write with synchronous_commit=remote_write (replica is DOWN)...")
        print("  Expect this to BLOCK then TIMEOUT — demonstrating CP unavailability...\n")
        start = time.perf_counter()
        try:
            with primary.cursor() as cur:
                cur.execute("SET statement_timeout = '4s'")
                cur.execute("SET synchronous_commit = 'remote_write'")
                cur.execute(
                    "UPDATE accounts SET balance = %s, updated_at = now() WHERE name = %s",
                    (700, "Alice")
                )
            elapsed = time.perf_counter() - start
            print(f"  Write returned in {elapsed:.2f}s — replica may have reconnected unexpectedly.")
        except psycopg2.errors.QueryCanceled:
            elapsed = time.perf_counter() - start
            print(f"  Write TIMED OUT after {elapsed:.1f}s — CP system sacrificed AVAILABILITY.")
            print("  The primary refused to acknowledge the write without replica confirmation.")
            print("  This is ZooKeeper/etcd behavior: correct data or no response.")
        except Exception as e:
            elapsed = time.perf_counter() - start
            print(f"  Write failed after {elapsed:.1f}s: {e}")

    except Exception as e:
        print(f"  [!] Could not configure synchronous mode: {e}")
    finally:
        # Reset to async so partition healing works normally
        try:
            with primary.cursor() as cur:
                cur.execute("ALTER SYSTEM SET synchronous_standby_names = ''")
                cur.execute("SELECT pg_reload_conf()")
                cur.execute("SET synchronous_commit = 'local'")
                cur.execute("SET statement_timeout = 0")
            print("\n  Reset to async replication (synchronous_standby_names = '') for Phase 5.")
        except Exception:
            pass

    # ── Phase 5: Heal the partition ────────────────────────────────────────
    section("Phase 5: Healing the Partition — WAL Catchup")
    print("  Restarting postgres-replica...")
    result = docker_compose("start", "postgres-replica")
    if result.returncode != 0:
        print(f"  [!] docker compose start failed: {result.stderr.strip()}")
        subprocess.run(["docker", "start", "02-cap-theorem-postgres-replica-1"],
                       capture_output=True, text=True)

    print("  Waiting for replica to reconnect and replay missed WAL records...")
    healed_replica = wait_for_postgres(REPLICA_DSN, "replica", max_attempts=25, delay=2)

    if healed_replica:
        print("  Polling until replica catches up to primary's LSN...")
        heal_start = time.perf_counter()
        # Poll until replica shows Alice's balance from the AP write (800)
        lag = poll_replica_for_balance(healed_replica, 800, timeout=30)
        heal_elapsed = time.perf_counter() - heal_start
        if lag is not None:
            print(f"  Replica caught up in {heal_elapsed:.2f}s via WAL replay.")
        else:
            print("  Replica is still catching up (WAL replay in progress — check logs).")

        print("\n  Both nodes after partition heals:")
        read_accounts(primary, "PRIMARY            ")
        read_accounts(healed_replica, "REPLICA (caught up)")

        print("\n  pg_stat_replication after healing:")
        show_replication_status(primary)
    else:
        print("  Replica did not reconnect in time.")
        print("  Check: docker compose logs postgres-replica")

    # ── Phase 6: Session guarantee — read-your-writes via LSN ─────────────
    section("Phase 6: Session Guarantee — Read-Your-Writes via LSN Tracking")
    print("""  In AP systems, a client writing to the primary might then read from
  a replica that hasn't yet applied that write — violating read-your-writes.

  Solution: attach the write's LSN to the client session. Before serving
  a read, check that the replica's replay LSN >= the client's write LSN.
  If not, either wait, redirect to primary, or serve from primary directly.
""")
    if healed_replica:
        print("  Writing to primary and capturing WAL LSN...")
        ryw_lsn = write_account(primary, "Alice", 750, "PRIMARY")

        if ryw_lsn:
            print(f"\n  Client's write LSN: {ryw_lsn}")
            print("  Checking replica's replay LSN before serving read...")

            wait_start = time.perf_counter()
            caught_up = False
            for _ in range(20):
                if replica_has_caught_up(healed_replica, ryw_lsn):
                    caught_up = True
                    break
                time.sleep(0.1)
            wait_elapsed = time.perf_counter() - wait_start

            if caught_up:
                print(f"  Replica replay LSN >= write LSN after {wait_elapsed * 1000:.1f}ms")
                print("  Safe to serve read-your-writes from replica:")
                read_accounts(healed_replica, "REPLICA (read-your-writes safe)")
            else:
                print("  Replica has not yet replayed write LSN — redirect read to PRIMARY")
                read_accounts(primary, "PRIMARY (fallback for read-your-writes)")
    else:
        print("  Skipping — replica is not available.")

    # ── Summary ────────────────────────────────────────────────────────────
    section("Summary")
    print("""
  CAP Theorem Takeaways:
  ──────────────────────
  1. P (Partition Tolerance) is NON-NEGOTIABLE for distributed systems.
     Networks fail. Choose: CP or AP.

  2. CP systems prioritize CONSISTENCY:
     - Return errors or block if quorum / replica not reachable
     - Examples: ZooKeeper, HBase, etcd, PostgreSQL (sync replication)
     - Phase 4 showed: write blocked for ~4s, then timed out — no bad data

  3. AP systems prioritize AVAILABILITY:
     - Return (possibly stale) data; never error for reachability reasons
     - Resolve conflicts after partition heals (LWW, vector clocks, CRDTs)
     - Examples: Cassandra, DynamoDB, CouchDB, PostgreSQL (async replication)
     - Phase 3 showed: primary wrote instantly; replica was stale/unreachable

  4. PACELC extends CAP: even without partitions, there is a latency vs
     consistency trade-off. Synchronous replication adds RTT to every write.

  5. "Consistency" in CAP means LINEARIZABILITY — stronger than ACID.
     ACID consistency = constraints hold. CAP consistency = reads always
     return the latest write, system-wide.

  6. Session guarantees (read-your-writes, monotonic reads) are achievable
     in AP systems via LSN/vector-clock tracking — often sufficient without
     requiring full linearizability.

  7. PostgreSQL lets you switch the dial per-session:
     - synchronous_commit=local   → AP (fast, async to replica)
     - synchronous_commit=remote_write → CP (blocks until replica acks WAL)

  Next steps: ../03-consistency-models/
""")


if __name__ == "__main__":
    main()
