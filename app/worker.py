from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import get_settings
from .db import execute, fetchrow, get_pool
from .kommo import (
    KommoClient,
    contact_email,
    contact_name,
    contact_phone,
    field_value,
    first_contact_id,
    lead_name,
    lead_value,
)
from .pipeline import decide
from .refcode import extract_ref
from .tiktok import (
    build_payload,
    event_name_warning,
    send_event,
    sha256_hex,
)

logger = logging.getLogger(__name__)


def _load_json(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return value if isinstance(value, dict) else {}


def _backoff_seconds(attempts: int) -> int:
    return min(300, 5 * (2 ** max(0, attempts - 1)))


class Worker:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_recovery = 0.0

    async def start(self) -> None:
        settings = get_settings()
        if not settings.worker_enabled:
            logger.info("Worker deshabilitado (WORKER_ENABLED=false)")
            return
        if self._task and not self._task.done():
            return
        await self._recover_stale()
        self._task = asyncio.create_task(self._run(), name="attribution-worker")
        logger.info("Worker iniciado")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Worker detenido")

    async def _run(self) -> None:
        settings = get_settings()
        while not self._stop.is_set():
            try:
                if time.monotonic() - self._last_recovery > 60:
                    await self._recover_stale()
                    self._last_recovery = time.monotonic()
                lead_jobs = await self._process_lead_jobs()
                outbox = await self._process_outbox()
                if lead_jobs or outbox:
                    logger.info(
                        "Worker: %s leads, %s eventos procesados", lead_jobs, outbox
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error en el ciclo del worker")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=max(0.2, settings.worker_poll_seconds)
                )
            except TimeoutError:
                continue

    async def _recover_stale(self) -> None:
        await execute(
            """
            UPDATE lead_jobs
               SET status = 'pending', locked_at = NULL, updated_at = now()
             WHERE status = 'processing'
               AND locked_at < now() - interval '5 minutes'
            """
        )
        await execute(
            """
            UPDATE events_outbox
               SET status = 'pending', locked_at = NULL
             WHERE status = 'processing'
               AND locked_at < now() - interval '5 minutes'
            """
        )

    async def _claim_lead_jobs(self) -> list[dict[str, Any]]:
        pool = await get_pool()
        rows = await pool.fetch(
            """
            WITH picked AS (
                SELECT id
                  FROM lead_jobs
                 WHERE status IN ('pending', 'failed')
                   AND next_attempt_at <= now()
                 ORDER BY id
                 LIMIT 20
                 FOR UPDATE SKIP LOCKED
            )
            UPDATE lead_jobs j
               SET status = 'processing',
                   locked_at = now(),
                   attempts = j.attempts + 1,
                   updated_at = now()
              FROM picked
             WHERE j.id = picked.id
            RETURNING j.*
            """
        )
        return [dict(row) for row in rows]

    async def _claim_outbox(self) -> list[dict[str, Any]]:
        pool = await get_pool()
        rows = await pool.fetch(
            """
            WITH picked AS (
                SELECT id
                  FROM events_outbox
                 WHERE status IN ('pending', 'failed')
                   AND next_attempt_at <= now()
                 ORDER BY id
                 LIMIT 20
                 FOR UPDATE SKIP LOCKED
            )
            UPDATE events_outbox o
               SET status = 'processing',
                   locked_at = now(),
                   attempts = o.attempts + 1
              FROM picked
             WHERE o.id = picked.id
            RETURNING o.*
            """
        )
        return [dict(row) for row in rows]

    async def _process_lead_jobs(self) -> int:
        settings = get_settings()
        processed = 0
        for job in await self._claim_lead_jobs():
            try:
                await self._process_lead_job(job)
                await execute(
                    """
                    UPDATE lead_jobs
                       SET status = 'done', last_error = NULL, updated_at = now()
                     WHERE id = $1
                    """,
                    job["id"],
                )
                processed += 1
            except Exception as exc:  # noqa: BLE001
                message = str(exc)[:1000]
                logger.warning("Lead job %s falló: %s", job["id"], message)
                attempts = int(job.get("attempts") or 1)
                if attempts >= settings.max_attempts:
                    await execute(
                        """
                        UPDATE lead_jobs
                           SET status = 'dead', last_error = $2, updated_at = now()
                         WHERE id = $1
                        """,
                        job["id"],
                        message,
                    )
                else:
                    next_at = datetime.now(UTC) + timedelta(
                        seconds=_backoff_seconds(attempts)
                    )
                    await execute(
                        """
                        UPDATE lead_jobs
                           SET status = 'failed',
                               next_attempt_at = $2,
                               last_error = $3,
                               updated_at = now()
                         WHERE id = $1
                        """,
                        job["id"],
                        next_at,
                        message,
                    )
        return processed

    async def _process_lead_job(self, job: dict[str, Any]) -> None:
        settings = get_settings()
        lead_id = int(job["lead_id"])
        job_payload = _load_json(job.get("payload"))

        client = KommoClient(settings)
        lead = await client.get_lead(lead_id)
        if not lead:
            raise ValueError(f"Kommo no devolvió el lead {lead_id}")

        pipeline_id = int(lead.get("pipeline_id") or 0)
        if settings.kommo_pipeline_id and pipeline_id != settings.kommo_pipeline_id:
            logger.info(
                "Lead %s ignorado: pipeline %s != %s",
                lead_id,
                pipeline_id,
                settings.kommo_pipeline_id,
            )
            return

        contact_id = first_contact_id(lead)
        contact = await client.get_contact(contact_id) if contact_id else {}
        status_id = int(lead.get("status_id") or 0)
        rule = settings.stage_map.get(status_id)

        existing_row = await fetchrow(
            "SELECT * FROM leads WHERE lead_id = $1", lead_id
        )
        existing = dict(existing_row) if existing_row else {}
        decision = decide(existing, rule)

        ref = extract_ref(job_payload.get("text"), lead, contact)
        click: dict[str, Any] | None = None
        if ref:
            click = await self._link_click(ref, lead_id)
        if click is None and settings.kommo_field_ref:
            field_ref = extract_ref(
                field_value(lead, field_id=settings.kommo_field_ref)
            )
            if field_ref:
                ref = field_ref
                click = await self._link_click(field_ref, lead_id)
        if click is None and settings.kommo_field_ttclid:
            ttclid = field_value(lead, field_id=settings.kommo_field_ttclid)
            if ttclid:
                click = {"ttclid": ttclid}

        consent = True
        if settings.kommo_field_consent:
            consent_value = field_value(
                lead, field_id=settings.kommo_field_consent
            )
            if consent_value and consent_value.strip().lower() in (
                "no",
                "false",
                "0",
                "negado",
                "rechazado",
            ):
                consent = False

        value = lead_value(lead)
        if settings.kommo_field_value:
            field_amount = field_value(
                lead, field_id=settings.kommo_field_value
            )
            if field_amount:
                try:
                    value = float(str(field_amount).replace(",", "."))
                except ValueError:
                    pass
        if value <= 0:
            value = float(settings.tiktok_default_value or 0)

        currency = settings.tiktok_currency or "COP"
        if settings.kommo_field_currency:
            field_currency = field_value(
                lead, field_id=settings.kommo_field_currency
            )
            if field_currency:
                currency = field_currency.strip().upper()

        lead_row = {
            "lead_id": lead_id,
            "contact_id": contact_id,
            "pipeline_id": pipeline_id,
            "status_id": status_id,
            "stage_key": rule.key if rule else None,
            "name": lead_name(lead) or contact_name(contact),
            "phone": contact_phone(contact),
            "email": contact_email(contact),
            "ref_code": ref,
            "ttclid": (click or {}).get("ttclid"),
            "ttp": (click or {}).get("ttp"),
            "attribution_source": (
                "bridge"
                if click and click.get("code")
                else ("kommo_field" if click else None)
            ),
            "utm_source": (click or {}).get("utm_source"),
            "utm_medium": (click or {}).get("utm_medium"),
            "utm_campaign": (click or {}).get("utm_campaign"),
            "utm_content": (click or {}).get("utm_content"),
            "utm_term": (click or {}).get("utm_term"),
            "value": value,
            "currency": currency,
            "consent": consent,
            "raw": lead,
        }
        await self._upsert_lead(lead_row)

        if not consent:
            logger.info("Lead %s sin consentimiento; no se envía a TikTok", lead_id)
            return
        if not decision.send:
            logger.debug(
                "Lead %s etapa %s sin evento: %s",
                lead_id,
                status_id,
                decision.reason,
            )
            return

        warning = event_name_warning(settings, decision.event_name or "")
        if warning:
            logger.warning(warning)

        # Clave estable: el mismo lead, etapa y evento nunca se envía dos veces.
        # Si el rank sube, cambia la etapa/evento y sí se envía el nuevo evento.
        dedupe_key = sha256_hex(
            f"{lead_id}|{status_id}|{decision.event_name}|{decision.rank}"
        )
        payload = build_payload(
            settings,
            event_name=decision.event_name or "",
            event_id=dedupe_key,
            lead_id=lead_id,
            lead=lead_row,
            click=click,
            stage_key=rule.key if rule else None,
            value=value,
            currency=currency,
        )
        inserted = await self._enqueue_outbox(
            dedupe_key=dedupe_key,
            lead_id=lead_id,
            event_name=decision.event_name or "",
            stage_key=rule.key if rule else None,
            payload=payload,
        )
        await execute(
            """
            UPDATE leads
               SET max_rank = GREATEST(max_rank, $2),
                   last_event_at = CASE WHEN $3 THEN now() ELSE last_event_at END,
                   stage_key = $4,
                   updated_at = now()
             WHERE lead_id = $1
            """,
            lead_id,
            decision.rank,
            inserted,
            rule.key if rule else None,
        )
        if inserted:
            logger.info(
                "Lead %s -> evento %s (rank %s) encolado",
                lead_id,
                decision.event_name,
                decision.rank,
            )
        else:
            logger.info(
                "Lead %s -> evento %s ya existía (dedupe)",
                lead_id,
                decision.event_name,
            )

    async def _link_click(self, ref: str, lead_id: int) -> dict[str, Any] | None:
        from .attribution import link_click_to_lead

        click = await link_click_to_lead(ref, lead_id)
        if click:
            return click
        logger.warning(
            "REF %s existe pero ya está vinculado a otro lead (lead actual %s)",
            ref,
            lead_id,
        )
        return None

    async def _upsert_lead(self, lead: dict[str, Any]) -> None:
        await execute(
            """
            INSERT INTO leads (
                lead_id, contact_id, pipeline_id, status_id, last_status_id,
                stage_key, name, phone, email, ref_code, ttclid, ttp,
                attribution_source, utm_source, utm_medium, utm_campaign,
                utm_content, utm_term, value, currency, consent, raw
            ) VALUES (
                $1, $2, $3, $4, $4,
                $5, $6, $7, $8, $9, $10, $11,
                $12, $13, $14, $15,
                $16, $17, $18, $19, $20, $21::jsonb
            )
            ON CONFLICT (lead_id) DO UPDATE SET
                contact_id = EXCLUDED.contact_id,
                pipeline_id = EXCLUDED.pipeline_id,
                status_id = EXCLUDED.status_id,
                last_status_id = EXCLUDED.last_status_id,
                stage_key = EXCLUDED.stage_key,
                name = COALESCE(EXCLUDED.name, leads.name),
                phone = COALESCE(EXCLUDED.phone, leads.phone),
                email = COALESCE(EXCLUDED.email, leads.email),
                ref_code = COALESCE(EXCLUDED.ref_code, leads.ref_code),
                ttclid = COALESCE(EXCLUDED.ttclid, leads.ttclid),
                ttp = COALESCE(EXCLUDED.ttp, leads.ttp),
                attribution_source = COALESCE(
                    EXCLUDED.attribution_source, leads.attribution_source
                ),
                utm_source = COALESCE(EXCLUDED.utm_source, leads.utm_source),
                utm_medium = COALESCE(EXCLUDED.utm_medium, leads.utm_medium),
                utm_campaign = COALESCE(EXCLUDED.utm_campaign, leads.utm_campaign),
                utm_content = COALESCE(EXCLUDED.utm_content, leads.utm_content),
                utm_term = COALESCE(EXCLUDED.utm_term, leads.utm_term),
                value = EXCLUDED.value,
                currency = EXCLUDED.currency,
                consent = EXCLUDED.consent,
                raw = EXCLUDED.raw,
                updated_at = now()
            """,
            lead["lead_id"],
            lead["contact_id"],
            lead["pipeline_id"],
            lead["status_id"],
            lead["stage_key"],
            lead["name"],
            lead["phone"],
            lead["email"],
            lead["ref_code"],
            lead["ttclid"],
            lead["ttp"],
            lead["attribution_source"],
            lead["utm_source"],
            lead["utm_medium"],
            lead["utm_campaign"],
            lead["utm_content"],
            lead["utm_term"],
            lead["value"],
            lead["currency"],
            lead["consent"],
            json.dumps(lead["raw"], ensure_ascii=False, default=str),
        )

    async def _enqueue_outbox(
        self,
        *,
        dedupe_key: str,
        lead_id: int,
        event_name: str,
        stage_key: str | None,
        payload: dict[str, Any],
    ) -> bool:
        row = await fetchrow(
            """
            INSERT INTO events_outbox (
                dedupe_key, event_id, lead_id, event_name, stage_key, payload
            ) VALUES ($1, $1, $2, $3, $4, $5::jsonb)
            ON CONFLICT (dedupe_key) DO NOTHING
            RETURNING id
            """,
            dedupe_key,
            lead_id,
            event_name,
            stage_key,
            json.dumps(payload, ensure_ascii=False, default=str),
        )
        return row is not None

    async def _process_outbox(self) -> int:
        settings = get_settings()
        processed = 0
        for event in await self._claim_outbox():
            payload = _load_json(event.get("payload"))
            result = await send_event(settings, payload)
            if result.ok:
                await execute(
                    """
                    UPDATE events_outbox
                       SET status = 'sent',
                           sent_at = now(),
                           locked_at = NULL,
                           response = $2::jsonb,
                           last_error = NULL
                     WHERE id = $1
                    """,
                    event["id"],
                    json.dumps(result.response, ensure_ascii=False, default=str),
                )
                processed += 1
                continue

            attempts = int(event.get("attempts") or 1)
            response_json = json.dumps(
                result.response, ensure_ascii=False, default=str
            )
            if result.retryable and attempts < settings.max_attempts:
                next_at = datetime.now(UTC) + timedelta(
                    seconds=_backoff_seconds(attempts)
                )
                await execute(
                    """
                    UPDATE events_outbox
                       SET status = 'failed',
                           next_attempt_at = $2,
                           locked_at = NULL,
                           response = $3::jsonb,
                           last_error = $4
                     WHERE id = $1
                    """,
                    event["id"],
                    next_at,
                    response_json,
                    result.message,
                )
            else:
                await execute(
                    """
                    UPDATE events_outbox
                       SET status = 'dead',
                           locked_at = NULL,
                           response = $2::jsonb,
                           last_error = $3
                     WHERE id = $1
                    """,
                    event["id"],
                    response_json,
                    result.message,
                )
        return processed
