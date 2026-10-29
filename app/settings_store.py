from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from pydantic import ValidationError

from .config import (
    Settings,
    get_base_settings,
    get_settings,
    set_runtime_settings,
)
from .db import fetch, get_pool

logger = logging.getLogger(__name__)

SECRET_FIELDS = (
    "kommo_token",
    "kommo_webhook_secret",
    "tiktok_access_token",
)

EDITABLE_FIELDS = (
    "kommo_subdomain",
    "kommo_token",
    "kommo_webhook_secret",
    "kommo_pipeline_id",
    "kommo_field_ttclid",
    "kommo_field_ref",
    "kommo_field_value",
    "kommo_field_currency",
    "kommo_field_consent",
    "kommo_stage_map",
    "tiktok_event_source",
    "tiktok_pixel_id",
    "tiktok_event_set_id",
    "tiktok_access_token",
    "tiktok_test_event_code",
    "tiktok_currency",
    "tiktok_default_value",
    "tiktok_web_page_url",
    "whatsapp_number",
    "whatsapp_message_template",
    "default_country_code",
    "lead_job_delay_seconds",
    "max_attempts",
)

_ENC_PREFIX = "enc:"
_fernet: Fernet | None = None


def _build_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet

    base = get_base_settings()
    raw = (base.secret_key or "").strip()
    if not raw:
        path = Path(base.secret_key_file)
        try:
            if path.exists():
                raw = path.read_text(encoding="ascii").strip()
            else:
                raw = Fernet.generate_key().decode()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(raw, encoding="ascii")
                os.chmod(path, 0o600)
        except OSError as exc:
            logger.warning(
                "No se pudo usar %s (%s); se usará una clave efímera. "
                "Configura SECRET_KEY para persistir los secretos.",
                path,
                exc,
            )
            raw = "clave-efimera-solo-desarrollo"

    try:
        _fernet = Fernet(raw.encode("ascii"))
    except (ValueError, TypeError):
        digest = hashlib.sha256(raw.encode("utf-8")).digest()
        _fernet = Fernet(base64.urlsafe_b64encode(digest))
    return _fernet


def reset_fernet_cache() -> None:
    global _fernet
    _fernet = None


def encrypt_secret(value: str) -> str:
    return _ENC_PREFIX + _build_fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    if not value.startswith(_ENC_PREFIX):
        return value
    try:
        return _build_fernet().decrypt(value[len(_ENC_PREFIX) :].encode("ascii")).decode(
            "utf-8"
        )
    except (InvalidToken, ValueError):
        logger.error(
            "No se pudo descifrar un secreto. ¿Cambió SECRET_KEY? "
            "Vuelve a guardarlo en /settings."
        )
        return ""


def secret_status(settings: Settings | None = None) -> dict[str, bool]:
    current = settings or get_settings()
    return {field: bool(getattr(current, field, "")) for field in SECRET_FIELDS}


def _fill_numeric_defaults(data: dict[str, Any]) -> None:
    if str(data.get("tiktok_default_value", "")).strip() == "":
        data["tiktok_default_value"] = 0
    if str(data.get("lead_job_delay_seconds", "")).strip() == "":
        data["lead_job_delay_seconds"] = 5.0
    if str(data.get("max_attempts", "")).strip() == "":
        data["max_attempts"] = 8


def _settings_from(data: dict[str, Any]) -> Settings:
    _fill_numeric_defaults(data)
    return Settings.model_validate(data)


async def load_overrides() -> dict[str, str]:
    rows = await fetch("SELECT key, value FROM app_settings")
    overrides: dict[str, str] = {}
    for row in rows:
        key = str(row["key"])
        if key not in EDITABLE_FIELDS:
            continue
        value = str(row["value"] or "")
        if key in SECRET_FIELDS:
            value = decrypt_secret(value)
        overrides[key] = value
    return overrides


async def reload_settings() -> Settings:
    """Recarga la configuración efectiva desde la base."""
    base = get_base_settings()
    try:
        overrides = await load_overrides()
        data = base.model_dump()
        data.update(overrides)
        settings = _settings_from(data)
    except Exception as exc:  # noqa: BLE001
        logger.error("Configuración de la base inválida; usando entorno: %s", exc)
        set_runtime_settings(base)
        return base
    set_runtime_settings(settings)
    return settings


def _format_validation_error(exc: ValidationError) -> list[str]:
    errors: list[str] = []
    for item in exc.errors():
        location = ".".join(str(part) for part in item.get("loc", ()))
        message = str(item.get("msg") or "valor inválido")
        errors.append(f"{location}: {message}")
    return errors


async def save_settings(form: Mapping[str, Any]) -> list[str]:
    """Guarda solo los campos enviados. Devuelve errores legibles."""
    base = get_base_settings()
    changes: dict[str, str] = {}

    for field in EDITABLE_FIELDS:
        if field in SECRET_FIELDS:
            clear = str(form.get(f"clear_{field}") or "").strip().lower()
            if clear in ("1", "true", "on", "yes"):
                changes[field] = ""
                continue
            if field not in form:
                continue
            value = str(form.get(field) or "").strip()
            if value:
                changes[field] = value
            continue

        if field not in form:
            continue
        changes[field] = str(form.get(field) or "").strip()

    if "kommo_stage_map" in changes:
        raw = changes["kommo_stage_map"] or "{}"
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            return [f"KOMMO_STAGE_MAP no es JSON válido: {exc}"]
        if not isinstance(parsed, dict):
            return ["KOMMO_STAGE_MAP debe ser un objeto JSON, por ejemplo {}"]
        changes["kommo_stage_map"] = raw

    data = base.model_dump()
    data.update(changes)
    try:
        _settings_from(data)
    except ValidationError as exc:
        return _format_validation_error(exc)

    if not changes:
        return []

    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        for field, value in changes.items():
            stored = (
                encrypt_secret(value) if field in SECRET_FIELDS else value
            )
            await conn.execute(
                """
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES ($1, $2, now())
                    ON CONFLICT (key) DO UPDATE SET
                        value = EXCLUDED.value,
                        updated_at = now()
                    """,
                field,
                stored,
            )

    await reload_settings()
    return []
