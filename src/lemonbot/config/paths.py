from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path

from lemonbot.config.settings import AppSettings


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    root: Path
    profile: str

    @classmethod
    def from_settings(cls, settings: AppSettings) -> RuntimePaths:
        configured = settings.runtime.data_root.strip()
        if configured:
            root = Path(configured).expanduser().resolve()
        elif local := os.environ.get("LOCALAPPDATA"):
            root = (Path(local) / "Lemonbot").resolve()
        else:
            root = user_data_path("Lemonbot").resolve()
        return cls(root=root, profile=settings.profile)

    @property
    def database(self) -> Path:
        return self.root / f"{self.profile}.db"

    @property
    def objects(self) -> Path:
        return self.root / "objects" / self.profile

    @property
    def quarantine(self) -> Path:
        return self.root / "quarantine" / self.profile

    @property
    def backups(self) -> Path:
        return self.root / "backups" / self.profile

    @property
    def lock_file(self) -> Path:
        return self.root / "runtime" / f"{self.profile}.lock"

    def ensure(self) -> None:
        for path in (self.root, self.objects, self.quarantine, self.backups, self.lock_file.parent):
            path.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            for path in (self.root, self.objects, self.quarantine, self.backups):
                path.chmod(0o700)
