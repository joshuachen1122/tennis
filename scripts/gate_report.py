"""Did the LLM gate (strategy v3) help? Reads the live dashboard's public CSV exports.

    python scripts/gate_report.py [https://tennis-kalshi-agent.fly.dev]

1. Paper results by strategy-rules version (v1 / v2 / v3) and strategy.
2. LLM gate decisions: how many candidates it was asked about, took, skipped, or that went stale.
3. The counterfactual: what the skipped candidates would have made if bought at the quoted ask
   and held to settlement (what an ungated v3 would have traded), vs what the taken ones made.
4. Reviewer accuracy (Brier) on every resolved candidate: LLM vs blended model vs raw model vs price.
"""

from __future__ import annotations

import csv
import io
import json
import math
import sys
import urllib.request
from collections import defaultdict

V2_START = "2026-11-03 21:35"  # v2 rules deployed (v1 before)


def fetch(base: str, table: str, attempts: int = 3) -> list[dict]:
    # The export streams from a busy shared-cpu VM and sometimes stalls mid-body; retry instead of dying.
    for i in range(attempts):
        try:
            with urllib.request.urlopen(f"{base}/api/export/{table}", timeout=120) as r:
                return list(csv.DictReader(io.StringIO(r.read().decode())))
        except (TimeoutError, OSError) as e:
            if i == attempts - 1:
                raise
            print(f"  (fetch {table} failed: {e!r}; retrying)", file=sys.stderr)
    return []


def fee(count: float, cents: float) -> float:
    p = cents / 100
    return math.ceil(round(0.07 * count * p * (1 - p) * 100, 6)) if count > 0 else 0.0


def num(x):
    return float(x) if x not in ("", None) else None


def main(base: str) -> None:
    trades, notes, matches = fetch(base, "paper_trades"), fetch(base, "llm_notes"), fetch(base, "matches")
    result = {m["event_ticker"]: num(m["result_a"]) for m in matches}
    market_a = {m["event_ticker"]: m["market_a"] for m in matches}

    def won(event: str, ticker: str):
        r = result.get(event)
        if r not in (0.0, 1.0):
            return None
        return r if ticker == market_a.get(event) else 1 - r

    # ------------------------------------------------------------ 1. results by version
    print("== 1. Paper results by rules version")
    agg = defaultdict(lambda: defaultdict(float))
    for t in trades:
        ctx = json.loads(t["context"] or "{}")
        ver = f"v{ctx['params_version']}" if ctx.get("params_version") else ("v2" if t["opened_at"] >= V2_START else "v1")
        for k in ((ver, t["strategy"]), (ver, "ALL")):
            a = agg[k]
            a["n"] += 1
            if t["pnl"] == "":
                a["open"] += 1
                continue
            a["closed"] += 1
            a["pnl"] += float(t["pnl"]) / 100
            a["wins"] += float(t["pnl"]) > 0
            a["staked"] += float(t["count"]) * float(t["entry_price"]) / 100
    for (ver, strat), a in sorted(agg.items()):
        c = a["closed"] or 1
        print(f"  {ver} {strat:10} trades {a['n']:3.0f}  closed {a['closed']:3.0f}  open {a['open']:2.0f}  won {a['wins'] / c * 100:5.1f}%"
              f"  pnl ${a['pnl']:8.2f}  roi {a['pnl'] / (a['staked'] or 1) * 100:6.1f}%")

    # ------------------------------------------------------------ 2-3. gate decisions
    gate = [json.loads(n["body"]) | {"cost": float(n["cost_usd"] or 0)} for n in notes if n["kind"] == "gate"]
    print(f"\n== 2. LLM gate: {len(gate)} reviews, ${sum(g['cost'] for g in gate):.2f}")
    by = defaultdict(list)
    for g in gate:
        by[(g["strategy"], g.get("outcome", "?"))].append(g)
    for (s, o), v in sorted(by.items()):
        print(f"  {s:10} {o:15} {len(v):4}")

    print("\n== 3. Counterfactual: each candidate bought at its quoted ask and held to settlement")
    cf = defaultdict(lambda: [0, 0, 0.0, 0])  # resolved, wins, pnl $, unresolved
    for g in gate:
        y = won(g["event_ticker"], g["ticker"])
        k = (g["strategy"], "taken" if g.get("outcome") == "entered" else "skipped/stale")
        if y is None:
            cf[k][3] += 1
            continue
        cents = g["count"] * (100 * y - g["ask"]) - fee(g["count"], g["ask"])
        cf[k][0] += 1
        cf[k][1] += y
        cf[k][2] += cents / 100
    for (s, k), (n, w, p, u) in sorted(cf.items()):
        print(f"  {s:10} {k:14} resolved {n:3}  won {w / (n or 1) * 100:5.1f}%  pnl ${p:8.2f}  (unresolved {u})")

    # Fade: when the reviewer's probability is well below the price, would the *other* side have paid?
    # Other side's ask is approximated as 100 - ask + 1 (a 1c spread).
    fade = [0, 0, 0.0]
    for g in gate:
        v, y = g.get("verdict") or {}, won(g["event_ticker"], g["ticker"])
        if y is None or "probability" not in v or v["probability"] > g["ask"] / 100 - 0.05:
            continue
        other = 100 - g["ask"] + 1
        fade[0] += 1
        fade[1] += 1 - y
        fade[2] += (g["count"] * (100 * (1 - y) - other) - fee(g["count"], other)) / 100
    if fade[0]:
        print(f"  fade (LLM prob >=5c under the ask -> buy the other side): resolved {fade[0]}  won {fade[1]}  pnl ${fade[2]:.2f}")

    # ------------------------------------------------------------ 4. accuracy
    rows = []
    for g in gate:
        y = won(g["event_ticker"], g["ticker"])
        v = g.get("verdict") or {}
        if y is None or "probability" not in v:
            continue
        rows.append((y, v["probability"], g["model_p"], g.get("raw_model_p") or g["model_p"], g["ask"] / 100))
    if rows:
        def brier(i: int) -> float:
            return sum((r[i] - r[0]) ** 2 for r in rows) / len(rows)

        print(f"\n== 4. Brier on {len(rows)} resolved candidates (lower is better): LLM {brier(1):.4f}  blended model {brier(2):.4f}"
              f"  raw model {brier(3):.4f}  price {brier(4):.4f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "https://tennis-kalshi-agent.fly.dev")
