"""
W9-5 — Unit tests for ``core/clob_client.py`` (auth helpers + dataclasses).

Covers the pure-function auth helpers and the lightweight dataclasses:

  1. ``_hmac_signature`` returns a deterministic base64-encoded HMAC-SHA256
     signature — same inputs always produce the same signature.
  2. ``_hmac_signature`` is method-agnostic via upper-case normalization —
     ``"get"`` and ``"GET"`` produce the same signature.
  3. ``_hmac_signature`` includes the body in the canonical message — a
     different body produces a different signature.
  4. ``_hmac_signature`` includes the path in the canonical message — a
     different path produces a different signature.
  5. ``_l2_headers`` returns all four POLY-* headers (POLY-ACCESS-KEY,
     POLY-PASSPHRASE, POLY-TIMESTAMP, POLY-SIGNATURE).
  6. ``_l2_headers`` POLY-ACCESS-KEY / POLY-PASSPHRASE echo the creds.
  7. ``_l2_headers`` POLY-TIMESTAMP is a unix-epoch integer string.
  8. ``_sign_l1`` returns a hex-encoded signature for a known Ethereum
     private key (non-empty, even-length hex string).
  9. ``ApiCreds`` dataclass exposes ``api_key`` / ``api_secret`` /
     ``api_passphrase``.
 10. ``OrderArgs`` dataclass exposes ``token_id`` / ``price`` / ``side`` /
     ``size`` with ``order_type`` defaulting to ``"GTC"``.
 11. ``ClobClient.__init__`` initializes ``_http=None`` and ``_creds=None``
     when no API keys are configured.
 12. ``ClobClient.address`` returns the placeholder
     ``"0x0000...0000"`` when no private key is configured.

Isolation
----------
These tests target the pure helper functions and lightweight dataclasses —
no HTTP calls are made. ``ClobClient`` is instantiated against the default
``config.settings`` (which has empty API keys / private key in the test
sandbox). Where a private key is needed (``_sign_l1``), we use a known
deterministic test key.

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (mirrors every sibling test module —
even though these tests are sync, the mark is harmless and keeps
collection consistent).
"""
from __future__ import annotations

import base64
import hashlib
import hmac as hmac_module
import time

import pytest

from config import settings
from core.clob_client import (
    ApiCreds,
    ClobClient,
    OrderArgs,
    _hmac_signature,
    _l2_headers,
    _sign_l1,
)
from core.data_store import Side

pytestmark = pytest.mark.asyncio


# A deterministic test-only Ethereum private key (well-known test vector).
# DO NOT USE IN PRODUCTION.
_TEST_PRIVATE_KEY = "0x" + "a" * 64  # 32 bytes of 0xaa


# ── 1. _hmac_signature is deterministic ──────────────────────────────────────
def test_hmac_signature_is_deterministic():
    """The same inputs must always produce the same HMAC-SHA256 signature."""
    sig1 = _hmac_signature("secret", "12345", "GET", "/markets", "")
    sig2 = _hmac_signature("secret", "12345", "GET", "/markets", "")
    assert sig1 == sig2
    assert isinstance(sig1, str)
    assert len(sig1) > 0


# ── 2. _hmac_signature is method-agnostic via upper-case ────────────────────
def test_hmac_signature_method_case_insensitive():
    """``"get"`` and ``"GET"`` must produce the same signature — the
    function upper-cases the method before signing."""
    sig_upper = _hmac_signature("secret", "12345", "GET", "/markets", "")
    sig_lower = _hmac_signature("secret", "12345", "get", "/markets", "")
    sig_mixed = _hmac_signature("secret", "12345", "GeT", "/markets", "")
    assert sig_upper == sig_lower == sig_mixed


# ── 3. _hmac_signature includes the body ────────────────────────────────────
def test_hmac_signature_includes_body():
    """A different body must produce a different signature — the body is
    part of the canonical message."""
    sig_no_body = _hmac_signature("secret", "12345", "POST", "/order", "")
    sig_with_body = _hmac_signature("secret", "12345", "POST", "/order", '{"a":1}')
    assert sig_no_body != sig_with_body


# ── 4. _hmac_signature includes the path ──────────────────────────────────────
def test_hmac_signature_includes_path():
    """A different path must produce a different signature."""
    sig_a = _hmac_signature("secret", "12345", "GET", "/markets", "")
    sig_b = _hmac_signature("secret", "12345", "GET", "/orders", "")
    assert sig_a != sig_b


# ── 5. _l2_headers returns all four POLY-* headers ───────────────────────────
def test_l2_headers_returns_all_four_poly_headers():
    """``_l2_headers`` must return a dict carrying exactly the four
    documented POLY-* headers — no missing, no extras."""
    creds = ApiCreds(api_key="my-key", api_secret="my-secret", api_passphrase="my-pass")
    headers = _l2_headers(creds, "GET", "/markets")

    expected_keys = {"POLY-ACCESS-KEY", "POLY-PASSPHRASE", "POLY-TIMESTAMP", "POLY-SIGNATURE"}
    assert set(headers.keys()) == expected_keys


# ── 6. _l2_headers echoes creds in POLY-ACCESS-KEY / POLY-PASSPHRASE ────────
def test_l2_headers_echoes_creds():
    """``POLY-ACCESS-KEY`` and ``POLY-PASSPHRASE`` must echo the supplied
    ``ApiCreds`` fields verbatim."""
    creds = ApiCreds(api_key="my-key", api_secret="my-secret", api_passphrase="my-pass")
    headers = _l2_headers(creds, "GET", "/markets")

    assert headers["POLY-ACCESS-KEY"] == "my-key"
    assert headers["POLY-PASSPHRASE"] == "my-pass"


# ── 7. _l2_headers POLY-TIMESTAMP is a unix-epoch integer string ────────────
def test_l2_headers_timestamp_is_unix_epoch_integer_string():
    """``POLY-TIMESTAMP`` must be a string of digits representing the
    current unix epoch — within ±5 seconds of ``time.time()``."""
    creds = ApiCreds(api_key="k", api_secret="s", api_passphrase="p")
    before = int(time.time())
    headers = _l2_headers(creds, "GET", "/markets")
    after = int(time.time())

    ts_str = headers["POLY-TIMESTAMP"]
    assert ts_str.isdigit()
    ts_int = int(ts_str)
    # Within ±5 seconds of the call window (allows for test scheduling
    # delays on loaded CI boxes).
    assert before - 5 <= ts_int <= after + 5


# ── 8. _sign_l1 returns a hex-encoded signature ──────────────────────────────
def test_sign_l1_returns_hex_signature():
    """``_sign_l1`` returns a hex-encoded string signature for a known
    Ethereum private key. The signature is non-empty and contains only
    hex characters (after the optional 0x prefix)."""
    sig = _sign_l1(_TEST_PRIVATE_KEY, "polymarket-api-key-12345")
    assert isinstance(sig, str)
    assert len(sig) > 0
    # Hex characters only (with or without 0x prefix).
    sig_body = sig[2:] if sig.startswith("0x") else sig
    assert all(c in "0123456789abcdef" for c in sig_body)
    # EIP-191 signatures are 65 bytes → 130 hex chars.
    assert len(sig_body) == 130


# ── 9. ApiCreds dataclass exposes three fields ───────────────────────────────
def test_api_creds_dataclass_exposes_three_fields():
    """``ApiCreds`` exposes ``api_key``, ``api_secret``, ``api_passphrase``."""
    creds = ApiCreds(api_key="k", api_secret="s", api_passphrase="p")
    assert creds.api_key == "k"
    assert creds.api_secret == "s"
    assert creds.api_passphrase == "p"


# ── 10. OrderArgs dataclass has GTC default ──────────────────────────────────
def test_order_args_dataclass_has_gtc_default():
    """``OrderArgs`` exposes ``token_id``, ``price``, ``side``, ``size``,
    and ``order_type`` (default ``"GTC"``)."""
    args = OrderArgs(token_id="TOK_X", price=0.62, side=Side.BUY, size=10.0)
    assert args.token_id == "TOK_X"
    assert args.price == 0.62
    assert args.side == Side.BUY
    assert args.size == 10.0
    assert args.order_type == "GTC"  # default

    # Explicit override.
    args_fok = OrderArgs(
        token_id="TOK_X", price=0.62, side=Side.SELL, size=10.0, order_type="FOK",
    )
    assert args_fok.order_type == "FOK"


# ── 11. ClobClient.__init__ initializes _http=None, _creds=None ─────────────
def test_clob_client_init_initializes_http_and_creds_to_none(monkeypatch):
    """When ``settings.has_api_keys`` is False, ``ClobClient.__init__``
    must leave ``_http=None`` and ``_creds=None`` (no pre-configuration)."""
    # Patch has_api_keys to False so the creds-loading branch is skipped
    # even when the test sandbox happens to have API keys configured.
    monkeypatch.setattr(type(settings), "has_api_keys", property(lambda self: False))

    client = ClobClient()
    assert client._http is None
    assert client._creds is None


# ── 12. ClobClient.address returns placeholder when no key ───────────────────
def test_clob_client_address_placeholder_when_no_key(monkeypatch):
    """When ``settings.poly_private_key`` is empty (or the placeholder
    literal), ``ClobClient.address`` returns the placeholder
    ``"0x0000...0000"`` — no real wallet address is derivable."""
    monkeypatch.setattr(settings, "poly_private_key", "")
    client = ClobClient()
    assert client.address == "0x0000...0000"

    # The placeholder literal string also triggers the same guard.
    monkeypatch.setattr(settings, "poly_private_key", "your_wallet_private_key_here")
    client2 = ClobClient()
    assert client2.address == "0x0000...0000"


# ── 13. _hmac_signature matches a hand-computed reference value ─────────────
def test_hmac_signature_matches_hand_computed_reference():
    """The signature produced by ``_hmac_signature`` must match a
    hand-computed reference using ``hmac`` + ``hashlib.sha256`` directly —
    proving the helper doesn't deviate from the documented algorithm."""
    secret = "test-secret"
    timestamp = "1700000000"
    method = "POST"
    path = "/order"
    body = '{"size":10}'

    # Reference: base64(HMAC-SHA256(secret, timestamp + method + path + body))
    canonical = timestamp + method.upper() + path + body
    expected_mac = hmac_module.new(
        secret.encode(), canonical.encode(), hashlib.sha256,
    )
    expected_sig = base64.b64encode(expected_mac.digest()).decode()

    actual = _hmac_signature(secret, timestamp, method, path, body)
    assert actual == expected_sig
