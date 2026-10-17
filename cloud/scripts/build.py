"""Build a Linux Python 3.12 FC ZIP locally, using uv and no Docker/Alibaba CLI."""

import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source_hash():
    paths = [
        ROOT / "pyproject.toml",
        ROOT / "uv.lock",
        ROOT / "infra/app.py",
        ROOT / "scripts/build.py",
    ]
    paths += sorted(
        p
        for p in (ROOT / "src").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def run(*args, quiet=False):
    subprocess.run(
        list(args), cwd=ROOT.parent, check=True, stdout=subprocess.DEVNULL if quiet else None
    )


def main():
    out = ROOT / ".artifacts"
    out.mkdir(exist_ok=True)
    requirements = out / "requirements-linux.txt"
    # FC python3.12 bundles oss2 and crcmod. crcmod has no portable Linux wheel;
    # do not accidentally ship a Windows extension. Other dependencies are vendored.
    run(
        "uv",
        "export",
        "--project",
        str(ROOT),
        "--frozen",
        "--no-dev",
        "--no-emit-project",
        "--no-emit-package",
        "oss2",
        "--no-emit-package",
        "crcmod",
        "--output-file",
        str(requirements),
        quiet=True,
    )
    with tempfile.TemporaryDirectory(prefix="fc-package-", dir=out) as directory:
        target = Path(directory)
        run(
            "uv",
            "pip",
            "install",
            "--python-version",
            "3.12",
            "--python-platform",
            "x86_64-manylinux_2_28",
            "--target",
            str(target),
            "--no-deps",
            "--no-compile-bytecode",
            "--requirements",
            str(requirements),
        )
        shutil.copytree(
            ROOT / "src" / "transcript_cloud",
            target / "transcript_cloud",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        temporary = out / "function.zip"
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(target.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                    if path.suffix in {".pyd", ".dll"}:
                        raise RuntimeError(f"Windows binary in Linux package: {path.name}")
                    info = zipfile.ZipInfo(
                        path.relative_to(target).as_posix(), (2020, 1, 1, 0, 0, 0)
                    )
                    info.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(info, path.read_bytes())
    checksum = hashlib.sha256(temporary.read_bytes()).hexdigest()
    manifest = {
        "sha256": checksum,
        "source_sha256": source_hash(),
        "file": str(temporary),
        "object_key": f"code/{checksum}.zip",
        "runtime": "python3.12",
    }
    (out / "build.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Built {temporary} ({temporary.stat().st_size / 1024**2:.1f} MiB). No upload performed.")


if __name__ == "__main__":
    main()
