"""Temporarily suppress one SDK logger family during secret-bearing writes."""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

_LOOP_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, asyncio.Lock]
] = weakref.WeakKeyDictionary()


class _PrefixFilter(logging.Filter):
    """Drop records emitted by one logger namespace only."""

    def __init__(self, prefix: str) -> None:
        super().__init__()
        self._prefix = prefix

    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            record.name == self._prefix
            or record.name.startswith(f"{self._prefix}.")
        )


def _lock_for(prefix: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    locks = _LOOP_LOCKS.setdefault(loop, {})
    return locks.setdefault(prefix, asyncio.Lock())


def _known_loggers() -> list[logging.Logger]:
    loggers = [logging.getLogger()]
    for candidate in logging.Logger.manager.loggerDict.values():
        if isinstance(candidate, logging.Logger):
            loggers.append(candidate)
    return loggers


@asynccontextmanager
async def suppress_sensitive_library_logs(prefix: str) -> AsyncIterator[None]:
    """Suppress a library logger namespace for one serialized async write.

    Some radio SDKs log complete protobufs or raw command bytes at DEBUG.  A
    settings write may contain keys, passwords, or a new device PIN, so the
    guard filters both root and library-owned handlers and disables every
    currently known logger in that namespace.  State is restored exactly on
    success, cancellation, and failure.
    """
    if not prefix or not all(part.isidentifier() for part in prefix.split(".")):
        raise ValueError("logger prefix must be a dotted identifier")
    async with _lock_for(prefix):
        prefix_filter = _PrefixFilter(prefix)
        loggers = _known_loggers()
        prefixed_loggers = [
            logger
            for logger in loggers
            if logger.name == prefix or logger.name.startswith(f"{prefix}.")
        ]
        # Ensure the base namespace exists and is disabled even when the SDK
        # had not emitted a record before this settings operation.
        base_logger = logging.getLogger(prefix)
        if base_logger not in prefixed_loggers:
            prefixed_loggers.append(base_logger)
        disabled_states = {logger: logger.disabled for logger in prefixed_loggers}
        base_level = base_logger.level
        handlers = list(
            {
                handler
                for logger in (*loggers, base_logger)
                for handler in logger.handlers
            }
        )
        filtered_handlers: list[logging.Handler] = []
        try:
            # A child logger created while the guarded operation is running
            # inherits this prohibitive level. Existing namespace loggers are
            # disabled as well, while filters protect propagation through all
            # handlers that existed when the operation began.
            base_logger.setLevel(logging.CRITICAL + 1)
            for handler in handlers:
                handler.addFilter(prefix_filter)
                filtered_handlers.append(handler)
            for logger in prefixed_loggers:
                logger.disabled = True
            yield
        finally:
            for logger, disabled in disabled_states.items():
                logger.disabled = disabled
            base_logger.setLevel(base_level)
            for handler in filtered_handlers:
                handler.removeFilter(prefix_filter)
