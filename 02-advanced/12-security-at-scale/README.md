# Security at Scale

**Prerequisites:** `../11-observability/`
**Next:** `../13-service-discovery-coordination/`

---

## Concept

Security in distributed systems is harder than in monolithic applications because the attack surface grows with every service, network connection, and human who can access credentials. A single service has one authentication boundary; a microservices architecture has N services, N×(N-1) inter-service connections, and potentially N separate secret stores. The principle of defence in depth — multiple independent security layers so that no single failure exposes everything — becomes essential at scale.

Authentication (AuthN) answers "who are you?" — verifying identity via passwords, tokens, or certificates. Authorisation (AuthZ) answers "what are you allowed to do?" — enforcing access control policies after identity is established. The distinction matters for architecture: authentication is typically centralised (a single auth service issues tokens), while authorisation is distributed (each service checks the token's claims against its own policy). Confusing the two leads to security holes — a system that authenticates users but relies on the front-end to enforce permissions is vulnerable to any direct backend call.

JSON Web Tokens (JWTs) are the dominant stateless authentication mechanism for HTTP APIs. A JWT is three base64url-encoded components: header (algorithm), payload (claims), and signature. The signature is a cryptographic hash of header+payload using a secret key. Any service that knows the key can verify the signature without contacting the auth server — this stateless verification is what makes JWTs scale horizontally. The critical limitation is that JWTs cannot be revoked: once issued, a token is valid until its `exp` claim expires, even if the user logs out or the account is suspended. The standard mitigation is short access token TTLs (5-15 minutes) combined with longer-lived refresh tokens that can be stored in a server-side denylist.

OAuth2 is an authorisation framework for delegated access — allowing one application to act on behalf of a user in another system. The authorisation code flow (for user-facing apps) issues an authorisation code that is exchanged for tokens, keeping tokens server-side and preventing them from appearing in browser URLs. The client credentials flow (for machine-to-machine) issues tokens directly to trusted services. OAuth2 is NOT an authentication protocol — it issues access tokens that say "this app can access these scopes," not "this user is authenticated." OpenID Connect (OIDC) adds an identity layer on top of OAuth2 by including an `id_token` (a JWT with user claims) alongside the access token.

Secrets management is the unglamorous foundation of security at scale. Environment variables are the baseline — better than hard-coded strings, worse than a proper secrets manager. HashiCorp Vault provides dynamic secrets (credentials that expire automatically), secret versioning, audit logs, and role-based access. AWS Secrets Manager and GCP Secret Manager provide similar capabilities in cloud environments. The key practices: never commit secrets to source control, rotate secrets regularly, use dynamic short-lived credentials where possible (Vault's AWS dynamic secrets), and audit all secret access.

## How It Works

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

### Trade-offs

| Mechanism | Stateless | Revocable | Complexity | Use Case |
|---|---|---|---|---|
| JWT (HS256) | Yes | No (until exp) | Low | Stateless API auth |
| JWT + refresh token | Partial | Yes (refresh) | Medium | User sessions |
| Opaque token + session DB | No | Yes (instant) | Medium | Requires session store |
| mTLS (mutual TLS) | Yes | Via cert revoke | High | Service-to-service |
| API Key | Yes | Yes (in DB) | Low | M2M, developer APIs |

### Failure Modes

**JWT "none" algorithm attack:** An attacker modifies the JWT header to `{"alg":"none"}` and removes the signature. A vulnerable library that accepts unsigned tokens will accept any payload. Mitigation: always specify the expected algorithm explicitly when verifying — never allow the algorithm to be selected from the token itself.

**Refresh token theft and silent token rotation:** If a refresh token is stolen, the attacker can silently obtain new access tokens. Detection: implement refresh token rotation — each use of a refresh token issues a new one and invalidates the old. If an old refresh token is used, all tokens for that user are revoked immediately (detects theft).

**JWKS endpoint SSRF:** If your API service fetches the auth server's public keys dynamically (`/.well-known/jwks.json`), an attacker who can control the `iss` (issuer) claim may redirect your service to fetch keys from an attacker-controlled URL. Mitigation: pin the issuer URL in configuration, never derive it from the token.

**Secret sprawl:** Microservices environments accumulate secrets in environment variables, Kubernetes Secrets, config files, and CI/CD pipelines. A single leaked secret can compromise the whole system. Mitigation: centralise in Vault or a cloud secrets manager, rotate automatically, and audit access logs.

## Interview Talking Points

- "JWT is stateless — any service with the key can verify tokens without a database call. The trade-off: tokens can't be revoked before expiry. Short TTL (5-15 min) + refresh tokens is the standard mitigation."
- "OAuth2 is for delegated authorisation, not authentication. 'Sign in with Google' is OpenID Connect (OAuth2 + id_token) — OAuth2 alone doesn't tell you who the user is."
- "HS256 uses a shared secret — every service that verifies tokens can also forge them. RS256 uses asymmetric keys — API services get the public key and can verify but not forge. Prefer RS256 in multi-service architectures."
- "Never put sensitive data in JWT claims — the payload is base64, not encrypted. Anyone who intercepts the token can read the claims. JWTs provide integrity (tamper detection), not confidentiality."
- "mTLS for service-to-service authentication: each service has a certificate, both sides verify. This is stronger than shared secrets because compromise of one service doesn't expose credentials for others."
- "Secrets management at scale: environment variables < Kubernetes Secrets (base64 only) < HashiCorp Vault (dynamic secrets, audit logs, auto-rotation). Never commit secrets to git."

## Hands-on Lab

**Time:** ~2 minutes
**Services:** auth-service (port 8001) + api-service (port 8002)

### Setup

```bash
cd system-design-interview/02-advanced/12-security-at-scale/
docker compose up
```

### Experiment

The script runs six phases automatically:

1. Sends requests without a token — both endpoints return 401 with a clear error.
2. Logs in as alice (admin) and as bob (user), displays the decoded JWT payload showing claims, TTL, and token structure.
3. Tampers with bob's JWT by changing the role from "user" to "admin" in the payload while keeping the original signature — the API rejects it with 401 (signature mismatch).
4. Gets a fresh token with 5s TTL, uses it immediately (200), waits 6 seconds, uses it again (401 expired).
5. Uses the long-lived refresh token from Phase 2 to obtain a new access token — demonstrates the refresh flow.
6. Prints a summary of JWT security properties, HS256 vs RS256, and the stateless revocation trade-off.

### Break It

Test the JWT "none" algorithm attack against a naive validator:

```python
import base64, json, jwt

# Craft a token with "none" algorithm
header = base64.urlsafe_b64encode(json.dumps({"alg":"none","typ":"JWT"}).encode()).rstrip(b"=").decode()
payload = base64.urlsafe_b64encode(json.dumps({"sub":"hacker","role":"admin","exp":9999999999}).encode()).rstrip(b"=").decode()
evil_token = f"{header}.{payload}."  # no signature

# A vulnerable validator that reads algorithm from the token:
# jwt.decode(evil_token, options={"verify_signature": False})  ← NEVER do this

# Safe: always specify algorithm explicitly
try:
    jwt.decode(evil_token, "any-secret", algorithms=["HS256"])
except jwt.InvalidSignatureError:
    print("Safe: algorithm=none rejected correctly")
```

### Observe

The tampered token in Phase 3 will always return 401 — the signature covers the payload, so any payload modification invalidates it. The expiry test in Phase 4 shows the exact moment a token stops being valid. The refresh flow in Phase 5 demonstrates how long-lived sessions are maintained with short-lived access tokens.

### Teardown

```bash
docker compose down
```

## Real-World Examples

- **Stripe API keys:** Stripe uses API keys (long-lived opaque tokens) for server-to-server integration, and OAuth2 for third-party app access. Keys are prefixed (`sk_live_`, `sk_test_`) for easy identification in logs and to prevent test keys from reaching production. Stripe recommends storing keys in environment variables and rotating them immediately if leaked. Source: Stripe documentation, "API Keys and Authentication."
- **Google Cloud IAM and Workload Identity:** Google Cloud's service accounts use short-lived OAuth2 access tokens (1-hour TTL) signed by Google's RSA keys. The public keys are published at `accounts.google.com/o/oauth2/certs`. Instead of distributing long-lived credentials, GCE instances and GKE pods automatically receive short-lived tokens from the metadata server, renewed transparently. Source: Google Cloud documentation, "Understanding Service Accounts."
- **HashiCorp Vault at scale:** Vault's dynamic secrets feature generates database credentials on demand — a service requests credentials, Vault creates a new DB user with a TTL (e.g., 1 hour), the service uses them, and Vault automatically deletes the user when the TTL expires. This means leaked credentials expire quickly and are never reused. Vault is used at scale by companies including Cloudflare, Nordstrom, and Adobe. Source: HashiCorp, "Vault: Dynamic Secrets," product documentation.

## Common Mistakes

- **Long-lived access tokens without refresh.** Using a 24-hour JWT access token "for convenience" means a stolen token is valid for up to a day after discovery. Use 5-15 minute access tokens with refresh tokens. The UX impact is zero — the refresh is transparent to the user.
- **Storing JWTs in localStorage.** `localStorage` is accessible to any JavaScript on the page, including injected XSS scripts. Store access tokens in memory (JavaScript variable) and refresh tokens in `HttpOnly, Secure, SameSite=Strict` cookies, which are inaccessible to JavaScript.
- **Not validating the `alg` field.** Some early JWT libraries would accept whatever algorithm was specified in the token header, including `none`. Always hardcode the expected algorithm in your verification call: `jwt.decode(token, secret, algorithms=["HS256"])`.
- **Putting sensitive data in JWT claims.** JWT payloads are base64-encoded, not encrypted. An attacker who captures the token can read all claims. Never include email addresses, PII, or business-sensitive data in the payload unless the token is also encrypted (JWE, not just JWS).
- **Sharing the same JWT secret across environments.** A leaked test secret that matches the production secret allows token forgery against production. Use separate secrets per environment, rotate them independently, and use a secrets manager rather than hardcoding them in environment files.
