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
