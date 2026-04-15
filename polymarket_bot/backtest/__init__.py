"""Backtesting and stress-testing engine."""

from polymarket_bot.backtest.data_generator import SyntheticMarketGenerator
from polymarket_bot.backtest.engine import BacktestEngine
from polymarket_bot.backtest.metrics import BacktestMetrics

__all__ = ["BacktestEngine", "SyntheticMarketGenerator", "BacktestMetrics"]
