#!/usr/bin/env python3
"""
REST API — Flask in-memory data store.
10 users, 5 posts each.
Routes: GET /health, GET /users, GET /users/<id>, GET /users/<id>/posts
"""

from flask import Flask, jsonify
import os

app = Flask(__name__)

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
    return jsonify(list(USERS.values()))


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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8001))
    app.run(host="0.0.0.0", port=port)
