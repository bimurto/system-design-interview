#!/usr/bin/env python3
"""
Service Discovery & Coordination Lab — etcd 3-node cluster.

What this demonstrates:
  1. Register 3 "services" with TTL leases
  2. Watch for service changes (etcd prefix watch)
  3. Let one service's lease expire → client detects auto-deregistration
  4. Leader election — concurrent race using atomic CAS transactions
  5. Leader failure → another candidate takes over via watch
  6. Fencing tokens — how etcd revision numbers prevent zombie-leader writes
  7. Summary: registry comparison, Redlock controversy, operational gotchas
"""

import os
import time
import threading
import random
import etcd3

ETCD_HOST = os.environ.get("ETCD_HOST", "localhost")

SERVICE_PREFIX = "/services/api/"
LEADER_KEY     = "/election/leader"


def section(title):
    print(f"\n{'=' * 65}")
    print(f"  {title}")
    print("=" * 65)


def banner(msg):
    print(f"\n  >>> {msg}")


def get_client():
    return etcd3.client(host=ETCD_HOST, port=2379)


# ── Service Registration ────────────────────────────────────────────────────

class Service:
    def __init__(self, name, address, ttl=10):
        self.name    = name
        self.address = address
        self.ttl     = ttl
        self.client  = get_client()
        self.lease   = None
        self._stop   = threading.Event()
        self._thread = None

    def register(self):
        self.lease = self.client.lease(self.ttl)
        self.client.put(
            f"{SERVICE_PREFIX}{self.name}",
            self.address,
            lease=self.lease
        )
        print(f"  Registered: {self.name} → {self.address} (lease TTL={self.ttl}s)")

    def start_heartbeat(self):
        """Refresh lease every TTL/3 seconds to keep service alive.

        TTL/3 gives two full missed heartbeats before the lease expires,
        providing tolerance for transient network hiccups.
        """
        def heartbeat_loop():
            while not self._stop.is_set():
                try:
                    self.lease.refresh()
                except Exception:
                    pass
                self._stop.wait(self.ttl / 3)

        self._thread = threading.Thread(target=heartbeat_loop, daemon=True)
        self._thread.start()
        print(f"  Heartbeat started for {self.name} (refresh every {self.ttl/3:.1f}s)")

    def stop_heartbeat(self):
        """Stop heartbeat — lease will expire after TTL seconds.

        Models a process that is still running but has stopped renewing its
        lease (e.g., stuck in a GC pause or network partition).
        """
        self._stop.set()
        print(f"  Heartbeat stopped for {self.name} (lease expires in ~{self.ttl}s)")

    def deregister(self):
        """Immediately remove the service by revoking the lease.

        Lease revocation is synchronous and immediate — the key disappears
        the moment the revoke RPC completes. This models graceful shutdown.
        """
        if self.lease:
            self.lease.revoke()
        print(f"  Deregistered: {self.name} (lease revoked — key removed immediately)")


def list_services(client):
    """Return all currently registered services under SERVICE_PREFIX."""
    services = {}
    results = client.get_prefix(SERVICE_PREFIX)
    for value, meta in results:
        name = meta.key.decode().replace(SERVICE_PREFIX, "")
        services[name] = value.decode()
    return services


def print_services(client, label=""):
    services = list_services(client)
    suffix = f" {label}" if label else ""
    print(f"\n  Registered services{suffix} ({len(services)}):")
    if services:
        for name, addr in sorted(services.items()):
            print(f"    {name:20s} → {addr}")
    else:
        print("    (none)")
    return services


# ── Leader Election ─────────────────────────────────────────────────────────

class Candidate:
    def __init__(self, name):
        self.name      = name
        self.client    = get_client()
        self.lease     = None
        self.is_leader = False
        self.fencing_token = None  # etcd revision of the winning put

    def campaign(self, jitter_ms=0):
        """Try to become leader using an atomic compare-and-swap transaction.

        The transaction writes the leader key only if its version == 0
        (i.e., the key does not exist). Exactly one concurrent caller wins.
        The winner records the etcd revision as a fencing token.

        jitter_ms: optional random sleep before campaigning to avoid
                   thundering herd when many candidates detect leader death.
        """
        if jitter_ms > 0:
            time.sleep(random.random() * jitter_ms / 1000)

        self.lease = self.client.lease(15)
        success, responses = self.client.transaction(
            compare=[
                self.client.transactions.version(LEADER_KEY) == 0
            ],
            success=[
                self.client.transactions.put(LEADER_KEY, self.name, lease=self.lease)
            ],
            failure=[]
        )
        self.is_leader = success
        # Record the fencing token: the etcd revision of the successful write.
        # Any storage system can use this to reject writes from stale leaders.
        if success:
            _, meta = self.client.get(LEADER_KEY)
            if meta:
                self.fencing_token = meta.mod_revision
        return success

    def get_current_leader(self):
        val, _ = self.client.get(LEADER_KEY)
        return val.decode() if val else None

    def resign(self):
        if self.is_leader and self.lease:
            self.lease.revoke()
            self.is_leader = False
            print(f"  {self.name} resigned leadership (lease revoked → key deleted)")

    def watch_for_takeover(self, timeout=20, jitter_ms=200):
        """Watch the leader key; campaign when current leader's key is deleted.

        jitter_ms: spread campaign attempts to avoid thundering herd.
        Returns the watcher thread so the caller can join() it.
        """
        print(f"  {self.name} watching for leader vacancy (jitter up to {jitter_ms}ms)...")
        start = time.time()
        events_iterator, cancel = self.client.watch(LEADER_KEY)

        def watch_thread():
            for event in events_iterator:
                if time.time() - start > timeout:
                    break
                # Use isinstance for robust event type checking
                if isinstance(event, etcd3.events.DeleteEvent):
                    print(f"  {self.name} detected leader DELETE event — campaigning...")
                    if self.campaign(jitter_ms=jitter_ms):
                        print(f"  *** {self.name} won the new election! "
                              f"(fencing token: revision={self.fencing_token})")
                        cancel()
                        return
                    else:
                        print(f"  {self.name} lost the race (another candidate was faster)")
            cancel()

        t = threading.Thread(target=watch_thread, daemon=True)
        t.start()
        return t


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    section("SERVICE DISCOVERY & COORDINATION LAB")
    print("""
  Architecture: 3-node etcd cluster (Raft consensus)

    etcd0 ←──peer──→ etcd1 ←──peer──→ etcd2
      ↑                  ↑
    client             client
    (reads/writes)     (reads/writes)

  Raft guarantees: writes committed only when acknowledged by
  a majority (2/3 nodes). A minority partition cannot serve
  consistent reads or accept writes.

  Service discovery pattern:
    1. Service registers itself with a TTL lease
    2. Heartbeat thread renews the lease before it expires
    3. Watchers detect registrations/deregistrations in real-time
    4. If service crashes (no heartbeat), lease expires → auto-removed
""")

    client = get_client()

    # ── Phase 1: Register services with TTL leases ─────────────────────────
    section("Phase 1: Register 3 Services with TTL Leases")
    print("""
  Each service creates a lease with a TTL, then writes its key bound to
  that lease. If the lease expires (no heartbeat renewal), etcd atomically
  deletes all keys bound to that lease — no manual cleanup required.
""")

    svc_a = Service("api-server-1", "10.0.0.1:8080", ttl=8)
    svc_b = Service("api-server-2", "10.0.0.2:8080", ttl=8)
    svc_c = Service("api-server-3", "10.0.0.3:8080", ttl=8)

    svc_a.register()
    svc_b.register()
    svc_c.register()
    print()

    # Start heartbeats for A and B, but NOT for C (will expire naturally)
    svc_a.start_heartbeat()
    svc_b.start_heartbeat()
    print(f"  NOTE: api-server-3 has NO heartbeat — lease will expire in ~8s")

    print_services(client, "(after registration)")

    # ── Phase 2: Watch for changes ─────────────────────────────────────────
    section("Phase 2: Watch for Service Changes (etcd prefix watch)")
    print("""
  A prefix watch streams all events (PUT/DELETE) under /services/api/.
  This is how load balancers and clients keep their routing tables current
  without polling — event latency is typically <10ms.
""")

    change_log = []
    watch_ready = threading.Event()

    def watch_services():
        events_iterator, cancel = client.watch_prefix(SERVICE_PREFIX)
        watch_ready.set()
        for event in events_iterator:
            key = event.key.decode().replace(SERVICE_PREFIX, "")
            val = event.value.decode() if event.value else ""
            ts  = time.strftime("%H:%M:%S")
            if isinstance(event, etcd3.events.DeleteEvent):
                change_log.append(("DELETE", key, "", ts))
                print(f"  [{ts}] WATCH DELETE: {key:20s} ← service deregistered")
            else:
                change_log.append(("PUT", key, val, ts))
                print(f"  [{ts}] WATCH PUT:    {key:20s} → {val}")

    watcher_thread = threading.Thread(target=watch_services, daemon=True)
    watcher_thread.start()
    watch_ready.wait(timeout=5)

    banner("Performing explicit deregistration of api-server-3 (graceful shutdown)...")
    time.sleep(0.5)
    svc_c.deregister()
    time.sleep(1.5)  # let the watch event print before moving on

    print_services(client, "(after explicit deregistration)")

    # ── Phase 3: Let a lease expire naturally ──────────────────────────────
    section("Phase 3: Lease Expiry — api-server-2 Stops Heartbeat (crash simulation)")
    print("""
  Stopping the heartbeat models a crashed process that can no longer renew
  its lease. etcd will wait for the full TTL to elapse before deleting the
  key. The watch fires a DELETE event automatically — no operator action.

  This is why TTL sizing matters:
    - Too short (1-2s): healthy services deregister during network blips
    - Too long (60s):   dead services stay registered, clients get errors
    - Rule of thumb: TTL = 3-5x heartbeat interval
""")

    svc_b.stop_heartbeat()
    print(f"\n  Waiting 10s for the 8s lease to expire...\n")

    # Count down so the output is clearly tied to time passing
    for remaining in range(10, 0, -2):
        time.sleep(2)
        print(f"  ...{remaining - 2}s remaining" if remaining > 2 else "  ...lease should have expired")

    print()
    services = print_services(client, "(after lease expiry)")
    if "api-server-2" not in services:
        print("\n  api-server-2 was auto-removed — no manual deregistration needed")

    # ── Phase 4: Concurrent leader election ────────────────────────────────
    section("Phase 4: Leader Election — Concurrent CAS Race")
    print("""
  Three candidates campaign concurrently in separate threads.
  Each executes an atomic etcd transaction:

    IF version(/election/leader) == 0     # key does not exist
    THEN put(/election/leader, <name>, lease=<lease>)
    ELSE (no-op)

  Exactly one thread wins — the one whose transaction was ordered first by
  the etcd leader. All others see version > 0 and lose atomically.
""")

    # Clean up any stale leader key from a previous run
    try:
        client.delete(LEADER_KEY)
    except Exception:
        pass
    time.sleep(0.3)

    candidates = [Candidate("node-A"), Candidate("node-B"), Candidate("node-C")]

    # Use threads so the race is real — not sequential (sequential always lets node-A win)
    results = {}
    campaign_threads = []

    def run_campaign(candidate):
        won = candidate.campaign(jitter_ms=0)  # no jitter — maximize race contention
        results[candidate.name] = won

    for c in candidates:
        t = threading.Thread(target=run_campaign, args=(c,))
        campaign_threads.append(t)

    banner("Starting all 3 campaigns simultaneously...")
    for t in campaign_threads:
        t.start()
    for t in campaign_threads:
        t.join()

    # Determine winner
    winner = None
    for c in candidates:
        won = results[c.name]
        status = "WON ELECTION" if won else "lost (key already set by winner)"
        fencing = f"  fencing token: revision={c.fencing_token}" if won else ""
        print(f"  {c.name}: {status}{fencing}")
        if won:
            winner = c

    current = candidates[0].get_current_leader()
    print(f"\n  etcd confirms current leader: {current}")
    print(f"  (Winner was determined by Raft ordering — whoever's transaction")
    print(f"   reached the etcd leader first won, regardless of local timing)")

    # ── Phase 5: Leader failure and re-election ────────────────────────────
    section("Phase 5: Leader Failure → Automatic Re-election")
    print(f"""
  Current leader: {winner.name}
  Simulating leader failure by revoking its lease.

  Non-leaders are watching the leader key in background threads.
  When they receive the DELETE event, each campaigns with jitter
  to avoid the thundering herd problem.
""")

    non_leaders = [c for c in candidates if c.name != winner.name]
    watch_threads = [c.watch_for_takeover(timeout=15, jitter_ms=300) for c in non_leaders]

    time.sleep(0.5)
    winner.resign()

    # Wait for re-election to complete
    for t in watch_threads:
        t.join(timeout=10)

    time.sleep(1)
    new_leader_name = candidates[0].get_current_leader()
    new_leader = next((c for c in non_leaders if c.name == new_leader_name), None)
    if new_leader_name:
        print(f"\n  New leader: {new_leader_name} "
              f"(fencing token: revision={new_leader.fencing_token if new_leader else 'N/A'})")
    else:
        print(f"\n  No leader yet — election still in progress")

    # ── Phase 6: Fencing tokens ────────────────────────────────────────────
    section("Phase 6: Fencing Tokens — Preventing Zombie Leader Writes")
    print("""
  Problem: A leader pauses (GC, network) → lease expires → new leader elected.
  Old leader resumes, thinks it's still leader, and writes to storage.
  Result: two simultaneous "leaders" — data corruption.

  Solution: fencing tokens (etcd revision numbers are monotonically increasing).

    Timeline:
      t=0   node-A wins election. etcd revision = 42 (fencing token).
            Storage system records: "last accepted token = 42"

      t=5   node-A stalls (GC pause). Lease expires.
      t=6   node-B wins election. etcd revision = 57 (new fencing token).
            Storage system records: "last accepted token = 57"

      t=10  node-A resumes. Sends write with its token (revision=42).
            Storage system rejects: 42 < 57 → this is a stale write.

      t=10  node-B sends write with its token (revision=57).
            Storage system accepts: 57 == 57 → valid write.

  Key insight: the storage system — not the leader — enforces correctness.
  The leader cannot prevent itself from being a zombie; the storage layer must.
""")

    # Demonstrate by reading the current fencing token from etcd
    _, meta = client.get(LEADER_KEY)
    if meta:
        print(f"  Current leader key revision (fencing token): {meta.mod_revision}")
        print(f"  Any write with revision < {meta.mod_revision} would be rejected")
        print(f"  (In practice: your storage system must check this; etcd does not enforce it)")
    else:
        print(f"  (Leader key not present — election may still be in progress)")

    print("""
  NOTE: Redlock (Redis-based distributed lock) does NOT provide fencing tokens.
  Martin Kleppmann (2016) showed Redlock is unsafe under:
    - Clock skew between Redis nodes
    - GC pauses causing lease expiry mid-critical-section
  Antirez (Redis author) disputed the severity.
  Consensus: use etcd (fencing tokens via revision) when correctness is critical.
""")

    # ── Phase 7: Watch event summary + comparison ──────────────────────────
    section("Phase 7: Event Log & Registry Comparison")

    print(f"\n  Watch events captured during this run ({len(change_log)}):")
    print(f"  {'Time':10s} {'Type':8s} {'Service'}")
    print(f"  {'-'*10} {'-'*8} {'-'*30}")
    for evt_type, key, val, ts in change_log:
        detail = f"→ {val}" if evt_type == "PUT" else "(removed)"
        print(f"  {ts:10s} {evt_type:8s} {key:20s} {detail}")

    print(f"""
  Service Registry Comparison:
  ┌──────────────┬────────────┬─────────────────────────┬──────────────────┐
  │ Registry     │ Model      │ Health Checks           │ Key Use Case     │
  ├──────────────┼────────────┼─────────────────────────┼──────────────────┤
  │ etcd         │ CP (Raft)  │ App-level TTL heartbeat │ Kubernetes, k/v  │
  │ Consul       │ CP + AP    │ Built-in HTTP/TCP/script│ Service mesh, DC │
  │ ZooKeeper    │ CP (ZAB)   │ Session timeout (manual)│ Kafka, Hadoop    │
  │ Eureka       │ AP         │ Client self-report       │ Netflix, spring  │
  └──────────────┴────────────┴─────────────────────────┴──────────────────┘

  Operational gotchas (staff-level knowledge):
    1. TTL too short  → false deregistrations during transient network issues
    2. No watch compaction handling → silent event loss on reconnect (ErrCompacted)
    3. No jitter on re-election → thundering herd saturates etcd on leader death
    4. No client-side cache → service discovery outage = full traffic blackout
    5. Zombie leaders → must use fencing tokens; the registry alone is not enough

  Next: ../15-idempotency-exactly-once/
""")


if __name__ == "__main__":
    main()
