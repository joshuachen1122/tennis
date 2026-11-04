from tennisbot.model.live import infer_format, parse_board, point_winner, to_state
from tennisbot.model.markov import MatchFormat, ScoreState
from tennisbot.strategies import DEFAULT_PARAMS, PARAMS_VERSION, Snapshot, blend, evaluate, fair_prob, net_edge, should_exit, size_contracts, taker_fee_cents

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
    out = evaluate("model_edge", DEFAULT_PARAMS["model_edge"], snap(0.66, 53, 54))
    trades = [i for i in out if i.skip is None]
    assert len(trades) == 1 and trades[0].side == "a" and trades[0].edge > 0.03
    assert trades[0].extra["raw_model_p"] == 0.66 and abs(trades[0].model_p - 0.5975) < 1e-9  # half model, half mid
    assert net_edge(0.62, 54) < 0.62 - 0.54


def test_blend_shrinks_toward_mid():
    assert blend(0.80, 70, 72, 0.5) == 0.755
    assert blend(0.80, 70, 72, 1.0) == 0.80
    assert blend(0.80, None, 72, 0.5) == 0.80  # no two-sided quote: raw model
    # a raw 6c edge that cleared v2's 5c bar is now only ~1.3c net: no trade
    assert all(i.skip for i in evaluate("model_edge", DEFAULT_PARAMS["model_edge"], snap(0.60, 53, 54)))


def test_favourite_prices_off_the_market_not_the_model():
    from dataclasses import replace

    p = replace(DEFAULT_PARAMS["favourite"], enabled=True)
    assert abs(fair_prob(0.5, 74, 75, p) - 0.8176) < 1e-3  # sigmoid(1.4 * logit(0.745)); the model is ignored
    assert fair_prob(0.9, None, 75, p) is None
    assert abs(fair_prob(0.5, 49, 51, p) - 0.5) < 1e-9  # a coin flip stays a coin flip
    out = [i for i in evaluate("favourite", p, snap(0.10, 74, 75)) if i.skip is None]
    assert len(out) == 1 and out[0].side == "a" and out[0].edge > 0.05
    # the underdog side is never bought, and nothing trades near 50c
    assert all(i.skip for i in evaluate("favourite", p, snap(0.99, 24, 25)) if i.side == "a")
    assert [i for i in evaluate("favourite", p, snap(0.5, 49, 51)) if i.skip is None] == []
    assert should_exit(p, 0.0, 99) is None  # held to settlement


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
    assert any(i.skip is None for i in evaluate("model_edge", p, snap(0.66, 53, 54)))
    assert evaluate("model_edge", p, snap(0.66, 53, 54, calib="inplay")) == []
    assert evaluate("model_edge", p, snap(0.66, 53, 54, series="KXITFMATCH")) == []
    assert evaluate("model_edge", p, snap(0.66, 53, 54, volume=500)) == []
    assert any(i.skip is None for i in evaluate("model_edge", p, snap(0.66, 53, 54, series="KXITFWMATCH")))
    # closer still trades in-play-calibrated matches
    assert any(i.skip is None for i in evaluate("closer", DEFAULT_PARAMS["closer"], snap(0.96, 91, 92, calib="inplay")))


class FakeLLM:
    """Stands in for the Claude reviewer: fixed verdict after a short delay, counts calls."""

    def __init__(self, take: bool, delay: float = 0.05):
        self.take, self.delay, self.calls = take, delay, 0

    async def review_trade(self, ctx, proposed=False):
        from tennisbot.llm import TradeVerdict, Usage

        import asyncio

        self.calls += 1
        await asyncio.sleep(self.delay)
        return TradeVerdict(take=self.take, probability=0.5, confidence=0.5, reasoning="test", risk_flags=[]), Usage("fake", 0.01), "ok"


async def _engine_with_edge(tmp_path, llm=None, edge_cents=12):
    """An engine with one live, quiet, pre-match-calibrated match whose player A is priced
    `edge_cents` below the model."""
    import time as _t

    from tennisbot.db import Database, Match
    from tennisbot.engine import Engine, MatchRT, Settings
    from tennisbot.kalshi.client import Book

    db = Database(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await db.init()
    eng = Engine(Settings(llm_shadow_reviews=False), db, client=None, llm=llm)  # type: ignore[arg-type]
    row = Match(event_ticker="EV", series="KXATPMATCH", market_a="MA", market_b="MB", comp_a=A, comp_b=B, pa0=0.66, pb0=0.62, calib_source="prematch")
    rt = MatchRT(row=row)
    rt.volume = 50_000
    eng.matches["EV"] = rt
    rt.fmt = MatchFormat()
    rt.board, rt.board_changed_at, rt.last_poll_ok = board([2], [2], 15, 0), _t.time() - 10, _t.time()
    eng._reprice(rt)
    ask = int(rt.model_a * 100) - edge_cents
    for t, yes, no in (("MA", ask - 1, 100 - ask), ("MB", 100 - ask - 1, ask)):
        eng.rest_books[t] = Book(t, {yes: 500.0}, {no: 500.0}, _t.time())
    rt.ref_mid = rt.since_lo = rt.since_hi = ask - 0.5
    return eng, rt, db


async def test_concurrent_evaluations_enter_once(tmp_path):
    """Five overlapping evaluations while the LLM gate is thinking: one review, one position."""
    import asyncio
    import time as _t

    llm = FakeLLM(take=True)
    eng, rt, db = await _engine_with_edge(tmp_path, llm)
    await asyncio.gather(*(eng._evaluate(rt, _t.time()) for _ in range(5)))
    await asyncio.sleep(0.2)
    for _ in range(3):  # later ticks must not add a second strategy on the same match
        await eng._evaluate(rt, _t.time())
    await asyncio.sleep(0.2)
    assert llm.calls == 1
    assert len(eng.open_trades) == 1
    (t,) = eng.open_trades.values()
    assert t.strategy == "model_edge" and t.llm_verdict["take"] and t.context["params_version"] == PARAMS_VERSION
    await db.dispose()


async def test_llm_gate_fails_closed_and_skip_blocks(tmp_path):
    import asyncio
    import time as _t

    from sqlalchemy import select

    from tennisbot.db import LLMNote

    eng, rt, db = await _engine_with_edge(tmp_path, llm=None)
    await eng._evaluate(rt, _t.time())
    assert not eng.open_trades  # no LLM: gated strategies do not trade
    await db.dispose()

    (tmp_path / "x").mkdir()
    llm = FakeLLM(take=False)
    eng, rt, db = await _engine_with_edge(tmp_path / "x", llm)
    await eng._evaluate(rt, _t.time())
    await asyncio.sleep(0.2)
    await eng._evaluate(rt, _t.time())
    await asyncio.sleep(0.2)
    assert not eng.open_trades and llm.calls == 1  # the skip starts a cooldown
    async with db.session() as s:
        notes = (await s.execute(select(LLMNote).where(LLMNote.kind == "gate"))).scalars().all()
    assert len(notes) == 1 and notes[0].body["outcome"] == "skip" and notes[0].body["ask"] > 0
    await db.dispose()


async def test_budget_boost_expires(tmp_path):
    from datetime import timedelta

    from tennisbot.db import utcnow

    eng, _, db = await _engine_with_edge(tmp_path)
    eng.s.llm_boost_daily_usd, eng.s.llm_boost_until = 5.0, utcnow() + timedelta(hours=1)
    assert eng.llm_budget() == 5.0
    eng.s.llm_boost_until = utcnow() - timedelta(minutes=1)
    assert eng.llm_budget() == eng.s.llm_daily_budget_usd
    await db.dispose()


async def test_one_position_per_match_across_strategies(tmp_path):
    import time as _t

    from tennisbot.db import PaperTrade

    eng, rt, db = await _engine_with_edge(tmp_path)
    eng.open_trades[1] = PaperTrade(id=1, strategy="model_edge", event_ticker="EV", ticker="MA", count=1, entry_price=50)
    assert eng._position_block(rt, "closer", DEFAULT_PARAMS["closer"], _t.time()) == "match_held"
    assert eng._position_block(rt, "model_edge", DEFAULT_PARAMS["model_edge"], _t.time()) == "already_open"
    await db.dispose()


async def test_restart_keeps_serve_points_and_entries(tmp_path):
    from tennisbot.db import Database, Match, PaperTrade, ScoreEvent, utcnow
    from tennisbot.engine import Engine, Settings

    db = Database(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await db.init()
    rows = [Match(event_ticker="EV", series="KXATPMATCH", market_a="MA", market_b="MB", comp_a=A, comp_b=B, pa0=0.66, pb0=0.62,
                  calib_source="prematch", status="live", first_seen=utcnow())]
    # Each point belongs to the server shown *before* it: A serves three (wins one; B breaks on the
    # third, so B serves next), then B serves one and wins it. The first row has no predecessor.
    for a_serving, pw in ((True, None), (True, 1), (True, 2), (False, 2), (False, 2)):
        rows.append(ScoreEvent(event_ticker="EV", status="live", a_serving=a_serving, point_winner=pw))
    rows.append(PaperTrade(strategy="closer", event_ticker="EV", ticker="MA", count=1, entry_price=90, entry_fee=0,
                           model_p=0.95, edge=0.03, closed_at=utcnow(), exit_price=100, pnl=10))
    await db.add_all(rows)
    eng = Engine(Settings(llm_shadow_reviews=False), db, client=None, llm=None)  # type: ignore[arg-type]
    await eng.restore()
    rt = eng.matches["EV"]
    assert (rt.won_a, rt.played_a, rt.won_b, rt.played_b) == (1, 3, 1, 1)
    assert rt.entries["closer"] == 1
    await db.dispose()
