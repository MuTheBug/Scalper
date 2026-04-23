"""Depth-of-Market / order-flow utilities (PDF Step 4).

The PDF describes two actionable patterns:
  * Walls: a resting limit-order cluster that is >= N-times the mean
    size of the surrounding levels. Retail interprets the wall as
    resistance; whales use it to mask accumulation.
  * Absorption: heavy aggressive flow into a price level that fails to
    move the last price, evidenced by a lopsided trade delta.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np


Level = Tuple[float, float]  # (price, size)


@dataclass
class Wall:
    side: str  # "bid" or "ask"
    price: float
    size: float
    ratio: float


@dataclass
class OrderBookSnapshot:
    bids: Sequence[Level]
    asks: Sequence[Level]

    @property
    def best_bid(self) -> float:
        return float(self.bids[0][0])

    @property
    def best_ask(self) -> float:
        return float(self.asks[0][0])

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid


def find_walls(snapshot: OrderBookSnapshot, multiplier: float = 4.0) -> List[Wall]:
    walls: List[Wall] = []
    for side, levels in (("bid", snapshot.bids), ("ask", snapshot.asks)):
        sizes = np.array([float(s) for _, s in levels], dtype=float)
        if len(sizes) == 0:
            continue
        mean = sizes.mean()
        if mean == 0:
            continue
        for (price, size), s in zip(levels, sizes):
            if s >= multiplier * mean:
                walls.append(Wall(side=side, price=float(price), size=float(s), ratio=float(s / mean)))
    return walls


def detect_absorption(
    recent_trades: Sequence[dict],
    threshold: float = 0.6,
    min_volume: float = 0.0,
) -> Optional[str]:
    """Classify recent trades as bullish/bearish absorption.

    Each trade dict is expected to contain at least ``qty`` and ``isBuyerMaker``.
    Binance reports ``isBuyerMaker=True`` when the buyer's order was resting on
    the book — i.e. the trade was a market-sell hitting the bid, contributing
    to negative taker delta.
    """
    if not recent_trades:
        return None

    buy_volume = 0.0
    sell_volume = 0.0
    for trade in recent_trades:
        qty = float(trade.get("qty", trade.get("q", 0.0)))
        buyer_maker = trade.get("isBuyerMaker", trade.get("m", False))
        if buyer_maker:
            sell_volume += qty
        else:
            buy_volume += qty

    total = buy_volume + sell_volume
    if total < min_volume or total == 0:
        return None

    delta = (buy_volume - sell_volume) / total
    # Aggressive sellers dominate but price is holding -> bullish absorption
    if delta <= -threshold:
        return "bullish"
    if delta >= threshold:
        return "bearish"
    return None


def nearest_support_resistance(
    snapshot: OrderBookSnapshot,
    walls: Sequence[Wall],
) -> Tuple[Optional[float], Optional[float]]:
    """Return the nearest wall below and above the mid price."""
    mid = snapshot.mid
    support = max(
        (w.price for w in walls if w.side == "bid" and w.price < mid), default=None
    )
    resistance = min(
        (w.price for w in walls if w.side == "ask" and w.price > mid), default=None
    )
    return support, resistance
