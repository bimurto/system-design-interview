# Security at Scale

**Prerequisites:** `../11-observability/`
**Next:** `../14-service-discovery-coordination/`

---

## Concept

Security in distributed systems is harder than in monolithic applications because the attack surface grows with every
service, network connection, and human who can access credentials. A single service has one authentication boundary; a
microservices architecture has N services, N×(N-1) inter-service connections, and potentially N separate secret stores.
The principle of defence in depth — multiple independent security layers so that no single failure exposes everything —
becomes essential at scale.

Authentication (AuthN) answers "who are you?" — verifying identity via passwords, tokens, or certificates.
Authorisation (AuthZ) answers "what are you allowed to do?" — enforcing access control policies after identity is
established. The distinction matters for architecture: authentication is typically centralised (a single auth service
issues tokens), while authorisation is distributed (each service checks the token's claims against its own policy).
Confusing the two leads to security holes — a system that authenticates users but relies on the front-end to enforce
permissions is vulnerable to any direct backend call.

JSON Web Tokens (JWTs) are the dominant stateless authentication mechanism for HTTP APIs. A JWT is three
base64url-encoded components: header (algorithm), payload (claims), and signature. The signature is a cryptographic hash
of header+payload using a secret key. Any service that knows the key can verify the signature without contacting the
auth server — this stateless verification is what makes JWTs scale horizontally. The critical limitation is that JWTs
cannot be revoked: once issued, a token is valid until its `exp` claim expires, even if the user logs out or the account
is suspended. The standard mitigation is short access token TTLs (5-15 minutes) combined with longer-lived refresh
tokens that can be stored in a server-side denylist.

OAuth2 is an authorisation framework for delegated access — allowing one application to act on behalf of a user in
another system. The authorisation code flow (for user-facing apps) issues an authorisation code that is exchanged for
tokens, keeping tokens server-side and preventing them from appearing in browser URLs. The client credentials flow (for
machine-to-machine) issues tokens directly to trusted services. OAuth2 is NOT an authentication protocol — it issues
access tokens that say "this app can access these scopes," not "this user is authenticated." OpenID Connect (OIDC) adds
an identity layer on top of OAuth2 by including an `id_token` (a JWT with user claims) alongside the access token.

Secrets management is the unglamorous foundation of security at scale. Environment variables are the baseline — better
than hard-coded strings, worse than a proper secrets manager. HashiCorp Vault provides dynamic secrets (credentials that
expire automatically), secret versioning, audit logs, and role-based access. AWS Secrets Manager and GCP Secret Manager
provide similar capabilities in cloud environments. The key practices: never commit secrets to source control, rotate
secrets regularly, use dynamic short-lived credentials where possible (Vault's AWS dynamic secrets), and audit all
secret access.

## How It Works

**JWT Authentication Request Flow:**

1. Client sends credentials (`POST /token` with username + password) to the authentication server
2. Auth server validates the credentials, creates a JWT payload (`sub`, `role`, `exp`, `iat`), and signs it with a
   secret (HS256) or private key (RS256)
3. Auth server returns a short-lived **access token** (5–15 min TTL) and a long-lived **refresh token** (e.g., 30 days);
   client stores both
4. Client attaches the access token to every API request: `Authorization: Bearer <token>`
5. API server decodes the JWT header and payload (base64url), then **verifies the signature** using the shared secret or
   public key — no database lookup required
6. If the signature is valid and `exp` has not passed, the request proceeds; the token's claims (`role`, `scopes`) drive
   authorisation decisions
7. When the access token expires, client sends the refresh token to `POST /refresh`; auth server validates it and issues
   a new access token without requiring re-login

**JWT structure:**

```
  eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9    ← base64url({"alg":"HS256","typ":"JWT"})
  .
  eyJzdWIiOiJhbGljZSIsInJvbGUiOiJhZG1pbiIsImV4cCI6MTcwMDAwMH0  ← base64url(payload)
  .
  SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c   ← HMAC-SHA256(header.payload, secret)

  Anyone can decode the payload (it is base64, not encrypted).
  Only the holder of the secret can produce a valid signature.
  → JWTs provide integrity, not confidentiality. Never put secrets in claims.
```

**HS256 vs RS256:**

```
  HS256 (HMAC-SHA256):
    Signing:     HMAC(secret_key, header.payload)
    Verifying:   HMAC(secret_key, header.payload) == signature
    Key type:    Symmetric — same key signs and verifies
    Use case:    Single service or tight service cluster (all share secret)
    Risk:        Any service that can verify tokens can also forge them

  RS256 (RSA-SHA256):
    Signing:     RSA_sign(private_key, header.payload)
    Verifying:   RSA_verify(public_key, header.payload, signature)
    Key type:    Asymmetric — private key signs, public key verifies
    Use case:    Auth server signs; API services verify without forging ability
    Benefit:     Compromise of API service doesn't expose signing capability
```

**Refresh token flow:**

```
  Login:
    Client → POST /token (user+password) → Auth Server
    Auth Server → access_token (5min TTL) + refresh_token (30 days)

  API request:
    Client → GET /api/data (Bearer access_token) → API Server
    API Server → verify signature locally (no Auth Server call) → 200

  Token expired:
    Client → POST /refresh (refresh_token) → Auth Server
    Auth Server → lookup refresh_token in DB → if valid → new access_token
    (Auth Server can revoke the refresh_token at any time)

  Logout:
    Client → POST /logout → Auth Server
    Auth Server → delete refresh_token from DB
    Old access tokens still valid until their TTL expires (~5 min window)
```

**HTTPS / TLS handshake:**

```
  Client Hello  → [TLS versions, cipher suites]
  Server Hello  ← [chosen cipher, server certificate]
  Client verifies certificate against CA chain
  Key Exchange  → ECDHE: client sends public key component
  Server sends  ← public key component
  Both compute shared pre-master secret → session keys
  Finished      → first encrypted message from both sides
  ~ 1 RTT (TLS 1.3) or 2 RTT (TLS 1.2)
```

### Roles vs. Scopes

Authentication establishes *who* the caller is. Authorisation has two layers: **roles** and **scopes**.

- **Roles** are coarse-grained identity attributes ("admin", "user") attached to a person. They answer "what kind of
  user is this?"
- **Scopes** are fine-grained, per-token permissions ("read:data", "write:data", "admin:all") that limit what a specific
  token can do — even if the user would normally be allowed to do more. An admin user can request a scope-limited token
  for a CI/CD pipeline, applying least privilege without creating a separate account.

In OAuth2, the `scope` parameter in the token request explicitly bounds what access is granted. The access token's
`scopes` claim is checked by the resource server (API service), not the auth server. This decoupling means a single auth
server can issue tokens for dozens of resource servers with different scope requirements.

### Refresh Token Rotation

The standard pattern for long-lived sessions:

1. Login returns a short-lived **access token** (5-15 min) + a long-lived **refresh token** (hours/days).
2. The client uses the access token for API calls. When it expires, the client silently calls `/refresh` with the
   refresh token.
3. **On each `/refresh` call**, the server issues a NEW refresh token and immediately invalidates the old one. This is *
   *refresh token rotation**.
4. If an old (rotated) refresh token is presented again, the server infers the token was stolen (someone is replaying a
   token that should no longer exist). The server revokes the **entire token family** — all refresh tokens from that
   login session — forcing re-authentication. The attacker and the legitimate client are both kicked out.

Without rotation, a stolen refresh token is valid until it expires, potentially for days. With rotation, the exposure
window is bounded to the time between the theft and the next legitimate use.

### Trade-offs

| Mechanism                    | Stateless | Revocable            | Complexity | Best For                             |
|------------------------------|-----------|----------------------|------------|--------------------------------------|
| JWT access token (HS256)     | Yes       | No (until exp)       | Low        | Stateless API auth, horizontal scale |
| JWT + refresh token rotation | Partial   | Yes (refresh family) | Medium     | User sessions, mobile apps           |
| Opaque token + session DB    | No        | Yes (instant)        | Medium     | When instant revocation is required  |
| mTLS (mutual TLS)            | Yes       | Via CRL/OCSP         | High       | Service-to-service (zero trust)      |
| API Key (opaque, DB-backed)  | No        | Yes (immediate)      | Low        | M2M integrations, developer APIs     |
| OIDC id_token                | Yes       | No (until exp)       | Low-Medium | User identity layer on top of OAuth2 |

### Failure Modes

**JWT "none" algorithm attack:** An attacker modifies the JWT header to `{"alg":"none"}` and removes the signature. A
vulnerable library that accepts unsigned tokens will accept any payload. Mitigation: always specify the expected
algorithm explicitly when verifying — never allow the algorithm to be selected from the token itself. In PyJWT:
`jwt.decode(token, secret, algorithms=["HS256"])`.

**Algorithm confusion (RS256 → HS256):** If an API service accepts both HS256 and RS256, an attacker can take the RS256
public key (which is public!) and craft an HS256-signed token using it as the symmetric secret. The server, accepting
HS256, re-computes HMAC with the public key and finds a match. Mitigation: pin one algorithm per service — never accept
a flexible algorithm list.

**Token type confusion:** If refresh tokens and access tokens use the same signing key and the resource server doesn't
check the `type` claim, a long-lived refresh token can be used directly as an access token. Mitigation: include a `type`
claim and reject tokens where `type != "access"` at the resource server.

**Refresh token theft without rotation:** If a refresh token is stolen, the attacker can silently obtain new access
tokens indefinitely. Detection requires rotation — each use issues a new token; reuse of a rotated-out token signals
theft and triggers full-family revocation.

**JWKS endpoint SSRF:** If your API service fetches the auth server's public keys dynamically (
`/.well-known/jwks.json`), an attacker who can control the `iss` (issuer) claim may redirect your service to fetch keys
from an attacker-controlled URL, effectively issuing themselves valid tokens. Mitigation: pin the issuer URL in
configuration, never derive it from the token itself.

**JWT revocation gap:** After revoking a user (account suspension, logout), their outstanding access tokens remain
cryptographically valid until `exp`. A user suspended at 10:00 can still make API calls until 10:15 with a 15-minute
TTL. Mitigation: short TTL (5-15 min) bounds the window; for stricter requirements, add a Redis-backed access-token
denylist checked on every request (trades the stateless advantage for immediate revocation).

**Secret sprawl:** Microservices environments accumulate secrets in environment variables, Kubernetes Secrets, config
files, and CI/CD pipelines. A single leaked secret can compromise the whole system. Mitigation: centralise in Vault or a
cloud secrets manager, use dynamic short-lived credentials (Vault dynamic secrets), rotate automatically, and audit all
access logs.

## Interview Talking Points

- "JWT is stateless — any service with the key can verify tokens without a database call. The trade-off: tokens can't be
  revoked before expiry. Short TTL (5-15 min) + refresh tokens is the standard mitigation. For strict revocation (
  account suspension), add a Redis-backed denylist checked on every request — but this sacrifices the stateless
  advantage."
- "OAuth2 is for delegated authorisation, not authentication. 'Sign in with Google' is OpenID Connect (OAuth2 +
  id_token) — OAuth2 alone tells you what the app *can do*, not *who the user is*. The id_token is a JWT containing user
  identity claims."
- "HS256 uses a shared secret — every service that verifies tokens can also forge them. RS256 uses asymmetric keys — API
  services hold only the public key and can verify but not sign. Prefer RS256 in multi-service architectures to contain
  blast radius if a service is compromised."
- "Algorithm confusion: if your verification code accepts multiple algorithms, an attacker can take the RS256 public key
  and forge an HS256 token using it as the symmetric secret. Always pin the algorithm in configuration, never derive it
  from the token header."
- "Roles vs. scopes: roles are coarse ('admin', 'user') and describe what kind of user someone is. Scopes are
  fine-grained per-token permissions ('read:data', 'write:data') that can be narrower than the user's full permissions.
  A CI/CD token should have only the minimum scopes needed, even if the human has admin rights."
- "Refresh token rotation: each use of a refresh token issues a new one and invalidates the old. If the old token is
  seen again, it signals theft — revoke the entire family. Without rotation, a stolen refresh token is valid
  indefinitely."
- "Never put sensitive data in JWT claims — the payload is base64, not encrypted. Anyone who intercepts the token can
  read the claims. JWTs provide integrity (tamper detection via signature), not confidentiality. Encrypt with JWE if you
  need confidential claims."
- "mTLS for service-to-service: both sides present and verify X.509 certificates. Compromise of one service doesn't
  expose credentials for others. Services mesh products (Istio, Linkerd) handle mTLS transparently so application code
  never sees it."
- "Secrets management at scale: environment variables < Kubernetes Secrets (base64-encoded, not encrypted at rest by
  default) < HashiCorp Vault (dynamic secrets, auto-rotation, per-request audit log). Dynamic secrets are the gold
  standard — credentials expire automatically and are never reused."

## Hands-on Lab

**Time:** ~2 minutes
**Services:** auth-service (port 8001) + api-service (port 8002)

### Setup

```bash
cd system-design-interview/02-advanced/13-security-at-scale/
docker compose up
```

### Experiment

The script runs nine phases automatically:

1. Sends requests without a token — both endpoints return 401 with a clear error.
2. Logs in as alice (admin) and bob (user); displays the decoded JWT payload including `sub`, `role`, `scopes`, `jti`,
   `exp`, and `type` claims.
3. Tampers with bob's JWT by changing the role from "user" to "admin" in the payload while keeping the original
   signature — the API rejects it with 401 (signature mismatch). Shows why payload integrity is guaranteed.
4. Uses a refresh token directly as a Bearer token against the API service — rejected with 401 (token type confusion
   check).
5. Gets a fresh token with 5s TTL, uses it immediately (200), waits 6 seconds, uses it again (401 expired).
6. Demonstrates refresh token rotation: the old refresh token is replayed after rotation, triggering theft detection —
   the entire token family is revoked.
7. Scope-based authorisation: alice (admin, scopes=[read:data, write:data, admin:all]) and bob (user,
   scopes=[read:data]) both access `/scoped`; bob is rejected from `/admin-only` by role check.
8. Logout: invalidates the token family via the refresh token; the access token remains valid until TTL (demonstrates
   the JWT revocation gap).
9. Full summary table of attacks demonstrated, HS256 vs RS256 comparison, and OWASP API Security Top 10 references.

### Break It

**Attack 1 — JWT "none" algorithm attack:**

```python
import base64, json, jwt

# Craft a token with "none" algorithm
header  = base64.urlsafe_b64encode(json.dumps({"alg":"none","typ":"JWT"}).encode()).rstrip(b"=").decode()
payload = base64.urlsafe_b64encode(json.dumps({"sub":"hacker","role":"admin","exp":9999999999}).encode()).rstrip(b"=").decode()
evil_token = f"{header}.{payload}."  # no signature

# A vulnerable validator that trusts the "alg" header in the token:
# jwt.decode(evil_token, options={"verify_signature": False})  ← NEVER do this

# Safe: always pin the algorithm — PyJWT rejects "none" when algorithms=["HS256"]
try:
    jwt.decode(evil_token, "any-secret", algorithms=["HS256"])
except jwt.exceptions.DecodeError:
    print("Safe: algorithm=none rejected correctly")
```

**Attack 2 — Algorithm confusion (RS256 → HS256):**

```python
# If a service accepts BOTH HS256 and RS256, an attacker can:
# 1. Obtain the RS256 public key (it's public — e.g., from /.well-known/jwks.json)
# 2. Use it as the HS256 symmetric secret to sign a forged token
# 3. The server re-computes HMAC(public_key) and finds a match

# Safe: never accept multiple algorithms on the same endpoint
# jwt.decode(token, public_key, algorithms=["HS256", "RS256"])  ← VULNERABLE
# jwt.decode(token, public_key, algorithms=["RS256"])            ← Safe
```

### Observe

- Phase 3: The tampered token always returns 401 — the signature covers the exact bytes of the payload, so any
  modification invalidates it.
- Phase 4: The type confusion check shows that even a cryptographically valid refresh token is rejected when used as an
  access token.
- Phase 5: The exact moment a token crosses its `exp` boundary — the same request returns 200 then 401 with no code
  changes.
- Phase 6: After rotation, replaying the old refresh token triggers a 401 with "theft detection" and revokes the new
  token too — observe that the new refresh token is also dead after the family is revoked.
- Phase 8: After logout, the access token remains valid for its remaining TTL — this is the JWT revocation gap in
  action.

### Teardown

```bash
docker compose down
```

## Real-World Examples

- **Stripe API keys:** Stripe uses API keys (long-lived opaque tokens) for server-to-server integration, and OAuth2 for
  third-party app access. Keys are prefixed (`sk_live_`, `sk_test_`) for easy identification in logs and to prevent test
  keys from reaching production. Stripe recommends storing keys in environment variables and rotating them immediately
  if leaked. Source: Stripe documentation, "API Keys and Authentication."
- **Google Cloud IAM and Workload Identity:** Google Cloud's service accounts use short-lived OAuth2 access tokens (
  1-hour TTL) signed by Google's RSA keys. The public keys are published at `accounts.google.com/o/oauth2/certs`.
  Instead of distributing long-lived credentials, GCE instances and GKE pods automatically receive short-lived tokens
  from the metadata server, renewed transparently. Source: Google Cloud documentation, "Understanding Service Accounts."
- **HashiCorp Vault at scale:** Vault's dynamic secrets feature generates database credentials on demand — a service
  requests credentials, Vault creates a new DB user with a TTL (e.g., 1 hour), the service uses them, and Vault
  automatically deletes the user when the TTL expires. This means leaked credentials expire quickly and are never
  reused. Vault is used at scale by companies including Cloudflare, Nordstrom, and Adobe. Source: HashiCorp, "Vault:
  Dynamic Secrets," product documentation.

## Common Mistakes

- **Long-lived access tokens without refresh.** Using a 24-hour JWT access token "for convenience" means a stolen token
  is valid for up to a day after discovery. Use 5-15 minute access tokens with refresh tokens. The UX impact is zero —
  the refresh is transparent to the user.
- **Storing JWTs in localStorage.** `localStorage` is accessible to any JavaScript on the page, including injected XSS
  scripts. Store access tokens in memory (JavaScript variable) and refresh tokens in `HttpOnly, Secure, SameSite=Strict`
  cookies, which are inaccessible to JavaScript.
- **Not validating the `alg` field.** Some early JWT libraries would accept whatever algorithm was specified in the
  token header, including `none`. Always hardcode the expected algorithm in your verification call:
  `jwt.decode(token, secret, algorithms=["HS256"])`.
- **Putting sensitive data in JWT claims.** JWT payloads are base64-encoded, not encrypted. An attacker who captures the
  token can read all claims. Never include email addresses, PII, or business-sensitive data in the payload unless the
  token is also encrypted (JWE, not just JWS).
- **Sharing the same JWT secret across environments.** A leaked test secret that matches the production secret allows
  token forgery against production. Use separate secrets per environment, rotate them independently, and use a secrets
  manager rather than hardcoding them in environment files.
