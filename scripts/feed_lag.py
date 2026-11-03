"""How stale is the score feed? For every attributable point, measure how much of the market's
move around that point had already happened when our board showed it.

    python scripts/feed_lag.py path/to/tennis.db

A share near 0 means we see points before the market reprices; near 1 means the market has
already moved by the time our feed updates (trading on point events would be adverse selection).
"""

import bisect
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime


def ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def main(path: str) -> None:
    db = sqlite3.connect(path)
    ticks: dict[str, list[tuple[float, float]]] = defaultdict(list)
    series = dict(db.execute("select event_ticker, series from matches"))
    for ev, t, b, a in db.execute("select event_ticker, ts, bid_a, ask_a from book_ticks where live=1 and bid_a is not null and ask_a is not null order by id"):
        ticks[ev].append((ts(t), (b + a) / 2))
    shares: dict[str, list[float]] = defaultdict(list)
    for ev, t, pw in db.execute("select event_ticker, ts, point_winner from score_events where point_winner is not null"):
        tl = ticks.get(ev)
        if not tl:
            continue
        times = [x[0] for x in tl]
        t0 = ts(t)

        def mid(at: float):
            i = bisect.bisect_right(times, at) - 1
            return tl[i][1] if i >= 0 else None

        before, at, after = mid(t0 - 25), mid(t0 - 0.5), mid(t0 + 8)
        if None in (before, at, after) or abs(after - before) < 1.5:
            continue
        s = (at - before) / (after - before)
        shares["all"].append(s)
        shares[series.get(ev, "?")].append(s)
    for k, v in sorted(shares.items(), key=lambda kv: -len(kv[1])):
        v.sort()
        print(f"{k:24} n={len(v):4}  share of move already priced when board updated: median {statistics.median(v):.2f}  "
              f"p25 {v[len(v) // 4]:.2f}  p75 {v[3 * len(v) // 4]:.2f}")


if __name__ == "__main__":
    main(sys.argv[1])
