"""SQLite storage (one file on the Fly volume). Research tables are append-only; analysis exports
them with ``scripts/export.py``."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, Index, Integer, String, Text, event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Match(Base):
    """One Kalshi match event (two mutually exclusive player markets)."""

    __tablename__ = "matches"
    event_ticker: Mapped[str] = mapped_column(String, primary_key=True)
    series: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String, default="")
    tournament: Mapped[str] = mapped_column(String, default="")
    round: Mapped[str] = mapped_column(String, default="")
    best_of: Mapped[int] = mapped_column(Integer, default=3)
    fmt: Mapped[str] = mapped_column(String, default="")
    milestone_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    sportradar_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    comp_a: Mapped[str] = mapped_column(String, default="")  # live-board competitor 1
    comp_b: Mapped[str] = mapped_column(String, default="")
    player_a: Mapped[str] = mapped_column(String, default="")
    player_b: Mapped[str] = mapped_column(String, default="")
    market_a: Mapped[str] = mapped_column(String, default="")  # Kalshi market paying if A wins
    market_b: Mapped[str] = mapped_column(String, default="")
    scheduled: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    prematch_mid_a: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # last mid before first ball
    serve_total: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pa0: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # calibrated serve-point probs
    pb0: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    calib_source: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="scheduled", index=True)
    final_score: Mapped[str] = mapped_column(String, default="")
    result_a: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # settlement value of market A (1, 0, or fair price)
    volume: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScoreEvent(Base):
    """Every change of the live board, aligned with the market and the model at that moment."""

    __tablename__ = "score_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_ticker: Mapped[str] = mapped_column(String)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String, default="")
    sets_a: Mapped[int] = mapped_column(Integer, default=0)
    sets_b: Mapped[int] = mapped_column(Integer, default=0)
    games_a: Mapped[int] = mapped_column(Integer, default=0)
    games_b: Mapped[int] = mapped_column(Integer, default=0)
    points_a: Mapped[str] = mapped_column(String, default="")
    points_b: Mapped[str] = mapped_column(String, default="")
    a_serving: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    set_scores: Mapped[str] = mapped_column(String, default="")
    point_winner: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 1=A, 2=B if attributable
    bid_a: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # cents
    ask_a: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    model_a: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pa: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pb: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    leverage: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    delayed: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (Index("ix_score_events_event_ts", "event_ticker", "ts"),)


class BookTick(Base):
    """Top of book for both player markets, written when it changes (at most once per second per match)."""

    __tablename__ = "book_ticks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_ticker: Mapped[str] = mapped_column(String)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    bid_a: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ask_a: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bid_a_size: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ask_a_size: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bid_b: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ask_b: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    live: Mapped[bool] = mapped_column(Boolean, default=False)
    model_a: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    __table_args__ = (Index("ix_book_ticks_event_ts", "event_ticker", "ts"),)


class TradeBar(Base):
    """Public trades aggregated per market per second (prices are YES cents of that market)."""

    __tablename__ = "trade_bars"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    n: Mapped[int] = mapped_column(Integer, default=0)
    yes_taker_qty: Mapped[float] = mapped_column(Float, default=0.0)
    no_taker_qty: Mapped[float] = mapped_column(Float, default=0.0)
    vwap: Mapped[float] = mapped_column(Float, default=0.0)
    hi: Mapped[float] = mapped_column(Float, default=0.0)
    lo: Mapped[float] = mapped_column(Float, default=0.0)
    # No index: nothing in the app reads this table, and its index was a quarter of the file.


class PaperTrade(Base):
    """A paper position: entry (taker buy of YES on one player's market), optional exit, settlement."""

    __tablename__ = "paper_trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy: Mapped[str] = mapped_column(String, index=True)
    event_ticker: Mapped[str] = mapped_column(String, index=True)
    ticker: Mapped[str] = mapped_column(String)
    player: Mapped[str] = mapped_column(String, default="")
    series: Mapped[str] = mapped_column(String, default="")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    count: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)  # cents
    entry_fee: Mapped[float] = mapped_column(Float)  # cents, whole order
    model_p: Mapped[float] = mapped_column(Float)
    edge: Mapped[float] = mapped_column(Float)
    leverage: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score: Mapped[str] = mapped_column(String, default="")
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    mark_10s: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # bid of our side 10s / 60s later
    mark_60s: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # cents (sale price or settlement value)
    exit_fee: Mapped[float] = mapped_column(Float, default=0.0)
    exit_reason: Mapped[str] = mapped_column(String, default="")
    pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # cents, net of fees
    llm_verdict: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)


class Signal(Base):
    """Candidates a strategy saw (traded or skipped and why) — the input for tuning thresholds."""

    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    strategy: Mapped[str] = mapped_column(String, index=True)
    event_ticker: Mapped[str] = mapped_column(String)
    ticker: Mapped[str] = mapped_column(String)
    ask: Mapped[float] = mapped_column(Float)
    model_p: Mapped[float] = mapped_column(Float)
    edge: Mapped[float] = mapped_column(Float)
    action: Mapped[str] = mapped_column(String)  # "trade" or skip reason
    score: Mapped[str] = mapped_column(String, default="")


class LLMNote(Base):
    __tablename__ = "llm_notes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    kind: Mapped[str] = mapped_column(String)  # "review" (strategy analyst) or "shadow" (per-trade)
    model: Mapped[str] = mapped_column(String, default="")
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    body: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self.engine: AsyncEngine = create_async_engine(url)
        if url.startswith("sqlite"):
            event.listen(self.engine.sync_engine, "connect", _pragmas)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("DROP INDEX IF EXISTS ix_trade_bars_ticker_ts"))

    async def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        async with self.session() as s:
            row = await s.get(Setting, key)
            return row.value if row else default

    async def set_setting(self, key: str, value: str) -> None:
        async with self.session() as s:
            row = await s.get(Setting, key)
            if row:
                row.value = value
            else:
                s.add(Setting(key=key, value=value))
            await s.commit()

    async def add_all(self, rows: list[Any]) -> None:
        if not rows:
            return
        async with self.session() as s:
            s.add_all(rows)
            await s.commit()

    async def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        async with self.engine.connect() as conn:
            for t in ("matches", "score_events", "book_ticks", "trade_bars", "paper_trades", "signals", "llm_notes"):
                out[t] = (await conn.execute(text(f"SELECT COUNT(*) FROM {t}"))).scalar_one()
        return out

    async def dispose(self) -> None:
        await self.engine.dispose()


def _pragmas(conn: Any, _rec: Any) -> None:
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


__all__ = ["Base", "Database", "Match", "ScoreEvent", "BookTick", "TradeBar", "PaperTrade", "Signal", "LLMNote", "Setting", "select", "utcnow"]
