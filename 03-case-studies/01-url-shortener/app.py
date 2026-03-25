#!/usr/bin/env python3
"""
URL Shortener Flask Service

Endpoints:
  POST /shorten   body: {"url": "https://..."} → {"short_url": ..., "short_code": ...}
  GET  /<code>    → 302 redirect to original URL (Redis cache-aside, TTL 24h)
  GET  /health    → {"status": "ok"}
  GET  /stats     → cache hit/miss counters
"""

import os
import time
import hashlib
import string
import redis
import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify, redirect, abort

app = Flask(__name__)

BASE62 = string.digits + string.ascii_letters  # 62 chars: 0-9a-zA-Z
CACHE_TTL = 86400  # 24 hours

# ── DB / Cache connections ──────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(os.environ["DATABASE_URL"])

def get_redis():
    return redis.from_url(os.environ["REDIS_URL"], decode_responses=True)

# ── Schema bootstrap ────────────────────────────────────────────────────────

def init_db():
    conn = get_db()
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
            cur.execute("CREATE INDEX IF NOT EXISTS idx_short_code ON urls(short_code)")
    conn.close()

# ── Base62 encoding ─────────────────────────────────────────────────────────

def base62_encode(num: int) -> str:
    """Encode a positive integer to base-62 string."""
    if num == 0:
        return BASE62[0]
    chars = []
    while num:
        chars.append(BASE62[num % 62])
        num //= 62
    return "".join(reversed(chars))

# ── Routes ──────────────────────────────────────────────────────────────────

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

    conn = get_db()
    try:
        with conn:
            with conn.cursor() as cur:
                # Insert and get auto-increment ID → encode as base62
                cur.execute(
                    "INSERT INTO urls (short_code, original) VALUES (%s, %s) "
                    "ON CONFLICT (short_code) DO NOTHING RETURNING id",
                    ("__placeholder__", original_url),
                )
                row = cur.fetchone()
                if row is None:
                    # Collision — retry with hash fallback
                    cur.execute("SELECT short_code FROM urls WHERE original = %s LIMIT 1", (original_url,))
                    existing = cur.fetchone()
                    if existing:
                        code = existing[0]
                        base_url = request.host_url.rstrip("/")
                        return jsonify({"short_url": f"{base_url}/{code}", "short_code": code})

                new_id = row[0]
                code   = base62_encode(new_id)
                # Update with real code (collision detection: unique constraint)
                cur.execute("UPDATE urls SET short_code = %s WHERE id = %s", (code, new_id))
    finally:
        conn.close()

    base_url = request.host_url.rstrip("/")
    return jsonify({"short_url": f"{base_url}/{code}", "short_code": code}), 201

@app.route("/<code>")
def redirect_url(code):
    r = get_redis()
    cache_key = f"url:{code}"

    # Cache-aside: check Redis first
    cached = r.get(cache_key)
    if cached:
        r.incr("stat:cache_hits")
        return redirect(cached, code=302)

    r.incr("stat:cache_misses")

    # Cache miss: query Postgres
    conn = get_db()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE urls SET hits = hits + 1 WHERE short_code = %s RETURNING original",
                    (code,),
                )
                row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        abort(404)

    original = row[0]
    r.setex(cache_key, CACHE_TTL, original)
    return redirect(original, code=302)


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
