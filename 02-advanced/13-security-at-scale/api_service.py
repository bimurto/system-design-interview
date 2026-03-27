#!/usr/bin/env python3
"""
API service: validates JWTs, enforces role-based access control.
GET  /health
GET  /protected      — requires valid JWT (any role)
GET  /admin-only     — requires JWT with role=admin
"""
import os
import jwt
from flask import Flask, request, jsonify

app = Flask(__name__)
SECRET = os.environ.get("JWT_SECRET", "dev-secret")


def require_jwt(required_role=None):
    """Validate Bearer token and optionally enforce role."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None, (jsonify({"error": "missing Authorization header"}), 401)
    token = auth_header.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return None, (jsonify({"error": "token expired"}), 401)
    except jwt.InvalidTokenError as e:
        return None, (jsonify({"error": f"invalid token: {e}"}), 401)
    if payload.get("type") != "access":
        return None, (jsonify({"error": "not an access token"}), 401)
    if required_role and payload.get("role") != required_role:
        return None, (jsonify({"error": "insufficient permissions"}), 403)
    return payload, None


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "api"})


@app.route("/protected")
def protected():
    payload, err = require_jwt()
    if err:
        return err
    return jsonify({
        "message": f"Hello, {payload['sub']}! You have role: {payload['role']}",
        "user": payload["sub"],
        "role": payload["role"],
    })


@app.route("/admin-only")
def admin_only():
    payload, err = require_jwt(required_role="admin")
    if err:
        return err
    return jsonify({
        "message": f"Admin area — welcome, {payload['sub']}",
        "secret":  "TOP SECRET DATA",
    })


if __name__ == "__main__":
    print("API service on :8002")
    app.run(host="0.0.0.0", port=8002)
