"""Secret lookup boundaries.

Provider adapters deliberately receive a :class:`SecretStore` instead of
reading environment variables or configuration files.  The production
implementation can therefore use Linux Secret Service while tests
can use the small in-memory implementation below.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable


class SecretNotFoundError(RuntimeError):
    """Raised when a configured provider credential is unavailable."""


@runtime_checkable
class SecretStore(Protocol):
    async def get_secret(self, name: str) -> str | None:
        """Return a secret without logging or serialising it."""


class MappingSecretStore:
    """Minimal test/development store; never include this object in logs."""

    def __init__(self, secrets: Mapping[str, str]) -> None:
        self._secrets = dict(secrets)

    async def get_secret(self, name: str) -> str | None:
        return self._secrets.get(name)


async def require_secret(store: SecretStore, name: str) -> str:
    value = await store.get_secret(name)
    if not value:
        raise SecretNotFoundError(f"credential {name!r} is not configured")
    return value
