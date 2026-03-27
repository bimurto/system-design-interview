#!/usr/bin/env python3
"""
Security at Scale Lab — JWT auth, tampering, expiry, refresh token rotation,
scope-based authz, token type confusion, and theft detection.

What this demonstrates:
  1. Request without token → 401
  2. Login to get JWT → decode and display all claims including scopes and jti
  3. Tamper with JWT payload (change role) → 401 (signature mismatch)
  4. Token type confusion: use refresh token as access token → 401
  5. Wait for token to expire (5s TTL) → 401
  6. Refresh token rotation: each use issues a NEW refresh token;
     replaying the old one triggers theft detection → all sessions revoked
  7. Scope-based authorisation: user with read:data can access /scoped;
     bob (user role) cannot access /admin-only
  8. Logout: invalidate a token family; subsequent refresh → 401
  9. Summary of all JWT security properties demonstrated
"""

import os
import time
import base64
import json
import requests

AUTH = f"http://{os.environ.get('AUTH_HOST', 'localhost')}:8001"
API  = f"http://{os.environ.get('API_HOST',  'localhost')}:8002"


def section(title):
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print("=" * 70)


def call(method, url, label, *, show_body=False, **kwargs):
    resp = getattr(requests, method)(url, **kwargs)
    status_icon = "OK " if resp.status_code < 400 else "ERR"
    print(f"  [{status_icon}] {resp.status_code}  {label}")
    try:
        body = resp.json()
        if show_body:
            print(f"        → {json.dumps(body, indent=8)}")
        elif "error" in body:
            print(f"        Error: {body['error']}")
        elif "message" in body:
            print(f"        Message: {body['message']}")
    except Exception:
        pass
    return resp


def decode_jwt_payload(token):
    """Decode JWT payload without signature verification (for display only)."""
    try:
        parts = token.split(".")
        padding = "=" * (4 - len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    except Exception:
        return {}


def tamper_jwt(token, new_role):
    """Modify the role field in the JWT payload; keep the original signature (now invalid)."""
    parts = token.split(".")
    payload = decode_jwt_payload(token)
    payload["role"] = new_role
    new_payload_b64 = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    # header . TAMPERED_PAYLOAD . original_signature  →  signature mismatch
    return f"{parts[0]}.{new_payload_b64}.{parts[2]}"


def main():
    section("SECURITY AT SCALE — JWT DEEP DIVE")
    print("""
  Architecture:
    [Client] → POST /token      → [Auth Service :8001] → access_token + refresh_token
    [Client] → GET  /protected  → [API Service  :8002] → verifies JWT locally (no DB call)
    [Client] → POST /refresh    → [Auth Service :8001] → rotates refresh token, new access_token
    [Client] → POST /logout     → [Auth Service :8001] → revokes entire token family

  JWT Structure:
    header.payload.signature   (each part is base64url-encoded, NOT encrypted)

    Header:    {"alg": "HS256", "typ": "JWT"}
    Payload:   {"sub": "alice", "role": "admin", "scopes": [...], "jti": "...", "exp": ...}
    Signature: HMAC-SHA256(base64url(header) + "." + base64url(payload), secret_key)

  Key insight: the payload is readable by anyone who intercepts the token.
  The signature only proves it was issued by the holder of the secret key.
  JWTs provide INTEGRITY, not CONFIDENTIALITY.
""")

    # ── Phase 1: No token → 401 ──────────────────────────────────────────────
    section("Phase 1: Request Without Token → 401")
    call("get", f"{API}/protected",  "GET /protected  (no Authorization header)")
    call("get", f"{API}/admin-only", "GET /admin-only (no Authorization header)")
    print("""
  The API service checks for "Authorization: Bearer <token>" on every request.
  Missing or malformed headers are rejected before any business logic runs.
  This is the first gate in defence-in-depth.
""")

    # ── Phase 2: Login, decode claims ────────────────────────────────────────
    section("Phase 2: Login → JWT with Claims (sub, role, scopes, jti, exp)")
    print("  Logging in as alice (role=admin)...\n")

    r = call("post", f"{AUTH}/token", "POST /token (alice / password123)",
             json={"username": "alice", "password": "password123"})
    tokens        = r.json()
    access_token  = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")

    payload = decode_jwt_payload(access_token)
    exp_in  = payload.get("exp", 0) - int(time.time())
    print(f"""
  Decoded JWT payload:
    sub:    {payload.get('sub')}
    role:   {payload.get('role')}
    scopes: {payload.get('scopes')}
    jti:    {payload.get('jti')}   ← unique token ID (useful for denylist)
    exp:    {payload.get('exp')}   (expires in ~{exp_in}s)
    type:   {payload.get('type')}  ← "access" — API rejects "refresh" tokens

  Token anatomy:
    Part 1 (header):    {access_token.split('.')[0]}
    Part 2 (payload):   {access_token.split('.')[1][:50]}...
    Part 3 (signature): {access_token.split('.')[2][:30]}...
""")

    call("get", f"{API}/protected",  "GET /protected  (valid alice token)",
         headers={"Authorization": f"Bearer {access_token}"})
    call("get", f"{API}/admin-only", "GET /admin-only (alice has admin role)",
         headers={"Authorization": f"Bearer {access_token}"})

    # Also log in bob for later phases
    r_bob       = call("post", f"{AUTH}/token", "POST /token (bob / pass456)",
                       json={"username": "bob", "password": "pass456"})
    bob_tokens  = r_bob.json()
    bob_token   = bob_tokens.get("access_token", "")
    bob_refresh = bob_tokens.get("refresh_token", "")
    bob_payload = decode_jwt_payload(bob_token)
    print(f"\n  Bob's token: sub={bob_payload.get('sub')}, role={bob_payload.get('role')}, "
          f"scopes={bob_payload.get('scopes')}")

    # ── Phase 3: Tamper with JWT payload ─────────────────────────────────────
    section("Phase 3: JWT Payload Tampering — Privilege Escalation Attempt")
    print("  Bob (role=user) modifies his token to claim role=admin:\n")

    tampered = tamper_jwt(bob_token, "admin")
    tampered_payload = decode_jwt_payload(tampered)
    print(f"  Original  token payload: role={bob_payload.get('role')}")
    print(f"  Tampered  token payload: role={tampered_payload.get('role')}  ← modified in-place")
    print(f"  Signature: unchanged (still bob's original signature)\n")

    call("get", f"{API}/admin-only", "GET /admin-only (tampered token, role=admin in payload) → 401",
         headers={"Authorization": f"Bearer {tampered}"})

    print("""
  Why this fails:
    Signature = HMAC-SHA256(original_header + "." + original_payload, secret)
    We changed the payload → the re-computed signature does not match.
    The API service detects the mismatch and rejects the request.
    Without knowing the secret, an attacker cannot produce a valid signature
    for a modified payload.
""")

    # ── Phase 4: Token type confusion ────────────────────────────────────────
    section("Phase 4: Token Type Confusion — Refresh Token Used as Access Token")
    print("  Sending alice's REFRESH token to a protected API endpoint:\n")

    call("get", f"{API}/protected", "GET /protected (refresh_token in Bearer header) → 401",
         headers={"Authorization": f"Bearer {refresh_token}"})

    print("""
  Why this matters:
    Refresh tokens are long-lived (hours/days). If an API service accepted any
    valid JWT — regardless of type — a stolen refresh token would grant indefinite
    API access. The type="access" check ensures only short-lived access tokens
    are accepted by resource servers.

    The refresh token's signature IS valid (same secret), so without the type check
    a naive implementation would accept it. This is a token type confusion attack.
""")

    # ── Phase 5: Token expiry ────────────────────────────────────────────────
    section("Phase 5: Token Expiry — Short-Lived Access Tokens (5s TTL)")
    print("  Getting fresh token with 5s TTL, using it, then waiting 6s...\n")

    r_fresh   = call("post", f"{AUTH}/token", "POST /token (fresh alice token, TTL=5s)",
                     json={"username": "alice", "password": "password123"})
    fresh_tok = r_fresh.json().get("access_token", "")

    call("get", f"{API}/protected", "GET /protected immediately → 200",
         headers={"Authorization": f"Bearer {fresh_tok}"})

    print("  Waiting 6 seconds for token to expire...")
    time.sleep(6)

    call("get", f"{API}/protected", "GET /protected after expiry  → 401",
         headers={"Authorization": f"Bearer {fresh_tok}"})

    print("""
  JWT expiry trade-off:
    Short TTL (5-15 min)  → Minimal exposure if stolen; transparent to users via refresh
    Long TTL  (24h+)      → Convenient but stolen token valid until expiry, cannot be revoked
    Solution: short-lived access token + long-lived refresh token
    The refresh loop is transparent to the user (client does it automatically).

  Critical: a revoked user account is still "valid" for up to TTL minutes
  if access tokens are not in a denylist. Use short TTLs to bound this window.
""")

    # ── Phase 6: Refresh token rotation + theft detection ────────────────────
    section("Phase 6: Refresh Token Rotation + Theft Detection")
    print("  Using bob's refresh token to get a new access token:\n")

    r_rot = call("post", f"{AUTH}/refresh", "POST /refresh (bob's refresh_token) → new tokens",
                 json={"refresh_token": bob_refresh})
    if r_rot.status_code != 200:
        print("  Refresh failed — bob's initial token may have expired (lab ran slowly).")
    else:
        rot_data       = r_rot.json()
        new_bob_access = rot_data.get("access_token", "")
        new_bob_refresh= rot_data.get("refresh_token", "")
        print(f"  rotated: {rot_data.get('rotated')} — old refresh token is NOW invalid\n")

        call("get", f"{API}/protected", "GET /protected (new access token) → 200",
             headers={"Authorization": f"Bearer {new_bob_access}"})

        # Now replay the OLD refresh token — simulates an attacker who had stolen it
        print("\n  Replaying the OLD (rotated-out) refresh token (simulating token theft):\n")
        r_stolen = call("post", f"{AUTH}/refresh",
                        "POST /refresh (OLD bob_refresh — theft detection) → 401",
                        json={"refresh_token": bob_refresh})
        if r_stolen.status_code == 401:
            print(f"  Theft detected! Response: {r_stolen.json().get('error')}")
            print("  ALL sessions in bob's token family have been revoked.")

        # Confirm the new refresh token is also now dead (family revoked)
        print("\n  Trying the NEW refresh token (family was revoked) → 401:\n")
        call("post", f"{AUTH}/refresh",
             "POST /refresh (new_bob_refresh after family revocation) → 401",
             json={"refresh_token": new_bob_refresh})

    print("""
  Refresh token rotation design:
    1. Every successful /refresh call issues a NEW refresh token and invalidates the old one.
    2. If an attacker steals a refresh token and uses it AFTER the legitimate client has
       already rotated it, the server sees a reuse of an invalidated token.
    3. On reuse detection, the server revokes the ENTIRE token family (all outstanding
       refresh tokens for that login session). The attacker is locked out. The legitimate
       user is forced to re-authenticate.
    4. Without rotation, a stolen refresh token is usable indefinitely until expiry.
""")

    # ── Phase 7: Scope-based authorisation ───────────────────────────────────
    section("Phase 7: Scope-Based Authorisation (Fine-Grained, Beyond Roles)")
    print("  Accessing /scoped — requires scope 'read:data' in the token:\n")

    # Re-login alice to get a fresh token (previous one expired)
    r_alice2   = call("post", f"{AUTH}/token", "POST /token (alice, fresh)",
                      json={"username": "alice", "password": "password123"})
    alice2_tok = r_alice2.json().get("access_token", "")
    r_bob2     = call("post", f"{AUTH}/token", "POST /token (bob, fresh)",
                      json={"username": "bob",   "password": "pass456"})
    bob2_tok   = r_bob2.json().get("access_token", "")

    alice_scopes = decode_jwt_payload(alice2_tok).get("scopes", [])
    bob_scopes   = decode_jwt_payload(bob2_tok).get("scopes", [])
    print(f"\n  Alice's scopes: {alice_scopes}")
    print(f"  Bob's   scopes: {bob_scopes}\n")

    call("get", f"{API}/scoped", "GET /scoped (alice, has read:data) → 200",
         headers={"Authorization": f"Bearer {alice2_tok}"})
    call("get", f"{API}/scoped", "GET /scoped (bob, has read:data)   → 200",
         headers={"Authorization": f"Bearer {bob2_tok}"})
    call("get", f"{API}/admin-only", "GET /admin-only (bob, role=user) → 403  [role check]",
         headers={"Authorization": f"Bearer {bob2_tok}"})

    print("""
  Roles vs. Scopes:
    Roles  ("admin", "user")         → coarse-grained, who you ARE
    Scopes ("read:data","write:data") → fine-grained, what you CAN DO
    In OAuth2, scopes are explicitly requested per-token and can be narrower
    than the user's full permissions. A CI/CD token might have only "read:data"
    even if the human has "admin:all". Principle of least privilege.
""")

    # ── Phase 8: Logout ───────────────────────────────────────────────────────
    section("Phase 8: Logout — Revoke Token Family")
    print("  Logging alice out using her refresh token:\n")

    r_alice3    = call("post", f"{AUTH}/token", "POST /token (alice, fresh login)",
                       json={"username": "alice", "password": "password123"})
    alice3_data = r_alice3.json()
    alice3_access  = alice3_data.get("access_token", "")
    alice3_refresh = alice3_data.get("refresh_token", "")

    call("get", f"{API}/protected", "GET /protected (before logout) → 200",
         headers={"Authorization": f"Bearer {alice3_access}"})

    call("post", f"{AUTH}/logout", "POST /logout (alice's refresh token)",
         json={"refresh_token": alice3_refresh})

    # Access token still valid until its TTL expires — this is the JWT revocation gap
    call("get", f"{API}/protected", "GET /protected (access token, still valid ~5s window) → 200",
         headers={"Authorization": f"Bearer {alice3_access}"})

    # But the refresh token is now dead
    call("post", f"{AUTH}/refresh", "POST /refresh (after logout) → 401",
         json={"refresh_token": alice3_refresh})

    print("""
  The JWT revocation gap:
    After logout, the access token remains cryptographically valid until its
    'exp' claim passes (~5s in this lab, 5-15 min in production).
    The API service verifies the signature locally — it does NOT call the auth
    service — so it cannot know the user logged out.

    Mitigation options (in order of increasing cost):
      1. Short access token TTL (5-15 min) — bound the exposure window
      2. Access token denylist checked on every request (adds a DB/Redis lookup —
         sacrifices the stateless benefit of JWTs)
      3. Token binding / Pushed Authorisation Requests (PAR) for sensitive endpoints
""")

    # ── Phase 9: Security Summary ─────────────────────────────────────────────
    section("Phase 9: Security Properties — Full Summary")
    print("""
  Attacks demonstrated and mitigated:
  ┌──────────────────────────────────┬──────────────────────────────────────────────────┐
  │ Attack                           │ Mitigation demonstrated                          │
  ├──────────────────────────────────┼──────────────────────────────────────────────────┤
  │ No token / missing header        │ 401 before any business logic                    │
  │ Payload tampering (role change)  │ Signature covers payload — mismatch → 401        │
  │ Token type confusion             │ type="access" check rejects refresh tokens       │
  │ Expired token reuse              │ exp claim checked on every request               │
  │ Refresh token theft              │ Rotation + reuse detection → family revocation   │
  │ Privilege escalation via role    │ Role checked on /admin-only per request          │
  │ Scope bypass                     │ Scope list checked on /scoped per request        │
  │ Indefinite session after logout  │ TTL bounds the window (short access token)       │
  └──────────────────────────────────┴──────────────────────────────────────────────────┘

  HS256 vs RS256:
  ┌──────────────┬──────────────────────┬──────────────────────────────────────────────┐
  │              │ HS256 (this lab)     │ RS256 (production multi-service)             │
  ├──────────────┼──────────────────────┼──────────────────────────────────────────────┤
  │ Key          │ Shared secret        │ Private key (auth) + public key (APIs)       │
  │ Verification │ Re-compute HMAC      │ RSA signature verify (public key)            │
  │ Forgery risk │ Any service can sign │ Only auth service can sign                   │
  │ Key rotation │ Redeploy all services│ Rotate private only; publish new JWKS        │
  │ Discovery    │ Out-of-band          │ GET /.well-known/jwks.json (standard)        │
  └──────────────┴──────────────────────┴──────────────────────────────────────────────┘

  JWT stateless verification vs session DB:
    Stateless JWT: O(1) verification, no DB call, horizontal scale trivial
                   Trade-off: cannot revoke before expiry without a denylist
    Session DB:    O(1) lookup, instant revocation, requires session store
                   Trade-off: every request touches the session DB (latency, SPOF)

  OWASP API Security Top 10 (relevant items):
    API1  Broken Object Level Auth  → always verify the token's sub matches the resource owner
    API2  Broken Authentication     → pin algorithm, validate type, short TTL + refresh rotation
    API3  Broken Object Property    → never expose internal fields in JWT claims
    API5  Broken Function Level Auth → check role/scope on EVERY endpoint, not just at the gateway
    API8  Security Misconfiguration → never allow alg=none, rotate secrets, use TLS everywhere

  Next: ../14-service-discovery-coordination/
""")


if __name__ == "__main__":
    main()
