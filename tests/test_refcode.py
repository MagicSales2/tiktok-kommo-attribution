from app.refcode import extract_ref, generate_ref, normalize_ref

REF_RE = r"^REF-[A-Z2-9]{8}$"


def test_generate_ref_format():
    import re

    ref = generate_ref()
    assert re.match(REF_RE, ref)


def test_normalize_ref_lowercase():
    assert normalize_ref("hola ref-abcd2345") == "REF-ABCD2345"
    assert normalize_ref("sin codigo") is None


def test_extract_ref_from_nested_objects():
    lead = {
        "name": "Cliente",
        "custom_fields_values": [
            {"field_id": 1, "values": [{"value": "Mensaje: REF-ABCD2345"}]}
        ],
    }
    assert extract_ref("nada", lead) == "REF-ABCD2345"
