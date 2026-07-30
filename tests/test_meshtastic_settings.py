"""Tests for privacy-safe local Meshtastic settings handling."""

from __future__ import annotations

import logging
from typing import Any

import pytest

pytest.importorskip("google.protobuf")

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from custom_components.meshnet.meshtastic_settings import (
    MeshtasticSettingsState,
    MeshtasticSettingsValidationError,
)


def _messages() -> dict[str, type[Any]]:
    """Build the small protobuf surface used by the settings state."""
    file_proto = descriptor_pb2.FileDescriptorProto(
        name="meshnet_settings_test.proto",
        package="meshnet.settings.test",
        syntax="proto3",
    )

    def message(name: str) -> descriptor_pb2.DescriptorProto:
        value = file_proto.message_type.add()
        value.name = name
        return value

    def scalar(
        owner: descriptor_pb2.DescriptorProto,
        name: str,
        number: int,
        field_type: int,
        *,
        repeated: bool = False,
        oneof: int | None = None,
    ) -> None:
        value = owner.field.add(
            name=name,
            number=number,
            type=field_type,
            label=(
                descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED
                if repeated
                else descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
            ),
        )
        if oneof is not None:
            value.oneof_index = oneof

    def nested(
        owner: descriptor_pb2.DescriptorProto,
        name: str,
        number: int,
        type_name: str,
        *,
        oneof: int | None = None,
    ) -> None:
        value = owner.field.add(
            name=name,
            number=number,
            type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
            type_name=f".meshnet.settings.test.{type_name}",
            label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        )
        if oneof is not None:
            value.oneof_index = oneof

    network = message("NetworkConfig")
    scalar(network, "wifi_ssid", 1, descriptor_pb2.FieldDescriptorProto.TYPE_STRING)
    scalar(network, "wifi_psk", 2, descriptor_pb2.FieldDescriptorProto.TYPE_STRING)
    scalar(network, "enabled", 3, descriptor_pb2.FieldDescriptorProto.TYPE_BOOL)

    security = message("SecurityConfig")
    scalar(security, "private_key", 1, descriptor_pb2.FieldDescriptorProto.TYPE_BYTES)
    scalar(
        security,
        "admin_key",
        2,
        descriptor_pb2.FieldDescriptorProto.TYPE_BYTES,
        repeated=True,
    )
    scalar(security, "is_managed", 3, descriptor_pb2.FieldDescriptorProto.TYPE_BOOL)

    bluetooth = message("BluetoothConfig")
    scalar(bluetooth, "enabled", 1, descriptor_pb2.FieldDescriptorProto.TYPE_BOOL)
    scalar(
        bluetooth,
        "fixed_pin",
        2,
        descriptor_pb2.FieldDescriptorProto.TYPE_UINT32,
    )

    display_mode = file_proto.enum_type.add(name="DisplayMode")
    display_mode.value.add(name="DEFAULT", number=0)
    display_mode.value.add(name="COLOR", number=1)
    display = message("DisplayConfig")
    scalar(
        display,
        "flip_screen",
        1,
        descriptor_pb2.FieldDescriptorProto.TYPE_BOOL,
    )
    display.field.add(
        name="displaymode",
        number=2,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_ENUM,
        type_name=".meshnet.settings.test.DisplayMode",
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
    )

    config = message("Config")
    config.oneof_decl.add(name="payload_variant")
    nested(config, "network", 1, "NetworkConfig", oneof=0)
    nested(config, "security", 2, "SecurityConfig", oneof=0)
    nested(config, "bluetooth", 3, "BluetoothConfig", oneof=0)
    nested(config, "display", 4, "DisplayConfig", oneof=0)

    mqtt = message("MqttConfig")
    scalar(mqtt, "enabled", 1, descriptor_pb2.FieldDescriptorProto.TYPE_BOOL)
    scalar(mqtt, "username", 2, descriptor_pb2.FieldDescriptorProto.TYPE_STRING)
    scalar(mqtt, "password", 3, descriptor_pb2.FieldDescriptorProto.TYPE_STRING)

    module = message("ModuleConfig")
    module.oneof_decl.add(name="payload_variant")
    nested(module, "mqtt", 1, "MqttConfig", oneof=0)

    role = file_proto.enum_type.add(name="ChannelRole")
    role.value.add(name="DISABLED", number=0)
    role.value.add(name="PRIMARY", number=1)
    role.value.add(name="SECONDARY", number=2)

    channel_settings = message("ChannelSettings")
    scalar(channel_settings, "name", 1, descriptor_pb2.FieldDescriptorProto.TYPE_STRING)
    scalar(channel_settings, "psk", 2, descriptor_pb2.FieldDescriptorProto.TYPE_BYTES)
    channel = message("Channel")
    scalar(channel, "index", 1, descriptor_pb2.FieldDescriptorProto.TYPE_INT32)
    role_field = channel.field.add(
        name="role",
        number=2,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_ENUM,
        type_name=".meshnet.settings.test.ChannelRole",
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
    )
    assert role_field.name == "role"
    nested(channel, "settings", 3, "ChannelSettings")

    user = message("User")
    scalar(user, "long_name", 1, descriptor_pb2.FieldDescriptorProto.TYPE_STRING)
    scalar(user, "short_name", 2, descriptor_pb2.FieldDescriptorProto.TYPE_STRING)
    scalar(user, "public_key", 3, descriptor_pb2.FieldDescriptorProto.TYPE_BYTES)

    metadata = message("Metadata")
    scalar(metadata, "firmware_version", 1, descriptor_pb2.FieldDescriptorProto.TYPE_STRING)
    scalar(metadata, "serial_number", 2, descriptor_pb2.FieldDescriptorProto.TYPE_STRING)
    scalar(metadata, "has_bluetooth", 3, descriptor_pb2.FieldDescriptorProto.TYPE_BOOL)

    my_info = message("MyInfo")
    scalar(my_info, "my_node_num", 1, descriptor_pb2.FieldDescriptorProto.TYPE_UINT32)
    node_info = message("NodeInfo")
    scalar(node_info, "num", 1, descriptor_pb2.FieldDescriptorProto.TYPE_UINT32)
    nested(node_info, "user", 2, "User")

    from_radio = message("FromRadio")
    from_radio.oneof_decl.add(name="payload_variant")
    nested(from_radio, "config", 1, "Config", oneof=0)
    nested(from_radio, "moduleConfig", 2, "ModuleConfig", oneof=0)
    nested(from_radio, "channel", 3, "Channel", oneof=0)
    nested(from_radio, "metadata", 4, "Metadata", oneof=0)
    nested(from_radio, "my_info", 5, "MyInfo", oneof=0)
    nested(from_radio, "node_info", 6, "NodeInfo", oneof=0)
    scalar(
        from_radio,
        "config_complete_id",
        7,
        descriptor_pb2.FieldDescriptorProto.TYPE_UINT32,
        oneof=0,
    )

    admin = message("AdminMessage")
    scalar(admin, "begin_edit_settings", 1, descriptor_pb2.FieldDescriptorProto.TYPE_BOOL)
    scalar(admin, "commit_edit_settings", 2, descriptor_pb2.FieldDescriptorProto.TYPE_BOOL)
    nested(admin, "set_config", 3, "Config")
    nested(admin, "set_module_config", 4, "ModuleConfig")
    nested(admin, "set_channel", 5, "Channel")
    nested(admin, "set_owner", 6, "User")

    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    return {
        name: message_factory.GetMessageClass(
            pool.FindMessageTypeByName(f"meshnet.settings.test.{name}")
        )
        for name in ("FromRadio", "AdminMessage")
    }


def _captured_state(*, managed: bool = False) -> tuple[MeshtasticSettingsState, type[Any]]:
    classes = _messages()
    FromRadio = classes["FromRadio"]
    state = MeshtasticSettingsState()
    state.begin_refresh()

    record = FromRadio()
    record.my_info.my_node_num = 123
    state.capture_from_radio(record, my_node_num=123)

    record = FromRadio()
    record.config.network.wifi_ssid = "private-wifi-name"
    record.config.network.wifi_psk = "private-wifi-password"
    record.config.network.enabled = True
    state.capture_from_radio(record, my_node_num=123)

    record = FromRadio()
    record.config.security.private_key = b"private-radio-key"
    record.config.security.admin_key.append(b"private-admin-key")
    record.config.security.is_managed = managed
    state.capture_from_radio(record, my_node_num=123)

    record = FromRadio()
    record.config.bluetooth.enabled = True
    record.config.bluetooth.fixed_pin = 123456
    state.capture_from_radio(record, my_node_num=123)

    record = FromRadio()
    record.config.display.flip_screen = False
    record.config.display.displaymode = 0
    state.capture_from_radio(record, my_node_num=123)

    record = FromRadio()
    record.moduleConfig.mqtt.enabled = True
    record.moduleConfig.mqtt.username = "private-mqtt-user"
    record.moduleConfig.mqtt.password = "private-mqtt-password"
    state.capture_from_radio(record, my_node_num=123)

    record = FromRadio()
    record.channel.index = 0
    record.channel.role = 1
    record.channel.settings.name = "Private Channel"
    record.channel.settings.psk = b"private-channel-psk"
    state.capture_from_radio(record, my_node_num=123)

    record = FromRadio()
    record.node_info.num = 123
    record.node_info.user.long_name = "Private Owner"
    record.node_info.user.short_name = "PRIV"
    record.node_info.user.public_key = b"private-owner-key"
    state.capture_from_radio(record, my_node_num=123)

    record = FromRadio()
    record.metadata.firmware_version = "2.7.11"
    record.metadata.serial_number = "private-serial-number"
    record.metadata.has_bluetooth = True
    state.capture_from_radio(record, my_node_num=123)
    state.mark_complete()
    return state, classes["AdminMessage"]


def _fields(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        field["path"]: field
        for category in snapshot["categories"]
        for field in category["fields"]
    }


def test_snapshot_never_projects_credentials_or_private_metadata() -> None:
    state, _ = _captured_state()

    snapshot = state.public_snapshot(transport="bluetooth")
    rendered = repr(snapshot)
    fields = _fields(snapshot)

    for private_value in (
        "private-wifi-password",
        "private-radio-key",
        "private-admin-key",
        "private-mqtt-user",
        "private-mqtt-password",
        "private-channel-psk",
        "private-owner-key",
        "private-serial-number",
    ):
        assert private_value not in rendered
    assert fields["config.network.wifi_psk"]["configured"] is True
    assert fields["config.security.private_key"]["configured"] is True
    assert fields["config.security.admin_key"]["configured"] is True
    assert fields["config.security.admin_key"]["multiple"] is True
    assert fields["config.security.admin_key"]["writable"] is False
    assert fields["config.security.admin_key"]["allow_clear"] is False
    assert fields["module.mqtt.username"]["configured"] is True
    assert fields["module.mqtt.password"]["configured"] is True
    assert fields["channel.0.settings.psk"]["configured"] is True
    assert fields["config.network.wifi_ssid"]["value"] == "private-wifi-name"
    assert fields["channel.0.role"]["type"] == "select"
    assert fields["channel.0.role"]["options"] == [
        {"value": "DISABLED", "label": "Disabled"},
        {"value": "PRIMARY", "label": "Primary"},
        {"value": "SECONDARY", "label": "Secondary"},
    ]
    assert fields["metadata.firmware_version"]["value"] == "2.7.11"
    assert "metadata.serial_number" not in fields
    assert "owner.public_key" not in fields
    private_material = snapshot["_secret_revision_material"]
    assert private_material["config.bluetooth.fixed_pin"] == 123456
    assert private_material["channel.0.settings.psk"] == b"private-channel-psk"
    assert "private-channel-psk" not in repr(private_material)


def test_plan_uses_begin_commit_and_connection_critical_changes_last() -> None:
    state, AdminMessage = _captured_state()
    replacement_secret = "654321"

    plan = state.build_plan(
        {
            "config.display.flip_screen": True,
            "owner.short_name": "HOME",
            "config.bluetooth.fixed_pin": {
                "operation": "replace",
                "value": replacement_secret,
            },
        },
        transport="bluetooth",
        admin_message_factory=AdminMessage,
    )

    assert [operation.operation for operation in plan.operations] == [
        "begin_edit_settings",
        "set_config",
        "set_owner",
        "set_config",
        "commit_edit_settings",
    ]
    assert plan.operations[0].message.begin_edit_settings is True
    assert plan.operations[-1].message.commit_edit_settings is True
    assert plan.operations[-2].connection_critical is True
    assert plan.connection_critical_paths == ("config.bluetooth.fixed_pin",)
    assert replacement_secret not in repr(plan)
    assert replacement_secret not in repr(plan.public_summary())
    assert replacement_secret not in repr(
        plan.read_only_result("confirmed_admin_write_and_verification_not_available")
    )


def test_secrets_require_explicit_replace_or_clear_operations() -> None:
    state, AdminMessage = _captured_state()

    with pytest.raises(MeshtasticSettingsValidationError, match="explicit"):
        state.build_plan(
            {"config.bluetooth.fixed_pin": "123456"},
            transport="bluetooth",
            admin_message_factory=AdminMessage,
        )

    with pytest.raises(MeshtasticSettingsValidationError, match="six-digit"):
        state.build_plan(
            {
                "config.bluetooth.fixed_pin": {
                    "operation": "replace",
                    "value": "000000",
                }
            },
            transport="bluetooth",
            admin_message_factory=AdminMessage,
        )

    clear_plan = state.build_plan(
        {"config.bluetooth.fixed_pin": {"operation": "clear"}},
        transport="bluetooth",
        admin_message_factory=AdminMessage,
    )
    set_config = next(
        operation.message.set_config
        for operation in clear_plan.operations
        if operation.operation == "set_config"
    )
    assert set_config.bluetooth.fixed_pin == 0


def test_managed_radio_returns_field_reasons_without_building_writes() -> None:
    state, AdminMessage = _captured_state(managed=True)

    plan = state.build_plan(
        {"owner.short_name": "HOME"},
        transport="bluetooth",
        admin_message_factory=AdminMessage,
    )

    assert plan.operations == ()
    assert plan.blocked_paths == {
        "owner.short_name": "managed_mode_rejects_local_admin_changes"
    }


def test_planning_secret_change_emits_no_meshtastic_log_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state, AdminMessage = _captured_state()
    secret = "123456"
    caplog.set_level(logging.DEBUG)

    plan = state.build_plan(
        {
            "config.bluetooth.fixed_pin": {
                "operation": "replace",
                "value": secret,
            }
        },
        transport="bluetooth",
        admin_message_factory=AdminMessage,
    )
    result = plan.read_only_result(
        "confirmed_admin_write_and_verification_not_available"
    )

    assert result["success"] is False
    assert secret not in caplog.text
    assert secret not in repr(result)


@pytest.mark.parametrize(
    "path",
    [
        "config.bluetooth.enabled",
        "config.display.displaymode",
        "config.network.enabled",
        "config.security.is_managed",
        "module.mqtt.enabled",
        "channel.0.role",
    ],
)
def test_unvalidated_or_ble_hazardous_fields_fail_closed(path: str) -> None:
    state, AdminMessage = _captured_state()

    plan = state.build_plan(
        {path: False if path.endswith("enabled") else 0},
        transport="bluetooth",
        admin_message_factory=AdminMessage,
    )

    assert plan.operations == ()
    assert path in plan.blocked_paths


def test_full_section_verification_detects_companion_field_change() -> None:
    state, AdminMessage = _captured_state()
    plan = state.build_plan(
        {"config.display.flip_screen": True},
        transport="bluetooth",
        admin_message_factory=AdminMessage,
    )
    set_operation = next(
        operation for operation in plan.operations if operation.paths
    )

    state._configs["display"].CopyFrom(
        set_operation.message.set_config.display
    )
    verified, unverified = state.verify_plan(plan)
    assert verified == ["config.display.flip_screen"]
    assert unverified == []

    state._configs["display"].displaymode = 1
    verified, unverified = state.verify_plan(plan)
    assert verified == []
    assert unverified == ["config.display.flip_screen"]


def test_current_color_display_mode_blocks_any_ble_display_setter() -> None:
    state, AdminMessage = _captured_state()
    state._configs["display"].displaymode = 1

    plan = state.build_plan(
        {"config.display.flip_screen": True},
        transport="bluetooth",
        admin_message_factory=AdminMessage,
    )

    assert plan.operations == ()
    assert plan.blocked_paths == {
        "config.display.flip_screen": (
            "current_display_mode_can_disable_bluetooth_on_reboot"
        )
    }


def test_disabled_bluetooth_state_blocks_pin_setter_over_ble() -> None:
    state, AdminMessage = _captured_state()
    state._configs["bluetooth"].enabled = False

    plan = state.build_plan(
        {
            "config.bluetooth.fixed_pin": {
                "operation": "replace",
                "value": "654321",
            }
        },
        transport="bluetooth",
        admin_message_factory=AdminMessage,
    )

    assert plan.operations == ()
    assert plan.blocked_paths == {
        "config.bluetooth.fixed_pin": (
            "the_active_bluetooth_transport_cannot_preserve_a_disabled_state"
        )
    }
