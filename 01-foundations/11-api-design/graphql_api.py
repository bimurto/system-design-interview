#!/usr/bin/env python3
"""
GraphQL API — Strawberry + Flask.

User type with id, name, email, age, city and nested posts field.
Queries: users() and user(id: String)

Intentional design for demo purposes:
  - The `posts` resolver on User fires a separate data lookup per user.
  - When fetching N users with posts, this triggers N resolver calls.
  - This is the GraphQL-layer N+1 problem (HTTP layer is solved, DB layer is not).
  - The fix is the DataLoader pattern — documented in the resolver comment.

A resolver call counter is exposed at GET /resolver-stats so experiment.py
can surface the N resolver calls that occurred during a users+posts query.
"""

import os
from typing import List, Optional

import strawberry
from flask import Flask, jsonify
from strawberry.flask import GraphQLView

# ── Resolver call counter (in-memory, reset per request via /resolver-stats) ───

_resolver_stats = {"posts_resolver_calls": 0}


def _reset_stats():
    _resolver_stats["posts_resolver_calls"] = 0


# ── In-memory data (same shape as REST API) ────────────────────────────────────

_USERS_DATA = {
    str(i): {
        "id": str(i),
        "name": f"User {i}",
        "email": f"user{i}@example.com",
        "age": 20 + i,
        "city": ["New York", "London", "Tokyo", "Berlin", "Sydney"][i % 5],
    }
    for i in range(1, 11)
}

_POSTS_DATA = {
    str(i): [
        {
            "id": f"post-{i}-{j}",
            "user_id": str(i),
            "title": f"Post {j} by User {i}",
            "body": f"This is the body of post {j} written by user {i}. " * 3,
            "created_at": f"2023-0{j}-15",
            "likes": i * j * 3,
        }
        for j in range(1, 6)
    ]
    for i in range(1, 11)
}


def _fetch_posts_for_user(user_id: str) -> list:
    """
    Simulates a per-user DB query.

    In a real app this would be:
        SELECT * FROM posts WHERE user_id = %s

    When called inside a User resolver that iterates N users, this fires N
    separate queries — the GraphQL N+1 problem at the database layer.

    Fix: DataLoader batches all user_ids collected in the same event-loop tick
    into a single:
        SELECT * FROM posts WHERE user_id IN (1, 2, 3, ..., N)
    """
    _resolver_stats["posts_resolver_calls"] += 1
    return _POSTS_DATA.get(user_id, [])


# ── Strawberry types ───────────────────────────────────────────────────────────

@strawberry.type
class Post:
    id: str
    user_id: str
    title: str
    body: str
    created_at: str
    likes: int


@strawberry.type
class User:
    id: str
    name: str
    email: str
    age: int
    city: str

    @strawberry.field
    def posts(self) -> List[Post]:
        # Each call here is a separate "DB query" — N+1 at the resolver layer.
        # With DataLoader this entire method body would be replaced by:
        #   return await posts_loader.load(self.id)
        # which batches all concurrent loads into one bulk fetch.
        raw = _fetch_posts_for_user(self.id)
        return [Post(**p) for p in raw]


def _make_user(data: dict) -> User:
    return User(
        id=data["id"],
        name=data["name"],
        email=data["email"],
        age=data["age"],
        city=data["city"],
    )


# ── Schema ─────────────────────────────────────────────────────────────────────

@strawberry.type
class Query:
    @strawberry.field
    def users(self) -> List[User]:
        return [_make_user(u) for u in _USERS_DATA.values()]

    @strawberry.field
    def user(self, id: str) -> Optional[User]:
        data = _USERS_DATA.get(id)
        return _make_user(data) if data else None


schema = strawberry.Schema(query=Query)

# ── Flask app ──────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.add_url_rule(
    "/graphql",
    view_func=GraphQLView.as_view("graphql_view", schema=schema),
)


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/resolver-stats")
def resolver_stats():
    """Return and reset resolver call counters for experiment.py to inspect."""
    stats = dict(_resolver_stats)
    _reset_stats()
    return jsonify(stats), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8002))
    app.run(host="0.0.0.0", port=port)
