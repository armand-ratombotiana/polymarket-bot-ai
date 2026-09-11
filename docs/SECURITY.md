# Security Policy

This document is the canonical security reference for the **Polymarket Pro
Bot** (the FastAPI service in `mini-services/polymarket-bot/`) and its
Next.js frontend host. It documents the OWASP Top 10 (2021) compliance
status, the authentication mechanism, the CORS / rate-limit / security-
header posture, the logging & monitoring approach, and the incident-
response / responsible-disclosure procedures.

This file is owned by the platform security task (W11-6) and is updated
whenever the security posture changes. Last reviewed: **W11-6**.

---

## 1. OWASP Top 10 (2021) Compliance Status

| OWASP Category | Status | Where Enforced |
|---|---|---|
| **A01 — Broken Access Control** | ✅ Compliant | `api/server.py::enforce_api_auth` (fail-closed bearer-token middleware on every route except `/api/health`); comparison via `hmac.compare_digest` (constant-time). |
| **A02 — Cryptographic Failures** | ✅ Compliant | API token never logged in plaintext (`request_logging_middleware` logs only method/path/status/latency); `core.security.redact_authorization_header` provides a log-safe helper; production TLS terminated by Caddy (`Caddyfile.prod`). |
| **A03 — Injection (SQL)** | ✅ Compliant | All SQLite/Postgres queries use parameterized `?` / `$1` placeholders. The `table` param to `/api/database/records` is whitelist-validated against `_TABLES` before interpolation. Verified by `tests/test_security.py::TestSQLInjection`. |
| **A04 — Insecure Design** | ✅ Compliant | Rate limiting (W10-4 — slowapi, 4-tier policy: 120/min read, 30/min write, 5/min heavy, 20/min trade); input validation on every route (`Query(ge=…, le=…)`, Pydantic models); global exception handler returns sanitized 500 (`{"detail": "Internal server error"}`). |
| **A05 — Security Misconfiguration** | ✅ Compliant (post W11-6) | `CORS_ORIGINS=*` removed from `.env` and replaced with explicit allowlist; CORS `allow_methods` tightened to `[GET, POST, PUT, DELETE, OPTIONS]`; `security_headers_middleware` adds `X-Content-Type-Options`, `X-Frame-Options`, `X-XSS-Protection`, `Referrer-Policy`, `Content-Security-Policy` to every response; debug/docs endpoints (`/docs`, `/redoc`, `/openapi.json`) stripped from `PUBLIC_PATHS` when `trading_mode=live`. |
| **A06 — Vulnerable & Outdated Components** | ⚠️ Best-effort | `requirements.txt` pins all dependencies to compatible-release ranges (`>=X,<Y`); operators should run `pip-audit` / `safety check` periodically. No known CVEs in pinned versions as of W11-6. |
| **A07 — Identification & Authentication Failures** | ✅ Compliant (post W11-6) | `core.security.validate_token_strength` rejects empty / short (<32 chars) / generic-placeholder / low-entropy (<10 unique chars) tokens; the check runs at server startup (warn-only — the auth middleware still fails-closed on empty tokens) and is exercised by `tests/test_security.py::TestTokenStrengthValidator`. |
| **A08 — Software & Data Integrity Failures** | ⚠️ N/A | No auto-update mechanism in the bot; the operator controls deploy. Frontend `bun.lock` provides integrity for JS dependencies. |
| **A09 — Security Logging & Monitoring Failures** | ✅ Compliant (post W11-6) | `core.audit_logger` writes immutable SQLite rows for every trading / risk / system event; W11-6 added `category='security', event_type='auth_failure'` rows for every 401 (with `mode=missing` vs `mode=invalid` so operators can distinguish misconfigured clients from brute-force attempts); W10-7 alerting engine surfaces bursts of 401s / rate-limit hits to the operator dashboard. |
| **A10 — Server-Side Request Forgery (SSRF)** | ✅ Compliant | The bot only calls the configured Polymarket CLOB / Gamma / Data hosts (`settings.poly_*_host` — never user input). `core.security.is_safe_external_url` provides a default-deny hostname allowlist for any future route that accepts a URL. The Caddy reverse proxy's `?XTransformPort=` query is documented as an operator-only knob (dev Caddyfile) and is removed from `Caddyfile.prod`. |

---

## 2. Authentication Mechanism

### Bearer token (HTTP)

Every API route — except the liveness probe `/api/health` and the CORS
preflight (`OPTIONS` method) — requires an `Authorization: Bearer <token>`
header. The token is compared against `settings.api_token` using
`hmac.compare_digest` (constant-time, prevents timing-side-channel
enumeration).

```python
def _valid_token(authorization: str | None) -> bool:
    if not settings.api_token:
        return False  # fail-closed (503 AUTH_NOT_CONFIGURED)
    scheme, _, creds = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not creds:
        return False
    return hmac.compare_digest(creds, settings.api_token)
```

**Fail-closed posture.** If `API_TOKEN` is unset, every authenticated
route returns HTTP 503 with `{"detail": "API authentication not
configured — set API_TOKEN in .env", "code": "AUTH_NOT_CONFIGURED"}` —
the server boots, but no data is exposed until the operator configures
a strong token.

### WebSocket token

The `/ws` endpoint reads the token from the `?token=` query parameter
and compares it via the same `hmac.compare_digest` call. If no token is
configured, the upgrade is rejected with code 4401 (fail-closed).

---

## 3. Token Management

### Generation

A strong API token can be generated with the in-tree helper:

```bash
cd mini-services/polymarket-bot
python3 -c "from core.security import generate_strong_token; print(generate_strong_token())"
# → e.g. "TdzKAAqW-EoLpX8-iUZa2MTw3le18wT78wT78wT78wT78wT78wT78wT78wT"
```

`generate_strong_token(32)` produces 32 bytes (256 bits) of
cryptographic randomness via `secrets.token_urlsafe`, rendered as ~43
URL-safe base64 chars — well above the 32-char minimum and the
10-unique-char entropy floor enforced by `validate_token_strength`.

### Strength validation

`core.security.validate_token_strength(token)` is called at server
startup (in the `lifespan` context manager). It rejects:

* Empty / whitespace-only tokens (`"Token is empty"`).
* Tokens shorter than 32 chars (`"Token must be at least 32 characters"`).
* Tokens longer than 1024 chars (defensive ceiling).
* Generic placeholders (`change_me`, `secret`, `password`, `test`,
  `test-token-conftest`, …) from a curated blocklist.
* Low-entropy tokens (fewer than 10 distinct characters).

If the check fails, the server still starts (the auth middleware
already fails-closed on empty tokens) but logs a `WARNING` and writes
a `category='security', event_type='weak_token_warning'` audit event.

### Rotation

The token is read once from the `API_TOKEN` env var at process start.
To rotate:

1. Generate a new strong token (see above).
2. Update `mini-services/polymarket-bot/.env` (and `/home/z/my-project/.env`
   for `NEXT_PUBLIC_API_TOKEN` if the frontend reads it).
3. Restart the bot: `POST /api/bot?action=restart` (or kill + spawn).
4. Verify: `curl -H "Authorization: Bearer <new>" http://localhost:8080/api/status`
   returns 200.

There is no in-process rotation API: the token is a deployment-time
secret, not a runtime credential. (If a runtime-rotation API is needed
in the future, it would have to read the env var via a re-load + atomic
swap of the `settings.api_token` attribute under a lock — out of scope
for W11-6.)

### Revocation

To revoke a compromised token immediately:

1. Set `API_TOKEN=` (empty) in `.env` and restart the bot. The auth
   middleware immediately starts returning 503 on every authenticated
   route — no requests can succeed until a new token is set.
2. Generate a new strong token and update `.env`.
3. Restart again.

The audit trail (`category='security', event_type='auth_failure'`)
captures every rejected request during the gap, so operators can
post-mortem any unauthorized access attempts.

---

## 4. CORS Policy

Configured via the `CORS_ORIGINS` env var in
`mini-services/polymarket-bot/.env`. The value is a comma-separated list
of explicit origins — **no wildcard `*` is accepted** (removed in
W11-6 per OWASP A05).

### Default (production-ready)

```
CORS_ORIGINS=http://localhost:3000,http://localhost:3010,http://127.0.0.1:3000,http://127.0.0.1:3010
```

Add your production origin (e.g. `https://yourdomain.com`) before
deploying.

### Allowed methods

Only `GET`, `POST`, `PUT`, `DELETE`, `OPTIONS` are reflected in CORS
preflight responses. `TRACE`, `CONNECT`, `PATCH`, and other verbs are
NOT allowed.

### Credentials

`allow_credentials=True` — the frontend sends the `Authorization`
header, so credentials must be allowed. This is safe because no
wildcard origin is permitted (a credentialed wildcard would be a
security hole; an explicit allowlist with credentials is the OWASP-
approved posture).

### Origin reflection

The `enforce_api_auth` middleware explicitly checks the `Origin`
header against the `cors_origin_list`. If the origin is not in the
allowlist, NO `Access-Control-Allow-Origin` header is reflected —
the browser blocks the cross-origin request.

---

## 5. Rate Limiting

Implemented in W10-4 via `slowapi` (see `api/rate_limit.py` for the
shared `Limiter` singleton). Four tiers:

| Tier | Limit | Routes |
|---|---|---|
| READ | 120/minute | `GET /api/health`, `/api/status`, `/api/snapshot`, `/api/markets`, `/api/orderbooks`, `/api/positions` |
| WRITE | 30/minute | (applied selectively — most writes use TRADE_LIMIT) |
| HEAVY | 5/minute | `POST /api/ml/retrain`, `/api/ml/learn`, `/api/backtest/run`, `/api/kill-switch/activate`, `/api/kill-switch/deactivate` |
| TRADE | 20/minute | `POST /api/trade`, `DELETE /api/orders`, `DELETE /api/orders/{id}`, `POST /api/positions/{token_id}/close` |
| ARBITRAGE | 10/minute | `POST /api/arbitrage/execute` |
| LIVE_ENABLE | 3/minute | `POST /api/live/enable` |

Every response carries an informational `X-RateLimit-Policy` header:
`120/min read, 30/min write, 5/min heavy`. When a limit is exceeded,
the response is HTTP 429 with body `{"detail": "Rate limit exceeded",
"retry_after": N}` and headers `Retry-After: N` and
`X-RateLimit-Limit: <amount>/<granularity>`.

The limiter is keyed on the client IP via `get_remote_address`. In
tests it's disabled via `limiter.enabled = False` (see
`tests/conftest.py`).

---

## 6. Security Headers

Added by `security_headers_middleware` (W11-6, OWASP A05). Every
response — 200, 4xx, 5xx, OPTIONS preflight — carries:

| Header | Value | Purpose |
|---|---|---|
| `X-Content-Type-Options` | `nosniff` | Blocks MIME-type sniffing. |
| `X-Frame-Options` | `DENY` | Blocks clickjacking via framing. |
| `X-XSS-Protection` | `1; mode=block` | Legacy reflected-XSS filter (older browsers). |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | Strips path/query from Referer on cross-origin nav. |
| `Content-Security-Policy` | `default-src 'self'` | Only same-origin resources may load (dashboard is fully same-origin). |

**Not added by the bot** (must be terminated by the reverse proxy):
* `Strict-Transport-Security` — must be set by Caddy (or whatever TLS-
  aware proxy terminates HTTPS) so it only ships over an actual HTTPS
  connection (otherwise an active MITM could inject it into a plain-
  HTTP response and pin the client).

The production `Caddyfile.prod` terminates TLS via Let's Encrypt and
should be configured to add the HSTS header at the proxy layer.

---

## 7. Logging and Monitoring

### Audit trail (durable)

`core.audit_logger.AuditLogger` writes immutable rows to a SQLite
database (`AUDIT_DB_PATH`, default `/app/data/audit_trail.db`).

Schema:

```sql
CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    category TEXT NOT NULL,        -- 'trading' | 'risk' | 'system' | 'security' | ...
    event_type TEXT NOT NULL,      -- 'order_placed' | 'kill_switch_activated' | 'auth_failure' | ...
    token_id TEXT,
    slug TEXT,
    details TEXT NOT NULL,         -- human-readable summary
    pnl REAL DEFAULT 0.0,
    strategy TEXT,
    idempotency_key TEXT UNIQUE    -- dedup key
);
```

**Security-relevant event types** (added in W11-6):

| `event_type` | When | `details` example |
|---|---|---|
| `auth_failure` | Every 401 response | `mode=invalid ip=10.0.0.5 path=/api/status method=GET` |
| `auth_failure` | Missing Authorization header | `mode=missing ip=10.0.0.5 path=/api/status method=GET` |
| `weak_token_warning` | Server startup if `API_TOKEN` fails strength check | `reason=Token must be at least 32 characters mode=startup` |
| `mode_change` | Every trading-mode transition | `mode=paper paper_trade=True live_trading_enabled=False` |

**No sensitive data is ever persisted.** The `details` field records
the remote IP, the path, the method, and the failure mode — NEVER the
rejected Authorization header value.

### Server logs (ephemeral)

`request_logging_middleware` logs every request at INFO level:

```
[request] GET /api/status → 200 (0.045s)
[request] GET /api/status → 401 (0.012s)
```

Only method / path / status / latency — no headers, no body. Captured
to `mini-services/polymarket-bot/server.log` (rotated by operator
policy).

### Alerting (W10-7)

The `core.alerting` module evaluates threshold-based alert rules
against the audit trail and the live metrics stream. Alerts are
surfaced at `GET /api/alerts` and can be acknowledged via
`POST /api/alerts/{id}/acknowledge`. The default rule set includes:

* Auth-failure burst: >10 `auth_failure` events in 60s → CRITICAL.
* Rate-limit burst: >5 429 responses in 60s → WARNING.
* Kill-switch activated: → CRITICAL.

---

## 8. Incident Response Plan

### Severity classification

| Severity | Example | Response time |
|---|---|---|
| **P0 — Critical** | Compromised API token, live-trading malfunction, kill switch auto-triggered by daily loss limit | Immediate (on-call) |
| **P1 — High** | Sustained auth-failure burst (brute force), DB corruption, market-data feed stall >5 min | <1 hour |
| **P2 — Medium** | Single 500 error, missing security header on one route, rate-limit hit | <24 hours |
| **P3 — Low** | Documentation gap, dependency upgrade available | <1 week |

### P0 playbook (compromised API token)

1. **Revoke.** Set `API_TOKEN=` (empty) in `.env`, restart the bot
   (`POST /api/bot?action=restart`). Every authenticated route now
   returns 503 — no requests can succeed.
2. **Audit.** Query the audit trail for the compromised token's usage:
   ```bash
   sqlite3 /app/data/audit_trail.db \
     "SELECT * FROM audit_events WHERE category='security' AND event_type='auth_failure' ORDER BY timestamp DESC LIMIT 100"
   ```
3. **Generate new token.** `python3 -c "from core.security import
   generate_strong_token; print(generate_strong_token())"`.
4. **Update `.env`** with the new token and restart.
5. **Post-mortem.** Document the timeline, the root cause, and the
   remediation in `worklog.md` under a new "Incident" section.

### P0 playbook (live-trading malfunction)

1. **Halt trading.** `POST /api/kill-switch/activate` — every order
   path is blocked immediately. The durable kill-switch marker file
   survives restarts.
2. **Verify.** `GET /api/system/health` — the `kill_switch` check
   should read `BREACHED`.
3. **Investigate.** Pull recent trades via `GET /api/trades?limit=100`
   and recent audit events via `GET /api/audit/logs?limit=100`.
4. **Clear.** Once root cause is fixed: `POST /api/kill-switch/deactivate`.

---

## 9. Responsible Disclosure Policy

### Reporting a vulnerability

Email security@<your-domain> with:

* A description of the vulnerability.
* Steps to reproduce (PoC if possible).
* Affected version (run `GET /api/system/mode` to capture the trading
  mode + auth posture).
* Your name / handle for credit.

### Response timeline

* **Acknowledgement:** within 48 hours.
* **Initial assessment:** within 7 days.
* **Fix or mitigation:** within 90 days (faster for P0/P1).
* **Public disclosure:** coordinated with the reporter after the fix
  is deployed; we will NOT publish details until the reporter agrees
  or 90 days have elapsed (whichever comes first).

### Scope

**In scope:**

* The FastAPI service in `mini-services/polymarket-bot/`.
* The Next.js frontend in `src/`.
* The Caddy reverse-proxy config in `Caddyfile` / `Caddyfile.prod`.
* The bot's audit trail (`audit_events` SQLite DB).

**Out of scope:**

* Third-party Polymarket infrastructure (`clob.polymarket.com`,
  `gamma-api.polymarket.com`) — report to Polymarket directly.
* Vulnerabilities in dependencies that have already been disclosed
  to the upstream maintainer — please report those to the upstream
  project directly (we'll upgrade as soon as a fix is released).

### Safe harbor

We will NOT pursue legal action against security researchers who:

* Make a good-faith effort to avoid privacy violations, destruction of
  data, and interruption or degradation of services.
* Do not access data other than their own.
* Do not use social engineering or physical attacks against employees.
* Report vulnerabilities according to this policy.

---

## 10. Verification

The security posture is verified by the test suite in
`mini-services/polymarket-bot/tests/test_security.py` (79 tests,
grouped by OWASP category):

* `TestBrokenAccessControl` (A01) — 6 tests: 401 on missing/invalid
  token, 200 on valid token, malformed-header rejection, constant-
  time comparison (near-miss vs far-miss identical body + within
  timing tolerance).
* `TestCryptographicFailures` (A02) — 4 tests: token not echoed in
  401 body, token not logged in plaintext (caplog assertion), error
  messages don't leak stack traces, `redact_authorization_header`
  helper contract.
* `TestSQLInjection` (A03) — 11 parametrized tests: SQL-injection
  payloads as `token_id` path params and `category` query params
  don't alter executed SQL; `table` param is whitelist-validated.
* `TestSecurityMisconfiguration` (A05) — 16 tests: every security
  header present on 200 / 401 / 500 responses; CORS doesn't reflect
  arbitrary origins; CORS preflight works for allowlisted origins;
  debug endpoints are public only in paper mode.
* `TestTokenStrengthValidator` (A07) — 14 tests: rejects empty /
  short / generic / low-entropy tokens; accepts the configured API
  token; doesn't leak the token value in the reason string;
  `generate_strong_token` produces tokens the validator accepts.
* `TestSecurityLogging` (A09) — 2 tests: 401 (invalid token) and 401
  (missing header) each append a `category='security',
  event_type='auth_failure'` audit row with the correct `mode=`.
* `TestSSRFProtection` (A10) — 27 parametrized tests: `is_safe_external_url`
  rejects non-HTTPS / private-IP / metadata-service / non-allowlisted
  hosts; bot doesn't accept user-supplied URLs for outbound fetches.

### Running the tests

```bash
cd mini-services/polymarket-bot
python3 -m pytest tests/test_security.py -v
```

### Full suite (regression check)

```bash
cd mini-services/polymarket-bot
python3 -m pytest tests/ --tb=short -q
```

As of W11-6: **709 passed, 0 failed, 0 errors**.

### Lint

```bash
cd /home/z/my-project
bun run lint
# exit 0 — clean
```

---

## 11. File Index

| File | Purpose |
|---|---|
| `mini-services/polymarket-bot/api/server.py` | `enforce_api_auth` middleware, `security_headers_middleware`, `_audit_auth_failure`, lifespan token-strength check, CORS config. |
| `mini-services/polymarket-bot/core/security.py` | `validate_token_strength`, `is_safe_external_url`, `redact_authorization_header`, `generate_strong_token`. |
| `mini-services/polymarket-bot/core/audit_logger.py` | Durable SQLite audit trail (immutable rows). |
| `mini-services/polymarket-bot/api/rate_limit.py` | Shared `slowapi.Limiter` + policy constants (W10-4). |
| `mini-services/polymarket-bot/core/alerting.py` | Threshold-based alerting (W10-7). |
| `mini-services/polymarket-bot/.env` | `API_TOKEN`, `CORS_ORIGINS`, paths. **Must not be committed to a public repo.** |
| `mini-services/polymarket-bot/tests/test_security.py` | 79 OWASP-coverage tests. |
| `docs/SECURITY.md` | This file. |

---

## 12. Change Log

| Date | Change | Task |
|---|---|---|
| 2025-W11 | Initial OWASP Top 10 audit + fixes: removed `CORS_ORIGINS=*`, added `security_headers_middleware`, added `core.security` module, wired `_audit_auth_failure` into the 401 path, added token-strength startup check. | W11-6 |
| 2025-W10 | Rate limiting (slowapi, 4-tier), threshold-based alerting. | W10-4, W10-7 |
| 2025-W09 | Auth middleware hardened to fail-closed; global exception handler sanitizes 500s; input validation bounds on every route. | W9-8 |
| 2025-S12 | `CORS_ORIGINS` default narrowed to explicit allowlist (but `.env` still had `*` until W11-6). | S12 |
