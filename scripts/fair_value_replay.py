"""Which fair value would have traded profitably? Replays entries on a copy of the database.

    python scripts/fair_value_replay.py path/to/tennis.db

Samples quiet moments of settled matches (spread <= 5c, board unchanged >= 10s, mid within 2c over
5s, one per 30s), then for each rule buys the first side per match whose fair value beats the real
ask by `thr` after the taker fee, and holds to settlement. Reports P&L per contract with a
match-level bootstrap, and a 5-fold cross-fitted test of the favourite-longshot slope.
"""

from __future__ import annotations

import bisect
import math
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime


def lg(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def sg(z: float) -> float:
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def fee(c: float) -> float:
    return 0.07 * c * (1 - c)


def load(path: str) -> list[tuple]:
    db = sqlite3.connect(path)
    ts = lambda s: datetime.fromisoformat(s).timestamp()  # noqa: E731
    res = dict(db.execute("select event_ticker, result_a from matches where result_a in (0,1)"))
    sev = defaultdict(list)
    for e, t in db.execute("select event_ticker, ts from score_events order by id"):
        if e in res:
            sev[e].append(ts(t))
    ticks = defaultdict(list)
    for e, t, b, a, m in db.execute("select event_ticker, ts, bid_a, ask_a, model_a from book_ticks where live=1 and model_a is not null "
                                    "and bid_a is not null and ask_a is not null order by id"):
        if e in res:
            ticks[e].append((ts(t), b, a, m))
    data = []
    for e, tl in ticks.items():
        se, last, times = sev[e], -1e9, [x[0] for x in tl]
        for i, (t, b, a, m) in enumerate(tl):
            if a - b > 5 or b <= 0 or a >= 100:
                continue
            j = bisect.bisect_right(se, t)
            if j == 0 or t - se[j - 1] < 10:
                continue
            w = [(x[1] + x[2]) / 2 for x in tl[bisect.bisect_left(times, t - 5): i + 1]]
            if max(w) - min(w) > 2 or t - last < 30:
                continue
            last = t
            data.append((e, res[e], b, a, m))
    return data


def sides(d):
    e, y, b, a, m = d
    yield a / 100, m, (b + a) / 200, y
    yield (100 - b) / 100, 1 - m, 1 - (b + a) / 200, 1 - y


def per_match(data, fair, thr, lo, hi, only=None):
    out = {}
    for d in data:
        if d[0] in out or (only is not None and d[0] not in only):
            continue
        for ask, m, mid, y in sides(d):
            if lo <= ask <= hi and fair(mid, m) - ask - fee(ask) >= thr:
                out[d[0]] = y - ask - fee(ask)
                break
    return out


def boot(pm: dict, label: str) -> None:
    v = list(pm.values())
    if not v:
        print(f"  {label:44} no trades")
        return
    random.seed(1)
    bs = sorted(sum(s) / len(s) for s in (random.choices(v, k=len(v)) for _ in range(2000)))
    print(f"  {label:44} n={len(v):3}  {sum(v) / len(v):+.3f}/contract  90% CI [{bs[100]:+.3f}, {bs[1900]:+.3f}]")


def fit_slope(data, evs) -> float:
    rows = [(mid, y) for d in data if d[0] in evs for ask, m, mid, y in list(sides(d))[:1]]
    return min((sum((sg(b * lg(mi)) - y) ** 2 for mi, y in rows), b) for b in [x / 50 for x in range(40, 126)])[1]


def main(path: str) -> None:
    data = load(path)
    print(f"quiet samples {len(data)}, settled matches {len({d[0] for d in data})}")
    rules = {
        "raw model": lambda mid, m: m,
        "model blended 50/50 with mid": lambda mid, m: mid + 0.5 * (m - mid),
        "market, slope 1.4": lambda mid, m: sg(1.4 * lg(mid)),
    }
    for name, f in rules.items():
        for lo, hi, band in ((0.10, 0.45, "underdogs"), (0.55, 0.90, "favourites")):
            boot(per_match(data, f, 0.02, lo, hi), f"{name}, {band}")
    evs = sorted({d[0] for d in data})
    random.seed(7)
    random.shuffle(evs)
    pm, slopes = {}, []
    for i in range(5):
        test = set(evs[i::5])
        b = fit_slope(data, set(evs) - test)
        slopes.append(b)
        b = 1 + 0.75 * (b - 1)
        pm.update(per_match(data, lambda mid, m: sg(b * lg(mid)), 0.02, 0.55, 0.90, only=test))
    boot(pm, f"cross-fitted slope (fold fits {slopes})")


if __name__ == "__main__":
    main(sys.argv[1])
