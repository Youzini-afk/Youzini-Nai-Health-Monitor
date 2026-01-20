from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.history_db import insert_events, prune_max_points_per_target, prune_old


@dataclass(frozen=True)
class Target:
    name: str
    url: str
    expect: int = 200
    contains: str | None = None
    regex: str | None = None


@dataclass
class ProbeResult:
    name: str
    ok: bool
    status_code: int | None
    latency_ms: float | None
    error: str | None
    checked_at: str

    url: str | None = None
    expect: int | None = None


def parse_targets(raw: str) -> list[Target]:
    raw = (raw or "").strip()
    if not raw:
        return []

    targets: list[Target] = []
    for idx, item in enumerate([p.strip() for p in raw.split(",") if p.strip()], start=1):
        parts = [p.strip() for p in item.split("|") if p.strip()]
        if len(parts) < 2:
            continue
        name, url = parts[0], parts[1]
        expect = 200
        contains: str | None = None
        regex: str | None = None

        for opt in parts[2:]:
            if opt.startswith("expect="):
                try:
                    expect = int(opt.split("=", 1)[1].strip())
                except Exception:
                    expect = 200
            elif opt.startswith("contains="):
                contains = opt.split("=", 1)[1]
            elif opt.startswith("regex="):
                regex = opt.split("=", 1)[1]

        targets.append(Target(name=name or f"target-{idx}", url=url, expect=expect, contains=contains, regex=regex))

    return targets


class Prober:
    _targets_raw: str | None = None
    _targets: list[Target] = []
    _results: dict[str, ProbeResult] = {}
    _lock = asyncio.Lock()

    @classmethod
    def targets(cls) -> list[Target]:
        raw = settings.targets
        if cls._targets_raw == raw:
            return cls._targets
        cls._targets_raw = raw
        cls._targets = parse_targets(raw)
        cls._results = {t.name: cls._results.get(t.name) for t in cls._targets if t.name in cls._results}
        return cls._targets

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @classmethod
    async def probe_one(cls, client: httpx.AsyncClient, target: Target) -> ProbeResult:
        start = time.perf_counter()
        status_code: int | None = None
        error: str | None = None
        ok = False
        try:
            resp = await client.get(target.url)
            status_code = resp.status_code
            if status_code != target.expect:
                ok = False
                error = f"unexpected_status:{status_code}"
            else:
                ok = True
            if ok and (target.contains or target.regex):
                body = resp.text
                if target.contains and target.contains not in body:
                    ok = False
                    error = "missing_contains"
                if ok and target.regex:
                    if not re.search(target.regex, body):
                        ok = False
                        error = "missing_regex"
        except Exception as exc:
            ok = False
            error = str(exc)
        latency_ms = (time.perf_counter() - start) * 1000.0
        return ProbeResult(
            name=target.name,
            ok=ok,
            status_code=status_code,
            latency_ms=latency_ms,
            error=error,
            checked_at=cls._utc_now_iso(),
            url=target.url if settings.expose_urls else None,
            expect=target.expect,
        )

    @classmethod
    async def probe_all_once(cls) -> list[ProbeResult]:
        targets = cls.targets()
        if not targets:
            async with cls._lock:
                cls._results = {}
            return []

        timeout = httpx.Timeout(float(settings.probe_timeout_seconds or 5.0))
        limits = httpx.Limits(max_connections=max(1, int(settings.probe_concurrency or 20)))
        sem = asyncio.Semaphore(max(1, int(settings.probe_concurrency or 20)))

        async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True) as client:
            async def run_one(t: Target) -> ProbeResult:
                async with sem:
                    return await cls.probe_one(client, t)

            results = await asyncio.gather(*(run_one(t) for t in targets), return_exceptions=False)

        async with cls._lock:
            cls._results = {r.name: r for r in results}

        # Persist history to SQLite (batch).
        now = time.time()
        rows = [
            (r.name, now, 1 if r.ok else 0, r.status_code, r.latency_ms)
            for r in results
        ]
        await insert_events(rows)
        await prune_old(int(settings.history_retention_minutes or 0))
        await prune_max_points_per_target(int(settings.history_max_points_per_target or 0))
        return results

    @classmethod
    def snapshot(cls) -> list[dict]:
        targets = cls.targets()
        out: list[dict] = []
        for t in targets:
            r = cls._results.get(t.name)
            out.append(
                {
                    "name": t.name,
                    "ok": bool(r.ok) if r else False,
                    "status_code": r.status_code if r else None,
                    "latency_ms": r.latency_ms if r else None,
                    "error": r.error if r else "not checked yet",
                    "checked_at": r.checked_at if r else None,
                    **({"url": t.url} if settings.expose_urls else {}),
                    "expect": t.expect,
                }
            )
        return out

    @classmethod
    def overall_ok(cls) -> bool:
        items = cls.snapshot()
        if not items:
            return True
        strategy = (settings.ready_strategy or "all").strip().lower()
        oks = [bool(i.get("ok")) for i in items]
        if strategy == "any":
            return any(oks)
        return all(oks)


async def probe_loop() -> None:
    while True:
        try:
            await Prober.probe_all_once()
        except Exception:
            pass
        try:
            await asyncio.sleep(max(1, int(settings.probe_interval_seconds or 30)))
        except asyncio.CancelledError:
            return
