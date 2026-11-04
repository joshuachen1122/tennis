"""Match-by-match dashboard in the Kalshi / Citadel style: a Games page of match tiles (live score,
both players' prices, the model, our paper position), a match page with the price chart and
point-by-point log, Performance (P&L history, per-strategy stats), Lab (controls, analyst, data)."""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text

from markupsafe import Markup

from tennisbot.archive import archive_dir, list_archives
from tennisbot.db import PaperTrade
from tennisbot.views import game_detail, games_view

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
EXPORT_TABLES = {"matches", "score_events", "book_ticks", "trade_bars", "paper_trades", "signals", "llm_notes"}


def _password_ok(header: Optional[str], password: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        _, _, supplied = base64.b64decode(header[6:]).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    return secrets.compare_digest(supplied.encode(), password.encode())


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _cents(v: Any) -> str:
    return f"{v:.0f}" if _num(v) else "—"


def _pct(v: Any, digits: int = 1) -> str:
    return f"{v * 100:.{digits}f}%" if _num(v) else "—"


def _usd(v: Any) -> str:
    if not _num(v):
        return "—"
    return f"{'−' if v < 0 else ''}${abs(v):,.2f}"


def _signed_usd(v: Any) -> str:
    if not _num(v):
        return "—"
    return f"{'+' if v > 0 else ''}{_usd(v)}"


def _vol(v: Any) -> str:
    """Kalshi-style volume: contracts traded, e.g. "$1.2M vol"."""
    if not _num(v) or v <= 0:
        return "—"
    for div, suf in ((1e6, "M"), (1e3, "K")):
        if v >= div:
            return f"${v / div:.1f}{suf} vol"
    return f"${v:.0f} vol"


def _iso(v: Any) -> str:
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).isoformat()
    return ""


def _local_time(v: Any) -> Markup:
    if not isinstance(v, datetime):
        return Markup("")
    return Markup(f'<time data-local datetime="{_iso(v)}">{v.strftime("%a %H:%M UTC")}</time>')


TEMPLATES.env.filters.update(cents=_cents, pct=_pct, usd=_usd, signed_usd=_signed_usd, vol=_vol, iso=_iso, local_time=_local_time)

STRATEGY_BLURBS = {
    "model_edge": "Buy when the model, half-blended with the market, beats the ask by 3¢+; LLM-gated",
    "late_close": "Same model, only late in tight deciding sets",
    "closer": "Back 90%+ favorites the market underprices; hold to settlement",
    "favourite": "Market price only, stretched toward the favorite (slope 1.4); hold to settlement",
}


def create_app(engine: Any, password: Optional[str]) -> FastAPI:
    app = FastAPI(title="Tennis Kalshi Agent")
    db = engine.db

    @app.middleware("http")
    async def auth(request: Request, call_next: Any) -> Response:
        # Like Fifteen: anyone can view the dashboard; changing anything (every POST) needs the password.
        if password and request.method not in SAFE_METHODS and not _password_ok(request.headers.get("authorization"), password):
            return Response("Authentication required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="tennis"'})
        return await call_next(request)

    def ctx(**extra: Any) -> dict[str, Any]:
        path = engine.db_path()
        return {"stats": engine.stats, "health": engine.health, "review": engine.last_review,
                "controls": engine.controls_state(), "editable": bool(password),
                "archives": list_archives(path) if path else [], "archive_days": engine.s.archive_after_days, **extra}

    def page(request: Request, name: str, **extra: Any) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, name, ctx(**extra))

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "tracked": len(engine.matches), "live": engine.health.get("live"), "ws": engine.stream.connected}

    @app.get("/", response_class=HTMLResponse)
    async def games(request: Request, view: str = "mine", tour: str = "all") -> HTMLResponse:
        return page(request, "games.html", page="games", G=await games_view(engine, view, tour))

    @app.get("/performance", response_class=HTMLResponse)
    async def performance(request: Request) -> HTMLResponse:
        async with db.session() as s:
            closed = list((await s.execute(select(PaperTrade).where(PaperTrade.pnl.is_not(None)).order_by(PaperTrade.closed_at.desc()).limit(200))).scalars())
        return page(request, "performance.html", page="performance", pnl=await engine.pnl_series(), closed=closed, blurbs=STRATEGY_BLURBS)

    @app.get("/lab", response_class=HTMLResponse)
    async def lab(request: Request) -> HTMLResponse:
        return page(request, "lab.html", page="lab")

    @app.get("/match/{event}", response_class=HTMLResponse)
    async def match(request: Request, event: str) -> HTMLResponse:
        d = await game_detail(engine, event)
        if d is None:
            return HTMLResponse("Match not found", status_code=404)
        return page(request, "match.html", page="games", D=d)

    @app.get("/partials/match/{event}", response_class=HTMLResponse)
    async def match_live(request: Request, event: str) -> HTMLResponse:
        d = await game_detail(engine, event)
        if d is None:
            return HTMLResponse("", status_code=404)
        return page(request, "partials/match_live.html", D=d)

    @app.get("/api/match/{event}/chart")
    async def match_chart(event: str) -> JSONResponse:
        d = await game_detail(engine, event)
        return JSONResponse(d["chart"] if d else {"error": "not found"}, status_code=200 if d else 404)

    @app.get("/partials/{name}", response_class=HTMLResponse)
    async def partial(request: Request, name: str) -> HTMLResponse:
        if name in ("games", "summary"):
            q = request.query_params
            return page(request, f"partials/{name}.html", G=await games_view(engine, q.get("view", "mine"), q.get("tour", "all")))
        if name == "pnl":
            return page(request, "partials/pnl.html", pnl=await engine.pnl_series())
        if name in ("data", "review", "controls"):
            return page(request, f"partials/{name}.html")
        return HTMLResponse("not found", status_code=404)

    async def form(request: Request) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}

    @app.post("/controls/agent", response_class=HTMLResponse)
    async def agent(request: Request) -> HTMLResponse:
        action = (await form(request)).get("action")
        await engine.set_agent_enabled(action == "resume")
        return page(request, "partials/controls.html", notice=f"Agent {'resumed' if action == 'resume' else 'stopped'}.")

    @app.post("/controls/params", response_class=HTMLResponse)
    async def params(request: Request) -> HTMLResponse:
        raw = (await form(request)).get("params", "")
        try:
            await engine.set_params(json.loads(raw))
            return page(request, "partials/controls.html", notice="Strategy parameters saved.")
        except (ValueError, TypeError) as exc:
            return page(request, "partials/controls.html", error=f"Invalid parameters: {exc}")

    @app.post("/controls/analyst", response_class=HTMLResponse)
    async def analyst(request: Request) -> HTMLResponse:
        err = await engine.maybe_run_analyst(force=True)
        return page(request, "partials/review.html", error=err)

    @app.get("/api/archive/{day}/{name}")
    async def archive_file(day: str, name: str) -> Response:
        path = engine.db_path()
        if path is None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or not re.fullmatch(r"[a-z_]+(\.\d+)?\.csv\.gz", name):
            return Response("not found", status_code=404)
        f = archive_dir(path) / day / name
        if not f.exists():
            return Response("not found", status_code=404)
        return FileResponse(f, media_type="application/gzip", filename=f"{day}-{name}")

    @app.get("/api/stats")
    async def api_stats() -> JSONResponse:
        st = dict(engine.stats.get("strategies", {}))
        return JSONResponse({"strategies": st, "health": {k: str(v) if isinstance(v, datetime) else v for k, v in engine.health.items()}})

    mem_cache: dict[str, Any] = {}

    @app.get("/api/debug/mem")
    async def api_mem() -> JSONResponse:
        """Process memory and the biggest object types (cached for a minute: counting is slow)."""
        if mem_cache.get("at", 0) > time.time() - 60:
            return JSONResponse(mem_cache["out"])
        import gc
        import sys
        from collections import Counter

        status = {}
        try:
            for line in open("/proc/self/status"):
                k, _, v = line.partition(":")
                if k in ("VmRSS", "VmSwap", "Threads"):
                    status[k] = v.strip()
        except OSError:
            pass
        objs = gc.get_objects()
        types = Counter(type(o).__name__ for o in objs)
        sized = sorted(((len(o), o) for o in objs if isinstance(o, (list, dict, set, bytearray)) or type(o).__name__ == "deque"),
                       key=lambda x: -x[0])[:12]
        largest = []
        for n, o in sized:
            first = next(iter(o), None)
            largest.append({"type": type(o).__name__, "len": n, "first": repr(first)[:80]})
        del objs, sized
        import tracemalloc

        if tracemalloc.is_tracing():  # PYTHONTRACEMALLOC=n: growth by allocation site since the first reading
            snap = tracemalloc.take_snapshot().filter_traces([tracemalloc.Filter(False, tracemalloc.__file__)])
            base = mem_cache.setdefault("base", snap)
            diff = snap.compare_to(base, "traceback")[:8]
            mem_cache["trace"] = {"traced_mb": round(tracemalloc.get_traced_memory()[0] / 1e6, 1),
                                  "growth": [{"kb": round(d.size_diff / 1e3), "n": d.count_diff,
                                              "at": [f"{f.filename.split('site-packages/')[-1]}:{f.lineno}" for f in d.traceback]}
                                             for d in diff]}
        out = {"proc": status, "top_types": types.most_common(20), "largest": largest, "containers": engine.memory_report(),
               "uptime_s": round(time.time() - engine.health["started"].timestamp()),
               "py_blocks": sys.getallocatedblocks(), "trace": mem_cache.get("trace")}
        mem_cache.update(at=time.time(), out=out)
        return JSONResponse(out)

    @app.post("/api/debug/profile")
    async def api_profile(seconds: float = 20.0) -> Response:
        """Profile the event loop thread for a few seconds (POST, so it needs the password)."""
        import cProfile
        import pstats
        import asyncio

        prof = cProfile.Profile()
        prof.enable()
        await asyncio.sleep(min(max(seconds, 1.0), 60.0))
        prof.disable()
        out = io.StringIO()
        st = pstats.Stats(prof, stream=out)
        st.sort_stats("tottime").print_stats(25)
        st.sort_stats("cumulative").print_stats(35)
        return Response(out.getvalue(), media_type="text/plain")

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
            async with db.read_engine.connect() as conn:
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
