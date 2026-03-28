#!/usr/bin/env python3
"""
api_server.py — Flask API with Redis-backed distributed rate limiting.

Endpoints:
  GET /health                → 200 OK
  GET /api/data              → 200 OK or 429 Too Many Requests
  GET /api/data?key=<api_key>→ rate limited by api_key (key-level limits)
  GET /stats                 → current rate limit counters for known keys

Rate limiting uses a Lua script for atomic check-and-increment.

Key behaviors demonstrated:
  - Fixed-window counter with atomic Lua script (no TOCTOU race)
  - Fail-open: Redis unavailable → requests pass through with X-Redis-Available: False
  - Retry-After and X-RateLimit-Reset headers on 429 responses
  - Automatic Redis reconnection: once Redis recovers, rate limiting resumes
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

# Lua script for atomic fixed-window rate limiting.
#
# Why Lua instead of MULTI/EXEC?
#   Redis transactions (MULTI/EXEC) do not support conditional branching —
#   you cannot check the counter and conditionally EXPIRE within a transaction.
#   Lua scripts execute atomically on the server side: no interleaving with
#   other clients' commands. The single INCR + conditional EXPIRE is safe
#   because if two clients call INCR simultaneously, Redis serialises them —
#   exactly one sees count==1 and sets the EXPIRE.
RATE_LIMIT_LUA = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

local window_key = math.floor(now / window)
local full_key = key .. ":" .. window_key

local count = redis.call("INCR", full_key)
if count == 1 then
    -- Only the very first INCR in this window sets the TTL.
    -- Subsequent INCRs skip this branch, preserving the original expiry.
    redis.call("EXPIRE", full_key, window)
end
if count > limit then
    return {0, count, limit, window_key, window}  -- denied
else
    return {1, count, limit, window_key, window}  -- allowed
end
"""

# ── Redis connection management ────────────────────────────────────────────────
# REDIS_AVAILABLE tracks live reachability, not just startup state.
# check_rate_limit() attempts to reconnect on each request when unavailable,
# so the server automatically recovers when Redis comes back (fail-open is
# not permanent — it ends as soon as Redis is reachable again).

_redis_client = None
_lua_script = None
REDIS_AVAILABLE = False

# Sentinel used in headers during fail-open: indicates rate limiting is
# suspended, not that 0 requests remain. Avoids misleading clients.
FAIL_OPEN_REMAINING = "unlimited"


def _connect_redis():
    """Attempt to (re)connect to Redis. Returns True on success."""
    global _redis_client, _lua_script, REDIS_AVAILABLE
    try:
        client = redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        client.ping()
        _redis_client = client
        _lua_script = client.register_script(RATE_LIMIT_LUA)
        REDIS_AVAILABLE = True
        return True
    except Exception:
        _redis_client = None
        _lua_script = None
        REDIS_AVAILABLE = False
        return False


# Initial connection attempt at startup
_connect_redis()


def check_rate_limit(api_key: str) -> tuple[bool, int | None, int | None, int]:
    """
    Returns (allowed, current_count, limit, window_reset_ts).

    current_count and limit are None when Redis is unavailable (fail-open).
    window_reset_ts is the Unix timestamp when the current window ends —
    used to populate Retry-After and X-RateLimit-Reset headers.

    If Redis is unavailable: fail-open (allowed=True, count=None, limit=None).
    Automatically retries Redis connection on each call while unavailable.
    """
    global REDIS_AVAILABLE

    limit, window = KEY_LIMITS.get(api_key, (DEFAULT_LIMIT, DEFAULT_WINDOW))
    now = int(time.time())
    window_reset_ts = (now // window + 1) * window  # end of current fixed window

    if not REDIS_AVAILABLE:
        # Attempt reconnect — recovers automatically when Redis comes back
        if not _connect_redis():
            return True, None, None, window_reset_ts  # still unavailable: fail-open

    redis_key = f"ratelimit:{api_key}"

    try:
        result = _lua_script(keys=[redis_key], args=[limit, window, now])
        allowed, count, lim, window_id, win_size = result
        reset_ts = (int(window_id) + 1) * int(win_size)
        return bool(int(allowed)), int(count), int(lim), reset_ts
    except Exception:
        # Redis became unavailable mid-request: fail-open and mark for reconnect
        REDIS_AVAILABLE = False
        return True, None, None, window_reset_ts


@app.route("/health")
def health():
    # Report live Redis state (not cached from startup)
    return jsonify({
        "status": "ok",
        "server": SERVER_ID,
        "redis": "available" if REDIS_AVAILABLE else "unavailable",
    })


@app.route("/api/data")
def api_data():
    api_key = request.args.get("key", "anonymous")
    allowed, count, limit, reset_ts = check_rate_limit(api_key)
    now = int(time.time())
    retry_after = max(0, reset_ts - now)

    # During fail-open, limit and count are None.
    # Use sentinel strings so clients can detect the fail-open state
    # rather than seeing misleading numeric values (e.g., "0 remaining").
    if limit is None:
        limit_header     = "unavailable"
        remaining_header = FAIL_OPEN_REMAINING
    else:
        limit_header     = str(limit)
        remaining_header = str(max(0, limit - count))

    headers = {
        "X-RateLimit-Limit":     limit_header,
        "X-RateLimit-Remaining": remaining_header,
        "X-RateLimit-Reset":     str(reset_ts),   # Unix timestamp: window end
        "X-Server":              SERVER_ID,
        "X-Redis-Available":     str(REDIS_AVAILABLE),
    }

    if not allowed:
        headers["Retry-After"] = str(retry_after)  # RFC 7231: seconds to wait
        resp = jsonify({
            "error": "rate_limit_exceeded",
            "message": (
                f"Too many requests. Limit: {limit}/window. "
                f"Retry after {retry_after}s."
            ),
            "current_count": count,
            "limit": limit,
            "retry_after_seconds": retry_after,
            "window_reset_ts": reset_ts,
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
    """Return current window counters for all known keys — useful for debugging."""
    if not REDIS_AVAILABLE:
        return jsonify({
            "redis": "unavailable",
            "note": "fail-open mode active — rate limiting suspended",
        })

    now = int(time.time())
    keys_info = {}
    all_keys = [("anonymous", DEFAULT_LIMIT, DEFAULT_WINDOW)] + [
        (k, lim, win) for k, (lim, win) in KEY_LIMITS.items()
    ]
    for key_name, limit, window in all_keys:
        window_id = now // window
        full_key = f"ratelimit:{key_name}:{window_id}"
        try:
            count = _redis_client.get(full_key)
            ttl = _redis_client.ttl(full_key)
            keys_info[key_name] = {
                "count": int(count) if count else 0,
                "limit": limit,
                "window_seconds": window,
                "ttl_remaining": ttl,
                "window_reset_ts": (window_id + 1) * window,
            }
        except Exception:
            keys_info[key_name] = {"error": "lookup_failed"}

    # Also surface any other active ratelimit:* keys (e.g. experiment keys)
    try:
        all_redis_keys = _redis_client.keys("ratelimit:*")
        known_prefixes = {f"ratelimit:{k}" for k, _, _ in all_keys}
        for rkey in sorted(all_redis_keys):
            # Skip keys already covered by the static list above
            if any(rkey.startswith(p + ":") for p in known_prefixes):
                continue
            # Parse key name from "ratelimit:{name}:{window_id}"
            parts = rkey.split(":", 2)
            if len(parts) == 3:
                dyn_name = parts[1]
                try:
                    count = _redis_client.get(rkey)
                    ttl = _redis_client.ttl(rkey)
                    keys_info[dyn_name] = {
                        "count": int(count) if count else 0,
                        "limit": "(dynamic)",
                        "ttl_remaining": ttl,
                        "redis_key": rkey,
                    }
                except Exception:
                    pass
    except Exception:
        pass

    return jsonify({"keys": keys_info, "server": SERVER_ID, "now": now})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
