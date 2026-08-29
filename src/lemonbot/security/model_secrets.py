from __future__ import annotations

import asyncio
from dataclasses import dataclass

from lemonbot.models.secrets import SecretStore as AsyncModelSecretStore
from lemonbot.security.secrets import SecretStore


@dataclass(slots=True)
class AsyncSecretStoreAdapter(AsyncModelSecretStore):
    """Keeps blocking Secret Service calls out of the asyncio event loop."""

    inner: SecretStore

    async def get_secret(self, name: str) -> str | None:
        return await asyncio.to_thread(self.inner.get, name)
