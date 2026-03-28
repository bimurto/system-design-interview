# Authentication & Authorization

**Prerequisites:** [11 — API Design](../11-api-design/)
**Next:** [12 — Blob/Object Storage](../12-blob-object-storage/)

---

## Concept

Authentication and authorization are often conflated but solve fundamentally different problems. **Authentication** (authn) answers "who are you?" — it establishes identity. **Authorization** (authz) answers "what are you allowed to do?" — it enforces permissions. A system can authenticate you successfully (verify your identity) yet deny access because you lack authorization for a specific resource. Confusing these leads to architectural mistakes: treating authentication as sufficient for security, or building authorization checks that bypass authentication entirely.

Authentication has evolved from server-side sessions to token-based systems. **Session-based authentication** stores user state server-side (in memory or Redis) and issues a session cookie. It is simple, stateful, and easy to invalidate — a compromised session can be deleted from the store. The trade-off is scalability: session lookups add latency to every request, and session affinity complicates horizontal scaling. **Token-based authentication** (JWT) moves state to the client: the token contains claims (user ID, roles, expiration) signed by the server. It scales better — no database lookup per request — but tokens cannot be individually revoked before expiration, creating a window of vulnerability if stolen.

Authorization models break down into **RBAC (Role-Based Access Control)** and **ABAC (Attribute-Based Access Control)**. RBAC assigns permissions to roles, and users inherit permissions through role membership: "admins can delete posts." It is simple to understand and audit. ABAC evaluates policies against attributes of the user, resource, and environment: "users can edit posts they authored, during business hours, from corporate IP ranges." ABAC is more expressive but harder to reason about and slower to evaluate. Most systems start with RBAC and add ABAC only when necessary.

**OAuth 2.0** is an authorization framework, not an authentication protocol — though it is commonly (mis)used for both. It solves the "login with Google" problem: a user grants a third-party application limited access to their resources without sharing their password. The application receives an access token scoped to specific permissions. **OpenID Connect (OIDC)** adds an identity layer on top of OAuth 2.0, providing a standardized way to authenticate users via third-party providers. Without OIDC, OAuth 2.0 alone does not tell you who the user is — only that they granted access.

**API Keys** are for machine-to-machine authentication. They identify the calling application, not a human user. API keys should be treated as secrets (stored in environment variables, never committed), rotated regularly, and scoped to specific permissions. Unlike user session tokens, API keys often have long or no expiration — making them high-value targets if leaked.

## How It Works

### Session-Based Authentication Flow

1. User submits credentials (username/password) to `/login`
2. Server validates credentials against database (bcrypt/Argon2 hash comparison)
3. Server creates session record in Redis/memory with UUID session ID, user ID, expiration
4. Server sets `HttpOnly; Secure; SameSite=Strict` cookie containing session ID
5. On subsequent requests, server reads cookie, looks up session in store, retrieves user context
6. On logout, server deletes session from store; cookie becomes invalid

### JWT-Based Authentication Flow

1. User submits credentials to `/login`
2. Server validates credentials, generates JWT containing: header (alg), payload (sub, exp, roles), signature (HMAC/RSA)
3. Server returns JWT to client (stored in memory or localStorage — trade-offs below)
4. Client sends JWT in `Authorization: Bearer <token>` header on each request
5. Server verifies signature (no DB lookup), extracts claims, enforces authorization
6. Token expires naturally; refresh token (longer-lived, single-use) can mint new access tokens

### JWT Structure

```
eyJhbGciOiJIUzI1NiJ9.  <- header (base64url)
eyJzdWIiOiJ1c2VyMTIzIiwiaWF0IjoxNTE2MjM5MDIyfQ.  <- payload (base64url)
SflKxwRJSMeKKF2QT4fwpMe...  <- signature (HMAC-SHA256)
```

**Critical security properties:**
- JWTs are signed, not encrypted — anyone can read the payload; the signature prevents tampering
- Use RS256 (RSA) when multiple services need to verify tokens (public key distribution); HS256 (HMAC) for single-service scenarios
- Short expiration (15 minutes typical) limits exposure window; refresh tokens enable long sessions without long-lived access tokens

### OAuth 2.0 Authorization Code Flow

```
User          Client App         Auth Server (Google)      Resource Server
  |                |                       |                       |
  |--click login-->|- - redirect to auth - >|                       |
  |<--consent page--| (user authenticates)  |                       |
  |--authorize----->|                       |                       |
  |                |<-----auth code--------|                       |
  |                |--exchange code + secret->|                     |
  |                |<----access token--------|                     |
  |                |---API call with token-------------------------->|
  |                |<----user data-----------------------------------|
```

The authorization code flow protects tokens from exposure: the access token is exchanged server-to-server, never visible to the browser. PKCE (Proof Key for Code Exchange) extends this for mobile/SPAs where the client cannot keep a secret.

### RBAC vs ABAC

**RBAC Example:**
```
Roles: admin, editor, viewer
Permissions: read_post, write_post, delete_post
Role-Permission Mapping:
  admin -> read_post, write_post, delete_post
  editor -> read_post, write_post
  viewer -> read_post
User-Role Assignment:
  alice -> admin
  bob -> editor
```

**ABAC Example:**
```
Policy: Allow write_post IF
  user.department == resource.department AND
  user.seniority >= 3 AND
  time.hour BETWEEN 9 AND 18 AND
  request.ip IN corporate_ip_range
```

### Storage Security: Cookies vs localStorage

| Storage                 | XSS Risk     | CSRF Risk    | Automatic Sending   | Size Limit   || ----------------------- | ------------ | ------------ | ------------------- | ------------ || Cookie (HttpOnly)       | Protected    | Vulnerable   | Yes                 | ~4KB         || Cookie (non-HttpOnly)   | Vulnerable   | Vulnerable   | Yes                 | ~4KB         || localStorage            | Vulnerable   | Protected    | No (must be JS)     | ~5-10MB      || Memory only             | Protected    | Protected    | No                  | Limited      |
**Recommendation:** Store access tokens in memory (short-lived), refresh tokens in HttpOnly cookies. This balances XSS protection with CSRF mitigation via SameSite cookies.

### Password Hashing

Never store passwords in plaintext. Use adaptive hashing algorithms designed to be slow:

- **bcrypt**: Industry standard, tunable cost factor, 72-byte input limit
- **Argon2**: Winner of Password Hashing Competition (2015), resistant to GPU attacks
- **PBKDF2**: Older, NIST-approved, but weaker against parallel attacks

Cost factors should target ~100-500ms per hash on production hardware — slow enough to deter brute force, fast enough for legitimate logins.

### Trade-offs

| Approach                 | Pros                                                     | Cons                                                                           | Best For                                  || ------------------------ | -------------------------------------------------------- | ------------------------------------------------------------------------------ | ----------------------------------------- || Session + Cookie         | Instant revocation; simple; HttpOnly protects from XSS   | Stateful; Redis lookup per request; session affinity                           | Traditional web apps; admin panels        || JWT in memory            | Stateless; no DB lookup; scales horizontally             | Cannot revoke instantly; XSS exposure if leaked; no automatic cookie sending   | SPAs; mobile apps; microservices          || JWT in HttpOnly cookie   | XSS protected; automatic sending; stateless              | CSRF risk (mitigate with SameSite); size limits (~4KB)                         | SPAs with SSR; same-domain APIs           || OAuth 2.0 + OIDC         | Delegated auth; SSO; no password storage                 | Complexity; dependency on external provider; token management overhead         | Consumer apps; third-party integrations   || API Keys                 | Simple for M2M                                           | No user context; long-lived risk; key rotation pain                            | Service-to-service; partner APIs          |
### Failure Modes

**Token theft without detection:** JWTs cannot be revoked individually. If an attacker steals a valid JWT, they can use it until expiration. Mitigations: short expiration (15min), binding tokens to device fingerprint or IP, maintaining a "blocklist" of compromised token JTI (JWT ID) claims in Redis.

**CSRF attacks on cookie-based auth:** A malicious site tricks a user's browser into making authenticated requests to your API using stored cookies. Mitigations: SameSite=Strict or Lax cookies, CSRF tokens for state-changing operations, validating Origin/Referer headers.

**XSS token exfiltration:** If tokens are stored in localStorage or non-HttpOnly cookies, injected JavaScript can steal them. Mitigations: HttpOnly cookies, Content Security Policy (CSP), input sanitization, output encoding.

**Refresh token rotation failure:** Refresh tokens should be single-use — when exchanged for a new access token, a new refresh token is issued and the old one invalidated. If refresh tokens are reusable, stolen refresh tokens grant indefinite access.

**Algorithm confusion attacks:** A JWT header specifies the algorithm (`alg: "none"` or `alg: "HS256"`). An attacker can modify the algorithm to bypass verification. Mitigations: explicitly specify allowed algorithms in verification, reject `alg: "none"`, use strong typing in JWT libraries.

**OAuth scope escalation:** A client requests broader permissions than necessary, and users blindly approve. The client now has more access than intended. Mitigations: principle of least privilege in scope design, audit scopes during security reviews.

**Session fixation:** An attacker obtains a valid session ID and tricks a victim into using it. After victim login, attacker uses the same session ID. Mitigations: regenerate session ID on privilege escalation (login), bind sessions to IP/user-agent fingerprint.

**Timing attacks on password comparison:** Naive string comparison (`password == hash`) leaks timing information about prefix matches. Mitigations: use constant-time comparison functions provided by crypto libraries.

## Interview Talking Points

- "Authentication establishes identity; authorization determines permissions. Never conflate them — a system can authenticate you yet deny authorization for a specific resource."
- "Sessions scale poorly but revoke instantly; JWTs scale well but cannot be revoked before expiration. The sweet spot is short-lived JWTs (15min) with refresh tokens stored in HttpOnly cookies."
- "OAuth 2.0 is for authorization, not authentication. If you need to know who the user is, layer OpenID Connect on top — otherwise you're misusing OAuth."
- "RBAC is simple and audit-friendly: roles map to permissions, users map to roles. ABAC is more expressive but harder to reason about — start with RBAC and add ABAC only when you need dynamic policies."
- "Store tokens in HttpOnly cookies to prevent XSS exfiltration, but mitigate CSRF with SameSite=Strict and origin validation. Never store sensitive tokens in localStorage."
- "Password hashing must be adaptive and slow — bcrypt with cost 12, or Argon2id. The goal is to make brute force prohibitively expensive even if the database is stolen."
- "API keys identify applications, not users. They're long-lived secrets that should rotate regularly and be scoped to specific permissions — never commit them to version control."

---

## Hands-on Lab

**Time:** ~25–35 minutes
**Services:** auth-service (Flask + Redis), protected-api (Flask)

### Setup

```bash
cd system-design-interview/01-foundations/15-authn-authz/
docker compose up -d
# Wait ~10 seconds for services to initialize
```

### Experiment

```bash
python experiment.py
```

The experiment demonstrates five authentication patterns:

1. **Session-based auth** — login creates server-side session; cookie-based request authentication; logout invalidates session
2. **JWT-based auth** — login returns signed JWT; stateless verification; expiration handling
3. **Role-based access control (RBAC)** — admin vs user role permissions; 403 for unauthorized actions
4. **API key authentication** — M2M auth with scoped permissions; header-based key validation
5. **OAuth 2.0 flow simulation** — authorization code exchange; token retrieval; protected resource access

### Break It

Test token expiration by modifying the JWT expiry in `docker-compose.yml` to 5 seconds, restart, and observe automatic refresh behavior:

```bash
docker compose down
docker compose up -d
python experiment.py --slow  # adds delays to trigger expiration
```

### Observe

- Session-based: Session ID in cookie; Redis lookup on each request; immediate invalidation on logout
- JWT-based: No Redis lookup; signature verification only; token remains valid until expiry even after "logout"
- RBAC: Admin endpoints return 403 for non-admin users; role extracted from token claims
- API keys: Different keys have different scopes; invalid key returns 401

### Teardown

```bash
docker compose down -v
```

---

## Real-World Examples

- **GitHub:** Uses session cookies for the web interface, JWTs for API access, and OAuth 2.0 for third-party app integrations. Personal access tokens provide long-lived API keys with granular scopes — source: GitHub API documentation
- **Auth0 / Okta:** Identity-as-a-service platforms that abstract OAuth 2.0 + OIDC complexity. They handle token issuance, refresh rotation, and social login providers — source: Auth0 architecture documentation
- **AWS IAM:** Sophisticated ABAC system where policies evaluate user attributes, resource tags, request context (time, IP), and environmental conditions. Most real-world systems don't need this complexity initially — source: AWS IAM documentation
- **Stripe:** API keys with publishable (client-side, restricted) and secret (server-side, full access) variants. Keys can be restricted to specific IP addresses and have separate test/live environments — source: Stripe API documentation

## Common Mistakes

- **Using JWTs like sessions.** Attempting to implement "logout" by deleting client-side JWTs — the token remains valid server-side until expiration. True logout requires a blocklist, undermining JWT's stateless benefit.
- **Storing sensitive data in JWT payloads.** JWTs are base64-encoded, not encrypted. Anyone can read the payload; only the signature prevents tampering. Never put PII or secrets in JWT claims.
- **Not validating token expiration.** A missing `exp` claim or disabled verification creates indefinite-lived tokens — a security vulnerability if leaked.
- **Rolling your own crypto.** Implementing custom JWT signing, password hashing, or session generation instead of using battle-tested libraries. Subtle bugs in crypto implementation have catastrophic consequences.
- **Missing authorization checks.** Authenticating the user but not verifying they own the resource (e.g., `DELETE /posts/123` succeeds because user is logged in, not because they authored post 123).
- **Overly permissive CORS.** Configuring `Access-Control-Allow-Origin: *` on authenticated endpoints allows malicious sites to make authenticated cross-origin requests with stolen tokens.
- **Committing API keys to version control.** Keys in Git are discoverable forever. Use environment variables, secret managers (AWS Secrets Manager, Vault), or .env files excluded from git.
- **Infinite refresh token lifetimes.** Refresh tokens that never expire create permanent backdoors if stolen. Implement refresh token expiration and rotation.
