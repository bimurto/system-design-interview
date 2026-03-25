#!/usr/bin/env python3
"""
Service Discovery & Coordination Lab — etcd 3-node cluster.

What this demonstrates:
  1. Register 3 "services" with TTL leases
  2. Watch for service changes (etcd watch)
  3. Let one service's lease expire → client detects deregistration
  4. Leader election using etcd transactions (compare-and-swap)
  5. Leader failure → another candidate takes over
"""

import os
import time
import threading
import etcd3

ETCD_HOST = os.environ.get("ETCD_HOST", "localhost")

SERVICE_PREFIX = "/services/api/"
LEADER_KEY     = "/election/leader"


def section(title):
    print(f"\n{'=' * 65}")
    print(f"  {title}")
    print("=" * 65)


def get_client():
    return etcd3.client(host=ETCD_HOST, port=2379)


# ── Service Registration ───────────────────────────────────────────────────

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
        print(f"  Registered: {self.name} → {self.address} (TTL={self.ttl}s)")

    def start_heartbeat(self):
        """Refresh lease every TTL/3 seconds to keep service alive."""
        def heartbeat_loop():
            while not self._stop.is_set():
                try:
                    self.lease.refresh()
                except Exception:
                    pass
                self._stop.wait(self.ttl / 3)

        self._thread = threading.Thread(target=heartbeat_loop, daemon=True)
        self._thread.start()

    def stop_heartbeat(self):
        """Stop heartbeat — lease will expire after TTL seconds."""
        self._stop.set()
        print(f"  Stopped heartbeat for {self.name} (lease will expire in ~{self.ttl}s)")

    def deregister(self):
        """Immediately remove the service."""
        if self.lease:
            self.lease.revoke()
        print(f"  Deregistered: {self.name}")


def list_services(client):
    """Return all currently registered services."""
    services = {}
    results = client.get_prefix(SERVICE_PREFIX)
    for value, meta in results:
        name = meta.key.decode().replace(SERVICE_PREFIX, "")
        services[name] = value.decode()
    return services


# ── Leader Election ────────────────────────────────────────────────────────

class Candidate:
    def __init__(self, name):
        self.name   = name
        self.client = get_client()
        self.lease  = None
        self.is_leader = False

    def campaign(self):
        """Try to become leader using compare-and-swap."""
        self.lease = self.client.lease(15)
        # Atomic: put the key only if it doesn't exist
        success, _ = self.client.transaction(
            compare=[
                self.client.transactions.version(LEADER_KEY) == 0
            ],
            success=[
                self.client.transactions.put(LEADER_KEY, self.name, lease=self.lease)
            ],
            failure=[]
        )
        self.is_leader = success
        return success

    def get_current_leader(self):
        val, _ = self.client.get(LEADER_KEY)
        return val.decode() if val else None

    def resign(self):
        if self.is_leader and self.lease:
            self.lease.revoke()
            self.is_leader = False
            print(f"  {self.name} resigned leadership (lease revoked)")

    def watch_for_takeover(self, timeout=20):
        """Watch leader key — when current leader dies, campaign."""
        print(f"  {self.name} watching for leader vacancy...")
        start = time.time()
        events_iterator, cancel = self.client.watch(LEADER_KEY)

        def watch_thread():
            for event in events_iterator:
                if time.time() - start > timeout:
                    break
                # Key was deleted (leader resigned/died)
                if type(event).__name__ == 'DeleteEvent':
                    print(f"  {self.name} detected leader vacancy! Campaigning...")
                    if self.campaign():
                        print(f"  {self.name} is now leader!")
                        cancel()
                        return
            cancel()

        t = threading.Thread(target=watch_thread, daemon=True)
        t.start()
        return t


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    section("SERVICE DISCOVERY & COORDINATION LAB")
    print("""
  Architecture: 3-node etcd cluster (Raft consensus)

    etcd0 ←→ etcd1 ←→ etcd2
      ↑           ↑
    client      client

  Service discovery pattern:
    1. Service registers itself with a TTL lease
    2. Heartbeat thread renews the lease before it expires
    3. Watchers detect registrations/deregistrations in real-time
    4. If service crashes (no heartbeat), lease expires → auto-removed
""")

    client = get_client()

    # ── Phase 1: Register services with TTL leases ─────────────────
    section("Phase 1: Register 3 Services with TTL Leases")

    svc_a = Service("api-server-1", "10.0.0.1:8080", ttl=8)
    svc_b = Service("api-server-2", "10.0.0.2:8080", ttl=8)
    svc_c = Service("api-server-3", "10.0.0.3:8080", ttl=8)

    svc_a.register()
    svc_b.register()
    svc_c.register()

    # Start heartbeats for A and B, but NOT for C (will expire)
    svc_a.start_heartbeat()
    svc_b.start_heartbeat()
    # svc_c: no heartbeat — will expire after 8s

    services = list_services(client)
    print(f"\n  Registered services ({len(services)}):")
    for name, addr in sorted(services.items()):
        print(f"    {name} → {addr}")

    # ── Phase 2: Watch for changes ────────────────────────────────
    section("Phase 2: Watch for Service Changes (etcd watch)")

    change_log = []

    def watch_services():
        events_iterator, cancel = client.watch_prefix(SERVICE_PREFIX)
        for event in events_iterator:
            key = event.key.decode().replace(SERVICE_PREFIX, "")
            event_type = type(event).__name__
            val = event.value.decode() if event.value else ""
            change_log.append((event_type, key, val, time.time()))
            if "Delete" in event_type:
                print(f"  [WATCH] DELETE: {key} (service deregistered!)")
            else:
                print(f"  [WATCH] PUT:    {key} → {val}")

    watcher_thread = threading.Thread(target=watch_services, daemon=True)
    watcher_thread.start()

    print("  Watcher running. Now deregistering api-server-3 and waiting for lease expiry...\n")
    time.sleep(1)

    # Immediately deregister C
    svc_c.deregister()
    time.sleep(1)

    services = list_services(client)
    print(f"\n  Services after deregistration ({len(services)}):")
    for name, addr in sorted(services.items()):
        print(f"    {name} → {addr}")

    # ── Phase 3: Let a lease expire naturally ─────────────────────
    section("Phase 3: Lease Expiry — Service B Stops Heartbeat")
    print("  Stopping heartbeat for api-server-2 (lease TTL=8s)...")
    svc_b.stop_heartbeat()
    print("  Waiting 10s for lease to expire...\n")
    time.sleep(10)

    services = list_services(client)
    print(f"  Services after lease expiry ({len(services)}):")
    for name, addr in sorted(services.items()):
        print(f"    {name} → {addr}")
    if "api-server-2" not in services:
        print("  api-server-2 auto-removed from registry (lease expired)")

    # ── Phase 4: Leader election ───────────────────────────────────
    section("Phase 4: Leader Election (compare-and-swap)")
    print("""
  Three candidates compete for leadership.
  Etcd transaction: atomically write the leader key only if it's empty.
  Only one can win — the first to execute the transaction successfully.
""")

    # Clear any stale leader key
    try:
        client.delete(LEADER_KEY)
    except Exception:
        pass
    time.sleep(0.5)

    candidates = [Candidate("node-A"), Candidate("node-B"), Candidate("node-C")]

    results = []
    for c in candidates:
        won = c.campaign()
        results.append((c.name, won))
        print(f"  {c.name} campaigned: {'WON ELECTION' if won else 'lost (key already set)'}")

    leader_name = next(c.name for c, (n, won) in zip(candidates, results) if won)
    leader = next(c for c in candidates if c.name == leader_name)

    current = candidates[0].get_current_leader()
    print(f"\n  Current leader in etcd: {current}")

    # ── Phase 5: Leader failure and re-election ───────────────────
    section("Phase 5: Leader Failure → New Election")
    print(f"  Current leader: {leader.name}")
    print(f"  Simulating leader failure (revoking lease)...\n")

    # Set up watchers for non-leaders
    non_leaders = [c for c in candidates if c.name != leader.name]
    watch_threads = [c.watch_for_takeover(timeout=15) for c in non_leaders]

    time.sleep(0.5)
    leader.resign()
    time.sleep(3)  # Give watchers time to detect and campaign

    current_leader = candidates[0].get_current_leader()
    print(f"\n  New leader after failure: {current_leader or '(no leader yet — watch fired)'}")

    # ── Phase 6: Summary ──────────────────────────────────────────
    section("Phase 6: Summary")
    print(f"""
  Service Discovery patterns:
  ┌────────────────────────────────────────────────────────────┐
  │ Client-side  │ Client queries registry, picks instance     │
  │              │ + No single point of failure                │
  │              │ - Client must have discovery logic          │
  │──────────────│─────────────────────────────────────────────│
  │ Server-side  │ Load balancer queries registry transparently│
  │              │ + Simple clients                            │
  │              │ - LB is a single point of failure           │
  └────────────────────────────────────────────────────────────┘

  Registry comparison:
    etcd    → CP (Raft), low latency, watches, used in Kubernetes
    Consul  → CP + health checks built in, DNS interface, ACLs
    Zookeeper → CP (ZAB), mature, complex, used in Kafka/Hadoop

  Key insight: etcd is CP — during a network partition, the minority
  partition rejects writes (and may reject reads). Services in the
  minority partition won't be discoverable until partition heals.

  Leader election pattern:
    1. Candidates race to write a key (with their name) + lease
    2. Only one wins (atomic compare-and-swap)
    3. Winner heartbeats the lease to stay leader
    4. On crash: lease expires, key deleted, others detect via watch
    5. New election happens automatically

  Distributed locks (Redlock controversy):
    Redis Redlock: acquire lock on N/2+1 Redis nodes simultaneously
    Martin Kleppmann (2016): Redlock is unsafe under clock skew / GC pauses
    Antirez (Redis author) disagreed — debate is still active
    Safe alternative: use etcd (fencing tokens) for strong guarantees

  Next: ../14-idempotency-exactly-once/
""")


if __name__ == "__main__":
    main()
