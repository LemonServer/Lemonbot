from __future__ import annotations

from pathlib import Path

import pytest

from lemonbot.tools.vault import FileVault, VaultError, VaultRoot


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "C:drive-relative.txt",
        "C:\\absolute.txt",
        "\\rooted.txt",
        "file.txt:secret-stream",
        "CON.txt",
        "trailing-dot./file.txt",
        "trailing-space /file.txt",
        "wild*.txt",
        "double//separator.txt",
    ],
)
def test_vault_rejects_windows_path_aliases(tmp_path: Path, path: str) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    vault = FileVault([VaultRoot("docs", root, writable=True)])

    with pytest.raises(VaultError, match="normalized relative"):
        vault.create_text("docs", path, "blocked")


def test_vault_reads_only_inside_named_root(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    (root / "note.txt").write_text("safe", encoding="utf-8")
    vault = FileVault([VaultRoot("docs", root)])

    assert vault.read_text("docs", "note.txt") == "safe"
    with pytest.raises(VaultError):
        vault.read_text("missing", "note.txt")


def test_vault_never_overwrites_and_versions(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    vault = FileVault([VaultRoot("out", root, writable=True)])

    first = vault.create_text("out", "answer.txt", "one")
    second = vault.create_text("out", "answer.txt", "two")

    assert first.name == "answer.txt"
    assert second.name == "answer.v1.txt"
    assert first.read_text("utf-8") == "one"
    assert second.read_text("utf-8") == "two"


def test_vault_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable for this Windows user")
    vault = FileVault([VaultRoot("docs", root)])
    with pytest.raises(VaultError):
        vault.read_text("docs", "link/secret.txt")


def test_vault_rejects_final_symlink_even_when_target_stays_inside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("not exposed through an alias", encoding="utf-8")
    link = root / "alias.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable for this Windows user")

    vault = FileVault([VaultRoot("docs", root)])

    with pytest.raises(VaultError, match="links and junctions"):
        vault.read_text("docs", "alias.txt")
