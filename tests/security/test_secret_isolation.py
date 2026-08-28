from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from lemonbot.security import secrets
from lemonbot.security.secrets import (
    LinuxSecretServiceStore,
    NamespacedSecretStore,
    SecretStore,
    SecretStoreError,
)


@dataclass
class MemorySecretStore(SecretStore):
    values: dict[str, str] = field(default_factory=dict)

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> bool:
        return self.values.pop(name, None) is not None


def test_prod_and_lab_credentials_are_separate_namespaces() -> None:
    inner = MemorySecretStore()
    prod = NamespacedSecretStore(inner, "prod")
    lab = NamespacedSecretStore(inner, "lab")

    prod.set("deepseek_api_key", "prod-value")
    lab.set("deepseek_api_key", "lab-value")

    assert prod.get("deepseek_api_key") == "prod-value"
    assert lab.get("deepseek_api_key") == "lab-value"
    assert inner.values == {
        "prod_deepseek_api_key": "prod-value",
        "lab_deepseek_api_key": "lab-value",
    }


@pytest.mark.parametrize("name", ["", "../secret", "UPPER", "secret:name", "a" * 129])
def test_secret_lookup_names_cannot_escape_the_namespace(name: str) -> None:
    store = NamespacedSecretStore(MemorySecretStore(), "prod")

    with pytest.raises(ValueError):
        store.get(name)


class _FakeSecretItem:
    def __init__(self, collection: _FakeCollection, key: str) -> None:
        self.collection = collection
        self.key = key

    def get_secret(self) -> bytes:
        return self.collection.values[self.key]

    def delete(self) -> None:
        self.collection.values.pop(self.key, None)


class _FakeCollection:
    def __init__(self, *, locked: bool = False) -> None:
        self.locked = locked
        self.values: dict[str, bytes] = {}

    def is_locked(self) -> bool:
        return self.locked

    def search_items(self, attributes: dict[str, str]) -> list[_FakeSecretItem]:
        key = attributes["credential"]
        return [_FakeSecretItem(self, key)] if key in self.values else []

    def create_item(
        self,
        _label: str,
        attributes: dict[str, str],
        value: bytes,
        *,
        replace: bool,
        content_type: str,
    ) -> _FakeSecretItem:
        assert replace
        assert content_type == "text/plain; charset=utf-8"
        key = attributes["credential"]
        self.values[key] = value
        return _FakeSecretItem(self, key)


class _FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeSecretStorage:
    def __init__(self, collection: _FakeCollection) -> None:
        self.collection = collection
        self.connections: list[_FakeConnection] = []

    def dbus_init(self) -> _FakeConnection:
        connection = _FakeConnection()
        self.connections.append(connection)
        return connection

    def check_service_availability(self, _connection: _FakeConnection) -> bool:
        return True

    def get_collection_by_alias(
        self, _connection: _FakeConnection, alias: str
    ) -> _FakeCollection:
        assert alias == "default"
        return self.collection


def test_linux_secret_service_round_trip_closes_each_dbus_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _FakeSecretStorage(_FakeCollection())
    monkeypatch.setattr(secrets.sys, "platform", "linux")
    monkeypatch.setattr(LinuxSecretServiceStore, "_module", staticmethod(lambda: module))
    store = LinuxSecretServiceStore()

    store.set("lab_deepseek_api_key", "secret-value")
    assert store.get("lab_deepseek_api_key") == "secret-value"
    assert store.delete("lab_deepseek_api_key")
    assert store.get("lab_deepseek_api_key") is None
    assert all(connection.closed for connection in module.connections)


def test_linux_secret_service_refuses_locked_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _FakeSecretStorage(_FakeCollection(locked=True))
    monkeypatch.setattr(secrets.sys, "platform", "linux")
    monkeypatch.setattr(LinuxSecretServiceStore, "_module", staticmethod(lambda: module))

    with pytest.raises(SecretStoreError, match="collection is locked"):
        LinuxSecretServiceStore().get("lab_deepseek_api_key")
