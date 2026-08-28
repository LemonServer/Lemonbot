from __future__ import annotations

import os
from pathlib import Path

import pytest

from lemonbot.supervisor.processes import _absolute_executable


def test_absolute_executable_keeps_virtualenv_style_symlink(tmp_path: Path) -> None:
    target = tmp_path / "python-target"
    target.write_bytes(b"interpreter")
    link = tmp_path / "python"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("creating symlinks is unavailable for this test user")

    result = _absolute_executable(link)

    assert result == Path(os.path.abspath(link))
    assert result != target.resolve()


def test_absolute_executable_rejects_missing_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="worker executable is invalid"):
        _absolute_executable(tmp_path / "missing")
