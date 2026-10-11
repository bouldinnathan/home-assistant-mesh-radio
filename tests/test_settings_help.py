"""Every setting the panel can show has reviewed help text."""

from __future__ import annotations

import pytest

from custom_components.meshnet.settings_contracts import _EXPLICIT_READ_ONLY, WRITE_CONTRACTS
from custom_components.meshnet.settings_help import (
    HELP_MAX_LENGTH,
    SETTING_HELP,
    SETTING_UNITS,
    UNIT_MAX_LENGTH,
    setting_help,
    setting_unit,
)


def test_every_contract_exclusion_and_unit_has_help() -> None:
    assert [path for path in WRITE_CONTRACTS if path not in SETTING_HELP] == []
    assert [path for path in _EXPLICIT_READ_ONLY if path not in SETTING_HELP] == []
    assert [path for path in SETTING_UNITS if path not in SETTING_HELP] == []


def test_help_text_is_short_plain_and_complete() -> None:
    for path, text in SETTING_HELP.items():
        assert 1 <= len(text) <= HELP_MAX_LENGTH, path
        assert text.endswith("."), path
        assert not any(ord(character) < 32 for character in text), path
        assert "<" not in text and ">" not in text, path
    for path, unit in SETTING_UNITS.items():
        assert 1 <= len(unit) <= UNIT_MAX_LENGTH, path


def test_channel_paths_share_one_help_entry() -> None:
    assert setting_help("channel.3.settings.name") == SETTING_HELP["channel.N.settings.name"]
    assert setting_unit("channel.0.settings.module_settings.position_precision") == "bits"
    assert setting_help("metadata.unknown_future_field") is None
    assert setting_help("config.device_ui.screen_timeout") == SETTING_HELP["device_ui.screen_timeout"]
    assert setting_unit("config.device_ui.screen_timeout") == "seconds"
    assert setting_unit("owner.long_name") is None
    assert setting_help(None) is None  # type: ignore[arg-type]
    assert setting_unit(42) is None  # type: ignore[arg-type]


def test_every_projected_real_protobuf_field_has_help_and_interval_units() -> None:
    pytest.importorskip("meshtastic")
    from meshtastic.protobuf import channel_pb2, config_pb2, device_ui_pb2, mesh_pb2, module_config_pb2

    from custom_components.meshnet.meshtastic_settings import _project_message_fields

    messages = []
    config = config_pb2.Config()
    for descriptor in config.DESCRIPTOR.fields:
        if descriptor.type not in (10, 11):
            continue
        section = getattr(config, descriptor.name)
        if descriptor.name == "network":
            section.ipv4_config.SetInParent()
        messages.append((f"config.{descriptor.name}", section))
    modules = module_config_pb2.ModuleConfig()
    for descriptor in modules.DESCRIPTOR.fields:
        if descriptor.type not in (10, 11):
            continue
        section = getattr(modules, descriptor.name)
        if descriptor.name == "mqtt":
            section.map_report_settings.SetInParent()
        messages.append((f"module.{descriptor.name}", section))
    channel = channel_pb2.Channel()
    channel.settings.module_settings.SetInParent()
    messages.append(("channel.0", channel))
    messages.append(("owner", mesh_pb2.User()))
    device_ui = device_ui_pb2.DeviceUIConfig()
    device_ui.node_filter.SetInParent()
    device_ui.node_highlight.SetInParent()
    messages.append(("device_ui", device_ui))

    missing: list[str] = []
    for prefix, message in messages:
        for field in _project_message_fields(message, prefix=prefix, write_supported=True, transport="remote"):
            if not field.get("description"):
                missing.append(field["path"])
            interval = field["path"].endswith(("_secs", "_interval", "_ms"))
            if field["type"] in {"integer", "number"} and interval and not field.get("unit"):
                missing.append(f"unit:{field['path']}")
            if field["type"] not in {"integer", "number"}:
                assert "unit" not in field, field["path"]
    assert missing == []
