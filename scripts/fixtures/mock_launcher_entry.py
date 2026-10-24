"""Isolated mock entry for run_b913a.cmd self-test. No JD, no OCR, no history."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Mock P4-D entry for launcher self-test")
    p.add_argument(
        "--exit-code",
        type=int,
        required=True,
        help="Process exit code to return (e.g. 0 or 3)",
    )
    p.add_argument(
        "--product-config",
        default="",
        help="Accepted for launcher argv compatibility; ignored",
    )
    args = p.parse_args(argv)
    code = int(args.exit_code)
    print(f"[mock_launcher_entry] exit_code={code} (no JD / no history)")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
