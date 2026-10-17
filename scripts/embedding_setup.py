#!/usr/bin/env python3
"""Install/download/build/verify the standalone embedding worker, without service changes."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from lemonbot.config.settings import load_settings
from lemonbot.embeddings.artifacts import download_artifacts
from lemonbot.embeddings.config import embedding_paths

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "assets", "build", "verify"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--precision", choices=("fp16", "fp32"))
    args = parser.parse_args()
    settings = load_settings(args.config)
    assets, python = embedding_paths(settings)
    if args.action in {"install", "assets"}:
        assets.mkdir(parents=True, exist_ok=True, mode=0o700)
        assets.chmod(0o700)
    if args.action == "install":
        uv = shutil.which("uv")
        if uv is None:
            raise SystemExit("uv is required")
        environment = python.parent.parent
        if python.name != "python" or python.parent.name != "bin":
            raise SystemExit("embedding_worker_python must be a venv/bin/python")
        if environment.resolve() == Path(sys.prefix).resolve():
            raise SystemExit("embedding worker must not use the core Python environment")
        if python.exists():
            configuration = environment / "pyvenv.cfg"
            if not configuration.is_file() or "include-system-site-packages = true" not in (
                configuration.read_text().lower()
            ):
                raise SystemExit("existing worker must be an independent system-site-packages venv")
        if not python.exists():
            subprocess.run(  # noqa: S603 - administrator-selected private venv, no shell
                [
                    uv,
                    "venv",
                    "--python",
                    "/usr/bin/python3",
                    "--system-site-packages",
                    str(environment),
                ],
                check=True,
            )
        environment.chmod(0o700)
        subprocess.run(  # noqa: S603 - fixed hash-locked requirements file
            [
                uv,
                "pip",
                "sync",
                "--python",
                str(python),
                "--require-hashes",
                str(ROOT / "deploy/embedding-worker-requirements.txt"),
            ],
            check=True,
        )
        subprocess.run(  # noqa: S603 - install this checkout without model-controlled arguments
            [uv, "pip", "install", "--python", str(python), "--no-deps", "--editable", str(ROOT)],
            check=True,
        )
        print(json.dumps({"installed": True, "independent_environment": str(environment)}))
    elif args.action == "assets":
        download_artifacts(assets / "model")
        print(json.dumps({"artifacts_verified": True}))
    else:
        precision = args.precision or settings.context_memory.embedding_precision
        subprocess.run(  # noqa: S603 - worker path is selected by administrator config
            [
                str(python),
                "-I",
                "-m",
                "lemonbot.embeddings.prepare",
                args.action,
                "--root",
                str(assets),
                "--precision",
                precision,
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
