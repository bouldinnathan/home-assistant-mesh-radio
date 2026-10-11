"""Contract-table safety tests for reviewed Meshtastic settings writes."""

from __future__ import annotations

import re

import pytest

from custom_components.meshnet import const
from custom_components.meshnet.settings_contracts import (
    LOCAL_WRITABLE_PATHS,
    POLICY_REMOTE,
    REASON_CHANNEL_KEY,
    REASON_GENERIC,
    REASON_HARDWARE_PIN,
    REASON_RADIO_PARAMETERS,
    REASON_SHUTDOWN,
    REMOTE_WRITABLE_PATHS,
    SCOPE_BOTH,
    SCOPE_LOCAL,
    SCOPE_REMOTE,
    WRITE_CONTRACTS,
    explicit_read_only_reason,
    normalize_contract_path,
    preview_warning,
    remote_write_allowed,
    section_of_path,
    write_allowed,
    write_contract,
)

_PATH_RE = re.compile(r"^(owner\.[a-z0-9_]+|(config|module)\.[a-z0-9_]+(\.[a-z0-9_]+)+|channel\.N(\.[a-z0-9_]+)+)$")
_FORBIDDEN_PARTS = ("security", "private_key", "public_key", "admin_key", "psk", "password", "fixed_pin")


def test_contract_table_never_lists_keys_secrets_or_security() -> None:
    for path, contract in WRITE_CONTRACTS.items():
        assert _PATH_RE.fullmatch(path), path
        assert contract.scope in {SCOPE_BOTH, SCOPE_LOCAL, SCOPE_REMOTE}
        if path in {"config.bluetooth.fixed_pin", "module.mqtt.password", "config.network.wifi_psk"}:
            # Write-only local credentials keep the existing gateway contract.
            assert contract.scope == SCOPE_LOCAL
            continue
        assert not any(part in path for part in _FORBIDDEN_PARTS), path
        assert not path.startswith("config.security."), path


def test_remote_set_excludes_local_only_credentials_and_transmit_toggle() -> None:
    for path in (
        "config.bluetooth.fixed_pin",
        "config.network.wifi_psk",
        "module.mqtt.username",
        "module.mqtt.password",
        "config.lora.tx_enabled",
    ):
        assert path in LOCAL_WRITABLE_PATHS
        assert path not in REMOTE_WRITABLE_PATHS
        assert not remote_write_allowed(path)
    assert const.REMOTE_ADMIN_WRITABLE_PATHS == REMOTE_WRITABLE_PATHS


def test_local_bluetooth_policy_defers_transport_hazards_to_the_planner() -> None:
    # These are remote-only because the local planner blocks them for the
    # active Bluetooth link; the contract itself only records the scope.
    for path in (
        "config.bluetooth.enabled",
        "config.bluetooth.mode",
        "config.power.is_power_saving",
        "config.network.wifi_enabled",
        "config.display.displaymode",
    ):
        assert path in REMOTE_WRITABLE_PATHS, path
        assert path not in LOCAL_WRITABLE_PATHS, path
        assert explicit_read_only_reason(path, path.rsplit(".", 1)[-1], policy="bluetooth")
        assert explicit_read_only_reason(path, path.rsplit(".", 1)[-1], policy=POLICY_REMOTE) is None


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("config.lora.region", REASON_RADIO_PARAMETERS),
        ("config.lora.modem_preset", REASON_RADIO_PARAMETERS),
        ("config.lora.channel_num", REASON_RADIO_PARAMETERS),
        ("config.power.on_battery_shutdown_after_secs", REASON_SHUTDOWN),
        ("config.power.sds_secs", REASON_SHUTDOWN),
        ("config.device.button_gpio", REASON_HARDWARE_PIN),
        ("module.external_notification.output", REASON_HARDWARE_PIN),
        ("module.audio.i2s_sck", REASON_HARDWARE_PIN),
        ("channel.3.settings.psk", REASON_CHANNEL_KEY),
        ("module.future.setting", REASON_GENERIC),
    ],
)
def test_excluded_fields_carry_their_reviewed_reason(path: str, reason: str) -> None:
    field_name = path.rsplit(".", 1)[-1]
    for policy in ("bluetooth", "serial", "tcp", POLICY_REMOTE):
        assert explicit_read_only_reason(path, field_name, policy=policy) == reason
        assert not write_allowed(path, policy=policy)


def test_channel_index_is_a_wildcard() -> None:
    assert normalize_contract_path("channel.0.settings.name") == "channel.N.settings.name"
    assert normalize_contract_path("channel.7.settings.module_settings.is_muted") == (
        "channel.N.settings.module_settings.is_muted"
    )
    assert write_contract("channel.5.settings.name") is write_contract("channel.N.settings.name")
    assert write_contract("channel.5.settings.name").max_bytes == 11
    assert remote_write_allowed("channel.2.settings.uplink_enabled")
    assert not remote_write_allowed("channel.2.role")
    assert section_of_path("channel.2.settings.name") == "channel.2"
    assert section_of_path("module.mqtt.map_report_settings.position_precision") == "module.mqtt"
    assert section_of_path("owner.short_name") == "owner"
    assert section_of_path("metadata.firmware_version") is None


def test_bounds_and_warnings_are_reviewed_values() -> None:
    hop = write_contract("config.lora.hop_limit")
    assert (hop.minimum, hop.maximum) == (1, 7)
    power = write_contract("config.lora.tx_power")
    assert (power.minimum, power.maximum) == (0, 30)
    interval = write_contract("config.position.position_broadcast_secs")
    assert interval.allow_zero and interval.minimum == 32
    nodeinfo = write_contract("config.device.node_info_broadcast_secs")
    assert nodeinfo.minimum == 3600 and not nodeinfo.allow_zero
    precision = write_contract("channel.N.settings.module_settings.position_precision")
    assert (precision.minimum, precision.maximum) == (0, 32)
    for path in ("config.device.tzdef", "config.network.wifi_ssid", "module.mqtt.address"):
        contract = write_contract(path)
        assert contract.max_bytes is not None and contract.allow_empty
    assert write_contract("owner.short_name").max_bytes == 4
    assert preview_warning("config.device.role")
    assert preview_warning("config.lora.override_duty_cycle")
    assert preview_warning("module.range_test.sender")
    assert preview_warning("config.display.flip_screen") is None
