import pytest

from app import settings_store
from app.config import Settings, get_base_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    yield
    get_base_settings.cache_clear()
    settings_store.reset_fernet_cache()


def test_secret_roundtrip_and_not_plaintext(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "clave-de-prueba")
    get_base_settings.cache_clear()
    settings_store.reset_fernet_cache()

    token = settings_store.encrypt_secret("token-super-secreto")
    assert token.startswith("enc:")
    assert "token-super-secreto" not in token
    assert settings_store.decrypt_secret(token) == "token-super-secreto"


def test_plaintext_values_are_compatible():
    assert settings_store.decrypt_secret("token-viejo") == "token-viejo"


def test_secret_status_marks_configured_fields():
    settings = Settings(
        kommo_token="a",
        kommo_webhook_secret="",
        tiktok_access_token="c",
    )
    status = settings_store.secret_status(settings)
    assert status == {
        "kommo_token": True,
        "kommo_webhook_secret": False,
        "tiktok_access_token": True,
    }
