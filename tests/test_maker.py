import math
import time

from tennisbot.maker import MakerState, Quote, desired_prices, maker_fee_cents, quote_price

from tests.test_live_and_strategies import _engine_with_edge


def test_quote_fills_after_the_queue_and_on_prints_through_it():
    q = Quote("a", "MA", 60, 20, queue_ahead=50, posted_at=0)
    assert q.on_trade(60, 30, "yes") == 0  # a taker buying YES never hits a YES bid
    assert q.on_trade(60, 30, "no") == 0 and q.queue_ahead == 20
    assert q.on_trade(60, 25, "no") == 5  # 20 ahead of us, then 5 for us
    assert q.on_trade(59, 4, "no") == 4  # sold below our bid: it would have hit us first
    assert q.on_trade(61, 100, "no") == 0
    assert q.on_top(bid=58, bid_size=10, ask=59) == 11  # the ask reached our bid: the rest fills
    assert q.remaining == 0


def test_quote_below_the_best_bid_waits_for_its_level():
    q = Quote("a", "MA", 58, 10, queue_ahead=math.inf, posted_at=0)
    assert q.on_trade(58, 100, "no") == 0
    q.on_top(bid=58, bid_size=30, ask=60)
    assert q.queue_ahead == 30


def test_desired_prices_need_a_gap_and_cap_the_completing_leg():
    st = MakerState(target=10)
    assert quote_price(60, 61) == 60 and quote_price(60, 64) == 61 and quote_price(None, 61) is None
    assert desired_prices(60, 61, 38, 39, st, 10, 90, 2) == {"a": 60, "b": 38}  # 98: 2c to collect
    assert desired_prices(60, 61, 39, 40, st, 10, 90, 2) == {"a": None, "b": None}  # 99: not enough
    st.add_fill(Quote("a", "MA", 60, 10, 0, 0), 10)
    # a filled at 60; b may pay up to 39 to lock in a cent, and a is done
    assert desired_prices(50, 51, 48, 49, st, 10, 90, 2) == {"a": None, "b": 39}
    assert desired_prices(65, 66, 33, 34, st, 10, 90, 2) == {"a": None, "b": 33}


async def test_engine_pairs_both_legs_and_locks_the_spread(tmp_path):
    from tennisbot.kalshi.client import Book

    eng, rt, db = await _engine_with_edge(tmp_path)
    eng.market_event.update({"MA": "EV", "MB": "EV"})
    eng.stream.connected = True
    for t, b in eng.rest_books.items():
        eng.stream.books[t] = Book(t, dict(b.yes), dict(b.no), time.time())
    bid_a, _, ask_a, _ = eng._bbo("MA")
    bid_b, _, ask_b, _ = eng._bbo("MB")
    assert bid_a + bid_b == 98
    await eng._evaluate(rt, time.time())
    st = eng.maker["EV"]
    assert set(st.quotes) == {"a", "b"} and st.quotes["a"].price == bid_a and st.quotes["a"].queue_ahead == 500
    n = st.target
    eng._on_trade({"market_ticker": "MA", "yes_price_dollars": f"{bid_a / 100:.2f}", "count_fp": str(500 + n), "taker_side": "no", "ts": int(time.time())})
    eng._on_trade({"market_ticker": "MB", "yes_price_dollars": f"{(bid_b - 1) / 100:.2f}", "count_fp": str(n), "taker_side": "no", "ts": int(time.time())})
    rt.last_eval = 0
    await eng._evaluate(rt, time.time())
    legs = [t for t in eng.open_trades.values() if t.strategy == "maker"]
    assert sorted((t.ticker, t.count, t.entry_price) for t in legs) == [("MA", n, bid_a), ("MB", n, bid_b)]
    assert eng.maker["EV"].cycles == 1
    for t in legs:  # A wins
        await eng._close(t, 100.0 if t.ticker == "MA" else 0.0, 0.0, "settled")
    fees = maker_fee_cents(n, bid_a) + maker_fee_cents(n, bid_b)
    assert abs(eng.realized_cents["maker"] - (2 * n - fees)) < 1e-6
    await db.dispose()


async def test_orphaned_trades_on_settled_matches_get_settled(tmp_path):
    from datetime import timedelta

    from tennisbot.db import Match, PaperTrade, utcnow

    eng, rt, db = await _engine_with_edge(tmp_path)
    async with db.session() as s:
        s.add(Match(event_ticker="OLD", series="KXITFMATCH", market_a="OA", market_b="OB", status="settled", result_a=0.0))
        s.add(PaperTrade(strategy="favourite", event_ticker="OLD", ticker="OB", count=10, entry_price=70, entry_fee=2,
                         model_p=0.7, edge=0.02, opened_at=utcnow() - timedelta(hours=3)))
        await s.commit()
    assert not eng.open_trades
    await eng._settle_orphans()
    assert not eng.open_trades
    assert eng.realized_cents["favourite"] == 10 * 30 - 2  # B won: 100c - 70c on 10, less the fee
    await db.dispose()
