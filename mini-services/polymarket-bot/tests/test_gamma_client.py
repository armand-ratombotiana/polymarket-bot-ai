"""
tests/test_gamma_client.py — Unit tests for ``core/gamma_client.py``.

V7 — Gamma API client unit tests.

Covers the six guarantees enumerated in the V7 task spec:

  (1) ``GammaClient.extract_token_ids`` extracts token ids from the
      ``tokens`` array shape (the modern Polymarket Gamma API market
      payload — each ``token`` row carries a ``token_id`` field).
  (2) ``extract_token_ids`` extracts token ids from a ``clobTokenIds``
      field that is a JSON-encoded string (the legacy / compact shape —
      the API serialises the list as ``'[\"111\",\"222\"]'``).
  (3) ``extract_token_ids`` extracts token ids from a ``clobTokenIds``
      field that is already a Python ``list`` (the inline-JSON-decoded
      shape some intermediate caches hand back).
  (4) ``extract_token_ids`` returns ``[]`` for an empty / unrecognised
      market dict — the documented no-token-ids fallback.
  (5) ``GammaClient.get_markets`` builds the correct query params
      (``active``, ``closed``, ``limit``, ``offset``, ``order``,
      ``ascending``) for both the default (active markets) and the
      resolved-markets (``active=False, closed=True``) invocations.
  (6) ``GammaClient.search_markets`` builds the correct query params
      (``search``, ``limit``, ``active``) for a free-text query.

Mocking strategy (per V7 task spec — "mocked httpx")
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

  * ``mock_httpx_client`` — a ``MagicMock`` standing in for the
    ``httpx.AsyncClient`` instance that ``GammaClient._ensure_client``
    would normally construct. The fixture patches
    ``core.gamma_client.httpx.AsyncClient`` so that
    ``httpx.AsyncClient(base_url=..., timeout=..., headers=...)``
    returns the same mock instance on every call. The mock instance's
    ``get`` attribute is an ``AsyncMock`` returning a canned
    ``MagicMock`` response whose ``raise_for_status()`` is a no-op
    and whose ``.json()`` returns ``[]`` (an empty list — what the
    Gamma API returns when no markets match). This is the "mocked
    httpx" surface: no real network socket is opened, no real
    ``httpx.AsyncClient`` is constructed, and the params the
    production code passes to ``client.get(path, params=...)`` are
    inspectable post-call via ``mock_httpx_client.get.call_args``.

  * Tests (1)-(4) exercise ``GammaClient.extract_token_ids`` directly.
    ``extract_token_ids`` is a ``@staticmethod`` that takes a market
    dict and returns a ``list[str]`` — no I/O, no instance state, no
    mocking required. The four shapes enumerated above are exercised
    one at a time with deterministic input.

  * Tests (5)-(6) construct a fresh ``GammaClient()`` (NOT the
    module-level singleton — the singleton's ``self._client`` could
    have been cached from a prior test) and call ``get_markets`` /
    ``search_markets`` against the mocked httpx client, then assert
    on ``call_args.kwargs["params"]`` (the param dict production
    hands to ``httpx.AsyncClient.get``).

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (the repo's ``pytest.ini`` /
``pyproject.toml`` cannot be edited per the V7 "Do NOT edit existing
files" constraint, so ``asyncio_mode = "auto"`` cannot be enabled via
config — mirrors the convention already used by every sibling test
module: ``tests/test_attribution.py``, ``tests/test_decision_ledger.py``,
``tests/test_settlement.py``, etc.).
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.gamma_client import GammaClient


# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. The repo's ``pytest.ini`` / ``pyproject.toml`` cannot be edited
# per the V7 task constraint ("Do NOT edit existing files"), so we use the
# module-level ``pytestmark`` idiom instead of ``asyncio_mode = "auto"``
# (mirrors ``tests/test_attribution.py``, ``tests/test_decision_ledger.py``,
# and every other Wave 3/4 test module).
pytestmark = pytest.mark.asyncio


# ── Fixture: mocked httpx.AsyncClient ──────────────────────────────────────


@pytest.fixture
def mock_httpx_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``httpx.AsyncClient`` (as seen inside ``core.gamma_client``)
    with a deterministic mock so no real network socket is opened.

    Why patch the class symbol instead of ``_ensure_client`` directly?
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    ``GammaClient._ensure_client`` calls ``httpx.AsyncClient(base_url=...,
    timeout=..., headers=...)`` to build its underlying transport. The
    V7 task spec asks for "mocked httpx" — patching the class symbol
    referenced by the production module (``core.gamma_client.httpx``)
    is the most faithful interpretation: the real ``_ensure_client``
    code path runs end-to-end, only the actual ``AsyncClient``
    instantiation is intercepted. ``monkeypatch.setattr`` on the
    module-qualified name ``core.gamma_client.httpx.AsyncClient`` scopes
    the patch to the gamma_client module's view of httpx and restores
    the real class at teardown (so sibling tests that exercise the
    real ``httpx`` — e.g. ``test_live_safety_gate.py``'s ASGI transport
    tests — are unaffected).

    Returned mock shape
    ~~~~~~~~~~~~~~~~~~~
      * ``mock_client.is_closed`` → ``False`` (so ``_ensure_client``'s
        cache check keeps the same instance rather than recreating).
      * ``mock_client.get``        → ``AsyncMock`` returning a canned
        ``MagicMock`` response (``raise_for_status`` no-op,
        ``.json()`` returns ``[]``).
      * ``mock_client.aclose``     → ``AsyncMock`` (no-op) so
        ``GammaClient.close()`` doesn't crash if a test invokes it.

    The ``httpx.AsyncClient`` callable itself is replaced with
    ``MagicMock(return_value=mock_client)`` so every construction call
    returns the SAME mock instance — that way the params captured by
    ``mock_client.get.call_args`` always correspond to the most recent
    ``_get`` invocation, regardless of how many times ``_ensure_client``
    re-ran.
    """
    # Canned response: empty market list (the Gamma API's "no matches"
    # shape). ``get_markets`` / ``search_markets`` both return this
    # unchanged via the ``isinstance(data, list)`` branch — but the
    # V7 assertions are about the *params*, not the return value, so
    # any well-formed list works.
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()  # no-op — never raises
    mock_resp.json = MagicMock(return_value=[])

    mock_client = MagicMock()
    mock_client.is_closed = False
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.aclose = AsyncMock()

    # Replace the httpx.AsyncClient *class symbol* referenced inside
    # core.gamma_client with a callable that always returns our mock
    # instance. ``MagicMock(return_value=mock_client)`` is itself
    # callable: invoking it (as ``httpx.AsyncClient(base_url=..., ...)``)
    # returns ``mock_client``.
    monkeypatch.setattr(
        "core.gamma_client.httpx.AsyncClient",
        MagicMock(return_value=mock_client),
    )
    return mock_client


# ── (1) extract_token_ids — tokens array shape ────────────────────────────


async def test_extract_token_ids_from_tokens_array():
    """``extract_token_ids`` pulls token ids from the ``tokens`` list.

    The modern Gamma API market payload carries a ``tokens`` array
    whose rows each expose ``token_id`` (and ``outcome``). The parser
    must read ``token_id`` from every well-formed row and return them
    in source order, skipping rows that are not dicts or that lack
    a ``token_id`` key.
    """
    market = {
        "tokens": [
            {"token_id": "TOK_YES_111", "outcome": "Yes"},
            {"token_id": "TOK_NO_222", "outcome": "No"},
        ],
    }
    ids = GammaClient.extract_token_ids(market)
    assert ids == ["TOK_YES_111", "TOK_NO_222"]


# ── (2) extract_token_ids — clobTokenIds JSON-string shape ────────────────


async def test_extract_token_ids_from_clob_token_ids_string():
    """``extract_token_ids`` parses a ``clobTokenIds`` JSON string.

    The legacy / compact Gamma API shape serialises the token id list
    as a JSON-encoded string (``'[\"111\",\"222\"]'``). The parser
    must ``json.loads`` it and return the ids as a flat list of
    strings, in source order, regardless of whether the JSON values
    were quoted strings or bare integers.
    """
    market = {"clobTokenIds": '["111", "222"]'}
    ids = GammaClient.extract_token_ids(market)
    assert ids == ["111", "222"]

    # Integer-encoded variant: the JSON list contains bare ints; the
    # parser must coerce them to strings via ``str(x)`` so downstream
    # consumers (which expect ``list[str]``) never receive an int.
    market_int = {"clobTokenIds": "[111, 222]"}
    assert GammaClient.extract_token_ids(market_int) == ["111", "222"]


# ── (3) extract_token_ids — clobTokenIds list shape ───────────────────────


async def test_extract_token_ids_from_clob_token_ids_list():
    """``extract_token_ids`` handles ``clobTokenIds`` as a Python list.

    Some intermediate caches hand back the ``clobTokenIds`` field as
    a Python ``list`` (the JSON already decoded). The parser must
    detect this shape via ``isinstance(raw_ids, list)`` and return
    the entries as a list of strings (coercing non-string entries
    via ``str(x)`` and dropping falsy entries via ``if x``).
    """
    market = {"clobTokenIds": ["TOK_A", "TOK_B"]}
    ids = GammaClient.extract_token_ids(market)
    assert ids == ["TOK_A", "TOK_B"]

    # Non-string entries are coerced to strings; falsy entries
    # (None / "" / 0) are dropped.
    market_mixed = {"clobTokenIds": [111, None, "TOK_C", ""]}
    assert GammaClient.extract_token_ids(market_mixed) == ["111", "TOK_C"]


# ── (4) extract_token_ids — empty dict → [] ───────────────────────────────


async def test_extract_token_ids_returns_empty_for_empty_dict():
    """``extract_token_ids({})`` returns ``[]`` (the documented fallback).

    A market dict with neither ``tokens`` nor ``clobTokenIds`` must
    yield an empty list — NOT raise ``KeyError`` / ``TypeError``.
    Downstream code paths (e.g. ``extract_binary_pair``) rely on this
    to short-circuit gracefully when a market has no resolvable
    token ids.
    """
    assert GammaClient.extract_token_ids({}) == []

    # Also: a market dict whose ``tokens`` list is empty and whose
    # ``clobTokenIds`` is missing must still return ``[]`` (the
    # ``if tokens and isinstance(tokens, list)`` guard treats ``[]``
    # as falsy, so the parser falls through to the clobTokenIds
    # branch, finds nothing, and returns ``[]``).
    assert GammaClient.extract_token_ids({"tokens": []}) == []
    # And: a market dict with malformed tokens rows (no token_id key)
    # and no clobTokenIds must also return ``[]``.
    assert (
        GammaClient.extract_token_ids(
            {"tokens": [{"outcome": "Yes"}, {"outcome": "No"}]}
        )
        == []
    )


# ── (5) get_markets builds correct params ──────────────────────────────────


async def test_get_markets_builds_correct_params(mock_httpx_client: MagicMock):
    """``GammaClient.get_markets`` builds the expected Gamma API query
    params dict and passes it to ``httpx.AsyncClient.get`` as the
    ``params`` kwarg.

    Two invocations are exercised:

      (a) Default ``get_markets()`` call → active markets, not closed,
          limit=100, offset=0, order="volume24hr", ascending=False.
          Expected params dict::

              {
                  "limit":   100,
                  "offset":  0,
                  "order":   "volume24hr",
                  "ascending": "false",
                  "active":  "true",   # active=True  → key added
                  "closed":  "false",   # closed=False → key added (str(False).lower())
              }

      (b) ``get_resolved_markets`` invocation — i.e.
          ``get_markets(active=False, closed=True, limit=30,
          order="updatedAt", ascending=False)`` — produces::

              {
                  "limit":     30,
                  "offset":    0,
                  "order":     "updatedAt",
                  "ascending": "false",
                  # NO "active" key  (active=False → not added)
                  "closed":    "true",
              }

    The assertion captures the ``params`` kwarg from the most recent
    ``client.get`` call (``mock_httpx_client.get.call_args``) and
    checks every key the V7 spec enumerates (``active``, ``closed``,
    ``limit``, ``order`` — plus ``offset`` / ``ascending`` for
    completeness) against the expected values.
    """
    # ── (a) Default active-markets invocation ──
    client = GammaClient()
    await client.get_markets()  # all defaults

    mock_httpx_client.get.assert_called_once()
    call = mock_httpx_client.get.call_args
    assert call.args[0] == "/markets", (
        "get_markets must GET the /markets endpoint (path is the first "
        "positional arg to client.get)."
    )
    params = call.kwargs.get("params")
    assert params is not None, (
        "get_markets must pass the params dict as the `params` kwarg."
    )

    assert params["active"] == "true", (
        "Default get_markets() (active=True) must add active=true."
    )
    assert params["closed"] == "false", (
        "Default get_markets() (closed=False) must add closed=false "
        "via str(closed).lower()."
    )
    assert params["limit"] == 100, "Default limit is 100."
    assert params["offset"] == 0, "Default offset is 0."
    assert params["order"] == "volume24hr", "Default order is volume24hr."
    assert params["ascending"] == "false", (
        "Default ascending=False → params['ascending']='false'."
    )

    # ── (b) Resolved-markets invocation (active=False, closed=True) ──
    mock_httpx_client.get.reset_mock()
    await client.get_markets(
        active=False,
        closed=True,
        limit=30,
        order="updatedAt",
        ascending=False,
    )

    mock_httpx_client.get.assert_called_once()
    call = mock_httpx_client.get.call_args
    assert call.args[0] == "/markets"
    params = call.kwargs["params"]

    assert "active" not in params, (
        "active=False must NOT add an 'active' key (the `if active:` "
        "guard skips the assignment when active is falsy)."
    )
    assert params["closed"] == "true", (
        "closed=True → params['closed']='true' via str(closed).lower()."
    )
    assert params["limit"] == 30
    assert params["offset"] == 0
    assert params["order"] == "updatedAt"
    assert params["ascending"] == "false"


# ── (6) search_markets builds correct params ───────────────────────────────


async def test_search_markets_builds_correct_params(mock_httpx_client: MagicMock):
    """``GammaClient.search_markets`` builds the expected Gamma API
    query params dict for a free-text search.

    Expected params dict for ``search_markets("ethereum merge", limit=20)``::

        {
            "search": "ethereum merge",
            "limit":  20,
            "active": "true",
        }

    The V7 spec asserts on the three search-specific keys
    (``search``, ``limit``, ``active``). The default ``limit=20`` and
    the hard-coded ``active="true"`` are both verified, as is the
    pass-through of the caller-supplied query string (no URL-encoding
    or normalisation — the production code passes ``query`` through
    verbatim).
    """
    client = GammaClient()
    await client.search_markets("ethereum merge", limit=20)

    mock_httpx_client.get.assert_called_once()
    call = mock_httpx_client.get.call_args
    assert call.args[0] == "/markets", (
        "search_markets GETs the same /markets endpoint as get_markets "
        "(the search filter is a query param, not a separate path)."
    )
    params = call.kwargs["params"]

    assert params["search"] == "ethereum merge", (
        "search_markets must forward the caller's query string verbatim "
        "as params['search']."
    )
    assert params["limit"] == 20, "Caller-supplied limit must be forwarded."
    assert params["active"] == "true", (
        "search_markets hard-codes active='true' (it never searches "
        "closed markets)."
    )

    # The search params dict must NOT carry the get_markets-only keys
    # (offset / order / ascending / closed) — search_markets does not
    # construct those. This guards against an accidental future merge
    # of the two param builders.
    assert "offset" not in params
    assert "order" not in params
    assert "ascending" not in params
    assert "closed" not in params
