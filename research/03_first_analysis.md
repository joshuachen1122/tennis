# First data analysis: day one (2026-11-03, ~2h of WebSocket-resolution data)

Reproduce with: `python scripts/analyze.py <copy of tennis.db>`. Sample: 337 matches tracked, 5.8k score events,
24k book ticks, 38k trade bars, 36 closed paper trades, 29 settled matches with model prices.

**Caveat:** score events inside one match are strongly correlated, so the effective sample for anything
match-level is closer to 29 than 1,105. Treat everything here as direction, not proof.

## 1. Score-feed lag: confirmed, every tier

| Tier | Points (≥2¢ moves) | p10 | median | p75 | p90 |
|---|---|---|---|---|---|
| All | 1,577 | 3.4s | **8.0s** | 27.8s | 38.4s |
| ATP | 53 | 7.8s | 10.7s | 30.8s | 37.6s |
| ATP Challenger | 391 | 6.5s | 9.4s | 30.2s | 40.1s |
| ITF men / women | 574 / 352 | ~2.8s | ~6.3s | ~27.5s | ~37s |

Across all 1,765 attributable points, the median share of the market's move already done when our board updated
was **1.00 (p25 1.00)**. The market had always finished repricing before Kalshi's free feed showed the point.
The fat tail (p75 ~28s) means the board is often a whole point behind.

→ Point-reactive trading on this feed is impossible. The `board_behind_market` gate is necessary and must stay.
Only a faster paid feed would change this.

## 2. Model vs market (Brier, lower is better)

- Overall the model is slightly better than the market mid: 0.1171 vs 0.1188 over 1,105 events. That's within noise.
- **Pre-match-calibrated matches:** model 0.1443 vs market 0.1759, over 109 events. Every one is an ATP match;
  this is effectively one or two matches.
- **ITF men:** market better, 0.1624 vs 0.1675. That fits the trading losses there, and the v2 exclusion.
- **WTA / WTA 125:** market better, but on tiny samples (35 and 12 events).

→ There's no evidence yet that the model systematically beats the market. The pre-match-calibrated result is
the most interesting thread, but it needs dozens of matches.

## 3. Market calibration (favorite–longshot check)

Live mid → realized win rate: 0–10¢ → 0%, 10–20¢ → 3%, 80–90¢ → 87%, 90–100¢ → 92%. The middle buckets swing
widely (20–30¢ → 49%) because each bucket is only a few matches. There's a hint that low prices are *overpriced*
(longshots win less than their price), which supports `closer`. Again, the sample is tiny.

## 4. Paper trades and the v2 rules

| Strategy | All closed | PnL | Kept by v2 rules | PnL of kept |
|---|---|---|---|---|
| `model_edge` | 26 | −$32.22 | 7 | **+$2.24** |
| `late_close` | 6 | −$46.29 | 0 | — |
| `closer` | 4 | +$11.67 | 4 | +$11.67 |

The v2 filters (pre-match calibration, no ITF men, ≥10k volume) would have removed essentially all of the
losses. That's in-sample, because the rules were picked from this same data, so it isn't proof.

## 5. LLM shadow reviewer

| Claude's verdict | Trades | PnL |
|---|---|---|
| would take | 3 | +$8.35 |
| would skip | 30 | −$74.72 |

That's the strongest signal so far: the reviewer rejected almost everything, and what it rejected lost.
Next experiment: an **LLM-gated variant** of `model_edge` (paper), run alongside the ungated one.

## Next steps

1. Let v2 run for several days to get an out-of-sample read. The archive keeps all raw data.
2. Add `model_edge_llm`, the same rules but gated on the LLM verdict, to measure the gate prospectively.
3. Fit the serve-total prior (`SERVE_TOTALS`) per tier from settled matches once there are ≥50.
4. Decide whether a faster score feed is worth paying for. The ~8s median lag is the ceiling on any in-play edge.
