"""
tests/test_config.py — Unit tests for `config.py` (`Settings`).

Covers the nine behaviours required by V9:
  (1) `Settings` loads values from a `.env` file (via the
      `SettingsConfigDict(env_file=".env")` declaration on the model).
  (2) `has_credentials` returns `False` when `poly_private_key` is empty.
  (3) `has_credentials` returns `True` when `poly_private_key` is non-empty.
  (4) `has_api_keys` returns `False` when ANY of the three CLOB credentials
      (`poly_api_key`, `poly_api_secret`, `poly_api_passphrase`) is empty.
  (5) `mm_token_ids_list` parses the comma-separated
      `mm_market_token_ids` string into a trimmed list of token ids.
  (6) The `mode` property returns the canonical `trading_mode` value.
  (7) `cors_origin_list` parses the comma-separated `cors_origins` string
      into a trimmed list of origin URLs.
  (8) `validate_trading_mode` rejects invalid values (raises `ValueError`).
  (9) `validate_log_level` normalizes the value to uppercase.

Isolation strategy
-------------------
Every test constructs a FRESH `Settings()` instance via the
`isolated_settings` fixture (kwarg-driven, no reliance on the
process-global `settings = Settings()` singleton constructed at
`config.py` import time). This guarantees each test is hermetic:
  - The module-level singleton is never mutated, so production code
    paths that import it (`from config import settings`) see the
    original import-time state throughout the suite.
  - Init kwargs to `Settings(...)` take precedence over BOTH env vars
    and `.env` files in pydantic-settings' source-precedence chain
    (kwargs > env vars > .env > defaults), so passing explicit kwargs
    overrides the conftest-seeded `TRADING_MODE` / `CORS_ORIGINS` /
    `API_TOKEN` env vars without needing `monkeypatch.delenv`.
  - For the `.env` loading test (test 1), the fixture points
    `Settings(_env_file=tmp_path / ".env")` at a test-local .env file
    AND `monkeypatch.delenv`s `POLY_PRIVATE_KEY` so the .env file is
    unambiguously the source of truth for that key.

A note on `validate_trading_mode`
---------------------------------
The `Settings` model has a `@model_validator(mode="before")`
(`_derive_mode`) that runs BEFORE the `@field_validator("trading_mode")`
(`validate_trading_mode`) and SILENTLY coerces any invalid
`trading_mode` value to `"paper"` (or `"live"` when `paper_trade=False`).
This means constructing `Settings(trading_mode="invalid")` does NOT
raise — the `validate_trading_mode` field-validator's
`raise ValueError(...)` branch is unreachable through normal
construction because `_derive_mode` filters the value first.

To test the validator's rejection contract directly (V9 test 8), we
invoke the `Settings.validate_trading_mode` classmethod directly — the
same way pydantic v2 invokes field validators internally for a single
field. This isolates the `validate_trading_mode` function's contract
from the `_derive_mode` model-validator's coercion behaviour, which is
exactly what the V9 spec asks for ("validate_trading_mode rejects
invalid values").
"""
from __future__ import annotations

import sys
from pathlib import Path

# Inline sys.path bootstrap — mirrors the pattern in tests/test_features.py
# and tests/conftest.py. Required so the test module can
# `from config import Settings` regardless of the cwd pytest was launched
# from (monorepo root, CI checkout, IDE runner, …).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (sys.path must be set first)

from config import Settings  # noqa: E402


# ── Fixture ──────────────────────────────────────────────────────────────────

@pytest.fixture
def isolated_settings():
    """Return a factory that constructs fresh `Settings(**kwargs)` instances.

    Each test gets a brand-new `Settings` instance built from explicit
    kwargs — independent of the module-level `settings = Settings()`
    singleton constructed at `config.py` import time. Kwargs take
    precedence over env vars and `.env` in pydantic-settings' source
    chain, so passing a value as a kwarg deterministically pins that
    field regardless of what the surrounding process environment
    (conftest-seeded `TRADING_MODE`, `CORS_ORIGINS`, etc.) happens to
    contain.

    Returning a *factory* (rather than a pre-built instance) lets each
    test specify exactly the kwargs it needs while keeping the
    boilerplate of "fresh, isolated `Settings`" in one place.
    """
    def _make(**kwargs) -> Settings:
        return Settings(**kwargs)
    return _make


# ── (1) Settings loads from .env ─────────────────────────────────────────────

def test_settings_loads_from_dotenv(tmp_path, monkeypatch):
    """`Settings` reads values from a `.env` file when one is configured.

    Writes a unique `POLY_PRIVATE_KEY` to a temp `.env` file, constructs
    `Settings(_env_file=tmp_path / ".env")`, and asserts the value is
    surfaced on the resulting instance. `POLY_PRIVATE_KEY` is also
    `monkeypatch.delenv`'d from the live process environment so the
    `.env` file is the unambiguous source of truth for that key
    (pydantic-settings precedence: kwargs > env vars > .env > defaults;
    with no kwarg and no env var, .env wins).
    """
    # Ensure the live process env doesn't shadow the .env value.
    monkeypatch.delenv("POLY_PRIVATE_KEY", raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text("POLY_PRIVATE_KEY=0xfrom_dotenv_file_12345\n")

    s = Settings(_env_file=str(env_file))

    assert s.poly_private_key == "0xfrom_dotenv_file_12345"


# ── (2) has_credentials returns False for empty private key ──────────────────

def test_has_credentials_false_for_empty_private_key(isolated_settings):
    """`has_credentials` is `False` when `poly_private_key` is empty string.

    Belt-and-braces: also covers the placeholder sentinel
    `"your_wallet_private_key_here"` (the fail-closed default guard in
    the production `has_credentials` property) — that value is treated
    identically to an empty key and must NOT report credentials present.
    """
    s = isolated_settings(poly_private_key="")
    assert s.has_credentials is False

    # Belt-and-braces: the placeholder sentinel is also "no credentials".
    s_placeholder = isolated_settings(poly_private_key="your_wallet_private_key_here")
    assert s_placeholder.has_credentials is False


# ── (3) has_credentials returns True for non-empty ───────────────────────────

def test_has_credentials_true_for_non_empty_private_key(isolated_settings):
    """`has_credentials` is `True` when `poly_private_key` is a non-empty,
    non-placeholder string."""
    s = isolated_settings(poly_private_key="0xabc123def4567890abcdef")
    assert s.has_credentials is True


# ── (4) has_api_keys returns False when any key is empty ─────────────────────

def test_has_api_keys_false_when_any_key_empty(isolated_settings):
    """`has_api_keys` is `False` if ANY of the three CLOB credentials
    (`poly_api_key`, `poly_api_secret`, `poly_api_passphrase`) is empty.

    Iterates over each of the three keys being the empty one while the
    other two are filled. Belt-and-braces: also asserts the positive
    case (all three filled → `True`) so a regression that inverted the
    boolean would be caught.
    """
    # Positive case: all three filled → True.
    s_full = isolated_settings(
        poly_api_key="key-abc",
        poly_api_secret="secret-xyz",
        poly_api_passphrase="passphrase-123",
    )
    assert s_full.has_api_keys is True

    # Each of the three keys takes a turn being the empty one.
    for empty_field in ("poly_api_key", "poly_api_secret", "poly_api_passphrase"):
        kwargs = {
            "poly_api_key": "key-abc",
            "poly_api_secret": "secret-xyz",
            "poly_api_passphrase": "passphrase-123",
            empty_field: "",
        }
        s = isolated_settings(**kwargs)
        assert s.has_api_keys is False, (
            f"has_api_keys should be False when {empty_field!r} is empty"
        )


# ── (5) mm_token_ids_list parses comma-separated string ─────────────────────

def test_mm_token_ids_list_parses_comma_separated_string(isolated_settings):
    """`mm_token_ids_list` splits the comma-separated
    `mm_market_token_ids` string into a list, trimming whitespace and
    dropping empty segments.

    Belt-and-braces:
      - Empty source string → empty list (the production short-circuit
        at `if not self.mm_market_token_ids: return []`).
      - Whitespace-heavy input is trimmed per-segment.
      - Trailing comma does NOT produce a trailing empty-string entry
        (the `if t.strip()` filter in the comprehension drops it).
    """
    s = isolated_settings(mm_market_token_ids="111,222,333")
    assert s.mm_token_ids_list == ["111", "222", "333"]

    # Whitespace + trailing comma edge cases.
    s_ws = isolated_settings(mm_market_token_ids="  111 , 222 , 333 ,  ")
    assert s_ws.mm_token_ids_list == ["111", "222", "333"]

    # Empty source → empty list (not [""]).
    s_empty = isolated_settings(mm_market_token_ids="")
    assert s_empty.mm_token_ids_list == []


# ── (6) mode property returns trading_mode ───────────────────────────────────

def test_mode_property_returns_trading_mode(isolated_settings):
    """The `mode` property is the canonical, network-visible trading
    mode — it MUST equal the `trading_mode` field, whatever that field
    holds.

    Exercises all three valid modes (`paper`, `shadow`, `live`) to
    guard against a regression that hardcoded a single return value.
    """
    for mode_value in ("paper", "shadow", "live"):
        s = isolated_settings(trading_mode=mode_value)
        assert s.mode == mode_value, (
            f"mode should equal trading_mode={mode_value!r}"
        )
        assert s.mode == s.trading_mode


# ── (7) cors_origin_list parses comma-separated origins ──────────────────────

def test_cors_origin_list_parses_comma_separated_origins(isolated_settings):
    """`cors_origin_list` splits the comma-separated `cors_origins`
    string into a list of trimmed origin URLs.

    Belt-and-braces:
      - Whitespace around each origin is trimmed.
      - Trailing comma does NOT produce a trailing empty-string entry.
      - Single origin → single-element list.
    """
    s = isolated_settings(cors_origins="http://a.com,http://b.com,http://c.com")
    assert s.cors_origin_list == ["http://a.com", "http://b.com", "http://c.com"]

    # Whitespace + trailing comma edge cases.
    s_ws = isolated_settings(cors_origins="  http://x.com , http://y.com ,  ")
    assert s_ws.cors_origin_list == ["http://x.com", "http://y.com"]

    # Single origin.
    s_one = isolated_settings(cors_origins="http://only.example.com")
    assert s_one.cors_origin_list == ["http://only.example.com"]


# ── (8) validate_trading_mode rejects invalid values ─────────────────────────

def test_validate_trading_mode_rejects_invalid_values():
    """`Settings.validate_trading_mode` raises `ValueError` for any
    value that is not in `{paper, shadow, live}` (case-insensitive).

    Invokes the `@field_validator("trading_mode")` classmethod directly.
    This is the semantically correct way to test the validator's
    rejection contract in isolation, because the model's
    `@model_validator(mode="before") _derive_mode` runs BEFORE the
    field-validator during normal `Settings(...)` construction and
    silently coerces invalid values to `"paper"`/`"live"` — which
    means the `validate_trading_mode` raise-branch is unreachable
    through the public constructor. Calling the classmethod directly
    bypasses `_derive_mode` and exercises the validator's own
    `if v not in {valid_set}: raise ValueError(...)` branch.

    Belt-and-braces: valid values pass through unchanged (with case
    normalization to lowercase + whitespace stripping).
    """
    # ── Invalid values must raise ──
    invalid_values = [
        "invalid",        # not in the allowed set
        "PAPR",           # typo, case-insensitive miss
        "production",     # plausible but not allowed
        "off",            # plausible but not allowed
        "",               # empty string
        "paper ",         # whitespace-only after strip is fine, but "paper " before strip is normalized to "paper" — this is actually VALID; use a genuinely invalid one below
    ]
    # Remove the misleading "paper " case (it normalizes to a valid value);
    # replace with a genuinely-invalid value.
    invalid_values = [v for v in invalid_values if v.strip().lower() in {"", "invalid", "papr", "production", "off"}]
    invalid_values.extend(["LIVE2", "paper_trade", "real"])  # extra invalid cases

    for bad in invalid_values:
        with pytest.raises(ValueError, match="trading_mode must be one of"):
            Settings.validate_trading_mode(bad)

    # ── Valid values pass through, normalized to lowercase ──
    for raw, expected in [
        ("paper", "paper"),
        ("PAPER", "paper"),     # uppercase normalized
        ("Shadow", "shadow"),   # mixed case normalized
        ("  live  ", "live"),   # whitespace stripped
        ("LIVE", "live"),
    ]:
        assert Settings.validate_trading_mode(raw) == expected


# ── (9) validate_log_level normalizes to uppercase ───────────────────────────

def test_validate_log_level_normalizes_to_uppercase(isolated_settings):
    """`validate_log_level` normalizes the value to uppercase AND rejects
    values that are not in the allowed set
    `{DEBUG, INFO, WARNING, ERROR, CRITICAL}`.

    The V9 spec asks specifically for the normalization contract:
    lowercase input → uppercase output. Belt-and-braces:
      - Mixed-case input normalizes to uppercase.
      - Already-uppercase input round-trips unchanged.
      - Constructing `Settings(log_level="debug")` surfaces the
        normalized `"DEBUG"` on the resulting instance (exercises the
        full pydantic field-validator → instance-attribute path, not just
        the bare classmethod).
      - An invalid log_level raises `ValidationError` when passed to
        the `Settings` constructor (the field-validator's raise-branch
        is reachable here, unlike `validate_trading_mode`, because
        there's no `mode="before"` model-validator intercepting
        `log_level`).
    """
    from pydantic import ValidationError

    # ── Direct classmethod: normalization ──
    assert Settings.validate_log_level("debug") == "DEBUG"
    assert Settings.validate_log_level("info") == "INFO"
    assert Settings.validate_log_level("warning") == "WARNING"
    assert Settings.validate_log_level("error") == "ERROR"
    assert Settings.validate_log_level("critical") == "CRITICAL"

    # Mixed case + uppercase already.
    assert Settings.validate_log_level("DeBuG") == "DEBUG"
    assert Settings.validate_log_level("INFO") == "INFO"

    # ── Full constructor path: lowercase → uppercase on instance ──
    s = isolated_settings(log_level="debug")
    assert s.log_level == "DEBUG"

    # ── Invalid log_level rejected via constructor ──
    with pytest.raises(ValidationError):
        isolated_settings(log_level="bogus_level")
