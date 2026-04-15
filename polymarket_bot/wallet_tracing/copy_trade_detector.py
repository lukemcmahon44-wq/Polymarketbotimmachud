"""On-chain copy-trade detection using Polygon RPC.

Monitors the Polymarket CTF Exchange for OrderFilled events.
When a wallet trades in a market where we have an open or recent position,
it computes a similarity score and triggers a defensive action.

All data used is PUBLIC on-chain data. No deanonymization, no privacy invasion.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

import structlog

from polymarket_bot.config.settings import BotConfig
from polymarket_bot.execution.order_manager import OrderManager
from polymarket_bot.types import (
    CopyTradeAlert,
    CopyTradeResponse,
    Order,
    Side,
    WalletActivity,
)

logger = structlog.get_logger(__name__)

# Polymarket CTF Exchange ABI (OrderFilled event only)
CTF_EXCHANGE_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "orderHash", "type": "bytes32"},
            {"indexed": True, "name": "maker", "type": "address"},
            {"indexed": True, "name": "taker", "type": "address"},
            {"indexed": False, "name": "makerAssetId", "type": "uint256"},
            {"indexed": False, "name": "takerAssetId", "type": "uint256"},
            {"indexed": False, "name": "makerAmountFilled", "type": "uint256"},
            {"indexed": False, "name": "takerAmountFilled", "type": "uint256"},
            {"indexed": False, "name": "fee", "type": "uint256"},
        ],
        "name": "OrderFilled",
        "type": "event",
    }
]


class CopyTradeDetector:
    """Monitors on-chain activity for wallets copying our trades.

    Strategy:
    1. After we place a trade, record the market, side, size, and timestamp.
    2. Subscribe to on-chain OrderFilled events for the same token.
    3. If a wallet trades the SAME market+side within the time window,
       compute similarity score.
    4. If score >= threshold, execute defensive action.

    Similarity score = weighted combination of:
    - Same market and side: 0.5 weight
    - Timing: closer in time = higher score (0.3 weight)
    - Size proportion: closer size-to-portfolio ratio = higher score (0.2 weight)
    """

    def __init__(
        self,
        config: BotConfig,
        order_manager: OrderManager,
        rpc_url: str | None = None,
    ) -> None:
        self._config = config
        self._om = order_manager
        self._rpc_url = rpc_url or config.polygon_rpc_url
        self._watchlist: dict[str, list[WalletActivity]] = defaultdict(list)
        self._wallet_scores: dict[str, float] = {}
        self._our_recent_orders: deque[tuple[Order, float]] = deque(maxlen=200)
        self._alerts: list[CopyTradeAlert] = []
        self._running = False
        self._web3: Any = None

    async def start(self) -> None:
        """Start monitoring if RPC is configured."""
        if not self._rpc_url:
            logger.warning("copy_trade_monitor_disabled", reason="no RPC URL configured")
            return

        self._running = True
        logger.info("copy_trade_monitor_starting", rpc=self._rpc_url[:30] + "...")
        asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        self._running = False

    def record_our_order(self, order: Order) -> None:
        """Record one of our orders for future copy-trade correlation."""
        self._our_recent_orders.append((order, time.time()))

    def get_alerts(self) -> list[CopyTradeAlert]:
        return list(self._alerts)

    def get_watchlist(self) -> dict[str, float]:
        """Return wallet → similarity score mapping."""
        return dict(self._wallet_scores)

    async def _monitor_loop(self) -> None:
        """Background loop polling for on-chain activity."""
        backoff = 5.0
        while self._running:
            try:
                await self._poll_recent_events()
                backoff = 5.0
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("copy_trade_monitor_error", error=str(e), retry_in=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _poll_recent_events(self) -> None:
        """Poll recent OrderFilled events from the CTF Exchange."""
        try:
            from web3 import Web3
            from web3.middleware import ExtraDataToPOAMiddleware

            if self._web3 is None:
                self._web3 = Web3(Web3.HTTPProvider(self._rpc_url))
                self._web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

            if not self._web3.is_connected():
                logger.warning("rpc_not_connected")
                return

            contract = self._web3.eth.contract(
                address=Web3.to_checksum_address(self._config.ctf_exchange_address),
                abi=CTF_EXCHANGE_ABI,
            )

            latest_block = self._web3.eth.block_number
            from_block = max(0, latest_block - 50)  # Last ~50 blocks (~100s on Polygon)

            events = contract.events.OrderFilled.get_logs(
                fromBlock=from_block, toBlock=latest_block
            )

            for event in events:
                await self._process_event(event)

        except ImportError:
            logger.debug("web3_not_available", msg="pip install web3 for on-chain monitoring")
        except Exception as e:
            logger.debug("event_poll_error", error=str(e))

    async def _process_event(self, event: Any) -> None:
        """Process a single OrderFilled event."""
        try:
            args = event.get("args", {})
            wallet = str(args.get("maker", "")).lower()
            asset_id = str(args.get("makerAssetId", ""))
            amount = int(args.get("makerAmountFilled", 0))
            block_ts = int(time.time())  # approximate

            if not wallet or wallet == "0x" + "0" * 40:
                return

            activity = WalletActivity(
                wallet_address=wallet,
                tx_hash=event.get("transactionHash", b"").hex(),
                token_id=asset_id,
                side=Side.BUY,  # Simplified; real impl decodes asset type
                size=amount / 1e6,  # USDC has 6 decimals
                timestamp=datetime.now(timezone.utc),
                block_number=event.get("blockNumber", 0),
            )

            self._watchlist[wallet].append(activity)
            # Keep only recent entries
            window = self._config.copy_trade.time_window_seconds
            cutoff = time.time() - window
            self._watchlist[wallet] = [
                a for a in self._watchlist[wallet]
                if a.timestamp.timestamp() > cutoff
            ]

            # Check similarity against our recent orders
            for our_order, our_ts in self._our_recent_orders:
                score = self._compute_similarity(our_order, our_ts, activity)
                if score >= self._config.copy_trade.similarity_threshold:
                    self._handle_copy_trade(wallet, our_order, activity, score)
                    self._wallet_scores[wallet] = max(
                        self._wallet_scores.get(wallet, 0), score
                    )

        except Exception as e:
            logger.debug("event_process_error", error=str(e))

    def _compute_similarity(
        self,
        our_order: Order,
        our_ts: float,
        activity: WalletActivity,
    ) -> float:
        """Compute 0.0–1.0 similarity score between our order and wallet activity."""
        score = 0.0

        # Same token (market + side) — 50% weight
        if activity.token_id == our_order.token_id:
            score += 0.5

        # Timing proximity — 30% weight
        time_delta = abs(activity.timestamp.timestamp() - our_ts)
        window = self._config.copy_trade.time_window_seconds
        if time_delta < window:
            timing_score = 1.0 - (time_delta / window)
            score += 0.3 * timing_score

        # Size similarity — 20% weight
        our_size = our_order.size_usd
        their_size = activity.size
        if our_size > 0 and their_size > 0:
            ratio = min(our_size, their_size) / max(our_size, their_size)
            score += 0.2 * ratio

        return min(1.0, score)

    def _handle_copy_trade(
        self,
        wallet: str,
        our_order: Order,
        activity: WalletActivity,
        score: float,
    ) -> None:
        """Execute defensive action for detected copy trader."""
        response_str = self._config.copy_trade.response
        try:
            response = CopyTradeResponse(response_str)
        except ValueError:
            response = CopyTradeResponse.FLAG_ONLY

        alert = CopyTradeAlert(
            suspect_wallet=wallet,
            our_order=our_order,
            suspect_activity=activity,
            similarity_score=score,
            time_delta_seconds=abs(
                activity.timestamp.timestamp() - self._our_recent_orders[-1][1]
                if self._our_recent_orders else 0
            ),
            recommended_action=response,
        )
        self._alerts.append(alert)

        logger.warning(
            "COPY_TRADE_DETECTED",
            wallet=wallet[:10] + "...",
            score=round(score, 3),
            token_id=activity.token_id[:16] + "...",
            action=response.value,
        )
