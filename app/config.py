from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class StageRule(BaseModel):
    key: str = ""
    rank: int = 0
    event: str = ""
    enabled: bool = True


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "TikTok <-> Kommo Attribution"
    environment: str = "production"
    log_level: str = "INFO"

    public_base_url: str = "http://localhost:8000"
    database_url: str = "postgresql://attribution:attribution@db:5432/attribution"
    secret_key: str = ""
    secret_key_file: str = "/app/data/secret.key"

    # Kommo
    kommo_subdomain: str = ""
    kommo_token: str = ""
    kommo_pipeline_id: int | None = None
    kommo_webhook_secret: str = ""
    kommo_field_ttclid: int | None = None
    kommo_field_ref: int | None = None
    kommo_field_value: int | None = None
    kommo_field_currency: int | None = None
    kommo_field_consent: int | None = None
    kommo_stage_map: str = "{}"

    # TikTok
    tiktok_event_source: Literal["web", "crm", "offline"] = "web"
    tiktok_pixel_id: str = ""
    tiktok_event_set_id: str = ""
    tiktok_access_token: str = ""
    tiktok_test_event_code: str = ""
    tiktok_currency: str = "COP"
    tiktok_default_value: float = 0
    tiktok_web_page_url: str = ""

    # WhatsApp / bridge
    whatsapp_number: str = ""
    whatsapp_message_template: str = "Hola, quiero información. {ref}"
    default_country_code: str = "57"

    # Dashboard / security
    dashboard_user: str = "admin"
    dashboard_password: str = ""
    require_webhook_secret: bool = True
    store_raw_payloads: bool = True

    # Worker
    worker_enabled: bool = True
    worker_poll_seconds: float = 1.0
    lead_job_delay_seconds: float = 5.0
    max_attempts: int = 8

    @field_validator(
        "kommo_pipeline_id",
        "kommo_field_ttclid",
        "kommo_field_ref",
        "kommo_field_value",
        "kommo_field_currency",
        "kommo_field_consent",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return value

    @field_validator("kommo_subdomain", mode="before")
    @classmethod
    def _normalize_subdomain(cls, value: object) -> str:
        text = str(value or "").strip()
        text = re.sub(r"^https?://", "", text)
        text = text.replace(".kommo.com", "")
        return text.strip("/")

    @field_validator("public_base_url", mode="before")
    @classmethod
    def _normalize_base_url(cls, value: object) -> str:
        text = str(value or "").strip()
        return text.rstrip("/") or "http://localhost:8000"

    @property
    def kommo_base_url(self) -> str:
        return f"https://{self.kommo_subdomain}.kommo.com" if self.kommo_subdomain else ""

    @property
    def whatsapp_message(self) -> str:
        return self.whatsapp_message_template or "Hola, quiero información. {ref}"

    @property
    def stage_map(self) -> dict[int, StageRule]:
        try:
            raw = json.loads(self.kommo_stage_map or "{}")
        except (TypeError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        result: dict[int, StageRule] = {}
        for key, value in raw.items():
            try:
                result[int(key)] = StageRule(**(value or {}))
            except (TypeError, ValueError):
                continue
        return result

    def runtime_errors(self) -> list[str]:
        errors: list[str] = []
        if not self.kommo_subdomain or not self.kommo_token:
            errors.append("Kommo: falta KOMMO_SUBDOMAIN o KOMMO_TOKEN")
        if not self.kommo_webhook_secret and self.require_webhook_secret:
            errors.append("Kommo: falta KOMMO_WEBHOOK_SECRET")
        if not self.stage_map:
            errors.append("Kommo: KOMMO_STAGE_MAP está vacío o no es JSON válido")
        if self.tiktok_event_source == "web":
            if not self.tiktok_pixel_id:
                errors.append("TikTok: falta TIKTOK_PIXEL_ID para event_source=web")
        elif not self.tiktok_event_set_id:
            errors.append("TikTok: falta TIKTOK_EVENT_SET_ID para event_source=crm/offline")
        if not self.tiktok_access_token:
            errors.append("TikTok: falta TIKTOK_ACCESS_TOKEN")
        if not self.whatsapp_number:
            errors.append("WhatsApp: falta WHATSAPP_NUMBER")
        if not self.dashboard_password:
            errors.append("Panel: falta DASHBOARD_PASSWORD")
        return errors

    def public_url(self, path: str) -> str:
        return f"{self.public_base_url}/{path.lstrip('/')}"


@lru_cache(maxsize=1)
def get_base_settings() -> Settings:
    """Configuración de entorno (sin datos guardados en la base)."""
    return Settings()


_runtime_settings: Settings | None = None


def get_settings() -> Settings:
    """Configuración efectiva: entorno + panel /settings."""
    return _runtime_settings or get_base_settings()


def set_runtime_settings(settings: Settings | None) -> None:
    global _runtime_settings
    _runtime_settings = settings
