#!/usr/bin/env python3
"""
api_server.py — Flask API with Redis-backed distributed rate limiting.

Endpoints:
  GET /health                → 200 OK
  GET /api/data              → 200 OK or 429 Too Many Requests
  GET /api/data?key=<api_key>→ rate limited by api_key (key-level limits)
  GET /stats                 → current rate limit counters

Rate limiting uses a Lua script for atomic check-and-increment.
"""

import os
import time
from flask import Flask, request, jsonify
import redis

app = Flask(__name__)
SERVER_ID = os.environ.get("SERVER_ID", "unknown")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

# Default limits: 10 requests per 10-second window
DEFAULT_LIMIT = 10
DEFAULT_WINDOW = 10  # seconds

# Per-key limits (simulating Stripe-style per-API-key limits)
KEY_LIMITS = {
    "key-premium": (50, 10),   # 50 req / 10s
    "key-standard": (10, 10),  # 10 req / 10s
    "key-trial": (3, 10),      # 3 req / 10s
}

# Lua script for atomic fixed-window rate limiting
RATE_LIMIT_LUA = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

local window_key = math.floor(now / window)
local full_key = key .. ":" .. window_key

local count = redis.call("INCR", full_key)
if count == 1 then
    redis.call("EXPIRE", full_key, window)
end
if count > limit then
    return {0, count, limit}
else
    return {1, count, limit}
end
"""

# Connect to Redis (with fail-open fallback)
try:
    r = redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
    r.ping()
    lua_script = r.register_script(RATE_LIMIT_LUA)
    REDIS_AVAILABLE = True
except Exception:
    REDIS_AVAILABLE = False
    lua_script = None


def check_rate_limit(api_key: str) -> tuple[bool, int, int]:
    """
    Returns (allowed, current_count, limit).
    If Redis is unavailable: fail-open (return allowed=True).
    """
    global REDIS_AVAILABLE, lua_script

    if not REDIS_AVAILABLE:
        return True, 0, 0  # fail-open

    limit, window = KEY_LIMITS.get(api_key, (DEFAULT_LIMIT, DEFAULT_WINDOW))
    redis_key = f"ratelimit:{api_key}"
    now = int(time.time())

    try:
        result = lua_script(keys=[redis_key], args=[limit, window, now])
        allowed, count, lim = result
        return bool(allowed), int(count), int(lim)
    except Exception:
        REDIS_AVAILABLE = False
        return True, 0, 0  # fail-open on Redis error


@app.route("/health")
def health():
    return jsonify({"status": "ok", "server": SERVER_ID, "redis": REDIS_AVAILABLE})


@app.route("/api/data")
def api_data():
    api_key = request.args.get("key", "anonymous")
    allowed, count, limit = check_rate_limit(api_key)

    headers = {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(max(0, limit - count)),
        "X-Server": SERVER_ID,
        "X-Redis-Available": str(REDIS_AVAILABLE),
    }

    if not allowed:
        resp = jsonify({
            "error": "rate_limit_exceeded",
            "message": f"Too many requests. Limit: {limit} per window.",
            "current_count": count,
            "server": SERVER_ID,
        })
        resp.status_code = 429
        for k, v in headers.items():
            resp.headers[k] = v
        return resp

    resp = jsonify({
        "data": f"Hello from {SERVER_ID}!",
        "api_key": api_key,
        "request_count": count,
        "limit": limit,
        "server": SERVER_ID,
        "redis_available": REDIS_AVAILABLE,
    })
    for k, v in headers.items():
        resp.headers[k] = v
    return resp


@app.route("/stats")
def stats():
    if not REDIS_AVAILABLE:
        return jsonify({"redis": "unavailable", "note": "fail-open mode active"})

    # Show current window counters for all keys
    now = int(time.time())
    keys_info = {}
    for key_name, (limit, window) in [("anonymous", (DEFAULT_LIMIT, DEFAULT_WINDOW))] + list(KEY_LIMITS.items()):
        window_key = now // window
        full_key = f"ratelimit:{key_name}:{window_key}"
        try:
            count = r.get(full_key)
            ttl = r.ttl(full_key)
            keys_info[key_name] = {
                "count": int(count) if count else 0,
                "limit": limit,
                "window_seconds": window,
                "ttl_remaining": ttl,
            }
        except Exception:
            keys_info[key_name] = {"error": "lookup_failed"}

    return jsonify({"keys": keys_info, "server": SERVER_ID})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
