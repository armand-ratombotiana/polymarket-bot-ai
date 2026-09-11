"""
core/security.py — Security helpers (W11-6 — OWASP Top 10 hardening).

Houses the W11-6 token-strength validator and the W11-6 SSRF guard
hostname-allowlist helper. Pure functions: no module-level singletons, no
side effects, no project-module imports — so this module is safe to import
from ``api/server.py`` AND from the test suite without dragging in the full
server startup graph.
"""
from __future__ import annotations

from typing import Final
from urllib.parse import urlparse

# ── Generic / default tokens that must NEVER be accepted as a real API token ─
# A short, hand-curated blocklist of placeholders that have historically shown
# up in `.env` files committed to public repos. None of these are strong —
# they exist purely so ``validate_token_strength`` can fail-closed when an
# operator forgets to replace the placeholder.
_GENERIC_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "change_me",
        "changeme",
        "secret",
        "password",
        "test",
        "test-token",
        "test-token-conftest",  # the conftest default — never valid in prod
        "token",
        "api_token",
        "your_token_here",
        "your-secret-token",
        "replace-me",
        "REPLACE_ME",
        "default",
        "example",
        "demo",
        "todo",
        "TODO",
    }
)

# Minimum acceptable length. 32 chars × 6 bits/char ≈ 192 bits of entropy —
# comfortably above the 128-bit OWASP A07 threshold and the 160-bit NIST
# SP 800-63B recommendation for a long-lived shared secret. Tokens shorter
# than this are rejected without further checks (length is the cheapest
# strong signal — no need to compute entropy on a 4-char string).
MIN_TOKEN_LENGTH: Final[int] = 32

# Minimum distinct characters. A 32-char token of all `a`s has plenty of
# length but ~0.4 bits/char of entropy — this catches that degenerate case.
MIN_UNIQUE_CHARS: Final[int] = 10

# Reasonable upper bound — guards against pathological inputs (e.g. a 10 MB
# paste from a misconfigured secrets manager). 1024 chars × 6 bits ≈ 6144
# bits, far past the point of diminishing returns.
MAX_TOKEN_LENGTH: Final[int] = 1024


def validate_token_strength(token: str | None) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for a candidate API bearer token.

    The OWASP A07 (Identification and Authentication Failures) guidance is:
    * Require a minimum length (we use 32 chars).
    * Reject default / guessable / placeholder values.
    * Reject low-entropy strings (we use a simple unique-character heuristic).

    This is a HEURISTIC strength check, not a cryptographic one — the actual
    token-vs-expected comparison still happens in
    ``api/server.py::_valid_token`` via ``hmac.compare_digest``. The job of
    this function is to refuse to START the server (or to log a loud
    warning) when ``API_TOKEN`` is the kind of value that gets a project on
    the front page of a "leaked .env files" list.

    Args:
        token: the candidate token (typically ``settings.api_token``).
            ``None`` / empty string are treated as "not configured".

    Returns:
        ``(True, "OK")`` when the token passes all checks.
        ``(False, "<human-readable reason>")`` otherwise. The reason is
        safe to log and surface to the operator; it does NOT contain the
        token itself.
    """
    if not token or not token.strip():
        return False, "Token is empty — set a strong API_TOKEN in .env"
    if len(token) < MIN_TOKEN_LENGTH:
        return (
            False,
            f"Token must be at least {MIN_TOKEN_LENGTH} characters "
            f"(got {len(token)} — see OWASP A07)",
        )
    if len(token) > MAX_TOKEN_LENGTH:
        return (
            False,
            f"Token is {len(token)} chars — exceeds the {MAX_TOKEN_LENGTH}-char "
            f"sanity ceiling (likely a misconfigured secrets manager)",
        )
    if token in _GENERIC_TOKENS:
        return False, "Token is a known generic placeholder — replace it with a strong secret"
    # Low-entropy guard: ``aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`` (34 chars, 1
    # unique) would otherwise pass the length check. 10 unique chars is the
    # floor a randomly-generated 32-char base64 token always beats (~50+
    # unique chars in practice).
    unique_chars = len(set(token))
    if unique_chars < MIN_UNIQUE_CHARS:
        return (
            False,
            f"Token has low entropy (only {unique_chars} distinct characters — "
            f"minimum {MIN_UNIQUE_CHARS} required; see OWASP A07)",
        )
    return True, "OK"


# ── SSRF guard (W11-6 — OWASP A10) ──────────────────────────────────────────
# The polymarket-bot's only outbound HTTP calls go to the configured Polymarket
# CLOB / Gamma / Data hosts (read from ``settings.poly_*_host`` at startup).
# No route handler accepts a user-supplied URL for the bot to fetch — but if
# one ever does, it MUST first be vetted by ``is_safe_external_url``. The
# allowlist is intentionally narrow: only HTTPS to the configured Polymarket
# hosts. Anything else (http://, file://, gopher://, an internal RFC1918 IP,
# a metadata-service IP like 169.254.169.254) is rejected.
_BLOCKED_URL_SCHEMES: Final[frozenset[str]] = frozenset(
    {"file", "ftp", "gopher", "dict", "ldap", "ldaps", "tftp", "sftp"}
)
_BLOCKED_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "169.254.169.254",  # AWS / GCP / Azure metadata service
        "metadata.google.internal",
        "fd00:ec2::254",  # AWS IMDSv6
        "0.0.0.0",
        "localhost",
    }
)
_BLOCKED_HOST_PREFIXES: Final[tuple[str, ...]] = (
    "127.",
    "10.",
    "192.168.",
    "169.254.",
    "100.64.",  # CGNAT
)
_POLY_ALLOWED_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "clob.polymarket.com",
        "gamma-api.polymarket.com",
        "data-api.polymarket.com",
        "ws-subscriptions-clob.polymarket.com",
    }
)


def is_safe_external_url(url: str) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for an outbound URL the bot is about to fetch.

    Per OWASP A10 (SSRF), the bot must NEVER fetch a URL that:
      * is not HTTPS,
      * points to a host outside the explicit allowlist (the configured
        Polymarket endpoints),
      * uses a blocked scheme (file://, gopher://, …),
      * resolves to a link-local / private / loopback / metadata IP.

    The allowlist strategy is the OWASP-recommended default-deny posture:
    rather than enumerating every blocked host (impossible — new metadata
    services appear constantly), we ONLY allow the four Polymarket hosts
    the bot legitimately talks to.

    This is a defensive helper. The bot's existing call sites
    (``core/gamma_client.py``, ``core/clob_client.py``,
    ``core/market_discovery.py``) all use ``settings.poly_*_host`` at
    construction time — never user input — so this function has ZERO
    callers in the current codebase. It exists so a future route that
    accepts a URL parameter has a vetted guard to reach for instead of
    re-implementing the check inline.
    """
    if not url or not isinstance(url, str):
        return False, "URL is empty"
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError) as e:
        return False, f"URL parse failed: {e}"
    scheme = (parsed.scheme or "").lower()
    if scheme != "https":
        return False, f"External URL must use HTTPS (got scheme={scheme!r})"
    if scheme in _BLOCKED_URL_SCHEMES:
        return False, f"Blocked URL scheme: {scheme}"
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "URL has no host component"
    if host in _BLOCKED_HOSTS:
        return False, f"Blocked host (metadata/loopback): {host}"
    if any(host.startswith(prefix) for prefix in _BLOCKED_HOST_PREFIXES):
        return False, f"Blocked host (private/loopback IP range): {host}"
    # IPv6 loopback / link-local / unique-local — strip zone identifier and brackets.
    bare_host = host.strip("[]").split("%")[0]
    if bare_host in ("::1", "::") or bare_host.startswith("fe80:") or bare_host.startswith("fc"):
        return False, f"Blocked host (IPv6 loopback / link-local / ULA): {bare_host}"
    if bare_host not in _POLY_ALLOWED_HOSTS:
        return False, f"Host {host!r} not in the Polymarket allowlist"
    return True, "OK"


# ── Sensitive-header redaction helper (W11-6 — OWASP A02) ──────────────────
# Used by the request-logging middleware and the audit-logger adapter to
# ensure a leaked Authorization header never ends up in a log file. The
# redaction is aggressive: only the first 8 chars are surfaced (so an
# operator can correlate "which token was used" without exposing it),
# followed by ``...REDACTED``.
_HEADER_REDACTION_PREFIX_LEN: Final[int] = 8


def redact_authorization_header(value: str | None) -> str:
    """Return a log-safe rendering of an ``Authorization`` header value.

    ``Bearer <token>`` becomes ``Bearer <first8>...REDACTED``; a malformed
    value (no scheme / empty) becomes ``<REDACTED>`` so we never accidentally
    surface a partial token in a log line.
    """
    if not value:
        return "<empty>"
    # ``partition`` returns ("", "", "") for an empty string and
    # ("value", "", "") for a value with no space — both render as
    # ``<REDACTED>`` which is the safe default.
    scheme, sep, creds = value.partition(" ")
    if not sep or not creds:
        return "<REDACTED>"
    if len(creds) <= _HEADER_REDACTION_PREFIX_LEN:
        return f"{scheme} <REDACTED>"
    return f"{scheme} {creds[:_HEADER_REDACTION_PREFIX_LEN]}...REDACTED"


# ── Token generation helper (W11-6 — OWASP A07) ────────────────────────────
# Used by the security docs and the test suite as the canonical "this is how
# you generate a new API token" recipe. ``secrets.token_urlsafe(32)`` yields
# 32 bytes of cryptographic randomness (256 bits) rendered as ~43 URL-safe
# base64 chars — comfortably above ``MIN_TOKEN_LENGTH`` and with the full
# ``string.printable`` entropy budget.
def generate_strong_token(byte_length: int = 32) -> str:
    """Generate a cryptographically-strong API token.

    Wraps ``secrets.token_urlsafe`` so the canonical generation recipe lives
    in one place; production tokens should be created via this helper (or
    via an equivalent ``secrets.token_urlsafe(32)`` invocation in the
    operator's shell — both produce the same kind of value).
    """
    if byte_length < 32:
        raise ValueError(f"byte_length={byte_length} is below the 32-byte OWASP minimum")
    import secrets

    return secrets.token_urlsafe(byte_length)


__all__ = [
    "MAX_TOKEN_LENGTH",
    "MIN_TOKEN_LENGTH",
    "MIN_UNIQUE_CHARS",
    "generate_strong_token",
    "is_safe_external_url",
    "redact_authorization_header",
    "validate_token_strength",
]
