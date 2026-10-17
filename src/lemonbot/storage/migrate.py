from __future__ import annotations

from importlib.resources import as_file, files
from pathlib import Path

from alembic import command
from alembic.config import Config


def upgrade_database(database_path: Path) -> None:
    database_path = database_path.expanduser().resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    resource = files("lemonbot.storage.migrations")
    with as_file(resource) as script_location:
        config = Config()
        config.set_main_option("script_location", str(script_location))
        config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
        # A caller supplying an explicit path (especially an offline copy) must
        # never be redirected to another database by the Alembic CLI environment.
        config.attributes["explicit_database_path"] = True
        command.upgrade(config, "head")
