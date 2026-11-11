"""Logging with secret redaction.

The engine never logs secret values on purpose. The redactor is the backstop: every
secret the engine has seen is registered here, and every formatted log line (message,
arguments, and tracebacks) is scrubbed before it reaches a file or the console.
"""

from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler

from engine.config import Config

REDACTED = "[REDACTED]"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_HANDLER_TAG = "_clipper_handler"


class SecretRedactor:
    # Shorter values would shred ordinary log text and are not plausible credentials.
    MIN_LEN = 4

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._lock = threading.Lock()

    def register(self, value: str | None) -> None:
        if value and len(value) >= self.MIN_LEN:
            with self._lock:
                self._values.add(value)

    def redact(self, text: str) -> str:
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for value in values:
            if value in text:
                text = text.replace(value, REDACTED)
        return text


class RedactingFormatter(logging.Formatter):
    def __init__(self, redactor: SecretRedactor, fmt: str = LOG_FORMAT) -> None:
        super().__init__(fmt)
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self._redactor.redact(super().format(record))


def setup_logging(config: Config, redactor: SecretRedactor, level: int = logging.INFO) -> None:
    """Route all logging through redacting handlers: a rotating file and stderr."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_TAG, False):
            root.removeHandler(handler)
            handler.close()

    formatter = RedactingFormatter(redactor)
    file_handler = RotatingFileHandler(
        config.log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    stream_handler = logging.StreamHandler(sys.stderr)
    for handler in (file_handler, stream_handler):
        handler.setFormatter(formatter)
        setattr(handler, _HANDLER_TAG, True)
        root.addHandler(handler)
    root.setLevel(level)
