"""Map repository TOML to independent worker paths without starting any process."""

from pathlib import Path

from lemonbot.config.paths import RuntimePaths
from lemonbot.config.settings import AppSettings

from .protocol import EmbeddingConfig


def embedding_paths(settings: AppSettings) -> tuple[Path, Path]:
    paths = RuntimePaths.from_settings(settings)
    options = settings.context_memory
    root = (
        Path(options.embedding_assets_root).expanduser()
        if options.embedding_assets_root
        else paths.embedding_assets
    )
    python = (
        Path(options.embedding_worker_python).expanduser()
        if options.embedding_worker_python
        else paths.embedding_worker
    )
    # Do not resolve the Python executable's venv symlink.
    return root.resolve(), python.absolute()


def worker_config(settings: AppSettings) -> EmbeddingConfig:
    root, _ = embedding_paths(settings)
    options = settings.context_memory
    return EmbeddingConfig(
        assets_root=str(root),
        backend=options.embedding_backend,
        precision=options.embedding_precision,
        cache_entries=options.embedding_cache_entries,
    )
