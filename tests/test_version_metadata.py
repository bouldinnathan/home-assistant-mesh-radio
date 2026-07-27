"""Keep public release version metadata synchronized."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from custom_components.meshnet.const import VERSION

ROOT = Path(__file__).resolve().parents[1]
MINIMUM_HOME_ASSISTANT = "2025.1.4"


def test_release_version_metadata_is_consistent() -> None:
    manifest = json.loads(
        (ROOT / "custom_components/meshnet/manifest.json").read_text(
            encoding="utf-8"
        )
    )
    project = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    setup_script = (ROOT / "setup.sh").read_text(encoding="utf-8")

    assert manifest["version"] == VERSION
    assert project["project"]["version"] == VERSION
    assert f"MeshNet {VERSION} targets Home Assistant" in setup_script


def test_minimum_home_assistant_metadata_is_consistent() -> None:
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    requirements = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    setup_script = (ROOT / "setup.sh").read_text(encoding="utf-8")
    verifier = (ROOT / "verify_setup.sh").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/tests.yml").read_text(
        encoding="utf-8"
    )

    assert hacs["homeassistant"] == MINIMUM_HOME_ASSISTANT
    assert f"homeassistant=={MINIMUM_HOME_ASSISTANT}" in requirements
    assert MINIMUM_HOME_ASSISTANT in setup_script
    assert MINIMUM_HOME_ASSISTANT in verifier
    assert f"homeassistant=={MINIMUM_HOME_ASSISTANT}" in workflow
