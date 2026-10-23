"""Prepare only this demo from an owner-supplied, immutable engine wheel."""

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from email.parser import BytesParser
from pathlib import Path

WHEEL = "pokerengine-2.0.0.dev2-cp314-cp314-linux_x86_64.whl"
SHA256 = "13a1a57f6c61860cf1c1b89d1a4db624eda8d8cad132bdf7d61f0e94ab798ee3"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if Path.cwd().resolve() != root:
        parser.error("Run preparation from this repository root")
    if (sys.implementation.name, sys.version_info[:2], sys.platform, platform.machine()) != (
        "cpython",
        (3, 14),
        "linux",
        "x86_64",
    ):
        parser.error("The supplied wheel requires CPython 3.14 on Linux x86-64")
    source = args.wheel.expanduser().resolve(strict=True)
    if source.name != WHEEL or hashlib.sha256(source.read_bytes()).hexdigest() != SHA256:
        parser.error("Wheel name or SHA256 differs from the pinned artifact")
    with zipfile.ZipFile(source) as archive:
        name = next(name for name in archive.namelist() if name.endswith(".dist-info/WHEEL"))
        metadata = BytesParser().parsebytes(archive.read(name))
        if "cp314-cp314-linux_x86_64" not in metadata.get_all("Tag", []):
            parser.error("Wheel tag is incompatible")
    destination = root / ".local/wheels" / WHEEL
    for path in (root / ".local", destination.parent, destination):
        if path.is_symlink():
            parser.error("Refusing a symlinked dependency destination")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if source != destination:
        shutil.copyfile(source, destination)
    print(f"Verified engine wheel: {SHA256}", flush=True)
    subprocess.run(
        ["uv", "sync", "--locked", "--no-editable", "--python", sys.executable],
        check=True,
        env={**os.environ, "UV_CACHE_DIR": str(root / ".local/uv-cache")},
    )


if __name__ == "__main__":
    main()
