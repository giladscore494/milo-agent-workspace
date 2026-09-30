"""Test harness defaults for isolated in-process API tests.

Production code fails closed unless gateway auth is configured. Most legacy
unit tests exercise bare internal identity headers, so they opt in explicitly
through this autouse fixture. Gateway-auth-specific tests manage the setting
manually to prove fail-closed behavior and production rejection.

The same is true of the RELEASE the runtime serves. Console 6 binds a release
into every executable run identity and refuses to create one when no release
is stated -- an absent `MILO_RELEASE_SHA` is a refusal, never a wildcard. A
test process states one for the same reason a deployment does: so the runtime
it is exercising HAS an identity. Stating it here does not soften the gate;
the tests that prove the gate (`test_run_identity_authority`,
`test_release_inventory`) set or clear the variable themselves, and a
monkeypatched value always wins over this default.
"""

import pytest

from backend.catalog.government import source as government_source

_GATEWAY_AUTH_MODULES = {"tests.test_gateway_auth", "tests.test_corrective_blockers"}

#: The one release the offline test runtime claims to be serving. A full
#: 40-character hex SHA, because anything else is "no release stated".
TEST_RELEASE_SHA = "84cd8696119c24662a954d0f0e23195268dab23f"


@pytest.fixture(autouse=True)
def explicit_insecure_dev_identity_for_legacy_unit_tests(monkeypatch, request):
    monkeypatch.setenv("MILO_RELEASE_SHA", TEST_RELEASE_SHA)
    if request.module.__name__ in _GATEWAY_AUTH_MODULES:
        return
    monkeypatch.setenv("MILO_ALLOW_INSECURE_DEV_IDENTITY", "true")
    monkeypatch.setenv("ENVIRONMENT", "test")


@pytest.fixture(autouse=True)
def unpaced_government_client_by_default(monkeypatch):
    """P51: every DataGovClient paces its sends (`MIN_REQUEST_INTERVAL_SECONDS`,
    1 s in production) on the real clock. Offline, a fixture transport answers
    at once, so the default pace is 0 here; the pacing tests construct their
    clients with an explicit interval and a fake clock, and pin the production
    constant from the source itself."""
    monkeypatch.setattr(government_source, "MIN_REQUEST_INTERVAL_SECONDS", 0.0)
    monkeypatch.delenv(government_source.MIN_REQUEST_INTERVAL_ENV, raising=False)
