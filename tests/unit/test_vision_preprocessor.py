from __future__ import annotations

import io

import pytest
from PIL import Image

from lemonbot.tools.vision import ImagePreprocessor, ImageRejected


def encoded_image(format_name: str = "PNG", size: tuple[int, int] = (16, 16)) -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", size, (255, 0, 0, 128)).save(output, format=format_name)
    return output.getvalue()


def test_sanitizes_to_metadata_free_jpeg() -> None:
    result = ImagePreprocessor().prepare(encoded_image())
    assert result.media_type == "image/jpeg"
    assert result.width == 16 and result.height == 16
    with Image.open(io.BytesIO(result.content)) as image:
        assert image.format == "JPEG"
        assert not image.getexif()


def test_rejects_animated_image() -> None:
    first = Image.new("RGB", (8, 8), "red")
    second = Image.new("RGB", (8, 8), "blue")
    output = io.BytesIO()
    first.save(output, format="GIF", save_all=True, append_images=[second])
    with pytest.raises(ImageRejected):
        ImagePreprocessor().prepare(output.getvalue())


def test_rejects_pixel_limit_before_full_decode() -> None:
    with pytest.raises(ImageRejected, match="pixel"):
        ImagePreprocessor(max_pixels=100).prepare(encoded_image(size=(20, 20)))


def test_rejects_valid_payload_with_unapproved_file_header() -> None:
    raw = encoded_image()
    with pytest.raises(ImageRejected, match="header"):
        ImagePreprocessor().prepare(b"not-image" + raw)
