#!/usr/bin/env python3
"""
Auth service: issues JWT access tokens and refresh tokens.
POST /token  — username/password → access_token + refresh_token
POST /refresh — refresh_token → new access_token
GET  /health
"""
import os
import time
import jwt
from flask import Flask, request, jsonify

app = Flask(__name__)
SECRET = os.environ.get("JWT_SECRET", "dev-secret")

# Simple in-memory user store (use a real DB in production)
USERS = {
    "alice": {"password": "password123", "role": "admin"},
    "bob":   {"password": "pass456",     "role": "user"},
}

# Refresh token store (maps refresh_token → username)
REFRESH_TOKENS = {}


def make_access_token(username, role, ttl_seconds=30):
    payload = {
        "sub": username,
        "role": role,
        "iat": int(time.time()),
        "exp": int(time.time()) + ttl_seconds,
        "type": "access",
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def make_refresh_token(username, ttl_seconds=3600):
    payload = {
        "sub": username,
        "iat": int(time.time()),
        "exp": int(time.time()) + ttl_seconds,
        "type": "refresh",
    }
    token = jwt.encode(payload, SECRET, algorithm="HS256")
    REFRESH_TOKENS[token] = username
    return token


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "auth"})


@app.route("/token", methods=["POST"])
def token():
    data = request.get_json() or {}
    username = data.get("username", "")
    password = data.get("password", "")
    user = USERS.get(username)
    if not user or user["password"] != password:
        return jsonify({"error": "invalid credentials"}), 401
    access  = make_access_token(username, user["role"], ttl_seconds=5)
    refresh = make_refresh_token(username)
    return jsonify({
        "access_token":  access,
        "refresh_token": refresh,
        "token_type":    "Bearer",
        "expires_in":    5,
    })


@app.route("/refresh", methods=["POST"])
def refresh():
    data = request.get_json() or {}
    refresh_token = data.get("refresh_token", "")
    if refresh_token not in REFRESH_TOKENS:
        return jsonify({"error": "invalid refresh token"}), 401
    try:
        payload = jwt.decode(refresh_token, SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        del REFRESH_TOKENS[refresh_token]
        return jsonify({"error": "refresh token expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "invalid token"}), 401
    username = payload["sub"]
    user = USERS.get(username)
    if not user:
        return jsonify({"error": "user not found"}), 401
    new_access = make_access_token(username, user["role"], ttl_seconds=5)
    return jsonify({
        "access_token": new_access,
        "expires_in":   5,
    })


if __name__ == "__main__":
    print("Auth service on :8001")
    app.run(host="0.0.0.0", port=8001)
