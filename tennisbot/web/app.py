"""HTMX dashboard: live matches with model vs market, paper performance per strategy, recorded
data health, the LLM analyst's latest review, controls, and CSV export for research."""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text

from tennisbot.db import BookTick, Match, PaperTrade, ScoreEvent

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
EXPORT_TABLES = {"matches", "score_events", "book_ticks", "trade_bars", "paper_trades", "signals", "llm_notes"}


def _password_ok(header: Optional[str], password: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        _, _, supplied = base64.b64decode(header[6:]).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    return secrets.compare_digest(supplied.encode(), password.encode())


def _cents(v: Any) -> str:
    return "—" if v is None else f"{v:.0f}"


def _pct(v: Any, digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def _usd(v: Any) -> str:
    if v is None:
        return "—"
    return f"{'−' if v < 0 else ''}${abs(v):,.2f}"


TEMPLATES.env.filters.update(cents=_cents, pct=_pct, usd=_usd)


def create_app(engine: Any, password: Optional[str]) -> FastAPI:
    app = FastAPI(title="Tennis Kalshi Agent")
    db = engine.db

    @app.middleware("http")
    async def auth(request: Request, call_next: Any) -> Response:
        if password and request.url.path != "/healthz" and not _password_ok(request.headers.get("authorization"), password):
            return Response("Authentication required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="tennis"'})
        return await call_next(request)

    def ctx(**extra: Any) -> dict[str, Any]:
        return {"stats": engine.stats, "health": engine.health, "live": engine.live_view(), "review": engine.last_review,
                "controls": engine.controls_state(), "editable": bool(password), **extra}

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "tracked": len(engine.matches), "live": engine.health.get("live"), "ws": engine.stream.connected}

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "index.html", ctx())

    @app.get("/partials/{name}", response_class=HTMLResponse)
    async def partial(request: Request, name: str) -> HTMLResponse:
        if name not in ("live", "stats", "trades", "data", "review", "controls"):
            return HTMLResponse("not found", status_code=404)
        return TEMPLATES.TemplateResponse(request, f"partials/{name}.html", ctx())

    async def form(request: Request) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}

    @app.post("/controls/agent", response_class=HTMLResponse)
    async def agent(request: Request) -> HTMLResponse:
        action = (await form(request)).get("action")
        await engine.set_agent_enabled(action == "resume")
        return TEMPLATES.TemplateResponse(request, "partials/controls.html", ctx(notice=f"Agent {'resumed' if action == 'resume' else 'stopped'}."))

    @app.post("/controls/params", response_class=HTMLResponse)
    async def params(request: Request) -> HTMLResponse:
        raw = (await form(request)).get("params", "")
        try:
            await engine.set_params(json.loads(raw))
            return TEMPLATES.TemplateResponse(request, "partials/controls.html", ctx(notice="Strategy parameters saved."))
        except (ValueError, TypeError) as exc:
            return TEMPLATES.TemplateResponse(request, "partials/controls.html", ctx(error=f"Invalid parameters: {exc}"))

    @app.post("/controls/analyst", response_class=HTMLResponse)
    async def analyst(request: Request) -> HTMLResponse:
        err = await engine.maybe_run_analyst(force=True)
        return TEMPLATES.TemplateResponse(request, "partials/review.html", ctx(error=err))

    @app.get("/match/{event}", response_class=HTMLResponse)
    async def match(request: Request, event: str) -> HTMLResponse:
        async with db.session() as s:
            m = await s.get(Match, event)
            ticks = list((await s.execute(select(BookTick).where(BookTick.event_ticker == event, BookTick.live.is_(True)).order_by(BookTick.id))).scalars())
            scores = list((await s.execute(select(ScoreEvent).where(ScoreEvent.event_ticker == event).order_by(ScoreEvent.id))).scalars())
            trades = list((await s.execute(select(PaperTrade).where(PaperTrade.event_ticker == event).order_by(PaperTrade.id))).scalars())
        chart = _chart(ticks, scores, trades, m)
        return TEMPLATES.TemplateResponse(request, "match.html", {"m": m, "chart": chart, "scores": scores[-60:][::-1], "trades": trades})

    @app.get("/api/stats")
    async def api_stats() -> JSONResponse:
        st = dict(engine.stats.get("strategies", {}))
        return JSONResponse({"strategies": st, "health": {k: str(v) if isinstance(v, datetime) else v for k, v in engine.health.items()}})

    @app.get("/api/export/{table}")
    async def export(table: str, since_hours: float = 0) -> Response:
        """CSV export of a research table (``since_hours`` limits time-stamped tables)."""
        if table not in EXPORT_TABLES:
            return Response("unknown table", status_code=404)
        col = {"matches": "first_seen"}.get(table, "ts" if table not in ("paper_trades",) else "opened_at")
        where = ""
        params: dict[str, Any] = {}
        if since_hours > 0:
            where = f" WHERE {col} >= :since"
            params["since"] = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).replace(tzinfo=None).isoformat(" ")

        async def rows():
            async with db.engine.connect() as conn:
                result = await conn.stream(text(f"SELECT * FROM {table}{where}"), params)
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(result.keys())
                async for part in result.partitions(5000):
                    for r in part:
                        w.writerow(r)
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate()
                yield buf.getvalue()

        return StreamingResponse(rows(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{table}.csv"'})

    return app


def _chart(ticks: list, scores: list, trades: list, m: Any) -> str:
    """Inline SVG: market mid (player A) vs model probability over the match."""
    pts_mid = [(t.ts, (t.bid_a + t.ask_a) / 2) for t in ticks if t.bid_a is not None and t.ask_a is not None]
    pts_model = [(s.ts, s.model_a * 100) for s in scores if s.model_a is not None]
    allp = pts_mid + pts_model
    if len(allp) < 2:
        return '<div class="empty">No in-play data recorded for this match yet.</div>'
    t0 = min(p[0] for p in allp)
    t1 = max(p[0] for p in allp)
    span = max((t1 - t0).total_seconds(), 1)
    W, H, L, R, T, B = 960, 300, 40, 10, 10, 24

    def x(ts: datetime) -> float:
        return L + (ts - t0).total_seconds() / span * (W - L - R)

    def y(v: float) -> float:
        return T + (100 - v) / 100 * (H - T - B)

    def poly(pts: list, cls: str) -> str:
        return f'<polyline class="{cls}" points="{" ".join(f"{x(t):.1f},{y(v):.1f}" for t, v in pts)}"/>'

    grid = "".join(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}"/><text class="tick" x="{L - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{v}</text>' for v in (0, 25, 50, 75, 100))
    marks = ""
    for tr in trades:
        side_a = tr.ticker == m.market_a
        v = tr.entry_price if side_a else 100 - tr.entry_price
        marks += f'<circle class="entry" cx="{x(tr.opened_at):.1f}" cy="{y(v):.1f}" r="5"><title>{tr.strategy} buy {tr.player} @{tr.entry_price:.0f}c</title></circle>'
    dur = f"{span / 60:.0f} min"
    return (f'<svg viewBox="0 0 {W} {H}" class="viz">{grid}{poly(pts_mid, "series-mid")}{poly(pts_model, "series-model")}{marks}'
            f'<text class="tick" x="{L}" y="{H - 6}">start</text><text class="tick" x="{W - R}" y="{H - 6}" text-anchor="end">+{dur}</text></svg>')
