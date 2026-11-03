"""Entrypoint: recorder + paper-trading engine + dashboard in one asyncio process."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from tennisbot.db import Database
from tennisbot.engine import Engine, Settings
from tennisbot.kalshi.client import KalshiClient, Signer
from tennisbot.llm import LLM
from tennisbot.web.app import create_app

logger = logging.getLogger("tennisbot")


class _Server(uvicorn.Server):
    def install_signal_handlers(self) -> None:  # the main loop owns signals
        pass


def _restore_if_staged(url: str) -> None:
    """Volume migrations: a database uploaded as `<db>.restore` replaces the live file at boot,
    before anything opens it."""
    if ":///" not in url:
        return
    path = Path(url.split(":///", 1)[1])
    staged = path.with_name(path.name + ".restore")
    if staged.exists():
        for suffix in ("-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
        os.replace(staged, path)
        logger.warning("restored database from %s", staged)


async def run(settings: Settings) -> None:
    _restore_if_staged(settings.database_url)
    db = Database(settings.database_url)
    await db.init()
    signer = Signer.from_env()
    if signer is None:
        logger.warning("Kalshi credentials not set: REST polling only (no WebSocket books or trade tape)")
    client = KalshiClient(signer, rps=settings.kalshi_rps)
    llm = LLM(settings.llm_model, settings.llm_review_effort, settings.llm_analyst_effort) if LLM.available() else None
    if llm is None:
        logger.warning("ANTHROPIC_API_KEY not set: LLM shadow reviews and analyst disabled")
    engine = Engine(settings, db, client, llm)
    await engine.restore()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    tasks = engine.tasks()
    app = create_app(engine, settings.dashboard_password)
    server = _Server(uvicorn.Config(app, host=settings.host, port=settings.port, log_level="warning"))
    tasks.append(asyncio.create_task(server.serve(), name="web"))
    logger.warning("tennis agent running (PAPER) on :%d, series %s", settings.port, settings.series)

    stopper = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait([stopper, *tasks], return_when=asyncio.FIRST_COMPLETED)
    for t in done:
        if t is not stopper and not t.cancelled() and t.exception():
            logger.error("task %s crashed", t.get_name(), exc_info=t.exception())
    server.should_exit = True
    await engine.shutdown()
    web = next(t for t in tasks if t.get_name() == "web")
    try:  # let uvicorn finish its lifespan instead of cancelling it mid-shutdown
        await asyncio.wait_for(asyncio.shield(web), timeout=10)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        pass
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await client.aclose()
    await db.dispose()


def main() -> None:
    load_dotenv()
    settings = Settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
