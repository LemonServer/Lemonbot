from __future__ import annotations

from pathlib import Path

from lemonbot.config import AppSettings, RuntimePaths
from lemonbot.config.settings import RuntimeSettings


def test_prod_and_lab_use_separate_databases_and_attachment_roots(tmp_path: Path) -> None:
    prod = RuntimePaths.from_settings(
        AppSettings(profile="prod", runtime=RuntimeSettings(data_root=str(tmp_path)))
    )
    lab = RuntimePaths.from_settings(
        AppSettings(profile="lab", runtime=RuntimeSettings(data_root=str(tmp_path)))
    )

    assert prod.database == tmp_path / "prod.db"
    assert lab.database == tmp_path / "lab.db"
    assert prod.database != lab.database
    assert prod.objects != lab.objects
    assert prod.quarantine != lab.quarantine
    assert prod.backups != lab.backups
