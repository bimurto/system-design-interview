#!/usr/bin/env python3
"""
REST API — Flask in-memory data store.
10 users, 5 posts each.

Routes:
  GET  /health
  GET  /users                        — list all users
  GET  /users?include=posts          — eager-load posts (solves N+1 server-side)
  GET  /users/<id>                   — single user (full object, demonstrates over-fetching)
  GET  /users/<id>/posts             — posts for a user (naive N+1 pattern)
  POST /payments                     — demo endpoint with Idempotency-Key support
"""

import json
import os
import time

import redis
from flask import Flask, jsonify, request

app = Flask(__name__)

# ── Redis for idempotency store ────────────────────────────────────────────────

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
_redis = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)

IDEMPOTENCY_TTL = 86400  # 24 hours

# ── In-memory data ─────────────────────────────────────────────────────────────

USERS = {
    str(i): {
        "id": str(i),
        "name": f"User {i}",
        "email": f"user{i}@example.com",
        "age": 20 + i,
        "city": ["New York", "London", "Tokyo", "Berlin", "Sydney"][i % 5],
        "joined": f"2022-0{(i % 9) + 1}-01",
        "bio": f"Bio for user {i}. Loves distributed systems.",
        "website": f"https://user{i}.example.com",
    }
    for i in range(1, 11)
}

POSTS = {
    str(i): [
        {
            "id": f"post-{i}-{j}",
            "user_id": str(i),
            "title": f"Post {j} by User {i}",
            "body": f"This is the body of post {j} written by user {i}. " * 3,
            "created_at": f"2023-0{j}-15",
            "likes": i * j * 3,
            "tags": ["engineering", "systems", "design"][:j % 3 + 1],
        }
        for j in range(1, 6)
    ]
    for i in range(1, 11)
}


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/users")
def get_users():
    include = request.args.get("include", "")
    users = list(USERS.values())
    if "posts" in include:
        # Eager-load: one pass, no extra HTTP round trips (server-side N+1 fix)
        result = []
        for user in users:
            u = dict(user)  # copy so we don't mutate the in-memory store
            u["posts"] = POSTS.get(u["id"], [])
            result.append(u)
        return jsonify(result)
    return jsonify(users)


@app.route("/users/<user_id>")
def get_user(user_id):
    user = USERS.get(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify(user)


@app.route("/users/<user_id>/posts")
def get_user_posts(user_id):
    if user_id not in USERS:
        return jsonify({"error": "User not found"}), 404
    return jsonify(POSTS.get(user_id, []))


@app.route("/payments", methods=["POST"])
def create_payment():
    """
    Demonstrates idempotency key pattern.

    Client sends:
        POST /payments
        Idempotency-Key: <uuid>
        { "amount": 100, "to": "user_456" }

    Server behaviour:
      - First call: process payment, cache result under key (24h TTL)
      - Subsequent calls with same key: return cached result, skip processing
      - Without key: process every time (dangerous — double-charge risk)

    Redis key: idempotency:<key>
    """
    idempotency_key = request.headers.get("Idempotency-Key")
    body = request.get_json(silent=True) or {}
    amount = body.get("amount", 0)
    recipient = body.get("to", "unknown")

    if idempotency_key:
        redis_key = f"idempotency:{idempotency_key}"
        cached = _redis.get(redis_key)
        if cached:
            cached_response = json.loads(cached)
            cached_response["idempotency_status"] = "DUPLICATE_RETURNED_FROM_CACHE"
            return jsonify(cached_response), 200

    # Simulate payment processing (in real life: charge card, write DB)
    time.sleep(0.05)  # 50ms fake processing
    payment_id = f"pay_{int(time.time() * 1000)}"
    response = {
        "payment_id": payment_id,
        "amount": amount,
        "to": recipient,
        "status": "completed",
        "idempotency_status": "NEW_PAYMENT_PROCESSED",
    }

    if idempotency_key:
        redis_key = f"idempotency:{idempotency_key}"
        _redis.setex(redis_key, IDEMPOTENCY_TTL, json.dumps(response))

    return jsonify(response), 201


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8001))
    app.run(host="0.0.0.0", port=port)
