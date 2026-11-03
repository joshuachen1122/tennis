"""Aggregations over the recorded data: dashboard stats and the analyst's report."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Optional

from sqlalchemy import select

from tennisbot.db import Database, LLMNote, Match, PaperTrade, ScoreEvent, Signal
from tennisbot.strategies import StrategyParams, params_to_dict


def _bucket(x: Optional[float], edges: list[float], fmt: str = "{:.0f}") -> str:
    if x is None:
        return "n/a"
    for lo, hi in zip(edges, edges[1:]):
        if lo <= x < hi:
            return f"{fmt.format(lo)}-{fmt.format(hi)}"
    return f">={fmt.format(edges[-1])}" if x >= edges[-1] else f"<{fmt.format(edges[0])}"


def _summ(trades: list[PaperTrade]) -> dict[str, Any]:
    closed = [t for t in trades if t.pnl is not None]
    staked = sum(t.count * t.entry_price for t in closed)
    pnl = sum(t.pnl or 0 for t in closed)
    wins = sum(1 for t in closed if (t.pnl or 0) > 0)
    m10 = [(t.mark_10s - t.entry_price) for t in trades if t.mark_10s is not None]
    m60 = [(t.mark_60s - t.entry_price) for t in trades if t.mark_60s is not None]
    exp = [(t.model_p * 100 - t.entry_price) * t.count for t in closed]
    return {
        "trades": len(trades),
        "closed": len(closed),
        "open": len(trades) - len(closed),
        "wins": wins,
        "win_rate": wins / len(closed) if closed else None,
        "pnl_usd": pnl / 100,
        "fees_usd": sum(t.entry_fee + (t.exit_fee or 0) for t in trades) / 100,
        "staked_usd": staked / 100,
        "roi": pnl / staked if staked else None,
        "avg_edge": sum(t.edge for t in trades) / len(trades) if trades else None,
        "model_expected_pnl_usd": sum(exp) / 100,
        "avg_markout_10s_c": sum(m10) / len(m10) if m10 else None,
        "avg_markout_60s_c": sum(m60) / len(m60) if m60 else None,
    }


async def strategy_stats(db: Database, params: dict[str, StrategyParams], bankroll_usd: float) -> dict[str, Any]:
    async with db.session() as s:
        trades = list((await s.execute(select(PaperTrade).order_by(PaperTrade.id.desc()).limit(5000))).scalars())
    by: dict[str, list[PaperTrade]] = defaultdict(list)
    for t in trades:
        by[t.strategy].append(t)
    out = {}
    for name in sorted(set(params) | set(by)):
        st = _summ(by.get(name, []))
        st["equity_usd"] = bankroll_usd + st["pnl_usd"]
        st["enabled"] = params[name].enabled if name in params else False
        llm = [t for t in by.get(name, []) if t.llm_verdict and t.pnl is not None]
        agree = [t for t in llm if t.llm_verdict.get("take")]
        st["llm_reviewed"] = len(llm)
        st["llm_take_pnl_usd"] = sum(t.pnl or 0 for t in agree) / 100 if llm else None
        st["llm_skip_pnl_usd"] = sum(t.pnl or 0 for t in llm if not t.llm_verdict.get("take")) / 100 if llm else None
        out[name] = st
    return {"strategies": out, "recent": trades[:40]}


async def build_report(db: Database, params: dict[str, StrategyParams]) -> dict[str, Any]:
    """Everything the analyst needs, aggregated (no raw rows)."""
    async with db.session() as s:
        trades = list((await s.execute(select(PaperTrade))).scalars())
        matches = {m.event_ticker: m for m in (await s.execute(select(Match).where(Match.result_a.is_not(None)))).scalars()}
        events = list((await s.execute(
            select(ScoreEvent.event_ticker, ScoreEvent.model_a, ScoreEvent.bid_a, ScoreEvent.ask_a, ScoreEvent.sets_a, ScoreEvent.sets_b,
                   ScoreEvent.games_a, ScoreEvent.games_b, ScoreEvent.leverage)
            .where(ScoreEvent.model_a.is_not(None)).order_by(ScoreEvent.id.desc()).limit(60000)
        )).all())
        signals = list((await s.execute(select(Signal.strategy, Signal.action).order_by(Signal.id.desc()).limit(50000))).all())
        notes = list((await s.execute(select(LLMNote).where(LLMNote.kind == "review").order_by(LLMNote.id.desc()).limit(2))).scalars())

    by_strat: dict[str, list[PaperTrade]] = defaultdict(list)
    for t in trades:
        by_strat[t.strategy].append(t)
    breakdown: dict[str, Any] = {}
    for name, ts in by_strat.items():
        dims: dict[str, dict[str, list[PaperTrade]]] = defaultdict(lambda: defaultdict(list))
        for t in ts:
            dims["series"][t.series].append(t)
            dims["entry_price_c"][_bucket(t.entry_price, [0, 20, 40, 60, 80, 90, 100])].append(t)
            dims["edge"][_bucket(t.edge * 100, [0, 3, 5, 8, 12, 20, 100])].append(t)
            dims["point_leverage"][_bucket((t.leverage or 0) * 100, [0, 2, 5, 10, 20, 100])].append(t)
            dims["exit_reason"][t.exit_reason or "open"].append(t)
        breakdown[name] = {d: {k: _summ(v) for k, v in vals.items()} for d, vals in dims.items()}

    # Model vs market calibration on settled matches: Brier score of each at every score event.
    cal: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0, "brier_model": 0.0, "brier_market": 0.0})
    for ev, model_a, bid, ask, sa, sb, ga, gb, lev in events:
        m = matches.get(ev)
        if m is None or bid is None or ask is None or m.result_a not in (0.0, 1.0):
            continue
        mid = (bid + ask) / 200.0
        y = m.result_a
        for key in ("all", m.series, "deciding_set" if sa == sb and sa >= 1 else "earlier_sets",
                    _bucket((lev or 0) * 100, [0, 5, 10, 20, 100]) + "c_leverage"):
            c = cal[key]
            c["n"] += 1
            c["brier_model"] += (model_a - y) ** 2
            c["brier_market"] += (mid - y) ** 2
    calibration = {k: {"n": int(v["n"]), "brier_model": v["brier_model"] / v["n"], "brier_market": v["brier_market"] / v["n"]}
                   for k, v in cal.items() if v["n"]}

    skip_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for strat, action in signals:
        skip_counts[strat][action] += 1

    return {
        "totals": _summ(trades),
        "per_strategy": {k: _summ(v) for k, v in by_strat.items()},
        "breakdown": breakdown,
        "model_vs_market_calibration_settled_matches": calibration,
        "signal_actions_recent": {k: dict(v) for k, v in skip_counts.items()},
        "settled_matches": len(matches),
        "current_params": params_to_dict(params),
        "previous_review_summaries": [n.body.get("summary") for n in notes],
        "notes": "Prices in cents. markout = our side's bid N seconds after entry minus entry price (adverse selection if negative). "
                 "Brier: lower is better; if the market's Brier beats the model's, model-vs-market edges are mostly model error.",
    }


def to_json(x: Any) -> str:
    return json.dumps(x, indent=2, default=str)
