#!/usr/bin/env python3
"""
API service: validates JWTs, enforces role-based access control.
GET  /health
GET  /protected      — requires valid JWT (any role)
GET  /admin-only     — requires JWT with role=admin
GET  /scoped         — requires JWT with scope containing "read:data"

Security design notes:
  - Algorithm is pinned to ["HS256"] — the token's "alg" header is IGNORED.
    This prevents the "none" algorithm attack and algorithm confusion attacks.
  - Token type is checked: refresh tokens are rejected even if the signature
    is valid — prevents refresh token misuse as access tokens.
  - Scope check demonstrates fine-grained authorisation beyond coarse roles.
    Roles ("admin", "user") are coarse; scopes ("read:data", "write:data")
    are fine-grained and per-operation. In practice, both are used together.
"""
import os
import jwt
from flask import Flask, request, jsonify

app = Flask(__name__)
SECRET = os.environ.get("JWT_SECRET", "dev-secret")


def require_jwt(required_role=None, required_scope=None):
    """Validate Bearer token and optionally enforce role or scope.

    Role check: exact match (coarse RBAC).
    Scope check: token must contain the required scope string (fine-grained ABAC).
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None, (jsonify({"error": "missing Authorization header"}), 401)
    token = auth_header.split(" ", 1)[1]
    try:
        # algorithms= is a whitelist — PyJWT ignores the token's "alg" header
        # and uses this list. Prevents algorithm confusion (e.g., RS256→HS256).
        payload = jwt.decode(token, SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return None, (jsonify({"error": "token expired"}), 401)
    except jwt.InvalidTokenError as e:
        return None, (jsonify({"error": f"invalid token: {e}"}), 401)

    # Reject refresh tokens used as access tokens (type confusion attack)
    if payload.get("type") != "access":
        return None, (jsonify({"error": "not an access token — refresh tokens cannot be used here"}), 401)

    if required_role and payload.get("role") != required_role:
        return None, (jsonify({"error": f"insufficient permissions — role '{required_role}' required"}), 403)

    if required_scope:
        token_scopes = payload.get("scopes", [])
        if required_scope not in token_scopes:
            return None, (jsonify({"error": f"insufficient scope — '{required_scope}' required"}), 403)

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
        "user":    payload["sub"],
        "role":    payload["role"],
        "jti":     payload.get("jti", "n/a"),
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


@app.route("/scoped")
def scoped():
    """Demonstrates scope-based (fine-grained) authorisation.

    Even admin users need the explicit scope in their token to access this endpoint.
    This is the OAuth2 scopes model — identity + role is not enough.
    """
    payload, err = require_jwt(required_scope="read:data")
    if err:
        return err
    return jsonify({
        "message": f"Scoped resource accessed by {payload['sub']}",
        "data":    "sensitive-dataset-42",
        "granted_scopes": payload.get("scopes", []),
    })


if __name__ == "__main__":
    print("API service on :8002")
    app.run(host="0.0.0.0", port=8002)
