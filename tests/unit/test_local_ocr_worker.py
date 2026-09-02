from __future__ import annotations

import json

from lemonbot.research.local_ocr_worker import _minimize_ocr_result


def test_local_ocr_returns_only_session_scoped_display_pseudonym() -> None:
    raw_label = "display label"
    result = _minimize_ocr_result(
        [[[[0, 0], [10, 0], [10, 5], [0, 5]], raw_label, 0.98]],
        session_salt=b"a" * 32,
        session_ref="session_one",
    )

    encoded = json.dumps(result)
    assert result["text_count"] == 1
    assert str(result["unverified_display_sender"]).startswith("uds_")
    assert raw_label not in encoded


def test_local_ocr_fails_closed_on_multiple_or_low_confidence_labels() -> None:
    ambiguous = _minimize_ocr_result(
        [
            [[], "first", 0.99],
            [[], "second", 0.95],
        ],
        session_salt=b"a" * 32,
        session_ref="session_one",
    )
    low_confidence = _minimize_ocr_result(
        [[[], "uncertain", 0.74]],
        session_salt=b"a" * 32,
        session_ref="session_one",
    )

    assert ambiguous == {
        "text_count": 2,
        "ambiguous": True,
        "unverified_display_sender": None,
    }
    assert low_confidence == {
        "text_count": 0,
        "ambiguous": False,
        "unverified_display_sender": None,
    }
