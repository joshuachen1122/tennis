import csv
import gzip
import sqlite3
from datetime import date

from tennisbot.archive import archive_days, list_archives


def test_archive_moves_old_days_to_gzip(tmp_path):
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    for t in ("score_events", "book_ticks", "trade_bars", "signals"):
        con.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY, ts TEXT, v REAL)")
    con.executemany("INSERT INTO book_ticks (ts, v) VALUES (?, ?)", [
        ("2026-11-01 10:00:00.1", 1), ("2026-11-01 23:59:59.9", 2), ("2026-11-02 00:00:00.0", 3), ("2026-11-04 09:00:00", 4)])
    con.commit()
    con.close()
    assert archive_days(db, keep_days=2, today=date(2026, 11, 4)) == ["2026-11-01"]
    con = sqlite3.connect(db)
    assert [r[0] for r in con.execute("SELECT v FROM book_ticks ORDER BY id")] == [3, 4]
    con.close()
    with gzip.open(tmp_path / "archive" / "2026-11-01" / "book_ticks.csv.gz", "rt") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["id", "ts", "v"] and len(rows) == 3
    assert list_archives(db)[0]["day"] == "2026-11-01"
    assert archive_days(db, keep_days=2, today=date(2026, 11, 4)) == []  # idempotent


async def test_read_then_write_sessions_do_not_fail_with_locked(tmp_path):
    """Two sessions that each read a row and then update it, interleaved. With deferred
    transactions the second update failed at once with 'database is locked' (SQLITE_BUSY_SNAPSHOT)."""
    import asyncio

    from tennisbot.db import Database, Setting

    db = Database(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await db.init()
    await db.set_setting("k", "0")
    first_read = asyncio.Event()

    async def bump(wait: bool) -> None:
        async with db.session() as s:
            row = await s.get(Setting, "k")
            first_read.set()
            if wait:
                await asyncio.sleep(0.2)  # the other session writes and commits meanwhile
            row.value = str(int(row.value) + 1)
            await s.commit()

    async def second() -> None:
        await first_read.wait()
        await bump(False)

    await asyncio.gather(bump(True), second())
    assert await db.get_setting("k") == "2"  # neither update was lost
    await db.dispose()
