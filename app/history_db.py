from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from app.config import settings


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS probe_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  ts REAL NOT NULL,
  ok INTEGER NOT NULL,
  status_code INTEGER,
  latency_ms REAL
);

CREATE INDEX IF NOT EXISTS idx_probe_events_name_ts ON probe_events(name, ts);

CREATE TABLE IF NOT EXISTS nai_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key_hash TEXT NOT NULL UNIQUE,
  key_encrypted TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  tier INTEGER,
  is_enabled INTEGER NOT NULL DEFAULT 1,
  fail_streak INTEGER NOT NULL DEFAULT 0,
  cooldown_until REAL,
  last_checked_at REAL,
  last_error TEXT,
  total_checkouts INTEGER NOT NULL DEFAULT 0,
  last_checked_out_at REAL,
  created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nai_keys_status_enabled ON nai_keys(status, is_enabled);
CREATE INDEX IF NOT EXISTS idx_nai_keys_cooldown ON nai_keys(cooldown_until);

CREATE TABLE IF NOT EXISTS system_config (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS key_health_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  enabled INTEGER NOT NULL,
  healthy INTEGER NOT NULL,
  unhealthy INTEGER NOT NULL,
  invalid INTEGER NOT NULL,
  pending INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_key_health_events_ts ON key_health_events(ts);
"""


def _db_path() -> str:
    preferred = (settings.db_path or "").strip()
    if preferred:
        return preferred
    path = (settings.history_db_path or "./data/history.db").strip()
    return path or "./data/history.db"


async def init_db() -> None:
    path = _db_path()
    Path(os.path.dirname(path) or ".").mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.executescript(SCHEMA_SQL)
        # Lightweight migrations for existing SQLite files.
        async with db.execute("PRAGMA table_info('nai_keys')") as cur:
            cols = await cur.fetchall()
        col_names = {str(r[1]) for r in cols}
        if "cooldown_until" not in col_names:
            await db.execute("ALTER TABLE nai_keys ADD COLUMN cooldown_until REAL")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_nai_keys_cooldown ON nai_keys(cooldown_until)")
        await db.commit()


async def get_config(keys: list[str]) -> dict[str, str]:
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            f"SELECT key, value FROM system_config WHERE key IN ({placeholders})",
            tuple(keys),
        ) as cur:
            rows = await cur.fetchall()
    return {str(k): ("" if v is None else str(v)) for (k, v) in rows}


async def set_config(values: dict[str, str]) -> None:
    if not values:
        return
    now = time.time()
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute("BEGIN")
        for k, v in values.items():
            await db.execute(
                """
                INSERT INTO system_config(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (str(k), str(v), now),
            )
        await db.commit()


async def insert_key_health_event(enabled: int, healthy: int, unhealthy: int, invalid: int, pending: int) -> None:
    now = time.time()
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            "INSERT INTO key_health_events(ts, enabled, healthy, unhealthy, invalid, pending) VALUES (?, ?, ?, ?, ?, ?)",
            (now, int(enabled), int(healthy), int(unhealthy), int(invalid), int(pending)),
        )
        await db.commit()


async def key_health_timeline(limit: int = 240) -> list[dict]:
    limit = max(1, int(limit or 240))
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            """
            SELECT ts, enabled, healthy, unhealthy, invalid, pending
            FROM key_health_events
            ORDER BY ts DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    out = [
        {
            "ts": float(ts),
            "enabled": int(enabled),
            "healthy": int(healthy),
            "unhealthy": int(unhealthy),
            "invalid": int(invalid),
            "pending": int(pending),
        }
        for (ts, enabled, healthy, unhealthy, invalid, pending) in rows
    ]
    out.reverse()
    return out


@dataclass(frozen=True)
class Availability:
    windows_percent: dict[str, float | None]
    since_retention_percent: float | None


def parse_windows_minutes(raw: str) -> list[int]:
    raw = (raw or "").strip()
    out: list[int] = []
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        try:
            v = int(part)
            if v > 0:
                out.append(v)
        except Exception:
            continue
    return out or [60, 1440]


async def insert_events(rows: list[tuple]) -> None:
    if not rows:
        return
    async with aiosqlite.connect(_db_path()) as db:
        await db.executemany(
            "INSERT INTO probe_events(name, ts, ok, status_code, latency_ms) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        await db.commit()


async def prune_old(retention_minutes: int) -> None:
    if retention_minutes <= 0:
        return
    cutoff = time.time() - (int(retention_minutes) * 60)
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute("DELETE FROM probe_events WHERE ts < ?", (cutoff,))
        await db.commit()


async def prune_max_points_per_target(max_points: int) -> None:
    if max_points <= 0:
        return
    # Keep newest N per target (SQLite window functions; SQLite >= 3.25).
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            DELETE FROM probe_events
            WHERE id IN (
              SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (PARTITION BY name ORDER BY ts DESC) AS rn
                FROM probe_events
              ) WHERE rn > ?
            )
            """,
            (int(max_points),),
        )
        await db.commit()


async def availability_by_target(windows_minutes: list[int]) -> dict[str, Availability]:
    windows_minutes = windows_minutes or parse_windows_minutes(settings.availability_windows_minutes)
    now = time.time()
    by_name: dict[str, Availability] = {}

    async with aiosqlite.connect(_db_path()) as db:
        # since retention window (best-effort): based on current retention cutoff
        retention_minutes = int(settings.history_retention_minutes or 0)
        if retention_minutes > 0:
            cutoff = now - (retention_minutes * 60)
            async with db.execute(
                "SELECT name, SUM(ok), COUNT(*) FROM probe_events WHERE ts >= ? GROUP BY name",
                (cutoff,),
            ) as cur:
                async for name, ok_sum, total in cur:
                    total = int(total or 0)
                    ok_sum = int(ok_sum or 0)
                    percent = round(ok_sum * 100.0 / total, 2) if total > 0 else None
                    by_name[name] = Availability(windows_percent={}, since_retention_percent=percent)

        for minutes in windows_minutes:
            cutoff = now - (int(minutes) * 60)
            key = f"{int(minutes)}m"
            async with db.execute(
                "SELECT name, SUM(ok), COUNT(*) FROM probe_events WHERE ts >= ? GROUP BY name",
                (cutoff,),
            ) as cur:
                async for name, ok_sum, total in cur:
                    total = int(total or 0)
                    ok_sum = int(ok_sum or 0)
                    percent = round(ok_sum * 100.0 / total, 2) if total > 0 else None
                    current = by_name.get(name) or Availability(windows_percent={}, since_retention_percent=None)
                    current.windows_percent[key] = percent
                    by_name[name] = current

    return {
        name: Availability(
            windows_percent=dict(v.windows_percent),
            since_retention_percent=v.since_retention_percent,
        )
        for name, v in by_name.items()
    }


async def history_tail_per_target(limit: int) -> dict[str, list[dict]]:
    limit = max(1, int(limit or 120))
    series: dict[str, list[dict]] = {}
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            """
            SELECT name, ts, ok, status_code, latency_ms
            FROM (
              SELECT name, ts, ok, status_code, latency_ms,
                     ROW_NUMBER() OVER (PARTITION BY name ORDER BY ts DESC) AS rn
              FROM probe_events
            )
            WHERE rn <= ?
            ORDER BY name ASC, ts ASC
            """,
            (limit,),
        ) as cur:
            async for name, ts, ok, status_code, latency_ms in cur:
                series.setdefault(name, []).append(
                    {
                        "ts": float(ts),
                        "ok": bool(ok),
                        "status_code": status_code if status_code is None else int(status_code),
                        "latency_ms": latency_ms if latency_ms is None else float(latency_ms),
                    }
                )
    return series


async def monitor_timeline(limit: int = 240) -> list[dict]:
    """
    Returns aggregated probe timeline grouped by cycle timestamp.
    Each cycle inserts one row per target with the same `ts`.
    """
    limit = max(1, int(limit or 240))
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            """
            SELECT ts, SUM(ok) AS ok_count, COUNT(*) AS total_count
            FROM probe_events
            GROUP BY ts
            ORDER BY ts DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    out = [
        {"ts": float(ts), "ok": int(ok_count or 0), "total": int(total_count or 0)}
        for (ts, ok_count, total_count) in rows
    ]
    out.reverse()
    return out
