"""Backtesting engine with replay, metrics, and Monte Carlo simulation."""

from polymarket_bot.backtest.engine import BacktestEngine
from polymarket_bot.backtest.data_generator import SyntheticDataGenerator
from polymarket_bot.backtest.metrics import compute_metrics

__all__ = ["BacktestEngine", "SyntheticDataGenerator", "compute_metrics"]
