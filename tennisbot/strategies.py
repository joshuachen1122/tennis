"""Paper-trading strategies. Pure functions over a market/score snapshot so they can be replayed
against recorded data in research.

Every strategy is a taker that buys YES on one player's market. Shared guard rails (in the
engine): only while the live board is fresh, only after both the score and the price have been
quiet for a few seconds (so we are pricing *between* points, not racing faster feeds), one open
position per strategy per match, and a cap on entries per match.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Optional

from tennisbot.model.markov import MatchFormat, ScoreState


def taker_fee_cents(count: float, price_cents: float, rate: float = 0.07) -> float:
    """Kalshi taker fee for one order: roundup(rate * C * P * (1-P)) to the cent."""
    p = price_cents / 100.0
    return math.ceil(round(rate * count * p * (1 - p) * 100, 6)) if count > 0 else 0.0


@dataclass
class StrategyParams:
    enabled: bool = True
    min_edge: float = 0.05  # model prob - ask, net of the per-contract taker fee
    min_price: int = 10  # cents; never buy below
    max_price: int = 90  # cents; never buy above
    exit_on_convergence: bool = True  # sell when the bid reaches model - exit_buffer
    exit_buffer: float = 0.01
    stop_edge: Optional[float] = None  # sell if model - bid falls below -stop_edge (None = hold)
    kelly_fraction: float = 0.25
    max_stake_usd: float = 25.0
    max_entries_per_match: int = 3
    # extra state filters (strategy-specific meaning)
    min_side_prob: float = 0.0
    deciding_set_only: bool = False
    min_games_each: int = 0  # both players have at least this many games in the current set (or tiebreak)


DEFAULT_PARAMS: dict[str, StrategyParams] = {
    # Primary hypothesis: the Markov model calibrated to the pre-match price, updated with
    # observed serve points, finds in-play prices that drift from fair between points.
    "model_edge": StrategyParams(min_edge=0.05, min_price=10, max_price=90),
    # The user's hypothesis: late, close deciding sets swing hardest on each point, so mispricing
    # there should be largest. Same model, restricted to 4-4 or later in the deciding set.
    "late_close": StrategyParams(min_edge=0.03, min_price=15, max_price=85, deciding_set_only=True, min_games_each=4),
    # Favorite-longshot test: near-certain leaders are underpriced when retail buys comebacks.
    # Fees are tiny at the extremes. Hold to settlement.
    "closer": StrategyParams(min_edge=0.025, min_price=60, max_price=97, min_side_prob=0.90, exit_on_convergence=False,
                             max_entries_per_match=2),
}


@dataclass
class Snapshot:
    """Everything a strategy needs about one match right now (A = competitor 1)."""

    event_ticker: str
    series: str
    fmt: MatchFormat
    state: ScoreState
    model_a: float
    leverage: float
    bid_a: Optional[int]
    ask_a: Optional[int]
    ask_a_size: float
    bid_b: Optional[int]
    ask_b: Optional[int]
    ask_b_size: float


@dataclass
class Intent:
    strategy: str
    side: str  # "a" or "b"
    ask: int
    ask_size: float
    model_p: float  # probability that the bought side wins
    edge: float  # net of fee per contract
    reason: str = ""
    skip: Optional[str] = None  # set when filtered out (logged as a near-miss)
    extra: dict = field(default_factory=dict)


def _side_quotes(s: Snapshot, side: str) -> tuple[Optional[int], float, float]:
    if side == "a":
        return s.ask_a, s.ask_a_size, s.model_a
    return s.ask_b, s.ask_b_size, 1.0 - s.model_a


def net_edge(model_p: float, ask: int) -> float:
    return model_p - ask / 100.0 - taker_fee_cents(100, ask) / 10000.0  # fee per contract on a 100-lot


def evaluate(name: str, params: StrategyParams, s: Snapshot) -> list[Intent]:
    """Return candidate entries (with ``skip`` set for near-misses worth logging)."""
    if not params.enabled:
        return []
    out: list[Intent] = []
    st = s.state
    if params.deciding_set_only and not st.is_deciding(s.fmt):
        return []
    if params.min_games_each and not st.in_tiebreak(s.fmt) and min(st.games_a, st.games_b) < params.min_games_each:
        return []
    for side in ("a", "b"):
        ask, size, p = _side_quotes(s, side)
        if ask is None or ask <= 0 or ask >= 100:
            continue
        edge = net_edge(p, ask)
        if edge < params.min_edge * 0.6:
            continue
        intent = Intent(name, side, ask, size, p, edge)
        if p < params.min_side_prob:
            intent.skip = "side_prob_below_min"
        elif ask < params.min_price:
            intent.skip = "price_below_min"
        elif ask > params.max_price:
            intent.skip = "price_above_max"
        elif edge < params.min_edge:
            intent.skip = "edge_below_min"
        elif size < 1:
            intent.skip = "no_size"
        out.append(intent)
    return out


def size_contracts(params: StrategyParams, bankroll_cents: float, model_p: float, ask: int, ask_size: float) -> int:
    """Fractional Kelly for a binary contract, capped by stake, available size, and bankroll."""
    c = ask / 100.0
    f = max(0.0, (model_p - c) / (1.0 - c)) * params.kelly_fraction
    stake_cents = min(f * bankroll_cents, params.max_stake_usd * 100)
    n = int(stake_cents // ask)
    return max(0, min(n, int(ask_size)))


def should_exit(params: StrategyParams, model_p: float, bid: Optional[int]) -> Optional[str]:
    if bid is None or bid <= 0:
        return None
    if params.exit_on_convergence and bid / 100.0 >= model_p - params.exit_buffer:
        return "converged"
    if params.stop_edge is not None and model_p - bid / 100.0 < -params.stop_edge:
        return "stop"
    return None


def params_to_dict(p: dict[str, StrategyParams]) -> dict[str, dict]:
    return {k: asdict(v) for k, v in p.items()}


def params_from_dict(d: dict[str, dict]) -> dict[str, StrategyParams]:
    out = {k: StrategyParams(**{**asdict(v)}) for k, v in DEFAULT_PARAMS.items()}
    for name, vals in (d or {}).items():
        base = asdict(out.get(name, StrategyParams()))
        base.update({k: v for k, v in vals.items() if k in base})
        out[name] = StrategyParams(**base)
    return out
