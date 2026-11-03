"""Research report over a copy of the agent's database.

    python scripts/analyze.py path/to/tennis.db

1. Score-feed lag: seconds between the market's price jump and the board showing the point.
2. Model vs market: Brier score of each at every score event of settled matches, by tier and by
   how the match was calibrated.
3. Market calibration: realised win rate by market price bucket (favourite-longshot check).
4. Paper trades: results by strategy, and what the v2 rules would have kept.
5. LLM shadow verdicts: PnL of trades it would have taken vs skipped.
"""

from __future__ import annotations

import bisect
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime


def ts(s: str) -> float:
    return datetime.fromisoformat(str(s)).timestamp()


def q(v: list[float], p: float) -> float:
    v = sorted(v)
    return v[min(len(v) - 1, int(p * (len(v) - 1)))]


def main(path: str) -> None:
    db = sqlite3.connect(path)
    series = dict(db.execute("select event_ticker, series from matches"))
    calib = dict(db.execute("select event_ticker, calib_source from matches"))
    result = {ev: r for ev, r in db.execute("select event_ticker, result_a from matches where result_a is not null")}

    # ---------------------------------------------------------------- 1. feed lag (seconds)
    ticks: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for ev, t, b, a in db.execute("select event_ticker, ts, bid_a, ask_a from book_ticks where live=1 and bid_a is not null and ask_a is not null order by id"):
        ticks[ev].append((ts(t), (b + a) / 2))
    lags: dict[str, list[float]] = defaultdict(list)
    for ev, t, pw, lev in db.execute("select event_ticker, ts, point_winner, leverage from score_events where point_winner is not null"):
        tl = ticks.get(ev)
        if not tl:
            continue
        t0 = ts(t)
        best = None
        for (ta, ma), (tb, mb) in zip(tl, tl[1:]):
            if t0 - 45 <= tb <= t0 + 2:
                d = abs(mb - ma)
                if best is None or d > best[0]:
                    best = (d, tb)
        if best and best[0] >= 2:
            lags["all"].append(t0 - best[1])
            lags[series.get(ev, "?")].append(t0 - best[1])
    print("== 1. Score feed lag: board update minus the biggest price jump before it (s), points moving >=2c")
    for k, v in sorted(lags.items(), key=lambda kv: -len(kv[1])):
        print(f"  {k:24} n={len(v):5}  p10 {q(v, .1):5.1f}  median {statistics.median(v):5.1f}  p75 {q(v, .75):5.1f}  p90 {q(v, .9):5.1f}")

    # ---------------------------------------------------------------- 2. model vs market
    acc: dict[str, list[float]] = defaultdict(lambda: [0, 0.0, 0.0])
    cal_rows = []
    for ev, model, bid, ask, sa, sb in db.execute(
        "select event_ticker, model_a, bid_a, ask_a, sets_a, sets_b from score_events where model_a is not null and bid_a is not null and ask_a is not null"
    ):
        y = result.get(ev)
        if y not in (0.0, 1.0):
            continue
        mid = (bid + ask) / 200
        for k in ("all", series.get(ev, "?"), f"calib={calib.get(ev) or '?'}"):
            a = acc[k]
            a[0] += 1
            a[1] += (model - y) ** 2
            a[2] += (mid - y) ** 2
        cal_rows.append((mid, y))
    print("\n== 2. Brier score on settled matches (lower is better), every score event")
    for k, (n, bm, bk) in sorted(acc.items(), key=lambda kv: -kv[1][0]):
        verdict = "model better" if bm < bk else "market better"
        print(f"  {k:24} n={n:5}  model {bm / n:.4f}  market {bk / n:.4f}  -> {verdict}")
    print(f"  settled matches with model data: {len({e for e, *_ in db.execute('select event_ticker from score_events where model_a is not null') if e in result})}")

    # ---------------------------------------------------------------- 3. market calibration
    print("\n== 3. Market calibration (all live score events of settled matches): price bucket -> realised win rate")
    buckets: dict[int, list[float]] = defaultdict(list)
    for mid, y in cal_rows:
        buckets[min(int(mid * 10), 9)].append(y)
    for b in sorted(buckets):
        v = buckets[b]
        print(f"  {b * 10:3d}-{b * 10 + 10:3d}c  n={len(v):5}  wins {sum(v) / len(v) * 100:5.1f}%  (mid of bucket {b * 10 + 5}%)")

    # ---------------------------------------------------------------- 4. paper trades and v2 rules
    print("\n== 4. Paper trades (closed)")
    rows = db.execute("select strategy, series, entry_price, count, pnl, exit_reason, context, llm_verdict, mark_10s, mark_60s from paper_trades where pnl is not null").fetchall()
    by = defaultdict(list)
    kept = defaultdict(list)
    for strat, ser, entry, cnt, pnl, reason, ctx, llm, m10, m60 in rows:
        c = json.loads(ctx or "{}")
        by[strat].append(pnl)
        v2 = c.get("calibration") == "prematch" and ser != "KXITFMATCH" and (c.get("match_volume") or 0) >= 10_000
        if strat == "closer":
            v2 = (c.get("match_volume") or 0) >= 10_000
        if v2:
            kept[strat].append(pnl)
    for strat in sorted(by):
        v, k = by[strat], kept.get(strat, [])
        print(f"  {strat:12} all: n={len(v):3} pnl ${sum(v) / 100:8.2f} | v2 rules would keep n={len(k):3} pnl ${sum(k) / 100:8.2f}")

    # ---------------------------------------------------------------- 5. LLM shadow
    print("\n== 5. LLM shadow verdicts on closed trades")
    take, skip = [], []
    for strat, ser, entry, cnt, pnl, reason, ctx, llm, *_ in rows:
        if not llm:
            continue
        verdict = json.loads(llm)
        (take if verdict.get("take") else skip).append(pnl)
    if take or skip:
        print(f"  would take: n={len(take):3} pnl ${sum(take) / 100:8.2f} | would skip: n={len(skip):3} pnl ${sum(skip) / 100:8.2f}")


if __name__ == "__main__":
    main(sys.argv[1])
