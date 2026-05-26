from __future__ import annotations

import platform
from pathlib import Path

from PyInstaller.__main__ import run as pyinstaller_run


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    entrypoint = root / "screenfile" / "__main__.py"
    dist = root / "dist"
    build = root / "build"
    executable_name = "screenfile.exe" if platform.system() == "Windows" else "screenfile"

    pyinstaller_run(
        [
            "--noconfirm",
            "--clean",
            "--onefile",
            "--name",
            executable_name,
            "--distpath",
            str(dist),
            "--workpath",
            str(build),
            str(entrypoint),
        ]
    )
    print(f"Executable written under: {dist / executable_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
