"""Local image handling at the isolated provider boundary."""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from pathlib import Path

from lemonbot.domain.models import ModelImage
from lemonbot.tools.vision import ImagePreprocessor, ImageRejected


class ImageInputReader:
    def __init__(self, root: Path) -> None:
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise ValueError("image object root must be an existing private directory")
        self.root = root.resolve(strict=True)
        self.preprocessor = ImagePreprocessor()

    def data_url(self, image: ModelImage) -> str:
        shard = self.root / image.sha256[:2]
        path = shard / image.sha256
        if shard.is_symlink() or not shard.is_dir() or path.is_symlink():
            raise ImageRejected("image object contains a link or missing shard")
        # Open each component without following links, including the shard, so a
        # replacement between validation and open cannot escape the object root.
        root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            shard_fd = os.open(
                shard.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd
            )
            try:
                fd = os.open(
                    path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=shard_fd
                )
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size != image.size:
                        raise ImageRejected("image object size or type mismatch")
                    raw = stream.read(image.size + 1)
            finally:
                os.close(shard_fd)
        finally:
            os.close(root_fd)
        if len(raw) != image.size or hashlib.sha256(raw).hexdigest() != image.sha256:
            raise ImageRejected("image object integrity mismatch")
        prepared = self.preprocessor.prepare(raw)
        return "data:image/jpeg;base64," + base64.b64encode(prepared.content).decode("ascii")
