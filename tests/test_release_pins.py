"""The permanent release pins in scripts/release/pins/ (cleanup D8).

``PINNED_POLICY_FINGERPRINT`` (policy_envelope.py), ``REQUIRED_RPC_ARGS``,
semantic_acceptance.py, verify_caps.py and verify_images.py came here from the
deleted Stage D toolkit. The pinned values are unchanged; their tests run from
this directory.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PINS = REPO / "scripts" / "release" / "pins"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_the_fingerprint_pin_did_not_move_value():
    envelope = _load(PINS / "policy_envelope.py", "pins_policy_envelope_value_check")
    assert envelope.PINNED_POLICY_FINGERPRINT == (
        "8f4ef66c58878468feeb9309eaa540dbaee7fb01f2425d2f65dca66df8476de1")


def test_the_in_process_policy_envelope_is_the_permanent_copy():
    """Tests import ``policy_envelope`` by name; it must resolve to pins/."""
    sys.path.insert(0, str(PINS))
    import policy_envelope

    assert Path(policy_envelope.__file__).resolve().parent == PINS.resolve()


def test_no_test_puts_the_stage_d_directory_on_the_import_path():
    """Order-independent half of the check above: a bare ``import
    policy_envelope`` can only resolve to the Stage D copy if some test puts
    that directory on ``sys.path``, and none does."""
    offenders = [path.name for path in (REPO / "tests").glob("test_*.py")
                 if re.search(r"sys\.path\.(insert|append)\([^)]*(stage-d|stage_d)", path.read_text(),
                              re.IGNORECASE)]
    assert offenders == []
