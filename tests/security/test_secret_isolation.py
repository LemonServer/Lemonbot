from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from lemonbot.security.secrets import NamespacedSecretStore, SecretStore


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
