"""Logging helpers — a formatter that surfaces ``extra={...}`` fields.

The application attaches structured context to log records via the standard
``logging`` ``extra`` mechanism (e.g. ``service``, ``language``, ``exit_code``,
``stderr_tail``). A plain ``%(message)s`` formatter silently drops those keys,
which hides the very diagnostics we log. :class:`ExtraFieldFormatter` renders
the base line and then appends any non-standard record attributes as
``key=value`` pairs so nothing is lost on the console.
"""

from __future__ import annotations

import logging

# Attributes present on every LogRecord; anything outside this set was supplied
# by the caller through ``extra={...}`` and is worth printing.
_STANDARD_LOGRECORD_ATTRS: frozenset[str] = frozenset(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime", "taskName"}


class ExtraFieldFormatter(logging.Formatter):
    """Formatter that appends ``extra={...}`` fields to the formatted line."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_LOGRECORD_ATTRS
        }
        if not extras:
            return base
        rendered = " ".join(f"{key}={value!r}" for key, value in extras.items())
        return f"{base} {rendered}"
