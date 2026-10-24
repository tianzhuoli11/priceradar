"""
Read-only PriceRadar environment check for WorkBuddy / first-time setup.

Does NOT open a browser, visit JD, print credentials, or dump all env vars.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MARKER_FILES = (
    "README.md",
    "requirements.txt",
    "scripts/p4d_run_product.py",
    "config/products/b913a.json",
)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def venv_python(root: Path) -> Path:
    if platform.system() == "Windows":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def looks_like_project_root(path: Path) -> tuple[bool, list[str]]:
    missing = [rel for rel in MARKER_FILES if not (path / rel).is_file()]
    return (len(missing) == 0, missing)


def chrome_candidates() -> list[Path]:
    system = platform.system()
    homes: list[Path] = []
    if system == "Darwin":
        homes = [
            Path("/Applications/Google Chrome.app"),
            Path.home() / "Applications" / "Google Chrome.app",
        ]
    elif system == "Windows":
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        homes = [
            Path(pf) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(pf86) / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
        if local:
            homes.append(
                Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe"
            )
    else:
        which = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
        if which:
            homes.append(Path(which))
    return homes


def try_import(mod: str) -> dict[str, Any]:
    try:
        m = importlib.import_module(mod)
        ver = getattr(m, "__version__", None)
        return {"ok": True, "version": ver}
    except Exception as exc:  # noqa: BLE001 — report any import failure
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def check_project(root: Path) -> dict[str, Any]:
    root = root.resolve()
    ok, missing = looks_like_project_root(root)
    py_venv = venv_python(root)
    artifacts = root / "artifacts"
    writable = False
    write_error = None
    if artifacts.exists() or True:
        try:
            artifacts.mkdir(parents=True, exist_ok=True)
            probe = artifacts / f"_env_check_probe_{os.getpid()}.txt"
            probe.write_text("ok\n", encoding="utf-8")
            probe.unlink(missing_ok=True)
            writable = True
        except OSError as exc:
            write_error = f"{type(exc).__name__}: {exc}"

    chrome_hits = [str(p) for p in chrome_candidates() if p.exists()]
    deps = {
        "playwright": try_import("playwright"),
        "rapidocr": try_import("rapidocr"),
        "onnxruntime": try_import("onnxruntime"),
        "PIL": try_import("PIL"),
    }

    next_steps: list[str] = []
    if not ok:
        next_steps.append(
            "选定的目录不是 PriceRadar 项目根（缺少: " + ", ".join(missing) + "）"
        )
    if not py_venv.is_file():
        next_steps.append(
            f"创建项目虚拟环境: 使用兼容 Python 执行 "
            f"`python -m venv .venv`（目标解释器: {py_venv.as_posix()}）"
        )
    if not deps["playwright"]["ok"]:
        next_steps.append("在项目 .venv 中安装: pip install -r requirements.txt")
    if not deps["rapidocr"]["ok"] or not deps["onnxruntime"]["ok"]:
        next_steps.append(
            "在项目 .venv 中安装 OCR 依赖: pip install -r requirements-ocr.txt"
        )
    if not chrome_hits:
        next_steps.append(
            "安装 Google Chrome（官方安装包）。Playwright 使用 channel=chrome，"
            "不下载捆绑 Chromium。"
        )
    if not writable:
        next_steps.append(f"确保可写 artifacts/（{write_error}）")

    return {
        "checked_at_utc": utc_iso(),
        "project_root": str(root),
        "project_root_valid": ok,
        "missing_markers": missing,
        "system": {
            "os": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python_version": platform.python_version(),
            "python_executable": sys.executable,
            "implementation": platform.python_implementation(),
        },
        "venv": {
            "expected_python": str(py_venv),
            "exists": py_venv.is_file(),
            "using_venv_now": Path(sys.executable).resolve() == py_venv.resolve()
            if py_venv.is_file()
            else False,
        },
        "dependencies": deps,
        "chrome": {
            "found": bool(chrome_hits),
            "paths": chrome_hits,
            "playwright_channel": "chrome",
            "note": "Does not launch browser in this check",
        },
        "artifacts_writable": writable,
        "artifacts_write_error": write_error,
        "profile_dir_expected": str(
            (root / "browser_profiles" / "jd_persistent").resolve()
        ),
        "profile_note": (
            "Do not copy .venv or browser_profiles from another machine; "
            "create a fresh Profile and log in locally."
        ),
        "next_steps": next_steps,
        "ready_for_live_check": bool(
            ok
            and py_venv.is_file()
            and deps["playwright"]["ok"]
            and deps["rapidocr"]["ok"]
            and deps["onnxruntime"]["ok"]
            and chrome_hits
            and writable
        ),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PriceRadar read-only environment check")
    p.add_argument(
        "--project-root",
        default="",
        help="Project root (default: parent of scripts/)",
    )
    p.add_argument(
        "--json-out",
        default="",
        help="Optional path to write JSON report (still prints summary)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.project_root).resolve() if args.project_root else PROJECT_ROOT
    report = check_project(root)
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"PRICERADAR_ENV_CHECK={out.resolve()}")
    print("=== PriceRadar env check (read-only) ===")
    print(f"project_root: {report['project_root']}")
    print(f"project_root_valid: {report['project_root_valid']}")
    sysinfo = report["system"]
    print(
        f"system: {sysinfo['os']} {sysinfo['release']} "
        f"machine={sysinfo['machine']} python={sysinfo['python_version']}"
    )
    print(f"python_executable: {sysinfo['python_executable']}")
    print(
        f"venv: exists={report['venv']['exists']} "
        f"path={report['venv']['expected_python']}"
    )
    for name, info in report["dependencies"].items():
        print(f"dep {name}: {info}")
    print(f"chrome_found: {report['chrome']['found']}")
    print(f"artifacts_writable: {report['artifacts_writable']}")
    print(f"ready_for_live_check: {report['ready_for_live_check']}")
    if report["next_steps"]:
        print("next_steps:")
        for step in report["next_steps"]:
            print(f"  - {step}")
    return 0 if report["project_root_valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
