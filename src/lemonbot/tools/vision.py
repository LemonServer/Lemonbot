from __future__ import annotations

import hashlib
import io
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from PIL import Image, ImageOps, UnidentifiedImageError


class ImageRejected(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedImage:
    content: bytes
    media_type: str
    sha256: str
    width: int
    height: int


class ImagePreprocessor:
    ALLOWED_FORMATS: ClassVar[dict[str, tuple[str, str]]] = {
        "JPEG": ("JPEG", "image/jpeg"),
        "PNG": ("PNG", "image/png"),
        "WEBP": ("WEBP", "image/webp"),
    }

    def __init__(
        self,
        *,
        max_file_bytes: int = 10 * 1024 * 1024,
        max_pixels: int = 20_000_000,
        max_dimension: int = 4096,
    ) -> None:
        self._max_file_bytes = max_file_bytes
        self._max_pixels = max_pixels
        self._max_dimension = max_dimension

    def prepare(self, source: Path | bytes) -> PreparedImage:
        raw = source if isinstance(source, bytes) else source.read_bytes()
        if not raw or len(raw) > self._max_file_bytes:
            raise ImageRejected("image is empty or exceeds the configured byte limit")
        self._validate_file_header(raw)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as probe:
                    source_format = probe.format
                    width, height = probe.size
                    frames = getattr(probe, "n_frames", 1)
                    probe.verify()
        except (
            UnidentifiedImageError,
            OSError,
            SyntaxError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as exc:
            raise ImageRejected("file is not a valid supported image") from exc
        if source_format not in self.ALLOWED_FORMATS or frames != 1:
            raise ImageRejected("only single-frame JPEG, PNG and WebP images are accepted")
        if width <= 0 or height <= 0 or width * height > self._max_pixels:
            raise ImageRejected("image dimensions exceed the configured pixel limit")

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as opened:
                    sanitized = ImageOps.exif_transpose(opened).convert("RGB")
                    sanitized.thumbnail((self._max_dimension, self._max_dimension))
                    output = io.BytesIO()
                    sanitized.save(output, format="JPEG", quality=88, optimize=True)
                    clean = output.getvalue()
                    clean_width, clean_height = sanitized.size
        except (
            OSError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as exc:
            raise ImageRejected("image could not be decoded safely") from exc
        if not clean or len(clean) > 8 * 1024 * 1024:
            raise ImageRejected("sanitized image exceeds the safe byte limit")
        return PreparedImage(
            content=clean,
            media_type="image/jpeg",
            sha256=hashlib.sha256(clean).hexdigest(),
            width=clean_width,
            height=clean_height,
        )

    @staticmethod
    def _validate_file_header(raw: bytes) -> None:
        jpeg = len(raw) >= 3 and raw[:3] == b"\xff\xd8\xff"
        png = raw.startswith(b"\x89PNG\r\n\x1a\n")
        webp = len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
        if not (jpeg or png or webp):
            raise ImageRejected("image file header is not JPEG, PNG or WebP")


class RapidOCRReader:
    def __init__(self) -> None:
        self._engine: Any | None = None

    def available(self) -> bool:
        try:
            import rapidocr_onnxruntime  # type: ignore  # noqa: F401
        except ImportError:
            return False
        return True

    def read(self, image: PreparedImage) -> str:
        if self._engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as exc:
                raise RuntimeError("RapidOCR is not installed; install the 'vision' extra") from exc
            self._engine = RapidOCR()
        result, _ = self._engine(image.content)
        if not result:
            return ""
        lines = [str(item[1]) for item in result if len(item) >= 2]
        return "\n".join(lines)
