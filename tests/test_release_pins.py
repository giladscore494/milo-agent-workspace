"""The permanent release pins in scripts/release/pins/ (cleanup D8).

``PINNED_POLICY_FINGERPRINT`` (policy_envelope.py), ``REQUIRED_RPC_ARGS``,
semantic_acceptance.py, verify_caps.py and verify_images.py were copied here
from the Stage D toolkit. Their tests now run from this directory. The Stage D
step scripts keep calling their own originals, and ``probe_db.py`` keeps its own
``REQUIRED_RPC_ARGS``, because the reviewed probe source is pinned by sha256.

Until the Stage D toolkit is deleted, the originals and the copies must stay
byte-identical. This module is where a drift is caught, and it goes away
with the toolkit.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PINS = REPO / "scripts" / "release" / "pins"
STAGE_D = REPO / "scripts" / "release" / "stage-d"

COPIED = ("policy_envelope.py", "semantic_acceptance.py", "verify_caps.py", "verify_images.py")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.mark.parametrize("name", COPIED)
def test_the_permanent_copy_is_byte_identical_to_the_stage_d_original(name):
    assert (PINS / name).read_bytes() == (STAGE_D / name).read_bytes(), (
        f"scripts/release/pins/{name} and scripts/release/stage-d/{name} diverged; "
        "until the Stage D toolkit is deleted a change must be made to both")


def test_the_pinned_rpc_inventory_is_the_probes_literal_value_for_value():
    pinned = _load(PINS / "required_rpc_args.py", "pins_required_rpc_args_copy_check")
    probe = _load(STAGE_D / "probe_db.py", "stage_d_probe_db_copy_check")
    assert pinned.REQUIRED_RPC_ARGS == probe.REQUIRED_RPC_ARGS


def test_the_pinned_rpc_literal_is_the_probes_literal_text_for_text():
    """Byte-identical values: the dict literal is the same text, not merely equal."""
    def literal(path: Path) -> str:
        text = path.read_text()
        start = text.index("REQUIRED_RPC_ARGS: dict[str, set[str]] = {")
        return text[start:text.index("\n}\n", start) + 3]
    assert literal(PINS / "required_rpc_args.py") == literal(STAGE_D / "probe_db.py")


def test_the_fingerprint_pin_did_not_move_value():
    envelope = _load(PINS / "policy_envelope.py", "pins_policy_envelope_value_check")
    assert envelope.PINNED_POLICY_FINGERPRINT == (
        "8f4ef66c58878468feeb9309eaa540dbaee7fb01f2425d2f65dca66df8476de1")


def test_the_in_process_policy_envelope_is_the_permanent_copy():
    """Tests import ``policy_envelope`` by name; it must resolve to pins/."""
    sys.path.insert(0, str(PINS))
    import policy_envelope

    assert Path(policy_envelope.__file__).resolve().parent == PINS.resolve()
