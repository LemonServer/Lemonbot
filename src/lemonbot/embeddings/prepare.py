"""Independent-environment build/verify entry point; explicit setup operations only."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .artifacts import EmbeddingError
from .tensorrt_backend import _write_private, build_engine
from .verification import verify_device


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "verify"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--precision", choices=("fp16", "fp32"), default="fp16")
    args = parser.parse_args()
    output_fd = os.dup(sys.stdout.fileno())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    try:
        root = args.root.expanduser().resolve(strict=True)
        if args.action == "build":
            report = build_engine(root, args.precision)
        else:
            report = verify_device(root)
            _write_private(root / "verification.json", json.dumps(report, indent=2).encode())
        os.write(output_fd, (json.dumps(report, ensure_ascii=False) + "\n").encode())
    except EmbeddingError as error:
        os.write(output_fd, (json.dumps({"passed": False, "code": str(error)}) + "\n").encode())
        raise SystemExit(1) from None
    finally:
        os.close(output_fd)


if __name__ == "__main__":
    main()
