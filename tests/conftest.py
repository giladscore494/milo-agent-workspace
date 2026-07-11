"""Test harness defaults for isolated in-process API tests.

Production code fails closed unless gateway auth is configured. Most legacy
unit tests exercise bare internal identity headers, so they opt in explicitly
through this autouse fixture. Gateway-auth-specific tests manage the setting
manually to prove fail-closed behavior and production rejection.
"""

import pytest

_GATEWAY_AUTH_MODULES = {"tests.test_gateway_auth", "tests.test_corrective_blockers"}


@pytest.fixture(autouse=True)
def explicit_insecure_dev_identity_for_legacy_unit_tests(monkeypatch, request):
    if request.module.__name__ in _GATEWAY_AUTH_MODULES:
        return
    monkeypatch.setenv("MILO_ALLOW_INSECURE_DEV_IDENTITY", "true")
    monkeypatch.setenv("ENVIRONMENT", "test")
