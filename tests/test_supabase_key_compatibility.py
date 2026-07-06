"""Supabase key compatibility regression tests.

All keys in this module are fake placeholders. These tests must not read real
secrets, print environment values, or perform network I/O.
"""

import pytest
from pydantic import ValidationError

from backend.config import Settings
from backend.repository import supabase as supabase_repository
from backend.repository.supabase import SupabaseRepository

FAKE_URL = "https://example.supabase.co"
FAKE_MODERN_SECRET_KEY = "sb_secret_test_placeholder"
FAKE_LEGACY_JWT_SHAPED_KEY = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.fake_signature"


class FakeSupabaseClient:
    def table(self, name):  # pragma: no cover - not needed for constructor assertions
        raise AssertionError(f"unexpected network-capable table access for {name}")


def test_repository_accepts_modern_sb_secret_key_without_client_side_format_rejection(monkeypatch):
    calls = []

    def fake_create_client(url, key):
        calls.append((url, key))
        return FakeSupabaseClient()

    monkeypatch.setattr(supabase_repository, "create_client", fake_create_client)

    settings = Settings(SUPABASE_URL=FAKE_URL, SUPABASE_SERVICE_ROLE_KEY=FAKE_MODERN_SECRET_KEY)
    repository = SupabaseRepository(settings)

    assert isinstance(repository.client, FakeSupabaseClient)
    assert calls == [("https://example.supabase.co/", FAKE_MODERN_SECRET_KEY)]


def test_settings_prefers_supabase_secret_key_and_keeps_service_role_alias(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", FAKE_URL)
    monkeypatch.setenv("SUPABASE_SECRET_KEY", FAKE_MODERN_SECRET_KEY)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "backward_compatible_placeholder")

    settings = Settings()

    assert settings.supabase_service_role_key == FAKE_MODERN_SECRET_KEY


def test_settings_accepts_existing_cloud_run_service_role_mapping(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", FAKE_URL)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", FAKE_MODERN_SECRET_KEY)

    settings = Settings()

    assert settings.supabase_service_role_key == FAKE_MODERN_SECRET_KEY


def test_legacy_jwt_shaped_key_remains_configuration_compatible_when_supabase_client_supports_it(monkeypatch):
    calls = []

    def fake_create_client(url, key):
        calls.append((url, key))
        return FakeSupabaseClient()

    monkeypatch.setattr(supabase_repository, "create_client", fake_create_client)

    settings = Settings(SUPABASE_URL=FAKE_URL, SUPABASE_SERVICE_ROLE_KEY=FAKE_LEGACY_JWT_SHAPED_KEY)
    SupabaseRepository(settings)

    assert calls == [("https://example.supabase.co/", FAKE_LEGACY_JWT_SHAPED_KEY)]


@pytest.mark.parametrize("env_name", ["SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY"])
def test_empty_supabase_key_configuration_fails_safely(monkeypatch, env_name):
    monkeypatch.setenv("SUPABASE_URL", FAKE_URL)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.setenv(env_name, "")

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    message = str(exc_info.value)
    assert "String should have at least 1 character" in message
    assert FAKE_MODERN_SECRET_KEY not in message
    assert FAKE_LEGACY_JWT_SHAPED_KEY not in message
