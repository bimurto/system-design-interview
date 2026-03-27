#!/usr/bin/env python3
"""
URL Shortener Flask Service

Endpoints:
  POST /shorten      body: {"url": "https://..."} -> {"short_url": ..., "short_code": ...}
  GET  /<code>       -> 302 redirect to original URL (Redis cache-aside, TTL 24h)
  GET  /health       -> {"status": "ok"}
  GET  /stats        -> cache hit/miss counters
  POST /flush_hits   -> batch-flush Redis hit counters to Postgres

Design notes (relevant to the lab):
  - Connection pooling: a psycopg2 ThreadedConnectionPool is shared across
    requests. Without this, each request opens/closes a TCP connection to
    Postgres -- fine for one user, catastrophic at 115K RPS. The pool keeps
    minconn warm connections and refuses to exceed maxconn, creating natural
    back-pressure instead of piling up Postgres connections.
  - Hit counting: redirects INCR a per-code Redis counter (O(1), no row lock).
    /flush_hits bulk-updates Postgres with the accumulated deltas. This models
    the "Redis INCR + batch flush" pattern described in Deep Dive #3 and makes
    the contrast with naive per-redirect UPDATEs visible in the experiment.
  - The shorten endpoint does INSERT ... RETURNING id so the base62 code is
    derived from the real auto-increment ID without a separate SELECT.
"""

import os
import time
import string
import redis
import psycopg2
import psycopg2.pool
from flask import Flask, request, jsonify, redirect, abort

app = Flask(__name__)

BASE62 = string.digits + string.ascii_letters  # 62 chars: 0-9a-zA-Z
CACHE_TTL = 86400  # 24 hours

# -- Connection pool (shared across threads) ----------------------------------
# minconn=2 keeps two warm connections ready; maxconn=10 caps Postgres load.
# In production, size maxconn to roughly (2 * CPU cores + 1) per app server,
# and put PgBouncer in front for connection multiplexing at very high RPS.

_pg_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_redis_client: redis.Redis | None = None


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pg_pool
    if _pg_pool is None:
        _pg_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            dsn=os.environ["DATABASE_URL"],
        )
    return _pg_pool


def get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    return _redis_client


# -- Schema bootstrap ---------------------------------------------------------

def init_db():
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS urls (
                        id          BIGSERIAL PRIMARY KEY,
                        short_code  TEXT UNIQUE NOT NULL,
                        original    TEXT NOT NULL,
                        created_at  TIMESTAMPTZ DEFAULT NOW(),
                        hits        BIGINT DEFAULT 0
                    )
                """)
                # The index on short_code is the hot path for every redirect.
                # Postgres creates one automatically for UNIQUE constraints, but
                # we name it explicitly so it appears clearly in EXPLAIN output.
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_short_code ON urls(short_code)"
                )
    finally:
        pool.putconn(conn)


# -- Base62 encoding ----------------------------------------------------------

def base62_encode(num: int) -> str:
    """Encode a positive integer to a base-62 string (URL-safe, avoids +/)."""
    if num == 0:
        return BASE62[0]
    chars = []
    while num:
        chars.append(BASE62[num % 62])
        num //= 62
    return "".join(reversed(chars))


# -- Routes -------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/stats")
def stats():
    r = get_redis()
    hits   = int(r.get("stat:cache_hits")   or 0)
    misses = int(r.get("stat:cache_misses") or 0)
    total  = hits + misses
    rate   = hits / total * 100 if total else 0
    return jsonify({
        "cache_hits":   hits,
        "cache_misses": misses,
        "hit_rate_pct": round(rate, 1),
    })


@app.route("/shorten", methods=["POST"])
def shorten():
    data = request.get_json(force=True)
    original_url = (data or {}).get("url", "").strip()
    if not original_url:
        return jsonify({"error": "url is required"}), 400

    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn:
            with conn.cursor() as cur:
                # Step 1: reserve a row and retrieve the auto-increment ID.
                # A temporary placeholder is used so the transaction can commit
                # before we compute the real code -- this prevents a race
                # where two concurrent writers both compute base62(same_id).
                cur.execute(
                    "INSERT INTO urls (short_code, original) VALUES (%s, %s) RETURNING id",
                    ("__tmp__", original_url),
                )
                new_id = cur.fetchone()[0]

                # Step 2: compute the real code and update in the same transaction.
                code = base62_encode(new_id)
                cur.execute(
                    "UPDATE urls SET short_code = %s WHERE id = %s",
                    (code, new_id),
                )
    finally:
        pool.putconn(conn)

    base_url = request.host_url.rstrip("/")
    return jsonify({"short_url": f"{base_url}/{code}", "short_code": code}), 201


@app.route("/<code>")
def redirect_url(code):
    r = get_redis()
    cache_key = f"url:{code}"

    # Cache-aside read: O(1) Redis GET before touching Postgres.
    cached = r.get(cache_key)
    if cached:
        r.incr("stat:cache_hits")
        # Increment hit counter in Redis -- batch-flush to Postgres via /flush_hits.
        # This avoids a row-locking UPDATE on every redirect (the naive approach
        # that saturates Postgres at high RPS).
        r.incr(f"hits:{code}")
        return redirect(cached, code=302)

    r.incr("stat:cache_misses")

    # Cache miss: pull from Postgres using the pooled connection.
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT original FROM urls WHERE short_code = %s",
                    (code,),
                )
                row = cur.fetchone()
    finally:
        pool.putconn(conn)

    if not row:
        abort(404)

    original = row[0]
    # Populate cache so subsequent reads never touch Postgres.
    r.setex(cache_key, CACHE_TTL, original)
    # Count this miss too.
    r.incr(f"hits:{code}")
    return redirect(original, code=302)


@app.route("/flush_hits", methods=["POST"])
def flush_hits():
    """
    Batch-flush Redis hit counters to Postgres.

    This endpoint models the background worker described in Deep Dive #3.
    Instead of a row-locking UPDATE on every redirect (which would saturate
    Postgres at 115K RPS), redirects INCR a per-code Redis counter (O(1)).
    This endpoint atomically GETDEL each counter and applies the delta to
    Postgres in bulk -- converting 115K writes/second into one batch per flush.

    In production this runs as a cron job or Celery beat task every ~60s.
    It is exposed as HTTP here so experiment.py can trigger it explicitly and
    verify that hits accumulate correctly end-to-end.

    Trade-off: hit counts in Postgres lag by up to one flush interval (60s).
    For a URL shortener this is acceptable. For billing or fraud detection,
    use synchronous writes or a streaming pipeline (Kafka -> aggregator).
    """
    r = get_redis()
    # scan_iter issues incremental SCAN commands -- safe on large keyspaces
    # because it never blocks Redis the way KEYS "*" would.
    keys = list(r.scan_iter("hits:*"))
    if not keys:
        return jsonify({"flushed": 0})

    # Atomically retrieve-and-delete each counter in a pipeline so we don't
    # double-count if a flush overlaps with incoming redirects.
    pipe = r.pipeline(transaction=False)
    for key in keys:
        pipe.getdel(key)
    deltas = pipe.execute()

    pool = get_pool()
    conn = pool.getconn()
    flushed = 0
    try:
        with conn:
            with conn.cursor() as cur:
                for key, delta in zip(keys, deltas):
                    if delta and int(delta) > 0:
                        short_code = key.split(":", 1)[1]
                        cur.execute(
                            "UPDATE urls SET hits = hits + %s WHERE short_code = %s",
                            (int(delta), short_code),
                        )
                        flushed += 1
    finally:
        pool.putconn(conn)

    return jsonify({"flushed": flushed, "keys_processed": len(keys)})


if __name__ == "__main__":
    # Wait for DB to be ready (healthcheck should handle this, but belt-and-suspenders)
    for attempt in range(20):
        try:
            init_db()
            break
        except Exception as e:
            print(f"DB not ready (attempt {attempt+1}): {e}")
            time.sleep(2)

    app.run(host="0.0.0.0", port=5000, debug=False)
