"""Tests for settings-write log suppression."""

from __future__ import annotations

import asyncio
import logging

import pytest

from custom_components.meshnet.gateway import MeshGateway
from custom_components.meshnet.models import GatewayConfig
from custom_components.meshnet.sensitive_logging import (
    suppress_sensitive_library_logs,
)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class _TestGateway(MeshGateway):
    async def async_start(self) -> None:
        return None

    async def async_stop(self) -> None:
        return None

    async def async_send_message(self, **_kwargs) -> str:
        return "message"


def test_sensitive_sdk_records_are_suppressed_and_logger_state_is_restored() -> None:
    async def run() -> None:
        root = logging.getLogger()
        sdk = logging.getLogger("private_radio.transport")
        unrelated = logging.getLogger("meshnet.test")
        capture = _Capture()
        root.addHandler(capture)
        original_disabled = sdk.disabled
        try:
            async with suppress_sensitive_library_logs("private_radio"):
                sdk.warning("PIN=123456 raw=deadbeef")
                unrelated.warning("safe unrelated record")
            sdk.warning("safe post-write status")
        finally:
            root.removeHandler(capture)
            sdk.disabled = original_disabled

        assert capture.messages == [
            "safe unrelated record",
            "safe post-write status",
        ]

    asyncio.run(run())


def test_library_owned_handlers_are_filtered_and_restored_after_failure() -> None:
    async def run() -> None:
        sdk = logging.getLogger("private_radio")
        capture = _Capture()
        sdk.addHandler(capture)
        sdk.propagate = False
        original_disabled = sdk.disabled
        try:
            with pytest.raises(RuntimeError):
                async with suppress_sensitive_library_logs("private_radio"):
                    sdk.error("private key material")
                    raise RuntimeError("write failed")
            sdk.error("safe failure category")
        finally:
            sdk.removeHandler(capture)
            sdk.propagate = True
            sdk.disabled = original_disabled

        assert capture.messages == ["safe failure category"]

    asyncio.run(run())


def test_invalid_logger_prefix_is_rejected() -> None:
    async def run() -> None:
        with pytest.raises(ValueError):
            async with suppress_sensitive_library_logs("meshcore;secret"):
                pass

    asyncio.run(run())


def test_child_logger_created_inside_guard_inherits_suppression() -> None:
    """A newly created normal SDK child must not bypass the guard."""

    async def run() -> None:
        child_name = "late_private_radio.transport"
        base = logging.getLogger("late_private_radio")
        child = logging.getLogger(child_name)
        capture = _Capture()
        child.handlers.clear()
        logging.Logger.manager.loggerDict.pop(child_name, None)
        original_level = base.level
        try:
            async with suppress_sensitive_library_logs("late_private_radio"):
                late_child = logging.getLogger(child_name)
                late_child.addHandler(capture)
                late_child.error("PIN=654321")
                late_child.removeHandler(capture)
            logging.getLogger(child_name).addHandler(capture)
            logging.getLogger(child_name).error("safe post-write status")
        finally:
            logging.getLogger(child_name).removeHandler(capture)
            base.setLevel(original_level)

        assert capture.messages == ["safe post-write status"]

    asyncio.run(run())


def test_gateway_warning_never_logs_identity_or_raw_provider_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> None:
        private_gateway_id = "kitchen-radio-aabbccddeeff"
        private_error = (
            "connection to AA:BB:CC:DD:EE:FF at /dev/ttyUSB-private failed"
        )
        statuses = []

        async def status_callback(status) -> None:
            statuses.append(status)

        gateway = _TestGateway(
            None,
            GatewayConfig(
                gateway_id=private_gateway_id,
                name="Private kitchen radio",
                protocol="meshtastic",
                transport="bluetooth",
            ),
            lambda _packet: None,
            lambda _node: None,
            status_callback,
            logging.getLogger("meshnet.privacy-test"),
        )
        with caplog.at_level(logging.WARNING, logger="meshnet.privacy-test"):
            await gateway._emit_error(RuntimeError(private_error))

        rendered = caplog.text
        assert private_gateway_id not in rendered
        assert "Private kitchen radio" not in rendered
        assert "AA:BB:CC:DD:EE:FF" not in rendered
        assert "/dev/ttyUSB-private" not in rendered
        assert "connection failure" in rendered
        assert gateway.status.errors == [private_error]
        assert statuses[-1] is gateway.status

    asyncio.run(run())


def test_callback_failures_log_only_exception_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pytest.importorskip("meshtastic")
    from custom_components.meshnet.aiomeshtastic.client import (
        MeshtasticBluetoothClient,
    )

    async def run() -> None:
        secret = "radio AA:BB:CC:DD:EE:FF at /dev/ttyUSB-private"

        async def device_provider(_address: str) -> object:
            return object()

        logger_name = "meshnet.callback-privacy-test"
        client = MeshtasticBluetoothClient(
            address="AA:BB:CC:DD:EE:FF",
            device_provider=device_provider,
            logger=logging.getLogger(logger_name),
        )

        def failed_callback(_value: object) -> None:
            raise RuntimeError(secret)

        async def failed_async_callback(_value: object) -> None:
            raise RuntimeError(secret)

        with caplog.at_level(logging.WARNING, logger=logger_name):
            client._invoke_callback(failed_callback, object())
            client._invoke_callback(failed_async_callback, object())
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        rendered = caplog.text
        assert secret not in rendered
        assert "AA:BB:CC:DD:EE:FF" not in rendered
        assert "/dev/ttyUSB-private" not in rendered
        assert rendered.count("RuntimeError") == 2
        assert client.diagnostic_snapshot()["callback_error_count"] == 2

    asyncio.run(run())
