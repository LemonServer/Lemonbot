"""Durable, sequential promotion gate for the personal WeChat lab adapter."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal

from lemonbot.config.settings import WechatUIASettings
from lemonbot.domain import AuditRecord
from lemonbot.storage import CoreRepository

UIAStage = Literal["observe", "draft", "reply", "proactive"]
_STAGES: tuple[UIAStage, ...] = ("observe", "draft", "reply", "proactive")
_STATE_KEY = "uia:promotion_gate:v1"


def enrollment_fingerprint(settings: WechatUIASettings) -> str:
    payload = {
        "account": settings.expected_account,
        "windows_user": settings.expected_windows_user,
        "process": settings.expected_process_name,
        "executable_path": settings.expected_executable_path.casefold(),
        "executable_sha256": settings.expected_executable_sha256,
        "client_version": settings.enrolled_client_version,
        "selector_signature": settings.enrolled_selector_signature,
        "selector_bundle_path": settings.selector_bundle_path.casefold(),
        "allow_chat_ids": sorted(settings.allow_chat_ids),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def effective_uia_stage(
    repository: CoreRepository,
    settings: WechatUIASettings,
) -> UIAStage:
    fingerprint = enrollment_fingerprint(settings)
    stored = await repository.runtime_state(_STATE_KEY)
    stored_stage = stored.get("stage") if stored else None
    stored_fingerprint = stored.get("fingerprint") if stored else None
    if stored_stage not in _STAGES or stored_fingerprint != fingerprint:
        reason = "initialized" if stored is None else "enrollment_changed"
        await repository.set_runtime_state(
            _STATE_KEY,
            {
                "stage": "observe",
                "fingerprint": fingerprint,
                "verified_at": datetime.now(UTC).isoformat(),
            },
        )
        await repository.append_audit(
            AuditRecord(
                action="uia.stage_gate",
                outcome="reset_observe",
                channel="wechat_personal_lab",
                detail={"reason": reason},
            )
        )
        stored_stage = "observe"
    requested_index = _STAGES.index(settings.stage)
    verified_index = _STAGES.index(stored_stage)
    return _STAGES[min(requested_index, verified_index)]


async def promote_uia_stage(
    repository: CoreRepository,
    settings: WechatUIASettings,
    target: UIAStage,
) -> UIAStage:
    await effective_uia_stage(repository, settings)
    stored = await repository.runtime_state(_STATE_KEY)
    assert stored is not None
    current = stored.get("stage")
    if current not in _STAGES:
        raise RuntimeError("UIA promotion state is invalid")
    current_index = _STAGES.index(current)
    if current_index >= len(_STAGES) - 1 or target != _STAGES[current_index + 1]:
        raise ValueError("UIA stage promotion must advance exactly one step")
    await repository.set_runtime_state(
        _STATE_KEY,
        {
            "stage": target,
            "fingerprint": enrollment_fingerprint(settings),
            "verified_at": datetime.now(UTC).isoformat(),
        },
    )
    await repository.append_audit(
        AuditRecord(
            action="uia.stage_gate",
            outcome="promoted",
            channel="wechat_personal_lab",
            detail={"from": current, "to": target},
        )
    )
    return target
