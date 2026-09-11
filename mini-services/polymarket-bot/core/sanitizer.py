"""
core/sanitizer.py — Input sanitization utilities (W15-6 — final hardening).

The W11-6 security baseline (``core/security.py``) covers the OWASP Top 10
*server-side* enforcement layer: bearer-token auth, SSRF allowlist,
token-strength validation, header redaction. This module complements that
with **input sanitization** helpers for the few routes that accept
free-form string parameters (``token_id``, ``category``, ``query``,
``strategy_name``, ``reason``) so a future route handler can reach for a
vetted helper instead of re-implementing escape / validation logic inline.

These helpers are PURE: no module-level singletons, no side effects, no
project-module imports — so the module is safe to import from
``api/server.py`` AND from the test suite without dragging in the full
server startup graph (mirrors the contract of ``core/security.py``).

Why now (W15-6)?
~~~~~~~~~~~~~~~~~
The W11-6 audit confirmed every existing route either:

  * validates ``token_id`` via a dict ``.get()`` lookup (no SQL),
  * passes ``category`` / ``table`` / ``strategy_name`` through a
    whitelist + parameterized SQL placeholder (no string interpolation
    into the SQL text), or
  * round-trips a free-form ``query`` through an in-memory vector index
    (no shell, no SQL, no eval).

So there were no live injection vectors at audit time. The helpers below
exist so a future route that DOES take a free-form string parameter has a
vetted guard to reach for, and so the penetration-test suite
(``tests/test_penetration.py``) can import them to assert the contract
directly without coupling to the route layer.

Coverage
~~~~~~~~~
* ``sanitize_string``        — trim, length-cap, HTML-escape a free-form
                               string for log-safe / response-safe use.
* ``sanitize_token_id``      — enforce the ``^[a-zA-Z0-9_-]+$`` shape a
                               Polymarket token ID always has (rejecting
                               SQL / path-traversal / XSS payloads at the
                               schema boundary).
* ``sanitize_path``          — resolve-and-verify a file path is inside an
                               allowed base directory (defense against
                               ``../../etc/passwd`` traversal).
"""
from __future__ import annotations

import re
from html import escape
from pathlib import Path

# ── Regexes (compiled once at module import — re-use across every call) ──────
# Polymarket token IDs are ERC-1155 token IDs — long hex strings today, but
# the CLOB also accepts the underscore / hyphen forms the gamma API uses in
# its URL slugs. Anything outside this charset is either a SQL-injection
# payload (``'; DROP TABLE positions; --``), a path-traversal payload
# (``../../etc/passwd``), or an XSS payload (``<script>alert(1)</script>``).
# 200 chars is the sanity ceiling: real Polymarket token IDs are 70-80 chars
# of hex; a 200-char input is either an attacker probe or a misconfigured
# client (e.g. pasting a URL into a token field).
_TOKEN_ID_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_-]+$")
_TOKEN_ID_MAX_LENGTH: int = 200

# Default length cap for ``sanitize_string`` — keeps a runaway paste from
# consuming memory in a log line / response payload. 1000 chars is generous
# for human-typed reasons / queries / strategy names.
_DEFAULT_STRING_MAX_LENGTH: int = 1000


def sanitize_string(s: str, max_length: int = _DEFAULT_STRING_MAX_LENGTH) -> str:
    """Sanitize a free-form string input.

    Args:
        s: the candidate string. Non-string inputs (None, int, list, ...)
           are coerced to an empty string (defensive — a misconfigured
           client can submit a JSON number where a string was expected;
           raising would 500 the route, returning "" lets Pydantic's own
           type-validation surface the proper 422).
        max_length: hard ceiling on the returned length. Defaults to
           1000 chars (the longest reasonable human-typed reason / query
           / strategy description).

    Returns:
        A trimmed, length-capped, HTML-escaped string. Never raises.

    Why HTML-escape even though the API returns JSON (which the browser
    won't render as HTML)? Defense-in-depth: a future route might log
    the string into an HTML dashboard, or a bug in the JSON serializer
    might produce a ``text/html`` response. Escaping at the boundary
    means the string is safe regardless of where it ends up.
    """
    if not isinstance(s, str):
        return ""
    # Trim leading / trailing whitespace — surprising how often a stray
    # newline at the end of a CLI paste breaks a downstream regex.
    s = s.strip()
    # Length cap BEFORE escape so the escaped form doesn't balloon past
    # the cap (``<`` → ``&lt;`` is 4× the length).
    if len(s) > max_length:
        s = s[:max_length]
    # HTML-escape — converts ``<``, ``>``, ``&``, ``"``, ``'`` to their
    # entity forms. The JSON response serialiser will re-escape the
    # backslashes if needed (so the on-wire form is double-escaped), but
    # the log-rendered form (which uses ``str()``) is safe.
    return escape(s)


def sanitize_token_id(token_id: str) -> str:
    """Validate a Polymarket token ID format.

    A valid token ID matches ``^[a-zA-Z0-9_-]+$`` and is at most 200
    chars. Anything else raises ``ValueError`` — the route handler is
    expected to catch the exception and return 422.

    Args:
        token_id: the candidate token ID from a path / query / body
            parameter.

    Returns:
        The validated token ID (unchanged — no transformation, just
        validation).

    Raises:
        ValueError: if the token ID is empty, contains characters
           outside ``[a-zA-Z0-9_-]``, or exceeds 200 chars.
    """
    if not token_id or not isinstance(token_id, str):
        raise ValueError("Invalid token_id — must be a non-empty string")
    if not _TOKEN_ID_RE.match(token_id):
        raise ValueError(
            "token_id contains invalid characters "
            "(must match ^[a-zA-Z0-9_-]+$)"
        )
    if len(token_id) > _TOKEN_ID_MAX_LENGTH:
        raise ValueError(
            f"token_id too long ({len(token_id)} chars — max "
            f"{_TOKEN_ID_MAX_LENGTH})"
        )
    return token_id


def sanitize_path(path: str, allowed_base: str | None = None) -> str:
    """Validate a file path doesn't escape the allowed base directory.

    Args:
        path: the candidate file path (relative or absolute).
        allowed_base: the canonical allowed base directory. If provided,
           the resolved ``path`` MUST start with the resolved
           ``allowed_base`` — otherwise the path is rejected (path
           traversal detected). If ``None``, the path is resolved but
           not bounds-checked (the caller is responsible for the
           allowlist).

    Returns:
        The resolved (absolute, symlinks-followed) path as a string.

    Raises:
        ValueError: if ``path`` resolves outside ``allowed_base``.
        TypeError: if ``path`` is not a string or ``Path``.

    Notes:
        ``Path.resolve()`` follows symlinks — so a symlink attack (where
        an attacker creates a symlink inside the allowed base pointing
        outside) is also defeated: the resolved path is the symlink's
        TARGET, which won't start with the allowed base.
    """
    if not isinstance(path, (str, Path)):
        raise TypeError(
            f"sanitize_path: path must be str or Path, got {type(path).__name__}"
        )
    p = Path(path).resolve()
    if allowed_base is not None:
        base = Path(allowed_base).resolve()
        # ``Path.is_relative_to`` (3.9+) is the canonical check; the
        # ``startswith`` fallback supports older Pythons and handles the
        # edge case where ``base`` has a trailing slash.
        try:
            # ``is_relative_to`` returns ``True`` if ``p`` is the same as
            # or inside ``base``.
            if not p.is_relative_to(base):
                raise ValueError(
                    f"Path escapes allowed base: {path} (resolved: {p}, "
                    f"base: {base})"
                )
        except AttributeError:  # pragma: no cover — Python <3.9 fallback
            if not str(p).startswith(str(base)):
                raise ValueError(
                    f"Path escapes allowed base: {path} (resolved: {p}, "
                    f"base: {base})"
                ) from None
    return str(p)


__all__ = [
    "sanitize_path",
    "sanitize_string",
    "sanitize_token_id",
]
