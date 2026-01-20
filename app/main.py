import asyncio
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth import COOKIE_NAME, issue_session_cookie, require_login, verify_credentials, verify_session_cookie
from app.config import settings
from app.history_db import (
    get_config,
    init_db,
    key_health_timeline,
    set_config,
)
from app.keypool import (
    check_all_keys,
    check_key_health,
    checkout_best_key,
    delete_key,
    import_keys,
    list_keys,
    set_enabled,
    summary as keypool_summary,
)



def _require_status_token(authorization: str | None) -> None:
    token = (settings.status_token or "").strip()
    if not token:
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    got = authorization.split(" ", 1)[1].strip()
    if got != token:
        raise HTTPException(status_code=403, detail="Invalid token")


def _get_session_from_request(request: Request):
    if not settings.auth_enabled:
        return None
    return verify_session_cookie(request.cookies.get(COOKIE_NAME))


def _require_auth(request: Request, authorization: str | None) -> None:
    if not settings.auth_enabled:
        _require_status_token(authorization)
        return
    session = _get_session_from_request(request)
    if session:
        require_login(session)
        return
    # Allow API access via bearer token when configured
    _require_status_token(authorization)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; connect-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';",
        )
        return response


app = FastAPI(title=settings.app_name)
app.add_middleware(SecurityHeadersMiddleware)

BASE_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = BASE_DIR / "static"
if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/readyz")
async def readyz():
    # Readiness is based on key pool health when enabled.
    ok = True
    detail = None
    if settings.keypool_enabled:
        try:
            s = await keypool_summary()
            healthy = int((s.get("statuses") or {}).get("healthy") or 0)
            ok = healthy > 0
            if not ok:
                detail = "no healthy keys"
        except Exception as exc:
            ok = False
            detail = str(exc)

    payload = {
        "status": "ok" if ok else "error",
        "time": datetime.now(timezone.utc).isoformat(),
        "keypool_enabled": bool(settings.keypool_enabled),
    }
    if detail:
        payload["detail"] = detail
    return payload if ok else JSONResponse(payload, status_code=503)


@app.get("/statusz")
async def statusz(request: Request, authorization: str | None = Header(default=None)):
    _require_auth(request, authorization)

    return {
        "time": datetime.now(timezone.utc).isoformat(),
        "keypool_enabled": bool(settings.keypool_enabled),
        "keypool": (await keypool_summary()) if settings.keypool_enabled else None,
    }


@app.get("/api/keypool/timeline")
async def api_keypool_timeline(
    request: Request,
    authorization: str | None = Header(default=None),
    limit: int = Query(default=240, ge=10, le=5000),
):
    _require_auth(request, authorization)
    if not settings.keypool_enabled:
        raise HTTPException(status_code=400, detail="KEYPOOL_ENABLED=false")
    try:
        return {"items": await key_health_timeline(limit=int(limit))}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/login")
async def login_page():
    if not settings.auth_enabled:
        return RedirectResponse(url="/status", status_code=302)
    page = STATIC_DIR / "login.html"
    if page.exists():
        return FileResponse(page)
    return HTMLResponse("<h1>Login page not found</h1>", status_code=500)


@app.post("/login")
async def login(username: str = Form(...), password: str = Form(...)):
    if not settings.auth_enabled:
        return RedirectResponse(url="/status", status_code=302)
    if not verify_credentials(username=username, password=password):
        return HTMLResponse("Invalid username/password", status_code=403)
    token = issue_session_cookie(username)
    resp = RedirectResponse(url="/status", status_code=302)
    resp.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=bool(settings.auth_cookie_secure),
        max_age=int(settings.auth_session_minutes or 1440) * 60,
        path="/",
    )
    return resp


@app.post("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@app.get("/api/keys/summary")
async def api_keys_summary(request: Request, authorization: str | None = Header(default=None)):
    _require_auth(request, authorization)
    if not settings.keypool_enabled:
        raise HTTPException(status_code=400, detail="KEYPOOL_ENABLED=false")
    try:
        return await keypool_summary()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/config")
async def api_get_config(request: Request, authorization: str | None = Header(default=None)):
    _require_auth(request, authorization)
    keys = [
        "KEYPOOL_HEALTH_CHECK_ENABLED",
        "KEYPOOL_REQUIRE_OPUS_TIER",
        "KEYPOOL_HEALTH_CHECK_INTERVAL_SECONDS",
        "KEYPOOL_HEALTH_CHECK_FAIL_THRESHOLD",
    ]
    values = await get_config(keys)
    return {
        "keypool": {
            "require_opus_tier": values.get("KEYPOOL_REQUIRE_OPUS_TIER", str(settings.keypool_require_opus_tier)).lower()
            in ("1", "true", "yes", "on"),
            "health_check_enabled": values.get("KEYPOOL_HEALTH_CHECK_ENABLED", str(settings.keypool_health_check_enabled)).lower()
            in ("1", "true", "yes", "on"),
            "health_check_interval_seconds": int(
                values.get("KEYPOOL_HEALTH_CHECK_INTERVAL_SECONDS") or settings.keypool_health_check_interval_seconds or 300
            ),
            "health_check_fail_threshold": int(
                values.get("KEYPOOL_HEALTH_CHECK_FAIL_THRESHOLD") or settings.keypool_health_check_fail_threshold or 3
            ),
        }
    }


@app.post("/api/config")
async def api_set_config(
    request: Request,
    authorization: str | None = Header(default=None),
    require_opus_tier: bool = Form(False),
    health_check_enabled: bool = Form(True),
    health_check_interval_seconds: int = Form(300),
    health_check_fail_threshold: int = Form(3),
):
    _require_auth(request, authorization)
    await set_config(
        {
            "KEYPOOL_REQUIRE_OPUS_TIER": "true" if require_opus_tier else "false",
            "KEYPOOL_HEALTH_CHECK_ENABLED": "true" if health_check_enabled else "false",
            "KEYPOOL_HEALTH_CHECK_INTERVAL_SECONDS": str(int(health_check_interval_seconds)),
            "KEYPOOL_HEALTH_CHECK_FAIL_THRESHOLD": str(int(health_check_fail_threshold)),
        }
    )
    return {"saved": True}


@app.get("/api/keys")
async def api_list_keys(request: Request, authorization: str | None = Header(default=None)):
    _require_auth(request, authorization)
    if not settings.keypool_enabled:
        raise HTTPException(status_code=400, detail="KEYPOOL_ENABLED=false")
    try:
        items = await list_keys()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    now = datetime.now(timezone.utc).timestamp()
    return {
        "items": [
            {
                "id": k.id,
                "key_hash": k.key_hash,
                "status": k.status,
                "tier": k.tier,
                "is_enabled": k.is_enabled,
                "fail_streak": k.fail_streak,
                "cooldown_until": k.cooldown_until,
                "cooldown_seconds": (None if not k.cooldown_until or k.cooldown_until <= now else int(k.cooldown_until - now)),
                "last_checked_at": k.last_checked_at,
                "last_error": k.last_error,
                "total_checkouts": k.total_checkouts,
                "last_checked_out_at": k.last_checked_out_at,
                "created_at": k.created_at,
            }
            for k in items
        ]
    }


@app.post("/api/keys/import")
async def api_import_keys(request: Request, keys: str = Form(...), authorization: str | None = Header(default=None)):
    _require_auth(request, authorization)
    if not settings.keypool_enabled:
        raise HTTPException(status_code=400, detail="KEYPOOL_ENABLED=false")
    try:
        result = await import_keys(keys)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return result


@app.post("/api/keys/{key_id}/toggle")
async def api_toggle_key(
    key_id: int,
    request: Request,
    enabled: bool = Form(...),
    authorization: str | None = Header(default=None),
):
    _require_auth(request, authorization)
    if not settings.keypool_enabled:
        raise HTTPException(status_code=400, detail="KEYPOOL_ENABLED=false")
    try:
        await set_enabled(int(key_id), bool(enabled))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"id": int(key_id), "is_enabled": bool(enabled)}


@app.post("/api/keys/{key_id}/delete")
async def api_delete_key(key_id: int, request: Request, authorization: str | None = Header(default=None)):
    _require_auth(request, authorization)
    if not settings.keypool_enabled:
        raise HTTPException(status_code=400, detail="KEYPOOL_ENABLED=false")
    try:
        await delete_key(int(key_id))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"id": int(key_id), "deleted": True}


@app.post("/api/keys/{key_id}/check")
async def api_check_key(key_id: int, request: Request, authorization: str | None = Header(default=None)):
    _require_auth(request, authorization)
    if not settings.keypool_enabled:
        raise HTTPException(status_code=400, detail="KEYPOOL_ENABLED=false")
    try:
        await check_key_health(int(key_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Key not found")
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"id": int(key_id), "checked": True}


@app.post("/api/keys/check-all")
async def api_check_all(request: Request, authorization: str | None = Header(default=None)):
    _require_auth(request, authorization)
    if not settings.keypool_enabled:
        raise HTTPException(status_code=400, detail="KEYPOOL_ENABLED=false")
    try:
        total = await check_all_keys()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"checked": int(total)}


@app.post("/api/keys/checkout")
async def api_checkout(request: Request, authorization: str | None = Header(default=None)):
    _require_auth(request, authorization)
    if not settings.keypool_enabled:
        raise HTTPException(status_code=400, detail="KEYPOOL_ENABLED=false")
    try:
        return await checkout_best_key()
    except KeyError:
        raise HTTPException(status_code=503, detail="No healthy keys available")
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.get("/")
@app.get("/status")
@app.get("/keys")
async def status_page(request: Request):
    if settings.auth_enabled:
        session = _get_session_from_request(request)
        if not session:
            return RedirectResponse(url="/login", status_code=302)
        require_login(session)
    page = STATIC_DIR / "index.html"
    if page.exists():
        return FileResponse(page)
    return JSONResponse({"detail": "status page not found"}, status_code=404)


@app.on_event("startup")
async def on_startup():
    await init_db()
    loop = asyncio.get_event_loop()

    if settings.keypool_enabled and settings.keypool_health_check_enabled:
        async def _keypool_loop():
            while True:
                try:
                    cfg = await get_config(["KEYPOOL_HEALTH_CHECK_ENABLED", "KEYPOOL_HEALTH_CHECK_INTERVAL_SECONDS"])
                    enabled = cfg.get("KEYPOOL_HEALTH_CHECK_ENABLED")
                    enabled = (
                        str(enabled).lower() in ("1", "true", "yes", "on")
                        if enabled is not None and enabled != ""
                        else bool(settings.keypool_health_check_enabled)
                    )
                    interval = cfg.get("KEYPOOL_HEALTH_CHECK_INTERVAL_SECONDS") or settings.keypool_health_check_interval_seconds or 300
                    if enabled:
                        await check_all_keys()
                except Exception:
                    pass
                try:
                    # When disabled, keep a short sleep so "enable" takes effect quickly.
                    await asyncio.sleep(max(5, int(interval) if enabled else 5))
                except asyncio.CancelledError:
                    return

        app.state._keypool_task = loop.create_task(_keypool_loop())
