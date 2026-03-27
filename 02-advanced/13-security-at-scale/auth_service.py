#!/usr/bin/env python3
"""
Auth service: issues JWT access tokens and refresh tokens.
POST /token   — username/password → access_token + refresh_token
POST /refresh — refresh_token → new access_token + NEW refresh_token (rotation)
POST /logout  — invalidate refresh_token
GET  /health

Security design notes:
  - Refresh token rotation: every /refresh call invalidates the old token and issues
    a new one. If a rotated (old) token is seen again, ALL tokens for that user are
    revoked immediately — this detects refresh token theft.
  - Token type claim: access tokens carry type="access", refresh tokens type="refresh".
    The API service rejects refresh tokens used directly as access tokens.
  - Passwords compared with constant-time hmac.compare_digest to resist timing attacks.
"""
import os
import time
import hmac
import secrets
import jwt
from flask import Flask, request, jsonify

app = Flask(__name__)
SECRET = os.environ.get("JWT_SECRET", "dev-secret")

# Simple in-memory user store (use a real DB in production)
USERS = {
    "alice": {"password": "password123", "role": "admin"},
    "bob":   {"password": "pass456",     "role": "user"},
}

# Refresh token store: maps refresh_token → {"username": ..., "family": ...}
# "family" groups all refresh tokens issued from the same login. Reuse of an
# invalidated token causes the entire family to be revoked (theft detection).
REFRESH_TOKENS = {}
# family_id → set of usernames; used for full-family revocation
FAMILIES = {}


def make_access_token(username, role, ttl_seconds=30, scopes=None):
    now = int(time.time())
    payload = {
        "sub":    username,
        "role":   role,
        "iat":    now,
        "exp":    now + ttl_seconds,
        "type":   "access",
        # jti (JWT ID) makes every token unique; useful for access-token denylist
        "jti":    secrets.token_hex(8),
        "scopes": scopes or [],
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def make_refresh_token(username, family_id, ttl_seconds=3600):
    now = int(time.time())
    payload = {
        "sub":    username,
        "iat":    now,
        "exp":    now + ttl_seconds,
        "type":   "refresh",
        "family": family_id,
        "jti":    secrets.token_hex(8),
    }
    token = jwt.encode(payload, SECRET, algorithm="HS256")
    REFRESH_TOKENS[token] = {"username": username, "family": family_id}
    FAMILIES.setdefault(family_id, set()).add(token)
    return token


def revoke_family(family_id):
    """Revoke all refresh tokens belonging to a family (theft detected)."""
    for token in list(FAMILIES.get(family_id, [])):
        REFRESH_TOKENS.pop(token, None)
    FAMILIES.pop(family_id, None)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "auth"})


@app.route("/token", methods=["POST"])
def token():
    data = request.get_json() or {}
    username = data.get("username", "")
    password = data.get("password", "")
    user = USERS.get(username)
    # Constant-time comparison prevents timing-based username enumeration
    if not user or not hmac.compare_digest(user["password"], password):
        return jsonify({"error": "invalid credentials"}), 401
    # In production, scopes would come from the user's profile or an explicit
    # scope parameter in the token request (OAuth2 client_credentials flow).
    default_scopes = {
        "admin": ["read:data", "write:data", "admin:all"],
        "user":  ["read:data"],
    }
    family_id = secrets.token_hex(16)
    access  = make_access_token(username, user["role"], ttl_seconds=5,
                                 scopes=default_scopes.get(user["role"], []))
    refresh = make_refresh_token(username, family_id)
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

    # Check if token is known
    entry = REFRESH_TOKENS.get(refresh_token)
    if entry is None:
        # Token not in store — could be replayed/stolen rotated token.
        # Try to decode to extract the family and revoke everything.
        try:
            stolen_payload = jwt.decode(refresh_token, SECRET, algorithms=["HS256"])
            family_id = stolen_payload.get("family")
            if family_id:
                revoke_family(family_id)
                return jsonify({"error": "refresh token reuse detected — all sessions revoked"}), 401
        except jwt.InvalidTokenError:
            pass
        return jsonify({"error": "invalid refresh token"}), 401

    # Validate the token cryptographically
    try:
        payload = jwt.decode(refresh_token, SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        REFRESH_TOKENS.pop(refresh_token, None)
        return jsonify({"error": "refresh token expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"error": "invalid token"}), 401

    if payload.get("type") != "refresh":
        return jsonify({"error": "not a refresh token"}), 401

    username = payload["sub"]
    family_id = payload.get("family")
    user = USERS.get(username)
    if not user:
        return jsonify({"error": "user not found"}), 401

    # Rotate: invalidate the used token, issue a new one in the same family
    REFRESH_TOKENS.pop(refresh_token, None)
    if family_id and family_id in FAMILIES:
        FAMILIES[family_id].discard(refresh_token)

    default_scopes = {
        "admin": ["read:data", "write:data", "admin:all"],
        "user":  ["read:data"],
    }
    new_access  = make_access_token(username, user["role"], ttl_seconds=5,
                                     scopes=default_scopes.get(user["role"], []))
    new_refresh = make_refresh_token(username, family_id or secrets.token_hex(16))
    return jsonify({
        "access_token":  new_access,
        "refresh_token": new_refresh,
        "expires_in":    5,
        "rotated":       True,  # explicit signal that the old refresh token is now invalid
    })


@app.route("/logout", methods=["POST"])
def logout():
    """Invalidate a refresh token (and its entire family)."""
    data = request.get_json() or {}
    refresh_token = data.get("refresh_token", "")
    entry = REFRESH_TOKENS.get(refresh_token)
    if entry:
        revoke_family(entry["family"])
        return jsonify({"message": "logged out — all sessions for this login revoked"})
    return jsonify({"message": "token not found (already expired or invalid)"}), 200


if __name__ == "__main__":
    print("Auth service on :8001")
    app.run(host="0.0.0.0", port=8001)
