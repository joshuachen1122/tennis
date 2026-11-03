"""Point-based Markov model of a tennis match (Klaassen & Magnus style).

Inputs are each player's probability of winning a point on their own serve (``pa`` for player A,
``pb`` for player B) and the exact score state. Points are treated as i.i.d. given the server,
which Klaassen & Magnus (2001) found to be a close-enough approximation for pricing.

Formats covered:
  * best of 3 or best of 5 sets, advantage or no-ad games
  * standard 7-point tiebreak at 6-6 in every set (tour, Challenger, ITF, WTA 125)
  * 10-point tiebreak at 6-6 in the deciding set (all four Grand Slams since 2022)
  * deciding set replaced by a 10-point match tiebreak (doubles, Laver Cup)
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

FinalSet = Literal["tb7", "tb10", "mtb10"]

_CACHE = 400_000


@dataclass(frozen=True)
class MatchFormat:
    best_of: int = 3
    final_set: FinalSet = "tb7"
    no_ad: bool = False

    @property
    def sets_to_win(self) -> int:
        return (self.best_of + 1) // 2

    @property
    def code(self) -> str:
        return f"bo{self.best_of}-{self.final_set}{'-noad' if self.no_ad else ''}"


@dataclass(frozen=True)
class ScoreState:
    """Score from player A's perspective. ``points_*`` are game points as 0,1,2,3 (=40), 4 (=AD),
    or raw tiebreak points when the current set is in a tiebreak."""

    sets_a: int = 0
    sets_b: int = 0
    games_a: int = 0
    games_b: int = 0
    points_a: int = 0
    points_b: int = 0
    a_serving: bool = True

    def in_tiebreak(self, fmt: MatchFormat) -> bool:
        if self.is_deciding(fmt) and fmt.final_set == "mtb10":
            return True
        return self.games_a == 6 and self.games_b == 6

    def is_deciding(self, fmt: MatchFormat) -> bool:
        need = fmt.sets_to_win
        return self.sets_a == need - 1 and self.sets_b == need - 1


def _r(p: float) -> float:
    return round(min(max(p, 0.01), 0.99), 4)


# --------------------------------------------------------------------------- games


@lru_cache(maxsize=_CACHE)
def _game(p: float, a: int, b: int, no_ad: bool) -> float:
    """P(server wins the game) from server points ``a`` and receiver points ``b``."""
    q = 1.0 - p
    if no_ad:
        if a >= 4:
            return 1.0
        if b >= 4:
            return 0.0
        if a == 3 and b == 3:
            return p
    else:
        if a >= 3 and b >= 3:
            deuce = p * p / (p * p + q * q)
            if a == b:
                return deuce
            if a == b + 1:
                return p + q * deuce
            if b == a + 1:
                return p * deuce
            return 1.0 if a > b else 0.0
        if a >= 4:
            return 1.0
        if b >= 4:
            return 0.0
    return p * _game(p, a + 1, b, no_ad) + q * _game(p, a, b + 1, no_ad)


def p_hold(p: float, no_ad: bool = False) -> float:
    return _game(_r(p), 0, 0, no_ad)


# --------------------------------------------------------------------------- tiebreaks


def tb_server_is_first(n: int) -> bool:
    """In a tiebreak the first server serves point 0, then serves alternate in pairs:
    points 0 | 1,2 | 3,4 | 5,6 ... -> first server serves points 0, 3, 4, 7, 8, ..."""
    return ((n + 1) // 2) % 2 == 0


@lru_cache(maxsize=_CACHE)
def _tb(pa: float, pb: float, a: int, b: int, a_first: bool, target: int) -> float:
    """P(A wins the tiebreak) from points (a, b), where ``a_first`` says whether A served point 0."""
    if a >= target and a - b >= 2:
        return 1.0
    if b >= target and b - a >= 2:
        return 0.0
    n = a + b
    if a == b and a >= target - 1:
        # From a tie at or beyond target-1, each pair of points has one serve by each player.
        win2 = pa * (1.0 - pb)
        lose2 = (1.0 - pa) * pb
        return win2 / (win2 + lose2)
    a_serves = tb_server_is_first(n) == a_first
    pw = pa if a_serves else 1.0 - pb
    return pw * _tb(pa, pb, a + 1, b, a_first, target) + (1.0 - pw) * _tb(pa, pb, a, b + 1, a_first, target)


# --------------------------------------------------------------------------- match


@lru_cache(maxsize=_CACHE)
def _match(pa: float, pb: float, sa: int, sb: int, ga: int, gb: int, a_serves: bool, fmt: MatchFormat) -> float:
    """P(A wins the match) at the start of a game (or tiebreak) with A serving if ``a_serves``."""
    need = fmt.sets_to_win
    if sa >= need:
        return 1.0
    if sb >= need:
        return 0.0
    deciding = sa == need - 1 and sb == need - 1
    if deciding and fmt.final_set == "mtb10":
        return _tb(pa, pb, 0, 0, a_serves, 10)
    if ga == 6 and gb == 6:
        target = 10 if (deciding and fmt.final_set == "tb10") else 7
        w = _tb(pa, pb, 0, 0, a_serves, target)
        # The player who received first in the tiebreak serves first in the next set.
        return w * _match(pa, pb, sa + 1, sb, 0, 0, not a_serves, fmt) + (1.0 - w) * _match(
            pa, pb, sa, sb + 1, 0, 0, not a_serves, fmt
        )
    hold = _game(pa if a_serves else pb, 0, 0, fmt.no_ad)
    a_wins_game = hold if a_serves else 1.0 - hold
    return a_wins_game * _after_game(pa, pb, sa, sb, ga + 1, gb, not a_serves, fmt) + (1.0 - a_wins_game) * _after_game(
        pa, pb, sa, sb, ga, gb + 1, not a_serves, fmt
    )


def _after_game(pa: float, pb: float, sa: int, sb: int, ga: int, gb: int, a_serves_next: bool, fmt: MatchFormat) -> float:
    if (ga >= 6 and ga - gb >= 2) or ga == 7:
        return _match(pa, pb, sa + 1, sb, 0, 0, a_serves_next, fmt)
    if (gb >= 6 and gb - ga >= 2) or gb == 7:
        return _match(pa, pb, sa, sb + 1, 0, 0, a_serves_next, fmt)
    return _match(pa, pb, sa, sb, ga, gb, a_serves_next, fmt)


def win_prob(pa: float, pb: float, state: ScoreState, fmt: MatchFormat) -> float:
    """P(player A wins the match) from any score state."""
    pa, pb = _r(pa), _r(pb)
    s = state
    need = fmt.sets_to_win
    if s.sets_a >= need:
        return 1.0
    if s.sets_b >= need:
        return 0.0
    deciding = s.is_deciding(fmt)
    if s.in_tiebreak(fmt):
        target = 10 if (deciding and fmt.final_set in ("tb10", "mtb10")) else 7
        n = s.points_a + s.points_b
        a_first = s.a_serving if tb_server_is_first(n) else not s.a_serving
        w = _tb(pa, pb, s.points_a, s.points_b, a_first, target)
        if deciding and fmt.final_set == "mtb10":
            return w
        return w * _match(pa, pb, s.sets_a + 1, s.sets_b, 0, 0, not a_first, fmt) + (1.0 - w) * _match(
            pa, pb, s.sets_a, s.sets_b + 1, 0, 0, not a_first, fmt
        )
    if s.a_serving:
        g = _game(pa, s.points_a, s.points_b, fmt.no_ad)
        a_wins_game = g
    else:
        g = _game(pb, s.points_b, s.points_a, fmt.no_ad)
        a_wins_game = 1.0 - g
    return a_wins_game * _after_game(pa, pb, s.sets_a, s.sets_b, s.games_a + 1, s.games_b, not s.a_serving, fmt) + (
        1.0 - a_wins_game
    ) * _after_game(pa, pb, s.sets_a, s.sets_b, s.games_a, s.games_b + 1, not s.a_serving, fmt)


def prematch_prob(pa: float, pb: float, fmt: MatchFormat) -> float:
    """Match-win probability before the toss (average over who serves first)."""
    start_a = ScoreState(a_serving=True)
    start_b = ScoreState(a_serving=False)
    return 0.5 * (win_prob(pa, pb, start_a, fmt) + win_prob(pa, pb, start_b, fmt))


def solve_serve_split(target_prob: float, total: float, fmt: MatchFormat, state: ScoreState | None = None) -> tuple[float, float]:
    """Find (pa, pb) with pa + pb = ``total`` such that the model reproduces ``target_prob``
    (pre-match if ``state`` is None, else at ``state``). Bisection on the serve-probability gap."""
    target = min(max(target_prob, 0.005), 0.995)

    def f(d: float) -> float:
        pa, pb = total / 2 + d, total / 2 - d
        return prematch_prob(pa, pb, fmt) if state is None else win_prob(pa, pb, state, fmt)

    lo, hi = -0.35, 0.35
    lo = max(lo, 0.02 - total / 2)
    hi = min(hi, total / 2 - 0.02)
    if f(lo) >= target:
        d = lo
    elif f(hi) <= target:
        d = hi
    else:
        for _ in range(40):
            mid = (lo + hi) / 2
            if f(mid) < target:
                lo = mid
            else:
                hi = mid
        d = (lo + hi) / 2
    return total / 2 + d, total / 2 - d


def leverage(pa: float, pb: float, state: ScoreState, fmt: MatchFormat) -> float:
    """How much the match price moves on the next point: P(win | A wins point) - P(win | A loses point)."""
    up, down = next_point_states(state, fmt)
    return win_prob(pa, pb, up, fmt) - win_prob(pa, pb, down, fmt)


def next_point_states(state: ScoreState, fmt: MatchFormat) -> tuple[ScoreState, ScoreState]:
    """States after A wins / loses the next point (approximate at game/set boundaries: the new
    game starts at 0-0 with the other server)."""
    return _advance(state, fmt, a_wins=True), _advance(state, fmt, a_wins=False)


def _advance(s: ScoreState, fmt: MatchFormat, a_wins: bool) -> ScoreState:
    pa_, pb_ = s.points_a + (1 if a_wins else 0), s.points_b + (0 if a_wins else 1)
    if s.in_tiebreak(fmt):
        deciding = s.is_deciding(fmt)
        target = 10 if (deciding and fmt.final_set in ("tb10", "mtb10")) else 7
        n = s.points_a + s.points_b
        a_first = s.a_serving if tb_server_is_first(n) else not s.a_serving
        if (pa_ >= target and pa_ - pb_ >= 2) or (pb_ >= target and pb_ - pa_ >= 2):
            a_set = pa_ > pb_
            return ScoreState(s.sets_a + a_set, s.sets_b + (not a_set), 0, 0, 0, 0, not a_first)
        next_server_first = tb_server_is_first(n + 1)
        return ScoreState(s.sets_a, s.sets_b, s.games_a, s.games_b, pa_, pb_, a_first == next_server_first)
    # regular game, points are from A's perspective
    srv, rcv = (pa_, pb_) if s.a_serving else (pb_, pa_)
    done = False
    server_won = False
    if fmt.no_ad:
        if srv >= 4 or rcv >= 4:
            done, server_won = True, srv > rcv
    else:
        if srv >= 4 and srv - rcv >= 2:
            done, server_won = True, True
        elif rcv >= 4 and rcv - srv >= 2:
            done, server_won = True, False
        elif srv == 4 and rcv == 4:
            srv, rcv = 3, 3  # back to deuce
    if not done:
        a_pts, b_pts = (srv, rcv) if s.a_serving else (rcv, srv)
        return ScoreState(s.sets_a, s.sets_b, s.games_a, s.games_b, a_pts, b_pts, s.a_serving)
    a_won_game = server_won == s.a_serving
    ga, gb = s.games_a + a_won_game, s.games_b + (not a_won_game)
    if (ga >= 6 and ga - gb >= 2) or ga == 7:
        return ScoreState(s.sets_a + 1, s.sets_b, 0, 0, 0, 0, not s.a_serving)
    if (gb >= 6 and gb - ga >= 2) or gb == 7:
        return ScoreState(s.sets_a, s.sets_b + 1, 0, 0, 0, 0, not s.a_serving)
    return ScoreState(s.sets_a, s.sets_b, ga, gb, 0, 0, not s.a_serving)


def remaining_points(pa: float, pb: float, state: ScoreState, fmt: MatchFormat, sims: int = 300, seed: int = 0) -> float:
    """Expected number of points left in the match (Monte Carlo over the same point process).
    Used to estimate time remaining; tennis has no clock."""
    import random

    rng = random.Random(seed)
    need = fmt.sets_to_win
    total = 0
    for _ in range(sims):
        s, n = state, 0
        while s.sets_a < need and s.sets_b < need and n < 400:
            if s.in_tiebreak(fmt):
                k = s.points_a + s.points_b
                a_first = s.a_serving if tb_server_is_first(k) else not s.a_serving
                a_serves = tb_server_is_first(k) == a_first
            else:
                a_serves = s.a_serving
            p_point_a = pa if a_serves else 1 - pb
            s = _advance(s, fmt, a_wins=rng.random() < p_point_a)
            n += 1
        total += n
    return total / sims
