from __future__ import annotations

import io
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image

from lemonbot.models import (
    BudgetLimits,
    BudgetManager,
    IsolatedVisionBackend,
    ModelPrice,
    VisionFileRequest,
    VisionProviderConfig,
    VisionWorkerConfig,
)
from lemonbot.supervisor import WorkerSupervisor
from lemonbot.tools.object_store import ContentAddressedStore

_RUNTIME_WORKER_CWD = Path("src/lemonbot").resolve()


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (24, 16), "orange").save(output, format="PNG")
    return output.getvalue()


def _budget() -> BudgetManager:
    return BudgetManager(
        limits=BudgetLimits(daily=Decimal(10), monthly=Decimal(100)),
        prices={("zhipu", "glm-4.6v-flash"): ModelPrice(Decimal(1), Decimal(1))},
    )


@pytest.mark.integration
@pytest.mark.linux
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux Secret Service boundary")
async def test_real_vision_worker_decodes_then_explicitly_falls_back_without_secret(
    tmp_path: Path,
) -> None:
    root = tmp_path / "objects"
    stored = ContentAddressedStore(root).put_bytes(_png_bytes())
    budget = _budget()
    supervisor = WorkerSupervisor()
    vision: IsolatedVisionBackend | None = None
    try:
        vision = await IsolatedVisionBackend.create(
            config=VisionWorkerConfig(
                profile="lab",
                objects_root=str(root.resolve()),
                provider=VisionProviderConfig(
                    # A deliberately absent Credential Manager lookup name.
                    secret_name="zhipu_vision_worker_test_missing_4f67a0",  # noqa: S106
                    timeout_seconds=5,
                ),
                ocr_enabled=False,
            ),
            budget=budget,
            supervisor=supervisor,
            python_executable=Path(sys.executable),
            cwd=_RUNTIME_WORKER_CWD,
            rpc_timeout_seconds=20,
        )
        result = await vision.analyze_file(
            VisionFileRequest(
                object_path=str(stored.path.resolve()),
                expected_sha256=stored.sha256,
                expected_size=stored.size,
                declared_media_type="image/png",
            )
        )
        assert not result.result.semantic_available
        assert result.result.model is None
        assert result.result.limitation == "semantic vision and local OCR unavailable"
        assert not result.provider_call_started
        snapshot = await budget.snapshot()
        assert snapshot.daily_spent == 0
        assert snapshot.daily_reserved == 0
    finally:
        if vision is not None:
            await vision.aclose()
        await supervisor.stop_all()
