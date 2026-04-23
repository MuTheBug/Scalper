"""Main scalping bot loop.

Orchestrates market data ingestion, signal evaluation, order placement,
and exit management in line with the 9-step strategy defined in
"Comprehensive Quantitative Framework for Dogecoin Scalping" (2026).
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from typing import Optional

from dotenv import load_dotenv

from .config import CONFIG, BotConfig
from .execution.binance_client import BinanceFuturesClient
from .risk.manager import OpenTrade, RiskManager
from .strategy.signals import SignalEngine, compute_psar_trail


logger = logging.getLogger("scalper")


class ScalpingBot:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        api_key = os.environ.get("BINANCE_API_KEY", "")
        api_secret = os.environ.get("BINANCE_API_SECRET", "")
        self.client = BinanceFuturesClient(
            api_key=api_key,
            api_secret=api_secret,
            testnet=config.testnet,
            symbol=config.symbol,
        )
        self.signals = SignalEngine(config)
        self.risk = RiskManager(config)
        self.active: Optional[OpenTrade] = None
        self._running = True

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        logger.info(
            "Starting scalper on %s (testnet=%s dry_run=%s)",
            self.config.symbol, self.config.testnet, self.config.dry_run,
        )
        if not self.config.dry_run:
            self.client.load_symbol_filters()
            self.client.set_leverage(self.config.leverage)

        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        while self._running:
            try:
                self._tick()
            except Exception:  # noqa: BLE001 — keep the loop alive
                logger.exception("Tick failed")
            time.sleep(self.config.poll_interval_s)

    def _shutdown(self, *_args) -> None:
        logger.info("Shutdown signal received, exiting main loop")
        self._running = False

    # ---- per-tick logic ----------------------------------------------------

    def _tick(self) -> None:
        cfg = self.config
        execution_df = self.client.klines(cfg.execution_interval, cfg.klines_limit)
        trend_df = self.client.klines(cfg.trend_interval, cfg.klines_limit)
        snapshot = self.client.order_book(cfg.orderbook_depth)
        trades = self.client.recent_trades(limit=500)

        current_price = snapshot.mid

        # 1) Manage any open trade before looking for new entries.
        if self.active is not None:
            self._manage_open_trade(execution_df, current_price)
            return

        # 2) Hunt for a fresh signal.
        signal_result = self.signals.evaluate(execution_df, trend_df, snapshot, trades)
        if signal_result is None:
            return

        equity = self._get_equity()
        plan = self.risk.build_plan(
            side=signal_result.side,
            entry=signal_result.entry,
            stop=signal_result.stop,
            equity=equity,
        )
        if plan is None:
            logger.debug("Signal rejected by risk sizing: %s", signal_result)
            return

        logger.info(
            "Entering %s %s qty=%.4f entry=%.6f stop=%.6f tp1=%.6f risk=$%.2f",
            plan.side, cfg.symbol, plan.total_quantity, plan.entry,
            plan.stop, plan.tp1, plan.risk_usd,
        )
        if self.config.dry_run:
            self.active = OpenTrade(plan=plan)
            return

        self._open_position(plan)

    # ---- trade lifecycle ---------------------------------------------------

    def _open_position(self, plan) -> None:
        cfg = self.config
        # Step 8: prefer maker (post-only) entries; fall back to market if rejected.
        try:
            if cfg.prefer_maker:
                self.client.place_limit(plan.side, plan.total_quantity, plan.entry, post_only=True)
                time.sleep(cfg.maker_post_only_timeout_s)
                position = self.client.position()
                filled = abs(float(position.get("positionAmt", 0.0))) >= plan.total_quantity * 0.5
                if not filled:
                    self.client.cancel_all()
                    self.client.place_market(plan.side, plan.total_quantity)
            else:
                self.client.place_market(plan.side, plan.total_quantity)

            self.client.place_stop_market(plan.side, plan.stop, close_position=True)
            self.client.place_take_profit(plan.side, plan.tp1, plan.tp1_quantity)
            self.active = OpenTrade(plan=plan)
        except Exception:
            logger.exception("Order submission failed — flattening")
            self.client.cancel_all()

    def _manage_open_trade(self, execution_df, current_price: float) -> None:
        assert self.active is not None
        trade = self.active

        if self.risk.stop_hit(trade, current_price):
            logger.info("Stop-loss hit at %.6f", current_price)
            self._close_trade("stop")
            return

        if not trade.tp1_filled and self.risk.tp1_hit(trade, current_price):
            logger.info("TP1 filled at %.6f; advancing stop to break-even", current_price)
            self.risk.advance_to_break_even(trade)
            if not self.config.dry_run:
                self.client.cancel_all()
                self.client.place_stop_market(trade.plan.side, trade.plan.entry, close_position=True)
            return

        # Runner management: trail with Parabolic SAR
        if trade.tp1_filled:
            psar = compute_psar_trail(execution_df, self.config.psar_step, self.config.psar_max)
            if self.risk.update_trailing_stop(trade, psar):
                logger.info("Trailing stop advanced to %.6f", trade.trailing_stop)
                if not self.config.dry_run:
                    self.client.cancel_all()
                    self.client.place_stop_market(
                        trade.plan.side, trade.trailing_stop, close_position=True,
                    )

    def _close_trade(self, reason: str) -> None:
        assert self.active is not None
        if not self.config.dry_run:
            trade = self.active
            remaining = (
                trade.plan.runner_quantity if trade.tp1_filled else trade.plan.total_quantity
            )
            try:
                self.client.place_market(
                    "short" if trade.plan.side == "long" else "long",
                    remaining,
                    reduce_only=True,
                )
            finally:
                self.client.cancel_all()
        logger.info("Trade closed (%s)", reason)
        self.active = None

    # ---- helpers -----------------------------------------------------------

    def _get_equity(self) -> float:
        if self.config.dry_run:
            return 1000.0
        return self.client.available_balance(self.config.quote_asset)


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", CONFIG.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    CONFIG.testnet = os.environ.get("BINANCE_TESTNET", "true").lower() == "true"
    CONFIG.dry_run = os.environ.get("DRY_RUN", "true").lower() == "true"

    bot = ScalpingBot(CONFIG)
    try:
        bot.start()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
