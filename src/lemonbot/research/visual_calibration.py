"""Fail-closed evidence model for a future local visual calibration experiment.

This module has no screen-capture, OCR, model, connector, or UI-action code. It
only evaluates minimized structural facts produced in memory by a separately
reviewed local process after explicit xdg-desktop-portal authorization.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VisualCalibrationSample(_StrictModel):
    schema_version: Literal[1] = 1
    run_ref: str = Field(pattern=r"^[a-z0-9_-]{1,64}$")
    portal_authorized: bool
    capture_source: Literal["xdg-desktop-portal"]
    local_processing_only: bool
    cloud_processing_used: bool = False
    client_restart_observed: bool
    lock_cycle_observed: bool
    self_layout_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    peer_layout_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    segment_label_anchor_proven: bool
    continuation_binding_proven: bool
    ambiguous: bool = False


class VisualCalibrationDecision(_StrictModel):
    schema_version: Literal[1] = 1
    calibrated: bool
    reason_codes: tuple[str, ...] = Field(max_length=16)
    self_layout_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    peer_layout_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    direction_clue_only: Literal[True] = True
    identity_authorized: Literal[False] = False
    connector_enrollment_allowed: Literal[False] = False
    reply_generation_allowed: Literal[False] = False

    @model_validator(mode="after")
    def require_consistent_decision(self) -> VisualCalibrationDecision:
        fingerprints = (self.self_layout_fingerprint, self.peer_layout_fingerprint)
        if self.calibrated and (
            self.reason_codes
            or any(value is None for value in fingerprints)
            or fingerprints[0] == fingerprints[1]
        ):
            raise ValueError("calibrated evidence must be distinct and reason-free")
        if not self.calibrated and (
            not self.reason_codes or any(value is not None for value in fingerprints)
        ):
            raise ValueError("failed calibration must expose reasons but no fingerprints")
        return self


class MinimalVisualObservation(_StrictModel):
    schema_version: Literal[1] = 1
    layout_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    ambiguous: bool = False
    unverified_display_sender: str | None = Field(
        default=None, pattern=r"^uds_[0-9a-f]{64}$"
    )


class VisualDirectionClue(_StrictModel):
    schema_version: Literal[1] = 1
    direction_hint: Literal["self", "peer", "unknown"]
    unverified_display_sender: str | None = Field(
        default=None, pattern=r"^uds_[0-9a-f]{64}$"
    )
    identity_authorized: Literal[False] = False
    reply_generation_allowed: Literal[False] = False
    stop_processing: bool


def evaluate_visual_calibration(
    samples: tuple[VisualCalibrationSample, ...],
) -> VisualCalibrationDecision:
    """Require repeatable canary evidence while granting no runtime capability."""
    reasons: list[str] = []
    if len(samples) < 2:
        reasons.append("insufficient_rounds")
    if len({sample.run_ref for sample in samples}) != len(samples):
        reasons.append("duplicate_run_ref")
    if any(not sample.portal_authorized for sample in samples):
        reasons.append("portal_authorization_missing")
    if any(not sample.local_processing_only for sample in samples):
        reasons.append("nonlocal_processing")
    if any(sample.cloud_processing_used for sample in samples):
        reasons.append("cloud_processing_detected")
    if any(sample.ambiguous for sample in samples):
        reasons.append("ambiguous_observation")
    if any(
        sample.self_layout_fingerprint == sample.peer_layout_fingerprint
        for sample in samples
    ):
        reasons.append("direction_layout_not_distinct")

    self_fingerprints = {sample.self_layout_fingerprint for sample in samples}
    peer_fingerprints = {sample.peer_layout_fingerprint for sample in samples}
    if len(self_fingerprints) != 1 or len(peer_fingerprints) != 1:
        reasons.append("direction_layout_drift")
    if not any(sample.client_restart_observed for sample in samples):
        reasons.append("restart_coverage_missing")
    if not any(sample.lock_cycle_observed for sample in samples):
        reasons.append("lock_cycle_coverage_missing")
    if any(not sample.segment_label_anchor_proven for sample in samples):
        reasons.append("segment_label_anchor_unproven")
    if any(not sample.continuation_binding_proven for sample in samples):
        reasons.append("continuation_binding_unproven")

    calibrated = not reasons
    return VisualCalibrationDecision(
        calibrated=calibrated,
        reason_codes=tuple(reasons),
        self_layout_fingerprint=next(iter(self_fingerprints)) if calibrated else None,
        peer_layout_fingerprint=next(iter(peer_fingerprints)) if calibrated else None,
    )


def hash_unverified_display_sender(
    display_sender: str,
    *,
    session_salt: bytes,
    session_ref: str,
) -> str:
    """Create a session-bound local pseudonym without retaining display text."""
    if len(session_salt) < 16:
        raise ValueError("session salt is too short")
    if re.fullmatch(r"[a-z0-9_-]{1,64}", session_ref) is None:
        raise ValueError("session ref is invalid")
    normalized = unicodedata.normalize("NFKC", display_sender).strip()
    if not normalized or len(normalized) > 512:
        raise ValueError("display sender is invalid")
    digest = hashlib.sha256(
        b"lemonbot-unverified-display-sender\0"
        + session_salt
        + b"\0"
        + session_ref.encode("ascii")
        + b"\0"
        + normalized.encode("utf-8")
    ).hexdigest()
    return f"uds_{digest}"


def classify_visual_direction(
    calibration: VisualCalibrationDecision,
    observation: MinimalVisualObservation,
) -> VisualDirectionClue:
    """Return only a direction clue; never turn layout or labels into identity."""
    direction: Literal["self", "peer", "unknown"] = "unknown"
    if calibration.calibrated and not observation.ambiguous:
        if observation.layout_fingerprint == calibration.self_layout_fingerprint:
            direction = "self"
        elif observation.layout_fingerprint == calibration.peer_layout_fingerprint:
            direction = "peer"
    return VisualDirectionClue(
        direction_hint=direction,
        unverified_display_sender=observation.unverified_display_sender,
        stop_processing=direction == "unknown",
    )
