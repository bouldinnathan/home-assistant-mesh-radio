"""Keep public release version metadata synchronized."""

from __future__ import annotations

import json
from pathlib import Path
import tomllib

from custom_components.meshnet.const import VERSION


ROOT = Path(__file__).resolve().parents[1]


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
