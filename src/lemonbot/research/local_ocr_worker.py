"""Local-only OCR minimizer for one sender-label crop.

Input is one bounded PNG on stdin. Output contains only a count, ambiguity bit,
and an optional session-scoped salted display-label pseudonym.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from typing import Any

from lemonbot.research.visual_calibration import hash_unverified_display_sender

MAX_LABEL_PNG_BYTES = 4 * 1024 * 1024


def _minimize_ocr_result(
    result: Any,
    *,
    session_salt: bytes,
    session_ref: str,
) -> dict[str, object]:
    candidates: list[str] = []
    if isinstance(result, list):
        for item in result[:16]:
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                continue
            text = item[1]
            score = item[2]
            if (
                not isinstance(text, str)
                or not isinstance(score, (int, float))
                or isinstance(score, bool)
                or not math.isfinite(float(score))
                or float(score) < 0.75
            ):
                continue
            normalized = text.strip()
            if normalized and len(normalized) <= 128:
                candidates.append(normalized)
    if len(candidates) != 1:
        return {
            "text_count": len(candidates),
            "ambiguous": len(candidates) > 1,
            "unverified_display_sender": None,
        }
    return {
        "text_count": 1,
        "ambiguous": False,
        "unverified_display_sender": hash_unverified_display_sender(
            candidates[0],
            session_salt=session_salt,
            session_ref=session_ref,
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local sender-label OCR minimizer")
    parser.add_argument("--session-salt", required=True)
    parser.add_argument("--session-ref", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if len(arguments.session_salt) != 64:
            raise ValueError("invalid salt")
        session_salt = bytes.fromhex(arguments.session_salt)
        payload = sys.stdin.buffer.read(MAX_LABEL_PNG_BYTES + 1)
        if len(payload) > MAX_LABEL_PNG_BYTES or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("invalid image")
        from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-untyped]

        result, _elapsed = RapidOCR()(payload)
        minimized = _minimize_ocr_result(
            result,
            session_salt=session_salt,
            session_ref=arguments.session_ref,
        )
    except Exception:
        print(json.dumps({"error": "LocalOCRFailure"}, sort_keys=True))
        return 1
    print(json.dumps(minimized, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
