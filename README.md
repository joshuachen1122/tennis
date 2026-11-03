# Tennis Agent

Records Kalshi tennis matches and paper-trades them. It prices every match with a point-based
Markov model, compares that with the market, and shows everything on a password-protected
dashboard. Built in the style of `~/fifteen` (the BTC 15-minute agent) and deployed the same
way, on one Fly.io machine with SQLite on a volume.

**Paper only:** no order endpoints are wired up.

## What it does

- **Discovery:** finds open match markets every 45s, and Kalshi milestones (score-feed ids) every
  3 min. Covers ATP, WTA, ATP Challenger, WTA 125, and ITF men and women.
- **Live scores:** polls `GET /live_data/batch` once a second for live matches.
- **Order books:** maintained over the authenticated WebSocket (`orderbook_delta` + `trade`), with
  REST polling as a fallback.
- **Model** (`tennisbot/model/markov.py`):
  - Exact recursion over points, games, tiebreaks and sets for Bo3, Bo5, the Slam 10-point
    final-set tiebreak, and doubles match tiebreaks. Cross-checked against Monte Carlo in the tests.
  - Serve-point probabilities are backed out of the pre-match price, then updated with observed serve points.
- **Strategies** (`tennisbot/strategies.py`): `model_edge`, `late_close` and `closer`. See
  `research/02_strategy_hypotheses.md`.
- **LLM** (`tennisbot/llm.py`, Claude Opus 5.5):
  - A shadow reviewer gives a take/skip verdict on every paper entry. It is scored, never gating.
  - A strategy analyst reads aggregated results every 6h and suggests parameter changes.
  - Daily budget cap.
- **Dashboard** (`tennisbot/web`):
  - Live matches with model vs market and why each match is or isn't tradeable.
  - Per-strategy PnL and markouts, recent trades, a match page with a price chart, and the analyst review.
  - Kill switch and parameter editor.
  - CSV export of every research table at `/api/export/{table}`.

## Run locally

```bash
uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env            # keys optional locally (REST-only without Kalshi keys)
.venv/bin/python main.py        # http://localhost:8080
.venv/bin/python -m pytest -q
```

## Deploy (Fly.io)

```bash
fly deploy -a tennis-kalshi-agent
```

App `tennis-kalshi-agent`, region `iad`, volume `tennis_data` at `/data`. Secrets
`ANTHROPIC_API_KEY`, `KALSHI_KEY_ID`, `KALSHI_PRIVATE_KEY` and `DASHBOARD_PASSWORD` are the same
values as the Fifteen app's.

## Research data

Pull a table for analysis:

```bash
curl -u :$DASHBOARD_PASSWORD https://tennis-kalshi-agent.fly.dev/api/export/score_events -o score_events.csv
```

Re-measure the score-feed lag from a copy of the database:

```bash
python scripts/feed_lag.py tennis.db
```
