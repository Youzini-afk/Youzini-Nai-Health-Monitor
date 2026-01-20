from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Mapping

import aiosqlite
import httpx
from cryptography.fernet import Fernet

from app.config import settings
from app.history_db import _db_path, get_config, insert_key_health_event


SUBSCRIPTION_URL = "https://api.novelai.net/user/subscription"


def _require_keypool_enabled() -> None:
    if not settings.keypool_enabled:
        raise RuntimeError("KEYPOOL_ENABLED=false")


def _fernet() -> Fernet:
    key = (settings.keypool_encryption_key or "").strip().encode("utf-8")
    if not key:
        raise RuntimeError("KEYPOOL_ENCRYPTION_KEY must be set when KEYPOOL_ENABLED=true")
    return Fernet(key)


def _split_keys(value: str) -> list[str]:
    parts = []
    for line in (value or "").replace(",", "\n").splitlines():
        s = line.strip()
        if s:
            parts.append(s)
    return parts


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def encrypt_key(raw_key: str) -> str:
    return _fernet().encrypt(raw_key.encode("utf-8")).decode("utf-8")


def decrypt_key(encrypted: str) -> str:
    return _fernet().decrypt(encrypted.encode("utf-8")).decode("utf-8")


@dataclass(frozen=True)
class KeyRow:
    id: int
    key_hash: str
    status: str
    tier: int | None
    is_enabled: bool
    fail_streak: int
    cooldown_until: float | None
    last_checked_at: float | None
    last_error: str | None
    total_checkouts: int
    last_checked_out_at: float | None
    created_at: float


async def import_keys(raw: str) -> dict:
    _require_keypool_enabled()
    keys = _split_keys(raw)
    if not keys:
        return {"received": 0, "created": 0, "skipped_existing": 0}

    now = time.time()
    created = 0
    skipped = 0
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute("BEGIN")
        for k in keys:
            h = hash_key(k)
            enc = encrypt_key(k)
            try:
                await db.execute(
                    """
                    INSERT INTO nai_keys(key_hash, key_encrypted, status, is_enabled, fail_streak, created_at)
                    VALUES (?, ?, 'pending', 1, 0, ?)
                    """,
                    (h, enc, now),
                )
                created += 1
            except aiosqlite.IntegrityError:
                skipped += 1
        await db.commit()
    return {"received": len(keys), "created": created, "skipped_existing": skipped}


async def list_keys() -> list[KeyRow]:
    _require_keypool_enabled()
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            """
            SELECT id, key_hash, status, tier, is_enabled, fail_streak, cooldown_until, last_checked_at, last_error,
                   total_checkouts, last_checked_out_at, created_at
            FROM nai_keys
            ORDER BY id DESC
            """
        ) as cur:
            rows = await cur.fetchall()
    return [
        KeyRow(
            id=int(r[0]),
            key_hash=str(r[1]),
            status=str(r[2]),
            tier=(None if r[3] is None else int(r[3])),
            is_enabled=bool(r[4]),
            fail_streak=int(r[5] or 0),
            cooldown_until=(None if r[6] is None else float(r[6])),
            last_checked_at=(None if r[7] is None else float(r[7])),
            last_error=(None if r[8] is None else str(r[8])),
            total_checkouts=int(r[9] or 0),
            last_checked_out_at=(None if r[10] is None else float(r[10])),
            created_at=float(r[11]),
        )
        for r in rows
    ]


async def set_enabled(key_id: int, enabled: bool) -> None:
    _require_keypool_enabled()
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute("UPDATE nai_keys SET is_enabled=? WHERE id=?", (1 if enabled else 0, int(key_id)))
        await db.commit()


async def delete_key(key_id: int) -> None:
    _require_keypool_enabled()
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute("DELETE FROM nai_keys WHERE id=?", (int(key_id),))
        await db.commit()


def _parse_retry_after(headers: Mapping[str, str] | None) -> int | None:
    if not headers:
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    if not value:
        return None
    value = value.strip()
    try:
        seconds = int(value)
        return max(0, seconds)
    except ValueError:
        return None


def _compute_backoff(base_seconds: int, fail_streak: int, max_seconds: int = 600) -> int:
    if base_seconds <= 0:
        return 0
    streak = max(1, int(fail_streak or 1))
    factor = 2 ** min(streak - 1, 5)
    seconds = base_seconds * factor
    return min(seconds, max_seconds) if max_seconds > 0 else seconds


async def _set_cooldown(db: aiosqlite.Connection, key_id: int, seconds: int) -> None:
    if seconds <= 0:
        return
    until = time.time() + int(seconds)
    await db.execute(
        "UPDATE nai_keys SET cooldown_until = COALESCE(MAX(cooldown_until, ?), ?) WHERE id=?",
        (until, until, int(key_id)),
    )


async def _mark_status(
    db: aiosqlite.Connection,
    key_id: int,
    status: str,
    tier: int | None,
    error: str | None,
    fail_streak: int,
) -> None:
    await db.execute(
        """
        UPDATE nai_keys
        SET status=?, tier=?, last_checked_at=?, last_error=?, fail_streak=?
        WHERE id=?
        """,
        (status, tier, time.time(), error, int(fail_streak), int(key_id)),
    )


async def check_key_health(key_id: int) -> dict:
    _require_keypool_enabled()
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            "SELECT id, key_encrypted, status, tier, fail_streak FROM nai_keys WHERE id=?",
            (int(key_id),),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            raise KeyError("not_found")
        _, key_encrypted, status, tier, fail_streak = row
        raw_key = decrypt_key(str(key_encrypted))

        headers = {"Authorization": f"Bearer {raw_key}"}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(SUBSCRIPTION_URL, headers=headers)

            code = int(resp.status_code)
            msg = f"HTTP {code}"

            # Follow your NovelAI error-code mapping.
            if code in (401,):
                fail_streak = int(fail_streak or 0) + 1
                await _mark_status(db, int(key_id), "invalid", None, "Unauthorized", fail_streak)
                await db.commit()
                return {"id": int(key_id), "status": "checked", "result": "invalid"}

            if code in (403,):
                fail_streak = int(fail_streak or 0) + 1
                await _mark_status(db, int(key_id), "invalid", tier, "Forbidden", fail_streak)
                await db.commit()
                return {"id": int(key_id), "status": "checked", "result": "invalid"}

            if code == 402:
                # Quota/subscription issue: treat as unhealthy and back off longer.
                fail_streak = int(fail_streak or 0) + 1
                await _set_cooldown(db, int(key_id), _compute_backoff(60, fail_streak, 3600))
                new_status = "unhealthy" if fail_streak >= int(settings.keypool_health_check_fail_threshold or 3) else status
                await _mark_status(db, int(key_id), new_status, tier, "Payment Required (402)", fail_streak)
                await db.commit()
                return {"id": int(key_id), "status": "checked", "result": "unhealthy"}

            if code in (409, 429):
                # Concurrency / rate limit: transient, do not immediately mark invalid.
                fail_streak = min(int(fail_streak or 0) + 1, int(settings.keypool_health_check_fail_threshold or 3))
                base = 3 if code == 409 else 8
                cooldown = _compute_backoff(base, fail_streak, 300)
                if code == 429:
                    ra = _parse_retry_after(resp.headers)
                    if ra is not None:
                        cooldown = max(cooldown, ra)
                await _set_cooldown(db, int(key_id), cooldown)
                await _mark_status(db, int(key_id), status, tier, msg, fail_streak)
                await db.commit()
                return {"id": int(key_id), "status": "checked", "result": "transient"}

            if code >= 500 or code in (502, 504):
                # Upstream/server issue: transient. Mark unhealthy only after threshold.
                fail_streak = int(fail_streak or 0) + 1
                await _set_cooldown(db, int(key_id), _compute_backoff(15, fail_streak, 600))
                new_status = "unhealthy" if fail_streak >= int(settings.keypool_health_check_fail_threshold or 3) else status
                await _mark_status(db, int(key_id), new_status, tier, msg, fail_streak)
                await db.commit()
                return {"id": int(key_id), "status": "checked", "result": "transient"}

            if code >= 400:
                # Other 4xx: treat as unknown, degrade after threshold.
                fail_streak = int(fail_streak or 0) + 1
                new_status = "unhealthy" if fail_streak >= int(settings.keypool_health_check_fail_threshold or 3) else status
                await _mark_status(db, int(key_id), new_status, tier, msg, fail_streak)
                await db.commit()
                return {"id": int(key_id), "status": "checked", "result": "unhealthy"}

            # Success
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            tier_val = data.get("tier")
            tier_val = int(tier_val) if isinstance(tier_val, int) or (isinstance(tier_val, str) and tier_val.isdigit()) else None
            if settings.keypool_require_opus_tier and tier_val != 3:
                await _mark_status(db, int(key_id), "unhealthy", tier_val, "Not Opus tier", 0)
            else:
                await _mark_status(db, int(key_id), "healthy", tier_val, None, 0)
            await db.commit()
        except Exception as exc:
            fail_streak = int(fail_streak or 0) + 1
            new_status = status
            if fail_streak >= int(settings.keypool_health_check_fail_threshold or 3):
                new_status = "unhealthy"
            await _mark_status(db, int(key_id), new_status, tier, f"Error: {exc}", fail_streak)
            await _set_cooldown(db, int(key_id), _compute_backoff(10, fail_streak, 300))
            await db.commit()
    return {"id": int(key_id), "status": "checked"}


async def check_all_keys() -> int:
    _require_keypool_enabled()

    # Allow runtime overrides via DB config (front-end configurable).
    cfg = await get_config(
        [
            "KEYPOOL_REQUIRE_OPUS_TIER",
            "KEYPOOL_HEALTH_CHECK_FAIL_THRESHOLD",
        ]
    )
    require_opus = str(cfg.get("KEYPOOL_REQUIRE_OPUS_TIER", "")).strip().lower() in ("1", "true", "yes", "on")
    try:
        fail_threshold = int(cfg.get("KEYPOOL_HEALTH_CHECK_FAIL_THRESHOLD") or settings.keypool_health_check_fail_threshold or 3)
    except Exception:
        fail_threshold = int(settings.keypool_health_check_fail_threshold or 3)

    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute("SELECT id FROM nai_keys WHERE is_enabled=1") as cur:
            rows = await cur.fetchall()

    count = 0
    for (kid,) in rows:
        try:
            # Temporarily override these checks for this cycle.
            old_req = settings.keypool_require_opus_tier
            old_thr = settings.keypool_health_check_fail_threshold
            settings.keypool_require_opus_tier = bool(require_opus)
            settings.keypool_health_check_fail_threshold = int(fail_threshold)
            await check_key_health(int(kid))
        except Exception:
            pass
        finally:
            settings.keypool_require_opus_tier = old_req
            settings.keypool_health_check_fail_threshold = old_thr
        count += 1

    # Record aggregate snapshot for dashboard timeline.
    s = await summary()
    statuses = s.get("statuses") or {}
    await insert_key_health_event(
        enabled=int(s.get("enabled") or 0),
        healthy=int(statuses.get("healthy") or 0),
        unhealthy=int(statuses.get("unhealthy") or 0),
        invalid=int(statuses.get("invalid") or 0),
        pending=int(statuses.get("pending") or 0),
    )

    return count


async def checkout_best_key() -> dict:
    """
    Pick a healthy key with low load (simple strategy):
    - status=healthy AND is_enabled=1
    - order by total_checkouts ASC, last_checked_out_at ASC (NULLS FIRST)
    """
    _require_keypool_enabled()
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            """
            SELECT id, key_encrypted, key_hash, total_checkouts, last_checked_out_at
            FROM nai_keys
            WHERE is_enabled=1 AND status='healthy' AND (cooldown_until IS NULL OR cooldown_until <= ?)
            ORDER BY total_checkouts ASC, COALESCE(last_checked_out_at, 0) ASC, id ASC
            LIMIT 1
            """,
            (time.time(),),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            await db.commit()
            raise KeyError("no_healthy_key")
        key_id, key_encrypted, key_hash, total_checkouts, last_checked_out_at = row
        raw_key = decrypt_key(str(key_encrypted))
        now = time.time()
        await db.execute(
            "UPDATE nai_keys SET total_checkouts=?, last_checked_out_at=? WHERE id=?",
            (int(total_checkouts or 0) + 1, now, int(key_id)),
        )
        await db.commit()
    return {"id": int(key_id), "key": raw_key, "key_hash": str(key_hash)}


async def summary() -> dict:
    _require_keypool_enabled()
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute("SELECT COUNT(*) FROM nai_keys") as cur:
            total = await cur.fetchone()
        async with db.execute("SELECT COUNT(*) FROM nai_keys WHERE is_enabled=1") as cur:
            enabled = await cur.fetchone()
        async with db.execute("SELECT status, COUNT(*) FROM nai_keys WHERE is_enabled=1 GROUP BY status") as cur:
            by_status = await cur.fetchall()
        async with db.execute("SELECT MAX(last_checked_at) FROM nai_keys WHERE is_enabled=1") as cur:
            last_checked = await cur.fetchone()
    status_map = {str(s): int(c or 0) for (s, c) in by_status}
    return {
        "total": int((total[0] if total else 0) or 0),
        "enabled": int((enabled[0] if enabled else 0) or 0),
        "statuses": status_map,
        "last_checked_at": (None if not last_checked or last_checked[0] is None else float(last_checked[0])),
    }
