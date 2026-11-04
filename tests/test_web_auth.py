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
