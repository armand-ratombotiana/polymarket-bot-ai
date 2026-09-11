"""
config.py — Pydantic-based settings for the Polymarket bot.
Reads from environment variables / .env file.
"""
from __future__ import annotations

import os

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── API Security ────────────────────────────────────────────────────
    # Bearer token required for all API routes except /api/health.
    # Default is empty → fail-closed (HTTP 503 AUTH_NOT_CONFIGURED on every
    # authenticated route) until a strong token is set in .env. (S12)
    api_token: str = Field(default="", description="API bearer token (set in .env; empty = fail-closed 503)")
    # Comma-separated allowed browser origins — explicit hosts only, no wildcard.
    # An empty list = same-origin only (no CORS). (S12)
    cors_origins: str = Field(default="http://localhost:3000,http://localhost:3010,http://127.0.0.1:3000,http://127.0.0.1:3010,http://10.73.89.150:3010,http://10.73.89.150:3000", description="Comma-separated allowed CORS origins (no wildcard)")

    # ── Wallet & Auth ──────────────────────────────────────────────────
    poly_private_key: str = Field(default="", description="Polygon wallet private key")
    poly_api_key: str = Field(default="", description="CLOB API key (auto-derived if blank)")
    poly_api_secret: str = Field(default="", description="CLOB API secret")
    poly_api_passphrase: str = Field(default="", description="CLOB API passphrase")

    # ── Network ────────────────────────────────────────────────────────
    poly_chain_id: int = 137
    poly_clob_host: str = "https://clob.polymarket.com"
    poly_gamma_host: str = "https://gamma-api.polymarket.com"
    poly_data_host: str = "https://data-api.polymarket.com"
    poly_ws_host: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    # ── Mode (single network-visible source of truth) ────────────────────
    # paper → simulated orders; shadow → evaluation only, no orders;
    # live  → real funds (requires live_trading_enabled + credentials).
    trading_mode: str = Field(default="paper", description="paper | shadow | live")
    # Legacy flag, derived from trading_mode when TRADING_MODE is set; keep it
    # a real field so all existing references keep working.
    paper_trade: bool = True

    # ── Risk (USD 100 operating / USD 200 hard ceiling — never auto-increased) ──
    max_open_orders: int = 8
    max_position_per_market_usdc: float = 3.0
    max_total_exposure_usdc: float = 25.0
    daily_loss_limit_usdc: float = 2.0
    live_trading_enabled: bool = False   # live trading disabled by default

    # ── Watchdog / Tripwires ─────────────────────────────────────────────
    watchdog_heartbeat_timeout: int = Field(default=120, ge=30)
    watchdog_check_interval: int = Field(default=15, ge=5)
    tripwire_auto_kill: bool = Field(default=True, description="Auto-activate kill switch on critical tripwires")
    book_stall_seconds: int = Field(default=120, ge=30)

    # ── Market Making ──────────────────────────────────────────────────
    mm_enabled: bool = True
    mm_market_token_ids: str = ""   # comma-separated token IDs
    mm_spread_bps: int = 200
    mm_quote_size_usdc: float = 1.5
    mm_max_inventory_usdc: float = 15.0

    # ── Arbitrage ──────────────────────────────────────────────────────
    arb_enabled: bool = True
    arb_min_profit_bps: int = 50
    arb_scan_interval_seconds: int = 15
    arb_order_size_usdc: float = 1.5

    # ── Signal ─────────────────────────────────────────────────────────
    signal_enabled: bool = False
    signal_min_confidence: float = 0.65
    signal_order_size_usdc: float = 1.5

    # ── Dashboard ──────────────────────────────────────────────────────
    dashboard_refresh_ms: int = 500
    log_level: str = "INFO"

    # ── Historical data path ───────────────────────────────────────────
    # W19-1 — Historical Replay Backtest Engine. The canonical path of
    # the SQLite ``market_snapshots`` / ``orderbook_ticks`` DB (created by
    # ``core/timescale_db.py::_init_sqlite_fallback`` and
    # ``core/market_db.py::_init_db``). The env-var override
    # ``MARKET_DB_PATH`` is the same one ``core/timescale_db.py`` reads,
    # so the API route's ``settings.market_db_path`` resolves to the
    # exact path the snapshot recorder wrote to.
    market_db_path: str = Field(
        default=os.environ.get(
            "MARKET_DB_PATH", "/app/data/market_intelligence.db"
        ),
        description="SQLite path for the market_snapshots / orderbook_ticks DB",
    )

    # ── Computed helpers ───────────────────────────────────────────────
    @property
    def has_credentials(self) -> bool:
        return bool(self.poly_private_key and self.poly_private_key != "your_wallet_private_key_here")

    @property
    def has_api_keys(self) -> bool:
        return bool(self.poly_api_key and self.poly_api_secret and self.poly_api_passphrase)

    @property
    def mm_token_ids_list(self) -> list[str]:
        if not self.mm_market_token_ids:
            return []
        return [t.strip() for t in self.mm_market_token_ids.split(",") if t.strip()]

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        v = v.upper()
        if v not in valid:
            raise ValueError(f"log_level must be one of {valid}")
        return v

    @field_validator("trading_mode")
    @classmethod
    def validate_trading_mode(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"paper", "shadow", "live"}:
            raise ValueError("trading_mode must be one of: paper | shadow | live")
        return v

    @model_validator(mode="before")
    @classmethod
    def _derive_mode(cls, values: dict) -> dict:
        """Single source of truth for the trading mode.

        Precedence: TRADING_MODE env > PAPER_TRADE env > defaults. `paper_trade`
        is derived so the legacy flag can never disagree with the canonical mode.
        """
        mode_env = values.get("trading_mode") or os.environ.get("TRADING_MODE")
        if mode_env is None or mode_env.strip().lower() not in {"paper", "shadow", "live"}:
            mode_env = "paper" if values.get("paper_trade", True) is not False else "live"
        values["trading_mode"] = mode_env.strip().lower()
        values["paper_trade"] = values["trading_mode"] != "live"
        return values

    @property
    def mode(self) -> str:
        """Canonical, network-visible trading mode: paper | shadow | live."""
        return self.trading_mode

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


# Global singleton — import this throughout the project
settings = Settings()
