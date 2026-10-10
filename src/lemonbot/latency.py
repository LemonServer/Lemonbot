"""Monotonic stage timings without message bodies or upstream identifiers."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)


def record(stage: str, started: float) -> None:
    logger.info("latency stage=%s seconds=%.3f", stage, time.monotonic() - started)


@contextmanager
def measure(stage: str) -> Iterator[None]:
    started = time.monotonic()
    try:
        yield
    finally:
        record(stage, started)
