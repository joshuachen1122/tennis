import time

import pytest
from fastapi.testclient import TestClient

from tennisbot.db import Database, Match
from tennisbot.engine import Engine, MatchRT, Settings
from tennisbot.kalshi.client import Book
from tennisbot.model.live import parse_board
from tennisbot.model.markov import MatchFormat
from tennisbot.web.app import create_app


class FakeStream:
    connected = False
    books: dict = {}
    messages = 0


@pytest.fixture
def engine(tmp_path):
    import asyncio

    db = Database(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    asyncio.run(db.init())
    eng = Engine(Settings(), db, client=None, llm=None)  # type: ignore[arg-type]
    eng.stream = FakeStream()  # type: ignore[assignment]
    row = Match(event_ticker="KXATPMATCH-26NOV04AAABBB", series="KXATPMATCH", title="A vs B", player_a="Carlos Alcaraz",
                player_b="Alex Michelsen", market_a="MA", market_b="MB", comp_a="ca", comp_b="cb", pa0=0.67, pb0=0.62,
                tournament="ATP Paris", round="Round Of 32")
    asyncio.run(_save(db, row))
    rt = MatchRT(row=row)
    rt.fmt = MatchFormat()
    rt.board = parse_board({"status": "live", "competitor1_id": "ca", "competitor2_id": "cb", "server": "ca",
                            "competitor1_round_scores": [{"score": 6, "outcome": "winner"}, {"score": 2, "outcome": "ongoing"}],
                            "competitor2_round_scores": [{"score": 4, "outcome": "loser"}, {"score": 1, "outcome": "ongoing"}],
                            "competitor1_current_round_score": 40, "competitor2_current_round_score": 15})
    rt.last_poll_ok = time.time()
    eng._reprice(rt)
    eng.matches[row.event_ticker] = rt
    eng.rest_books["MA"] = Book("MA", {84: 100.0}, {15: 100.0}, time.time())
    eng.rest_books["MB"] = Book("MB", {15: 100.0}, {84: 100.0}, time.time())
    return eng


async def _save(db, row):
    async with db.session() as s:
        s.add(row)
        await s.commit()


def test_pages_render_and_controls_need_password(engine):
    c = TestClient(create_app(engine, "secret"))
    for path in ("/", "/performance", "/lab", "/partials/games", "/partials/summary", "/partials/pnl", "/partials/data",
                 "/match/KXATPMATCH-26NOV04AAABBB", "/partials/match/KXATPMATCH-26NOV04AAABBB", "/api/match/KXATPMATCH-26NOV04AAABBB/chart"):
        r = c.get(path)
        assert r.status_code == 200, (path, r.text[:300])
    assert "No open positions" in c.get("/").text  # default tab: your positions (none yet)
    assert "1 live now" in c.get("/").text  # live matches are counted (recorded), not listed
    assert "Carlos Alcaraz" not in c.get("/?view=live").text  # unknown tab falls back to positions
    assert c.get("/?view=finished").status_code == 200
    mem = c.get("/api/debug/mem").json()
    assert mem["containers"]["matches"] == 1 and mem["top_types"]
    assert c.post("/api/debug/profile?seconds=1").status_code == 401
    assert "function calls" in c.post("/api/debug/profile?seconds=1", auth=("", "secret")).text
    assert c.get("/api/archive/2026-11-01/book_ticks.csv.gz").status_code == 404
    assert c.get("/api/archive/..%2F..%2Fetc/passwd").status_code == 404
    r = c.post("/controls/agent", data={"action": "stop"})
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    assert engine.agent_enabled
    assert c.post("/controls/agent", data={"action": "stop"}, auth=("", "wrong")).status_code == 401
    assert c.post("/controls/agent", data={"action": "stop"}, auth=("", "secret")).status_code == 200
    assert not engine.agent_enabled


async def test_stream_unsubscribes_markets_it_no_longer_wants():
    from tennisbot.kalshi.client import MarketStream

    sent = []

    class WS:
        async def send(self, raw):
            import json

            sent.append(json.loads(raw))

    st = MarketStream(client=None, on_book=lambda t: None, on_trade=lambda m: None)  # type: ignore[arg-type]
    st._ws, st.connected = WS(), True
    st._sids = {"orderbook_delta": 1, "trade": 2}
    await st.set_tickers({"A", "B"})
    st.book("A"), st.book("B")
    sent.clear()
    await st.set_tickers({"B", "C"})
    actions = {(m["params"]["action"], tuple(m["params"]["market_tickers"]), m["params"]["sids"][0]) for m in sent}
    assert actions == {("add_markets", ("C",), 1), ("add_markets", ("C",), 2), ("delete_markets", ("A",), 1), ("delete_markets", ("A",), 2)}
    assert st._subscribed == {"B", "C"} and "A" not in st.books


async def test_stream_coalesces_book_callbacks():
    from tennisbot.kalshi.client import MarketStream

    seen: list[str] = []
    st = MarketStream(client=None, on_book=lambda t: seen.append(t), on_trade=lambda m: None)  # type: ignore[arg-type]
    snap = {"type": "orderbook_snapshot", "sid": 1, "seq": 1,
            "msg": {"market_ticker": "T", "yes_dollars_fp": [["0.4100", "10"]], "no_dollars_fp": [["0.5700", "5"]]}}
    await st._handle(snap)
    assert st.books["T"].bbo() == (41, 10.0, 43, 5.0)
    for seq, price in ((2, "0.4200"), (3, "0.4300")):
        await st._handle({"type": "orderbook_delta", "sid": 1, "seq": seq,
                          "msg": {"market_ticker": "T", "price_dollars": price, "delta_fp": "3", "side": "yes"}})
    assert seen == []  # nothing until the drain
    assert st.books["T"].bbo()[0] == 43  # the cached top of book was refreshed by the deltas
    await st.drain()
    assert seen == ["T"]
    await st.drain()
    assert seen == ["T"]
