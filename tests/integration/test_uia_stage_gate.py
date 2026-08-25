from __future__ import annotations

from pathlib import Path

import pytest

from lemonbot.config.settings import WechatUIASettings
from lemonbot.connectors import effective_uia_stage, promote_uia_stage
from lemonbot.storage import CoreRepository, Database


def _settings(**overrides: object) -> WechatUIASettings:
    values: dict[str, object] = {
        "enabled": True,
        "stage": "proactive",
        "expected_account": "a" * 64,
        "expected_windows_user": "lemon-lab",
        "expected_process_name": "WeChat.exe",
        "expected_executable_path": r"C:\Program Files\Tencent\WeChat.exe",
        "expected_executable_sha256": "b" * 64,
        "enrolled_client_version": "4.1.2.3",
        "enrolled_selector_signature": "c" * 64,
        "selector_bundle_path": r"C:\Lemonbot\selectors.json",
        "allow_chat_ids": ("chat-1",),
    }
    values.update(overrides)
    return WechatUIASettings.model_validate(values)


async def test_uia_stage_gate_starts_observe_and_only_advances_one_step(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "lab.db")
    await database.initialise()
    repository = CoreRepository(database)
    settings = _settings()
    try:
        assert await effective_uia_stage(repository, settings) == "observe"
        with pytest.raises(ValueError, match="exactly one step"):
            await promote_uia_stage(repository, settings, "reply")

        assert await promote_uia_stage(repository, settings, "draft") == "draft"
        assert await effective_uia_stage(repository, settings) == "draft"
        assert await promote_uia_stage(repository, settings, "reply") == "reply"
        assert await promote_uia_stage(repository, settings, "proactive") == "proactive"
        assert await effective_uia_stage(repository, settings) == "proactive"
    finally:
        await database.close()


async def test_uia_enrollment_change_resets_the_durable_gate_to_observe(
    tmp_path: Path,
) -> None:
    database = Database.from_path(tmp_path / "lab.db")
    await database.initialise()
    repository = CoreRepository(database)
    settings = _settings()
    try:
        await promote_uia_stage(repository, settings, "draft")
        assert await effective_uia_stage(repository, settings) == "draft"

        changed = _settings(expected_executable_sha256="d" * 64)
        assert await effective_uia_stage(repository, changed) == "observe"
        state = await repository.runtime_state("uia:promotion_gate:v1")
        assert state is not None
        assert state["stage"] == "observe"
    finally:
        await database.close()
