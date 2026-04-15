"""Configuration loader with safe defaults and environment variable overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class CopyTradeConfig:
    time_window_seconds: int = 600
    similarity_threshold: float = 0.8
    response: str = "reduce_size"
    enabled: bool = True
    watchlist_max_size: int = 100


@dataclass
class RiskConfig:
    global_exposure_usd: float = 10_000.0
    per_market_exposure_usd: float = 1_000.0
    per_trade_max_usd: float = 500.0
    max_concurrent_positions: int = 10
    slippage_tolerance_pct: float = 0.02
    max_fill_price_deviation_pct: float = 0.03
    order_ttl_seconds: int = 300
    kelly_fraction: float = 0.25  # Quarter-Kelly for safety
    min_liquidity_usd: float = 5_000.0
    stop_loss_pct: float = 0.15
    time_decay_hours: int = 48
    correlation_threshold: float = 0.7
    max_category_exposure_pct: float = 0.4
    drawdown_circuit_breaker_pct: float = 0.10
    anomalous_fill_threshold: float = 3.0
    max_daily_loss_usd: float = 2_000.0


@dataclass
class ExecutionConfig:
    prefer_limit_orders: bool = True
    market_order_slippage_cap_pct: float = 0.03
    order_timeout_seconds: int = 60
    retry_max_attempts: int = 3
    retry_backoff_base_seconds: float = 2.0


@dataclass
class ScannerConfig:
    poll_interval_seconds: int = 30
    websocket_enabled: bool = True
    categories: list[str] = field(
        default_factory=lambda: ["crypto", "finance", "technology", "politics", "sports"]
    )
    min_volume_usd: float = 1_000.0
    min_liquidity_usd: float = 2_000.0
    max_markets: int = 200


@dataclass
class AlertConfig:
    slack_webhook_url: str = ""
    discord_webhook_url: str = ""
    email: str = ""
    alert_on_circuit_breaker: bool = True
    alert_on_copy_trade: bool = True
    alert_on_large_fill: bool = True
    large_fill_threshold_usd: float = 500.0


@dataclass
class RateLimitConfig:
    orders_per_10s: int = 50  # Well under the 3500/10s limit
    data_requests_per_10s: int = 100  # Well under the 1500/10s limit
    max_ws_connections: int = 3  # Under the 5/IP limit
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 60.0


@dataclass
class BotConfig:
    """Top-level bot configuration with conservative defaults."""

    paper_mode: bool = True
    enable_live_trading: bool = False
    first_live_trades_manual_review: int = 10
    ev_threshold: float = 0.02  # 2% min EV
    min_confidence: float = 0.6
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    copy_trade: CopyTradeConfig = field(default_factory=CopyTradeConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    rate_limits: RateLimitConfig = field(default_factory=RateLimitConfig)

    # API connection settings (loaded from env, not YAML)
    polymarket_host: str = "https://clob.polymarket.com"
    chain_id: int = 137
    polygon_rpc_url: str = "https://polygon-rpc.com"
    polygon_ws_url: str = ""

    # CTF Exchange contract for on-chain tracking
    ctf_exchange_address: str = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982e"
    neg_risk_ctf_exchange_address: str = "0xC5d563A36AE78145C45a50134d48A1215220f80a"


def _deep_update(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into base dict."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def _dict_to_config(data: dict[str, Any]) -> BotConfig:
    """Convert a flat/nested dict to BotConfig dataclass."""
    risk_data = data.pop("risk", {})
    exec_data = data.pop("execution", {})
    scanner_data = data.pop("scanner", {})
    copy_data = data.pop("copy_trade_detection", data.pop("copy_trade", {}))
    alert_data = data.pop("alerts", {})
    rate_data = data.pop("rate_limits", {})

    return BotConfig(
        risk=RiskConfig(**{k: v for k, v in risk_data.items() if hasattr(RiskConfig, k)}),
        execution=ExecutionConfig(
            **{k: v for k, v in exec_data.items() if hasattr(ExecutionConfig, k)}
        ),
        scanner=ScannerConfig(
            **{k: v for k, v in scanner_data.items() if hasattr(ScannerConfig, k)}
        ),
        copy_trade=CopyTradeConfig(
            **{k: v for k, v in copy_data.items() if hasattr(CopyTradeConfig, k)}
        ),
        alerts=AlertConfig(**{k: v for k, v in alert_data.items() if hasattr(AlertConfig, k)}),
        rate_limits=RateLimitConfig(
            **{k: v for k, v in rate_data.items() if hasattr(RateLimitConfig, k)}
        ),
        **{k: v for k, v in data.items() if hasattr(BotConfig, k)},
    )


def load_config(config_path: str | Path | None = None) -> BotConfig:
    """Load configuration from YAML file with environment variable overrides.

    Priority: env vars > config file > defaults
    """
    data: dict[str, Any] = {}

    # Load YAML config if provided
    path = config_path or os.environ.get("CONFIG_PATH", "config/default.yaml")
    config_file = Path(path)
    if config_file.exists():
        with open(config_file) as f:
            file_data = yaml.safe_load(f)
            if file_data:
                data = file_data

    # Environment variable overrides (critical settings)
    env_overrides: dict[str, Any] = {}
    if os.environ.get("ENABLE_LIVE_TRADING", "").lower() == "true":
        env_overrides["enable_live_trading"] = True
        env_overrides["paper_mode"] = False
    if os.environ.get("POLYMARKET_HOST"):
        env_overrides["polymarket_host"] = os.environ["POLYMARKET_HOST"]
    if os.environ.get("POLYMARKET_CHAIN_ID"):
        env_overrides["chain_id"] = int(os.environ["POLYMARKET_CHAIN_ID"])
    if os.environ.get("POLYGON_RPC_URL"):
        env_overrides["polygon_rpc_url"] = os.environ["POLYGON_RPC_URL"]
    if os.environ.get("POLYGON_WS_URL"):
        env_overrides["polygon_ws_url"] = os.environ["POLYGON_WS_URL"]
    if os.environ.get("SLACK_WEBHOOK_URL"):
        env_overrides.setdefault("alerts", {})["slack_webhook_url"] = os.environ[
            "SLACK_WEBHOOK_URL"
        ]
    if os.environ.get("GLOBAL_EXPOSURE_USD"):
        env_overrides.setdefault("risk", {})["global_exposure_usd"] = float(
            os.environ["GLOBAL_EXPOSURE_USD"]
        )

    _deep_update(data, env_overrides)
    return _dict_to_config(data)
