#!/usr/bin/env python3
"""
Replication Lab — Leader-Follower with Real PostgreSQL Streaming Replication

Prerequisites: docker compose up -d (wait ~30s for both replicas to complete
               pg_basebackup and establish streaming replication)

What this demonstrates:
  1. Real pg_stat_replication output (sent_lsn, write_lsn, replay_lsn, sync_state)
  2. Actual replication lag measurement: write to primary, poll replica until data appears
  3. Read scaling across primary + 2 replicas
  4. Replica failure: stop replica1, reads continue from replica2
  5. Replica reconnection: replica1 restarts and catches up via WAL replay
"""

import time
import random
import subprocess
import threading
from collections import defaultdict

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("Installing psycopg2-binary...")
    subprocess.run(["pip", "install", "psycopg2-binary", "-q"], check=True)
    import psycopg2
    import psycopg2.extras

PRIMARY_DSN   = "host=localhost port=5432 dbname=replication_test user=postgres password=postgres connect_timeout=5"
REPLICA1_DSN  = "host=localhost port=5433 dbname=replication_test user=postgres password=postgres connect_timeout=5"
REPLICA2_DSN  = "host=localhost port=5434 dbname=replication_test user=postgres password=postgres connect_timeout=5"

FIRST_NAMES = ["Alice", "Bob", "Carol", "Dave", "Eve", "Frank", "Grace", "Hank",
               "Iris", "Jack", "Karen", "Leo", "Mia", "Nina", "Oscar", "Pam"]
LAST_NAMES  = ["Smith", "Jones", "Williams", "Brown", "Davis", "Miller", "Wilson"]


def connect(dsn, label):
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        return conn
    except Exception as e:
        print(f"  [!] Cannot connect to {label}: {e}")
        return None


def wait_for_replica(dsn, label, max_attempts=20):
    """Wait for replica to become available after pg_basebackup + startup."""
    for attempt in range(max_attempts):
        conn = connect(dsn, label)
        if conn:
            return conn
        print(f"  Waiting for {label}... ({attempt + 1}/{max_attempts})")
        time.sleep(2)
    return None


def setup_schema(conn, label):
    """Only called on primary; replicas receive schema via WAL streaming."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id       SERIAL PRIMARY KEY,
                name     TEXT NOT NULL,
                email    TEXT NOT NULL,
                created  TIMESTAMPTZ DEFAULT now()
            )
        """)
        cur.execute("TRUNCATE users RESTART IDENTITY")
    print(f"  Schema ready on {label} (will stream to replicas automatically)")


def insert_user(conn, name, email):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (name, email) VALUES (%s, %s) RETURNING id",
            (name, email)
        )
        return cur.fetchone()[0]


def count_users(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM users")
        return cur.fetchone()[0]


def show_replication_status(conn):
    """Display pg_stat_replication with LSN positions and lag."""
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    application_name,
                    client_addr,
                    state,
                    sent_lsn,
                    write_lsn,
                    flush_lsn,
                    replay_lsn,
                    sync_state,
                    (sent_lsn - replay_lsn) AS lag_bytes
                FROM pg_stat_replication
                ORDER BY application_name
            """)
            rows = cur.fetchall()
            if rows:
                print(f"  pg_stat_replication ({len(rows)} replica(s) connected):")
                for r in rows:
                    print(f"    [{r['application_name']}] client={r['client_addr']}  "
                          f"state={r['state']}  sync={r['sync_state']}")
                    print(f"      sent_lsn={r['sent_lsn']}  write_lsn={r['write_lsn']}  "
                          f"replay_lsn={r['replay_lsn']}  lag_bytes={r['lag_bytes']}")
            else:
                print("  pg_stat_replication: no connected replicas")
            return rows
    except Exception as e:
        print(f"  [!] pg_stat_replication failed: {e}")
        return []


def measure_replication_lag(primary_conn, replica_conn, replica_label):
    """
    Write a sentinel row to primary, then poll replica until it appears.
    Returns measured lag in milliseconds.
    """
    sentinel_email = f"lag-probe-{time.time_ns()}@example.com"
    t0 = time.perf_counter()
    with primary_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (name, email) VALUES (%s, %s)",
            ("__lag_probe__", sentinel_email)
        )
    # Poll replica
    deadline = time.perf_counter() + 5.0
    while time.perf_counter() < deadline:
        try:
            with replica_conn.cursor() as cur:
                cur.execute("SELECT 1 FROM users WHERE email = %s", (sentinel_email,))
                if cur.fetchone():
                    lag_ms = (time.perf_counter() - t0) * 1000
                    return lag_ms
        except Exception:
            pass
        time.sleep(0.001)
    return None


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def main():
    section("REPLICATION LAB")
    print("""
  Leader-Follower (Primary-Replica) replication:
    - All writes go to primary
    - Replicas receive changes via WAL streaming automatically
    - Increases read throughput; provides redundancy

  This lab uses REAL PostgreSQL streaming replication.
  pg_basebackup seeds each replica; WAL streaming keeps them in sync.
""")

    primary = connect(PRIMARY_DSN, "primary")
    if not primary:
        print("  ERROR: Primary not reachable. Run 'docker compose up -d' first.")
        return

    print("  Waiting for replicas to complete pg_basebackup and connect...")
    replica1 = wait_for_replica(REPLICA1_DSN, "replica1")
    replica2 = wait_for_replica(REPLICA2_DSN, "replica2")

    available_replicas = [(r, f"replica{i+1}") for i, r in enumerate([replica1, replica2]) if r]
    print(f"\n  Connected nodes: primary + {len(available_replicas)} replica(s)")

    # ── Phase 1: Schema setup (primary only) ──────────────────────
    section("Phase 1: Schema Setup")

    setup_schema(primary, "primary")
    print("  Replicas receive schema automatically via WAL — no manual setup needed.")
    time.sleep(1)  # Allow WAL to stream schema to replicas

    # ── Phase 2: pg_stat_replication ──────────────────────────────
    section("Phase 2: pg_stat_replication — Real Replication State")

    print("  Querying pg_stat_replication on primary:")
    show_replication_status(primary)

    # ── Phase 3: Write throughput on primary ──────────────────────
    section("Phase 3: Write Performance (primary only)")

    WRITE_COUNT = 200
    print(f"  Inserting {WRITE_COUNT} users into primary...")
    write_start = time.perf_counter()
    for i in range(WRITE_COUNT):
        name  = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
        email = f"user{i}@example.com"
        insert_user(primary, name, email)
    write_elapsed = time.perf_counter() - write_start
    write_qps = WRITE_COUNT / write_elapsed

    print(f"  Wrote {WRITE_COUNT} rows in {write_elapsed:.3f}s ({write_qps:.0f} writes/sec)")

    # ── Phase 4: Real replication lag measurement ──────────────────
    section("Phase 4: Real Replication Lag Measurement")

    print("""  Measuring actual WAL streaming lag:
    1. Write a sentinel row to primary
    2. Poll replica until the row appears
    3. Elapsed time = true replication lag
""")

    primary_count = count_users(primary)
    print(f"  Primary has: {primary_count} rows")

    for replica_conn, replica_label in available_replicas:
        # Give WAL a moment to stream the bulk writes
        time.sleep(0.1)
        r_count = count_users(replica_conn)
        print(f"  {replica_label} has: {r_count} rows  "
              f"(lag: {primary_count - r_count} rows behind)")

    print()
    for replica_conn, replica_label in available_replicas:
        lag_ms = measure_replication_lag(primary, replica_conn, replica_label)
        if lag_ms is not None:
            print(f"  Replication lag to {replica_label}: {lag_ms:.2f}ms (real WAL streaming)")
        else:
            print(f"  {replica_label}: lag probe timed out")

    print("\n  pg_stat_replication after bulk writes:")
    show_replication_status(primary)

    # Allow full catchup
    time.sleep(1)
    print("\n  Row counts after WAL catchup:")
    print(f"  Primary:  {count_users(primary)} rows")
    for replica_conn, replica_label in available_replicas:
        print(f"  {replica_label}: {count_users(replica_conn)} rows")

    # ── Phase 5: Read scaling ──────────────────────────────────────
    section("Phase 5: Read Scaling — Primary Only vs Primary+Replicas")

    READ_COUNT = 300
    all_nodes = [primary] + [r for r, _ in available_replicas]
    node_labels = ["primary"] + [lbl for _, lbl in available_replicas]

    # Scenario A: reads only from primary
    print(f"  [A] {READ_COUNT} reads from PRIMARY only...")
    start_a = time.perf_counter()
    for _ in range(READ_COUNT):
        uid = random.randint(1, WRITE_COUNT)
        with primary.cursor() as cur:
            cur.execute("SELECT name, email FROM users WHERE id = %s", (uid,))
            cur.fetchone()
    elapsed_a = time.perf_counter() - start_a

    # Scenario B: reads distributed round-robin across all nodes
    print(f"  [B] {READ_COUNT} reads distributed across {len(all_nodes)} nodes (round-robin)...")
    start_b = time.perf_counter()
    node_read_counts = defaultdict(int)
    for i in range(READ_COUNT):
        node = all_nodes[i % len(all_nodes)]
        label = node_labels[i % len(all_nodes)]
        uid = random.randint(1, WRITE_COUNT)
        with node.cursor() as cur:
            cur.execute("SELECT name, email FROM users WHERE id = %s", (uid,))
            cur.fetchone()
        node_read_counts[label] += 1
    elapsed_b = time.perf_counter() - start_b

    qps_a = READ_COUNT / elapsed_a
    qps_b = READ_COUNT / elapsed_b
    print(f"""
  Results:
    Primary-only:              {elapsed_a:.3f}s  ({qps_a:.0f} reads/sec)
    Distributed read scaling:  {elapsed_b:.3f}s  ({qps_b:.0f} reads/sec)
    Speedup from read replicas: {qps_b/qps_a:.1f}x

  Distribution across nodes:""")
    for label, count in node_read_counts.items():
        print(f"    {label}: {count} reads")

    # ── Phase 6: Replica failure and catchup ──────────────────────
    section("Phase 6: Replica Failure — Graceful Degradation")

    print("  Stopping replica1 to simulate a replica failure...")
    r1_result = subprocess.run(
        ["docker", "compose", "stop", "postgres-replica1"],
        capture_output=True, text=True
    )
    if r1_result.returncode != 0:
        subprocess.run(["docker", "stop", "04-replication-postgres-replica1-1"],
                       capture_output=True, text=True)
    # Close its connection immediately so fallback doesn't hang on a dead TCP socket
    try:
        replica1.close()
    except Exception:
        pass
    replica1 = None
    time.sleep(1)

    print("\n  pg_stat_replication after replica1 failure:")
    show_replication_status(primary)

    print("\n  Attempting reads — should fall back to replica2 and primary...")
    success = fail = 0
    for i in range(20):
        node_order = [replica1, replica2, primary] if replica1 else [replica2, primary]
        for node in node_order:
            if node is None:
                continue
            try:
                uid = random.randint(1, WRITE_COUNT)
                with node.cursor() as cur:
                    cur.execute("SELECT name FROM users WHERE id = %s", (uid,))
                    cur.fetchone()
                success += 1
                break
            except Exception:
                fail += 1

    print(f"  20 reads attempted: {success} succeeded, {fail} routed away from dead replica")
    print("  System continues serving reads — replica failure is non-fatal")

    # Write more rows while replica1 is down
    print("\n  Writing 10 more rows to primary while replica1 is down...")
    for i in range(10):
        insert_user(primary, "OfflineWrite", f"offline{i}@example.com")
    print(f"  Primary now has: {count_users(primary)} rows")

    # Restart replica1 and observe catchup
    section("Phase 7: Replica Reconnection — WAL Catchup")

    print("  Restarting replica1...")
    subprocess.run(["docker", "compose", "start", "postgres-replica1"],
                   capture_output=True, text=True)

    print("  Waiting for replica1 to reconnect and replay missed WAL...")
    replica1_new = wait_for_replica(REPLICA1_DSN, "replica1", max_attempts=20)
    if replica1_new:
        # Poll until replica1 catches up
        target = count_users(primary)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                r1_count = count_users(replica1_new)
                if r1_count >= target:
                    print(f"  replica1 caught up: {r1_count} rows (primary: {target})")
                    break
                print(f"  replica1: {r1_count}/{target} rows (catching up...)")
            except Exception:
                pass
            time.sleep(1)

        print("\n  Final pg_stat_replication after catchup:")
        show_replication_status(primary)
    else:
        print("  replica1 did not come back in time. Check: docker compose logs postgres-replica1")

    # ── Summary ───────────────────────────────────────────────────
    section("Summary")
    print("""
  Replication Key Concepts:
  ─────────────────────────
  Streaming replication (PostgreSQL default):
    + Replica receives WAL records continuously, low lag
    + pg_stat_replication shows real-time sent/write/replay LSN
    + Replica reconnects and replays missed WAL automatically
    - Async by default: data loss possible if primary crashes

  pg_stat_replication columns:
    sent_lsn    — LSN sent from primary's WAL sender
    write_lsn   — LSN written to replica's WAL buffer
    flush_lsn   — LSN flushed to replica's disk
    replay_lsn  — LSN applied to replica's data files
    lag_bytes   — sent_lsn - replay_lsn (replication lag)

  Sync replication (synchronous_standby_names):
    + Zero data loss (primary waits for replica flush/apply)
    - Write latency increases by replica round-trip
    - Availability decreases if any sync replica is down

  Leader-follower pattern:
    - One primary accepts writes
    - N replicas serve reads
    - Read QPS scales with replica count
    - Writes still bottleneck at single primary

  Multi-leader / Leaderless (Cassandra, DynamoDB):
    - Multiple nodes accept writes
    - Higher write availability
    - Conflict resolution required (last-write-wins, CRDTs)

  Next steps: ../05-partitioning-sharding/
""")


if __name__ == "__main__":
    main()
