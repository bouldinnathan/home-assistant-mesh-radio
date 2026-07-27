from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(script: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(REPO_ROOT / script), *args],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _installed_config(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    component_dir = config_dir / "custom_components" / "meshnet"
    component_dir.mkdir(parents=True)
    (component_dir / "manifest.json").write_text(
        '{"domain": "meshnet"}\n', encoding="utf-8"
    )
    (config_dir / "configuration.yaml").write_text("# test configuration\n", encoding="utf-8")
    return config_dir


def _uninstall_metadata(
    path: Path,
    *,
    config_dir: Path,
    component_path: Path,
    backup_path: Path | None = None,
) -> None:
    path.write_text(
        json.dumps(
            {
                "config_dir": str(config_dir),
                "custom_component_path": str(component_path),
                "custom_component_installed_by_setup": True,
                "backup_path": str(backup_path or ""),
            }
        ),
        encoding="utf-8",
    )


def _fake_command(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    command = bin_dir / name
    command.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    command.chmod(0o755)


def test_env_example_has_no_active_fake_gateway_endpoints() -> None:
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    for variable in ("WIFI_MESHTASTIC", "USB_MESHTASTIC", "WIFI_MESHCORE", "USB_MESHCORE"):
        assert re.search(rf"^{variable}=$", env_example, re.MULTILINE)


def test_install_env_values_are_not_executed_as_shell(tmp_path: Path) -> None:
    marker = tmp_path / "should-not-exist"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DRY_RUN=1\n"
        "YES=1\n"
        f"HA_CONFIG_DIR={config_dir}\n"
        f"MESHNET_OUTPUT_DIR={tmp_path / 'output'}\n"
        f"UNKNOWN_VALUE=$(touch {marker})\n",
        encoding="utf-8",
    )
    env = {**os.environ, "ENV_FILE": str(env_file)}

    result = _run("install.sh", env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Ignoring unknown .env key" in result.stderr
    assert not marker.exists()


def test_setup_without_gateways_generates_empty_gateway_mapping(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    output_dir = tmp_path / "output"

    result = _run(
        "setup.sh",
        "--config-dir",
        str(config_dir),
        "--output-dir",
        str(output_dir),
        "--dry-run",
        "--yes",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    generated = (output_dir / "generated_config.yaml").read_text(encoding="utf-8")
    assert "  gateways: {}" in generated
    assert "host:" not in generated
    assert "192.0.2.50" not in generated
    assert stat.S_IMODE(output_dir.stat().st_mode) == 0o700
    environment = (output_dir / "detected_environment.txt").read_text(encoding="utf-8")
    assert "user=" not in environment
    assert "groups=" not in environment
    assert "hostname=" not in environment


def test_setup_warns_when_home_assistant_is_too_old(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".HA_VERSION").write_text("2024.12.5\n", encoding="utf-8")
    output_dir = tmp_path / "output"

    result = _run(
        "setup.sh",
        "--config-dir",
        str(config_dir),
        "--output-dir",
        str(output_dir),
        "--dry-run",
        "--yes",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "targets Home Assistant 2025.1 or newer" in result.stdout


def test_setup_dry_run_does_not_reconfigure_serial_device(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    output_dir = tmp_path / "output"
    serial_device = tmp_path / "ttyUSB-test"
    serial_device.write_bytes(b"")
    marker = tmp_path / "stty-was-run"
    bin_dir = tmp_path / "bin"
    _fake_command(bin_dir, "stty", 'touch "${STTY_MARKER}"')
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STTY_MARKER": str(marker),
    }

    result = _run(
        "setup.sh",
        "--config-dir",
        str(config_dir),
        "--output-dir",
        str(output_dir),
        "--usb-meshtastic",
        str(serial_device),
        "--dry-run",
        "--yes",
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "read-only checks only" in result.stdout
    assert not marker.exists()


def test_verify_treats_missing_staged_artifacts_as_optional(tmp_path: Path) -> None:
    config_dir = _installed_config(tmp_path)
    output_dir = tmp_path / "unused-output"
    bin_dir = tmp_path / "bin"
    _fake_command(bin_dir, "ha", "exit 0")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = _run(
        "verify_setup.sh",
        "--config-dir",
        str(config_dir),
        "--output-dir",
        str(output_dir),
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARN: optional staged file not found" in result.stdout
    assert "Home Assistant config validation" in result.stdout
    assert "Summary:" in result.stdout
    assert "0 failed" in result.stdout
    assert "Verification PASSED" in result.stdout


def test_verify_accumulates_required_failures_and_returns_nonzero(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "generated_config.yaml").write_text(
        """meshnet:
  gateways:
    test:
      host: 192.0.2.1
      port: 4403
""",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    _fake_command(bin_dir, "nc", "exit 1")
    _fake_command(bin_dir, "ha", "exit 1")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = _run(
        "verify_setup.sh",
        "--config-dir",
        str(config_dir),
        "--output-dir",
        str(output_dir),
        env=env,
    )

    assert result.returncode == 1
    assert "required file missing" in result.stdout
    assert "TCP closed or unreachable" in result.stdout
    assert "ha core check failed" in result.stdout
    assert "Summary:" in result.stdout
    assert "Verification FAILED" in result.stdout


def test_verify_rejects_home_assistant_older_than_minimum(tmp_path: Path) -> None:
    config_dir = _installed_config(tmp_path)
    (config_dir / ".HA_VERSION").write_text("2024.12.5\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    _fake_command(bin_dir, "ha", "exit 0")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = _run(
        "verify_setup.sh",
        "--config-dir",
        str(config_dir),
        "--output-dir",
        str(tmp_path / "unused-output"),
        env=env,
    )

    assert result.returncode == 1
    assert "older than the supported minimum 2025.1" in result.stdout


def test_uninstall_removes_only_validated_meshnet_component(tmp_path: Path) -> None:
    config_dir = _installed_config(tmp_path)
    component_dir = config_dir / "custom_components" / "meshnet"
    metadata = tmp_path / "rollback.json"
    _uninstall_metadata(
        metadata,
        config_dir=config_dir,
        component_path=component_dir,
    )

    result = _run("uninstall.sh", "--metadata", str(metadata), "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not component_dir.exists()
    assert (config_dir / "configuration.yaml").exists()


def test_uninstall_rejects_metadata_target_outside_config_dir(tmp_path: Path) -> None:
    config_dir = _installed_config(tmp_path)
    outside = tmp_path / "do-not-remove"
    outside.mkdir()
    (outside / "manifest.json").write_text(
        '{"domain": "meshnet"}\n', encoding="utf-8"
    )
    metadata = tmp_path / "malicious-rollback.json"
    _uninstall_metadata(
        metadata,
        config_dir=config_dir,
        component_path=outside,
    )

    result = _run("uninstall.sh", "--metadata", str(metadata), "--yes")

    assert result.returncode == 1
    assert outside.exists()
    assert "Nothing removed" in result.stdout


def test_uninstall_rejects_symlinked_component_target(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    component_parent = config_dir / "custom_components"
    component_parent.mkdir(parents=True)
    outside = tmp_path / "outside" / "custom_components" / "meshnet"
    outside.mkdir(parents=True)
    (outside / "manifest.json").write_text(
        '{"domain": "meshnet"}\n', encoding="utf-8"
    )
    component_path = component_parent / "meshnet"
    component_path.symlink_to(outside, target_is_directory=True)
    metadata = tmp_path / "symlink-rollback.json"
    _uninstall_metadata(
        metadata,
        config_dir=config_dir,
        component_path=component_path,
    )

    result = _run("uninstall.sh", "--metadata", str(metadata), "--yes")

    assert result.returncode == 1
    assert component_path.is_symlink()
    assert outside.exists()
    assert "Nothing removed" in result.stdout
