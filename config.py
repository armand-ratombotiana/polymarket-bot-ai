"""
config.py — Pydantic-based settings for the Polymarket bot.
Reads from environment variables / .env file.
"""
from __future__ import annotations

from typing import List, Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

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

    # ── Mode ───────────────────────────────────────────────────────────
    paper_trade: bool = True

    # ── Risk ($200 USD Max Portfolio Allocation) ───────────────────────
    max_open_orders: int = 10
    max_position_per_market_usdc: float = 40.0
    max_total_exposure_usdc: float = 180.0
    daily_loss_limit_usdc: float = 20.0

    # ── Market Making ──────────────────────────────────────────────────
    mm_enabled: bool = True
    mm_market_token_ids: str = ""   # comma-separated token IDs
    mm_spread_bps: int = 200
    mm_quote_size_usdc: float = 5.0
    mm_max_inventory_usdc: float = 40.0

    # ── Arbitrage ──────────────────────────────────────────────────────
    arb_enabled: bool = True
    arb_min_profit_bps: int = 50
    arb_scan_interval_seconds: int = 30
    arb_order_size_usdc: float = 10.0

    # ── Signal ─────────────────────────────────────────────────────────
    signal_enabled: bool = False
    signal_min_confidence: float = 0.65
    signal_order_size_usdc: float = 5.0

    # ── Dashboard ──────────────────────────────────────────────────────
    dashboard_refresh_ms: int = 500
    log_level: str = "INFO"

    # ── Computed helpers ───────────────────────────────────────────────
    @property
    def has_credentials(self) -> bool:
        return bool(self.poly_private_key and self.poly_private_key != "your_wallet_private_key_here")

    @property
    def has_api_keys(self) -> bool:
        return bool(self.poly_api_key and self.poly_api_secret and self.poly_api_passphrase)

    @property
    def mm_token_ids_list(self) -> List[str]:
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


# Global singleton — import this throughout the project
settings = Settings()
