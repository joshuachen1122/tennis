# Paper market making (`maker`, from 2026-11-04 ~19:00 UTC)

All four taker strategies lost money or broke even (research/06). Paying the spread plus a ~1.5¢ taker fee
needs a forecasting edge we don't have. This experiment does the opposite: it rests limit bids and
*collects* the spread.

## How it works

- Every match has two markets, YES on player A and YES on player B. The maker rests a bid on each one
  while the match is quiet:
  - It joins the best bid, or improves it by 1¢ when the spread is 3¢ or wider.
  - It only quotes when the two bids leave at least 2¢ below 100.
- If both bids fill, we own both players, and exactly one of them pays 100¢. The pair locks in
  `100 − pa − pb`, minus maker fees.
- Once one leg fills, the other leg's bid is capped at `100 − avg − 1`. It waits for the price to come back
  instead of chasing it. A leg that never pairs is held to settlement.
- Quotes are pulled whenever the score or the price isn't quiet (the same gate the takers use). Kalshi's
  score feed is ~13s behind the market, so this mostly reacts *after* a move.
- Settings:
  - 10k minimum match volume.
  - Bids between 10¢ and 90¢.
  - $10 per leg.
  - Up to 10 completed pairs per match.

## Fill model (conservative)

- Only a taker *selling* YES fills a YES bid.
- At our price, a print first eats the size that was already showing when we posted. A print below our
  price fills us up to its size. An ask at or below our price fills the rest.
- Others cancelling ahead of us never moves us up the queue.
- Our order doesn't change anyone else's behaviour, which flatters us slightly.
- Fees: Kalshi's maker fee, `ceil(0.0175 · C · P · (1−P))` per fill. That's assumed to apply to tennis; it
  may not.

## The question

Does the spread we collect on completed pairs beat what unpaired legs lose to adverse selection, net of
fees?

What to read from the trades:
- Completed pairs vs unpaired legs.
- P&L per contract.
- `mark_10s` / `mark_60s`: the bid 10s and 60s after a fill. Consistently negative marks mean we're
  getting picked off.

## Results

### First 6 hours (21:04 UTC Nov 4 to 03:10 UTC Nov 5)

429 fills, 5,495 contracts, $21.28 of maker fees. Settled P&L is **−$67.92**, or −1.2¢ per contract.
Cycles are grouped by event and cycle. A new group starts when the cycle number changes or the quoting
state was reset.

| Settled cycles | Cycles | Contracts | P&L | Per contract |
|---|---|---|---|---|
| Completed pairs | 133 | 3,830 | +$15.62 | +0.41¢ |
| Unpaired, last cycle of the match | 15 | 205 | −$70.84 | −34.6¢ |
| Unpaired, abandoned by a state reset | 4 | 52 | −$12.71 | −24.4¢ |

- **Pairs:** they lock 0.82¢ per contract gross. Maker fees take about 0.4¢ of that.
- **The break-even problem:** a pair earns about $0.12 and an unpaired cycle loses about $4.70. To break
  even, fewer than about 1 cycle in 40 can end unpaired. Here it was 1 in 8.
- **Markouts** (bid minus fill price): −1.7¢ at 10s and −1.9¢ at 60s across all fills. The bid drops
  about 2¢ after we're filled, so the fills are adversely selected.
- **Leverage:** markouts are flat across point leverage.
  - 10s markout ranges from −1.2¢ to −2.0¢ per bucket. Below 0.02 it is −1.4¢; at 0.05 and above it
    is −2.0¢.
  - Every leverage bucket loses on cycle P&L. The buckets at 0.05 and above lose the most (−$49 of the
    −$68), but on only 11 unpaired cycles.
  - The data doesn't clearly support filtering out high-leverage points, so the strategy is unchanged.
- **Fix (no params change):**
  - The quoting state was held only in memory. A deploy, or a match briefly dropping out of the live
    set, forgot a half-filled pair and left its leg unhedged. That happened 4 times.
  - `Engine._maker_resume` now rebuilds the latest cycle (target and legs) from the open maker trades.


### Verdict: retired (2026-11-05 18:10 UTC, PARAMS_VERSION 6)

Results from 21:04 UTC Nov 4 to 18:05 UTC Nov 5: 1,883 fills, 22,661 contracts, $90.20 of maker fees.
Settled P&L is **−$338.17**.

| Settled cycles | Cycles | Contracts | P&L | Per contract |
|---|---|---|---|---|
| Completed pairs | 757 | 20,289 | +$80.59 | +0.40¢ |
| Unpaired, last cycle of the match | 87 | 1,158 | −$403.61 | −34.9¢ |
| Unpaired, abandoned by a state reset (all before the fix) | 5 | 64 | −$15.15 | −23.7¢ |

- **Per contract, by match:** −1.57¢ on 176 matches. The match-bootstrap 90% interval is
  **[−2.06¢, −1.15¢]**, so the loss is clearly not chance.
- **Unpaired rate:** about 11% of cycles end one-legged. Break-even needs under about 2.5%.
- **Markouts** (bid minus fill price): −2.2¢ at 10s and −2.5¢ at 60s, getting worse as the sample
  grew. The bid falls about 2¢ after a fill, which is more than the spread a pair locks.
- **Leverage:** every leverage bucket loses, from −0.8¢ per contract at the lowest to −2.6¢ at the
  highest (0.08 and above). There is no subset of game states where quoting pays.
- **Why:** the fills that happen are the ones where an informed seller (someone watching the court,
  ~13s ahead of Kalshi's score feed) hits the bid. Passive quoting without a faster score feed or the
  ability to requote within a second is picked off.
- **Change:** `maker` is now `enabled=False` by default, and `PARAMS_VERSION` is bumped to 6 so the
  dashboard's saved parameters reset. `favourite` stays off. The code stays in place for a re-test with
  a faster feed.
