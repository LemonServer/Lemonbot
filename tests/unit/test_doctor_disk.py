from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lemonbot.doctor import _data_disk_check


def test_doctor_reports_low_data_disk_as_attachment_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[Path] = []

    def disk_usage(path: Path) -> SimpleNamespace:
        observed.append(path)
        return SimpleNamespace(free=512)

    monkeypatch.setattr("lemonbot.doctor.shutil.disk_usage", disk_usage)

    check = _data_disk_check(tmp_path / "not-created" / "data", minimum_free_bytes=1024)

    assert not check.ok
    assert not check.required
    assert check.name == "data-disk-free"
    assert "free" in check.detail
    assert observed == [tmp_path]


def test_doctor_accepts_data_disk_at_reserve_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "lemonbot.doctor.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=1024),
    )

    check = _data_disk_check(tmp_path, minimum_free_bytes=1024)

    assert check.ok
    assert "attachment reserve" in check.detail
