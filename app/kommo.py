from __future__ import annotations

from typing import Any

import httpx

from .config import Settings


class KommoError(RuntimeError):
    pass


class KommoClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        if not self.settings.kommo_base_url or not self.settings.kommo_token:
            raise KommoError("Kommo sin KOMMO_SUBDOMAIN/KOMMO_TOKEN")
        url = f"{self.settings.kommo_base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.settings.kommo_token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.request(
                method, url, params=params, json=json_body, headers=headers
            )
        if response.status_code == 204:
            return None
        if response.status_code >= 400:
            raise KommoError(
                f"Kommo {response.status_code} en {path}: {response.text[:300]}"
            )
        if not response.content:
            return None
        return response.json()

    async def get_lead(self, lead_id: int) -> dict[str, Any]:
        data = await self._request(
            "GET", f"/api/v4/leads/{lead_id}", params={"with": "contacts"}
        )
        return data or {}

    async def get_contact(self, contact_id: int) -> dict[str, Any]:
        data = await self._request("GET", f"/api/v4/contacts/{contact_id}")
        return data or {}

    async def find_lead_by_contact(self, contact_id: int) -> int | None:
        data = await self._request(
            "GET", f"/api/v4/contacts/{contact_id}", params={"with": "leads"}
        ) or {}
        leads = ((data.get("_embedded") or {}).get("leads") or [])
        for lead in leads:
            if lead.get("id"):
                return int(lead["id"])
        return None

    async def list_pipelines(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/v4/leads/pipelines") or {}
        return ((data.get("_embedded") or {}).get("pipelines") or [])


def _entry_value(entry: dict[str, Any]) -> str | None:
    value = entry.get("value")
    if value not in (None, ""):
        return str(value)
    enum = entry.get("enum")
    if isinstance(enum, dict):
        enum_value = enum.get("value")
        if enum_value not in (None, ""):
            return str(enum_value)
    return None


def field_value(
    obj: dict[str, Any] | None,
    *,
    field_id: int | None = None,
    field_code: str | None = None,
    field_type: str | None = None,
) -> str | None:
    if not obj:
        return None
    wanted_code = (field_code or "").upper()
    for field in obj.get("custom_fields_values") or []:
        try:
            current_id = int(field.get("field_id"))
        except (TypeError, ValueError):
            current_id = None
        current_code = str(field.get("field_code") or "").upper()
        current_type = str(field.get("field_type") or "").lower()
        matches = (
            (field_id is not None and current_id == int(field_id))
            or (bool(wanted_code) and current_code == wanted_code)
            or (bool(field_type) and current_type == field_type.lower())
        )
        if not matches:
            continue
        for entry in field.get("values") or []:
            if isinstance(entry, dict):
                value = _entry_value(entry)
                if value:
                    return value
    return None


def first_contact_id(lead: dict[str, Any] | None) -> int | None:
    if not lead:
        return None
    contacts = ((lead.get("_embedded") or {}).get("contacts") or [])
    for contact in contacts:
        if contact.get("id"):
            return int(contact["id"])
    return None


def contact_phone(contact: dict[str, Any] | None) -> str | None:
    return field_value(contact, field_code="PHONE", field_type="phone")


def contact_email(contact: dict[str, Any] | None) -> str | None:
    return field_value(contact, field_code="EMAIL", field_type="email")


def contact_name(contact: dict[str, Any] | None) -> str | None:
    if not contact:
        return None
    name = contact.get("name")
    return str(name).strip() if name else None


def lead_name(lead: dict[str, Any] | None) -> str | None:
    if not lead:
        return None
    name = lead.get("name")
    return str(name).strip() if name else None


def lead_value(lead: dict[str, Any] | None) -> float:
    if not lead:
        return 0.0
    try:
        return float(lead.get("price") or 0)
    except (TypeError, ValueError):
        return 0.0
