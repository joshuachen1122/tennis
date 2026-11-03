# Tennis Kalshi paper-trading agent

Python 3.11, asyncio, FastAPI + HTMX, SQLAlchemy (aiosqlite), pytest. Deployed on Fly.io as
`tennis-kalshi-agent` (flyctl at `~/.fly/bin/flyctl`). Sister project: `~/fifteen` (BTC 15m agent);
reuse its patterns.

- `tennisbot/model/markov.py`: tennis win-probability engine. Any change must keep
  `tests/test_markov.py` (including the Monte Carlo cross-check) passing.
- `tennisbot/model/live.py`: Kalshi `live_data` boards → `ScoreState`. Points are 0/15/30/40/50(=AD).
  Tiebreak points are raw integers. Player A = board competitor 1.
- `tennisbot/engine.py`: all runtime loops and paper execution. Kalshi's score feed lags the market
  by ~13s (median), so trading must stay gated on `board_behind_market` / quiet windows.
  See `research/02_strategy_hypotheses.md`.
- `tennisbot/strategies.py`: pure strategy logic (replayable on recorded data).
- Paper only: there is no order-placement code. Adding live trading needs explicit user approval,
  spend caps and a kill switch, as in Fifteen.
- Research tables are append-only. Export them via `/api/export/{table}`.
- Run tests: `.venv/bin/python -m pytest -q`
