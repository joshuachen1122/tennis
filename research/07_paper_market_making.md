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

(to be added)
