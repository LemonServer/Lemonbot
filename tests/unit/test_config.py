from __future__ import annotations

from pathlib import Path

import pytest

from lemonbot.config.settings import AppSettings, load_settings


def enabled_atspi_raw() -> dict[str, object]:
    raw = AppSettings().model_dump(mode="python")
    raw["profile"] = "lab"
    raw["runtime"]["connector"] = "wechat_atspi"  # type: ignore[index]
    raw["wechat_atspi"].update(  # type: ignore[union-attr]
        {
            "enabled": True,
            "expected_linux_user": "lemon",
            "worker_python_path": "/home/lemon/.local/share/Lemonbot/atspi-worker/bin/python",
            "expected_executable_sha256": "a" * 64,
            "enrolled_client_version": "4.1.1.8",
            "account_fingerprint": "b" * 64,
            "ui_signature": "c" * 64,
            "enrollment_bundle_path": "/home/lemon/.config/Lemonbot/atspi.json",
            "enrollment_bundle_sha256": "d" * 64,
            "allow_target_refs": ["private_test", "group_test"],
        }
    )
    return raw


def test_example_configuration_is_safe_linux_lab() -> None:
    settings = load_settings(Path("config/lemonbot.example.toml"))
    assert settings.schema_version == 2
    assert settings.profile == "lab"
    assert settings.models.provider == "disabled"
    assert not settings.wechat_atspi.enabled


def test_cloud_budget_requires_limits_and_prices() -> None:
    raw = AppSettings().model_dump(mode="python")
    raw["models"]["budget"]["enabled"] = True  # type: ignore[index]
    with pytest.raises(ValueError, match="explicit prices"):
        AppSettings.model_validate(raw)


def test_atspi_requires_lab_profile_and_disabled_model() -> None:
    raw = enabled_atspi_raw()
    raw["profile"] = "prod"
    with pytest.raises(ValueError, match="lab profile"):
        AppSettings.model_validate(raw)
    raw["profile"] = "lab"
    raw["models"]["provider"] = "deepseek"  # type: ignore[index]
    with pytest.raises(ValueError, match="provider='disabled'"):
        AppSettings.model_validate(raw)


def test_atspi_connector_is_closed_while_direction_is_unproven() -> None:
    with pytest.raises(ValueError, match="direction is unproven"):
        AppSettings.model_validate(enabled_atspi_raw())


def test_atspi_requires_absolute_paths_hashes_and_safe_target_refs() -> None:
    raw = enabled_atspi_raw()
    raw["wechat_atspi"]["expected_executable_path"] = "wechat"  # type: ignore[index]
    with pytest.raises(ValueError, match="absolute POSIX"):
        AppSettings.model_validate(raw)
    raw = enabled_atspi_raw()
    raw["wechat_atspi"]["ui_signature"] = "NOT-A-HASH"  # type: ignore[index]
    with pytest.raises(ValueError, match="64 lowercase hex"):
        AppSettings.model_validate(raw)
    raw = enabled_atspi_raw()
    raw["wechat_atspi"]["allow_target_refs"] = ["unsafe target"]  # type: ignore[index]
    with pytest.raises(ValueError, match="safe identifiers"):
        AppSettings.model_validate(raw)


def test_schema_v1_and_removed_channels_are_rejected() -> None:
    raw = AppSettings().model_dump(mode="python")
    raw["schema_version"] = 1
    raw["wecom"] = {"enabled": False}
    with pytest.raises(ValueError):
        AppSettings.model_validate(raw)


def test_enabled_vision_is_pinned_to_official_provider() -> None:
    raw = AppSettings().model_dump(mode="python")
    raw["vision"].update(  # type: ignore[union-attr]
        {
            "enabled": True,
            "input_cny_per_million": "0",
            "output_cny_per_million": "0",
            "base_url": "http://127.0.0.1:9999/v1",
        }
    )
    with pytest.raises(ValueError, match="official Zhipu endpoint"):
        AppSettings.model_validate(raw)


def test_plaintext_openai_compatible_provider_must_be_loopback() -> None:
    raw = AppSettings().model_dump(mode="python")
    raw["models"].update(  # type: ignore[union-attr]
        {
            "provider": "openai_compatible",
            "base_url": "http://192.0.2.10:11434/v1",
            "api_key_secret_name": "",
        }
    )
    with pytest.raises(ValueError, match="loopback-only"):
        AppSettings.model_validate(raw)


def test_mcp_requires_enabled_server() -> None:
    raw = AppSettings().model_dump(mode="python")
    raw["mcp"]["enabled"] = True  # type: ignore[index]
    with pytest.raises(ValueError, match="explicitly enabled server"):
        AppSettings.model_validate(raw)
