#!/usr/bin/env python3
"""
API service: validates JWTs, enforces role-based access control.
GET  /health
GET  /protected      — requires valid JWT (any role)
GET  /admin-only     — requires JWT with role=admin
GET  /scoped         — requires JWT with scope containing "read:data"
POST /data           — requires JWT with scope containing "write:data"
POST /revoke-jti     — add a JTI to the in-memory denylist (immediate revocation)
GET  /denylist-check — report the current JTI denylist (for demo/introspection)

Security design notes:
  - Algorithm is pinned to ["HS256"] — the token's "alg" header is IGNORED.
    This prevents the "none" algorithm attack and algorithm confusion attacks.
  - Token type is checked: refresh tokens are rejected even if the signature
    is valid — prevents refresh token misuse as access tokens.
  - Scope check demonstrates fine-grained authorisation beyond coarse roles.
    Roles ("admin", "user") are coarse; scopes ("read:data", "write:data")
    are fine-grained and per-operation. In practice, both are used together.
  - JTI denylist: in-memory set checked on every request.  A revoked JTI is
    rejected immediately, before the token's exp claim is reached.  This
    sacrifices the "stateless" property in exchange for instant revocation of
    individual tokens — useful when a specific access token is known to be
    compromised.  In production, use Redis (shared across replicas).
"""
import os
import jwt
from flask import Flask, request, jsonify

# ── JTI denylist ────────────────────────────────────────────────────────────
# In-memory set of revoked JWT IDs.  In production this would be a shared
# Redis SET (SADD jti:<jti> EX <remaining_ttl>) so all API service replicas
# share the same denylist.  Here a Python set suffices for demonstration.
JTI_DENYLIST: set = set()

app = Flask(__name__)
SECRET = os.environ.get("JWT_SECRET", "dev-secret")


def require_jwt(required_role=None, required_scope=None):
    """Validate Bearer token and optionally enforce role or scope.

    Role check: exact match (coarse RBAC).
    Scope check: token must contain the required scope string (fine-grained ABAC).
    JTI check:  token's jti must NOT be in the in-memory denylist.
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

    # JTI denylist check — catches explicitly revoked (but not yet expired) tokens
    jti = payload.get("jti")
    if jti and jti in JTI_DENYLIST:
        return None, (jsonify({"error": "token has been revoked (jti in denylist)"}), 401)

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


@app.route("/data", methods=["POST"])
def write_data():
    """Mutating endpoint — requires the write:data scope.

    Only tokens that explicitly carry 'write:data' are accepted.
    alice (admin) receives ['read:data', 'write:data', 'admin:all'] on login.
    bob   (user)  receives ['read:data'] only — he cannot POST here.

    This mirrors OAuth2 best practice: narrowing what a token can DO independent
    of who the user IS.  A CI/CD pipeline token might have only 'read:data' even
    if the human operator has admin rights.
    """
    payload, err = require_jwt(required_scope="write:data")
    if err:
        return err
    body = request.get_json() or {}
    return jsonify({
        "message": f"Data written by {payload['sub']}",
        "written": body,
        "granted_scopes": payload.get("scopes", []),
    }), 201


# ── JTI denylist management endpoints ───────────────────────────────────────

@app.route("/revoke-jti", methods=["POST"])
def revoke_jti():
    """Add a JTI to the in-memory denylist.

    In production this endpoint would be authenticated (e.g. admin-only or
    service-to-service mTLS).  For this lab it is open so the experiment script
    can demonstrate immediate access-token revocation without waiting for exp.

    Workflow:
      1. Auth service (or an admin tool) learns a specific access token is
         compromised (e.g. logged in a proxy, seen in a crash dump).
      2. It POSTs {"jti": "<value>"} here.
      3. All subsequent requests bearing that token are rejected with 401,
         even if the token's exp is still in the future.
    """
    data = request.get_json() or {}
    jti = data.get("jti", "").strip()
    if not jti:
        return jsonify({"error": "jti field required"}), 400
    JTI_DENYLIST.add(jti)
    return jsonify({"message": f"jti '{jti}' added to denylist", "denylist_size": len(JTI_DENYLIST)})


@app.route("/denylist-check")
def denylist_check():
    """Return the current JTI denylist (for demo / introspection only).

    A production service would NOT expose this — it leaks information about
    revoked tokens.  Included here purely so the experiment output is readable.
    """
    return jsonify({
        "denylist": sorted(JTI_DENYLIST),
        "size": len(JTI_DENYLIST),
    })


if __name__ == "__main__":
    print("API service on :8002")
    app.run(host="0.0.0.0", port=8002)
