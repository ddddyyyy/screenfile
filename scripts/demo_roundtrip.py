from __future__ import annotations

import argparse
from pathlib import Path

from screenfile.demo import run_demo_roundtrip


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a demo payload, encode it to video, and decode it back.")
    parser.add_argument("output_dir", nargs="?", default="demo-output", help="Directory to store demo artifacts.")
    parser.add_argument("--payload-size", type=int, default=64 * 1024)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--fps", type=int, default=8)
    args = parser.parse_args()

    result = run_demo_roundtrip(
        Path(args.output_dir),
        payload_size=args.payload_size,
        repeat=args.repeat,
        fps=args.fps,
    )
    print(f"Source file: {result.source_path}")
    print(f"Transfer video: {result.video_path}")
    print(f"Restored file: {result.restored_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
