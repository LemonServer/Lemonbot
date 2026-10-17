from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from lemonbot.config.settings import AppSettings

_spec = importlib.util.spec_from_file_location(
    "embedding_setup_test", Path(__file__).parents[2] / "scripts/embedding_setup.py"
)
assert _spec and _spec.loader
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)


def configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, python: Path) -> list[list[str]]:
    monkeypatch.setattr(sys, "argv", ["embedding_setup.py", "install"])
    monkeypatch.setattr(setup, "load_settings", lambda path: AppSettings())
    monkeypatch.setattr(setup, "embedding_paths", lambda settings: (tmp_path / "assets", python))
    monkeypatch.setattr(setup.shutil, "which", lambda name: "/synthetic/uv")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        setup.subprocess, "run", lambda argv, **kwargs: calls.append(argv) or SimpleNamespace()
    )
    return calls


def test_setup_rejects_install_into_core_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = configure(monkeypatch, tmp_path, Path(sys.prefix) / "bin/python")
    with pytest.raises(SystemExit, match="must not use the core"):
        setup.main()
    assert not calls


def test_setup_preserves_existing_worker_and_uses_hash_locked_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environment = tmp_path / "worker"
    (environment / "bin").mkdir(parents=True)
    python = environment / "bin/python"
    python.touch()
    marker = environment / "pyvenv.cfg"
    marker.write_text("include-system-site-packages = true\n# preserve me\n")
    calls = configure(monkeypatch, tmp_path, python)
    setup.main()
    setup.main()
    assert marker.read_text().endswith("# preserve me\n")
    assert len(calls) == 4
    assert all("venv" not in argv for argv in calls)
    assert "--require-hashes" in calls[0]
    assert "--no-deps" in calls[1] and "--editable" in calls[1]
    assert environment.stat().st_mode & 0o777 == 0o700


def test_setup_rejects_existing_environment_without_system_tensorrt_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    python = tmp_path / "worker/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    calls = configure(monkeypatch, tmp_path, python)
    with pytest.raises(SystemExit, match="independent system-site-packages"):
        setup.main()
    assert not calls
