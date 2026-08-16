from __future__ import annotations

from pathlib import Path

import pytest

from lemonbot.config.settings import AppSettings, load_settings


def test_example_configuration_is_valid() -> None:
    settings = load_settings(Path("config/lemonbot.example.toml"))
    assert settings.profile == "prod"
    assert settings.models.flash_model == "deepseek-v4-flash"
    assert not settings.models.budget.enabled
    assert not settings.mcp.enabled
    assert not settings.mcp.servers


def test_cloud_budget_requires_limits_and_prices() -> None:
    raw = AppSettings().model_dump(mode="python")
    raw["models"]["budget"]["enabled"] = True
    with pytest.raises(ValueError, match="explicit prices"):
        AppSettings.model_validate(raw)


def test_personal_wechat_requires_lab_profile() -> None:
    raw = AppSettings().model_dump(mode="python")
    raw["runtime"]["connector"] = "wechat_uia"
    raw["wechat_uia"].update(
        {
            "enabled": True,
            "expected_account": "account-sha256",
            "expected_windows_user": "lab-user",
            "expected_executable_path": r"C:\Program Files\Tencent\WeChat\WeChat.exe",
            "expected_executable_sha256": "c" * 64,
            "enrolled_client_version": "test-version",
            "enrolled_selector_signature": "selector-sha256",
            "selector_bundle_path": "selectors.json",
            "allow_chat_ids": ["chat-1"],
        }
    )
    with pytest.raises(ValueError, match="lab profile"):
        AppSettings.model_validate(raw)


def test_personal_wechat_requires_absolute_hash_pinned_executable() -> None:
    raw = AppSettings().model_dump(mode="python")
    raw["profile"] = "lab"
    raw["runtime"]["connector"] = "wechat_uia"
    raw["wechat_uia"].update(
        {
            "enabled": True,
            "expected_account": "account-sha256",
            "expected_windows_user": "lab-user",
            "expected_executable_path": "WeChat.exe",
            "expected_executable_sha256": "NOT-A-SHA256",
            "enrolled_client_version": "test-version",
            "enrolled_selector_signature": "selector-sha256",
            "selector_bundle_path": "selectors.json",
            "allow_chat_ids": ["chat-1"],
        }
    )

    with pytest.raises(ValueError, match="absolute local-drive path"):
        AppSettings.model_validate(raw)

    raw["wechat_uia"]["expected_executable_path"] = (
        r"C:\Program Files\Tencent\WeChat\WeChat.exe"
    )
    with pytest.raises(ValueError, match="64 lowercase hex"):
        AppSettings.model_validate(raw)


def test_enabled_vision_is_pinned_to_the_official_provider_and_model() -> None:
    raw = AppSettings().model_dump(mode="python")
    raw["vision"].update(
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
    raw["models"].update(
        {
            "provider": "openai_compatible",
            "base_url": "http://192.0.2.10:11434/v1",
            "api_key_secret_name": "",
        }
    )
    with pytest.raises(ValueError, match="loopback-only"):
        AppSettings.model_validate(raw)


def test_mcp_broker_requires_an_explicitly_enabled_server() -> None:
    raw = AppSettings().model_dump(mode="python")
    raw["mcp"]["enabled"] = True

    with pytest.raises(ValueError, match="explicitly enabled server"):
        AppSettings.model_validate(raw)
