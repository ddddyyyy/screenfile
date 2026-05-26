from __future__ import annotations

import os
import stat
import sys
import zipfile
from pathlib import Path


def _apply_permissions(info: zipfile.ZipInfo, source: Path) -> None:
    mode = stat.S_IMODE(source.stat().st_mode)
    info.external_attr = (mode & 0xFFFF) << 16


def build_zip(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        info = zipfile.ZipInfo(source.name)
        info.date_time = (2024, 1, 1, 0, 0, 0)
        _apply_permissions(info, source)
        with source.open("rb") as handle:
            zf.writestr(info, handle.read())


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    if len(args) != 2:
        print("Usage: python scripts/package_artifact.py <source-file> <zip-file>", file=sys.stderr)
        return 1

    source = Path(args[0]).resolve()
    destination = Path(args[1]).resolve()
    if not source.exists():
        raise FileNotFoundError(source)

    build_zip(source, destination)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
