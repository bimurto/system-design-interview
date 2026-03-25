#!/usr/bin/env python3
"""
GraphQL API — Strawberry + Flask.
User type with id, name, email and nested posts field.
Queries: users() and user(id: String)
"""

import strawberry
from strawberry.flask import GraphQLView
from flask import Flask
from typing import List, Optional
import os


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
        return [Post(**p) for p in _POSTS_DATA.get(self.id, [])]


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
    from flask import jsonify
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8002))
    app.run(host="0.0.0.0", port=port)
