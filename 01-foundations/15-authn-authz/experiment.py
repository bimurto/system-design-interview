#!/usr/bin/env python3
"""
Authentication & Authorization Lab
Demonstrates session auth, JWT auth, RBAC, API keys, and OAuth flow
"""

import sys
import time
import requests

AUTH_URL = 'http://localhost:5001'
API_URL = 'http://localhost:5002'


def print_section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


def print_response(response, label=""):
    status = "✓" if response.status_code < 400 else "✗"
    print(f"  [{status}] {label} (HTTP {response.status_code})")
    if response.status_code < 400:
        data = response.json()
        if isinstance(data, dict):
            for key, value in list(data.items())[:4]:
                print(f"      {key}: {value}")
    else:
        print(f"      Error: {response.json().get('error', 'Unknown')}")


# ============================================================================
# Phase 1: Session-Based Authentication
# ============================================================================

def phase1_session_auth():
    print_section("Phase 1: Session-Based Authentication")

    # 1.1 Login
    print("\n  Step 1: Login (creates server-side session)")
    resp = requests.post(f"{AUTH_URL}/auth/session/login", json={
        'username': 'alice',
        'password': 'password123'
    })
    session_cookie = resp.cookies.get('session_id')
    print_response(resp, "Login as alice")
    print(f"      Session cookie: {session_cookie[:20]}..." if session_cookie else "      No session cookie!")

    # 1.2 Access protected endpoint
    print("\n  Step 2: Access /me with session cookie")
    resp = requests.get(f"{AUTH_URL}/auth/session/me", cookies={'session_id': session_cookie})
    print_response(resp, "Get current user")

    # 1.3 Logout
    print("\n  Step 3: Logout (invalidates server-side session)")
    resp = requests.post(f"{AUTH_URL}/auth/session/logout", cookies={'session_id': session_cookie})
    print_response(resp, "Logout")

    # 1.4 Try to use invalidated session
    print("\n  Step 4: Try to use invalidated session")
    resp = requests.get(f"{AUTH_URL}/auth/session/me", cookies={'session_id': session_cookie})
    print_response(resp, "Access with invalidated session (should fail)")

    print("\n  → Session-based auth: Server-side state, instant revocation, requires Redis lookup")


# ============================================================================
# Phase 2: JWT-Based Authentication
# ============================================================================

def phase2_jwt_auth():
    print_section("Phase 2: JWT-Based Authentication")

    tokens = {'alice': None, 'bob': None}

    for username, password in [('alice', 'password123'), ('bob', 'password456')]:
        print(f"\n  User: {username}")

        # 2.1 Login
        print(f"  Step 1: Login (returns JWT)")
        resp = requests.post(f"{AUTH_URL}/auth/jwt/login", json={
            'username': username,
            'password': password
        })
        print_response(resp, f"Login as {username}")

        if resp.status_code == 200:
            tokens[username] = resp.json()
            access_token = tokens[username]['access_token']
            print(f"      Access token: {access_token[:40]}...")
            print(f"      Refresh token: {tokens[username]['refresh_token'][:20]}...")

        # 2.2 Access protected API
        print(f"\n  Step 2: Access protected API with JWT")
        resp = requests.get(f"{API_URL}/api/me", headers={
            'Authorization': f"Bearer {tokens[username]['access_token']}"
        })
        print_response(resp, f"Get /api/me as {username}")

    # 2.3 Demonstrate token contents
    print("\n  Step 3: Inspect JWT token contents")
    resp = requests.get(f"{API_URL}/debug/token", headers={
        'Authorization': f"Bearer {tokens['alice']['access_token']}"
    })
    if resp.status_code == 200:
        data = resp.json()
        print(f"      Header: {data['header']}")
        print(f"      Payload: {data['payload'][:100]}...")
        print(f"      Note: {data['note']}")

    print("\n  → JWT-based auth: Stateless, no Redis lookup, cannot revoke before expiry")

    return tokens


# ============================================================================
# Phase 3: Role-Based Access Control (RBAC)
# ============================================================================

def phase3_rbac(tokens):
    print_section("Phase 3: Role-Based Access Control (RBAC)")

    # Alice is admin, Bob is regular user
    for username in ['alice', 'bob']:
        token = tokens[username]['access_token']
        print(f"\n  User: {username}")

        # Try to access user endpoint
        print(f"  Step 1: Access regular user endpoint (/api/posts)")
        resp = requests.get(f"{API_URL}/api/posts", headers={
            'Authorization': f"Bearer {token}"
        })
        print_response(resp, f"List posts as {username}")

        # Try to access admin endpoint
        print(f"\n  Step 2: Access admin endpoint (/api/admin/users)")
        resp = requests.get(f"{API_URL}/api/admin/users", headers={
            'Authorization': f"Bearer {token}"
        })
        print_response(resp, f"List users as {username}")

    print("\n  → RBAC: Role in token determines access; admin endpoints reject non-admin users")


# ============================================================================
# Phase 4: Resource-Level Authorization
# ============================================================================

def phase4_resource_auth(tokens):
    print_section("Phase 4: Resource-Level Authorization")

    print("\n  Scenario: Resource owned by 'alice'")
    print("  Bob attempts to modify alice's resource")

    # Bob tries to read (should work)
    print("\n  Step 1: Bob reads alice's resource (GET - allowed for anyone)")
    resp = requests.get(f"{API_URL}/api/resource/123", headers={
        'Authorization': f"Bearer {tokens['bob']['access_token']}"
    })
    print_response(resp, "GET /api/resource/123 as bob")

    # Bob tries to update (should fail - not owner or admin)
    print("\n  Step 2: Bob updates alice's resource (PUT - owner/admin only)")
    resp = requests.put(f"{API_URL}/api/resource/123", json={
        'title': 'Modified by Bob'
    }, headers={
        'Authorization': f"Bearer {tokens['bob']['access_token']}"
    })
    print_response(resp, "PUT /api/resource/123 as bob (should fail)")

    # Alice updates her own resource (should work)
    print("\n  Step 3: Alice updates her own resource (PUT - owner)")
    resp = requests.put(f"{API_URL}/api/resource/123", json={
        'title': 'Modified by Alice'
    }, headers={
        'Authorization': f"Bearer {tokens['alice']['access_token']}"
    })
    print_response(resp, "PUT /api/resource/123 as alice (should succeed)")

    print("\n  → Resource auth: Ownership check in addition to authentication")


# ============================================================================
# Phase 5: API Key Authentication
# ============================================================================

def phase5_api_keys():
    print_section("Phase 5: API Key Authentication (Machine-to-Machine)")

    api_keys = [
        ('ak_prod_12345abcdef', 'Mobile App (read+write)'),
        ('ak_prod_67890ghijkl', 'Partner Integration (read-only)'),
        ('invalid_key_12345', 'Invalid key'),
    ]

    for key, description in api_keys:
        print(f"\n  API Key: {description}")

        # Verify key
        resp = requests.get(f"{AUTH_URL}/auth/apikey/verify", headers={
            'X-API-Key': key
        })
        print_response(resp, "Verify API key")

        # Test write scope
        print(f"\n  Step 2: Test write:posts scope")
        resp = requests.post(f"{AUTH_URL}/auth/apikey/test-scope", json={
            'scope': 'write:posts'
        }, headers={
            'X-API-Key': key
        })
        print_response(resp, "Check write:posts permission")

    print("\n  → API Keys: Identify applications, not users; scoped permissions; long-lived")


# ============================================================================
# Phase 6: OAuth 2.0 Flow Simulation
# ============================================================================

def phase6_oauth():
    print_section("Phase 6: OAuth 2.0 Authorization Code Flow")

    print("\n  Step 1: User clicks 'Login with OAuth' (authorization request)")
    resp = requests.get(f"{AUTH_URL}/auth/oauth/authorize", params={
        'client_id': 'my_app',
        'redirect_uri': 'http://localhost:8080/callback',
        'state': 'random_state_string'
    })
    auth_code = None
    if resp.status_code == 200:
        data = resp.json()
        auth_code = data.get('authorization_code')
        print(f"      Authorization code received: {auth_code[:30]}...")
        print(f"      Note: {data.get('note')}")

    print("\n  Step 2: Exchange authorization code for access token")
    if auth_code:
        resp = requests.post(f"{AUTH_URL}/auth/oauth/token", json={
            'grant_type': 'authorization_code',
            'code': auth_code,
            'client_id': 'my_app',
            'client_secret': 'client_secret_123'
        })
        print_response(resp, "Exchange code for token")

        if resp.status_code == 200:
            token_data = resp.json()
            access_token = token_data.get('access_token')
            print(f"\n  Step 3: Use access token to call protected API")
            resp = requests.get(f"{API_URL}/api/me", headers={
                'Authorization': f"Bearer {access_token}"
            })
            print_response(resp, "Access API with OAuth token")

    print("\n  → OAuth 2.0: Delegated authorization; user grants limited access to third party")


# ============================================================================
# Phase 7: Token Refresh
# ============================================================================

def phase7_token_refresh(tokens):
    print_section("Phase 7: Refresh Token Rotation")

    print("\n  Step 1: Use refresh token to get new access token")
    refresh_token = tokens['alice']['refresh_token']
    print(f"      Old refresh token: {refresh_token[:30]}...")

    resp = requests.post(f"{AUTH_URL}/auth/jwt/refresh", json={
        'refresh_token': refresh_token
    })
    print_response(resp, "Refresh access token")

    if resp.status_code == 200:
        new_data = resp.json()
        print(f"\n      New access token: {new_data['access_token'][:40]}...")
        print(f"      New refresh token: {new_data['refresh_token'][:30]}...")
        print("\n      → Old refresh token is now INVALID (single-use rotation)")

        # Try to use old refresh token (should fail)
        print("\n  Step 2: Try to reuse old refresh token (should fail)")
        resp = requests.post(f"{AUTH_URL}/auth/jwt/refresh", json={
            'refresh_token': refresh_token
        })
        print_response(resp, "Reuse old refresh token (should fail)")

    print("\n  → Refresh token rotation: Single-use tokens prevent replay attacks")


# ============================================================================
# Summary
# ============================================================================

def print_summary():
    print_section("Summary: Authentication Patterns Compared")

    print("""
  ┌─────────────────────┬─────────────────┬─────────────────┬─────────────────┐
  │ Aspect              │ Session + Cookie│ JWT (Bearer)    │ OAuth 2.0       │
  ├─────────────────────┼─────────────────┼─────────────────┼─────────────────┤
  │ Server State        │ Required (Redis)│ Stateless       │ Requires auth   │
  │                     │                 │                 │ server          │
  ├─────────────────────┼─────────────────┼─────────────────┼─────────────────┤
  │ Scalability         │ Needs sticky    │ Horizontally    │ Delegated to    │
  │                     │ sessions/Redis  │ scalable        │ provider        │
  ├─────────────────────┼─────────────────┼─────────────────┼─────────────────┤
  │ Revocation          │ Instant         │ Wait for expiry │ Wait for expiry │
  ├─────────────────────┼─────────────────┼─────────────────┼─────────────────┤
  │ XSS Protection      │ HttpOnly cookie │ Vulnerable      │ Vulnerable      │
  ├─────────────────────┼─────────────────┼─────────────────┼─────────────────┤
  │ CSRF Protection     │ SameSite/Lax    │ Protected       │ Protected       │
  ├─────────────────────┼─────────────────┼─────────────────┼─────────────────┤
  │ Use Case            │ Web apps        │ SPAs, Mobile    │ Third-party     │
  │                     │                 │ Microservices   │ integrations    │
  └─────────────────────┴─────────────────┴─────────────────┴─────────────────┘

  Key Takeaways:
  • Session auth = Stateful, instant revoke, XSS-protected, needs Redis
  • JWT auth = Stateless, scales well, cannot revoke early, vulnerable to XSS
  • OAuth 2.0 = Delegated auth, users don't share passwords with third parties
  • RBAC = Roles determine permissions; ABAC = Dynamic attribute-based policies
  • API Keys = Machine-to-machine; scoped; long-lived secrets
    """)


def main():
    print("""
    ============================================================
      AUTHENTICATION & AUTHORIZATION LAB
    ============================================================
    This lab demonstrates:
      1. Session-based authentication
      2. JWT-based authentication
      3. Role-Based Access Control (RBAC)
      4. Resource-level authorization
      5. API Key authentication
      6. OAuth 2.0 flow
      7. Refresh token rotation
    """)

    # Wait for services
    print("\n  Waiting for services to be ready...")
    max_retries = 30
    for i in range(max_retries):
        try:
            requests.get(f"{AUTH_URL}/health", timeout=1)
            requests.get(f"{API_URL}/health", timeout=1)
            print("  Services ready!\n")
            break
        except:
            if i == max_retries - 1:
                print("  ERROR: Services not ready. Run: docker compose up -d")
                sys.exit(1)
            time.sleep(1)

    # Run phases
    phase1_session_auth()
    tokens = phase2_jwt_auth()
    phase3_rbac(tokens)
    phase4_resource_auth(tokens)
    phase5_api_keys()
    phase6_oauth()
    phase7_token_refresh(tokens)
    print_summary()

    print("\n  Lab complete! Run 'docker compose down -v' to cleanup.")


if __name__ == '__main__':
    main()
