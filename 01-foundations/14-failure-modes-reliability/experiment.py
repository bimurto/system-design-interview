#!/usr/bin/env python3
"""
Failure Modes & Reliability Patterns Lab

Prerequisites: docker compose up -d (wait ~5s)

What this demonstrates:
  1. Timeout: bounded vs unbounded latency when downstream is slow
  2. Retry with exponential backoff + jitter: recovery from transient failures
  3. Circuit breaker: closed → open → half-open → closed state machine
  4. Cascading failure: how a slow downstream propagates without timeouts
  5. Fallback: degraded response when primary fails
"""

import json
import time
import random
import subprocess
from enum import Enum

try:
    import requests
    import redis
except ImportError:
    print("Installing dependencies...")
    subprocess.run(["pip", "install", "requests", "redis", "-q"], check=True)
    import requests
    import redis

DOWNSTREAM = "http://localhost:9001"


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


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


# ── Retry with exponential backoff + jitter ───────────────────

def call_with_retry(url, max_attempts=4, base_delay=0.5):
    """Retry with exponential backoff and jitter."""
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(url, timeout=2.0)
            if resp.status_code == 200:
                return resp.json(), attempt
            raise requests.HTTPError(f"HTTP {resp.status_code}")
        except Exception as e:
            if attempt == max_attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            jitter = random.uniform(0, delay * 0.3)
            wait = delay + jitter
            print(f"    Attempt {attempt} failed ({e}). Retrying in {wait:.2f}s...")
            time.sleep(wait)


# ── Circuit Breaker ────────────────────────────────────────────

class CircuitState(Enum):
    CLOSED    = "CLOSED"     # Normal — requests flow through
    OPEN      = "OPEN"       # Failing — requests blocked
    HALF_OPEN = "HALF_OPEN"  # Probing — one request allowed


class CircuitBreaker:
    def __init__(self, failure_threshold=3, recovery_timeout=5.0):
        self.state             = CircuitState.CLOSED
        self.failure_count     = 0
        self.failure_threshold = failure_threshold
        self.recovery_timeout  = recovery_timeout
        self.opened_at         = None

    def call(self, fn):
        if self.state == CircuitState.OPEN:
            elapsed = time.time() - self.opened_at
            if elapsed >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                print(f"    [CB] → HALF-OPEN after {elapsed:.1f}s cooldown. Sending probe...")
            else:
                raise Exception(f"Circuit OPEN — fast-fail (cooldown: {self.recovery_timeout - elapsed:.1f}s remaining)")

        try:
            result = fn()
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise

    def _on_success(self):
        if self.state == CircuitState.HALF_OPEN:
            print(f"    [CB] Probe succeeded → CLOSED")
        self.state         = CircuitState.CLOSED
        self.failure_count = 0

    def _on_failure(self):
        self.failure_count += 1
        if self.state == CircuitState.HALF_OPEN:
            print(f"    [CB] Probe failed → OPEN again")
            self.state     = CircuitState.OPEN
            self.opened_at = time.time()
        elif self.failure_count >= self.failure_threshold:
            print(f"    [CB] {self.failure_count} failures → OPEN")
            self.state     = CircuitState.OPEN
            self.opened_at = time.time()


def main():
    section("FAILURE MODES & RELIABILITY PATTERNS LAB")
    print("""
  Distributed systems fail in ways local programs do not:
    - Slow responses (timing failures)
    - Dropped connections (omission failures)
    - Cascading overload from retries
    - Downstream recovery after failure

  This lab demonstrates the four core reliability patterns.
""")

    if not wait_ready(DOWNSTREAM, "downstream service"):
        return

    # ── Phase 1: Timeout ──────────────────────────────────────────
    section("Phase 1: Timeout — Bounded vs Unbounded Latency")
    print("""  Making downstream slow (5s response time).
  Without a timeout: caller blocks for 5s.
  With a 1s timeout: caller fails fast and can handle it.
""")

    requests.get(f"{DOWNSTREAM}/slow", timeout=2)  # trigger slow mode
    time.sleep(0.2)

    # Without timeout (we use a very long one to simulate "no timeout")
    print("  Calling slow downstream WITHOUT timeout (simulated with 6s timeout):")
    t0 = time.time()
    try:
        resp = requests.get(f"{DOWNSTREAM}/data", timeout=6.0)
        elapsed = time.time() - t0
        print(f"    Response received after {elapsed:.2f}s — caller blocked the whole time")
    except requests.Timeout:
        elapsed = time.time() - t0
        print(f"    Timed out after {elapsed:.2f}s")

    # With tight timeout
    print("\n  Calling slow downstream WITH 1s timeout:")
    t0 = time.time()
    try:
        resp = requests.get(f"{DOWNSTREAM}/data", timeout=1.0)
        elapsed = time.time() - t0
        print(f"    Response received after {elapsed:.2f}s")
    except requests.Timeout:
        elapsed = time.time() - t0
        print(f"    Timed out after {elapsed:.2f}s — caller freed immediately to handle fallback")

    requests.get(f"{DOWNSTREAM}/recover", timeout=2)
    print("""
  Timeout rule: timeout < your own SLA budget.
  If your API must respond in 500ms, downstream timeouts must be ~200ms.
""")

    # ── Phase 2: Retry with backoff + jitter ──────────────────────
    section("Phase 2: Retry with Exponential Backoff + Jitter")
    print("""  Making downstream fail for ~5s (transient failure window).
  Retry with exponential backoff recovers without manual intervention.
""")

    requests.get(f"{DOWNSTREAM}/fail", timeout=2)  # fail for 10s
    time.sleep(0.1)

    print("  Calling with retry (max 4 attempts, base delay 0.5s + jitter):")
    # Recover after a few seconds so retries eventually succeed
    def do_recover():
        time.sleep(3)
        try:
            requests.get(f"{DOWNSTREAM}/recover", timeout=2)
        except Exception:
            pass

    import threading
    t = threading.Thread(target=do_recover, daemon=True)
    t.start()

    t0 = time.time()
    try:
        result, attempts = call_with_retry(f"{DOWNSTREAM}/data")
        elapsed = time.time() - t0
        print(f"    Success on attempt {attempts} after {elapsed:.2f}s: {result}")
    except Exception as e:
        print(f"    All retries exhausted: {e}")

    print("""
  Key rules for retries:
    1. Only retry idempotent operations (GET, PUT, DELETE — not POST without idempotency key)
    2. Always use exponential backoff with jitter (avoids synchronized retry storms)
    3. Gate retries behind a circuit breaker (stop retrying into a broken service)
    4. Set a max retry budget (total retry count or elapsed time limit)
""")

    # ── Phase 3: Circuit Breaker ──────────────────────────────────
    section("Phase 3: Circuit Breaker")
    print("""  Circuit breaker state machine:
    CLOSED    → requests flow normally
    OPEN      → fast-fail immediately (no requests sent)
    HALF-OPEN → one probe request; close on success, reopen on failure
""")

    requests.get(f"{DOWNSTREAM}/fail", timeout=2)  # fail for 10s
    time.sleep(0.1)

    cb = CircuitBreaker(failure_threshold=3, recovery_timeout=4.0)

    print("  Sending 8 requests with circuit breaker (threshold=3, cooldown=4s):\n")
    for i in range(1, 9):
        if i == 6:
            # Recover the downstream so the probe in HALF-OPEN succeeds
            requests.get(f"{DOWNSTREAM}/recover", timeout=2)
            time.sleep(0.2)

        try:
            result = cb.call(lambda: requests.get(f"{DOWNSTREAM}/data", timeout=1.0).json())
            print(f"  Request {i}: [{cb.state.value:9s}] SUCCESS → {result}")
        except Exception as e:
            print(f"  Request {i}: [{cb.state.value:9s}] FAILED  → {e}")

        time.sleep(0.5 if i < 4 else 0)

    print("""
  What the circuit breaker buys you:
    - Requests 4+ fail instantly rather than waiting for timeout
    - The downstream gets a 4s breathing room with zero traffic
    - One probe confirms recovery before reopening to full traffic
    - Thread pool stays free — no blocked threads accumulating
""")

    # ── Phase 4: Cascading failure ────────────────────────────────
    section("Phase 4: Cascading Failure (and How to Stop It)")

    requests.get(f"{DOWNSTREAM}/recover", timeout=2)
    time.sleep(0.2)

    print("""  Simulating a 3-tier call chain: caller → service-B → service-C (downstream)
  service-C becomes slow.

  WITHOUT timeouts: slowness propagates up the chain.
  WITH timeouts:    each layer fails fast and applies fallback.
""")

    def service_c(timeout=None):
        """Calls the downstream (which is slow)."""
        return requests.get(f"{DOWNSTREAM}/data", timeout=timeout).json()

    def service_b(c_timeout=None, b_timeout=None):
        """Calls service C, may time out."""
        t0 = time.time()
        try:
            result = service_c(timeout=c_timeout)
            return {"source": "service_c", "result": result, "latency": time.time() - t0}
        except requests.Timeout:
            return {"source": "fallback", "result": "cached_data", "latency": time.time() - t0}

    def caller(b_timeout=None):
        t0 = time.time()
        result = service_b(c_timeout=b_timeout)
        return result, time.time() - t0

    # Make downstream slow
    requests.get(f"{DOWNSTREAM}/slow", timeout=2)
    time.sleep(0.2)

    print("  --- Without timeouts (slowness cascades) ---")
    t0 = time.time()
    result, elapsed = caller(b_timeout=6.0)
    print(f"  caller received response after {elapsed:.2f}s (blocked the whole time)")

    print("\n  --- With 1s timeout at each layer (fail fast + fallback) ---")
    result, elapsed = caller(b_timeout=1.0)
    print(f"  caller received response after {elapsed:.2f}s → source={result['source']}")
    if result["source"] == "fallback":
        print("  Fallback applied: returned cached data instead of waiting")

    requests.get(f"{DOWNSTREAM}/recover", timeout=2)

    print("""
  Cascade prevention checklist:
    ✓ Timeout at every layer (not just the outermost)
    ✓ Circuit breaker per downstream dependency
    ✓ Bulkhead: separate thread pools per downstream
    ✓ Fallback for degraded responses
    ✓ Load shedding: return 503 when over capacity rather than queuing
""")

    # ── Summary ───────────────────────────────────────────────────
    section("Summary")
    print("""
  Reliability Pattern Cheat Sheet:
  ─────────────────────────────────────────────────────────────
  Timeout          — every network call needs a deadline
                     rule: timeout < your SLA budget for that call

  Retry            — recovers transient failures
                     rules: idempotent ops only; backoff + jitter; cap retries

  Circuit breaker  — fast-fail when downstream is broken
                     states: CLOSED → OPEN → HALF-OPEN → CLOSED

  Bulkhead         — isolate resource pools per downstream
                     prevents one slow service from starving others

  Fallback         — return degraded response on failure
                     turns availability failure into quality degradation

  Idempotency key  — makes POST-style ops safe to retry
                     server de-duplicates by key; exactly-once semantics

  Cascading failure is the enemy. One slow service → retries →
  amplified load → more failures → total outage.
  Timeouts + circuit breakers + load shedding break the cascade.

  Next steps: ../../02-advanced/01-consistent-hashing/
""")


if __name__ == "__main__":
    main()
