"""Risk & position-sizing (PDF Steps 7-9).

Encapsulates:
  * Per-trade 1-2% equity risk cap (Step 7)
  * Bifurcated exit: TP1 at 1:1 for 60-75% of size, TP2 runner trailed
    by Parabolic SAR (Steps 8-9)
  * Break-even advancement after TP1 fills (Step 9)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..config import BotConfig


@dataclass
class TradePlan:
    side: str
    entry: float
    stop: float
    tp1: float
    tp1_quantity: float
    runner_quantity: float
    total_quantity: float
    risk_usd: float
    notional: float


@dataclass
class OpenTrade:
    plan: TradePlan
    tp1_filled: bool = False
    break_even_set: bool = False
    trailing_stop: Optional[float] = None
    history: list = field(default_factory=list)


class RiskManager:
    def __init__(self, config: BotConfig) -> None:
        self.config = config

    # ---- sizing ------------------------------------------------------------

    def build_plan(
        self,
        side: str,
        entry: float,
        stop: float,
        equity: float,
    ) -> Optional[TradePlan]:
        cfg = self.config
        risk_pct = min(cfg.risk_per_trade, cfg.max_risk_per_trade)
        risk_usd = equity * risk_pct

        stop_distance = abs(entry - stop)
        if stop_distance <= 0:
            return None

        total_qty = risk_usd / stop_distance
        if total_qty * entry < cfg.min_notional:
            return None

        tp1_distance = stop_distance * cfg.tp1_rr
        tp1_price = entry + tp1_distance if side == "long" else entry - tp1_distance

        tp1_qty = total_qty * cfg.tp1_fraction
        runner_qty = total_qty - tp1_qty
        return TradePlan(
            side=side,
            entry=entry,
            stop=stop,
            tp1=tp1_price,
            tp1_quantity=tp1_qty,
            runner_quantity=runner_qty,
            total_quantity=total_qty,
            risk_usd=risk_usd,
            notional=total_qty * entry,
        )

    # ---- live trade management --------------------------------------------

    def tp1_hit(self, trade: OpenTrade, current_price: float) -> bool:
        plan = trade.plan
        if trade.tp1_filled:
            return False
        if plan.side == "long":
            return current_price >= plan.tp1
        return current_price <= plan.tp1

    def stop_hit(self, trade: OpenTrade, current_price: float) -> bool:
        plan = trade.plan
        stop = trade.trailing_stop if trade.trailing_stop is not None else plan.stop
        if plan.side == "long":
            return current_price <= stop
        return current_price >= stop

    def advance_to_break_even(self, trade: OpenTrade) -> None:
        plan = trade.plan
        trade.trailing_stop = plan.entry
        trade.break_even_set = True
        trade.tp1_filled = True

    def update_trailing_stop(self, trade: OpenTrade, psar: float) -> bool:
        """Move the runner's trailing stop in the favorable direction only."""
        if not trade.tp1_filled:
            return False
        plan = trade.plan
        current = trade.trailing_stop or plan.stop
        if plan.side == "long" and psar > current:
            trade.trailing_stop = psar
            return True
        if plan.side == "short" and psar < current:
            trade.trailing_stop = psar
            return True
        return False
