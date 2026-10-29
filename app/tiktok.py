from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings

logger = logging.getLogger(__name__)

EVENTS_URL = "https://business-api.tiktok.com/open_api/v1.3/event/track/"

STANDARD_EVENTS = {
    "ViewContent",
    "AddToCart",
    "InitiateCheckout",
    "AddPaymentInfo",
    "AddToWishlist",
    "Search",
    "PlaceAnOrder",
    "CompleteRegistration",
    "Purchase",
    "Contact",
    "SubmitForm",
    "Subscribe",
    "StartTrial",
    "Download",
    "ClickButton",
    "Schedule",
}


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def normalize_phone(phone: str | None, default_country_code: str = "") -> str:
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return ""
    digits = digits.removeprefix("00")
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    country = re.sub(r"\D", "", default_country_code or "")
    if country and len(digits) == 10:
        digits = f"{country}{digits}"
    return digits


def build_user(
    settings: Settings,
    lead: dict[str, Any],
    click: dict[str, Any] | None,
) -> dict[str, str]:
    click = click or {}
    user: dict[str, str] = {}

    ttclid = click.get("ttclid") or lead.get("ttclid")
    ttp = click.get("ttp") or lead.get("ttp")
    if ttclid:
        user["ttclid"] = str(ttclid)
    if ttp:
        user["ttp"] = str(ttp)

    email = normalize_email(lead.get("email"))
    phone = normalize_phone(lead.get("phone"), settings.default_country_code)
    if email:
        user["email"] = sha256_hex(email)
    if phone:
        user["phone"] = sha256_hex(phone)
    if lead.get("lead_id"):
        user["external_id"] = sha256_hex(f"kommo:{lead['lead_id']}")

    if click.get("ip"):
        user["ip"] = str(click["ip"])
    if click.get("user_agent"):
        user["user_agent"] = str(click["user_agent"])
    return user


def build_payload(
    settings: Settings,
    *,
    event_name: str,
    event_id: str,
    lead_id: int,
    lead: dict[str, Any],
    click: dict[str, Any] | None = None,
    stage_key: str | None = None,
    value: float = 0,
    currency: str | None = None,
    event_time: int | None = None,
) -> dict[str, Any]:
    source = settings.tiktok_event_source
    source_id = (
        settings.tiktok_pixel_id
        if source == "web"
        else settings.tiktok_event_set_id
    )
    click = click or {}
    currency = currency or settings.tiktok_currency or "COP"

    data: dict[str, Any] = {
        "event": event_name,
        "event_time": int(event_time or time.time()),
        "event_id": event_id,
        "user": build_user(settings, lead, click),
        "properties": {
            "currency": currency,
            "value": round(float(value or 0), 2),
        },
    }
    if stage_key:
        data["properties"]["lead_stage"] = stage_key

    if source == "web":
        page_url = (
            settings.tiktok_web_page_url
            or click.get("landing_url")
            or settings.public_base_url
        )
        data["page"] = {
            "url": str(page_url),
            "referrer": str(click.get("referrer") or ""),
        }
    elif source == "crm":
        data["lead"] = {"lead_id": str(lead_id)}
    # offline no lleva page ni lead; solo user + properties.

    payload: dict[str, Any] = {
        "event_source": source,
        "event_source_id": source_id,
        "data": [data],
    }
    if settings.tiktok_test_event_code:
        payload["test_event_code"] = settings.tiktok_test_event_code
    return payload


def event_name_warning(settings: Settings, event_name: str) -> str | None:
    if settings.tiktok_event_source == "web" and event_name not in STANDARD_EVENTS:
        return (
            f'"{event_name}" no es un evento estándar de TikTok; llegará como '
            "personalizado y no servirá como objetivo de optimización web."
        )
    if settings.tiktok_event_source == "offline" and event_name not in STANDARD_EVENTS:
        return f'"{event_name}" no es estándar; offline solo acepta eventos estándar.'
    return None


@dataclass
class TikTokResult:
    ok: bool
    retryable: bool
    code: int | None = None
    message: str = ""
    response: dict[str, Any] = field(default_factory=dict)


async def send_event(settings: Settings, payload: dict[str, Any]) -> TikTokResult:
    token = settings.tiktok_access_token
    if not token:
        return TikTokResult(False, False, None, "sin TIKTOK_ACCESS_TOKEN")
    source_id = payload.get("event_source_id")
    if not source_id:
        return TikTokResult(False, False, None, "event_source_id vacío")

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                EVENTS_URL,
                headers={
                    "Access-Token": token,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.HTTPError as exc:
        return TikTokResult(False, True, None, f"red: {exc}")

    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:1000]}
    if not isinstance(body, dict):
        body = {"raw": str(body)[:1000]}

    code = body.get("code")
    try:
        code_int = int(code) if code is not None else None
    except (TypeError, ValueError):
        code_int = None
    message = str(body.get("message") or body.get("msg") or "")

    if response.status_code == 200 and code_int == 0:
        return TikTokResult(True, False, 0, message or "OK", body)

    retryable = (
        response.status_code == 429
        or response.status_code >= 500
        or code_int == 40100  # rate limit
        or response.status_code == 0
    )
    detail = f"HTTP {response.status_code} code={code_int}: {message[:300]}"
    logger.warning("TikTok rechazó el evento: %s", detail)
    return TikTokResult(False, retryable, code_int, detail, body)
