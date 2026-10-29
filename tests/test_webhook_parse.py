from app.webhook_parse import parse_kommo_payload


def test_json_lead_status():
    body = b'{"leads":{"status":[{"id":123,"status_id":456,"pipeline_id":1}]}}'
    signals = parse_kommo_payload(body, "application/json")
    assert len(signals) == 1
    assert signals[0].kind == "lead"
    assert signals[0].lead_id == 123
    assert signals[0].status_id == 456


def test_form_lead_add():
    body = b"leads%5Badd%5D%5B0%5D%5Bid%5D=123&leads%5Badd%5D%5B0%5D%5Bstatus_id%5D=1"
    signals = parse_kommo_payload(body, "application/x-www-form-urlencoded")
    assert len(signals) == 1
    assert signals[0].lead_id == 123
    assert signals[0].event_type == "add"


def test_form_incoming_message():
    body = (
        b"message%5Badd%5D%5B0%5D%5Bid%5D=abc"
        b"&message%5Badd%5D%5B0%5D%5Blead_id%5D=123"
        b"&message%5Badd%5D%5B0%5D%5Btext%5D=Hola+REF-ABCD2345"
        b"&message%5Badd%5D%5B0%5D%5Belement_type%5D=2"
    )
    signals = parse_kommo_payload(body, "application/x-www-form-urlencoded")
    assert len(signals) == 1
    assert signals[0].kind == "message"
    assert signals[0].lead_id == 123
    assert signals[0].incoming is True
    assert "REF-ABCD2345" in signals[0].text


def test_outgoing_message_is_not_incoming():
    body = b'{"add":[{"text":"hola","lead_id":123,"element_type":"1"}]}'
    signals = parse_kommo_payload(body, "application/json")
    assert len(signals) == 1
    assert signals[0].incoming is False


def test_root_json_message():
    body = b'{"add":[{"text":"Hola REF-ABCD2345","lead_id":"123","element_type":"2"}]}'
    signals = parse_kommo_payload(body, "application/json")
    assert len(signals) == 1
    assert signals[0].lead_id == 123
    assert signals[0].text == "Hola REF-ABCD2345"


def test_message_contact_entity_fills_contact_id():
    body = b'{"add":[{"text":"Hola","entity_type":"contact","entity_id":77,"element_type":"2"}]}'
    signals = parse_kommo_payload(body, "application/json")
    assert len(signals) == 1
    assert signals[0].lead_id is None
    assert signals[0].contact_id == 77


def test_empty_payload():
    assert parse_kommo_payload(b"", "application/json") == []
