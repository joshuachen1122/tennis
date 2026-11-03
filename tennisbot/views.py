"""View models for the match-by-match dashboard (Kalshi / Citadel style): one tile per match with a
live scoreboard, both players' prices, the model, and our paper position in that match."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import select

from tennisbot.db import BookTick, Match, PaperTrade, ScoreEvent, utcnow

if TYPE_CHECKING:
    from tennisbot.engine import Engine, MatchRT

TOURS = {
    "KXATPMATCH": "ATP",
    "KXWTAMATCH": "WTA",
    "KXATPCHALLENGERMATCH": "Challenger",
    "KXWTACHALLENGERMATCH": "WTA 125",
    "KXITFMATCH": "ITF Men",
    "KXITFWMATCH": "ITF Women",
}
TOUR_ORDER = list(TOURS.values())
GATE_TEXT = {
    "ready": "Watching for an edge",
    "agent_stopped": "Agent stopped",
    "no_model": "Waiting for a calm moment to calibrate",
    "feed_delayed": "Score feed delayed",
    "board_stale": "Score feed stale",
    "score_not_quiet": "Point just played",
    "book_stale": "Order book stale",
    "price_not_quiet": "Price moving",
    "wide_spread": "Spread too wide",
    "board_behind_market": "Score feed behind the market",
}


def _aware(d: Optional[datetime]) -> Optional[datetime]:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _pt(x: Any) -> str:
    s = "" if x is None else str(x)
    return "AD" if s == "50" else s


def _trade_dict(engine: "Engine", t: PaperTrade, side_is_a: Optional[bool]) -> dict[str, Any]:
    bid = engine._bbo(t.ticker)[0] if t.closed_at is None else None
    cost = t.count * t.entry_price + t.entry_fee
    from tennisbot.strategies import taker_fee_cents

    unreal = None
    if t.closed_at is None and bid is not None:
        unreal = bid * t.count - taker_fee_cents(t.count, bid, engine.fee_rate.get(t.series, 0.07)) - cost
    return {
        "id": t.id, "strategy": t.strategy, "player": t.player, "count": t.count, "entry": t.entry_price, "cost": cost,
        "bid": bid, "open": t.closed_at is None, "unreal": unreal, "pnl": t.pnl, "exit": t.exit_price, "exit_reason": t.exit_reason,
        "win_payout": t.count * 100 - cost, "loss": -cost, "model_entry": t.model_p, "edge": t.edge, "score": t.score,
        "opened": _aware(t.opened_at), "closed": _aware(t.closed_at), "llm": t.llm_verdict, "side_a": side_is_a,
        "mark_10s": t.mark_10s, "mark_60s": t.mark_60s,
    }


def _players(engine: "Engine", rt: "MatchRT") -> list[dict[str, Any]]:
    row, b = rt.row, rt.board
    out = []
    for side in ("a", "b"):
        is_a = side == "a"
        ticker = row.market_a if is_a else row.market_b
        bid, _, ask, _ = engine._bbo(ticker)
        mid = (bid + ask) / 2 if (bid is not None and ask is not None) else (bid or ask)
        model = None
        if rt.model_a is not None:
            model = rt.model_a if is_a else 1 - rt.model_a
        sets = []
        if b:
            for i, (x, y) in enumerate(b.set_scores):
                mine, theirs = (x, y) if is_a else (y, x)
                done = i < len(b.set_scores) - 1 or not b.is_live
                sets.append({"g": mine, "won": done and mine > theirs})
        serving = bool(b and b.server and ((b.server == row.comp_a) == is_a) and b.is_live)
        winner = bool(b and b.winner and b.winner == (row.comp_a if is_a else row.comp_b))
        out.append({
            "side": side, "name": row.player_a if is_a else row.player_b, "ticker": ticker, "bid": bid, "ask": ask, "mid": mid,
            "model": model, "sets": sets, "points": _pt(b.raw_points_1 if is_a else b.raw_points_2) if (b and b.is_live) else "",
            "serving": serving, "winner": winner, "mult": (100 / ask) if ask else None,
        })
    return out


def game_card(engine: "Engine", rt: "MatchRT", closed: list[PaperTrade]) -> dict[str, Any]:
    row = rt.row
    now = utcnow()
    open_trades = [t for t in engine.open_trades.values() if t.event_ticker == row.event_ticker]
    trades = [_trade_dict(engine, t, t.ticker == row.market_a) for t in sorted(open_trades + closed, key=lambda t: t.opened_at)]
    gate = engine._gate(rt, __import__("time").time()) if rt.live else None
    sched = _aware(row.scheduled)
    status = "live" if rt.live else ("final" if (rt.final or row.status in ("final", "settled")) else "upcoming")
    tl = engine._time_left(rt) if rt.live else None
    unreal = sum(t["unreal"] or 0 for t in trades if t["open"])
    realized = sum(t["pnl"] or 0 for t in trades if not t["open"])
    return {
        "event": row.event_ticker, "tour": TOURS.get(row.series, row.series), "tournament": row.tournament or "", "round": row.round or "",
        "status": status, "scheduled": sched, "starts_in_min": (sched - now).total_seconds() / 60 if sched else None,
        "players": _players(engine, rt), "volume": rt.volume, "fmt": row.fmt, "gate": GATE_TEXT.get(gate or "", gate) if gate else None,
        "trades": trades, "has_open": any(t["open"] for t in trades), "unreal": unreal, "realized": realized,
        "time_left": tl, "final_score": row.final_score, "calib": row.calib_source,
    }


VIEWS = ("mine", "live", "upcoming", "finished")
UPCOMING_LIMIT = 60


def _status(rt: "MatchRT") -> str:
    if rt.live:
        return "live"
    if rt.final or rt.row.status in ("final", "settled"):
        return "final"
    return "upcoming"


async def games_view(engine: "Engine", view: str = "mine", tour: str = "all") -> dict[str, Any]:
    """Build tiles only for the tab being viewed (the page used to render ~300 tiles and hide most
    of them in the browser). Tab counts and the summary come from cheap in-memory passes."""
    view = view if view in VIEWS else "mine"
    since = utcnow() - timedelta(hours=24)
    now = utcnow()
    async with engine.db.session() as s:
        closed = list((await s.execute(select(PaperTrade).where(PaperTrade.closed_at.is_not(None), PaperTrade.opened_at >= since))).scalars())
        finished_rows: dict[str, Match] = {}
        if view == "finished" and closed:
            evs = {t.event_ticker for t in closed}
            finished_rows = {m.event_ticker: m for m in (await s.execute(select(Match).where(Match.event_ticker.in_(evs)))).scalars()}
    by_event: dict[str, list[PaperTrade]] = defaultdict(list)
    for t in closed:
        by_event[t.event_ticker].append(t)
    open_events = {t.event_ticker for t in engine.open_trades.values()}
    horizon = now + timedelta(hours=36)

    def in_tour(rt: "MatchRT") -> bool:
        return tour == "all" or TOURS.get(rt.row.series, rt.row.series) == tour

    counts = {"mine": len(open_events), "live": 0, "upcoming": 0, "finished": 0}
    picked: list["MatchRT"] = []
    for rt in engine.matches.values():
        st = _status(rt)
        if st == "live":
            counts["live"] += 1
        elif st == "upcoming" and (rt.row.scheduled is None or _aware(rt.row.scheduled) <= horizon):
            counts["upcoming"] += 1
        if not in_tour(rt):
            continue
        ev = rt.row.event_ticker
        if view == "mine" and ev in open_events:
            picked.append(rt)
        elif view == "live" and st == "live":
            picked.append(rt)
        elif view == "upcoming" and st == "upcoming" and (rt.row.scheduled is None or _aware(rt.row.scheduled) <= horizon):
            picked.append(rt)
        elif view == "finished" and st == "final" and (ev in by_event or ev in open_events):
            picked.append(rt)
    counts["finished"] = len({t.event_ticker for t in closed} - {rt.row.event_ticker for rt in engine.matches.values() if _status(rt) != "final"})

    more = 0
    if view == "upcoming":
        picked.sort(key=lambda r: (_aware(r.row.scheduled) or now + timedelta(days=9), -(r.volume or 0)))
        more = max(0, len(picked) - UPCOMING_LIMIT)
        picked = picked[:UPCOMING_LIMIT]
    cards = [game_card(engine, rt, by_event.get(rt.row.event_ticker, [])) for rt in picked]
    if view == "finished":
        from tennisbot.engine import MatchRT

        seen = {c["event"] for c in cards}
        for ev, row in finished_rows.items():
            if ev in seen or (tour != "all" and TOURS.get(row.series, row.series) != tour):
                continue
            rt = MatchRT(row=row)
            rt.final = True
            card = game_card(engine, rt, by_event.get(ev, []))
            card["status"] = "final"
            cards.append(card)
        cards.sort(key=lambda c: max((t["closed"] or t["opened"] or now) for t in c["trades"]) if c["trades"] else now, reverse=True)
    elif view in ("mine", "live"):
        cards.sort(key=lambda c: (not c["has_open"], c["status"] != "live", -(c["volume"] or 0)))

    days: list[tuple[str, list[dict]]] = []
    if view == "upcoming":
        grouped: dict[str, list[dict]] = defaultdict(list)
        for c in cards:
            grouped[c["scheduled"].strftime("%a, %b %-d") if c["scheduled"] else "Time to be announced"].append(c)
        days = list(grouped.items())
    return {"view": view, "tour": tour, "cards": cards, "days": days, "more": more, "counts": counts,
            "summary": summary_view(engine, closed)}


def summary_view(engine: "Engine", closed_24h: list[PaperTrade]) -> dict[str, Any]:
    from tennisbot.strategies import taker_fee_cents

    at_risk = unreal = 0.0
    for t in engine.open_trades.values():
        cost = t.count * t.entry_price + t.entry_fee
        at_risk += cost
        bid = engine._bbo(t.ticker)[0]
        if bid is not None:
            unreal += bid * t.count - taker_fee_cents(t.count, bid, engine.fee_rate.get(t.series, 0.07)) - cost
    return {"open": len(engine.open_trades), "at_risk": at_risk, "unreal": unreal, "realized_24h": sum(t.pnl or 0 for t in closed_24h)}


async def game_detail(engine: "Engine", event: str) -> Optional[dict[str, Any]]:
    from tennisbot.engine import MatchRT

    async with engine.db.session() as s:
        row = await s.get(Match, event)
        if row is None:
            return None
        ticks = list((await s.execute(select(BookTick.ts, BookTick.bid_a, BookTick.ask_a, BookTick.model_a)
                                     .where(BookTick.event_ticker == event).order_by(BookTick.id))).all())
        scores = list((await s.execute(select(ScoreEvent).where(ScoreEvent.event_ticker == event).order_by(ScoreEvent.id))).scalars())
        closed = list((await s.execute(select(PaperTrade).where(PaperTrade.event_ticker == event, PaperTrade.closed_at.is_not(None)))).scalars())
    rt = engine.matches.get(event)
    if rt is None:
        rt = MatchRT(row=row)
        rt.final = True
    card = game_card(engine, rt, closed)
    step = max(1, len(ticks) // 1500)  # keep the chart payload small
    pts_a, pts_model = [], []
    for ts, bid, ask, model in ticks[::step]:
        if bid is not None and ask is not None:
            pts_a.append([int(_aware(ts).timestamp() * 1000), (bid + ask) / 2])
    for sc in scores:
        if sc.model_a is not None:
            pts_model.append([int(_aware(sc.ts).timestamp() * 1000), round(sc.model_a * 100, 2)])
    entries = [{"t": int(t["opened"].timestamp() * 1000), "p": t["entry"] if t["side_a"] else 100 - t["entry"],
                "label": f'{t["strategy"]}: {t["player"]} {t["count"]:g} @ {t["entry"]:.0f}¢'} for t in card["trades"] if t["opened"]]
    chart = {"a": row.player_a, "b": row.player_b, "mid_a": pts_a, "model_a": pts_model, "entries": entries,
             "start": int(_aware(row.started_at).timestamp() * 1000) if row.started_at else None}
    log = []
    for sc in scores[::-1][:150]:
        try:
            sets = json.loads(sc.set_scores or "[]")
        except ValueError:
            sets = []
        log.append({"ts": _aware(sc.ts), "sets": " ".join(f"{x}-{y}" for x, y in sets), "pts": f"{_pt(sc.points_a)}-{_pt(sc.points_b)}",
                    "server": "a" if sc.a_serving else ("b" if sc.a_serving is False else ""), "winner": sc.point_winner,
                    "mid": ((sc.bid_a + sc.ask_a) / 2) if (sc.bid_a is not None and sc.ask_a is not None) else None,
                    "model": sc.model_a, "status": sc.status})
    return {"g": card, "row": row, "chart": chart, "log": log}
