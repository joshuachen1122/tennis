from tennisbot.model.live import infer_format, parse_board, point_winner, to_state
from tennisbot.model.markov import MatchFormat, ScoreState
from tennisbot.strategies import DEFAULT_PARAMS, Snapshot, evaluate, net_edge, should_exit, size_contracts, taker_fee_cents

A, B = "comp-a", "comp-b"


def board(r1, r2, p1, p2, server=A, status="live", **kw):
    def rs(scores, other):
        out = []
        for i, (x, y) in enumerate(zip(scores, other)):
            last = i == len(scores) - 1
            out.append({"score": x, "outcome": "ongoing" if last else ("winner" if x > y else "loser")})
        return out

    d = {
        "status": status, "competitor1_id": A, "competitor2_id": B, "server": server,
        "competitor1_round_scores": rs(r1, r2), "competitor2_round_scores": rs(r2, r1),
        "competitor1_current_round_score": p1, "competitor2_current_round_score": p2, "winner": "",
    }
    d.update(kw)
    return parse_board(d)


def test_parse_and_state():
    b = board([6, 1, 0], [4, 6, 3], 40, 50, server=B)
    assert (b.sets_1, b.sets_2, b.games_1, b.games_2) == (1, 1, 0, 3)
    st = to_state(b, MatchFormat(3, "tb7"), A, B)
    assert st == ScoreState(1, 1, 0, 3, 3, 4, a_serving=False)
    assert to_state(board([2], [2], 15, 0, server="someone-else"), MatchFormat(), A, B) is None


def test_tiebreak_points_are_raw():
    b = board([6, 6], [3, 6], 4, 5)
    st = to_state(b, MatchFormat(), A, B)
    assert st.games_a == st.games_b == 6 and (st.points_a, st.points_b) == (4, 5)


def test_point_winner():
    assert point_winner(board([2], [2], 15, 0), board([2], [2], 30, 0)) == 1
    assert point_winner(board([2], [2], 40, 40), board([2], [2], 40, 50)) == 2
    assert point_winner(board([2], [2], 50, 40), board([2], [2], 40, 40)) == 2
    assert point_winner(board([2], [2], 40, 15), board([3], [2], 0, 0)) == 1
    assert point_winner(board([2], [2], 15, 0), board([2], [2], 40, 0)) is None  # missed a point


def test_infer_format():
    assert infer_format("2026 US Open Men Singles", "5", False).code == "bo5-tb10"
    assert infer_format("ATP Paris", "3", False).code == "bo3-tb7"
    assert infer_format("W15 Antalya", "3", True).code == "bo3-mtb10-noad"
    assert infer_format("Next Gen ATP Finals", "5", False) is None


def test_fees():
    assert taker_fee_cents(100, 50) == 175
    assert taker_fee_cents(1, 50) == 2  # rounds up to the cent
    assert taker_fee_cents(10, 90) == 7


def snap(model_a, bid_a, ask_a, state=ScoreState(), fmt=MatchFormat(), series="KXATPMATCH", calib="prematch", volume=50_000):
    return Snapshot("EV", series, fmt, state, model_a, 0.05, bid_a, ask_a, 500.0, 100 - ask_a, 100 - bid_a, 500.0, calib=calib, volume=volume)


def test_model_edge_buys_underpriced_side():
    out = evaluate("model_edge", DEFAULT_PARAMS["model_edge"], snap(0.62, 53, 54))
    trades = [i for i in out if i.skip is None]
    assert len(trades) == 1 and trades[0].side == "a" and trades[0].edge > 0.05
    assert net_edge(0.62, 54) < 0.62 - 0.54


def test_late_close_only_in_tight_deciding_set():
    p = DEFAULT_PARAMS["late_close"]
    assert evaluate("late_close", p, snap(0.62, 53, 54, ScoreState(1, 0, 4, 4, 0, 0, True))) == []
    out = evaluate("late_close", p, snap(0.62, 53, 54, ScoreState(1, 1, 4, 5, 0, 0, True)))
    assert any(i.skip is None for i in out)


def test_closer_needs_big_favorite():
    p = DEFAULT_PARAMS["closer"]
    assert all(i.skip for i in evaluate("closer", p, snap(0.80, 70, 72)))
    assert any(i.skip is None for i in evaluate("closer", p, snap(0.96, 91, 92)))


def test_sizing_and_exit():
    p = DEFAULT_PARAMS["model_edge"]
    n = size_contracts(p, 100_000, 0.62, 54, 1000)
    assert 0 < n * 54 <= p.max_stake_usd * 100
    assert size_contracts(p, 100_000, 0.62, 54, 3) == 3
    assert should_exit(p, 0.60, 63) == "overshoot"  # sell only once the bid is 3c above the model
    assert should_exit(p, 0.60, 61) is None  # mere convergence: hold (avoids a second taker fee)
    assert should_exit(DEFAULT_PARAMS["closer"], 0.95, 99) is None  # closer always holds


def test_model_strategies_skip_inplay_calibration_itf_men_and_thin_matches():
    p = DEFAULT_PARAMS["model_edge"]
    assert any(i.skip is None for i in evaluate("model_edge", p, snap(0.62, 53, 54)))
    assert evaluate("model_edge", p, snap(0.62, 53, 54, calib="inplay")) == []
    assert evaluate("model_edge", p, snap(0.62, 53, 54, series="KXITFMATCH")) == []
    assert evaluate("model_edge", p, snap(0.62, 53, 54, volume=500)) == []
    assert any(i.skip is None for i in evaluate("model_edge", p, snap(0.62, 53, 54, series="KXITFWMATCH")))
    # closer still trades in-play-calibrated matches
    assert any(i.skip is None for i in evaluate("closer", DEFAULT_PARAMS["closer"], snap(0.96, 91, 92, calib="inplay")))


async def test_concurrent_evaluations_enter_once(tmp_path):
    import asyncio
    import time as _t

    from tennisbot.db import Database, Match
    from tennisbot.engine import Engine, MatchRT, Settings
    from tennisbot.kalshi.client import Book

    db = Database(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await db.init()
    eng = Engine(Settings(llm_shadow_reviews=False), db, client=None, llm=None)  # type: ignore[arg-type]
    row = Match(event_ticker="EV", series="KXATPMATCH", market_a="MA", market_b="MB", comp_a=A, comp_b=B, pa0=0.66, pb0=0.62, calib_source="prematch")
    rt = MatchRT(row=row)
    rt.volume = 50_000
    eng.matches["EV"] = rt
    rt.fmt = MatchFormat()
    b = board([2], [2], 15, 0)
    rt.board, rt.board_changed_at, rt.last_poll_ok = b, _t.time() - 10, _t.time()
    eng._reprice(rt)
    p = rt.model_a
    ask = int(p * 100) - 8
    for t, yes, no in (("MA", ask - 1, 100 - ask), ("MB", 100 - ask - 1, ask)):
        bk = Book(t, {yes: 500.0}, {no: 500.0}, _t.time())
        eng.rest_books[t] = bk
    rt.ref_mid = rt.since_lo = rt.since_hi = ask - 0.5
    await asyncio.gather(*(eng._evaluate(rt, _t.time()) for _ in range(5)))
    opened = [t for t in eng.open_trades.values() if t.strategy == "model_edge"]
    assert len(opened) == 1
    await db.dispose()
