#!/usr/bin/env python3
"""
Failure Modes & Reliability Patterns Lab

Prerequisites: docker compose up -d (wait ~5s)

What this demonstrates:
  1. Timeout: bounded vs unbounded latency when downstream is slow
  2. Retry with exponential backoff + jitter: recovery from transient failures
  3. Circuit breaker: closed -> open -> half-open -> closed state machine
  4. Bulkhead: resource-pool isolation prevents one slow dependency starving another
  5. Idempotency keys: making non-idempotent operations safe to retry
  6. Cascading failure: how a slow downstream propagates, and how to stop it
"""

import json
import time
import random
import uuid
import threading
import subprocess
from enum import Enum
from collections import defaultdict

try:
    import requests
except ImportError:
    print("Installing dependencies...")
    subprocess.run(["pip", "install", "requests", "-q"], check=True)
    import requests

DOWNSTREAM = "http://localhost:9001"


def section(title):
    print(f"\n{'=' * 62}")
    print(f"  {title}")
    print("=" * 62)


def wait_ready(url, label, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            requests.get(url + "/health", timeout=2)
            print(f"  {label} ready.")
            return True
        except Exception:
            time.sleep(1)
    print(f"  ERROR: {label} not ready. Run: docker compose up -d")
    return False


def recover_downstream():
    """Reset the downstream to healthy state."""
    try:
        requests.get(f"{DOWNSTREAM}/recover", timeout=2)
    except Exception:
        pass


# ── Retry with exponential backoff + jitter ─────────────────────────────────

def call_with_retry(url, max_attempts=5, base_delay=0.5, max_delay=8.0):
    """
    Retry with full jitter (random value in [0, computed_delay]).

    Full jitter (vs additive jitter) spreads retries more uniformly
    and is what AWS SDKs use. See Marc Brooker's 2015 AWS blog post.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(url, timeout=2.0)
            if resp.status_code == 200:
                return resp.json(), attempt
            raise requests.HTTPError(f"HTTP {resp.status_code}")
        except Exception as e:
            if attempt == max_attempts:
                raise
            cap = min(max_delay, base_delay * (2 ** (attempt - 1)))
            # Full jitter: uniform in [0, cap] — better than additive jitter
            # at high concurrency because it spreads retries more evenly
            wait = random.uniform(0, cap)
            print(f"    Attempt {attempt}/{max_attempts} failed ({e}). "
                  f"Retrying in {wait:.2f}s (cap={cap:.1f}s)...")
            time.sleep(wait)


# ── Circuit Breaker ──────────────────────────────────────────────────────────

class CircuitState(Enum):
    CLOSED    = "CLOSED"     # Normal — requests flow through
    OPEN      = "OPEN"       # Failing — requests blocked (fast-fail)
    HALF_OPEN = "HALF_OPEN"  # Probing — one request allowed through


class CircuitBreaker:
    """
    Count-based circuit breaker with min-request guard.

    Production note: real implementations (Resilience4j, Envoy) use a sliding
    time window (e.g., error rate over last 10s) rather than a raw count.
    The min-request guard here prevents false opens during low-traffic periods.
    """

    def __init__(self, failure_threshold=3, recovery_timeout=5.0,
                 min_requests=3):
        self.state             = CircuitState.CLOSED
        self.failure_count     = 0
        self.success_count     = 0
        self.failure_threshold = failure_threshold
        self.recovery_timeout  = recovery_timeout
        self.min_requests      = min_requests
        self.opened_at         = None
        self._lock             = threading.Lock()

    def call(self, fn):
        with self._lock:
            if self.state == CircuitState.OPEN:
                elapsed = time.time() - self.opened_at
                if elapsed >= self.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                    print(f"    [CB] OPEN -> HALF-OPEN after {elapsed:.1f}s "
                          f"cooldown. Sending probe...")
                else:
                    remaining = self.recovery_timeout - elapsed
                    raise Exception(
                        f"Circuit OPEN — fast-fail "
                        f"({remaining:.1f}s cooldown remaining)"
                    )

        # Execute outside the lock so the circuit doesn't block other callers
        try:
            result = fn()
            self._on_success()
            return result
        except Exception:
            self._on_failure()
            raise

    def _on_success(self):
        with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                print(f"    [CB] Probe succeeded: HALF-OPEN -> CLOSED")
            self.state         = CircuitState.CLOSED
            self.failure_count = 0
            self.success_count += 1

    def _on_failure(self):
        with self._lock:
            self.failure_count += 1
            total = self.failure_count + self.success_count
            if self.state == CircuitState.HALF_OPEN:
                print(f"    [CB] Probe failed: HALF-OPEN -> OPEN again")
                self.state     = CircuitState.OPEN
                self.opened_at = time.time()
            elif (self.failure_count >= self.failure_threshold
                  and total >= self.min_requests):
                print(f"    [CB] {self.failure_count}/{total} requests failed "
                      f"(>= threshold {self.failure_threshold}) -> OPEN")
                self.state     = CircuitState.OPEN
                self.opened_at = time.time()


# ── Bulkhead (thread-pool isolation) ────────────────────────────────────────

class BulkheadPool:
    """
    Fixed-size thread pool per downstream dependency.

    Submitting a task when all threads are busy raises immediately (fail-fast),
    preventing the caller's goroutine/thread from blocking indefinitely.
    This is analogous to Hystrix's thread-pool bulkhead mode.
    """

    def __init__(self, name, max_threads):
        self.name        = name
        self.max_threads = max_threads
        self._semaphore  = threading.Semaphore(max_threads)
        self.rejected    = 0
        self.completed   = 0

    def submit(self, fn):
        """Run fn in the pool. Raises if pool is exhausted."""
        acquired = self._semaphore.acquire(blocking=False)
        if not acquired:
            self.rejected += 1
            raise Exception(
                f"Bulkhead '{self.name}' full "
                f"({self.max_threads} threads busy) — request rejected"
            )
        try:
            result = fn()
            self.completed += 1
            return result
        finally:
            self._semaphore.release()


# ── Idempotency Key Store ────────────────────────────────────────────────────

class IdempotencyStore:
    """
    Server-side idempotency key deduplication.

    In production (Stripe, PayPal): stored in Redis/Postgres with TTL.
    Key maps to (status, response_body). On duplicate: return stored response.
    """

    def __init__(self):
        self._store = {}          # key -> {"status": ..., "response": ...}
        self._lock  = threading.Lock()

    def execute(self, key, fn):
        with self._lock:
            if key in self._store:
                entry = self._store[key]
                return entry["response"], True  # True = deduplicated

        # Execute outside lock (could be slow)
        response = fn()

        with self._lock:
            if key not in self._store:  # double-check after re-acquiring
                self._store[key] = {"response": response}
        return response, False  # False = first execution


def main():
    section("FAILURE MODES & RELIABILITY PATTERNS LAB")
    print("""
  Distributed systems fail in ways local programs do not:
    - Slow responses (timing failures) — caller blocks, threads accumulate
    - Dropped connections (omission failures) — no error, just silence
    - Cascading overload from retries — one failure becomes an avalanche
    - Partial failures — some nodes respond, others don't

  This lab demonstrates six core reliability patterns, each runnable
  against a real controllable downstream service.
""")

    if not wait_ready(DOWNSTREAM, "downstream service"):
        return

    # ── Phase 1: Timeout ─────────────────────────────────────────────────────
    section("Phase 1: Timeout — Bounded vs Unbounded Latency")
    print("""  Downstream set to 5s response time (simulates a slow DB query or GC pause).

  Without a timeout: the caller's thread blocks for the full 5s.
  With a 1s timeout:  the caller fails fast within its SLA budget.

  Rule of thumb: timeout < (your SLA) - (your processing overhead)
  Example: 500ms API SLA with 50ms processing -> 450ms timeout budget.
""")

    requests.get(f"{DOWNSTREAM}/slow", timeout=2)
    time.sleep(0.2)

    print("  [NO TIMEOUT] Calling slow downstream (simulated with 6s timeout):")
    t0 = time.time()
    try:
        resp = requests.get(f"{DOWNSTREAM}/data", timeout=6.0)
        elapsed = time.time() - t0
        data = resp.json()
        print(f"    Response received after {elapsed:.2f}s — "
              f"latency_mode={data.get('latency_mode')} "
              f"(caller blocked the entire time)")
    except requests.Timeout:
        elapsed = time.time() - t0
        print(f"    Timed out after {elapsed:.2f}s")

    print("\n  [WITH TIMEOUT] Calling slow downstream with 1s timeout:")
    t0 = time.time()
    try:
        resp = requests.get(f"{DOWNSTREAM}/data", timeout=1.0)
        elapsed = time.time() - t0
        print(f"    Response received after {elapsed:.2f}s")
    except requests.Timeout:
        elapsed = time.time() - t0
        print(f"    Timed out after {elapsed:.2f}s — "
              f"caller freed immediately; can apply fallback or return 503")

    recover_downstream()
    time.sleep(0.1)

    print("""
  Key insight: without a timeout, a single slow downstream can exhaust your
  entire thread pool. At 100 req/s and a 5s timeout, you'd need 500 threads
  just to absorb the slow calls — most services aren't configured for that.

  Configure THREE timeout types per dependency:
    - connect timeout: how long to wait for TCP handshake (~100-500ms)
    - read timeout:    how long to wait for response bytes (~SLA budget)
    - idle timeout:    how long to keep an idle keep-alive connection open
""")

    # ── Phase 2: Retry with backoff + jitter ─────────────────────────────────
    section("Phase 2: Retry with Exponential Backoff + Full Jitter")
    print("""  Downstream set to fail (HTTP 500) for ~15s — simulates a transient failure
  like a pod restart, a brief DB unavailability, or a momentary network blip.

  Strategy: retry with exponential backoff + FULL JITTER.
  Full jitter: wait = random.uniform(0, min(cap, base * 2^attempt))
  This is what AWS SDKs use. It spreads retries across the full window,
  preventing the synchronized "thundering herd" that additive jitter can cause.
""")

    requests.get(f"{DOWNSTREAM}/fail", timeout=2)
    time.sleep(0.1)

    # Recover the downstream after 4s so that retries eventually succeed.
    # With full jitter and base=0.5: delays are ~[0-0.5, 0-1.0, 0-2.0, 0-4.0].
    # Expected mean total wait before attempt 3: ~1.5s, so 4s recovery gives margin.
    def _delayed_recover(delay):
        time.sleep(delay)
        recover_downstream()

    recovery_thread = threading.Thread(target=_delayed_recover, args=(4.0,), daemon=True)
    recovery_thread.start()

    print("  Calling with retry (max 5 attempts, base_delay=0.5s, full jitter):")
    t0 = time.time()
    try:
        result, attempts = call_with_retry(f"{DOWNSTREAM}/data")
        elapsed = time.time() - t0
        print(f"\n    SUCCESS on attempt {attempts} after {elapsed:.2f}s total: {result}")
    except Exception as e:
        elapsed = time.time() - t0
        print(f"\n    All retries exhausted after {elapsed:.2f}s: {e}")

    recovery_thread.join(timeout=12)

    print("""
  Retry rules (violations cause production incidents):
    1. IDEMPOTENT ONLY — safe: GET, PUT, DELETE. Unsafe: POST without idempotency key.
       Retrying a charge without an idempotency key = double charge.
    2. BACKOFF + JITTER — without jitter, 1000 clients all failing simultaneously
       all retry at t+1s, t+2s, t+4s — perfectly synchronized, still a DDoS.
    3. GATE WITH CIRCUIT BREAKER — if the service is down for minutes, stop retrying.
    4. CAP RETRY BUDGET — set max elapsed time, not just max attempts.
       3 retries with 8s max backoff could block for 23s — longer than your SLA.
""")

    # ── Phase 3: Circuit Breaker ──────────────────────────────────────────────
    section("Phase 3: Circuit Breaker — State Machine")
    print("""  Circuit breaker protects a failing downstream by short-circuiting calls:

    CLOSED    -> requests flow normally
    OPEN      -> fast-fail immediately (no requests sent to downstream)
    HALF-OPEN -> one probe request; close on success, re-open on failure

  This demo uses threshold=3 failures, min_requests=3, cooldown=6s.
  The circuit will open, then probe after 6s, then close on recovery.
""")

    requests.get(f"{DOWNSTREAM}/fail", timeout=2)
    time.sleep(0.1)

    cb = CircuitBreaker(failure_threshold=3, recovery_timeout=6.0, min_requests=3)

    print("  Sending 10 requests. Downstream recovers between requests 7 and 8.\n")

    for i in range(1, 11):
        # After request 6 has been fast-failed, recover the downstream so the
        # HALF-OPEN probe (at request 7, after 6s cooldown) succeeds.
        if i == 7:
            print("  [Recovering downstream before probe attempt...]")
            recover_downstream()
            time.sleep(0.2)
            # Now wait out the cooldown — let the circuit transition to HALF-OPEN
            print(f"  [Waiting 6s for circuit cooldown...]")
            time.sleep(6.0)

        t0 = time.time()
        try:
            result = cb.call(
                lambda: requests.get(f"{DOWNSTREAM}/data", timeout=1.0).json()
            )
            elapsed = time.time() - t0
            print(f"  Request {i:2d}: [{cb.state.value:9s}] "
                  f"SUCCESS in {elapsed:.3f}s -> {result}")
        except Exception as e:
            elapsed = time.time() - t0
            print(f"  Request {i:2d}: [{cb.state.value:9s}] "
                  f"FAILED  in {elapsed:.3f}s -> {e}")

        if i < 4:
            time.sleep(0.3)   # small gap between early failures to be readable

    print("""
  What the circuit breaker buys you:
    - Requests after threshold fail in <1ms instead of waiting for timeout
    - The downstream gets breathing room (zero traffic during cooldown)
    - One probe confirms recovery before reopening to full traffic
    - Your thread pool stays free — no 1s-blocked threads accumulating

  Production gotcha — circuit oscillation:
    If the circuit opens too aggressively, the service starves of traffic,
    its in-memory cache goes cold, and it looks even slower when probed ->
    circuit re-opens -> repeat. Use a slow half-open ramp (Envoy's approach)
    or percentage-based recovery rather than binary open/closed.

  Production gotcha — per-instance vs shared state:
    An in-process circuit breaker (like above) makes decisions per pod.
    If you have 50 pods, each makes independent open/close decisions.
    A service mesh (Envoy, Linkerd) applies circuit breaking at the proxy
    layer with shared state — more consistent but higher infrastructure cost.
""")

    # ── Phase 4: Bulkhead ─────────────────────────────────────────────────────
    section("Phase 4: Bulkhead — Resource Pool Isolation")
    print("""  Named after ship bulkheads that contain flooding to one compartment.

  Scenario: your service calls two downstreams — payments API (critical)
  and recommendations API (non-critical). The recommendations API goes slow.

  WITHOUT bulkheads: both share a thread pool. Slow recommendations calls
  consume all threads, blocking payment calls too.

  WITH bulkheads: each downstream has its own fixed pool.
  Slow recommendations calls only exhaust the recommendations pool.
""")

    # Simulate slow downstream for recommendations
    requests.get(f"{DOWNSTREAM}/slow", timeout=2)
    time.sleep(0.1)

    # Two separate pools: recommendations (3 threads) and payments (3 threads)
    recommendations_pool = BulkheadPool("recommendations", max_threads=3)
    payments_pool         = BulkheadPool("payments",        max_threads=3)

    results = {"rec_ok": 0, "rec_rejected": 0, "pay_ok": 0, "pay_rejected": 0}
    results_lock = threading.Lock()

    def call_recommendations():
        try:
            recommendations_pool.submit(
                lambda: requests.get(f"{DOWNSTREAM}/data", timeout=0.5)
            )
            with results_lock:
                results["rec_ok"] += 1
        except Exception as e:
            with results_lock:
                results["rec_rejected"] += 1

    def call_payments():
        # Payments calls a healthy endpoint (health = fast)
        try:
            payments_pool.submit(
                lambda: requests.get(f"{DOWNSTREAM}/health", timeout=0.5)
            )
            with results_lock:
                results["pay_ok"] += 1
        except Exception as e:
            with results_lock:
                results["pay_rejected"] += 1

    print("  Firing 12 concurrent recommendation calls (pool=3) + "
          "12 concurrent payment calls (pool=3)...")
    print("  Recommendations go to the slow endpoint (5s response).")
    print("  Payments go to /health (fast).\n")

    threads = []
    for _ in range(12):
        threads.append(threading.Thread(target=call_recommendations))
        threads.append(threading.Thread(target=call_payments))

    t0 = time.time()
    for th in threads:
        th.start()

    # Collect results quickly — don't wait for slow threads to finish
    time.sleep(1.0)
    print(f"  After 1s:")
    print(f"    Recommendations: {results['rec_ok']} ok, "
          f"{results['rec_rejected']} rejected (pool exhausted)")
    print(f"    Payments:        {results['pay_ok']} ok, "
          f"{results['pay_rejected']} rejected")
    print()

    if results["pay_ok"] >= 10 and results["rec_rejected"] >= 8:
        print("  Bulkhead working: payment calls succeeded despite recommendations "
              "pool being saturated.")
    elif results["pay_ok"] >= 10:
        print("  Payments unaffected. Recommendations may have completed quickly "
              "(machine fast) — in production with real network latency the "
              "pool would fill up.")
    else:
        print("  Note: results vary by machine speed. The key pattern: "
              "separate pools guarantee payments can never be starved by "
              "a slow recommendations service.")

    recover_downstream()
    # Let background threads finish so we don't get output interleaving
    for th in threads:
        th.join(timeout=8)

    print("""
  Bulkhead sizing:
    - Don't share a global thread pool across all downstreams
    - Size each pool to: (target_concurrency) = (throughput) x (latency_p99)
      Example: 100 req/s to a service with p99=50ms -> 100 * 0.05 = 5 threads
    - Add headroom (~2x) for latency spikes

  Semaphore-based bulkheads (what we used above) are lighter than thread pools:
    - No dedicated threads — uses caller's thread
    - Simpler to implement; preferred in async frameworks (asyncio, Tokio)
""")

    # ── Phase 5: Idempotency Keys ─────────────────────────────────────────────
    section("Phase 5: Idempotency Keys — Safe Retries for Non-Idempotent Ops")
    print("""  Problem: POST /charge is NOT idempotent. If the client retries after a
  network error, the charge executes twice. Idempotency keys prevent this.

  Protocol:
    1. Client generates a UUID per logical operation (e.g., per "place order")
    2. Client sends the UUID in the request header (Idempotency-Key: <uuid>)
    3. Server stores (uuid -> response) on first execution
    4. On duplicate UUID: server returns the stored response without re-executing

  Used by: Stripe, PayPal, Braintree, Adyen.
""")

    store = IdempotencyStore()
    charge_count = 0

    def do_charge(amount, idempotency_key):
        """Simulate a payment charge — side-effectful, non-idempotent."""
        nonlocal charge_count

        def _execute():
            nonlocal charge_count
            charge_count += 1
            return {
                "charge_id": f"ch_{uuid.uuid4().hex[:8]}",
                "amount":    amount,
                "execution": charge_count,
                "status":    "success",
            }

        return store.execute(idempotency_key, _execute)

    # Simulate: client sends charge, gets a network error, retries 3 times
    key = str(uuid.uuid4())
    print(f"  Idempotency key: {key}\n")

    print("  Attempt 1 — first execution:")
    resp, deduplicated = do_charge(amount=9999, idempotency_key=key)
    print(f"    deduplicated={deduplicated}, charge_id={resp['charge_id']}, "
          f"execution_count={resp['execution']}")

    print("\n  Attempt 2 — client retries (e.g., network timeout on response):")
    resp, deduplicated = do_charge(amount=9999, idempotency_key=key)
    print(f"    deduplicated={deduplicated}, charge_id={resp['charge_id']}, "
          f"execution_count={resp['execution']}")

    print("\n  Attempt 3 — client retries again:")
    resp, deduplicated = do_charge(amount=9999, idempotency_key=key)
    print(f"    deduplicated={deduplicated}, charge_id={resp['charge_id']}, "
          f"execution_count={resp['execution']}")

    print(f"\n  Total actual charge executions: {charge_count} "
          f"(despite 3 client attempts)")

    print("\n  New key — different logical operation:")
    key2 = str(uuid.uuid4())
    resp2, deduplicated2 = do_charge(amount=5000, idempotency_key=key2)
    print(f"    deduplicated={deduplicated2}, charge_id={resp2['charge_id']}, "
          f"execution_count={resp2['execution']}")
    print(f"  Total charge executions now: {charge_count}")

    print("""
  Implementation details that matter:
    - Key scope: per-user, per-operation type (a key shouldn't work across
      different endpoints or different users)
    - TTL: keys expire (Stripe uses 24h). After TTL, same key = new execution.
    - Storage: Redis with TTL is common. Must be durable enough to survive
      server restarts during the retry window.
    - In-flight deduplication: if two concurrent requests arrive with the same key,
      one should wait for the other (use Redis SET NX for distributed locking).
    - Return exactly the same response body + status code, not just "duplicate".
      The client may not know it was a duplicate; it just needs a valid response.
""")

    # ── Phase 6: Cascading Failure ────────────────────────────────────────────
    section("Phase 6: Cascading Failure — Propagation and Containment")

    recover_downstream()
    time.sleep(0.2)

    print("""  Simulating a 3-tier call chain:

    [caller] --calls--> [service-B] --calls--> [service-C] (our downstream)

  service-C becomes slow (5s response). Without timeouts, the latency
  propagates to service-B, then to the caller. With timeouts + fallback,
  each layer fails fast and returns a degraded-but-available response.
""")

    def service_c(timeout=None):
        return requests.get(f"{DOWNSTREAM}/data", timeout=timeout).json()

    def service_b(c_timeout=None):
        t0 = time.time()
        try:
            result = service_c(timeout=c_timeout)
            return {"source": "live", "data": result, "latency_ms": int((time.time()-t0)*1000)}
        except requests.Timeout:
            # Fallback: return stale cached data rather than propagating the error
            return {"source": "cache", "data": {"result": "cached_ok"}, "latency_ms": int((time.time()-t0)*1000)}
        except Exception as e:
            return {"source": "error", "data": str(e), "latency_ms": int((time.time()-t0)*1000)}

    def caller_fn(c_timeout=None):
        t0 = time.time()
        response = service_b(c_timeout=c_timeout)
        return response, time.time() - t0

    requests.get(f"{DOWNSTREAM}/slow", timeout=2)
    time.sleep(0.2)

    print("  --- WITHOUT timeouts (latency cascades to caller) ---")
    t0 = time.time()
    result, elapsed = caller_fn(c_timeout=6.0)
    print(f"  caller blocked for {elapsed:.2f}s — source={result['source']}, "
          f"service-B latency={result['latency_ms']}ms")

    print("\n  --- WITH 1s timeout at each layer (fail fast + fallback) ---")
    result, elapsed = caller_fn(c_timeout=1.0)
    print(f"  caller received response in {elapsed:.2f}s — "
          f"source={result['source']}, "
          f"service-B latency={result['latency_ms']}ms")
    if result["source"] == "cache":
        print("  Fallback applied: stale cache returned; availability preserved "
              "at the cost of freshness")

    recover_downstream()

    print("""
  Cascade prevention checklist:
    [x] Timeout at EVERY layer — not just the outermost call
    [x] Circuit breaker per downstream — stop hammering a broken service
    [x] Bulkhead — isolate thread pools so one slow service can't starve others
    [x] Fallback — return degraded response rather than propagating failure
    [x] Load shedding — return 503 early when over capacity; don't queue forever
    [x] Retry budgets — cap total retries per time window (not just per call)
    [x] Deadline propagation — pass remaining SLA budget downstream
        (gRPC deadline propagation; if caller has 300ms left, set downstream
         timeout to 250ms, not independently to 1s)

  The full cascade scenario:
    1. DB gets hot -> service-C responds slowly
    2. service-B retries -> amplifies load on service-C (3x, 10x)
    3. service-B thread pool fills with pending calls -> service-B slows
    4. caller retries service-B -> amplifies further
    5. In minutes: entire stack is down

  The fix: each retry amplification step is prevented by the pattern
  applied at that layer. The patterns compound.
""")

    # ── Summary ────────────────────────────────────────────────────────────────
    section("Summary — Reliability Pattern Cheat Sheet")
    print("""
  Pattern           Benefit                         Cost / Gotcha
  ────────────────────────────────────────────────────────────────────────────
  Timeout           Bounds failure duration         Must tune per dependency;
                                                    wrong value = premature
                                                    failures or long hangs.
                                                    Set connect + read + idle.

  Retry             Recovers transient failures     Amplifies load; idempotent
  + backoff/jitter                                  ops only; needs jitter;
                                                    cap total budget not just
                                                    attempt count.

  Circuit breaker   Fast-fail; prevents cascade;   Adds state; oscillation if
                    gives downstream recovery time  thresholds too sensitive.
                                                    Per-instance vs shared state.

  Bulkhead          Isolates failure domains;       More pools to configure;
                    critical paths stay alive       semaphore vs thread pool
                    when non-critical paths fail    tradeoffs.

  Fallback          Turns outage into degradation   Fallback may itself fail
                                                    (test it!). Product must
                                                    define acceptable degradation.

  Idempotency key   Safe retry for non-idempotent   Key storage + TTL + in-flight
                    ops; exactly-once semantics      deduplication complexity.

  Load shedding     Controlled degradation under    Requires capacity estimation;
                    overload; prevents queue blowup  shedding too early = worse
                                                     than queuing.

  Deadline          Prevents retry budget waste     Requires framework support
  propagation       across tier boundaries          (gRPC, internal headers).
  ────────────────────────────────────────────────────────────────────────────

  Cascading failure is the enemy.
  One slow service -> retries -> load amplification -> more failures -> outage.
  Timeouts + circuit breakers + bulkheads + load shedding break the cascade
  at each layer. The patterns work together; using only one is not enough.

  Next steps: ../../02-advanced/01-consistent-hashing/
""")


if __name__ == "__main__":
    main()
