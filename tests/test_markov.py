import random

import pytest

from tennisbot.model.markov import (
    MatchFormat,
    ScoreState,
    leverage,
    next_point_states,
    p_hold,
    prematch_prob,
    solve_serve_split,
    tb_server_is_first,
    win_prob,
)

BO3 = MatchFormat(3, "tb7")
BO5_SLAM = MatchFormat(5, "tb10")
DOUBLES = MatchFormat(3, "mtb10", no_ad=True)


def test_hold_known_values():
    assert p_hold(0.5) == pytest.approx(0.5)
    assert p_hold(0.6) == pytest.approx(0.7357, abs=1e-4)
    assert p_hold(0.6, no_ad=True) < p_hold(0.6)


def test_symmetric_players_are_coin_flips():
    for fmt in (BO3, BO5_SLAM, DOUBLES):
        assert prematch_prob(0.63, 0.63, fmt) == pytest.approx(0.5, abs=1e-9)


def test_tiebreak_serve_order():
    assert [tb_server_is_first(n) for n in range(8)] == [True, False, False, True, True, False, False, True]


def test_terminal_and_monotone():
    assert win_prob(0.6, 0.6, ScoreState(sets_a=2), BO3) == 1.0
    assert win_prob(0.6, 0.6, ScoreState(sets_b=2), BO3) == 0.0
    assert prematch_prob(0.66, 0.62, BO3) > 0.5
    # best of five amplifies the better player's edge
    assert prematch_prob(0.66, 0.62, BO5_SLAM) > prematch_prob(0.66, 0.62, BO3)


def test_serving_for_match_is_high():
    s = ScoreState(1, 0, 5, 3, 3, 0, a_serving=True)  # 40-0 serving for the match
    assert win_prob(0.64, 0.64, s, BO3) > 0.97


def test_solve_serve_split_roundtrip():
    pa, pb = solve_serve_split(0.72, 1.28, BO3)
    assert pa + pb == pytest.approx(1.28)
    assert prematch_prob(pa, pb, BO3) == pytest.approx(0.72, abs=2e-3)
    st = ScoreState(0, 1, 2, 4, 1, 2, a_serving=False)
    pa, pb = solve_serve_split(0.30, 1.28, BO3, st)
    assert win_prob(pa, pb, st, BO3) == pytest.approx(0.30, abs=2e-3)


def test_leverage_is_highest_late_and_close():
    early = leverage(0.64, 0.64, ScoreState(0, 0, 0, 0, 0, 0, True), BO3)
    late = leverage(0.64, 0.64, ScoreState(1, 1, 6, 6, 5, 5, True), BO3)
    assert late > 5 * early


def test_advance_game_and_set():
    up, down = next_point_states(ScoreState(0, 0, 5, 4, 3, 1, True), BO3)
    assert (up.sets_a, up.games_a, up.games_b, up.a_serving) == (1, 0, 0, False)
    assert (down.points_a, down.points_b) == (3, 2)
    up, _ = next_point_states(ScoreState(0, 0, 0, 0, 3, 4, True), BO3)  # AD receiver -> deuce
    assert (up.points_a, up.points_b) == (3, 3)


# ---------------------------------------------------------------- Monte Carlo cross-check


def _simulate(pa, pb, s: ScoreState, fmt: MatchFormat, rng: random.Random) -> bool:
    while True:
        up, down = next_point_states(s, fmt)
        need = fmt.sets_to_win
        if s.in_tiebreak(fmt):
            n = s.points_a + s.points_b
            a_first = s.a_serving if tb_server_is_first(n) else not s.a_serving
            a_serves = tb_server_is_first(n) == a_first
        else:
            a_serves = s.a_serving
        p_point_a = pa if a_serves else 1 - pb
        s = up if rng.random() < p_point_a else down
        if s.sets_a >= need:
            return True
        if s.sets_b >= need:
            return False
        if s.is_deciding(fmt) and fmt.final_set == "mtb10" and (s.points_a or s.points_b):
            pass


@pytest.mark.parametrize(
    "fmt,state",
    [
        (BO3, ScoreState()),
        (BO3, ScoreState(1, 1, 6, 6, 2, 3, False)),
        (BO3, ScoreState(0, 1, 4, 5, 2, 3, True)),
        (BO5_SLAM, ScoreState(2, 2, 6, 6, 4, 6, True)),
        (BO5_SLAM, ScoreState(1, 2, 3, 2, 0, 0, False)),
    ],
)
def test_matches_monte_carlo(fmt, state):
    rng = random.Random(7)
    pa, pb = 0.66, 0.60
    n = 20000
    wins = sum(_simulate(pa, pb, state, fmt, rng) for _ in range(n))
    assert win_prob(pa, pb, state, fmt) == pytest.approx(wins / n, abs=0.012)
