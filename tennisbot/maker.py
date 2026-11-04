"""Paper market making: resting limit buys of YES on both players' markets.

Owning YES on player A at ``pa`` and YES on player B at ``pb`` pays 100c whatever happens, so a
completed pair locks in ``100 - pa - pb`` (less maker fees). The risk is the unpaired leg: our
bid tends to fill just as the price moves against it (adverse selection), especially as we read the
score ~13s behind the market. That leg is held to settlement and its P&L measures the problem.

Fill model (deliberately conservative; nothing here touches a real order):
- We join the best bid behind the size already showing there (``queue_ahead``), or, when the spread
  is 3c or wider, improve it by 1c with nobody ahead of us.
- Only a taker *selling* YES (``taker_side == "no"``) fills a YES bid. A print at our price first eats
  the queue ahead; a print below our price would have hit us first, so it fills up to its size.
- If the ask comes down to our price, someone would have sold to us: the rest fills.
- Cancels by others ahead of us are ignored (we never move up the queue except when the level clears).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

MAKER_FEE_SHARE = 0.25  # Kalshi's maker fee is 0.0175 * C * P * (1-P) where the taker rate is 0.07


def maker_fee_cents(count: float, price_cents: float, taker_rate: float = 0.07) -> float:
    p = price_cents / 100.0
    return math.ceil(round(taker_rate * MAKER_FEE_SHARE * count * p * (1 - p) * 100, 6)) if count > 0 else 0.0


@dataclass
class Quote:
    side: str  # "a" | "b"
    ticker: str
    price: int  # cents, a YES bid
    size: float  # contracts wanted
    queue_ahead: float
    posted_at: float
    filled: float = 0.0
    recorded: float = 0.0  # filled contracts already written as paper trades
    context: dict = field(default_factory=dict)

    @property
    def remaining(self) -> float:
        return max(0.0, self.size - self.filled)

    def on_trade(self, price_cents: float, qty: float, taker_side: Optional[str]) -> float:
        """Apply one public trade on our market; return the contracts it filled for us."""
        if taker_side != "no" or self.remaining <= 0 or price_cents > self.price + 1e-9:
            return 0.0
        if price_cents < self.price - 1e-9:
            fill = min(self.remaining, qty)
        else:
            fill = min(self.remaining, max(0.0, qty - self.queue_ahead))
            self.queue_ahead = max(0.0, self.queue_ahead - qty)
        self.filled += fill
        return fill

    def on_top(self, bid: Optional[int], bid_size: float, ask: Optional[int]) -> float:
        """Apply a new top of book; return contracts filled because the ask reached our bid."""
        if ask is not None and ask <= self.price and self.remaining > 0:
            fill = self.remaining
            self.filled += fill
            return fill
        if bid is None or bid < self.price:
            self.queue_ahead = 0.0  # every bid ahead of us cleared without printing through us
        elif bid == self.price and math.isinf(self.queue_ahead):
            self.queue_ahead = bid_size  # we were resting below the best bid; now it is our level
        return 0.0


@dataclass
class Leg:
    qty: float = 0.0
    cost: float = 0.0  # cents, before fees

    @property
    def avg(self) -> float:
        return self.cost / self.qty if self.qty else 0.0


@dataclass
class MakerState:
    """One match's quoting cycle: buy ``target`` of each side, then start again."""

    target: float = 0.0
    quotes: dict = field(default_factory=dict)  # side -> Quote
    legs: dict = field(default_factory=lambda: {"a": Leg(), "b": Leg()})
    cycles: int = 0
    fills: int = 0

    def add_fill(self, q: Quote, qty: float) -> None:
        if qty > 0:
            leg = self.legs[q.side]
            leg.qty += qty
            leg.cost += qty * q.price
            self.fills += 1

    def complete(self) -> bool:
        return self.target > 0 and all(self.legs[s].qty >= self.target - 1e-9 for s in ("a", "b"))


def quote_price(bid: Optional[int], ask: Optional[int]) -> Optional[int]:
    """Join the best bid, or improve it by 1c when the spread is 3c or more."""
    if bid is None or ask is None or bid <= 0 or ask >= 100 or ask - bid < 1:
        return None
    return bid + 1 if ask - bid >= 3 else bid


def desired_prices(bid_a: Optional[int], ask_a: Optional[int], bid_b: Optional[int], ask_b: Optional[int], st: MakerState,
                   min_price: int, max_price: int, min_pair_edge: int) -> dict[str, Optional[int]]:
    """The bid to rest on each side. With nothing filled, both quotes must leave ``min_pair_edge``
    cents between them and 100. Once one side has filled, the other is capped so the pair still
    clears 1c over its average cost (we wait for the price to come back rather than chase it)."""
    raw = {"a": quote_price(bid_a, ask_a), "b": quote_price(bid_b, ask_b)}
    out: dict[str, Optional[int]] = {}
    for s, other in (("a", "b"), ("b", "a")):
        p = raw[s]
        if st.legs[s].qty >= st.target - 1e-9:
            out[s] = None
            continue
        if st.legs[other].qty > 0:  # completing a pair: any price that still makes it pay
            cap = math.floor(100 - st.legs[other].avg - 1)
            p = min(p, cap) if p is not None else cap
            out[s] = p if p >= 1 else None
            continue
        if p is not None and (raw[other] is None or p + raw[other] > 100 - min_pair_edge or not (min_price <= p <= max_price)):
            p = None
        out[s] = p
    return out
