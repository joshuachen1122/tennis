"""The agent: discovers Kalshi tennis matches, records live scores / books / trades, prices every
match with the Markov model, and runs the paper strategies."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import func, select, update

from tennisbot.archive import archive_days
from tennisbot.db import BookTick, Database, LLMNote, Match, PaperTrade, ScoreEvent, Signal, TradeBar, utcnow
from tennisbot.kalshi.client import Book, KalshiAPIError, KalshiClient, MarketStream, dollars
from tennisbot.llm import LLM
from tennisbot.model.live import LIVE_STATUSES, SERVE_TOTALS, LiveBoard, infer_format, parse_board, point_winner, to_state
from tennisbot.model.markov import MatchFormat, ScoreState, leverage, remaining_points, solve_serve_split, win_prob
from tennisbot.strategies import (
    DEFAULT_PARAMS,
    PARAMS_VERSION,
    Snapshot,
    StrategyParams,
    evaluate,
    params_from_dict,
    params_to_dict,
    blend,
    should_exit,
    size_contracts,
    taker_fee_cents,
)

logger = logging.getLogger(__name__)

DEFAULT_SEC_PER_POINT = 40.0  # incl. changeovers; replaced by the match's own pace once it has 20+ points
DEFAULT_SERIES = "KXATPMATCH,KXWTAMATCH,KXATPCHALLENGERMATCH,KXWTACHALLENGERMATCH,KXITFMATCH,KXITFWMATCH"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./tennis.db"
    series: str = DEFAULT_SERIES
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8080
    dashboard_password: Optional[str] = None
    kalshi_rps: float = 8.0

    # cadence
    discovery_sec: float = 45.0
    milestone_sec: float = 180.0
    live_poll_sec: float = 1.0
    cold_poll_sec: float = 30.0
    rest_book_sec: float = 2.0  # REST top-of-book refresh when the WebSocket is down
    flush_sec: float = 2.0
    settle_sec: float = 30.0

    # model
    prior_points: float = 120.0  # weight of the pre-match serve estimate, in serve points
    max_serve_gap: float = 0.20  # cap on |pa - pb| (0.20 is already a heavy mismatch)
    inplay_calib_max_leverage: float = 0.12  # only back serve rates out of in-play prices at calm moments
    # trading guard rails (shared by all strategies)
    agent_enabled: bool = True
    quiet_sec: float = 5.0  # score and price unchanged for this long before trading
    max_mid_move_cents: float = 2.0  # max mid range over the quiet window
    # Kalshi's live board lags the market by ~10-25s, so a price move that the board has not yet
    # explained means the board is behind: block until the board changes. Threshold is the larger
    # of this and `unexplained_leverage_frac` x the model's one-point swing.
    unexplained_move_cents: float = 2.0
    unexplained_leverage_frac: float = 0.4
    max_spread_cents: int = 5
    max_board_age_sec: float = 5.0
    reentry_cooldown_sec: float = 30.0
    paper_bankroll_usd: float = 1000.0  # per strategy
    archive_after_days: int = 2  # raw research rows older than this move to gzip CSV on the volume

    # LLM
    llm_model: str = "claude-opus-5-5"
    llm_review_effort: str = "low"
    llm_analyst_effort: str = "high"
    llm_daily_budget_usd: float = 1.0
    # Temporary higher daily cap until a UTC time (experiments), then back to llm_daily_budget_usd.
    llm_boost_daily_usd: float = 0.0
    llm_boost_until: Optional[datetime] = None
    llm_shadow_reviews: bool = True
    # LLM-gated strategies: after a "skip", wait before asking again about the same match, and cap
    # the reviews per match. A "take" must still be tradeable when the answer arrives.
    llm_gate_cooldown_sec: float = 900.0
    llm_gate_max_reviews_per_match: int = 3
    llm_gate_max_slip_cents: int = 1
    analyst_interval_hours: float = 24.0
    analyst_min_closed_trades: int = 10

    def series_list(self) -> list[str]:
        return [s.strip() for s in self.series.split(",") if s.strip()]


@dataclass
class MatchRT:
    """In-memory runtime state for one match."""

    row: Match
    fmt: Optional[MatchFormat] = None
    board: Optional[LiveBoard] = None
    board_key: Optional[tuple] = None
    board_changed_at: float = 0.0
    last_poll_ok: float = 0.0
    state: Optional[ScoreState] = None
    won_a: int = 0
    played_a: int = 0
    won_b: int = 0
    played_b: int = 0
    pa: Optional[float] = None
    pb: Optional[float] = None
    model_a: Optional[float] = None
    lev: Optional[float] = None
    mids: deque = field(default_factory=lambda: deque(maxlen=900))  # (ts, mid_a)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # one evaluation per match at a time
    first_live: float = 0.0  # when we first saw this match live (for points-per-second pace)
    ref_mid: Optional[float] = None  # mid when the board last changed
    since_lo: Optional[float] = None  # mid range since the board last changed
    since_hi: Optional[float] = None
    last_book_write: float = 0.0
    last_book_key: Optional[tuple] = None
    last_eval: float = 0.0
    final: bool = False
    is_doubles: bool = False
    entries: dict = field(default_factory=lambda: defaultdict(int))  # strategy -> count
    last_exit: dict = field(default_factory=dict)  # strategy -> ts
    signal_throttle: dict = field(default_factory=dict)
    volume: float = 0.0
    reviewing: Optional[str] = None  # strategy waiting on an LLM gate verdict (reserves the match)
    gate_reviews: dict = field(default_factory=lambda: defaultdict(int))  # strategy -> reviews asked
    gate_skip_until: dict = field(default_factory=dict)  # strategy -> ts

    @property
    def live(self) -> bool:
        return bool(self.board and self.board.is_live)


class Engine:
    def __init__(self, settings: Settings, db: Database, client: KalshiClient, llm: Optional[LLM]) -> None:
        self.s = settings
        self.db = db
        self.client = client
        self.llm = llm
        self.matches: dict[str, MatchRT] = {}
        self.market_event: dict[str, str] = {}
        self.rest_books: dict[str, Book] = {}
        self.stream = MarketStream(client, self._on_book, self._on_trade)
        self.params: dict[str, StrategyParams] = {k: v for k, v in DEFAULT_PARAMS.items()}
        self.agent_enabled = settings.agent_enabled
        self.open_trades: dict[int, PaperTrade] = {}
        self.realized_cents: dict[str, float] = defaultdict(float)
        self.pending: list[Any] = []
        self.trade_agg: dict[tuple[str, int], list[float]] = {}
        self.marks: list[tuple[float, int, str]] = []  # (due_ts, trade_id, column)
        self.fee_rate: dict[str, float] = {}
        self.llm_spend_today = 0.0
        self.llm_day = utcnow().date()
        self.stats: dict[str, Any] = {}
        self.health: dict[str, Any] = {"started": utcnow()}
        self.last_review: Optional[dict[str, Any]] = None
        self._stop = asyncio.Event()
        self._analyst_running = False

    # ------------------------------------------------------------------ lifecycle

    async def restore(self) -> None:
        stored = await self.db.get_setting("strategy_params")
        version = await self.db.get_setting("strategy_params_version")
        if stored and version == str(PARAMS_VERSION):
            self.params = params_from_dict(json.loads(stored))
        else:  # new defaults replace parameters saved under an older version
            await self.set_params({})
        enabled = await self.db.get_setting("agent_enabled")
        if enabled is not None:
            self.agent_enabled = enabled == "true" and self.s.agent_enabled
        async with self.db.session() as s:
            for t in (await s.execute(select(PaperTrade).where(PaperTrade.closed_at.is_(None)))).scalars():
                self.open_trades[t.id] = t
            rows = await s.execute(select(PaperTrade.strategy, func.sum(PaperTrade.pnl)).where(PaperTrade.pnl.is_not(None)).group_by(PaperTrade.strategy))
            for strat, pnl in rows:
                self.realized_cents[strat] = float(pnl or 0)
            since = datetime.combine(utcnow().date(), datetime.min.time(), tzinfo=timezone.utc)
            spent = (await s.execute(select(func.sum(LLMNote.cost_usd)).where(LLMNote.ts >= since))).scalar()
            self.llm_spend_today = float(spent or 0)
            note = (await s.execute(select(LLMNote).where(LLMNote.kind == "review").order_by(LLMNote.id.desc()).limit(1))).scalar()
            if note:
                self.last_review = {"ts": note.ts, "model": note.model, "cost": note.cost_usd, **note.body}
            # Matches still in progress: reload so open positions keep their context after a restart.
            cutoff = utcnow() - timedelta(hours=36)
            for m in (await s.execute(select(Match).where(Match.first_seen >= cutoff, Match.status != "settled"))).scalars():
                if m.calib_source == "inplay":  # re-derive under the current calibration rules
                    m.pa0 = m.pb0 = None
                    m.calib_source = ""
                self._add_rt(m)
            await self._restore_match_history(s)
        logger.info("restored %d open paper trades, %d matches", len(self.open_trades), len(self.matches))

    async def _restore_match_history(self, s: Any) -> None:
        """Rebuild what a restart would otherwise forget: the serve points each player has won (the
        model's in-match update) and the entries per strategy per match. Without this every deploy
        reset in-progress matches to their pre-match serve rates; replaying 34 settled matches,
        that alone took the model from level with the market (Brier 0.1337 vs 0.1339) to 0.1413."""
        events = set(self.matches)
        if not events:
            return
        prev: dict[str, Optional[bool]] = {}
        rows = await s.execute(select(ScoreEvent.event_ticker, ScoreEvent.a_serving, ScoreEvent.point_winner, ScoreEvent.status)
                               .where(ScoreEvent.event_ticker.in_(events)).order_by(ScoreEvent.id))
        for ev, a_serving, pw, status in rows:
            rt = self.matches[ev]
            server = prev.get(ev)
            if pw is not None and server is not None:
                if server:
                    rt.played_a += 1
                    rt.won_a += pw == 1
                else:
                    rt.played_b += 1
                    rt.won_b += pw == 2
            prev[ev] = a_serving if status in LIVE_STATUSES else None
        rows = await s.execute(select(PaperTrade.event_ticker, PaperTrade.strategy).where(
            PaperTrade.event_ticker.in_(events), PaperTrade.closed_at.is_not(None)))
        for ev, strat in rows:
            self.matches[ev].entries[strat] += 1

    def _add_rt(self, m: Match) -> MatchRT:
        for attr in ("scheduled", "started_at", "ended_at", "first_seen"):  # SQLite hands back naive datetimes
            v = getattr(m, attr)
            if v is not None:
                setattr(m, attr, _aware(v))
        rt = MatchRT(row=m)
        rt.is_doubles = "DOUBLES" in m.series
        if m.pa0 is not None and m.pb0 is not None:
            rt.pa, rt.pb = m.pa0, m.pb0
        self.matches[m.event_ticker] = rt
        for t in (m.market_a, m.market_b):
            if t:
                self.market_event[t] = m.event_ticker
        for t in self.open_trades.values():
            if t.event_ticker == m.event_ticker:
                rt.entries[t.strategy] += 1
        return rt

    def tasks(self) -> list[asyncio.Task]:
        loops = {
            "discovery": self._loop(self.discover, self.s.discovery_sec),
            "milestones": self._loop(self.sync_milestones, self.s.milestone_sec, delay=3),
            "live-hot": self._loop(self.poll_live_hot, self.s.live_poll_sec, delay=6),
            "live-cold": self._loop(self.poll_live_cold, self.s.cold_poll_sec, delay=8),
            "rest-books": self._loop(self.refresh_rest_books, self.s.rest_book_sec, delay=5),
            "evaluate": self._loop(self.evaluate_all, 1.0, delay=10),
            "flush": self._loop(self.flush, self.s.flush_sec),
            "settle": self._loop(self.settle, self.s.settle_sec, delay=20),
            "stats": self._loop(self.refresh_stats, 10.0, delay=2),
            "analyst": self._loop(self.maybe_run_analyst, 600.0, delay=120),
            "archive": self._loop(self.archive, 3600.0, delay=300),
            "stream": self.stream.run(self._stop),
        }
        return [asyncio.create_task(c, name=n) for n, c in loops.items()]

    async def _loop(self, fn: Any, every: float, delay: float = 0.0) -> None:
        await asyncio.sleep(delay)
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a loop must never die
                logger.exception("loop %s failed", getattr(fn, "__name__", fn))
            await asyncio.sleep(max(0.05, every - (time.monotonic() - t0)))

    async def shutdown(self) -> None:
        self._stop.set()
        await self.flush(force=True)

    # ------------------------------------------------------------------ discovery

    async def discover(self) -> None:
        if not self.fee_rate:
            for series in self.s.series_list():
                try:
                    info = await self.client.series(series)
                    self.fee_rate[series] = 0.07 * float(info.get("fee_multiplier") or 1)
                except KalshiAPIError:
                    self.fee_rate[series] = 0.07
        open_events: set[str] = set()
        for series in self.s.series_list():
            try:
                markets = await self.client.markets(series_ticker=series, status="open")
            except KalshiAPIError as exc:
                logger.warning("discover %s: %s", series, exc)
                continue
            by_event: dict[str, list[dict]] = defaultdict(list)
            for m in markets:
                by_event[m["event_ticker"]].append(m)
            for ev, ms in by_event.items():
                open_events.add(ev)
                if len(ms) != 2:
                    continue
                rt = self.matches.get(ev)
                if rt is None:
                    rt = await self._new_match(series, ev, ms)
                for m in ms:
                    self._rest_book(m)
                rt.volume = sum(dollars(m.get("volume_fp")) or 0 for m in ms)
        await self.stream.set_tickers(self._stream_tickers())
        self.health["open_events"] = len(open_events)
        self.health["last_discovery"] = utcnow()
        # Matches that dropped off the open list have closed; settle them.
        for ev, rt in self.matches.items():
            if ev not in open_events and not rt.final and rt.row.status not in ("settled",):
                rt.final = True

    async def _new_match(self, series: str, ev: str, ms: list[dict]) -> MatchRT:
        m0, m1 = sorted(ms, key=lambda m: m["ticker"])
        sched = m0.get("occurrence_datetime") or m0.get("expected_expiration_time")
        row = Match(
            event_ticker=ev,
            series=series,
            title=f"{m0.get('yes_sub_title', '')} vs {m1.get('yes_sub_title', '')}",
            player_a=m0.get("yes_sub_title", ""),
            player_b=m1.get("yes_sub_title", ""),
            market_a=m0["ticker"],
            market_b=m1["ticker"],
            comp_a=(m0.get("custom_strike") or {}).get("tennis_competitor", ""),
            comp_b=(m1.get("custom_strike") or {}).get("tennis_competitor", ""),
            scheduled=_parse_ts(sched),
            serve_total=SERVE_TOTALS.get(series, 1.2),
        )
        async with self.db.session() as s:
            existing = await s.get(Match, ev)
            if existing is None:
                s.add(row)
                await s.commit()
            else:
                row = existing
        return self._add_rt(row)

    def _rest_book(self, m: dict) -> None:
        b = self.rest_books.setdefault(m["ticker"], Book(m["ticker"]))
        b.set_from_rest(m)

    def _stream_tickers(self) -> set[str]:
        now = utcnow()
        out: set[str] = set()
        for rt in self.matches.values():
            if rt.final:
                continue
            sched = rt.row.scheduled
            soon = sched is None or sched - timedelta(hours=3) <= now
            if rt.live or soon:
                out.update(t for t in (rt.row.market_a, rt.row.market_b) if t)
        return out

    def book(self, ticker: str) -> Optional[Book]:
        """The freshest book: WebSocket if it is connected and has the market, else REST top-of-book."""
        b = self.stream.books.get(ticker)
        if b is not None and self.stream.connected and b.updated:
            return b
        return self.rest_books.get(ticker)

    async def refresh_rest_books(self) -> None:
        if self.stream.connected:
            return
        live_series = {rt.row.series for rt in self.matches.values() if rt.live and not rt.final}
        for series in live_series:
            try:
                for m in await self.client.markets(series_ticker=series, status="open"):
                    self._rest_book(m)
                    ev = self.market_event.get(m["ticker"])
                    if ev:
                        await self._on_book(m["ticker"])
            except KalshiAPIError as exc:
                logger.debug("rest books %s: %s", series, exc)

    # ------------------------------------------------------------------ milestones & live scores

    async def sync_milestones(self) -> None:
        since = (utcnow() - timedelta(hours=36)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            milestones = await self.client.milestones(since)
        except KalshiAPIError as exc:
            logger.warning("milestones: %s", exc)
            return
        changed: list[Match] = []
        for ms in milestones:
            tickers = list(ms.get("primary_event_tickers") or []) + list(ms.get("related_event_tickers") or [])
            ev = next((t for t in tickers if t in self.matches), None)
            if ev is None:
                continue
            rt = self.matches[ev]
            d = ms.get("details") or {}
            row = rt.row
            if row.milestone_id == ms.get("id") and row.tournament:
                continue
            row.milestone_id = ms.get("id")
            row.sportradar_id = ms.get("source_id")
            row.tournament = d.get("tournament_name", "") or ""
            row.round = d.get("round", "") or ""
            try:
                row.best_of = int(d.get("best_of") or 3)
            except (TypeError, ValueError):
                row.best_of = 3
            if ms.get("start_date"):
                row.scheduled = _parse_ts(ms["start_date"]) or row.scheduled
            rt.is_doubles = ms.get("type") == "tennis_tournament_doubles" or rt.is_doubles
            changed.append(row)
        if changed:
            async with self.db.session() as s:
                for row in changed:
                    await s.merge(row)
                await s.commit()
        self.health["milestones"] = sum(1 for rt in self.matches.values() if rt.row.milestone_id)

    def _hot(self, rt: MatchRT, now: datetime) -> bool:
        if rt.final or not rt.row.milestone_id:
            return False
        if rt.live:
            return True
        sched = rt.row.scheduled
        return sched is not None and sched - timedelta(minutes=20) <= now <= sched + timedelta(hours=10)

    async def poll_live_hot(self) -> None:
        now = utcnow()
        await self._poll([rt for rt in self.matches.values() if self._hot(rt, now)])

    async def poll_live_cold(self) -> None:
        now = utcnow()
        await self._poll([rt for rt in self.matches.values() if rt.row.milestone_id and not rt.final and not self._hot(rt, now)])

    async def _poll(self, rts: list[MatchRT]) -> None:
        if not rts:
            return
        by_ms = {rt.row.milestone_id: rt for rt in rts}
        try:
            datas = await self.client.live_batch(list(by_ms))
        except KalshiAPIError as exc:
            logger.debug("live batch: %s", exc)
            return
        t = time.time()
        for ld in datas:
            rt = by_ms.get(ld.get("milestone_id"))
            board = parse_board(ld.get("details") or {})
            if rt is None or board is None:
                continue
            rt.last_poll_ok = t
            await self._on_board(rt, board, t)

    async def _on_board(self, rt: MatchRT, board: LiveBoard, t: float) -> None:
        key = board.key()
        if key == rt.board_key:
            return
        prev = rt.board
        row = rt.row
        # Canonical player A is the live board's competitor 1.
        if board.comp1 and row.comp_a and board.comp1 == row.comp_b and board.comp2 == row.comp_a:
            row.comp_a, row.comp_b = row.comp_b, row.comp_a
            row.market_a, row.market_b = row.market_b, row.market_a
            row.player_a, row.player_b = row.player_b, row.player_a
            row.pa0, row.pb0 = row.pb0, row.pa0
            if row.prematch_mid_a is not None:
                row.prematch_mid_a = 100 - row.prematch_mid_a
            rt.mids.clear()
            await self._save(row)
        if rt.fmt is None:
            rt.fmt = infer_format(row.tournament, row.best_of, rt.is_doubles, board)
            if rt.fmt is not None and row.fmt != rt.fmt.code:
                row.fmt = rt.fmt.code
        rt.board, rt.board_key, rt.board_changed_at = board, key, t
        rt.ref_mid = rt.since_lo = rt.since_hi = self._mid_a(rt)
        pw: Optional[int] = None
        if prev is not None and prev.is_live and board.is_live and prev.server in (row.comp_a, row.comp_b):
            pw = point_winner(prev, board)
            if pw is not None:
                if prev.server == row.comp_a:
                    rt.played_a += 1
                    rt.won_a += pw == 1
                else:
                    rt.played_b += 1
                    rt.won_b += pw == 2
        if board.is_live and not rt.first_live:
            rt.first_live = t
        if board.is_live and row.started_at is None:
            row.started_at = utcnow()
            row.status = "live"
            await self._save(row)
        self._reprice(rt)
        if board.is_live and (row.pa0 is None) and rt.state is not None and prev is not None:
            # Calibrate on a board *change* (not first sight), when board and market agree best.
            await self._calibrate(rt)
            self._reprice(rt)
        bid_a, ask_a = self._bbo(row.market_a)[0::2] if row.market_a else (None, None)
        self.pending.append(ScoreEvent(
            event_ticker=row.event_ticker, ts=utcnow(), status=board.status,
            sets_a=board.sets_1, sets_b=board.sets_2, games_a=board.games_1, games_b=board.games_2,
            points_a=str(board.raw_points_1), points_b=str(board.raw_points_2),
            a_serving=(board.server == row.comp_a) if board.server else None,
            set_scores=json.dumps(board.set_scores), point_winner=pw, bid_a=bid_a, ask_a=ask_a,
            model_a=rt.model_a, pa=rt.pa, pb=rt.pb, leverage=rt.lev, delayed=board.delayed,
        ))
        if board.is_final and not rt.final:
            rt.final = True
            row.status = "final"
            row.ended_at = utcnow()
            row.final_score = board.score_text()
            await self._save(row)
        elif board.status in ("cancelled", "canceled", "walkover") and not rt.final:
            rt.final = True
            row.status = board.status
            await self._save(row)

    async def _calibrate(self, rt: MatchRT) -> None:
        row = rt.row
        fmt = rt.fmt
        if fmt is None:
            return
        total = row.serve_total or SERVE_TOTALS.get(row.series, 1.2)
        st = rt.state
        early = st is not None and st.sets_a == st.sets_b == 0 and st.games_a + st.games_b <= 1
        if row.prematch_mid_a is not None and early:
            pa, pb = solve_serve_split(row.prematch_mid_a / 100.0, total, fmt)
            row.calib_source = "prematch"
        else:
            # Joined mid-match: invert the current price, but only at a low-leverage moment. At a
            # tiebreak or break point a one-point lag in the board makes the inversion extreme.
            mid = self._mid_a(rt)
            if mid is None or st is None or st.in_tiebreak(fmt) or self.unexplained_move(rt):
                return
            if leverage(total / 2, total / 2, st, fmt) > self.s.inplay_calib_max_leverage:
                return
            pa, pb = solve_serve_split(mid / 100.0, total, fmt, st)
            row.calib_source = "inplay"
        gap = max(-self.s.max_serve_gap, min(self.s.max_serve_gap, pa - pb))  # cap implausible splits
        pa, pb = total / 2 + gap / 2, total / 2 - gap / 2
        row.pa0, row.pb0 = round(pa, 4), round(pb, 4)
        rt.pa, rt.pb = row.pa0, row.pb0
        await self._save(row)
        logger.info("calibrated %s (%s): pa=%.3f pb=%.3f", row.event_ticker, row.calib_source, pa, pb)

    def _reprice(self, rt: MatchRT) -> None:
        row, board, fmt = rt.row, rt.board, rt.fmt
        rt.state = to_state(board, fmt, row.comp_a, row.comp_b) if (board and fmt and board.is_live) else None
        if rt.state is None or row.pa0 is None or row.pb0 is None:
            rt.model_a = None
            return
        k = self.s.prior_points
        rt.pa = (k * row.pa0 + rt.won_a) / (k + rt.played_a)
        rt.pb = (k * row.pb0 + rt.won_b) / (k + rt.played_b)
        rt.model_a = win_prob(rt.pa, rt.pb, rt.state, fmt)
        rt.lev = leverage(rt.pa, rt.pb, rt.state, fmt)

    async def _save(self, row: Match) -> None:
        async with self.db.session() as s:
            await s.merge(row)
            await s.commit()

    # ------------------------------------------------------------------ books & trades

    def _bbo(self, ticker: str) -> tuple[Optional[int], float, Optional[int], float]:
        b = self.book(ticker)
        return b.bbo() if b else (None, 0.0, None, 0.0)

    def _mid_a(self, rt: MatchRT) -> Optional[float]:
        bid, _, ask, _ = self._bbo(rt.row.market_a)
        if bid is None or ask is None or ask - bid > 10:
            return None
        return (bid + ask) / 2.0

    async def _on_book(self, ticker: str) -> None:
        ev = self.market_event.get(ticker)
        rt = self.matches.get(ev) if ev else None
        if rt is None:
            return
        row = rt.row
        bid_a, bsz_a, ask_a, asz_a = self._bbo(row.market_a)
        bid_b, _, ask_b, _ = self._bbo(row.market_b)
        now = time.time()
        mid = self._mid_a(rt)
        if mid is not None:
            if not rt.mids or rt.mids[-1][1] != mid:
                rt.mids.append((now, mid))
            if rt.ref_mid is None and rt.board is not None:
                rt.ref_mid = rt.since_lo = rt.since_hi = mid
            elif rt.ref_mid is not None:
                rt.since_lo = min(rt.since_lo if rt.since_lo is not None else mid, mid)
                rt.since_hi = max(rt.since_hi if rt.since_hi is not None else mid, mid)
            if row.started_at is None and not rt.live and bid_a and ask_a and ask_a - bid_a <= 6:
                row.prematch_mid_a = mid
        key = (bid_a, ask_a, bid_b, ask_b)
        interval = 1.0 if rt.live else 30.0
        if key != rt.last_book_key and now - rt.last_book_write >= interval:
            rt.last_book_key, rt.last_book_write = key, now
            self.pending.append(BookTick(event_ticker=row.event_ticker, ts=utcnow(), bid_a=bid_a, ask_a=ask_a, bid_a_size=bsz_a,
                                         ask_a_size=asz_a, bid_b=bid_b, ask_b=ask_b, live=rt.live, model_a=rt.model_a))
        if rt.live and now - rt.last_eval >= 0.5:
            await self._evaluate(rt, now)

    def _on_trade(self, msg: dict) -> None:
        ticker = msg.get("market_ticker")
        if ticker not in self.market_event:
            return
        ts = int((msg.get("ts_ms") or (msg.get("ts", 0) * 1000)) // 1000)
        price = (dollars(msg.get("yes_price_dollars")) or 0) * 100
        qty = dollars(msg.get("count_fp")) or 0.0
        side = msg.get("taker_outcome_side") or msg.get("taker_side")
        a = self.trade_agg.setdefault((ticker, ts), [0, 0.0, 0.0, 0.0, -1.0, 101.0])  # n, yes, no, notional, hi, lo
        a[0] += 1
        a[1 if side == "yes" else 2] += qty
        a[3] += qty * price
        a[4] = max(a[4], price)
        a[5] = min(a[5], price)

    async def flush(self, force: bool = False) -> None:
        cutoff = int(time.time()) - (0 if force else 2)
        bars = []
        for (ticker, ts), a in list(self.trade_agg.items()):
            if ts < cutoff:
                qty = a[1] + a[2]
                bars.append(TradeBar(ticker=ticker, ts=datetime.fromtimestamp(ts, timezone.utc), n=int(a[0]), yes_taker_qty=a[1],
                                     no_taker_qty=a[2], vwap=a[3] / qty if qty else 0, hi=a[4], lo=a[5]))
                del self.trade_agg[(ticker, ts)]
        rows, self.pending = self.pending + bars, []
        if rows:
            await self.db.add_all(rows)
        await self._process_marks()

    # ------------------------------------------------------------------ strategies

    def bankroll_cents(self, strategy: str) -> float:
        open_cost = sum(t.count * t.entry_price + t.entry_fee for t in self.open_trades.values() if t.strategy == strategy)
        return self.s.paper_bankroll_usd * 100 + self.realized_cents.get(strategy, 0.0) - open_cost

    def _gate(self, rt: MatchRT, now: float) -> Optional[str]:
        if not self.agent_enabled:
            return "agent_stopped"
        if rt.model_a is None or rt.fmt is None or rt.board is None or not rt.board.is_live:
            return "no_model"
        if rt.board.delayed:
            return "feed_delayed"
        if now - rt.last_poll_ok > self.s.max_board_age_sec:
            return "board_stale"
        if now - rt.board_changed_at < self.s.quiet_sec:
            return "score_not_quiet"
        b = self.book(rt.row.market_a)
        if b is None or now - b.updated > (30 if self.stream.connected else 8):
            return "book_stale"
        window = [m for t, m in rt.mids if t >= now - self.s.quiet_sec]
        if rt.mids:
            # include the last mid before the window (the price in force at its start)
            before = [m for t, m in rt.mids if t < now - self.s.quiet_sec]
            if before:
                window.append(before[-1])
        if window and max(window) - min(window) > self.s.max_mid_move_cents:
            return "price_not_quiet"
        bid, _, ask, _ = b.bbo()
        if bid is None or ask is None or ask - bid > self.s.max_spread_cents:
            return "wide_spread"
        if self.unexplained_move(rt):
            return "board_behind_market"
        return None

    def unexplained_move(self, rt: MatchRT) -> bool:
        """True if the price has moved since the last board change by more than a fraction of a
        point's swing: the market has seen a point our (lagging) score feed has not."""
        if rt.ref_mid is None or rt.since_lo is None or rt.since_hi is None:
            return True
        thr = max(self.s.unexplained_move_cents, self.s.unexplained_leverage_frac * (rt.lev or 0) * 100)
        return (rt.since_hi - rt.since_lo) > thr

    async def evaluate_all(self) -> None:
        now = time.time()
        for rt in list(self.matches.values()):
            if rt.live and not rt.final and now - rt.last_eval >= 0.9:
                await self._evaluate(rt, now)

    async def _evaluate(self, rt: MatchRT, now: float) -> None:
        # Book updates and the 1s timer both evaluate; without the lock two evaluations can pass
        # the "already open" check while the first entry is still being written.
        if rt.lock.locked():
            return
        async with rt.lock:
            await self._evaluate_locked(rt, now)

    async def _evaluate_locked(self, rt: MatchRT, now: float) -> None:
        rt.last_eval = now
        await self._check_exits(rt)
        gate = self._gate(rt, now)
        if gate is not None:
            return
        row = rt.row
        snap = self._snapshot(rt)
        for name, params in self.params.items():
            for intent in evaluate(name, params, snap):
                ticker = row.market_a if intent.side == "a" else row.market_b
                reason = intent.skip or self._position_block(rt, name, params, now)
                count = 0
                if reason is None:
                    count = size_contracts(params, self.bankroll_cents(name), intent.model_p, intent.ask, intent.ask_size)
                    if count < 1:
                        reason = "size_zero"
                if reason is None and params.llm_gate:
                    if self.llm is None or self._llm_budget_left() <= 0.2:
                        reason = "llm_unavailable"  # fail closed
                    else:
                        self._log_signal(rt, intent, ticker, "llm_review", now)
                        rt.reviewing = name
                        rt.gate_reviews[name] += 1
                        asyncio.create_task(self._gated_entry(rt, name, intent, ticker, count))
                        return
                self._log_signal(rt, intent, ticker, reason or "trade", now)
                if reason is None:
                    await self._enter(rt, name, intent, ticker, count)

    def _snapshot(self, rt: MatchRT) -> Snapshot:
        row = rt.row
        bid_a, _, ask_a, asz_a = self._bbo(row.market_a)
        bid_b, _, ask_b, asz_b = self._bbo(row.market_b)
        return Snapshot(row.event_ticker, row.series, rt.fmt, rt.state, rt.model_a, rt.lev or 0.0,  # type: ignore[arg-type]
                        bid_a, ask_a, asz_a, bid_b, ask_b, asz_b, calib=row.calib_source or "", volume=rt.volume or 0.0)

    def _position_block(self, rt: MatchRT, name: str, params: StrategyParams, now: float) -> Optional[str]:
        held = [t for t in self.open_trades.values() if t.event_ticker == rt.row.event_ticker]
        if any(t.strategy == name for t in held):
            return "already_open"
        if held:  # one position per match across strategies: they share the same model error
            return "match_held"
        if rt.reviewing is not None:
            return "llm_reviewing"
        if rt.entries[name] >= params.max_entries_per_match:
            return "max_entries"
        if now - rt.last_exit.get(name, 0) < self.s.reentry_cooldown_sec:
            return "cooldown"
        if params.llm_gate:
            if now < rt.gate_skip_until.get(name, 0):
                return "llm_skip_cooldown"
            if rt.gate_reviews[name] >= self.s.llm_gate_max_reviews_per_match:
                return "llm_max_reviews"
        return None

    def _log_signal(self, rt: MatchRT, intent: Any, ticker: str, action: str, now: float) -> None:
        k = (intent.strategy, ticker, action)
        if action != "trade" and now - rt.signal_throttle.get(k, 0) < 30:
            return
        rt.signal_throttle[k] = now
        self.pending.append(Signal(strategy=intent.strategy, event_ticker=rt.row.event_ticker, ticker=ticker, ask=intent.ask,
                                   model_p=round(intent.model_p, 4), edge=round(intent.edge, 4), action=action, score=self._score_text(rt)))

    def _score_text(self, rt: MatchRT) -> str:
        b = rt.board
        if b is None:
            return ""
        srv = "A" if b.server == rt.row.comp_a else ("B" if b.server == rt.row.comp_b else "?")
        return f"{b.score_text()} srv {srv}"

    def _context(self, rt: MatchRT, side: str) -> dict[str, Any]:
        row = rt.row
        recent = [round(m, 1) for _, m in list(rt.mids)[-30:]]
        return {
            "tournament": row.tournament, "round": row.round, "series": row.series, "format": rt.fmt.code if rt.fmt else None,
            "player_a": row.player_a, "player_b": row.player_b, "bought": row.player_a if side == "a" else row.player_b,
            "score": self._score_text(rt), "set_scores": rt.board.set_scores if rt.board else None,
            "prematch_mid_a_cents": row.prematch_mid_a, "calibration": row.calib_source,
            "serve_win_prior_a": row.pa0, "serve_win_prior_b": row.pb0, "serve_win_now_a": rt.pa, "serve_win_now_b": rt.pb,
            "serve_points_a": f"{rt.won_a}/{rt.played_a}", "serve_points_b": f"{rt.won_b}/{rt.played_b}",
            "model_prob_a": rt.model_a, "point_leverage": rt.lev, "recent_mid_a_cents": recent, "match_volume": rt.volume,
        }

    def _intent_context(self, rt: MatchRT, name: str, intent: Any) -> dict[str, Any]:
        params = self.params[name]
        return {**self._context(rt, intent.side), "strategy": name, "params_version": PARAMS_VERSION, "ask_cents": intent.ask,
                "model_prob_bought_side": round(intent.model_p, 4), "raw_model_prob_bought_side": intent.extra.get("raw_model_p"),
                "model_weight": params.model_weight, "edge": round(intent.edge, 4)}

    async def _enter(self, rt: MatchRT, name: str, intent: Any, ticker: str, count: int,
                     verdict: Optional[dict[str, Any]] = None) -> None:
        fee = taker_fee_cents(count, intent.ask, self.fee_rate.get(rt.row.series, 0.07))
        ctx = self._intent_context(rt, name, intent)
        trade = PaperTrade(
            strategy=name, event_ticker=rt.row.event_ticker, ticker=ticker, series=rt.row.series,
            player=rt.row.player_a if intent.side == "a" else rt.row.player_b, count=count, entry_price=intent.ask,
            entry_fee=fee, model_p=round(intent.model_p, 4), edge=round(intent.edge, 4), leverage=rt.lev,
            score=self._score_text(rt), context=ctx, llm_verdict=verdict,
        )
        async with self.db.session() as s:
            s.add(trade)
            await s.commit()
            await s.refresh(trade)
        self.open_trades[trade.id] = trade
        rt.entries[name] += 1
        now = time.time()
        self.marks += [(now + 10, trade.id, "mark_10s"), (now + 60, trade.id, "mark_60s")]
        logger.info("PAPER BUY [%s] %s x%d @ %dc model %.3f edge %.3f | %s", name, trade.player, count, intent.ask,
                    intent.model_p, intent.edge, trade.score)
        if verdict is None and self.llm is not None and self.s.llm_shadow_reviews:
            asyncio.create_task(self._shadow_review(trade.id, ctx))

    async def _gated_entry(self, rt: MatchRT, name: str, intent: Any, ticker: str, count: int) -> None:
        """Ask the LLM before entering. On "take", re-check the match under its lock and enter at the
        current ask if the trade still qualifies. Every verdict is stored (kind="gate") so skipped
        candidates can be scored against the settlement later."""
        ctx = self._intent_context(rt, name, intent)
        note: dict[str, Any] = {"strategy": name, "event_ticker": rt.row.event_ticker, "ticker": ticker, "side": intent.side,
                                "ask": intent.ask, "count": count, "model_p": round(intent.model_p, 4),
                                "raw_model_p": intent.extra.get("raw_model_p"), "edge": round(intent.edge, 4),
                                "score": ctx["score"], "params_version": PARAMS_VERSION}
        cost, model = 0.0, self.s.llm_model
        try:
            verdict, usage, status = await self.llm.review_trade(ctx, proposed=True)  # type: ignore[union-attr]
            self.llm_spend_today += usage.cost_usd
            cost, model = usage.cost_usd, usage.model
            body = verdict.model_dump() if verdict else None
            note.update(verdict=body, status=status)
            if body is None or not body.get("take"):
                rt.gate_skip_until[name] = time.time() + self.s.llm_gate_cooldown_sec
                note["outcome"] = "skip" if body else "error"
                return
            async with rt.lock:
                rt.reviewing = None
                note["outcome"] = await self._recheck_and_enter(rt, name, intent, body)
        except Exception:  # noqa: BLE001 - a failed review must never leave the match reserved
            logger.exception("LLM gate failed for %s", rt.row.event_ticker)
            note.setdefault("outcome", "error")
        finally:
            if rt.reviewing == name:
                rt.reviewing = None
            async with self.db.session() as s:
                s.add(LLMNote(kind="gate", model=model, cost_usd=cost, body=note))
                await s.commit()

    async def _recheck_and_enter(self, rt: MatchRT, name: str, intent: Any, verdict: dict[str, Any]) -> str:
        now = time.time()
        params = self.params.get(name)
        if params is None or self._gate(rt, now) is not None or rt.state is None:
            return "stale_gate"
        row = rt.row
        fresh = next((i for i in evaluate(name, params, self._snapshot(rt)) if i.side == intent.side and i.skip is None), None)
        if fresh is None or fresh.ask > intent.ask + self.s.llm_gate_max_slip_cents:
            return "stale_price"
        if self._position_block(rt, name, params, now) is not None:
            return "stale_position"
        count = size_contracts(params, self.bankroll_cents(name), fresh.model_p, fresh.ask, fresh.ask_size)
        if count < 1:
            return "stale_size"
        ticker = row.market_a if fresh.side == "a" else row.market_b
        self._log_signal(rt, fresh, ticker, "trade", now)
        await self._enter(rt, name, fresh, ticker, count, verdict=verdict)
        return "entered"

    async def _check_exits(self, rt: MatchRT) -> None:
        if rt.model_a is None:
            return
        for t in [t for t in self.open_trades.values() if t.event_ticker == rt.row.event_ticker]:
            params = self.params.get(t.strategy)
            if params is None:
                continue
            raw = rt.model_a if t.ticker == rt.row.market_a else 1 - rt.model_a
            bid, bid_size, ask, _ = self._bbo(t.ticker)
            reason = should_exit(params, blend(raw, bid, ask, params.model_weight), bid)
            if reason is None or bid is None or bid_size < 1:
                continue
            if time.time() - rt.board_changed_at < 2 or self.unexplained_move(rt):
                continue  # the board may be behind the market; the model price is stale
            fee = taker_fee_cents(t.count, bid, self.fee_rate.get(t.series, 0.07))
            await self._close(t, float(bid), fee, reason)
            rt.last_exit[t.strategy] = time.time()

    async def _close(self, t: PaperTrade, price: float, fee: float, reason: str) -> None:
        pnl = (price - t.entry_price) * t.count - t.entry_fee - fee
        async with self.db.session() as s:
            await s.execute(update(PaperTrade).where(PaperTrade.id == t.id).values(
                closed_at=utcnow(), exit_price=price, exit_fee=fee, exit_reason=reason, pnl=pnl))
            await s.commit()
        self.open_trades.pop(t.id, None)
        self.realized_cents[t.strategy] += pnl
        logger.info("PAPER CLOSE [%s] %s x%g %s @ %.0fc pnl %.0fc", t.strategy, t.player, t.count, reason, price, pnl)

    async def _process_marks(self) -> None:
        now = time.time()
        due = [m for m in self.marks if m[0] <= now]
        if not due:
            return
        self.marks = [m for m in self.marks if m[0] > now]
        async with self.db.session() as s:
            for _, tid, col in due:
                t = self.open_trades.get(tid)
                ticker = t.ticker if t else (await s.get(PaperTrade, tid)).ticker  # type: ignore[union-attr]
                bid = self._bbo(ticker)[0]
                if bid is not None:
                    await s.execute(update(PaperTrade).where(PaperTrade.id == tid).values(**{col: float(bid)}))
            await s.commit()

    # ------------------------------------------------------------------ settlement

    async def settle(self) -> None:
        # Open positions on finished / closed matches
        events = {t.event_ticker for t in self.open_trades.values()}
        candidates = [rt for rt in self.matches.values() if rt.final and rt.row.result_a is None]
        todo = [rt for rt in candidates if rt.row.event_ticker in events] + [rt for rt in candidates if rt.row.event_ticker not in events][:15]
        for rt in todo:
            row = rt.row
            try:
                ma = await self.client.market(row.market_a)
            except KalshiAPIError:
                continue
            value = _settlement_value(ma)
            if value is None:
                continue
            row.result_a = value
            row.status = "settled"
            row.volume = (dollars(ma.get("volume_fp")) or 0) * 2
            await self._save(row)
            for t in [t for t in self.open_trades.values() if t.event_ticker == row.event_ticker]:
                v = value if t.ticker == row.market_a else 1 - value
                await self._close(t, round(v * 100, 2), 0.0, "settled")
            self.matches.pop(row.event_ticker, None)

    # ------------------------------------------------------------------ storage

    def db_path(self) -> Optional[Path]:
        url = self.s.database_url
        return Path(url.split(":///", 1)[1]) if url.startswith("sqlite") and ":///" in url else None

    async def archive(self) -> None:
        path = self.db_path()
        if path is None or not path.exists():
            return
        days = await asyncio.to_thread(archive_days, path, self.s.archive_after_days)
        if days:
            logger.info("archived %s", ", ".join(days))

    # ------------------------------------------------------------------ LLM

    def llm_budget(self) -> float:
        until = self.s.llm_boost_until
        if until is not None and self.s.llm_boost_daily_usd > 0 and utcnow() < _aware(until):
            return self.s.llm_boost_daily_usd
        return self.s.llm_daily_budget_usd

    def _llm_budget_left(self) -> float:
        today = utcnow().date()
        if today != self.llm_day:
            self.llm_day, self.llm_spend_today = today, 0.0
        return self.llm_budget() - self.llm_spend_today

    async def _shadow_review(self, trade_id: int, ctx: dict[str, Any]) -> None:
        if self.llm is None or self._llm_budget_left() <= 0.2:  # keep headroom for the analyst
            return
        verdict, usage, status = await self.llm.review_trade(ctx)
        self.llm_spend_today += usage.cost_usd
        body = verdict.model_dump() if verdict else {"status": status}
        async with self.db.session() as s:
            s.add(LLMNote(kind="shadow", model=usage.model, cost_usd=usage.cost_usd, body={"trade_id": trade_id, **body}))
            if verdict:
                await s.execute(update(PaperTrade).where(PaperTrade.id == trade_id).values(llm_verdict=body))
            await s.commit()

    async def maybe_run_analyst(self, force: bool = False) -> Optional[str]:
        if self.llm is None:
            return "ANTHROPIC_API_KEY not set"
        if self._analyst_running:
            return "analyst already running"
        if not force and self.last_review and utcnow() - _aware(self.last_review["ts"]) < timedelta(hours=self.s.analyst_interval_hours):
            return None
        from tennisbot.research import build_report

        report = await build_report(self.db, self.params)
        if not force and report["totals"]["closed"] < self.s.analyst_min_closed_trades:
            return "not enough closed trades yet"
        if self._llm_budget_left() <= 0:
            return "LLM budget exhausted for today"
        self._analyst_running = True
        try:
            out, usage, status = await self.llm.analyze(report)
        finally:
            self._analyst_running = False
        self.llm_spend_today += usage.cost_usd
        body = out.model_dump() if out else {"summary": f"Analyst unavailable ({status})", "findings": [], "parameter_suggestions": [],
                                              "new_hypotheses": [], "data_gaps": []}
        async with self.db.session() as s:
            note = LLMNote(kind="review", model=usage.model, cost_usd=usage.cost_usd, body=body)
            s.add(note)
            await s.commit()
        self.last_review = {"ts": utcnow(), "model": usage.model, "cost": usage.cost_usd, **body}
        return None if out else status

    # ------------------------------------------------------------------ controls & stats

    async def set_agent_enabled(self, enabled: bool) -> None:
        self.agent_enabled = enabled
        await self.db.set_setting("agent_enabled", "true" if enabled else "false")

    async def set_params(self, raw: dict[str, Any]) -> None:
        self.params = params_from_dict(raw)
        await self.db.set_setting("strategy_params", json.dumps(params_to_dict(self.params)))
        await self.db.set_setting("strategy_params_version", str(PARAMS_VERSION))

    async def refresh_stats(self) -> None:
        from tennisbot.research import strategy_stats

        self.stats = await strategy_stats(self.db, self.params, self.s.paper_bankroll_usd)
        self.health.update({
            "ws_connected": self.stream.connected, "ws_messages": self.stream.messages, "rest_requests": self.client.requests,
            "rest_errors": self.client.errors, "tracked": len(self.matches), "live": sum(1 for r in self.matches.values() if r.live),
            "counts": await self.db.counts(), "llm_spend_today": round(self.llm_spend_today, 3),
            "llm_budget": self.llm_budget(), "llm": self.llm is not None, "now": utcnow(),
        })

    def _time_left(self, rt: MatchRT) -> Optional[dict[str, Any]]:
        """Expected points left (Monte Carlo on the model) x observed seconds per point."""
        if rt.state is None or rt.fmt is None or rt.pa is None or rt.pb is None:
            return None
        cache = getattr(rt, "_tl_cache", None)
        if cache and cache[0] == rt.board_key:
            pts = cache[1]
        else:
            pts = remaining_points(rt.pa, rt.pb, rt.state, rt.fmt, sims=200)
            rt._tl_cache = (rt.board_key, pts)  # type: ignore[attr-defined]
        played = rt.played_a + rt.played_b
        sec_per_point = DEFAULT_SEC_PER_POINT
        if rt.first_live and played >= 20:  # pace over the points we have watched
            sec_per_point = min(max((time.time() - rt.first_live) / played, 25.0), 75.0)
        return {"points": pts, "minutes": pts * sec_per_point / 60}

    async def pnl_series(self) -> dict[str, Any]:
        """Realized P&L steps (one per closed trade), for the P&L history chart."""
        async with self.db.session() as s:
            res = await s.execute(select(PaperTrade.closed_at, PaperTrade.pnl, PaperTrade.player, PaperTrade.strategy, PaperTrade.exit_reason, PaperTrade.opened_at)
                                  .where(PaperTrade.pnl.is_not(None)).order_by(PaperTrade.closed_at))
            closed = res.all()
            first = (await s.execute(select(func.min(PaperTrade.opened_at)))).scalar()
        pts = [[int(_aware(c).timestamp() * 1000), round(p, 2), name, strat, reason] for c, p, name, strat, reason, _ in closed]
        start = int(_aware(first).timestamp() * 1000) if first else int(time.time() * 1000)
        return {"now": int(time.time() * 1000), "start": start, "trades": pts, "strategies": sorted(self.params)}

    def controls_state(self) -> dict[str, Any]:
        return {"agent_enabled": self.agent_enabled, "params": params_to_dict(self.params)}


def _settlement_value(m: dict[str, Any]) -> Optional[float]:
    if m.get("status") not in ("settled", "finalized", "determined") and not m.get("result"):
        return None
    r = m.get("result")
    if r == "yes":
        return 1.0
    if r == "no":
        return 0.0
    if r in ("scalar", "void") or m.get("settlement_value_dollars"):
        v = dollars(m.get("settlement_value_dollars"))
        return v
    return None


def _parse_ts(x: Any) -> Optional[datetime]:
    if not x:
        return None
    try:
        return datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    except ValueError:
        return None


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
