#!/usr/bin/env python3
"""
Replication Lab — Leader-Follower with Real PostgreSQL Streaming Replication

Prerequisites: docker compose up -d (wait ~30s for both replicas to complete
               pg_basebackup and establish streaming replication)

What this demonstrates:
  1. Real pg_stat_replication output (sent_lsn, write_lsn, flush_lsn, replay_lsn, sync_state)
  2. Actual replication lag measurement: write to primary, poll replica until data appears
  3. synchronous_commit = off  vs  on — durability/latency trade-off on a SINGLE connection
  4. Read scaling: concurrent threads across primary + 2 replicas vs primary-only
  5. Read-your-own-writes hazard: write to primary, read immediately from replica
  6. Replica failure: stop replica1, reads continue from replica2
  7. Replica reconnection: replica1 restarts and replays missed WAL
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

PRIMARY_DSN  = "host=localhost port=5432 dbname=replication_test user=postgres password=postgres connect_timeout=5"
REPLICA1_DSN = "host=localhost port=5433 dbname=replication_test user=postgres password=postgres connect_timeout=5"
REPLICA2_DSN = "host=localhost port=5434 dbname=replication_test user=postgres password=postgres connect_timeout=5"

FIRST_NAMES = ["Alice", "Bob", "Carol", "Dave", "Eve", "Frank", "Grace", "Hank",
               "Iris", "Jack", "Karen", "Leo", "Mia", "Nina", "Oscar", "Pam"]
LAST_NAMES  = ["Smith", "Jones", "Williams", "Brown", "Davis", "Miller", "Wilson"]


# ── Connection helpers ────────────────────────────────────────────────────────

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


# ── Schema / data helpers ─────────────────────────────────────────────────────

def setup_schema(conn):
    """Only called on primary; replicas receive schema via WAL streaming."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id       SERIAL PRIMARY KEY,
                name     TEXT NOT NULL,
                email    TEXT UNIQUE NOT NULL,
                created  TIMESTAMPTZ DEFAULT now()
            )
        """)
        cur.execute("TRUNCATE users RESTART IDENTITY")
    print("  Schema ready on primary (streams to replicas automatically via WAL)")


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


# ── Replication status display ────────────────────────────────────────────────

def show_replication_status(conn):
    """Display pg_stat_replication with LSN positions and byte lag."""
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
                    pg_wal_lsn_diff(sent_lsn, replay_lsn) AS lag_bytes
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
                          f"flush_lsn={r['flush_lsn']}  replay_lsn={r['replay_lsn']}")
                    print(f"      lag_bytes={r['lag_bytes']}  "
                          f"(pg_wal_lsn_diff: bytes primary has sent but replica hasn't replayed)")
            else:
                print("  pg_stat_replication: no connected replicas")
            return rows
    except Exception as e:
        print(f"  [!] pg_stat_replication failed: {e}")
        return []


# ── Lag measurement ───────────────────────────────────────────────────────────

def measure_replication_lag(primary_conn, replica_conn, replica_label):
    """
    Write a sentinel row to primary, then poll replica until it appears.
    Returns measured end-to-end lag in milliseconds (true WAL streaming latency).
    """
    sentinel_email = f"lag-probe-{time.time_ns()}@example.com"
    t0 = time.perf_counter()
    with primary_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (name, email) VALUES (%s, %s)",
            ("__lag_probe__", sentinel_email)
        )
    deadline = time.perf_counter() + 5.0
    while time.perf_counter() < deadline:
        try:
            with replica_conn.cursor() as cur:
                cur.execute("SELECT 1 FROM users WHERE email = %s", (sentinel_email,))
                if cur.fetchone():
                    return (time.perf_counter() - t0) * 1000
        except Exception:
            pass
        time.sleep(0.001)
    return None


# ── Concurrent read-scaling benchmark ────────────────────────────────────────

def _read_worker(conn, label, read_count, max_id, results):
    """Thread worker: perform read_count point lookups, record throughput."""
    start = time.perf_counter()
    for _ in range(read_count):
        uid = random.randint(1, max_id)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT name, email FROM users WHERE id = %s", (uid,))
                cur.fetchone()
        except Exception:
            pass
    elapsed = time.perf_counter() - start
    results[label] = {"elapsed": elapsed, "reads": read_count,
                      "qps": read_count / elapsed if elapsed > 0 else 0}


def run_concurrent_reads(nodes_with_labels, reads_per_node, max_id):
    """
    Spawn one thread per node, each performing reads_per_node queries concurrently.
    Returns a dict of label -> {elapsed, reads, qps}.
    """
    results = {}
    threads = []
    for conn, label in nodes_with_labels:
        t = threading.Thread(target=_read_worker,
                             args=(conn, label, reads_per_node, max_id, results))
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


# ── Output helpers ────────────────────────────────────────────────────────────

def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    section("REPLICATION LAB")
    print("""
  Leader-Follower (Primary-Replica) replication:
    - All writes go to primary
    - Replicas receive changes via WAL streaming automatically
    - Read throughput scales with replica count
    - Async by default: replicas may lag behind primary

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

    available_replicas = [(r, f"replica{i+1}")
                          for i, r in enumerate([replica1, replica2]) if r]
    print(f"\n  Connected nodes: primary + {len(available_replicas)} replica(s)")

    # ── Phase 1: Schema setup ──────────────────────────────────────
    section("Phase 1: Schema Setup")
    setup_schema(primary)
    print("  Replicas receive schema automatically via WAL — no manual DDL needed.")
    time.sleep(1)

    # ── Phase 2: pg_stat_replication ──────────────────────────────
    section("Phase 2: pg_stat_replication — Real Replication State")
    print("  Querying pg_stat_replication on primary:")
    show_replication_status(primary)
    print("""
  Key columns:
    sent_lsn   — WAL position the primary has sent to this replica
    write_lsn  — WAL position the replica has written to its local WAL buffer
    flush_lsn  — WAL position durably flushed to replica's disk
    replay_lsn — WAL position applied to replica's data files (visible to queries)
    lag_bytes  — pg_wal_lsn_diff(sent_lsn, replay_lsn): bytes in-flight

  sync_state values:
    async      — primary does NOT wait for this replica before ack'ing writes
    sync       — primary waits for this replica's flush before ack'ing
    potential  — standby replica, will become sync if the current sync fails
""")

    # ── Phase 3: Write throughput ──────────────────────────────────
    section("Phase 3: Write Performance — synchronous_commit ON vs OFF")

    WRITE_COUNT = 200

    # Batch A: default synchronous_commit=on (WAL flushed to primary disk per commit)
    print(f"  [A] {WRITE_COUNT} writes with synchronous_commit=ON (default)...")
    start_a = time.perf_counter()
    for i in range(WRITE_COUNT):
        name  = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
        email = f"sync-{i}@example.com"
        insert_user(primary, name, email)
    elapsed_a = time.perf_counter() - start_a
    qps_a = WRITE_COUNT / elapsed_a

    # Batch B: synchronous_commit=off (WAL write deferred; up to wal_writer_delay ms)
    # NOTE: synchronous_commit=off does NOT disable replication — it only delays the
    # fsync of the WAL on the PRIMARY. Data loss window is wal_writer_delay (200ms
    # default), not the full replication lag. Replicas still receive WAL asynchronously.
    print(f"  [B] {WRITE_COUNT} writes with synchronous_commit=OFF (async local flush)...")
    with primary.cursor() as cur:
        cur.execute("SET synchronous_commit = off")
    start_b = time.perf_counter()
    for i in range(WRITE_COUNT):
        name  = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
        email = f"async-{i}@example.com"
        insert_user(primary, name, email)
    elapsed_b = time.perf_counter() - start_b
    qps_b = WRITE_COUNT / elapsed_b
    with primary.cursor() as cur:
        cur.execute("SET synchronous_commit = on")  # restore default

    print(f"""
  Results:
    synchronous_commit=ON:   {elapsed_a:.3f}s  ({qps_a:.0f} writes/sec)
    synchronous_commit=OFF:  {elapsed_b:.3f}s  ({qps_b:.0f} writes/sec)
    Speedup:                 {qps_b/qps_a:.1f}x

  Why this matters:
    synchronous_commit=ON  — each COMMIT waits for WAL to be flushed to PRIMARY disk.
                             No data loss on primary crash, but higher per-commit latency.
    synchronous_commit=OFF — COMMIT returns before WAL is flushed. Primary can lose up to
                             wal_writer_delay (default 200ms) of commits on crash.
                             Crucially, this is DIFFERENT from async replication:
                             replication to replicas proceeds independently regardless.
    synchronous_standby_names — controls whether the primary also waits for a REPLICA's
                             flush_lsn before acking. Not set here (all replicas are async).
""")

    # ── Phase 4: Real replication lag measurement ──────────────────
    section("Phase 4: Real Replication Lag Measurement")

    print("""  Measuring actual WAL streaming lag:
    1. Write a sentinel row to primary
    2. Poll replica until the row becomes visible
    3. Elapsed time = true end-to-end replication latency
""")

    primary_count = count_users(primary)
    print(f"  Primary has: {primary_count} rows (after both write batches)")
    for replica_conn, replica_label in available_replicas:
        time.sleep(0.1)
        r_count = count_users(replica_conn)
        print(f"  {replica_label} has: {r_count} rows  "
              f"({'caught up' if r_count == primary_count else f'{primary_count - r_count} rows behind'})")

    print()
    for replica_conn, replica_label in available_replicas:
        lag_ms = measure_replication_lag(primary, replica_conn, replica_label)
        if lag_ms is not None:
            print(f"  End-to-end lag to {replica_label}: {lag_ms:.2f}ms")
        else:
            print(f"  {replica_label}: lag probe timed out (replica may be down)")

    print("\n  pg_stat_replication after writes:")
    show_replication_status(primary)

    time.sleep(1)
    print("\n  Row counts after WAL catchup (all nodes should converge):")
    print(f"  Primary:  {count_users(primary)} rows")
    for replica_conn, replica_label in available_replicas:
        print(f"  {replica_label}: {count_users(replica_conn)} rows")

    # ── Phase 5: Read-your-own-writes hazard ──────────────────────
    section("Phase 5: Read-Your-Own-Writes Hazard")

    if available_replicas:
        replica_conn, replica_label = available_replicas[0]
        probe_email = f"ryow-probe-{time.time_ns()}@example.com"
        print(f"""  Scenario: client writes to primary, then immediately reads from {replica_label}.
  In async replication, the replica may not have applied the write yet.
""")
        with primary.cursor() as cur:
            cur.execute("INSERT INTO users (name, email) VALUES (%s, %s)",
                        ("RYOW Test", probe_email))

        # Check replica immediately (before any sleep)
        with replica_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE email = %s", (probe_email,))
            immediate_visible = cur.fetchone() is not None

        if immediate_visible:
            print(f"  Row visible on {replica_label} immediately — WAL lag was < 1ms (lucky timing).")
        else:
            print(f"  Row NOT yet visible on {replica_label} immediately after primary write.")
            print(f"  This is the read-your-own-writes violation: the client just wrote it,")
            print(f"  but a load balancer routing this read to a replica returns 'not found'.")

        # Now wait and confirm it eventually arrives
        deadline = time.perf_counter() + 3.0
        while time.perf_counter() < deadline:
            with replica_conn.cursor() as cur:
                cur.execute("SELECT 1 FROM users WHERE email = %s", (probe_email,))
                if cur.fetchone():
                    lag_ms = (3.0 - (deadline - time.perf_counter())) * 1000
                    print(f"  Row appeared on {replica_label} after ~{lag_ms:.0f}ms.")
                    break
            time.sleep(0.005)

        print("""
  Mitigations:
    1. Route reads-after-writes to the primary for that session.
    2. Read from replica only after confirming replica LSN >= write LSN
       (pg_last_wal_replay_lsn() >= target LSN).
    3. Use synchronous_standby_names so the replica is guaranteed to have the data.
    4. Session-level sticky routing in your load balancer / ORM.
""")

    # ── Phase 6: Concurrent read scaling ──────────────────────────
    section("Phase 6: Read Scaling — Concurrent Throughput Comparison")

    READS_PER_THREAD = 300
    max_id = count_users(primary)

    print(f"  Spawning concurrent read threads ({READS_PER_THREAD} reads each):")
    print(f"  [A] 1 thread -> primary only")
    results_a = run_concurrent_reads([(primary, "primary")], READS_PER_THREAD, max_id)

    all_nodes = [(primary, "primary")] + available_replicas
    node_count = len(all_nodes)
    print(f"  [B] {node_count} threads -> primary + {len(available_replicas)} replica(s) concurrently")
    results_b = run_concurrent_reads(all_nodes, READS_PER_THREAD, max_id)

    total_qps_a = sum(r["qps"] for r in results_a.values())
    total_qps_b = sum(r["qps"] for r in results_b.values())

    print(f"""
  Results (concurrent threads):
    Primary only (1 thread):          {total_qps_a:.0f} reads/sec total
    Distributed ({node_count} threads, all nodes): {total_qps_b:.0f} reads/sec total
    Throughput multiplier:            {total_qps_b/total_qps_a:.1f}x

  Per-node breakdown (scenario B):""")
    for label, r in results_b.items():
        print(f"    {label}: {r['reads']} reads in {r['elapsed']:.3f}s ({r['qps']:.0f} reads/sec)")

    print("""
  Key insight: read replicas scale read THROUGHPUT linearly.
  Each replica handles its own connection pool independently.
  Writes are still bottlenecked at the single primary.
""")

    # ── Phase 7: Replica failure and graceful degradation ─────────
    section("Phase 7: Replica Failure — Graceful Degradation")

    print("  Stopping replica1 to simulate a replica failure...")
    r1_stop = subprocess.run(
        ["docker", "compose", "stop", "postgres-replica1"],
        capture_output=True, text=True
    )
    if r1_stop.returncode != 0:
        # Fallback for older Docker Compose naming conventions
        subprocess.run(["docker", "stop", "04-replication-postgres-replica1-1"],
                       capture_output=True, text=True)
    try:
        replica1.close()
    except Exception:
        pass
    replica1 = None
    time.sleep(1)

    print("\n  pg_stat_replication after replica1 failure (should show 1 replica):")
    show_replication_status(primary)

    # Rebuild available list without replica1
    live_nodes = [(replica2, "replica2"), (primary, "primary")] if replica2 else [(primary, "primary")]

    print(f"\n  Routing 20 reads to live nodes: {[lbl for _, lbl in live_nodes]}...")
    success = 0
    for _ in range(20):
        for node, lbl in live_nodes:
            try:
                uid = random.randint(1, max_id)
                with node.cursor() as cur:
                    cur.execute("SELECT name FROM users WHERE id = %s", (uid,))
                    cur.fetchone()
                success += 1
                break
            except Exception:
                continue

    print(f"  {success}/20 reads succeeded via remaining live nodes")
    print("  System continues serving reads — replica failure is non-fatal for read traffic.")
    print("  Primary still accepts writes — replica failure does NOT affect write availability.")

    # Write more rows while replica1 is down to demonstrate WAL accumulation
    print("\n  Writing 10 more rows to primary while replica1 is down...")
    for i in range(10):
        insert_user(primary, "OfflineWrite", f"offline-{time.time_ns()}-{i}@example.com")
    primary_count_now = count_users(primary)
    print(f"  Primary now has: {primary_count_now} rows")
    print("  These WAL records are buffered; replica1 will replay them on reconnect.")
    print("  (wal_keep_size=256MB ensures WAL isn't recycled before replica1 returns)")

    # ── Phase 8: Replica reconnection and WAL catchup ─────────────
    section("Phase 8: Replica Reconnection — WAL Catchup")

    print("  Restarting replica1...")
    subprocess.run(["docker", "compose", "start", "postgres-replica1"],
                   capture_output=True, text=True)

    print("  Waiting for replica1 to reconnect and replay missed WAL...")
    replica1_new = wait_for_replica(REPLICA1_DSN, "replica1", max_attempts=20)
    if replica1_new:
        target = count_users(primary)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                r1_count = count_users(replica1_new)
                if r1_count >= target:
                    print(f"  replica1 caught up: {r1_count} rows  (primary: {target}) — fully in sync")
                    break
                print(f"  replica1: {r1_count}/{target} rows (replaying WAL...)")
            except Exception:
                pass
            time.sleep(1)

        print("\n  pg_stat_replication after catchup (lag_bytes should be ~0):")
        show_replication_status(primary)
    else:
        print("  replica1 did not come back in time.")
        print("  Check: docker compose logs postgres-replica1")

    # ── Summary ───────────────────────────────────────────────────
    section("Summary")
    print("""
  Replication Key Concepts
  ────────────────────────
  WAL streaming (PostgreSQL physical replication):
    + Replicas are byte-for-byte identical to primary at a given LSN
    + pg_stat_replication: monitor sent/write/flush/replay LSN per replica
    + Replica reconnects and replays missed WAL automatically (within wal_keep_size)
    - Physical: replicates the entire cluster, no partial/table-level filtering

  synchronous_commit (per-session, not about replication mode):
    ON  — each COMMIT waits for WAL fsync on PRIMARY disk (default, safe)
    OFF — WAL fsync deferred by up to wal_writer_delay (200ms). Faster, but
          up to 200ms of commits can be lost on PRIMARY crash. Replicas unaffected.
    REMOTE_WRITE — primary also waits for replica write_lsn (not flush)
    ON + synchronous_standby_names — primary waits for replica flush_lsn (zero data loss)

  pg_stat_replication columns:
    sent_lsn    — WAL sent from primary's WAL sender process
    write_lsn   — written to replica's WAL buffer (in memory)
    flush_lsn   — flushed to replica's disk (durable on replica)
    replay_lsn  — applied to replica's data files (query-visible)
    lag_bytes   — pg_wal_lsn_diff(sent_lsn, replay_lsn)

  Read-your-own-writes (RYOW) consistency:
    - Writing to primary then reading from async replica can miss the write
    - Mitigate: sticky primary routing, LSN-based read routing, or sync replica

  Read scaling:
    - Read QPS scales linearly with replica count (each handles its own connections)
    - Write QPS is bounded by the single primary

  Failure modes to know for interviews:
    - Replication slot bloat: unconnected slot blocks WAL cleanup → disk exhaustion
      Safeguard: max_slot_wal_keep_size (set to 512MB here)
    - Stale replica promoted: async replica missing recent commits becomes new primary
    - Split-brain: old primary accepts writes after new one elected; fencing tokens fix this

  Next steps: ../05-partitioning-sharding/
""")


if __name__ == "__main__":
    main()
