from __future__ import annotations

import pytest
from pydantic import ValidationError

from lemonbot.research.visual_calibration import (
    MinimalVisualObservation,
    VisualCalibrationDecision,
    VisualCalibrationSample,
    classify_visual_direction,
    evaluate_visual_calibration,
    hash_unverified_display_sender,
)


def _sample(
    run_ref: str,
    *,
    restart: bool = False,
    lock_cycle: bool = False,
    **overrides: object,
) -> VisualCalibrationSample:
    values: dict[str, object] = {
        "run_ref": run_ref,
        "portal_authorized": True,
        "capture_source": "xdg-desktop-portal",
        "local_processing_only": True,
        "cloud_processing_used": False,
        "client_restart_observed": restart,
        "lock_cycle_observed": lock_cycle,
        "self_layout_fingerprint": "a" * 64,
        "peer_layout_fingerprint": "b" * 64,
        "segment_label_anchor_proven": True,
        "continuation_binding_proven": True,
        "ambiguous": False,
    }
    values.update(overrides)
    return VisualCalibrationSample.model_validate(values)


def test_calibration_requires_two_stable_rounds_and_lifecycle_coverage() -> None:
    decision = evaluate_visual_calibration(
        (
            _sample("restart_round", restart=True),
            _sample("lock_round", lock_cycle=True),
        )
    )

    assert decision.calibrated
    assert decision.reason_codes == ()
    assert decision.direction_clue_only
    assert not decision.identity_authorized
    assert not decision.connector_enrollment_allowed
    assert not decision.reply_generation_allowed


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"portal_authorized": False}, "portal_authorization_missing"),
        ({"local_processing_only": False}, "nonlocal_processing"),
        ({"cloud_processing_used": True}, "cloud_processing_detected"),
        ({"ambiguous": True}, "ambiguous_observation"),
        ({"self_layout_fingerprint": "b" * 64}, "direction_layout_not_distinct"),
        ({"segment_label_anchor_proven": False}, "segment_label_anchor_unproven"),
        ({"continuation_binding_proven": False}, "continuation_binding_unproven"),
    ],
)
def test_calibration_fails_closed_on_unsafe_or_uncertain_evidence(
    overrides: dict[str, object], reason: str
) -> None:
    decision = evaluate_visual_calibration(
        (
            _sample("restart_round", restart=True, **overrides),
            _sample("lock_round", lock_cycle=True),
        )
    )

    assert not decision.calibrated
    assert reason in decision.reason_codes
    assert decision.self_layout_fingerprint is None
    assert not decision.reply_generation_allowed


def test_direction_classification_stops_on_unknown_layout() -> None:
    calibration = evaluate_visual_calibration(
        (
            _sample("restart_round", restart=True),
            _sample("lock_round", lock_cycle=True),
        )
    )
    known = classify_visual_direction(
        calibration,
        MinimalVisualObservation(layout_fingerprint="b" * 64),
    )
    unknown = classify_visual_direction(
        calibration,
        MinimalVisualObservation(layout_fingerprint="c" * 64),
    )

    assert known.direction_hint == "peer"
    assert not known.stop_processing
    assert not known.identity_authorized
    assert not known.reply_generation_allowed
    assert unknown.direction_hint == "unknown"
    assert unknown.stop_processing


def test_display_sender_hash_is_session_scoped_and_raw_text_is_not_a_field() -> None:
    raw_label = "local display label"
    first = hash_unverified_display_sender(
        raw_label, session_salt=b"a" * 32, session_ref="session_one"
    )
    other_session = hash_unverified_display_sender(
        raw_label, session_salt=b"a" * 32, session_ref="session_two"
    )
    other_salt = hash_unverified_display_sender(
        raw_label, session_salt=b"b" * 32, session_ref="session_one"
    )

    assert len({first, other_session, other_salt}) == 3
    observation = MinimalVisualObservation(
        layout_fingerprint="a" * 64,
        unverified_display_sender=first,
    )
    assert raw_label not in observation.model_dump_json()
    with pytest.raises(ValidationError):
        MinimalVisualObservation.model_validate(
            {
                "layout_fingerprint": "a" * 64,
                "raw_display_sender": raw_label,
            }
        )


def test_calibrated_decision_cannot_be_forged_without_distinct_evidence() -> None:
    with pytest.raises(ValidationError):
        VisualCalibrationDecision(
            calibrated=True,
            reason_codes=(),
            self_layout_fingerprint="a" * 64,
            peer_layout_fingerprint="a" * 64,
        )
