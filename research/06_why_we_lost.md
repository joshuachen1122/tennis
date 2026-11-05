# Why the paper trades lost, and strategy v4 (2026-11-04 03:00 UTC)

Data: production snapshot at 02:54 UTC on Nov 4. 68 paper trades (67 closed), 80 settled matches, and
73 settled matches with quiet-moment quotes. Reproduce the replays with
`python scripts/fair_value_replay.py <copy of tennis.db>`.

## Where the −$337 came from

| Strategy | Closed | P&L | Won | Avg entry | Model said | Note |
|---|---|---|---|---|---|---|
| `model_edge` | 44 | −$255.69 | 34% of trades | 48¢ | 56% | 28 of 44 were underdogs |
| `late_close` | 7 | −$71.64 | 3 | 62¢ | 68% | too few to read |
| `closer` | 16 | −$9.37 | 14 | 88¢ | 93% | 2 losses at ~88¢ cancel 14 wins |

`model_edge` broken down:

- **Underdogs** (bought below 50¢): 28 trades, −$204. Of 25 resolved, 28% won at an average price of 35¢,
  while the model said 43%.
- **Favourites:** 16 trades, −$52 as traded, but **+$38 if held to settlement**. They won 75% at an average price
  of 69¢.
- **Exits (v1):** selling when the price "converged" to the model cost $175 compared with holding: −$7 as
  traded vs +$168 if held. Most of that gap is luck, because selling at a fair price has the same expected value
  as holding. The real cost is the second fee plus the spread on each round trip. v2 already stopped these
  exits.
- **Fees:** $65 across all strategies, about 5% of the money staked.

The model's "edge" was mostly the [winner's curse](https://en.wikipedia.org/wiki/Winner%27s_curse). We bought
wherever the model sat above the price. When the model is noise around the market, the moments it looks
highest are the moments its error is largest. Expected P&L at the model's prices was +$185, and the realized
win rate (46%) came in just under the price paid (48%).

## Is the model worth anything beyond the market?

Out-of-sample Brier score (lower is better). The model is replayed with the restart fix, on 41
pre-match-calibrated matches and 4.6k score events, with 5-fold cross-validation by match:

| Forecast | Brier |
|---|---|
| Market mid | 0.1218 |
| Markov model | 0.1240 |
| Model averaged 50/50 with mid | 0.1223 |
| Model with a fitted calibration slope | 0.1229 |
| Logistic regression on mid + (model − mid) | 0.1251 |
| **Market mid with a fitted calibration slope** | **0.1180** |

- Adding the model to the market makes the forecast *worse* out of sample.
- The model's coefficient on its own disagreement with the market is −1.29. Its 90% interval, [−3.0, +0.6],
  includes zero, so the model carries no usable information. If anything, it points the wrong way.

The only thing that improves on the market is stretching the market itself.

## The market is too timid in play (favourite–longshot bias)

Win rate by the side's mid at quiet moments, each match weighted equally:

| Mid | Matches | Won |
|---|---|---|
| 50–60¢ | 38 | 66% |
| 60–70¢ | 46 | 76% |
| 70–80¢ | 55 | 83% |
| 80–90¢ | 61 | 87% |
| 90–100¢ | 67 | 97% |

- The fit is P(win) = sigmoid(b · logit(mid)) with **b = 1.54** (match bootstrap 90% interval
  [1.10, 2.36]). The pre-match prices on their own are close to calibrated (b = 1.1, 57 matches), so the bias
  appears in play.
- The fit is unstable over time. The first half of the matches fits b = 0.98 and the second half
  b = 2.5.
- Survivorship check: 18 matches were still in play at snapshot time and are excluded. Only 2 older matches
  never settled (still suspended on Kalshi).

## What would have traded profitably (first quiet moment per match, real ask, fee, hold to settlement)

| Rule (2¢ minimum edge) | Matches | P&L per contract | 90% CI |
|---|---|---|---|
| Raw model, underdogs | 46 | −0.152 | [−0.241, −0.059] |
| 50/50 blend, underdogs | 28 | −0.155 | [−0.261, −0.038] |
| Raw model, favourites | 41 | −0.020 | [−0.133, +0.093] |
| 50/50 blend, favourites | 17 | +0.076 | [−0.096, +0.226] |
| Market, slope 1.4, favourites 55–90¢ | 63 | −0.009 | [−0.104, +0.081] |
| Market, cross-fitted slope (fit on 4/5 of matches, traded on the rest) | 63 | +0.005 | [−0.087, +0.088] |

- **Robust:** buying underdogs on the model's say-so loses about 15¢ per contract. That's the only interval that
  excludes zero.
- **Not established:** the favourite bias improves forecasts, but after the ~1.5¢ fee and the spread, trading it
  replays as break-even.

## v4 changes (PARAMS_VERSION 4, paper)

- New `favourite` strategy. It prices off the market mid alone:
  - Fair value is `sigmoid(1.4 · logit(mid))`, with 1.4 shrunk from the fitted 1.54 toward no effect.
  - It buys sides priced 55–90¢ with a net edge of at least 2¢.
  - It needs a match volume of at least 10k, enters once per match and holds to settlement.
  - Not LLM-gated. The reviewer still scores it in shadow.
  - It's a prospective test of the one effect that survived cross-validation, not a proven edge.
- `model_edge` and `late_close` are left as they are until the LLM-gate experiment ends at
  2026-11-05 00:15 UTC. Under the gate they haven't traded (8 reviews, 8 skips).
- **Planned once the gate experiment ends:** a minimum price of 50¢ for `model_edge` and `late_close`,
  i.e. no more model-driven underdog buys, unless the gate counterfactual shows otherwise.

## How v4 will be judged

- **`favourite`:** after 50 or more settled entries, the mean P&L per contract, with its match-bootstrap
  interval, compared with buying the same favourite at the same moment with no fair-value filter.
- **Calibration:** re-fit b on matches recorded after today. If the interval includes 1, retire the
  strategy.

## Result: `favourite` retired (2026-11-04 15:30 UTC)

Live paper results from 03:00 to 15:17 UTC on Nov 4:

- 195 entries, 148 settled across 143 matches, all held to settlement.
- 73% won at an average entry of 75¢.
- −$116.80 after $52 of fees, or **−3.5¢ per contract**. The match-bootstrap 90% interval is [−9.7¢, +3.1¢].

Win rate by entry price:

| Entry | Trades | Won | Avg entry |
|---|---|---|---|
| 55–65¢ | 43 | 60% | 63¢ |
| 65–75¢ | 41 | 63% | 69¢ |
| 75–85¢ | 29 | 83% | 80¢ |
| 85–90¢ | 35 | 91% | 89¢ |

- The slope re-fitted on these entries, using logit of the ask paid, is **b = 1.00** with a 90% interval of
  [0.74, 1.37]. That includes 1 and excludes the in-sample 1.54. By the rule set above, the strategy is retired.
- The in-sample bias did not hold up prospectively, which matches the unstable split-half fits (0.98 vs 2.5).
- Caveat: the VM was CPU-throttled for parts of the day, so some books (and therefore paper fills) lagged real
  time.
- How it was switched off:
  - Disabled on the dashboard at 15:30 UTC.
  - The code default is now `enabled=False`, so the next `PARAMS_VERSION` bump keeps it off.

## Final `favourite` tally and the v5 min_price change (2026-11-05 00:22 UTC)

- **`favourite`, final:** all 195 entries are settled, including 16 that missed `open_trades`
  during the CPU throttling and were settled by the orphan sweep.
  - 140 won (72%).
  - 4,284 contracts, **−$206.76**, or **−4.8¢ per contract**.
  - It stays retired.
- **v5 (`PARAMS_VERSION = 5`): `model_edge` and `late_close` get `min_price = 50`.** No more model-driven
  underdog buys.
  - The gate counterfactual (research/04) had 18 underdog candidates under the v3/v4 blend: 8 won at an
    average of 34¢, +8.8¢ per contract, 90% CI [−13.6¢, +29.8¢].
  - The 50/50-blend underdog replay was −15.5¢ [−26.1¢, −3.8¢] on 28 matches, and v1/v2 underdogs lost $204.
  - The new interval is wide and includes zero, so it doesn't overturn the robust negative result.
  - Pooled, underdogs are still around −6¢ per contract.
- The bump reset the dashboard's saved parameters to the defaults. `favourite` is off there too (its
  default since 15:30 UTC). The `maker` experiment (research/07) carries on.
