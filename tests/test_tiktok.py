from app.config import Settings
from app.tiktok import (
    build_payload,
    build_user,
    event_name_warning,
    normalize_email,
    normalize_phone,
    sha256_hex,
)


def make_settings(**overrides):
    base = {
        "tiktok_event_source": "web",
        "tiktok_pixel_id": "PIXEL123",
        "tiktok_event_set_id": "EVENTSET123",
        "tiktok_access_token": "TOKEN",
        "tiktok_currency": "COP",
        "public_base_url": "https://track.example.com",
        "default_country_code": "57",
    }
    base.update(overrides)
    return Settings(**base)


def test_normalize_phone_and_email():
    assert normalize_phone("300 123 4567", "57") == "573001234567"
    assert normalize_phone("+57 300 123 4567", "57") == "573001234567"
    assert normalize_phone("", "57") == ""
    assert normalize_email("  Cliente@Example.COM ") == "cliente@example.com"


def test_build_user_hashes_pii_and_keeps_ttclid():
    settings = make_settings()
    lead = {
        "lead_id": 123,
        "email": "Cliente@Example.com",
        "phone": "3001234567",
        "ttclid": "click-1",
    }
    click = {"ttclid": "click-1", "ip": "1.2.3.4", "user_agent": "UA"}
    user = build_user(settings, lead, click)
    assert user["ttclid"] == "click-1"
    assert user["email"] == sha256_hex("cliente@example.com")
    assert user["phone"] == sha256_hex("573001234567")
    assert user["external_id"] == sha256_hex("kommo:123")
    assert user["ip"] == "1.2.3.4"
    assert user["user_agent"] == "UA"


def test_web_payload_uses_pixel_and_page_url():
    settings = make_settings(tiktok_test_event_code="TEST123")
    lead = {"lead_id": 123, "phone": "3001234567"}
    click = {"ttclid": "click-1", "landing_url": "https://track.example.com/r?ttclid=click-1"}
    payload = build_payload(
        settings,
        event_name="Contact",
        event_id="evt-1",
        lead_id=123,
        lead=lead,
        click=click,
        stage_key="nuevo",
        value=0,
    )
    assert payload["event_source"] == "web"
    assert payload["event_source_id"] == "PIXEL123"
    assert payload["test_event_code"] == "TEST123"
    assert payload["data"][0]["page"]["url"].startswith("https://track.example.com/r")
    assert payload["data"][0]["user"]["ttclid"] == "click-1"


def test_web_page_url_can_override_bridge_url():
    settings = make_settings(tiktok_web_page_url="https://mi-landing.com/promo")
    payload = build_payload(
        settings,
        event_name="Contact",
        event_id="evt-3",
        lead_id=123,
        lead={"lead_id": 123},
        click={"ttclid": "click-1", "landing_url": "https://track.example.com/r"},
    )
    assert payload["data"][0]["page"]["url"] == "https://mi-landing.com/promo"


def test_crm_payload_uses_event_set_and_lead_id():
    settings = make_settings(tiktok_event_source="crm")
    payload = build_payload(
        settings,
        event_name="LeadCalificado",
        event_id="evt-2",
        lead_id=555,
        lead={"lead_id": 555},
        click=None,
        stage_key="calificado",
        value=50000,
    )
    assert payload["event_source"] == "crm"
    assert payload["event_source_id"] == "EVENTSET123"
    assert payload["data"][0]["lead"]["lead_id"] == "555"
    assert "page" not in payload["data"][0]


def test_web_non_standard_event_warning():
    settings = make_settings()
    assert event_name_warning(settings, "LeadCalificado") is not None
    assert event_name_warning(settings, "Contact") is None
