import pytest
from pydantic import ValidationError
from backend.config import Settings


def test_config_validation_accepts_required_env_names():
    settings = Settings(SUPABASE_URL="https://example.supabase.co", SUPABASE_SERVICE_ROLE_KEY="secret")
    assert str(settings.supabase_url) == "https://example.supabase.co/"
    assert settings.supabase_service_role_key == "secret"


def test_config_validation_rejects_missing_required_values():
    with pytest.raises(ValidationError):
        Settings()
