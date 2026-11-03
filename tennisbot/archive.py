"""Keep the SQLite file small: once a UTC day is `archive_after_days` old, its raw research rows
(score events, book ticks, trade bars, signals) are written to gzip CSV files on the volume and
deleted from the database. SQLite reuses the freed pages, so the file stops growing at a few days
of data while the full history stays on disk at ~10x compression (downloadable from the Lab)."""

from __future__ import annotations

import csv
import gzip
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

ARCHIVED_TABLES = ("score_events", "book_ticks", "trade_bars", "signals")


def archive_dir(db_path: Path) -> Path:
    return db_path.parent / "archive"


def archive_days(db_path: Path, keep_days: int, today: date | None = None) -> list[str]:
    """Archive every whole UTC day older than ``keep_days``. Runs synchronously (call it in a
    thread). Returns the days archived."""
    today = today or datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=keep_days)
    out_root = archive_dir(db_path)
    done: list[str] = []
    con = sqlite3.connect(db_path, timeout=30)
    try:
        oldest = None
        for t in ARCHIVED_TABLES:
            row = con.execute(f"SELECT MIN(ts) FROM {t}").fetchone()
            if row and row[0]:
                d = datetime.fromisoformat(str(row[0])[:19]).date()
                oldest = d if oldest is None or d < oldest else oldest
        if oldest is None:
            return done
        day = oldest
        while day < cutoff:
            lo, hi = f"{day.isoformat()} 00:00:00", f"{(day + timedelta(days=1)).isoformat()} 00:00:00"
            folder = out_root / day.isoformat()
            folder.mkdir(parents=True, exist_ok=True)
            for t in ARCHIVED_TABLES:
                n = con.execute(f"SELECT COUNT(*) FROM {t} WHERE ts >= ? AND ts < ?", (lo, hi)).fetchone()[0]
                if not n:
                    continue
                path = folder / f"{t}.csv.gz"
                if path.exists():  # a previous run wrote part of this day: keep it, add the rest
                    path = folder / f"{t}.{int(datetime.now().timestamp())}.csv.gz"
                tmp = path.with_suffix(".tmp")
                cur = con.execute(f"SELECT * FROM {t} WHERE ts >= ? AND ts < ?", (lo, hi))
                with gzip.open(tmp, "wt", newline="") as fh:
                    w = csv.writer(fh)
                    w.writerow([c[0] for c in cur.description])
                    w.writerows(cur)
                tmp.rename(path)
                con.execute(f"DELETE FROM {t} WHERE ts >= ? AND ts < ?", (lo, hi))
                con.commit()
                logger.info("archived %s %s: %d rows -> %s", day, t, n, path.name)
            done.append(day.isoformat())
            day += timedelta(days=1)
    finally:
        con.close()
    return done


def list_archives(db_path: Path) -> list[dict]:
    root = archive_dir(db_path)
    if not root.exists():
        return []
    out = []
    for folder in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        files = sorted(folder.glob("*.csv.gz"))
        out.append({"day": folder.name, "files": [{"name": f.name, "mb": f.stat().st_size / 1e6} for f in files]})
    return out
