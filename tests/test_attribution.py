from app.attribution import build_whatsapp_url


def test_whatsapp_url_keeps_template_and_ref():
    url = build_whatsapp_url(
        "+57 300 123 4567", "Hola, quiero información. {ref}", "REF-ABCD2345"
    )
    assert url.startswith("https://wa.me/573001234567?text=")
    assert "REF-ABCD2345" in url
    assert url.count("REF-ABCD2345") == 1


def test_whatsapp_url_appends_ref_if_missing():
    url = build_whatsapp_url("573001234567", "Hola fijo", "REF-ABCD2345")
    assert "REF-ABCD2345" in url
