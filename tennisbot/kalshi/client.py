"""Kalshi Trade API v2: RSA-PSS signer, async REST reads, and a WebSocket book/trade stream.

This platform only *reads* from Kalshi (paper trading); no order endpoints are wired up.
Auth is optional for REST reads but is sent when configured (higher rate limits); the
WebSocket requires it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from tennisbot.kalshi.rate_limiter import TokenBucketRateLimiter

logger = logging.getLogger(__name__)

PROD_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
PROD_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"


class KalshiAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class Signer:
    """Auth headers: base64(RSA-PSS-SHA256(timestamp_ms + METHOD + path))."""

    def __init__(self, key_id: str, private_key: rsa.RSAPrivateKey) -> None:
        self.key_id = key_id
        self._key = private_key

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> Optional["Signer"]:
        env = dict(os.environ) if env is None else env
        key_id = env.get("KALSHI_KEY_ID") or env.get("KALSHI_API_KEY_ID") or env.get("KALSHI_API_KEY")
        pem: Optional[bytes] = None
        if env.get("KALSHI_PRIVATE_KEY"):
            pem = env["KALSHI_PRIVATE_KEY"].encode()
        elif env.get("KALSHI_PRIVATE_KEY_PATH"):
            pem = Path(env["KALSHI_PRIVATE_KEY_PATH"]).expanduser().read_bytes()
        if not key_id or not pem:
            return None
        pem = pem.strip().replace(b"\\n", b"\n")
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("Kalshi API keys must be RSA private keys")
        return cls(key_id, key)

    def headers(self, method: str, path: str) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        msg = f"{ts}{method.upper()}{path.split('?', 1)[0]}".encode()
        sig = self._key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(), "KALSHI-ACCESS-TIMESTAMP": ts}


def dollars(x: Any) -> Optional[float]:
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


class KalshiClient:
    def __init__(self, signer: Optional[Signer] = None, base_url: str = PROD_BASE_URL, ws_url: str = PROD_WS_URL,
                 rps: float = 8.0, timeout: float = 10.0) -> None:
        self.signer = signer
        self.base_url = base_url.rstrip("/")
        self.ws_url = ws_url
        self._base_path = urlparse(self.base_url).path
        self._limiter = TokenBucketRateLimiter(rps, capacity=max(rps, 1.0))
        self._http = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "tennis-paper-agent/0.1"})
        self.requests = 0
        self.errors = 0

    async def aclose(self) -> None:
        await self._http.aclose()

    def ws_headers(self) -> dict[str, str]:
        return self.signer.headers("GET", urlparse(self.ws_url).path) if self.signer else {}

    async def get(self, endpoint: str, params: Any = None, retries: int = 3) -> dict[str, Any]:
        for attempt in range(retries):
            await self._limiter.acquire()
            headers = {"Accept": "application/json"}
            if self.signer:
                headers.update(self.signer.headers("GET", self._base_path + endpoint))
            self.requests += 1
            try:
                r = await self._http.get(self.base_url + endpoint, params=params, headers=headers)
            except httpx.HTTPError as exc:
                self.errors += 1
                if attempt == retries - 1:
                    raise KalshiAPIError(f"GET {endpoint}: {exc}") from exc
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                self.errors += 1
                if attempt == retries - 1:
                    raise KalshiAPIError(f"GET {endpoint} -> HTTP {r.status_code}", r.status_code)
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            if r.is_error:
                self.errors += 1
                raise KalshiAPIError(f"GET {endpoint} -> HTTP {r.status_code}: {r.text[:200]}", r.status_code)
            return r.json()
        raise KalshiAPIError(f"GET {endpoint} failed")

    async def markets(self, *, series_ticker: Optional[str] = None, event_ticker: Optional[str] = None,
                      status: Optional[str] = "open") -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = None
        for _ in range(20):
            params: dict[str, Any] = {"limit": 1000}
            if series_ticker:
                params["series_ticker"] = series_ticker
            if event_ticker:
                params["event_ticker"] = event_ticker
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor
            body = await self.get("/markets", params)
            out.extend(body.get("markets", []))
            cursor = body.get("cursor")
            if not cursor:
                break
        return out

    async def market(self, ticker: str) -> dict[str, Any]:
        return (await self.get(f"/markets/{ticker}")).get("market", {})

    async def series(self, ticker: str) -> dict[str, Any]:
        return (await self.get(f"/series/{ticker}")).get("series", {})

    async def milestones(self, minimum_start_date: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = None
        for _ in range(20):
            params: dict[str, Any] = {"limit": 500, "category": "Sports", "minimum_start_date": minimum_start_date}
            if cursor:
                params["cursor"] = cursor
            body = await self.get("/milestones", params)
            out.extend(m for m in body.get("milestones", []) if str(m.get("type", "")).startswith("tennis"))
            cursor = body.get("cursor")
            if not cursor:
                break
        return out

    async def live_batch(self, milestone_ids: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i in range(0, len(milestone_ids), 100):
            chunk = milestone_ids[i : i + 100]
            body = await self.get("/live_data/batch", [("milestone_ids", x) for x in chunk])
            out.extend(body.get("live_datas", []))
        return out


# --------------------------------------------------------------------------- order books


def _cents(price: Any) -> int:
    return int(round(float(price) * 100))


@dataclass
class Book:
    ticker: str
    yes: dict[int, float] = field(default_factory=dict)  # yes bids: cents -> size
    no: dict[int, float] = field(default_factory=dict)  # no bids
    updated: float = 0.0  # unix seconds
    _top: Optional[tuple] = field(default=None, repr=False, compare=False)  # cached bbo(), cleared on change

    def bbo(self) -> tuple[Optional[int], float, Optional[int], float]:
        """(yes_bid, bid_size, yes_ask, ask_size); the yes ask is 100 - best no bid."""
        if self._top is None:
            yb = max(self.yes) if self.yes else None
            nb = max(self.no) if self.no else None
            self._top = (yb, self.yes.get(yb, 0.0) if yb else 0.0, (100 - nb) if nb else None, self.no.get(nb, 0.0) if nb else 0.0)
        return self._top

    def ask_depth(self, max_price: int) -> float:
        """Yes contracts available at or below ``max_price`` cents."""
        return sum(q for p, q in self.no.items() if 100 - p <= max_price)

    def bid_depth(self, min_price: int) -> float:
        return sum(q for p, q in self.yes.items() if p >= min_price)

    def snapshot(self, msg: dict[str, Any]) -> None:
        self.yes = {_cents(p): float(q) for p, q in (msg.get("yes_dollars_fp") or msg.get("yes_dollars") or []) if float(q) > 0}
        self.no = {_cents(p): float(q) for p, q in (msg.get("no_dollars_fp") or msg.get("no_dollars") or []) if float(q) > 0}
        self._top = None
        self.updated = time.time()

    def delta(self, msg: dict[str, Any]) -> None:
        price = _cents(msg["price_dollars"]) if msg.get("price_dollars") is not None else int(msg["price"])
        d = float(msg.get("delta_fp", msg.get("delta", 0)))
        side = self.yes if msg.get("side") == "yes" else self.no
        q = side.get(price, 0.0) + d
        if q > 1e-6:
            side[price] = q
        else:
            side.pop(price, None)
        self._top = None
        self.updated = time.time()

    def set_from_rest(self, m: dict[str, Any]) -> None:
        """Top-of-book only, from a REST market object (used when the WebSocket is unavailable)."""
        yb, ya = dollars(m.get("yes_bid_dollars")), dollars(m.get("yes_ask_dollars"))
        self.yes = {round(yb * 100): dollars(m.get("yes_bid_size_fp")) or 0.0} if yb else {}
        self.no = {100 - round(ya * 100): dollars(m.get("yes_ask_size_fp")) or 0.0} if ya and ya < 1 else {}
        self._top = None
        self.updated = time.time()


TradeCallback = Callable[[dict[str, Any]], Awaitable[None] | None]
BookCallback = Callable[[str], Awaitable[None] | None]


class MarketStream:
    """Maintains order books for a dynamic set of tickers over one authenticated WebSocket and
    forwards public trades. Reconnects with backoff; rebuilds books from fresh snapshots."""

    def __init__(self, client: KalshiClient, on_book: BookCallback, on_trade: TradeCallback) -> None:
        self.client = client
        self.books: dict[str, Book] = {}
        self.wanted: set[str] = set()
        self._subscribed: set[str] = set()
        self._on_book = on_book
        self._on_trade = on_trade
        self._ws: Any = None
        self._sids: dict[str, int] = {}
        self._seq: dict[int, int] = {}
        self._cmd_id = 0
        self.connected = False
        self.messages = 0
        self.last_message = 0.0
        # Book callbacks are coalesced: each changed ticker is handled at most once per ``coalesce_sec``
        # rather than on every delta (~1,500/s), which kept the shared CPU throttled. 0 = call on every message.
        self.coalesce_sec = 0.25
        # "ticker" sends the top of book (best bid/ask and sizes) only when it changes; "orderbook_delta"
        # sends every change at every level (~2,000 msgs/s across live tennis, over the shared-CPU quota).
        # The engine only ever reads the top of book.
        self.book_channel = "ticker"
        self._dirty: dict[str, None] = {}

    def book(self, ticker: str) -> Book:
        if ticker not in self.books:
            self.books[ticker] = Book(ticker)
        return self.books[ticker]

    async def set_tickers(self, tickers: set[str]) -> None:
        self.wanted = set(tickers)
        if self._ws is not None and self.connected:
            add = sorted(self.wanted - self._subscribed)
            if add:
                await self._subscribe(add)
            drop = sorted(self._subscribed - self.wanted)
            if drop:
                await self._unsubscribe(drop)

    async def _unsubscribe(self, tickers: list[str]) -> None:
        """Stop streaming markets we no longer need (finished, or not yet close to the start)."""
        for i in range(0, len(tickers), 200):
            chunk = tickers[i : i + 200]
            for ch in (self.book_channel, "trade"):
                if ch in self._sids:
                    await self._send("update_subscription", {"sids": [self._sids[ch]], "market_tickers": chunk, "action": "delete_markets"})
            self._subscribed.difference_update(chunk)
            for t in chunk:
                self.books.pop(t, None)  # a dropped book would go stale; REST takes over

    async def _send(self, cmd: str, params: dict[str, Any]) -> None:
        self._cmd_id += 1
        await self._ws.send(json.dumps({"id": self._cmd_id, "cmd": cmd, "params": params}))

    async def _subscribe(self, tickers: list[str]) -> None:
        for i in range(0, len(tickers), 200):
            chunk = tickers[i : i + 200]
            ch = self.book_channel
            if ch in self._sids:
                await self._send("update_subscription", {"sids": [self._sids[ch]], "market_tickers": chunk, "action": "add_markets"})
            else:
                await self._send("subscribe", {"channels": [ch], "market_tickers": chunk})
            if "trade" in self._sids:
                await self._send("update_subscription", {"sids": [self._sids["trade"]], "market_tickers": chunk, "action": "add_markets"})
            else:
                await self._send("subscribe", {"channels": ["trade"], "market_tickers": chunk})
            self._subscribed.update(chunk)

    async def run(self, stop: asyncio.Event) -> None:
        from websockets.asyncio.client import connect

        backoff = 1.0
        while not stop.is_set():
            if self.client.signer is None:
                await asyncio.sleep(30)
                continue
            if not self.wanted:
                await asyncio.sleep(2)
                continue
            try:
                async with connect(self.client.ws_url, additional_headers=self.client.ws_headers(), ping_interval=10, max_size=2**24) as ws:
                    self._ws, self._sids, self._seq, self._subscribed = ws, {}, {}, set()
                    self.connected = True
                    backoff = 1.0
                    await self._subscribe(sorted(self.wanted))
                    drainer = asyncio.create_task(self._drain_loop()) if self.coalesce_sec > 0 else None
                    try:
                        async for raw in ws:
                            if stop.is_set():
                                break
                            await self._handle(json.loads(raw))
                    finally:
                        if drainer:
                            drainer.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                logger.warning("market stream disconnected: %s", exc)
            finally:
                self.connected = False
                self._ws = None
                self._dirty.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _handle(self, m: dict[str, Any]) -> None:
        self.messages += 1
        self.last_message = time.time()
        typ = m.get("type")
        if typ == "subscribed":
            ch = (m.get("msg") or {}).get("channel")
            if ch:
                self._sids[ch] = (m.get("msg") or {}).get("sid")
            return
        if typ == "error":
            logger.warning("ws error: %s", m.get("msg"))
            return
        sid, seq = m.get("sid"), m.get("seq")
        if sid is not None and seq is not None:
            prev = self._seq.get(sid)
            self._seq[sid] = seq
            if prev is not None and seq != prev + 1 and typ in ("orderbook_delta",):
                raise RuntimeError(f"sequence gap on sid {sid}: {prev} -> {seq}")
        msg = m.get("msg") or {}
        ticker = msg.get("market_ticker")
        if typ == "ticker" and ticker:
            if "yes_bid_dollars" not in msg or "yes_ask_dollars" not in msg:
                return  # not a full top of book; keep what we have
            self.book(ticker).set_from_rest(msg)  # same top-of-book fields as a REST market
            await self._changed(ticker)
        elif typ == "orderbook_snapshot" and ticker:
            self.book(ticker).snapshot(msg)
            await self._changed(ticker)
        elif typ == "orderbook_delta" and ticker:
            self.book(ticker).delta(msg)
            await self._changed(ticker)
        elif typ == "trade" and ticker:
            await _maybe_await(self._on_trade(msg))


    async def _changed(self, ticker: str) -> None:
        if self.coalesce_sec > 0:
            self._dirty[ticker] = None
        else:
            await _maybe_await(self._on_book(ticker))

    async def drain(self) -> None:
        """Run the book callback once for every ticker that changed since the last drain."""
        dirty, self._dirty = self._dirty, {}
        for ticker in dirty:
            if ticker in self.books:
                try:
                    await _maybe_await(self._on_book(ticker))
                except Exception:  # noqa: BLE001 - one bad book must not stop the stream
                    logger.exception("book callback failed for %s", ticker)

    async def _drain_loop(self) -> None:
        while True:
            await asyncio.sleep(self.coalesce_sec)
            await self.drain()


async def _maybe_await(x: Any) -> None:
    if asyncio.iscoroutine(x):
        await x
