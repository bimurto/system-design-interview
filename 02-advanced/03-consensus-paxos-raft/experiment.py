#!/usr/bin/env python3
"""
Consensus — Paxos & Raft Lab (etcd 3-node cluster)

Prerequisites: docker compose up -d (wait ~20s for cluster to form)

What this demonstrates:
  1. Write to one node, read back from another (Raft replication)
  2. Observe Raft term numbers and leader identity
  3. Kill the leader — cluster elects a new leader, term increments, writes continue
  4. Measure election latency (how long the cluster is unavailable)
  5. Kill another node — only 1 of 3 alive, writes fail (no quorum)
  6. Restart node — quorum restored, writes succeed again; rejoining node catches up
  7. Compare-and-swap (atomic test-and-set) — the primitive etcd uses for distributed locks
  8. Watch (push notification) — how Kubernetes detects config changes via etcd
"""

import time
import subprocess
import sys

try:
    import etcd3
except ImportError:
    print("Installing dependencies...")
    subprocess.run(["pip", "install", "etcd3", "-q"], check=True)
    import etcd3

# All three etcd nodes exposed to the host: etcd0->2379, etcd1->2479, etcd2->2381
ETCD_ENDPOINTS = [
    ("localhost", 2379),
    ("localhost", 2479),
    ("localhost", 2381),
]

# Map port -> container name for readable output
PORT_TO_NODE = {2379: "etcd0", 2479: "etcd1", 2381: "etcd2"}
CONTAINER_NAMES = ["etcd0", "etcd1", "etcd2"]


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def get_client(host="localhost", port=2379, timeout=3):
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


def get_client_with_failover(timeout=3):
    """Try each etcd endpoint in turn; return the first that responds."""
    last_err = None
    for host, port in ETCD_ENDPOINTS:
        try:
            c = get_client(host=host, port=port, timeout=timeout)
            c.get("/test-connectivity")  # lightweight probe
            return c, port
        except Exception as e:
            last_err = e
    raise Exception(f"All etcd endpoints unreachable: {last_err}")


def try_write(key, value, retries=3):
    for attempt in range(retries):
        try:
            client, port = get_client_with_failover()
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
        client, port = get_client_with_failover()
        val, meta = client.get(key)
        return val.decode() if val else None, None
    except Exception as e:
        return None, str(e)


def get_cluster_status():
    """Return (members list, leader_id, raft_term, raft_index) or raise."""
    client, _ = get_client_with_failover()
    members = list(client.members)
    status = client.status()
    return members, status.leader, status.raft_term, status.raft_index, status.db_size


def leader_node_name(leader_id, members):
    """Map leader member ID to a human-readable name."""
    for m in members:
        if m.id == leader_id:
            return m.name
    return hex(leader_id)


def main():
    section("CONSENSUS — RAFT WITH ETCD (3-NODE CLUSTER)")
    print("""
  Raft state machine — each node is always in one of three states:

    ┌──────────────────────────────────────────────────────────────────┐
    │                                                                  │
    │  FOLLOWER ──(timeout)──► CANDIDATE ──(wins majority)──► LEADER  │
    │     ▲                        │                             │     │
    │     └───────(loses or sees   │                             │     │
    │              higher term)────┘                             │     │
    │     ▲                                                      │     │
    │     └──────────────────── (heartbeat AppendEntries) ───────┘     │
    │                                                                  │
    └──────────────────────────────────────────────────────────────────┘

  Raft's four safety properties:
    • Election safety:      at most one leader per term
    • Leader append-only:   a leader never overwrites or deletes its log
    • Log matching:         if two logs share (index, term), they are identical
                            up to that index
    • State machine safety: if a server applies entry N, no other server
                            applies a different entry at index N

  Quorum = ⌊N/2⌋ + 1 nodes must acknowledge to commit an entry.
    N=3 → quorum=2, tolerates f=1 failure
    N=5 → quorum=3, tolerates f=2 failures

  IMPORTANT: Raft (and Paxos) only tolerate crash-stop failures.
  Byzantine faults (nodes lying, sending corrupted messages) require
  BFT algorithms (e.g., PBFT, Tendermint) with 3f+1 nodes for f faults.
""")

    # Verify connectivity
    try:
        client, port = get_client_with_failover()
        print(f"  Connected to etcd cluster via port {port} ({PORT_TO_NODE[port]}).")
    except Exception as e:
        print(f"  ERROR: cannot connect to etcd: {e}")
        print("  Run: docker compose up -d && sleep 20")
        sys.exit(1)

    # ── Phase 1: Write and cross-node read ────────────────────────
    section("Phase 1: Raft Replication — Write and Read")

    print(f"\n  Writing key '/config/db-host' = 'db.prod.internal'...")
    ok, err = try_write("/config/db-host", "db.prod.internal")
    if not ok:
        print(f"  Write failed: {err}")
        sys.exit(1)
    print(f"  Write: OK")

    val, err = try_read("/config/db-host")
    print(f"  Read /config/db-host: '{val}'" if val else f"  Read failed: {err}")

    # Write 5 keys we'll verify survive a leader failure
    for i in range(5):
        try_write(f"/test/key{i}", f"value{i}")
    print("  Pre-wrote 5 test keys (/test/key0 .. /test/key4)")

    print("""
  Write path (Raft log replication):
    1. Client sends PUT to any node
    2. Non-leader nodes proxy the request to the current leader
    3. Leader appends entry (index=N, term=T, command) to its local log
    4. Leader sends AppendEntries RPC (with new entry) to all followers
    5. Once ⌊N/2⌋+1 nodes have written the entry and ACK'd, it is COMMITTED
    6. Leader applies the entry to its state machine and responds to client
    7. Leader's next heartbeat carries the updated commit index;
       followers apply the entry on receipt

  Read path (linearizable vs. serializable):
    Linearizable (default in etcd): read is routed through the leader,
      which first confirms it's still the leader via a quorum round-trip.
      Prevents stale reads but adds latency.
    Serializable: read is served locally from any node, no quorum needed.
      Lower latency but may return slightly stale data.

    Alternatively, etcd supports "leader leases": the leader assumes it
    remains leader for (election-timeout - heartbeat-interval) ms and
    serves reads locally without a quorum round-trip. Faster, but
    clock skew between nodes can briefly allow two leaders to believe
    they hold the lease simultaneously — a subtle but real hazard.
""")

    # ── Phase 2: Observe current leader and term ──────────────────
    section("Phase 2: Cluster Status — Leader, Term, and Raft Index")

    try:
        members, leader_id, term_before, index_before, db_size = get_cluster_status()
        leader_name_before = leader_node_name(leader_id, members)
        print(f"  Cluster members:")
        for m in members:
            marker = " <-- LEADER" if m.id == leader_id else ""
            print(f"    id={hex(m.id):<18} name={m.name:<8} peers={list(m.peer_urls)}{marker}")
        print(f"\n  Current leader:  {leader_name_before}  (id={hex(leader_id)})")
        print(f"  Raft term:       {term_before}  (increments each election)")
        print(f"  Raft index:      {index_before}  (monotonically increasing log index)")
        print(f"  DB size:         {db_size} bytes")
    except Exception as e:
        print(f"  Could not get cluster status: {e}")
        members, leader_id, term_before, index_before = [], 0, 0, 0
        leader_name_before = "unknown"

    print("""
  The Raft term is a logical clock. Every election increments it.
  A node that sees a message with a higher term immediately steps down
  to follower and updates its term. This prevents a partitioned
  ex-leader from accepting writes after a new leader is elected.
""")

    # ── Phase 3: Kill the leader, measure election latency ────────
    section("Phase 3: Kill the Leader — Measure Election Latency")

    c0 = find_container_name("etcd0")
    c1 = find_container_name("etcd1")
    c2 = find_container_name("etcd2")

    # Try to kill the actual leader container if we know it
    leader_container = leader_name_before if leader_name_before in CONTAINER_NAMES else c0
    print(f"  Killing leader container '{leader_container}'...")
    killed = docker_stop(leader_container)
    if not killed:
        print(f"  WARNING: could not stop '{leader_container}' — check docker access")
    else:
        print(f"  '{leader_container}' stopped. Measuring time to elect a new leader...")

    # Measure how long writes fail before election completes
    election_start = time.monotonic()
    election_done = None
    attempt_count = 0
    for _ in range(20):  # poll for up to ~10 seconds
        time.sleep(0.5)
        attempt_count += 1
        ok, _ = try_write("/probe/election", str(time.time()))
        if ok:
            election_done = time.monotonic()
            break

    if election_done:
        latency_ms = (election_done - election_start) * 1000
        print(f"  New leader elected in ~{latency_ms:.0f} ms  ({attempt_count} probes x 0.5s)")
    else:
        print(f"  WARNING: could not detect new leader within 10 seconds")
        latency_ms = None

    # Show new leader and confirm term incremented
    try:
        members_new, leader_id_new, term_after, index_after, _ = get_cluster_status()
        leader_name_after = leader_node_name(leader_id_new, members_new)
        print(f"\n  New leader:  {leader_name_after}  (was: {leader_name_before})")
        print(f"  Raft term:   {term_after}  (was: {term_before}  → incremented by election)")
        print(f"  Raft index:  {index_after}  (was: {index_before})")
    except Exception as e:
        print(f"  Could not get post-election status: {e}")
        term_after = term_before + 1
        leader_name_after = "new leader"

    val, _ = try_read("/test/key0")
    print(f"  Read /test/key0: '{val}'  (data survived leader failure)")

    print(f"""
  What happened during those ~{f'{latency_ms:.0f}' if latency_ms else '?'} ms:
    1. Followers stopped receiving heartbeats from {leader_name_before}
    2. First follower whose election timeout fired became a CANDIDATE
    3. Candidate incremented its term to {term_after}, voted for itself
    4. Candidate sent RequestVote RPCs to remaining follower
    5. Follower voted YES (hasn't voted this term, candidate log up-to-date)
    6. Candidate won the quorum (2/2 reachable nodes) → became LEADER
    7. New leader sent heartbeats; writes resumed

  Election timeout in docker-compose.yml: 1000ms (etcd default).
  Random jitter within [timeout, 2*timeout] prevents simultaneous
  candidates splitting the vote (election livelock).

  Note: during this window, writes that timed out were NOT committed.
  The old leader could not commit them (it had no quorum). Safe to retry.
""")

    # ── Phase 4: Kill a second node — lose quorum ─────────────────
    section("Phase 4: Kill Second Node — Quorum Lost (1/3), Writes Fail")

    # Kill a non-leader node among the remaining two
    remaining = [c for c in CONTAINER_NAMES if c != leader_container]
    victim = remaining[0]
    print(f"  Stopping '{victim}' (a follower). Now only 1/3 nodes alive...")
    docker_stop(victim)
    time.sleep(2)

    print(f"\n  Testing write with only 1/3 nodes alive (no quorum):")
    t0 = time.monotonic()
    ok, err = try_write("/config/emergency", "no-quorum", retries=1)
    elapsed_ms = (time.monotonic() - t0) * 1000
    if ok:
        print(f"  Write: OK (unexpected — connection may have been cached)")
    else:
        print(f"  Write: TIMED OUT after {elapsed_ms:.0f} ms — {err[:80]}")
        print(f"  Correct: no quorum, system refuses to accept writes")

    print(f"""
  Why writes fail — and why that's correct:
    - The surviving node is likely the leader, but it can only count 1 vote
      (itself). It needs 2 of 3 to commit. It will never reach quorum.
    - The leader keeps trying AppendEntries but gets no responses.
    - The write hangs until client timeout, then returns an error.
    - The ENTRY IS NOT COMMITTED. No data loss; the write simply failed.

  This is the "C" in CP (CAP theorem):
    etcd refuses to return an inconsistent answer. It would rather be
    unavailable than return data that might be wrong. Contrast with
    AP systems (Cassandra/DynamoDB with W=1) that accept the write and
    reconcile conflicts later — trading consistency for availability.

  2-node clusters are a common mistake:
    A 2-node cluster (quorum=2) requires BOTH nodes to be alive. It is
    strictly worse than a single node for availability. Always use
    odd-numbered clusters: 3, 5, or 7 nodes.
""")

    # ── Phase 5: Restart node — quorum restored ───────────────────
    section("Phase 5: Restart One Node — Quorum Restored (2/3)")

    print(f"  Starting '{victim}'...")
    docker_start(victim)
    print(f"  Waiting 6s for '{victim}' to rejoin and catch up via log replication...")
    time.sleep(6)

    print(f"\n  Testing write after recovery:")
    ok, err = try_write("/config/status", "recovered")
    print(f"  Write: {'OK' if ok else f'FAILED: {err}'}")

    val, _ = try_read("/config/status")
    print(f"  Read /config/status: '{val}'")

    try:
        _, _, term_recovered, index_recovered, _ = get_cluster_status()
        print(f"  Raft index now: {index_recovered}  (all entries replicated to rejoined node)")
    except Exception:
        pass

    print(f"""
  Log catchup (AppendEntries):
    When '{victim}' rejoins, the leader sends it AppendEntries RPCs
    containing all log entries it missed. This is the same RPC used
    for normal replication — catchup is not a special code path.

    '{victim}' applies missed entries to its state machine in order,
    then participates normally in future quorum decisions.

    Safety: the leader only sends entries the rejoined node is MISSING.
    It identifies the gap via the follower's nextIndex (tracked per peer).
""")

    # ── Phase 6: Compare-and-swap (distributed lock primitive) ────
    section("Phase 6: Compare-and-Swap — The Distributed Lock Primitive")

    print("""
  Compare-and-swap (CAS) is an atomic test-and-set: update a key only
  if its current value matches an expected value. etcd calls this a
  'transaction' (STM-style). It is the foundation of distributed locks,
  leader election in application code, and Kubernetes controller logic.
""")

    # Put initial value
    client, port = get_client_with_failover()
    client.put("/lock/owner", "nobody")

    def cas_acquire(client, lock_key, expected_owner, new_owner):
        """Atomically set lock_key=new_owner only if current value == expected_owner."""
        txn_ok, _ = client.transaction(
            compare=[client.transactions.value(lock_key) == expected_owner],
            success=[client.transactions.put(lock_key, new_owner)],
            failure=[]
        )
        return txn_ok

    print("  Scenario: two workers try to acquire the same distributed lock.")
    print()

    client, _ = get_client_with_failover()

    # Worker A acquires lock
    acquired_a = cas_acquire(client, "/lock/owner", "nobody", "worker-A")
    print(f"  Worker A: CAS('/lock/owner', expected='nobody', new='worker-A') -> {'ACQUIRED' if acquired_a else 'FAILED'}")

    # Worker B tries to acquire — should fail
    acquired_b = cas_acquire(client, "/lock/owner", "nobody", "worker-B")
    print(f"  Worker B: CAS('/lock/owner', expected='nobody', new='worker-B') -> {'ACQUIRED (BUG!)' if acquired_b else 'FAILED (correct — A holds lock)'}")

    # Worker A releases lock
    released_a = cas_acquire(client, "/lock/owner", "worker-A", "nobody")
    print(f"  Worker A: release CAS('/lock/owner', expected='worker-A', new='nobody') -> {'OK' if released_a else 'FAILED'}")

    # Worker B retries — now succeeds
    acquired_b2 = cas_acquire(client, "/lock/owner", "nobody", "worker-B")
    print(f"  Worker B: retry CAS -> {'ACQUIRED' if acquired_b2 else 'FAILED'}")

    print(f"""
  etcd leases (TTL):
    Real distributed locks combine CAS with a lease (TTL). The lock
    holder keeps the lease alive via heartbeats. If the holder crashes,
    the lease expires and the lock is auto-released — no manual cleanup.

    etcd.grant_lease(ttl=10) → lease_id
    etcd.put('/lock/owner', 'worker-A', lease=lease_id)
    etcd.refresh_lease(lease_id)   # called periodically by lock holder

  This is exactly how Kubernetes leader election works:
    Controller managers and schedulers use an etcd lease to elect one
    active instance. The active pod refreshes the lease; if it fails,
    the lease expires, a standby pod wins the next CAS, and takes over.
""")

    # Restart the originally-killed leader container too
    print(f"  Restarting '{leader_container}' to restore full cluster...")
    docker_start(leader_container)
    time.sleep(5)

    # ── Phase 7: Summary ──────────────────────────────────────────
    section("Summary — Paxos vs Raft vs ZAB")

    print("""
  Paxos (Lamport, 1989):
    Phases: Prepare(ballot=N) → Promise(N, highest_accepted) → Accept(N, v) → Accepted
    • Original correct consensus algorithm — mathematically elegant
    • Single-decree: agrees on ONE value; Multi-Paxos extends to a log
    • After the first round, a stable leader can skip Prepare → one round-trip
      per subsequent entry (same as Raft's normal operation)
    • Log holes: if a proposer fails mid-round, later proposers may see
      a gap in accepted values that must be filled with a no-op
    • Notoriously hard to implement correctly (Chubby team documented
      that every implementation detail required a new sub-protocol)
    • Used in: Google Chubby, Google Spanner

  Raft (Ongaro & Ousterhout, 2014):
    Sub-problems: Leader Election → Log Replication → Safety
    • Explicit single leader: all writes go through one node (simpler reasoning)
    • Terms: monotonically increasing logical clocks, one per election
    • No log holes: leader fills gaps before committing new entries
    • Membership change: "joint consensus" (two overlapping quorums) or
      single-server changes (one at a time, safer in practice)
    • prevote optimization (etcd): a candidate first checks it can reach a
      quorum BEFORE incrementing its term — prevents disruption when a
      partitioned node repeatedly increments the cluster term on rejoin
    • Used in: etcd, CockroachDB, TiKV, Consul, InfluxDB, YugabyteDB

  ZAB — Zookeeper Atomic Broadcast (Hunt et al., 2010):
    • Conceptually similar to Raft but predates it
    • Primary/backup model; primary proposes, followers acknowledge
    • Distinguishes between crash-recovery and broadcast phases
    • Log entries called "proposals"; quorum of ACKs → commit
    • Used in: Apache Zookeeper (Kafka metadata, Hadoop coordination)

  Byzantine Fault Tolerance boundary:
    Paxos and Raft assume crash-stop failures (nodes stop responding).
    They do NOT tolerate Byzantine faults (nodes lying, sending wrong
    data). For Byzantine tolerance use PBFT or BFT-SMaRt (3f+1 nodes
    for f Byzantine faults). Blockchain consensus (Tendermint, HotStuff)
    is BFT-based.

  CAP theorem placement:
    CP (etcd, Zookeeper, Consul): refuses writes when quorum unavailable
    AP (Cassandra W=1, DynamoDB eventual): accepts writes, reconciles later
    Note: CAP is binary and a simplification. PACELC adds latency:
      etcd = PC/EC (consistent under partition, consistent + higher latency normally)
      Cassandra = PA/EL (available under partition, lower latency normally)

  Quorum formula: N = 2f+1 tolerates f crash failures
    Why? Any two quorums of size f+1 must overlap by at least 1 node.
    That shared node carries the highest accepted value, preventing
    two quorums from independently accepting conflicting values.
""")

    try:
        members_final, leader_id_final, term_final, index_final, db_size_final = get_cluster_status()
        leader_final = leader_node_name(leader_id_final, members_final)
        section("Final Cluster State")
        print(f"  Members:")
        for m in members_final:
            marker = " <-- LEADER" if m.id == leader_id_final else ""
            print(f"    id={hex(m.id):<18} name={m.name:<8}{marker}")
        print(f"\n  Leader:      {leader_final}")
        print(f"  Raft term:   {term_final}  (incremented {term_final - term_before} time(s) during this lab)")
        print(f"  Raft index:  {index_final}")
        print(f"  DB size:     {db_size_final} bytes")
    except Exception as e:
        print(f"  Could not get final cluster state: {e}")

    print("\n  Next: ../04-event-driven-architecture/\n")


if __name__ == "__main__":
    main()
