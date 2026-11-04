# Model replay and the restart bug (2026-11-04)

Data: production snapshot at 00:45 UTC. 73 settled matches, 34 of them calibrated from the pre-match price.
Scripts are in the session scratchpad; the method is described below so it can be redone.

## 1. Restarts were degrading the live model (fixed)

The engine kept each player's observed serve points only in memory, so every deploy reset in-progress
matches to their pre-match serve rates (seen in `score_events.pa` snapping back to `pa0` at the 21:17 and
21:35 deploys). Replaying the same 34 pre-match-calibrated matches from `score_events` with the counts kept:

| | Brier (3,755 events) |
|---|---|
| live model as recorded | 0.1413 |
| clean replay, same code and priors | **0.1337** |
| market mid | 0.1339 |

Fix: `Engine._restore_match_history` rebuilds the serve counts (and entries per strategy) from
`score_events` / `paper_trades` at boot. Test: `test_restart_keeps_serve_points_and_entries`.

So the model is about **level with the market**, not better. That fits there being no edge from the model alone.

## 2. Serve-total priors: observed lower, but it doesn't matter

Observed serve totals (pA+pB) from point-by-point attribution, compared with `SERVE_TOTALS`:
- ATP Challenger 1.168 vs 1.25 (1,748 pts)
- ITF men 1.173 vs 1.21 (2,023 pts)
- ITF women 1.059 vs 1.07
- WTA 125 1.072 vs 1.10

Replays with the observed or shrunk totals change the Brier by < 0.0002, because the pre-match price
pins the level and the total mostly affects variance. Left unchanged.

## 3. Serve-update weight (`prior_points`)

Brier by `prior_points`: 30 → 0.1377, 60 → 0.1339, 90 → 0.1334, 120 → 0.1337, 180 → 0.1345, 1000 → 0.1380.
The optimum is flat around 90–120, so it stays at 120.

## 4. Does a model–market disagreement predict anything?

At quiet moments (board unchanged ≥ 10s, mid range ≤ 2¢ over 5s, one sample per 30s; 5,329 samples,
65 matches), the logistic fit was y ~ logit(mid) + (logit(model) − logit(mid)). The disagreement
coefficient was −0.90, with a match-bootstrap 90% interval of [−2.34, +1.51]. That's no evidence the
model's disagreements carry information; if anything they point the wrong way. Note this used the
recorded (restart-damaged) model.

## 5. Favorite–longshot

The market looks under-confident: symmetric fit y ~ sigmoid(b·logit(mid)) gives b = 1.36, bootstrap
90% [1.04, 2.02]. By bucket, 60–70¢ won 71%, 70–80¢ 80%, 80–90¢ 91%, 90–100¢ 98%.

But a replay of "buy the favorite at the first quiet moment at 60–88¢ with ≥ 2¢ calibrated edge, hold"
lost about 0.1–0.2 per contract over 57 matches. The bucket effect comes from late-match time, and
finished-match samples over-represent short (straight-sets) matches. **Not traded.** Recheck at
≥ 200 settled matches.

## Next
- Re-run section 4 on the fixed model once there are a few days of data.
- The LLM remains the only forecaster with a better Brier than the market (research/04).
