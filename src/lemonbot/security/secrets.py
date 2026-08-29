from __future__ import annotations

import importlib
import os
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class SecretStoreError(RuntimeError):
    pass


class SecretNotFound(SecretStoreError):
    pass


class SecretStore(ABC):
    @abstractmethod
    def get(self, name: str) -> str | None: ...

    @abstractmethod
    def set(self, name: str, value: str) -> None: ...

    @abstractmethod
    def delete(self, name: str) -> bool: ...

    def require(self, name: str) -> str:
        value = self.get(name)
        if value is None:
            raise SecretNotFound(f"required credential is not configured: {name}")
        return value


def _validate_name(name: str) -> None:
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789_-"
    if not name or len(name) > 128 or any(ch not in allowed for ch in name):
        raise ValueError("secret names must contain only lowercase letters, digits, '_' or '-'")


@dataclass(slots=True)
class NamespacedSecretStore(SecretStore):
    inner: SecretStore
    namespace: str

    def _name(self, name: str) -> str:
        _validate_name(name)
        return f"{self.namespace}_{name}"

    def get(self, name: str) -> str | None:
        return self.inner.get(self._name(name))

    def set(self, name: str, value: str) -> None:
        self.inner.set(self._name(name), value)

    def delete(self, name: str) -> bool:
        return self.inner.delete(self._name(name))


class EnvironmentSecretStore(SecretStore):
    """Explicit test/development store; production never selects this implicitly."""

    def __init__(self, *, allow: bool = False, prefix: str = "LEMONBOT_SECRET_") -> None:
        if not allow:
            raise SecretStoreError("environment credentials require an explicit development opt-in")
        self._prefix = prefix

    def _key(self, name: str) -> str:
        _validate_name(name)
        return f"{self._prefix}{name.upper()}"

    def get(self, name: str) -> str | None:
        return os.environ.get(self._key(name))

    def set(self, name: str, value: str) -> None:
        raise SecretStoreError("environment store is read-only")

    def delete(self, name: str) -> bool:
        raise SecretStoreError("environment store is read-only")


class LinuxSecretServiceStore(SecretStore):
    """Stores secrets in the unlocked Freedesktop Secret Service collection.

    Lemonbot never opens an unlock prompt from a background process. The
    graphical login must already have unlocked the default collection.
    """

    _APPLICATION = "org.lemonbot.Lemonbot"
    _MAX_SECRET_BYTES = 5120

    @staticmethod
    def _module() -> Any:
        try:
            return importlib.import_module("secretstorage")
        except ImportError:
            raise SecretStoreError("Linux Secret Service support is not installed") from None

    def _with_collection(self, operation: Callable[[Any], object]) -> object:
        if not sys.platform.startswith("linux"):
            raise SecretStoreError("Linux Secret Service is only available on Linux")
        secretstorage = self._module()
        connection = None
        try:
            connection = secretstorage.dbus_init()
            if not secretstorage.check_service_availability(connection):
                raise SecretStoreError("Linux Secret Service is unavailable")
            collection = secretstorage.get_collection_by_alias(connection, "default")
            if collection.is_locked():
                raise SecretStoreError("Linux Secret Service default collection is locked")
            return operation(collection)
        except SecretStoreError:
            raise
        except Exception:
            raise SecretStoreError("Linux Secret Service operation failed") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    raise SecretStoreError(
                        "Linux Secret Service connection cleanup failed"
                    ) from None

    @classmethod
    def _attributes(cls, name: str) -> dict[str, str]:
        _validate_name(name)
        return {"application": cls._APPLICATION, "credential": name}

    def get(self, name: str) -> str | None:
        attributes = self._attributes(name)

        def read(collection: Any) -> str | None:
            items = list(collection.search_items(attributes))
            if not items:
                return None
            if len(items) != 1:
                raise SecretStoreError("Linux Secret Service credential identity is ambiguous")
            try:
                raw = items[0].get_secret()
                if not isinstance(raw, bytes):
                    raise AttributeError
                return raw.decode("utf-8")
            except (UnicodeError, AttributeError):
                raise SecretStoreError(
                    "Linux Secret Service credential encoding is invalid"
                ) from None

        result = self._with_collection(read)
        if result is not None and not isinstance(result, str):
            raise SecretStoreError("Linux Secret Service returned an invalid credential")
        return result

    def set(self, name: str, value: str) -> None:
        attributes = self._attributes(name)
        if not value:
            raise ValueError("refusing to store an empty credential")
        encoded = value.encode("utf-8")
        if len(encoded) > self._MAX_SECRET_BYTES:
            raise ValueError("credential is too large for Linux Secret Service")

        def write(collection: Any) -> None:
            collection.create_item(
                "Lemonbot credential",
                attributes,
                encoded,
                replace=True,
                content_type="text/plain; charset=utf-8",
            )

        self._with_collection(write)

    def delete(self, name: str) -> bool:
        attributes = self._attributes(name)

        def remove(collection: Any) -> bool:
            items = list(collection.search_items(attributes))
            for item in items:
                item.delete()
            return bool(items)

        result = self._with_collection(remove)
        if not isinstance(result, bool):
            raise SecretStoreError("Linux Secret Service returned an invalid deletion result")
        return result


def platform_secret_store() -> SecretStore:
    if sys.platform.startswith("linux"):
        return LinuxSecretServiceStore()
    raise SecretStoreError("Lemonbot requires Linux Secret Service")
