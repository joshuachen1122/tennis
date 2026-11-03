# Tennis on Kalshi — Research & Lay of the Land

_Compiled 2026-11-03. Live numbers pulled from the public Kalshi API the same day._

---

## 1. Executive summary

1. **It is an in-play market.** Across a sample of ~50 settled match markets (ATP/WTA/Challenger/ITF), **~99% of volume trades in the last 4 hours before close**, i.e. during the match. Pre-match volume is a rounding error. An agent must be live-state aware.
2. **Liquidity is real and deep at the top.** Match-winner markets (last ~10 weeks): ATP median **1.8M contracts/match**, WTA **1.2M**, ATP Challenger **0.54M**, WTA 125 **0.53M**, ITF **82k**. US Open matches averaged ~7M (ATP). Top-tier books are 1¢ wide with tens of thousands of contracts per level.
3. **Props are thin.** Set winner, total games, game spread, exact score: medians 17k–71k contracts/event, spreads 5–29¢. Softer, but low capacity.
4. **Settlement rules differ from sportsbooks.** If a player **retires after the first ball, they lose** and the opponent's contract pays. A **walkover before the first ball** settles at a **"fair price"**, which in practice is about the last traded price (Tsitsipas–Tien settled 0.45/0.55). That is not a void or refund.
5. **Fees favor makers.** Taker fee is `ceil(0.07·C·P·(1−P))`, about 1.75¢/contract at 50¢. Maker fee on ATP/WTA match markets is 0.0175 (a quarter of the taker fee). **Challenger match markets charge no maker fee.** A Liquidity Incentive Program also pays for resting depth.
6. **Kalshi exposes a free live-score feed.** `/milestones` + `/live_data/milestone/{id}` gives server, points, games and sets, keyed to Sportradar event IDs. Its latency relative to market makers is unknown and is the first thing to measure.
7. **Latency is the main risk.** Official data (Sportradar holds ATP/Challenger via TDI, plus most Slams) reaches pros first. Racing point events on a slower feed means being adversely selected. More viable approaches: modelled fair value between points, resting maker quotes with fast cancellation, and cross-market consistency (match vs. set vs. exact score vs. totals).
8. **"Fifteen":** we could not find a public product by that name that trades Kalshi or Polymarket tennis. → **Need a link/description from you.**

---

## 2. Kalshi tennis market map

~150 tennis series exist. The ones that matter:

| Series | What | Open now (evts) | Settled sample (since 2026-08-27) | Median vol/match | p90 | Median spread |
|---|---|---|---|---|---|---|
| `KXATPMATCH` | ATP match winner (incl. Slams, 1000/500/250) | 16 | 704 | 1.81M | 7.0M | 1¢ |
| `KXWTAMATCH` | WTA match winner | 12 | 866 | 1.22M | 4.7M | 2¢ |
| `KXATPCHALLENGERMATCH` | ATP Challenger match | 35 | 2,641 | 0.54M | 2.5M | 1¢ |
| `KXWTACHALLENGERMATCH` | WTA 125 match | 6 | 837 | 0.53M | 2.5M | 1¢ |
| `KXITFMATCH` / `KXITFWMATCH` | ITF men / women | 130 / 113 | 5,453 / – | 82k (men) | 635k | 6–7¢ |
| `KXATPSETWINNER` / `KXWTASETWINNER` | Set N winner | 30 / 23 | 1,602 | 71k | 308k | 5–23¢ |
| `KXATPGTOTAL` / `KXWTAGTOTAL` | Total games (multi-strike) | 14 / 12 | 647 | 22k | 89k | 4–5¢ |
| `KXATPGSPREAD` | Game handicap | 14 | 647 | 17k | 65k | 7¢ |
| `KXATPEXACTMATCH` | Exact set score | 16 | 702 | 18k | 83k | 29¢ |
| `KXATPDOUBLES`, `KXWTADOUBLES`, `KXITFDOUBLES`… | Doubles | ~90 | – | small | | 4–27¢ |
| Futures: `KXATP`, `KXWTA`, `KXATPFINALSQUAL`, `KXATP1RANK`, Slam winners | Outrights | | | small–mid | | |

_Volume is in contracts ($1 notional each). Totals traded since late August: ATP match ~2.2B, Challenger ~2.6B, WTA ~1.7B, ITF ~1.3B contracts._

**Per-tournament volume per match (ATP):** US Open 7.1M; Vienna 3.2M; Basel 2.9M; Beijing 2.5M; Tokyo 2.4M; Lyon 2.4M; Paris (so far) 2.2M; Shanghai 1.9M; 250s ~1.2–1.6M.

**Challengers in US time zones nearly match ATP 250s.** San Diego and Tiburon averaged ~2.2M/match. This looks like a US-retail effect: volume follows US waking hours, not tournament prestige.

**Market structure facts:**
- Each match is an **event** with two mutually exclusive markets, one per player (e.g. `KXATPMATCH-26NOV03RUUJOD-JOD`). Prop events share the same suffix (`KXATPSETWINNER-26NOV03RUUJOD-1`, `KXATPGTOTAL-…`).
- Markets open about 1–2 days before the match. `close_time` sits ~2 weeks out to cover postponements, and the market closes early once a winner is declared. Settlement follows after a 60s timer, typically 1–2 minutes after the match.
- Tick: $0.01 (`linear_cent`). Fractional contracts down to 0.01 are supported. Prices and sizes are fixed-point strings (`*_dollars`, `*_fp`).
- The order book returns **bids only** on each side: Yes ask = 1 − best No bid.
- Example depth 3h before Ruud–Jodar (Paris R32): Yes 62¢ bid 34k / 63¢ ask 54k, with 165k resting at 65¢-equivalent. Next-day Djokovic–Hijikata: 81/82 but only 185 contracts at the touch.
- Walkover/fair-price settlements ("scalar" result) make up **~3–6% of settled markets.**

---

## 3. Kalshi platform mechanics (for the agent)

**Fees**
- Taker: `roundup(0.07 × C × P × (1−P))`. That is 1.75¢ at 50¢, 1.26¢ at 30/70, 0.63¢ at 10/90.
- Maker: `0.0175 × C × P × (1−P)`, charged on fill only. Cancels are free.
- Per series (from `/series/{ticker}`): `KXATPMATCH`/`KXWTAMATCH` = `quadratic_with_maker_fees` (makers have paid since 2025-11-15). **`KXATPCHALLENGERMATCH` = `quadratic` (taker only).**
- Fees can be overridden per event. Watch `event_fee_update` on `market_lifecycle_v2`.
- **Liquidity Incentive Program:** pays resting orders whether or not they fill, scored every second by size and distance from a reference price, $1–$1,000 per market per day. The listed end date is 2027-01-01; verify it.

**API**
- REST: `https://external-api.kalshi.com/trade-api/v2` (`api.elections.kalshi.com` still works). Demo: `https://external-api.demo.kalshi.co/trade-api/v2`.
- WS: `wss://external-api-ws.kalshi.com/trade-api/ws/v2`. Channels: `orderbook_delta`, `ticker`, `trade`, `fill`, `market_positions`, `market_lifecycle_v2`, `user_orders`. A low-latency `wss://warp-ws.kalshi.com` serves fills and user orders.
- Auth: RSA-PSS SHA-256 signature over `timestamp_ms + METHOD + path`, sent in headers `KALSHI-ACCESS-KEY`, `-TIMESTAMP`, `-SIGNATURE`.
  - **Since Aug 2026, sports trading requires a current location attestation on the API key.**
- Orders: **V2 only**, `POST /portfolio/events/orders`.
  - `side` is bid/ask. TIF options: FOK / GTC / IOC, plus `expiration_time`.
  - Flags: `post_only`, `reduce_only` (IOC only), self-trade prevention, `cancel_order_on_pause`.
  - **Order groups** auto-cancel when a contract limit is hit in a rolling 15s window. This is a built-in kill switch.
  - Legacy `buy_max_cost` and market orders are gone.
- **Sharding:** tennis markets sit on `exchange_index=3`. **That shard must be funded via intra-account transfer before API orders work.**
- Rate limits are token buckets (~10 tokens per call). Basic tier: 200 read / 100 write tokens per second, so roughly 20 reads and 10 writes per second. Tiers go up to Prestige and are earned by volume share.
  - The unauthenticated public API rate-limited us (429) quickly during bulk pulls.
- FIX 5.0SP2 is available (order entry + market data).
- Weekly maintenance: **Thursday 3–5 AM ET**, no new orders. A market reactivating from `inactive` **cancels all resting orders**.
- Position accountability level: $25k per strike per member (a reporting threshold, not a hard cap).
- Live data: `GET /milestones?related_event_ticker=…` → `GET /live_data/milestone/{id}`. It returns status, server, advantage, per-set games, current-game points and winner. It includes `source_id: sr:sport_event:…` (Sportradar). The milestone's own `status` field is documented as lagging.
- Historical: `/historical/*` endpoints cover data before the cutoff (currently 2026-09-04 for trades and settled markets). Regular endpoints give ~2 months. `/series/{s}/markets/{t}/candlesticks?period_interval=1` gives 1-minute OHLC + volume.

**Settlement rules (ATP/WTA match, from the API's `rules_secondary`, `important_info` and ACHIEVEMENTS contract terms)**

| Situation | Settlement |
|---|---|
| Player wins after ≥1 ball played | Yes |
| **Retirement / forfeit / default after first ball** | Retiring player = **No** (opponent Yes). Undetermined props → fair price |
| Walkover / injury / cancellation **before first ball** | **Fair price** for each player (≈ last traded, or Outcome Review Committee) |
| Postponed / delayed | Market stays open; settles after rescheduled match (within ~2 weeks) |
| Sources | atptour.com / wtatennis.com (plus AP, ESPN, etc.) |

**Implications:**
- Betfair settles retirements similarly (the advancing player wins). Many US books void them. Kalshi prices should therefore embed retirement risk, so an injury mid-match is a directional event, not a void.
- Pre-match walkover exposure is limited to how stale the last traded price is.

**Regulatory**
- Courts are split:
  - Adverse rulings in the 9th Circuit (Aug 2026) and 6th Circuit (Sep 2026; rehearing requested).
  - The 3rd Circuit let NJ operations continue; an Illinois federal court ruled in Kalshi's favor (Oct).
- The CFTC proposed (Oct 9) treating sports event contracts as swaps.
- Sports contracts are reportedly blocked in NV, MI, WA, MA, and possibly MO. The platform should treat this as operational risk.

**Industry context:**
- Kalshi is the US Open's official prediction-market partner (Aug 2026) and has an ITIA integrity data-sharing deal. Polymarket partners with the ATP.
- Kalshi claims tennis volume is up 25× YoY.
- A Catalist Sports deal gives Kalshi official low-latency data for 65k+ lower-tier matches. That is likely why ITF markets exist.

---

## 4. Tennis rules that matter for pricing

**Core scoring:**
- Game: 0-15-30-40, deuce/advantage (or **no-ad**: sudden-death point at deuce).
- Set: first to 6 games, win by 2. **Tiebreak at 6–6**: first to 7 points, win by 2. The serve rotates after the 1st point, then every 2 points.
- Match: best of 3 (BO3) or best of 5 (BO5).
- Match tiebreak (MTB): first to 10 points, win by 2, played instead of a deciding set.

| Competition | Format | Deciding set |
|---|---|---|
| Grand Slams – men's singles main draw | **BO5** | 6–6 → **10-point tiebreak** (unified at all 4 Slams since 2022) |
| Grand Slams – women's singles; all Slam qualifying | BO3 | 6–6 → 10-point tiebreak |
| Grand Slams – doubles | BO3, advantage scoring | 6–6 → 10-pt TB (some Slams play deciding-set MTB in early rounds; check each event) |
| ATP/WTA tour (1000 / 500 / 250), Challenger, WTA 125, ITF | BO3, advantage | 7-point tiebreak at 6–6 in all sets |
| Tour doubles (ATP/WTA/Challenger/ITF) | BO3, **no-ad** | **10-point match tiebreak** instead of 3rd set |
| ATP Finals (Turin) / WTA Finals | BO3; **round robin** (2 groups of 4) → SF → F | 7-pt TB. Round-robin creates qualification math and dead-rubber/tanking scenarios |
| Davis Cup / BJK Cup | Ties = 2 singles + 1 doubles, BO3 | 7-pt TB; doubles can be dead rubbers |
| Laver Cup | BO3 | 10-pt MTB instead of 3rd set |
| Next Gen ATP Finals | Experimental formats, which have changed by year | **Verify each year** |
| Exhibitions (Six Kings Slam, etc.) | Varies | Check each event |

**Other rules and dynamics that move prices:**
- **Serve dominance.** Hold rates run ~80% (ATP) vs ~63–65% (WTA), and are higher on grass/fast indoor and lower on clay.
  - WTA and clay → more breaks → bigger, more frequent in-play swings and more mean-reversion.
  - Grass and fast indoor courts → more tiebreaks, with single points carrying huge leverage.
- **Surface calendar:**
  - Hard: Jan Australian swing; Feb–Mar Middle East / Indian Wells / Miami; Jul–Sep North American swing → US Open; Sep–Nov Asian swing + European indoor.
  - Clay: Feb South American "Golden Swing"; Apr–Jun Europe → Roland-Garros.
  - Grass: Jun–Jul → Wimbledon.
- **Medical timeouts** (3 min), toilet/change-of-attire breaks, a **25-second shot clock**, rain delays (outdoor courts without roofs), and roof closures can all change conditions mid-match.
  - These pauses are when stale resting orders get picked off.
- **Lucky losers / alternates** replace withdrawals. Kalshi lists markets by player, so late withdrawals before the first ball → fair-price settlement.
- **Electronic line calling** (Hawk-Eye Live) is used at all ATP events since 2025; video review is at 1000s and all courts at 500s in 2026.
- **2026 ATP ranking change:** 18 counting results (4 Slams + 8 mandatory 1000s + best 7). Nine Masters 1000s in 2026, seven with 12-day 96-player draws.
  - Late-season motivation: ATP Finals race, ranking points defence, players skipping events after injuries (Alcaraz and Sinner injuries prompted October 2026 proposals).

**Near-term calendar:**
- ATP Paris Masters is in progress now.
- **ATP Finals, Turin, Nov 15–22.**
- **WTA Finals:** reports say it relocated to Indian Wells for 2026; dates are unconfirmed and some listings still say Riyadh, Nov 7–14.
- **Davis Cup Final 8, Bologna, Nov 24–29.**
- Challenger/ITF continue into December, then a short off-season.
- United Cup and Australian swing in Jan 2027 → Australian Open.

---

## 5. Betting landscape & where edges might live

**Venues:**
- **Betfair Exchange:** the global in-play tennis benchmark, not available in the US. Slams often match £1M+; Challenger and ITF are thin there.
- **Pinnacle:** the sharp reference price; public API access was reportedly closed in 2025.
- **Polymarket** (ATP partner), **ProphetX**, **Novig**, Crypto.com: US-accessible venues that open the door to cross-venue comparison and arbitrage.

**Efficiency findings (literature):**
- In-play exchange prices track a Klaassen–Magnus point model closely. Markets price "important points" early.
- Easton & Uylangco (2010) found one documented inefficiency: players who concede a break underperform afterward, and odds don't fully reflect it immediately.
- Favorite–longshot bias in tennis is mixed or weak, and some earlier "edges" were data-snooping.
- The common view is that lower tiers (Challenger, ITF) are softer, with the caveat that capacity is lower. On Kalshi, Challenger capacity is *not* low.

**Latency / courtsiding:**
- TV is delayed by seconds, and umpire-entered official data trails the court.
- Courtsiding is banned by the tours. The ITIA monitors it and now shares data with Kalshi.
- **Design implication:** don't try to win races on point outcomes with a slower feed. Instead:
  - Price between points.
  - Quote passively, and pull quotes on serve/point-start signals or in any period where stale quotes can be picked off.
  - Exploit structural and model edges.

**Candidate edges for our agent** (to test in data analysis):
1. **Cross-market consistency.** One Markov model (p_serve_A, p_serve_B, score state) implies coherent prices for the match, each set, game totals, the game spread and the exact score. The props are thin and wide (5–29¢), and probably priced loosely against the deep match market.
2. **Market making on Challenger/ITF.** No maker fee on Challenger, liquidity rewards, and a deep US-retail flow.
3. **Calibration / favorite–longshot bias on Kalshi specifically.** Retail-heavy flow may differ from Betfair. Measure it.
4. **In-play overreaction / momentum mispricing** after breaks, set losses and medical timeouts.
5. **Retirement and fatigue modelling.** Kalshi's "retiring player loses" rule plus late-season fatigue.
6. **Walkover / fair-price edge cases.** Know when last-traded is stale.
7. **Round-robin incentives** at the ATP/WTA Finals: dead rubbers and qualification scenarios.

---

## 6. Data sources

| Source | What | Notes |
|---|---|---|
| Kalshi API (public) | Markets, order books, 1-min candlesticks, trades, live score (`live_data`), historical endpoints | Free. Our primary price history. Unauth'd calls hit 429 quickly |
| JeffSackmann `tennis_atp`, `tennis_wta`, `tennis_slam_pointbypoint` | Match results/stats; Slam point-by-point | **GitHub returns 404 as of today.** Mirrors: HF `Aneeshers/tennis-sackmann-archive`, `Tennismylife/TML-Database` (ATP 1968–2026). License CC BY-NC-SA (non-commercial) |
| `tennis_MatchChartingProject` | Shot-by-shot charted matches | Still public. CC BY-NC-SA |
| tennis-data.co.uk | Results + bookmaker odds (incl. Pinnacle) ATP 2000+, WTA 2007+ | Returned 403 to our script; may need a browser |
| Live score APIs | Goalserve (~$150/mo, 5s updates), Live Tennis API ($10–100/mo; WS on top tier), tennisapi.dev ($15 + $15 WS), api-tennis, SportDevs | Latency unverified; must benchmark against Kalshi trade bursts |
| Official feeds | Sportradar (ATP/Challenger via TDI 2024–29; acquired IMG Arena portfolio incl. Wimbledon; holds 3 of 4 Slams), Catalist (Kalshi partner, lower tiers) | Enterprise pricing |

**Modelling references:**
- Elo / surface-blended Elo (Tennis Abstract style). In Kovalchik (2016), it was the best of ~11 published models but still slightly short of bookmakers.
- Bradley–Terry.
- Point-based Markov hierarchy: Klaassen & Magnus 2003, Barnett & Clarke 2005, O'Malley 2008 closed forms.
- In-play Bayesian updating: Kovalchik & Reid 2019.

---

## 7. Open questions / next steps

- **What exactly is "Fifteen"?** A link would let us match its architecture.
- **Data analysis plan (proposed):**
  1. Build a Kalshi tennis dataset: all settled match + prop markets via the regular and `/historical` endpoints, with 1-min candles and trades.
  2. Join it to results and point-by-point data (Kalshi `live_data` going forward; Sackmann mirrors / charting historically).
  3. Measure:
     - calibration of Kalshi prices, pre-match and in-play, by tier and price bucket;
     - spread and depth by tier and time-to-start;
     - how fair-price and retirement settlements behave;
     - lag between `live_data` score updates and trade/price bursts, to see how far behind the free feed is.
  4. Fit a Markov in-play model and check prop-vs-match consistency to quantify edge after fees.
- Start recording live order books + `live_data` now (WS), since historical depth snapshots aren't available from the API.

Scratch pull scripts (survey of open markets, settled-market download, candlestick sampling) are in this session's scratchpad. They will be rebuilt properly in the data-analysis phase.
