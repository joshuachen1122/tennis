# Initial strategy hypotheses & the score-feed lag finding

_2026-11-03. Written when the paper trader first went live._

## Measured on day one: Kalshi's free score feed lags the market by ~13s

On a local REST-only run (prices at ~2s resolution), I compared every point the feed attributed
against the market mid around it. Two measures:

- **Share of the price move already done when our board updated:** median **1.00** (14 points).
- **Board update time minus the biggest price jump before it:** p10 6.9s, **median 13.0s**, p75 22.8s,
  p90 26.0s (21 points).

Points arrive every ~25–40s, so the board is often a full point behind. Implications:

1. **Reacting to points on this feed can't work.** Market makers see each point ~13s earlier.
   Buying "right after a big point" means buying after the move.
2. **Disagreements between model and market are often just lag.** If the market has moved and
   our board hasn't, the model is pricing an old score.
3. **Gate added (`board_behind_market`):** after every board change, the mid at that moment is
   the reference. If the mid then moves by more than max(2¢, 0.4 × the model's one-point swing),
   trading on that match is blocked until the board changes again. Exits use the same rule.
   In-play calibration only happens on a board change, never on first sight.
4. **A faster feed could be the key edge.** Official Sportradar/TDI-level data is roughly 1–3s.
   Whether paying for a faster source is worth it is the first question for data analysis.
   `scripts/feed_lag.py` re-measures the lag from the recorded data at any time.

## Strategies running (paper, $1,000 notional bankroll each, taker fills at the live ask)

| Strategy | Hypothesis | Entry | Exit |
|---|---|---|---|
| `model_edge` | A Markov point model calibrated to the pre-match price, updated with observed serve points, finds in-play prices that drift from fair *between* points | model − ask − fee ≥ 5¢, ask 10–90¢ | sell when bid ≥ model − 1¢, else settle |
| `late_close` | **Your hypothesis:** late, tight deciding sets swing hardest per point, so mispricing is largest there | same model, deciding set, both players ≥ 4 games or in the tiebreak, edge ≥ 3¢, ask 15–85¢ | same |
| `closer` | Favorite–longshot bias: retail overpays for comebacks, so near-certain leaders are cheap. Fees are tiny at the extremes | side prob ≥ 90%, edge ≥ 2.5¢, ask ≤ 97¢ | hold to settlement |

Shared guard rails:
- Board fresh (≤ 5s since last poll) and not flagged delayed.
- Score unchanged ≥ 5s, and price range over the last 5s ≤ 2¢.
- Spread ≤ 5¢.
- No unexplained price move since the last board change.
- One open position per strategy per match, at most 3 entries per match, 30s re-entry cooldown.

Sizing is ¼-Kelly capped at $25 per trade and at the size showing at the ask.

## What gets recorded, for improving the edge

- `score_events`: every board change, with bid/ask, model probability, serve-point estimates,
  point leverage, and who won the point. This is the dataset for re-fitting the model and
  measuring lag.
- `book_ticks`: top of book for both players, ≤ 1/s per live match. Pre-match every 30s.
- `trade_bars`: public trades per market per second (WebSocket, needs Kalshi keys).
- `signals`: every candidate and why it was skipped. Use these to tune thresholds without new trades.
- `paper_trades`: entries, 10s/60s markouts (adverse selection), exits, PnL, and the LLM shadow verdict.
- `matches`: format, pre-match mid, calibrated serve priors, and the settlement value (1, 0, or fair price).

## First questions for data analysis

1. Feed lag by tier and tournament, with WebSocket-resolution prices.
2. Calibration (Brier score) of the model vs the market mid on settled matches, by state. If the
   market wins everywhere, the model needs better priors before any edge is real.
3. Markouts: are 10s/60s markouts negative? Negative means we're being picked off, i.e. the lag gate isn't enough.
4. Is the closer edge real? Measure realized win rate against the ask in the 85–97¢ bucket.
5. The serve-total prior (`SERVE_TOTALS`) by tier and surface. Fit it from settled matches.

## Change log

### 2026-11-03 (evening): v2 rules after the first 33 closed trades

| Evidence (first ~4h, 33 closed trades) | Change |
|---|---|
| In-play-calibrated matches lost $35.39 over 23 trades; pre-match-calibrated lost $0.48 over 10 | `model_edge` and `late_close` only trade matches calibrated from the pre-match price (`require_prematch_calibration`) |
| ITF men lost $50.84 over 12 trades; one W35 market had ~$20 of volume | `model_edge` and `late_close` exclude `KXITFMATCH`; all strategies need ≥ 10k contracts traded on the match (`min_match_volume`) |
| Selling at convergence netted +$0.20 over 23 exits while `model_edge` paid $44 in fees (taker fee on both legs) | Hold to settlement. Sell early only if the bid overshoots the model by ≥ 3¢ (`exit_buffer`, exit reason `overshoot`) |
| `closer` 4 of 4 wins, +$11.67 | Unchanged apart from the volume floor. Keep building the sample |

All of these come from tiny samples. They are risk reductions, not proof of an edge.
