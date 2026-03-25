#!/usr/bin/env python3
"""
Security at Scale Lab — JWT auth, tampering, expiry, refresh tokens.

What this demonstrates:
  1. Request without token → 401
  2. Login to get JWT → 200
  3. Tamper with JWT payload (change role) → 401 (signature mismatch)
  4. Wait for token to expire (5s TTL) → 401
  5. Use refresh token to get new access token → 200
  6. Role-based access: user cannot access admin endpoint → 403
"""

import os
import time
import base64
import json
import requests

AUTH = f"http://{os.environ.get('AUTH_HOST', 'localhost')}:8001"
API  = f"http://{os.environ.get('API_HOST',  'localhost')}:8002"


def section(title):
    print(f"\n{'=' * 65}")
    print(f"  {title}")
    print("=" * 65)


def call(method, url, label, **kwargs):
    resp = getattr(requests, method)(url, **kwargs)
    status_icon = "OK " if resp.status_code < 400 else "ERR"
    print(f"  [{status_icon}] {resp.status_code} {label}")
    try:
        body = resp.json()
        if "error" in body:
            print(f"        Error: {body['error']}")
        elif "message" in body:
            print(f"        Message: {body['message']}")
    except Exception:
        pass
    return resp


def decode_jwt_payload(token):
    """Decode JWT payload without signature verification (just for display)."""
    try:
        parts = token.split(".")
        payload_b64 = parts[1]
        # Add padding
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return {}


def tamper_jwt(token, new_role):
    """Modify the role field in the JWT payload, keep original signature (will be invalid)."""
    parts = token.split(".")
    payload = decode_jwt_payload(token)
    payload["role"] = new_role
    new_payload = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    # Return tampered token (header.NEW_PAYLOAD.original_signature)
    return f"{parts[0]}.{new_payload}.{parts[2]}"


def main():
    section("SECURITY AT SCALE LAB")
    print("""
  Architecture:
    [Client] → POST /token → [Auth Service :8001] → JWT
    [Client] → GET /protected (Bearer JWT) → [API Service :8002]

  JWT Structure:
    header.payload.signature
    Each part is base64url-encoded.

    Header:  {"alg": "HS256", "typ": "JWT"}
    Payload: {"sub": "alice", "role": "admin", "exp": 1234567890}
    Signature: HMAC-SHA256(base64(header) + "." + base64(payload), secret)
""")

    # ── Phase 1: No token → 401 ────────────────────────────────────
    section("Phase 1: Request Without Token → 401")
    call("get", f"{API}/protected", "GET /protected (no token)")
    call("get", f"{API}/admin-only", "GET /admin-only (no token)")
    print("\n  Without a Bearer token in the Authorization header,")
    print("  the API service returns 401 Unauthorized immediately.")

    # ── Phase 2: Login and get JWT ─────────────────────────────────
    section("Phase 2: Login and Get JWT")
    print("  Logging in as alice (role=admin)...\n")

    r = call("post", f"{AUTH}/token", "POST /token (alice/password123)",
             json={"username": "alice", "password": "password123"})
    tokens = r.json()
    access_token  = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")

    payload = decode_jwt_payload(access_token)
    print(f"""
  JWT received. Decoded payload:
    sub:  {payload.get('sub')}
    role: {payload.get('role')}
    exp:  {payload.get('exp')} (expires in {tokens.get('expires_in')}s)

  Token structure:
    {access_token[:30]}...  (truncated)
    ───────────────────────────────────
    Part 1 (header):    {access_token.split('.')[0]}
    Part 2 (payload):   {access_token.split('.')[1][:40]}...
    Part 3 (signature): {access_token.split('.')[2][:20]}...
""")

    call("get", f"{API}/protected", "GET /protected (valid token)",
         headers={"Authorization": f"Bearer {access_token}"})
    call("get", f"{API}/admin-only", "GET /admin-only (alice has admin role)",
         headers={"Authorization": f"Bearer {access_token}"})

    # ── Phase 3: Tamper with JWT payload ──────────────────────────
    section("Phase 3: Tamper With JWT Payload (change role)")
    print("  Login as bob (role=user), then try to escalate to admin:\n")

    r_bob = call("post", f"{AUTH}/token", "POST /token (bob/pass456)",
                 json={"username": "bob", "password": "pass456"})
    bob_token = r_bob.json().get("access_token", "")
    bob_payload = decode_jwt_payload(bob_token)
    print(f"  Bob's token payload: sub={bob_payload.get('sub')}, role={bob_payload.get('role')}")

    call("get", f"{API}/admin-only", "GET /admin-only (bob, role=user) → 403",
         headers={"Authorization": f"Bearer {bob_token}"})

    # Tamper: change role from "user" to "admin"
    tampered = tamper_jwt(bob_token, "admin")
    tampered_payload = decode_jwt_payload(tampered)
    print(f"\n  Tampered token payload: role={tampered_payload.get('role')} (changed to admin)")
    print("  Sending tampered token (original signature, modified payload)...\n")

    call("get", f"{API}/admin-only", "GET /admin-only (tampered token) → 401",
         headers={"Authorization": f"Bearer {tampered}"})

    print("""
  Why tampering fails:
    The JWT signature is HMAC-SHA256(header + "." + payload, secret)
    Changing the payload invalidates the signature.
    The API service re-computes the expected signature and rejects the mismatch.
    → JWT integrity guarantee: payload cannot be changed without knowing the secret.
""")

    # ── Phase 4: Token expiry ─────────────────────────────────────
    section("Phase 4: Token Expiry (5s TTL)")
    print("  Getting fresh token (TTL=5s), then waiting 6s...\n")

    r_fresh = call("post", f"{AUTH}/token", "POST /token (fresh alice token)",
                   json={"username": "alice", "password": "password123"})
    fresh_token = r_fresh.json().get("access_token", "")

    call("get", f"{API}/protected", "GET /protected immediately → 200",
         headers={"Authorization": f"Bearer {fresh_token}"})

    print("  Waiting 6 seconds for token to expire...")
    time.sleep(6)

    call("get", f"{API}/protected", "GET /protected after expiry → 401",
         headers={"Authorization": f"Bearer {fresh_token}"})

    print("""
  JWT expiry trade-off:
    Short TTL (5-15 min)  → Frequent re-auth needed, better security
    Long TTL (24h+)       → Convenient, but can't be revoked if stolen
    Solution: short access token (15min) + long refresh token (30 days)
    → Compromise: access token expires quickly, refresh token re-issues it
""")

    # ── Phase 5: Refresh token flow ───────────────────────────────
    section("Phase 5: Refresh Token → New Access Token")
    print("  Using the refresh token from Phase 2 to get a new access token:\n")

    r_refresh = call("post", f"{AUTH}/refresh", "POST /refresh (with refresh_token)",
                     json={"refresh_token": refresh_token})
    if r_refresh.status_code == 200:
        new_access = r_refresh.json().get("access_token", "")
        call("get", f"{API}/protected", "GET /protected (new access token) → 200",
             headers={"Authorization": f"Bearer {new_access}"})
    else:
        print("  Refresh token may have expired (if lab ran slowly).")

    # ── Phase 6: Summary ──────────────────────────────────────────
    section("Phase 6: Security Summary")
    print("""
  JWT Security Properties:
  ┌──────────────────────────────────────────────────────────────┐
  │ Property          │ HS256              │ RS256                │
  │───────────────────│────────────────────│──────────────────────│
  │ Algorithm         │ HMAC-SHA256        │ RSA SHA-256          │
  │ Key type          │ Shared secret      │ Public/private key   │
  │ Verification      │ Same secret        │ Public key only      │
  │ Use case          │ Single service     │ Multiple services    │
  │ Key rotation      │ Requires redeploy  │ Rotate private only  │
  └──────────────────────────────────────────────────────────────┘

  JWT vs Sessions vs API Keys:
    JWT        → stateless, scalable, self-contained, short-lived
    Sessions   → server-side state, revocable, requires sticky sessions
    API Keys   → long-lived, simple for M2M, must be rotated periodically

  Critical: JWT cannot be revoked before expiry without a denylist.
  Keep access tokens SHORT (5-15 min). Use refresh tokens for sessions.

  OWASP Top 10 (relevant at scale):
    A01 Broken Access Control    → check role on EVERY endpoint
    A02 Cryptographic Failures   → use HS256 minimum; never none algorithm
    A07 Identification/AuthN     → brute-force protection, rate limit /token
    A10 SSRF                     → validate URLs in user-supplied content

  Next: ../13-service-discovery-coordination/
""")


if __name__ == "__main__":
    main()
