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


class FakeSupabaseClient:
    def table(self, name):  # pragma: no cover - not needed for constructor assertions
        raise AssertionError(f"unexpected network-capable table access for {name}")


def _block_network(monkeypatch):
    """Fail the test if Supabase client construction attempts outbound I/O."""
    import httpx

    def fail_request(*args, **kwargs):
        raise AssertionError("Supabase compatibility test attempted outbound network I/O")

    monkeypatch.setattr(httpx.Client, "request", fail_request)
    monkeypatch.setattr(httpx.AsyncClient, "request", fail_request)


def _value_contains_key(value, key: str) -> bool:
    if value == key:
        return True
    if isinstance(value, str):
        return key in value
    if isinstance(value, dict):
        return any(_value_contains_key(item, key) for pair in value.items() for item in pair)
    if isinstance(value, (list, tuple, set)):
        return any(_value_contains_key(item, key) for item in value)
    if hasattr(value, "items"):
        return any(_value_contains_key(item, key) for pair in value.items() for item in pair)
    return False


def _client_contains_key(client, key: str) -> bool:
    """Detect retained fake key without printing or logging it."""
    candidates = [
        getattr(client, "supabase_key", None),
        getattr(client, "key", None),
        getattr(client, "api_key", None),
        getattr(client, "headers", None),
    ]
    options = getattr(client, "options", None)
    if options is not None:
        candidates.append(getattr(options, "headers", None))

    return any(_value_contains_key(candidate, key) for candidate in candidates)


def test_repository_accepts_modern_sb_secret_key_with_real_supabase_client_without_network(monkeypatch):
    _block_network(monkeypatch)

    settings = Settings(SUPABASE_URL=FAKE_URL, SUPABASE_SERVICE_ROLE_KEY=FAKE_MODERN_SECRET_KEY)
    try:
        repository = SupabaseRepository(settings)
    except Exception as exc:  # pragma: no cover - assertion message is the regression signal
        assert "Invalid API key" not in str(exc)
        raise

    assert _client_contains_key(repository.client, FAKE_MODERN_SECRET_KEY)


def test_repository_wiring_forwards_url_and_key_to_client_constructor(monkeypatch):
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
