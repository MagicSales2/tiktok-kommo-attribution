from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

LEAD_RE = re.compile(r"^leads\[([a-z_]+)\]\[(\d+)\]\[(.+?)\]$")
MESSAGE_RE = re.compile(r"^message\[([a-z_]+)\]\[(\d+)\]\[(.+?)\]$")


@dataclass
class KommoSignal:
    kind: str  # "lead" | "message"
    event_type: str
    lead_id: int | None = None
    status_id: int | None = None
    contact_id: int | None = None
    text: str = ""
    incoming: bool = False
    payload: dict[str, Any] = field(default_factory=dict)


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _is_incoming(item: dict[str, Any]) -> bool:
    element_type = str(item.get("element_type", "")).strip()
    if element_type == "2":
        return True
    if element_type == "1":
        return False
    message_type = str(item.get("type", "")).lower()
    if message_type in ("outgoing", "out", "sent"):
        return False
    sender = item.get("sender")
    if isinstance(sender, dict):
        sender_type = str(sender.get("type", "")).lower()
        if sender_type in ("user", "bot", "robot", "system", "operator"):
            return False
        if sender_type in ("contact", "customer"):
            return True
    return True


def _message_signal(item: dict[str, Any], event_type: str) -> KommoSignal:
    entity_type = str(item.get("entity_type", "")).lower()
    lead_id = _as_int(item.get("lead_id"))
    if lead_id is None and entity_type in ("", "lead", "leads", "2"):
        lead_id = _as_int(item.get("entity_id"))
    contact_id = _as_int(item.get("contact_id"))
    if contact_id is None and entity_type in ("contact", "contacts"):
        contact_id = _as_int(item.get("entity_id"))
    text = ""
    for key in ("text", "body", "content"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            break
    return KommoSignal(
        kind="message",
        event_type=event_type,
        lead_id=lead_id,
        contact_id=contact_id,
        text=text,
        incoming=_is_incoming(item),
        payload=dict(item),
    )


def _lead_signal(item: dict[str, Any], event_type: str) -> KommoSignal:
    lead_id = _as_int(item.get("id")) or _as_int(item.get("lead_id"))
    return KommoSignal(
        kind="lead",
        event_type=event_type,
        lead_id=lead_id,
        status_id=_as_int(item.get("status_id")),
        payload=dict(item),
    )


def _flatten_form(body: bytes) -> dict[str, str]:
    parsed = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def _signals_from_flat(data: dict[str, str]) -> list[KommoSignal]:
    leads: dict[tuple[str, str], dict[str, str]] = {}
    messages: dict[tuple[str, str], dict[str, str]] = {}

    for key, value in data.items():
        lead_match = LEAD_RE.match(key)
        if lead_match:
            event_type, index, field_name = lead_match.groups()
            leads.setdefault((event_type, index), {})[field_name] = value
            continue
        message_match = MESSAGE_RE.match(key)
        if message_match:
            event_type, index, field_name = message_match.groups()
            messages.setdefault((event_type, index), {})[field_name] = value

    signals: list[KommoSignal] = []
    for (event_type, _index), item in leads.items():
        signal = _lead_signal(item, event_type)
        if signal.lead_id:
            signals.append(signal)
    for (event_type, _index), item in messages.items():
        signals.append(_message_signal(item, event_type))

    if not signals and data.get("lead_id"):
        signal = _lead_signal(data, str(data.get("event_type") or "manual"))
        if signal.lead_id:
            signals.append(signal)
    return signals


def _signals_from_json(data: Any) -> list[KommoSignal]:
    signals: list[KommoSignal] = []

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                signals.append(_message_signal(item, "add"))
        return signals

    if not isinstance(data, dict):
        return signals

    leads = data.get("leads")
    if isinstance(leads, dict):
        for event_type, items in leads.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    signal = _lead_signal(item, str(event_type))
                    if signal.lead_id:
                        signals.append(signal)

    messages = data.get("message")
    if isinstance(messages, dict):
        for event_type, items in messages.items():
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        signals.append(_message_signal(item, str(event_type)))

    # Formato raíz {"add": [...]} que usa Kommo para mensajes.
    root_add = data.get("add")
    if isinstance(root_add, list):
        for item in root_add:
            if isinstance(item, dict):
                signals.append(_message_signal(item, "add"))

    if not signals and data.get("lead_id"):
        signal = _lead_signal(data, str(data.get("event_type") or "manual"))
        if signal.lead_id:
            signals.append(signal)

    return signals


def _dedupe(signals: list[KommoSignal]) -> list[KommoSignal]:
    seen: set[tuple[Any, ...]] = set()
    result: list[KommoSignal] = []
    for signal in signals:
        key = (
            signal.kind,
            signal.event_type,
            signal.lead_id,
            signal.status_id,
            signal.contact_id,
            signal.text[:200],
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(signal)
    return result


def parse_kommo_payload(body: bytes, content_type: str = "") -> list[KommoSignal]:
    if not body:
        return []
    content_type = (content_type or "").lower()
    stripped = body.lstrip()
    if "application/json" in content_type or stripped.startswith((b"{", b"[")):
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return []
        return _dedupe(_signals_from_json(data))
    return _dedupe(_signals_from_flat(_flatten_form(body)))
