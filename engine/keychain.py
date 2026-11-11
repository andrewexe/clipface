"""Secrets live only in the OS keychain (Windows Credential Manager, macOS Keychain,
Secret Service on Linux). Nothing here writes a secret to disk, the database, or a log.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import keyring
import keyring.errors
from keyring.backends import fail

from engine.logging_setup import SecretRedactor

log = logging.getLogger(__name__)

SERVICE = "clipper"
MAX_SECRET_LEN = 1024


@dataclass(frozen=True)
class SecretSpec:
    name: str
    label: str
    group: str
    help: str


_SPECS = [
    SecretSpec(
        "anthropic_api_key",
        "Anthropic API key",
        "Anthropic",
        "Clip scoring, titles, captions. Set a monthly spend limit in the Anthropic console.",
    ),
    SecretSpec("twitch_client_id", "Twitch client ID", "Twitch", "From your Twitch developer app."),
    SecretSpec(
        "twitch_client_secret",
        "Twitch client secret",
        "Twitch",
        "From your Twitch developer app.",
    ),
    SecretSpec("kick_client_id", "Kick client ID", "Kick", "From your Kick developer app."),
    SecretSpec("kick_client_secret", "Kick client secret", "Kick", "From your Kick developer app."),
    SecretSpec(
        "transcription_api_key",
        "Transcription API key",
        "Transcription",
        "Provider with word-level timestamps; chosen in Phase 4.",
    ),
]
KNOWN_SECRETS: dict[str, SecretSpec] = {spec.name: spec for spec in _SPECS}


class KeychainUnavailable(RuntimeError):
    """No usable OS keychain, or the keychain refused the operation."""


class SecretStore:
    def __init__(self, redactor: SecretRedactor, service: str = SERVICE) -> None:
        self._redactor = redactor
        self._service = service

    def backend(self) -> dict[str, object]:
        kr = keyring.get_keyring()
        module = type(kr).__module__
        # keyrings.alt backends write plaintext or weakly protected files: not a keychain.
        available = not isinstance(kr, fail.Keyring) and not module.startswith("keyrings.alt")
        return {"name": getattr(kr, "name", module), "available": available}

    def _require_backend(self) -> None:
        if not self.backend()["available"]:
            raise KeychainUnavailable("no OS keychain is available on this machine")

    def get(self, name: str) -> str | None:
        """Engine-internal only. The API never returns secret values."""
        self._check_name(name)
        self._require_backend()
        try:
            value = keyring.get_password(self._service, name)
        except keyring.errors.KeyringError as exc:
            raise KeychainUnavailable(f"keychain read failed: {type(exc).__name__}") from None
        self._redactor.register(value)
        return value

    def is_set(self, name: str) -> bool:
        return bool(self.get(name))

    def set(self, name: str, value: str) -> None:
        self._check_name(name)
        if not value or len(value) > MAX_SECRET_LEN:
            raise ValueError("secret must be 1 to 1024 characters")
        self._require_backend()
        self._redactor.register(value)
        try:
            keyring.set_password(self._service, name, value)
        except keyring.errors.KeyringError as exc:
            raise KeychainUnavailable(f"keychain write failed: {type(exc).__name__}") from None

    def delete(self, name: str) -> bool:
        """Returns False if there was nothing to delete."""
        self._check_name(name)
        self._require_backend()
        try:
            keyring.delete_password(self._service, name)
        except keyring.errors.PasswordDeleteError:
            return False
        except keyring.errors.KeyringError as exc:
            raise KeychainUnavailable(f"keychain delete failed: {type(exc).__name__}") from None
        return True

    def warm(self) -> None:
        """Register every stored secret with the redactor so logs are scrubbed from startup."""
        try:
            for name in KNOWN_SECRETS:
                self.get(name)
        except KeychainUnavailable as exc:
            log.warning("keychain not usable at startup: %s", exc)

    @staticmethod
    def _check_name(name: str) -> None:
        if name not in KNOWN_SECRETS:
            raise KeyError(name)
