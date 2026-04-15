"""Configuration loader — merges YAML defaults with environment overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "default.yaml"


@dataclass
class CopyTradeConfig:
    enabled: bool = True
    time_window_seconds: int = 600
    similarity_threshold: float = 0.8
    min_matching_trades: int = 3
    response: str = "reduce_size"
    size_reduction_factor: float = 0.5
    timing_randomize_seconds: int = 30


@dataclass
class ScannerConfig:
    poll_interval_seconds: int = 10
    categories: list[str] = field(default_factory=lambda: ["crypto", "finance", "technology"])
    max_markets_to_track: int = 200
    websocket_enabled: bool = True
    websocket_reconnect_delay_seconds: int = 5


@dataclass
class RateLimitConfig:
    rest_requests_per_10s: int = 9000
    order_requests_per_10s: int = 3500
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 60.0
    backoff_multiplier: float = 2.0


@dataclass
class MonitoringConfig:
    log_level: str = "INFO"
    structured_logging: bool = True
    metrics_export_interval_seconds: int = 60
    alert_webhook_url: str = ""
    alert_on_circuit_breaker: bool = True
    alert_on_large_fill: bool = True
    large_fill_threshold_usd: float = 250.0


@dataclass
class BacktestConfig:
    default_slippage_bps: int = 50
    default_latency_ms: int = 200
    monte_carlo_runs: int = 1000
    fee_rate: float = 0.02


@dataclass
class BotConfig:
    """Complete bot configuration with conservative defaults."""

    # Trading mode
    paper_mode: bool = True
    first_live_trades_manual_review: int = 10
    live_passphrase: str = "I UNDERSTAND AND ENABLE LIVE TRADING"

    # Layer 0: Hard limits
    global_exposure_usd: float = 10_000.0
    per_market_exposure_usd: float = 1_000.0
    per_trade_max_usd: float = 500.0
    max_concurrent_positions: int = 10

    # Layer 1: Position sizing
    kelly_fraction: float = 0.25
    min_liquidity_usd: float = 5_000.0
    min_ev_threshold: float = 0.02
    confidence_floor: float = 0.55

    # Layer 2: Execution
    slippage_tolerance_pct: float = 0.02
    max_fill_deviation_pct: float = 0.03
    order_ttl_seconds: int = 120
    prefer_limit_orders: bool = True

    # Layer 3: Stop & time decay
    stop_loss_pct: float = 0.15
    time_decay_hours: int = 168
    trailing_stop_pct: float = 0.10

    # Layer 4: Portfolio hedging
    max_category_concentration_pct: float = 0.40
    correlation_threshold: float = 0.70

    # Layer 5: Circuit breakers
    max_drawdown_pct: float = 0.10
    max_consecutive_losses: int = 5
    rpc_failure_threshold: int = 3
    anomalous_fill_deviation: float = 0.10

    # Layer 6: Compliance
    no_wash_trading: bool = True
    min_time_between_opposing_trades_seconds: int = 300

    # Sub-configs
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    rate_limits: RateLimitConfig = field(default_factory=RateLimitConfig)
    copy_trade_detection: CopyTradeConfig = field(default_factory=CopyTradeConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)

    # Environment-sourced (never in YAML)
    private_key: str = field(default="", repr=False)
    wallet_address: str = ""
    polygon_rpc_url: str = "https://polygon-rpc.com"
    polymarket_clob_url: str = "https://clob.polymarket.com"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    enable_live_trading_env: bool = False


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = base.copy()
    for key, val in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


def _build_sub_config(cls: type, data: dict[str, Any]) -> Any:
    valid_keys = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in data.items() if k in valid_keys})


def load_config(path: str | Path | None = None) -> BotConfig:
    """Load config from YAML file, then overlay environment variables."""
    config_path = Path(path) if path else _DEFAULT_CONFIG_PATH

    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}

    # Build sub-configs
    scanner_data = raw.pop("scanner", {})
    rate_data = raw.pop("rate_limits", {})
    copy_data = raw.pop("copy_trade_detection", {})
    monitoring_data = raw.pop("monitoring", {})
    backtest_data = raw.pop("backtest", {})

    # Filter to valid top-level fields
    valid_top = {f.name for f in BotConfig.__dataclass_fields__.values()}
    top_data = {k: v for k, v in raw.items() if k in valid_top}

    cfg = BotConfig(
        **top_data,
        scanner=_build_sub_config(ScannerConfig, scanner_data),
        rate_limits=_build_sub_config(RateLimitConfig, rate_data),
        copy_trade_detection=_build_sub_config(CopyTradeConfig, copy_data),
        monitoring=_build_sub_config(MonitoringConfig, monitoring_data),
        backtest=_build_sub_config(BacktestConfig, backtest_data),
    )

    # Environment overrides (secrets never in YAML)
    cfg.private_key = os.environ.get("PRIVATE_KEY", "")
    cfg.wallet_address = os.environ.get("WALLET_ADDRESS", "")
    cfg.polygon_rpc_url = os.environ.get("POLYGON_RPC_URL", cfg.polygon_rpc_url)
    cfg.polymarket_clob_url = os.environ.get("POLYMARKET_CLOB_URL", cfg.polymarket_clob_url)
    cfg.polymarket_gamma_url = os.environ.get("POLYMARKET_GAMMA_URL", cfg.polymarket_gamma_url)
    cfg.enable_live_trading_env = os.environ.get("ENABLE_LIVE_TRADING", "false").lower() == "true"
    cfg.monitoring.alert_webhook_url = os.environ.get(
        "ALERT_WEBHOOK_URL", cfg.monitoring.alert_webhook_url
    )
    cfg.monitoring.log_level = os.environ.get("LOG_LEVEL", cfg.monitoring.log_level)

    return cfg
