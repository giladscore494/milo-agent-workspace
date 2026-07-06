import pytest
from pydantic import ValidationError
from backend.config import Settings


def test_config_validation_accepts_required_env_names(monkeypatch):
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    settings = Settings(
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="sb_secret_test_placeholder",
    )
    assert str(settings.supabase_url) == "https://example.supabase.co/"
    assert settings.supabase_service_role_key == "sb_secret_test_placeholder"


def test_config_validation_rejects_missing_required_values(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    with pytest.raises(ValidationError):
        Settings()
