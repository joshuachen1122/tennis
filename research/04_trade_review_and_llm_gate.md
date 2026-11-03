# Trade review after day one, and the LLM-gate experiment (2026-11-03 → 11-04)

Data: 65 paper trades (55 closed) to 2026-11-03 23:48 UTC, results checked against Kalshi.

## Findings

| Strategy | Closed | Won | P&L |
|---|---|---|---|
| `model_edge` | 37 | 38% | −$177.30 |
| `late_close` | 7 | 43% | −$71.64 |
| `closer` | 11 | 82% | −$22.57 |

- **The model overrates the side it buys.** On 52 trades on resolved markets: the model said 64.6%, the price
  57.9%, actual 53.8% (28 wins vs 33.6 model-expected, z = −1.84). Worst at 60–85¢ entries: won 57%, price
  74%, model 82% (n = 14). Brier: price 0.189, model 0.197.
- **v2 rules failed out of sample:** −$144.62 on 13 closed. Their filters shifted entries to ~78¢ favorites,
  where the model bias is largest and a loss costs ~5× a win.
- **Stacked exposure:** 10 matches were traded by more than one strategy, −$174 combined
  (Rehberg–Svajda −$76 across three strategies).
- **Fees** were $58, 28% of the `model_edge` loss (mostly v1's sell-at-convergence round trips).
- **The LLM reviewer:** shadow-skipped 44 trades lost $210, took 4 made $11.50. Brier on 45 reviewed trades:
  LLM 0.183, price 0.189, model 0.197. The $1/day cap stopped reviews at ~22:44 UTC.

## Changes (strategy v3, `PARAMS_VERSION = 3`)

1. `model_edge` and `late_close` enter only when the LLM reviewer says take (`llm_gate`). It's asked
   *before* entry; a take must still qualify within 1¢ of the reviewed ask when the answer arrives.
   It fails closed with no LLM or no budget. After a skip, there's a 15-min cooldown per match, with
   at most 3 reviews per match per strategy.
2. One open position per match across all strategies (`match_held`), and a pending review reserves the match.
3. Model blended with the market: `p = mid + 0.5·(model − mid)` for `model_edge` / `late_close`, with
   min edge lowered to 3¢ / 2¢ blended (≈6¢ / 4¢ raw). This applies to exits too. `closer` keeps the raw
   model, since the model matched the market above 85¢.
4. LLM budget: $5/day until 2026-11-05 00:15 UTC (`LLM_BOOST_*`), then back to $1/day. At ~$0.012 per
   review, that's ~400 reviews/day.

## How it will be judged (fixed in advance)

Run `python scripts/gate_report.py` after 24h:
- v3 P&L by strategy vs v1/v2.
- The gate's counterfactual: skipped candidates bought at their quoted ask and held to settlement,
  vs taken ones. The gate helps if the skipped set loses money and the taken set beats it.
- Brier of LLM vs blended model vs raw model vs price on all resolved candidates.

Caveat: a day is maybe 20–60 resolved candidates. A clear sign either way is informative; a small
difference is not.
