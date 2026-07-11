"""Test harness defaults for isolated in-process API tests.

Production code fails closed unless gateway auth is configured. Unit tests that
exercise bare internal identity headers opt in explicitly here; gateway-auth
specific tests override/delete this variable to prove fail-closed behavior.
"""

import os

os.environ.setdefault("MILO_ALLOW_INSECURE_DEV_IDENTITY", "true")
os.environ.setdefault("ENVIRONMENT", "test")
