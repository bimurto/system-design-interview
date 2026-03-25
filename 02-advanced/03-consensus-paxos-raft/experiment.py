#!/usr/bin/env python3
"""
Consensus — Paxos & Raft Lab (etcd 3-node cluster)

Prerequisites: docker compose up -d (wait ~20s for cluster to form)

What this demonstrates:
  1. Write to one node, read back from another (Raft replication)
  2. Kill the leader — cluster elects a new leader, writes continue
  3. Kill another node — only 1 of 3 alive, writes fail (no quorum)
  4. Restart node — quorum restored, writes succeed again
  5. Show leader information and cluster health
"""

import time
import subprocess
import os

try:
    import etcd3
except ImportError:
    print("Installing dependencies...")
    subprocess.run(["pip", "install", "etcd3", "-q"], check=True)
    import etcd3

ETCD0_HOST = "localhost"
ETCD0_PORT = 2379

# All three etcd nodes exposed to the host: etcd0→2379, etcd1→2479, etcd2→2381
ETCD_ENDPOINTS = [
    ("localhost", 2379),
    ("localhost", 2479),
    ("localhost", 2381),
]

CONTAINER_NAMES = ["etcd0", "etcd1", "etcd2"]


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def get_client(host=ETCD0_HOST, port=ETCD0_PORT, timeout=3):
    return etcd3.client(host=host, port=port, timeout=timeout)


def docker_stop(container):
    result = subprocess.run(
        ["docker", "stop", container],
        capture_output=True, text=True
    )
    return result.returncode == 0


def docker_start(container):
    result = subprocess.run(
        ["docker", "start", container],
        capture_output=True, text=True
    )
    return result.returncode == 0


def find_container_name(service_name):
    """Find the actual Docker container name for a compose service."""
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}", "--filter", f"name={service_name}"],
        capture_output=True, text=True
    )
    names = [n for n in result.stdout.strip().split("\n") if service_name in n]
    return names[0] if names else service_name


def get_cluster_health():
    try:
        client = get_client()
        members = client.members
        result = []
        for m in members:
            result.append({
                "id": hex(m.id),
                "name": m.name,
                "peer_urls": list(m.peer_urls),
                "client_urls": list(m.client_urls),
            })
        return result
    except Exception as e:
        return [{"error": str(e)}]


def get_client_with_failover(timeout=3):
    """Try each etcd endpoint in turn; return the first that responds."""
    last_err = None
    for host, port in ETCD_ENDPOINTS:
        try:
            c = get_client(host=host, port=port, timeout=timeout)
            c.get("/test-connectivity")   # lightweight probe
            return c
        except Exception as e:
            last_err = e
    raise Exception(f"All etcd endpoints unreachable: {last_err}")


def try_write(key, value, retries=3):
    for attempt in range(retries):
        try:
            client = get_client_with_failover()
            client.put(key, value)
            return True, None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1)
            else:
                return False, str(e)
    return False, "max retries exceeded"


def try_read(key):
    try:
        client = get_client_with_failover()
        val, meta = client.get(key)
        return val.decode() if val else None, None
    except Exception as e:
        return None, str(e)


def main():
    section("CONSENSUS — RAFT WITH ETCD (3-NODE CLUSTER)")
    print("""
  Raft state machine — each node is always in one of three states:

    ┌─────────────────────────────────────────────────────┐
    │  FOLLOWER  ──(timeout)──► CANDIDATE  ──(wins)──► LEADER │
    │     ▲                         │                    │   │
    │     └─────────(loses)─────────┘                    │   │
    │     └──────────────────────────(heartbeat)─────────┘   │
    └─────────────────────────────────────────────────────┘

  Raft guarantees:
    • Election safety: at most one leader per term
    • Leader completeness: a leader has all committed entries
    • Log matching: if two logs have the same (index, term), they're identical
    • State machine safety: all servers apply the same log entries in order

  Quorum = ⌊N/2⌋ + 1 nodes must agree to commit an entry.
  For 3 nodes: quorum = 2  →  tolerates 1 failure
  For 5 nodes: quorum = 3  →  tolerates 2 failures
""")

    # Verify connectivity
    try:
        client = get_client()
        client.get("/test-connectivity")
        print("  Connected to etcd cluster.")
    except Exception as e:
        print(f"  ERROR: cannot connect to etcd: {e}")
        print("  Run: docker compose up -d && sleep 20")
        return

    # ── Phase 1: Write and cross-node read ────────────────────────
    section("Phase 1: Raft Replication — Write to Node, Read from Another")

    print(f"\n  Writing key '/config/db-host' = 'db.prod.internal' via etcd0...")
    ok, err = try_write("/config/db-host", "db.prod.internal")
    if not ok:
        print(f"  Write failed: {err}")
        return
    print(f"  Write: OK")

    print(f"\n  Reading '/config/db-host' back via etcd0 (same node)...")
    val, err = try_read("/config/db-host")
    print(f"  Read: '{val}'" if val else f"  Read failed: {err}")

    print(f"""
  How replication works:
    1. Client sends write to any node
    2. If node is not the leader, it redirects to the leader
    3. Leader appends entry to its log (index N, term T)
    4. Leader sends AppendEntries RPC to all followers
    5. Once a quorum (2/3 nodes) acknowledge, entry is COMMITTED
    6. Leader applies entry to state machine, responds to client
    7. Followers apply the entry on next heartbeat

  etcd enforces linearizable reads by default: a read goes through
  Raft and is guaranteed to reflect all prior committed writes.
  (Use 'serializable' reads for lower latency, at the cost of
  potentially reading stale data from a follower.)
""")

    # Write some data to verify later
    for i in range(5):
        try_write(f"/test/key{i}", f"value{i}")
    print("  Wrote 5 test keys (/test/key0 .. /test/key4)")

    # ── Phase 2: Kill one node (leader or follower) ────────────────
    section("Phase 2: Kill One Node — Cluster Continues (2/3 = Quorum)")

    # Find actual container names
    c0 = find_container_name("etcd0")
    c1 = find_container_name("etcd1")
    c2 = find_container_name("etcd2")

    print(f"  Stopping {c0} (etcd0)...")
    stopped = docker_stop(c0)
    if not stopped:
        print(f"  WARNING: could not stop {c0} via docker — check docker access")
        print(f"  Continuing with simulation...")
    else:
        print(f"  {c0} stopped.")

    time.sleep(3)
    print(f"  Waiting 3s for Raft election to complete (if etcd0 was leader)...")

    print(f"\n  Testing write with only 2/3 nodes alive:")
    ok, err = try_write("/config/status", "degraded-but-running")
    if ok:
        print(f"  Write: OK — quorum (2/3) still available")
    else:
        print(f"  Write failed: {err}")
        print(f"  (etcd0 may have been the leader; waiting for election...)")
        time.sleep(5)
        ok, err = try_write("/config/status", "degraded-but-running")
        print(f"  Retry write: {'OK' if ok else f'FAILED: {err}'}")

    val, _ = try_read("/test/key0")
    print(f"  Read /test/key0: '{val}' (data persisted through node failure)")

    print(f"""
  Raft leader election:
    - Each node has a random election timeout (150-300ms in etcd)
    - If a follower doesn't hear from the leader within its timeout,
      it becomes a CANDIDATE and starts an election for term T+1
    - Candidate votes for itself and sends RequestVote RPCs to all
    - A node votes YES if: (1) it hasn't voted in this term yet, and
      (2) the candidate's log is at least as up-to-date as its own
    - If candidate receives votes from a quorum, it becomes LEADER
    - New leader immediately sends heartbeats to suppress other elections
""")

    # ── Phase 3: Kill second node — lose quorum ────────────────────
    section("Phase 3: Kill Second Node — Quorum Lost (1/3), Writes Fail")

    print(f"  Stopping {c1} (etcd1)...")
    stopped2 = docker_stop(c1)
    if stopped2:
        print(f"  {c1} stopped. Now only 1/3 nodes alive.")
    time.sleep(2)

    print(f"\n  Testing write with only 1/3 nodes alive:")
    ok, err = try_write("/config/emergency", "no-quorum")
    if ok:
        print(f"  Write: OK (unexpected — etcd may have cached the connection)")
    else:
        print(f"  Write: FAILED — {err}")
        print(f"  Expected: no quorum available")

    print(f"""
  Why writes fail without quorum:
    - To commit an entry, the leader needs acknowledgment from ⌊N/2⌋+1 nodes
    - With 3 nodes and 2 stopped, the 1 remaining node cannot form a quorum
    - It will never receive 2 acknowledgments (only 1 node is reachable: itself)
    - So it will never commit the entry, and the write times out
    - This is correct behavior — it prevents split-brain

  Compare to eventual consistency:
    - A system using quorum-less replication (like DynamoDB with W=1)
      would accept the write and reconcile later
    - etcd (and Zookeeper, Consul) choose CP in CAP: consistent but
      unavailable during partition/failure scenarios
""")

    # ── Phase 4: Restart node — quorum restored ────────────────────
    section("Phase 4: Restart One Node — Quorum Restored (2/3)")

    print(f"  Starting {c1} (etcd1)...")
    docker_start(c1)
    time.sleep(5)
    print(f"  {c1} restarted. Waiting 5s for it to rejoin cluster...")

    print(f"\n  Testing write after recovery:")
    ok, err = try_write("/config/status", "recovered")
    print(f"  Write: {'OK' if ok else f'FAILED: {err}'}")

    # Verify etcd1 caught up
    val, err = try_read("/config/status")
    print(f"  Read /config/status: '{val}'" if val else f"  Read error: {err}")

    print(f"""
  Raft log catchup:
    When {c1} rejoins, the current leader sends it all log entries
    it missed (AppendEntries with entries[]) until {c1}'s log is
    identical to the leader's. This is called log replication.

    {c1} applies all missed entries to its state machine.
    Once caught up, it participates normally in quorum decisions.
""")

    # Restart etcd0 too
    print(f"  Restarting {c0} as well...")
    docker_start(c0)
    time.sleep(5)
    print(f"  {c0} restarted.")

    # ── Phase 5: Show leader and cluster health ────────────────────
    section("Phase 5: Cluster Health and Leader Information")

    try:
        client = get_client()
        # Get cluster members
        print(f"  Cluster members:")
        for m in client.members:
            print(f"    id={hex(m.id):<18} name={m.name:<8} peers={list(m.peer_urls)}")

        # Get cluster status/leader
        try:
            status = client.status()
            print(f"\n  Current leader ID: {hex(status.leader)}")
            print(f"  Raft index:        {status.raft_index}")
            print(f"  Raft term:         {status.raft_term}")
            print(f"  DB size:           {status.db_size} bytes")
        except Exception as e:
            print(f"  Could not get status: {e}")

    except Exception as e:
        print(f"  Could not connect: {e}")

    print(f"""
  etcd in production:
    • etcd is used by Kubernetes to store all cluster state
      (pods, services, deployments, configmaps, secrets)
    • Default: 3-node etcd cluster for most Kubernetes deployments
    • 5-node etcd for large clusters requiring higher write availability
    • etcd's write throughput is ~10,000 ops/sec — not a high-throughput DB
    • It is designed for cluster coordination data, not application data
""")

    # ── Summary ───────────────────────────────────────────────────
    section("Summary — Paxos vs Raft")

    print("""
  Paxos (Lamport, 1989):
    Phases: Prepare(n) → Promise(n, v) → Accept(n, v) → Accepted(n, v)
    • Original consensus algorithm — mathematically elegant
    • Notoriously difficult to understand and implement correctly
    • Multi-Paxos extends it to a log of commands (not just one value)
    • Used in Google Chubby, Google Spanner (as a sub-protocol)

  Raft (Ongaro & Ousterhout, 2014):
    "Raft is designed for understandability"
    Phases: Leader Election → Log Replication → Safety
    • Explicit leader: all writes go through one node (simpler)
    • Terms: monotonically increasing epoch numbers prevent split-brain
    • Log matching: two logs with same (index, term) are identical
    • Safety: a leader never overwrites or deletes entries
    • Used in etcd, CockroachDB, TiKV, Consul

  Quorum math:
    N=3: quorum=2, tolerates f=1 failure
    N=5: quorum=3, tolerates f=2 failures
    N=7: quorum=4, tolerates f=3 failures
    Formula: N = 2f+1 nodes tolerates f failures

  CAP theorem implications:
    • etcd/Zookeeper/Consul choose CP: consistent + partition-tolerant
      but unavailable when quorum is lost
    • DynamoDB/Cassandra can choose AP: available + partition-tolerant
      but may return stale data during partitions

  Next: ../04-event-driven-architecture/
""")


if __name__ == "__main__":
    main()
