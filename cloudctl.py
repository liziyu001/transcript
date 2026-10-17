"""Cross-platform entry point. Usage: python cloudctl.py up|down|redeploy|status."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    env = os.environ.copy()
    env.setdefault("UV_CACHE_DIR", str(ROOT / "data" / "uv-build-cache"))
    command = [
        "uv",
        "run",
        "--project",
        str(ROOT / "cloud"),
        "--python",
        "3.12",
        "--frozen",
        "--extra",
        "infra",
        "--extra",
        "dev",
        "--extra",
        "ingest",
        "python",
        str(ROOT / "cloud" / "manage.py"),
        *sys.argv[1:],
    ]
    try:
        return subprocess.call(command, cwd=ROOT, env=env)
    except FileNotFoundError:
        print(
            "Install uv first: https://docs.astral.sh/uv/getting-started/installation/",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
