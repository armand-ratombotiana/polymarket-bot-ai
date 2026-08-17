"""
core/clob_client.py — Async CLOB REST API client.
Handles L1 (wallet/EIP-712) and L2 (HMAC) authentication.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from base64 import b64encode
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct

from config import settings
from core.data_store import Order, OrderStatus, Side

log = logging.getLogger(__name__)

# Polymarket CTF Exchange EIP-712 order domain (Polygon mainnet).
CTF_EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
PRICE_SCALE = 1_000_000  # price and size are expressed in 6 decimals on-chain


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class ApiCreds:
    api_key: str
    api_secret: str
    api_passphrase: str


@dataclass
class OrderArgs:
    token_id: str
    price: float
    side: Side
    size: float
    order_type: str = "GTC"   # GTC | FOK | FAK


# ── Auth helpers ─────────────────────────────────────────────────────────────

def _sign_l1(private_key: str, message: str) -> str:
    """Sign a message with the Ethereum private key (EIP-191 personal_sign)."""
    account = Account.from_key(private_key)
    msg = encode_defunct(text=message)
    signed = account.sign_message(msg)
    return signed.signature.hex()


def _hmac_signature(secret: str, timestamp: str, method: str, path: str, body: str = "") -> str:
    """Compute HMAC-SHA256 signature for L2 authenticated requests."""
    message = timestamp + method.upper() + path + body
    mac = hmac.new(secret.encode(), message.encode(), hashlib.sha256)
    return b64encode(mac.digest()).decode()


def _l2_headers(creds: ApiCreds, method: str, path: str, body: str = "") -> Dict[str, str]:
    timestamp = str(int(time.time()))
    sig = _hmac_signature(creds.api_secret, timestamp, method, path, body)
    return {
        "POLY-ACCESS-KEY": creds.api_key,
        "POLY-PASSPHRASE": creds.api_passphrase,
        "POLY-TIMESTAMP": timestamp,
        "POLY-SIGNATURE": sig,
    }


# ── CLOB Client ───────────────────────────────────────────────────────────────

class ClobClient:
    """
    Async REST client for Polymarket's CLOB API.
    Supports both unauthenticated (public) and L2-authenticated endpoints.
    """

    def __init__(self) -> None:
        self._base = settings.poly_clob_host.rstrip("/")
        self._key = settings.poly_private_key
        self._creds: Optional[ApiCreds] = None
        self._http: Optional[httpx.AsyncClient] = None

        # Load pre-configured creds if available
        if settings.has_api_keys:
            self._creds = ApiCreds(
                api_key=settings.poly_api_key,
                api_secret=settings.poly_api_secret,
                api_passphrase=settings.poly_api_passphrase,
            )

    async def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=self._base,
                timeout=20.0,
                headers={"Content-Type": "application/json", "User-Agent": "polymarket-bot/1.0"},
            )
        return self._http

    async def close(self) -> None:
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    # ── Internal request helpers ──────────────────────────────────────────

    async def _get(self, path: str, params: Optional[Dict] = None, auth: bool = False) -> Any:
        client = await self._ensure_http()
        headers = {}
        if auth and self._creds:
            headers = _l2_headers(self._creds, "GET", path)
        resp = await client.get(path, params=params or {}, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, body: Any, auth: bool = True) -> Any:
        client = await self._ensure_http()
        raw = json.dumps(body)
        headers = {}
        if auth and self._creds:
            headers = _l2_headers(self._creds, "POST", path, raw)
        resp = await client.post(path, content=raw, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, path: str, body: Any = None, auth: bool = True) -> Any:
        client = await self._ensure_http()
        raw = json.dumps(body or {})
        headers = {}
        if auth and self._creds:
            headers = _l2_headers(self._creds, "DELETE", path, raw)
        resp = await client.request("DELETE", path, content=raw, headers=headers)
        resp.raise_for_status()
        return resp.json()

    # ── L1 Authentication ─────────────────────────────────────────────────

    async def derive_api_key(self) -> ApiCreds:
        """
        Derive CLOB API credentials from the wallet private key (L1 auth).
        This is the first step before making any authenticated calls.
        """
        if not self._key or self._key == "your_wallet_private_key_here":
            raise ValueError("POLY_PRIVATE_KEY is not configured in .env")

        account = Account.from_key(self._key)
        nonce = int(time.time())
        message = f"polymarket-api-key-{nonce}"
        signature = _sign_l1(self._key, message)

        payload = {
            "address": account.address,
            "signature": signature,
            "nonce": nonce,
        }
        try:
            data = await self._post("/auth/api-key", payload, auth=False)
            self._creds = ApiCreds(
                api_key=data["apiKey"],
                api_secret=data["secret"],
                api_passphrase=data["passphrase"],
            )
            log.info("API credentials derived for %s", account.address)
            return self._creds
        except Exception as e:
            log.warning("Could not derive API key from server (offline/paper mode): %s", e)
            # Generate deterministic fake creds for paper trading
            fake_key = hashlib.sha256(self._key.encode()).hexdigest()[:32]
            self._creds = ApiCreds(
                api_key=fake_key,
                api_secret=fake_key,
                api_passphrase="paper",
            )
            return self._creds

    @property
    def address(self) -> str:
        if not self._key or self._key == "your_wallet_private_key_here":
            return "0x0000...0000"
        return Account.from_key(self._key).address

    # ── Public endpoints ──────────────────────────────────────────────────

    async def get_markets(self, next_cursor: str = "") -> Dict:
        """Paginated list of markets from the CLOB."""
        params = {}
        if next_cursor:
            params["next_cursor"] = next_cursor
        return await self._get("/markets", params=params)

    async def get_market(self, condition_id: str) -> Dict:
        return await self._get(f"/markets/{condition_id}")

    async def get_order_book(self, token_id: str) -> Dict:
        """Fetch the current order book snapshot for a token."""
        return await self._get("/book", params={"token_id": token_id})

    async def get_spread(self, token_id: str) -> Dict:
        return await self._get("/spread", params={"token_id": token_id})

    async def get_price(self, token_id: str, side: str) -> Dict:
        return await self._get("/price", params={"token_id": token_id, "side": side})

    async def get_last_trade_price(self, token_id: str) -> Dict:
        return await self._get("/last-trade-price", params={"token_id": token_id})

    # ── Authenticated endpoints ────────────────────────────────────────────

    async def get_open_orders(self) -> List[Dict]:
        data = await self._get("/orders", auth=True)
        return data if isinstance(data, list) else data.get("data", [])

    async def get_positions(self) -> List[Dict]:
        data = await self._get("/positions", auth=True)
        return data if isinstance(data, list) else data.get("data", [])

    async def get_balance(self) -> Dict:
        return await self._get("/balance-allowance", params={"asset_type": "COLLATERAL"}, auth=True)

    async def create_order(self, args: OrderArgs) -> Optional[Dict]:
        """
        Sign an EIP-712 limit order and submit it to the CLOB. Returns the server
        response dict or None on error. Price is in [0.01, 0.99], size in shares.
        """
        if not self._creds:
            raise RuntimeError("Not authenticated. Call derive_api_key() first.")
        if not self._key or self._key == "your_wallet_private_key_here":
            log.error("POLY_PRIVATE_KEY not configured — cannot sign live orders")
            return None

        maker = Account.from_key(self._key).address
        signer = maker
        taker = "0x0000000000000000000000000000000000000000"
        nonce = int.from_bytes(secrets.token_bytes(16), "big")

        order_data = {
            "maker": maker,
            "signer": signer,
            "taker": taker,
            "tokenId": int(args.token_id),
            "price": int(round(args.price * PRICE_SCALE)),
            "size": int(round(args.size * PRICE_SCALE)),
            "side": 0 if args.side == Side.BUY else 1,
            "nonce": nonce,
            "feeRateBps": 0,
            "signatureType": 0,  # 0 = EIP-712
        }

        domain_data = {
            "name": "Polymarket CTF Exchange",
            "version": "1",
            "chainId": int(settings.poly_chain_id),
            "verifyingContract": CTF_EXCHANGE_ADDRESS,
        }
        message_types = {
            "Order": [
                {"name": "maker", "type": "address"},
                {"name": "signer", "type": "address"},
                {"name": "taker", "type": "address"},
                {"name": "tokenId", "type": "uint256"},
                {"name": "price", "type": "uint256"},
                {"name": "size", "type": "uint256"},
                {"name": "side", "type": "uint8"},
                {"name": "nonce", "type": "uint256"},
                {"name": "feeRateBps", "type": "uint256"},
                {"name": "signatureType", "type": "uint8"},
            ],
        }

        try:
            signed = Account.sign_typed_data(
                self._key,
                domain_data=domain_data,
                message_types=message_types,
                message_data=order_data,
            )
            signature = signed.signature.hex()
        except Exception as e:
            log.error("Order signing failed: %s", e)
            return None

        order_id = str(uuid.uuid4())
        payload = {
            "order": {
                "maker": maker,
                "signer": signer,
                "taker": taker,
                "tokenId": str(order_data["tokenId"]),
                "price": str(order_data["price"]),
                "size": str(order_data["size"]),
                "side": args.side.value,
                "nonce": str(nonce),
                "feeRateBps": "0",
                "signatureType": 0,
                "signature": signature,
            },
            "owner": maker,
            "orderType": args.order_type,
            "order_id": order_id,
        }

        try:
            resp = await self._post("/order", payload, auth=True)
            log.debug("Order placed: %s", resp)
            return resp
        except httpx.HTTPStatusError as e:
            log.error("Order rejected [%s]: %s", e.response.status_code, e.response.text)
            return None
        except Exception as e:
            log.error("Order error: %s", e)
            return None

    async def cancel_order(self, order_id: str) -> bool:
        try:
            await self._delete(f"/order/{order_id}")
            return True
        except Exception as e:
            log.error("Cancel failed for %s: %s", order_id, e)
            return False

    async def cancel_all_orders(self) -> bool:
        try:
            await self._delete("/orders")
            return True
        except Exception as e:
            log.error("Cancel-all failed: %s", e)
            return False

    async def get_trades(self, maker_address: str = "", limit: int = 50) -> List[Dict]:
        params: Dict[str, Any] = {"limit": limit}
        if maker_address:
            params["maker_address"] = maker_address
        data = await self._get("/data/trades", params=params, auth=True)
        return data if isinstance(data, list) else data.get("data", [])


# Module-level singleton
clob_client = ClobClient()
