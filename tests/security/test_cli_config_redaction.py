from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from lemonbot.cli import app


def test_invalid_config_never_echoes_an_accidentally_embedded_secret(tmp_path: Path) -> None:
    marker = "sk-" + "test-value-that-must-never-be-echoed"
    config = tmp_path / "unsafe.toml"
    config.write_text(
        f"schema_version = 1\nprofile = 'prod'\n[models]\napi_key = '{marker}'\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["doctor", "--config", str(config)])

    assert result.exit_code == 2
    assert marker not in result.output
    assert "models.api_key" in result.output
