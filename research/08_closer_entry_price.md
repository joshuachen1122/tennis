# `closer`: no entries under 85¢ (v7, 2026-11-08 08:45 UTC)

`closer` buys a big favourite (model at least 90%, edge at least 2.5¢) and holds it to settlement. Data:
323 settled trades from Nov 3 to Nov 8. P&L per contract, with match-bootstrap 90% intervals:

| Entry | Trades | Won | P&L | Per contract | 90% CI | After the Nov 6 15:16 flag |
|---|---|---|---|---|---|---|
| under 85¢ | 31 | 68% | −$146.99 | −15.8¢ | [−29.0, −2.9] | 12 trades, −$61.51 |
| 85–90¢ | 182 | 88% | −$8.30 | −0.2¢ | [−4.4, +3.6] | 51 trades, −$39.80 |
| 90–95¢ | 88 | 98% | +$125.63 | +5.4¢ | [+2.9, +7.8] | 32 trades, +$67.98 |
| 95¢ and up | 22 | 95% | −$0.82 | −0.1¢ | [−9.2, +4.5] | 4 trades, +$4.60 |

- **Entries under 85¢:** these are spots where the model says at least 90% and the market is well below that.
  - They lose about 16¢ per contract, with an interval that excludes zero.
  - The loss continued on trades after the bucket was flagged on Nov 6, so it isn't only a fit to the data
    that found it.
  - The model's confidence is the error there. When the market prices a "90%" leader at 75–85¢, the market is
    right.
- **90–95¢:** the only bucket whose interval is above zero. It's consistent with the favourite–longshot bias
  in research/06, but only near the top of the range. This wasn't pre-registered, so it isn't acted on yet.
- **Change:** `min_price` goes from 60 to 85, and `PARAMS_VERSION` goes to 7, which resets the dashboard's
  saved parameters. `maker` and `favourite` stay off by default.
- **Judge it:** the 85¢+ entries from here on, against the 85–90¢ row above. If the 85–90¢ bucket keeps
  losing over the next 100 trades, raise the minimum to 90¢.

## Result after 101 settled v7 trades (2026-11-09, export taken at 18:25 UTC)

All settled `closer` trades opened since the v7 deploy (09:16 UTC Nov 8). Match-bootstrap 90% intervals:

| Entry | Trades | Won | P&L | Per contract | 90% CI |
|---|---|---|---|---|---|
| 85–90¢ | 66 | 91% | +$58.27 | +3.4¢ | [−3.0, +8.8] |
| 90–95¢ | 30 | 100% | +$55.64 | +7.6¢ | [+7.1, +8.0] |
| 95¢ and up | 5 | 80% | −$20.43 | −15.7¢ | [−55.9, +4.7] |
| all | 101 | 93% | +$93.48 | +3.6¢ | [−0.9, +7.6] |

- **85–90¢ did not keep losing**, so by the rule above the minimum stays at 85¢.
- 90–95¢ is positive again, now in both samples. Its interval is narrow only because all 30 trades won, so
  it understates the risk.
- Overall `closer` is still not proven: the 101-trade interval just includes zero.
