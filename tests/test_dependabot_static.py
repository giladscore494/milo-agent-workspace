"""PR-HYG: the Dependabot configuration (PR-OBS OBS-4), held statically.

Weekly, grouped minor/patch PRs per ecosystem, at most 3 open, nothing
auto-merged; and for GitHub Actions every semver-major update is ignored --
a major action upgrade is made by hand.
"""

from __future__ import annotations

from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parents[1] / ".github" / "dependabot.yml"


def _updates() -> dict[str, dict]:
    doc = yaml.safe_load(CONFIG.read_text())
    assert doc["version"] == 2
    return {entry["package-ecosystem"]: entry for entry in doc["updates"]}


def test_every_ecosystem_is_weekly_grouped_and_bounded():
    updates = _updates()
    assert set(updates) == {"pip", "npm", "github-actions"}
    for entry in updates.values():
        assert entry["schedule"]["interval"] == "weekly"
        assert entry["open-pull-requests-limit"] == 3
        (group,) = entry["groups"].values()
        assert group["update-types"] == ["minor", "patch"]


def test_github_actions_ignores_every_semver_major_update():
    actions = _updates()["github-actions"]
    assert actions["ignore"] == [{"dependency-name": "*",
                                  "update-types": ["version-update:semver-major"]}]


def test_only_github_actions_ignores_majors():
    updates = _updates()
    assert "ignore" not in updates["pip"] and "ignore" not in updates["npm"]
