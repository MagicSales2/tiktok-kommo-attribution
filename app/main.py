from __future__ import annotations

import json
import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .attribution import build_whatsapp_url, resolve_click, store_click
from .auth import require_admin
from .config import get_base_settings, get_settings
from .db import close_pool, execute, fetch, fetchrow, fetchval
from .kommo import KommoClient, KommoError
from .migrations import run_migrations
from .refcode import generate_ref
from .settings_store import reload_settings, save_settings, secret_status
from .tiktok import build_payload, sha256_hex
from .webhook_parse import parse_kommo_payload
from .worker import Worker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_base_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    await run_migrations()
    await reload_settings()
    worker = Worker()
    await worker.start()
    app.state.worker = worker
    try:
        yield
    finally:
        await worker.stop()
        await close_pool()


app = FastAPI(
    title="TikTok <-> Kommo Attribution",
    version="1.0.0",
    lifespan=lifespan,
)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _load_json(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return value if isinstance(value, dict) else {}


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _payload_for_log(
    body: bytes, content_type: str, store_raw: bool
) -> dict[str, Any]:
    if not store_raw:
        return {"redacted": True}
    try:
        if "json" in (content_type or "").lower() or body.lstrip().startswith(
            (b"{", b"[")
        ):
            data = json.loads(body.decode("utf-8", "replace"))
            return data if isinstance(data, dict) else {"data": data}
    except (ValueError, UnicodeDecodeError):
        pass
    return {"raw": body.decode("utf-8", "replace")[:20000]}


async def _enqueue_lead_job(
    lead_id: int, reason: str, payload: dict[str, Any]
) -> bool:
    settings = get_settings()
    delay = max(0.0, float(settings.lead_job_delay_seconds))
    clean = {key: value for key, value in payload.items() if value not in (None, "")}
    row = await fetchrow(
        """
        INSERT INTO lead_jobs (lead_id, reason, payload, next_attempt_at)
        VALUES ($1, $2, $3::jsonb, now() + make_interval(secs => $4::double precision))
        ON CONFLICT (lead_id) WHERE status = 'pending'
        DO UPDATE SET
            payload = lead_jobs.payload || EXCLUDED.payload,
            reason = EXCLUDED.reason,
            next_attempt_at = now() + make_interval(secs => $4::double precision),
            updated_at = now()
        RETURNING id
        """,
        lead_id,
        reason,
        json.dumps(clean, ensure_ascii=False, default=str),
        delay,
    )
    return row is not None


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)


async def _handle_kommo_webhook(request: Request) -> JSONResponse:
    settings = get_settings()
    supplied = (
        request.query_params.get("secret")
        or request.headers.get("x-webhook-secret")
        or request.path_params.get("secret")
    )
    if settings.require_webhook_secret:
        if not settings.kommo_webhook_secret:
            raise HTTPException(
                status_code=503,
                detail="KOMMO_WEBHOOK_SECRET no está configurado",
            )
        if not supplied or not secrets.compare_digest(
            supplied, settings.kommo_webhook_secret
        ):
            raise HTTPException(status_code=401, detail="secret inválido")

    body = await request.body()
    content_type = request.headers.get("content-type", "")
    signals = parse_kommo_payload(body, content_type)
    dry_run = request.query_params.get("dry_run") in ("1", "true", "yes")

    if not signals:
        await fetchrow(
            """
            INSERT INTO webhook_log (event_type, payload, error)
            VALUES ($1, $2::jsonb, $3)
            RETURNING id
            """,
            "sin_senales",
            json.dumps(
                _payload_for_log(body, content_type, settings.store_raw_payloads),
                ensure_ascii=False,
                default=str,
            ),
            "No se encontraron leads ni mensajes reconocibles",
        )
        return JSONResponse(
            {
                "ok": True,
                "signals": 0,
                "queued": 0,
                "warning": "sin señales reconocidas",
            }
        )

    client = KommoClient(settings)
    queued = 0

    for signal in signals:
        if (
            signal.kind == "message"
            and signal.lead_id is None
            and signal.contact_id
        ):
            try:
                signal.lead_id = await client.find_lead_by_contact(
                    signal.contact_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "No se pudo resolver el lead del contacto %s: %s",
                    signal.contact_id,
                    exc,
                )

        log_payload = (
            signal.payload
            if settings.store_raw_payloads
            else {"event_type": signal.event_type}
        )
        log_row = await fetchrow(
            """
            INSERT INTO webhook_log (event_type, lead_id, payload)
            VALUES ($1, $2, $3::jsonb)
            RETURNING id
            """,
            f"{signal.kind}:{signal.event_type}",
            signal.lead_id,
            json.dumps(log_payload, ensure_ascii=False, default=str),
        )
        log_id = log_row["id"]

        if signal.kind == "message" and not signal.incoming:
            await execute(
                "UPDATE webhook_log SET processed = true, error = $2 WHERE id = $1",
                log_id,
                "mensaje saliente ignorado",
            )
            continue

        if signal.lead_id is None:
            await execute(
                "UPDATE webhook_log SET error = $2 WHERE id = $1",
                log_id,
                "sin lead_id",
            )
            continue

        if dry_run:
            await execute(
                "UPDATE webhook_log SET error = $2 WHERE id = $1",
                log_id,
                "dry_run: no se encoló",
            )
            continue

        job_payload = {
            "text": signal.text,
            "message_id": signal.payload.get("id"),
            "contact_id": signal.contact_id,
            "event_type": signal.event_type,
            "status_id": signal.status_id,
        }
        await _enqueue_lead_job(
            signal.lead_id, f"{signal.kind}:{signal.event_type}", job_payload
        )
        await execute(
            "UPDATE webhook_log SET processed = true WHERE id = $1", log_id
        )
        queued += 1

    return JSONResponse(
        {"ok": True, "signals": len(signals), "queued": queued, "dry_run": dry_run}
    )


@app.post("/webhook/kommo")
async def kommo_webhook(request: Request) -> JSONResponse:
    return await _handle_kommo_webhook(request)


@app.post("/webhook/kommo/{secret}")
async def kommo_webhook_with_secret(request: Request) -> JSONResponse:
    return await _handle_kommo_webhook(request)


@app.get("/r")
async def bridge(request: Request):
    settings = get_settings()
    query = dict(request.query_params)
    raw_ref = generate_ref()

    if query.get("dry_run") in ("1", "true", "yes"):
        try:
            preview_url = build_whatsapp_url(
                settings.whatsapp_number, settings.whatsapp_message, raw_ref
            )
        except ValueError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse(
            {
                "ok": True,
                "stored": False,
                "ref": raw_ref,
                "ttclid": query.get("ttclid"),
                "query": query,
                "whatsapp_url": preview_url,
                "hint": "Quita dry_run=1 para redirigir y guardar el clic.",
            }
        )

    try:
        ref = await store_click(
            ttclid=query.get("ttclid"),
            ttp=query.get("ttp") or query.get("_ttp"),
            utm_source=query.get("utm_source"),
            utm_medium=query.get("utm_medium"),
            utm_campaign=query.get("utm_campaign"),
            utm_content=query.get("utm_content"),
            utm_term=query.get("utm_term"),
            campaign_id=query.get("campaign_id"),
            adgroup_id=query.get("adgroup_id"),
            ad_id=query.get("ad_id"),
            referrer=query.get("referrer") or request.headers.get("referer"),
            landing_url=str(request.url),
            user_agent=request.headers.get("user-agent"),
            ip=_client_ip(request),
        )
        whatsapp_url = build_whatsapp_url(
            settings.whatsapp_number, settings.whatsapp_message, ref
        )
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not query.get("ttclid"):
        logger.warning("Clic %s guardado sin ttclid. Query: %s", ref, query)

    return RedirectResponse(
        whatsapp_url,
        status_code=status.HTTP_302_FOUND,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/health")
async def health() -> JSONResponse:
    settings = get_settings()
    try:
        await fetchval("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {
                "ok": False,
                "db": "error",
                "error": str(exc)[:300],
                "event_source": settings.tiktok_event_source,
            },
            status_code=503,
        )
    config_errors = settings.runtime_errors()
    return JSONResponse(
        {
            "ok": True,
            "db": "ok",
            "ready": not config_errors,
            "event_source": settings.tiktok_event_source,
            "worker_enabled": settings.worker_enabled,
            "config_errors": config_errors,
        }
    )


@app.get("/api/stats", dependencies=[Depends(require_admin)])
async def stats() -> JSONResponse:
    leads = await fetchval("SELECT count(*) FROM leads") or 0
    clicks = await fetchval("SELECT count(*) FROM attribution_clicks") or 0
    rows = await fetch(
        "SELECT status, count(*) AS total FROM events_outbox GROUP BY status"
    )
    outbox = {row["status"]: int(row["total"]) for row in rows}
    return JSONResponse(
        {
            "leads": int(leads),
            "clicks": int(clicks),
            "outbox": outbox,
        }
    )


@app.get("/api/pipelines", dependencies=[Depends(require_admin)])
async def pipelines() -> JSONResponse:
    settings = get_settings()
    try:
        data = await KommoClient(settings).list_pipelines()
    except KommoError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    result = []
    for pipeline in data:
        statuses = ((pipeline.get("_embedded") or {}).get("statuses") or [])
        result.append(
            {
                "id": pipeline.get("id"),
                "name": pipeline.get("name"),
                "statuses": [
                    {
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "sort": item.get("sort"),
                        "is_main": item.get("is_main"),
                    }
                    for item in statuses
                ],
            }
        )
    return JSONResponse({"pipelines": _jsonable(result)})


@app.get("/api/clicks/{code}", dependencies=[Depends(require_admin)])
async def click_detail(code: str) -> JSONResponse:
    click = await resolve_click(code)
    if not click:
        raise HTTPException(status_code=404, detail="Clic no encontrado")
    return JSONResponse(_jsonable(click))


@app.post(
    "/admin/outbox/{outbox_id}/retry",
    dependencies=[Depends(require_admin)],
)
async def retry_outbox(outbox_id: int) -> RedirectResponse:
    await execute(
        """
        UPDATE events_outbox
           SET status = 'pending',
               attempts = 0,
               next_attempt_at = now(),
               locked_at = NULL,
               last_error = NULL
         WHERE id = $1
        """,
        outbox_id,
    )
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@app.post(
    "/admin/leads/{lead_id}/sync",
    dependencies=[Depends(require_admin)],
)
async def sync_lead(lead_id: int) -> RedirectResponse:
    await _enqueue_lead_job(lead_id, "manual", {})
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@app.post(
    "/admin/test-event",
    dependencies=[Depends(require_admin)],
)
async def test_event(
    lead_id: int = Form(...),
    event: str = Form(...),
    ttclid: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    value: str = Form("0"),
) -> RedirectResponse:
    settings = get_settings()
    try:
        numeric_value = float(value.replace(",", ".")) if value.strip() else 0.0
    except ValueError:
        numeric_value = 0.0
    lead = {
        "lead_id": lead_id,
        "name": "Evento de prueba",
        "email": email,
        "phone": phone,
        "ttclid": ttclid,
    }
    click = {"ttclid": ttclid} if ttclid else None
    event_id = sha256_hex(f"test|{uuid.uuid4()}")
    payload = build_payload(
        settings,
        event_name=event,
        event_id=event_id,
        lead_id=lead_id,
        lead=lead,
        click=click,
        stage_key="test",
        value=numeric_value,
        currency=settings.tiktok_currency,
    )
    await fetchrow(
        """
        INSERT INTO events_outbox (
            dedupe_key, event_id, lead_id, event_name, stage_key, payload
        ) VALUES ($1, $1, $2, $3, $4, $5::jsonb)
        RETURNING id
        """,
        event_id,
        lead_id,
        event,
        "test",
        json.dumps(payload, ensure_ascii=False, default=str),
    )
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


_CSRF_COOKIE = "tiktokattr_csrf"


def _csrf_token(request: Request) -> str:
    return request.cookies.get(_CSRF_COOKIE) or secrets.token_urlsafe(32)


def _render_settings(
    request: Request,
    settings: Any,
    csrf_token: str,
    *,
    errors: list[str] | None = None,
    ok: bool = False,
) -> HTMLResponse:
    response = templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "request": request,
            "settings": settings,
            "secret_status": secret_status(settings),
            "csrf_token": csrf_token,
            "errors": errors or [],
            "ok": ok,
            "config_errors": settings.runtime_errors(),
            "webhook_url": (
                f"{settings.public_base_url}/webhook/kommo"
                f"?secret={settings.kommo_webhook_secret or 'FALTA_CONFIGURAR'}"
            ),
        },
        status_code=400 if errors else 200,
    )
    response.set_cookie(
        _CSRF_COOKIE,
        csrf_token,
        max_age=3600,
        httponly=True,
        samesite="strict",
        secure=settings.public_base_url.startswith("https://"),
    )
    return response


@app.get(
    "/settings",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin)],
)
async def settings_page(request: Request) -> HTMLResponse:
    settings = get_settings()
    return _render_settings(
        request,
        settings,
        _csrf_token(request),
        ok=request.query_params.get("ok") == "1",
    )


@app.post("/settings", dependencies=[Depends(require_admin)])
async def settings_save(request: Request):
    form = await request.form()
    cookie = request.cookies.get(_CSRF_COOKIE, "")
    submitted = str(form.get("csrf_token") or "")
    if not cookie or not submitted or not secrets.compare_digest(cookie, submitted):
        raise HTTPException(
            status_code=403, detail="CSRF inválido; recarga la página"
        )

    errors = await save_settings(form)
    if errors:
        return _render_settings(
            request, get_settings(), _csrf_token(request), errors=errors
        )
    return RedirectResponse("/settings?ok=1", status_code=status.HTTP_303_SEE_OTHER)


@app.get(
    "/",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin)],
)
async def dashboard(request: Request) -> HTMLResponse:
    settings = get_settings()
    metrics = {
        "leads": int(await fetchval("SELECT count(*) FROM leads") or 0),
        "clicks": int(
            await fetchval("SELECT count(*) FROM attribution_clicks") or 0
        ),
        "events": int(await fetchval("SELECT count(*) FROM events_outbox") or 0),
        "pending": int(
            await fetchval(
                "SELECT count(*) FROM events_outbox WHERE status IN ('pending','failed','processing')"
            )
            or 0
        ),
        "sent": int(
            await fetchval(
                "SELECT count(*) FROM events_outbox WHERE status = 'sent'"
            )
            or 0
        ),
        "dead": int(
            await fetchval(
                "SELECT count(*) FROM events_outbox WHERE status = 'dead'"
            )
            or 0
        ),
    }
    events = [
        dict(row)
        for row in await fetch(
            """
            SELECT o.id, o.lead_id, o.event_name, o.stage_key, o.status,
                   o.attempts, o.last_error, o.created_at, o.sent_at,
                   l.name, l.phone
              FROM events_outbox o
              LEFT JOIN leads l ON l.lead_id = o.lead_id
             ORDER BY o.id DESC
             LIMIT 50
            """
        )
    ]
    clicks = [
        dict(row)
        for row in await fetch(
            """
            SELECT code, lead_id, ttclid, utm_campaign, created_at, used_at
              FROM attribution_clicks
             ORDER BY created_at DESC
             LIMIT 50
            """
        )
    ]
    webhooks = [
        dict(row)
        for row in await fetch(
            """
            SELECT id, received_at, event_type, lead_id, processed, error
              FROM webhook_log
             ORDER BY id DESC
             LIMIT 50
            """
        )
    ]
    stage_map = [
        {"status_id": status_id, **rule.model_dump()}
        for status_id, rule in sorted(settings.stage_map.items())
    ]
    webhook_secret = settings.kommo_webhook_secret or "FALTA_CONFIGURAR"
    context = {
        "request": request,
        "metrics": metrics,
        "events": events,
        "clicks": clicks,
        "webhooks": webhooks,
        "stage_map": stage_map,
        "config_errors": settings.runtime_errors(),
        "webhook_url": f"{settings.public_base_url}/webhook/kommo?secret={webhook_secret}",
        "bridge_url": f"{settings.public_base_url}/r",
        "event_source": settings.tiktok_event_source,
        "worker_enabled": settings.worker_enabled,
        "test_code_set": bool(settings.tiktok_test_event_code),
        "public_base_url": settings.public_base_url,
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return templates.TemplateResponse(
        request=request, name="dashboard.html", context=context
    )
