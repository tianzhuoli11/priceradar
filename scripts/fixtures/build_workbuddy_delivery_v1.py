"""
Build WorkBuddy Skill ZIP + migratable project ZIP (allowlist only).

Does not delete user files, does not visit JD, does not write product history.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DIST = PROJECT_ROOT / "dist"
SKILL_SRC = PROJECT_ROOT / "delivery" / "workbuddy-skill" / "priceradar"

SKILL_ZIP_NAME = "priceradar-workbuddy-skill-v1.zip"
PROJECT_ZIP_NAME = "priceradar-project-v1.zip"

# Explicit allowlist (relative to project root). No whole-tree zip.
PROJECT_ALLOWLIST: tuple[str, ...] = (
    "README.md",
    ".gitignore",
    "requirements.txt",
    "requirements-ocr.txt",
    "run_b913a.cmd",
    "docs/macos_first_setup.md",
    "config/products/b913a.json",
    "scripts/priceradar_env_check.py",
    "scripts/p1a_open_chrome.py",
    "scripts/p1b_jd_login.py",
    "scripts/p2a_jd_product_check.py",
    "scripts/p2b_jd_price_read.py",
    "scripts/p2c_jd_capture.py",
    "scripts/p2d_jd_auto_capture.py",
    "scripts/p4a_local_ocr.py",
    "scripts/p4b_compare_from_ocr.py",
    "scripts/p4c_process_capture_run.py",
    "scripts/p4d_run_product.py",
    "scripts/p4e_report_history.py",
    "scripts/fixtures/mock_launcher_entry.py",
    "scripts/fixtures/p2d_discovery_fixture.html",
    "scripts/fixtures/p2d_geometry_fixture.html",
    "scripts/fixtures/p2d_price_bottom_bar_fixture.html",
    "scripts/fixtures/p2d_region_scroll_fixture.html",
    "scripts/fixtures/p2d_selected_spec_label_only_fixture.html",
    "scripts/fixtures/p2d_selected_spec_label_sibling_fixture.html",
    "scripts/fixtures/run_launcher_selftest.cmd",
    "scripts/fixtures/build_workbuddy_delivery_v1.py",
)

FORBIDDEN_NAME_FRAGMENTS = (
    ".venv",
    "browser_profiles",
    "artifacts",
    "__pycache__",
    ".git",
    ".env",
    "storage_state",
    "cookie",
    "token",
)

FORBIDDEN_SUFFIXES = (
    ".pyc",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def assert_safe_member(name: str) -> None:
    norm = name.replace("\\", "/")
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
        raise ValueError(f"absolute path in zip: {name}")
    parts = Path(norm).parts
    if ".." in parts:
        raise ValueError(f"path escape in zip: {name}")
    low = norm.lower()
    for frag in FORBIDDEN_NAME_FRAGMENTS:
        if frag.lower() in low.split("/"):
            raise ValueError(f"forbidden path fragment {frag!r} in {name}")
    for suf in FORBIDDEN_SUFFIXES:
        if low.endswith(suf) and "fixtures/" not in low and not low.endswith(".html"):
            # allow fixture html only; block image artifacts
            if suf != ".html":
                raise ValueError(f"forbidden suffix in zip member: {name}")


def write_zip(zip_path: Path, pairs: Iterable[tuple[Path, str]]) -> list[str]:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    members: list[str] = []
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for src, arc in pairs:
            assert_safe_member(arc)
            if not src.is_file():
                raise FileNotFoundError(src)
            zf.write(src, arcname=arc)
            members.append(arc)
    return members


def build_skill_zip() -> tuple[Path, list[str]]:
    if not (SKILL_SRC / "SKILL.md").is_file():
        raise FileNotFoundError(SKILL_SRC / "SKILL.md")
    pairs: list[tuple[Path, str]] = []
    for path in sorted(SKILL_SRC.rglob("*")):
        if path.is_file():
            rel = path.relative_to(SKILL_SRC.parent).as_posix()
            # top-level folder must be priceradar/
            pairs.append((path, rel))
    out = DIST / SKILL_ZIP_NAME
    members = write_zip(out, pairs)
    if "priceradar/SKILL.md" not in members:
        raise RuntimeError("skill zip missing priceradar/SKILL.md at archive root")
    return out, members


def build_project_zip() -> tuple[Path, list[str]]:
    pairs: list[tuple[Path, str]] = []
    missing: list[str] = []
    for rel in PROJECT_ALLOWLIST:
        src = PROJECT_ROOT / rel
        if not src.is_file():
            missing.append(rel)
            continue
        pairs.append((src, f"priceradar/{Path(rel).as_posix()}"))
    if missing:
        raise FileNotFoundError("allowlist missing: " + ", ".join(missing))
    out = DIST / PROJECT_ZIP_NAME
    members = write_zip(out, pairs)
    return out, members


def validate_skill_frontmatter(text: str) -> list[str]:
    fails: list[str] = []
    if not text.startswith("---"):
        fails.append("SKILL.md missing opening ---")
        return fails
    end = text.find("\n---", 3)
    if end < 0:
        fails.append("SKILL.md missing closing ---")
        return fails
    fm = text[3:end].strip()
    required = (
        "description:",
        "description_zh:",
        "description_en:",
        "version:",
        "author:",
    )
    for key in required:
        if key not in fm:
            fails.append(f"frontmatter missing {key}")
    if "name:" not in fm:
        fails.append("frontmatter missing name:")
    if "display_name:" not in fm:
        fails.append("frontmatter missing display_name:")
    # colon-space check for simple keys
    for line in fm.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if ":" not in line:
            fails.append(f"frontmatter line without colon: {line!r}")
            continue
        left, right = line.split(":", 1)
        if right != "" and not right.startswith(" "):
            fails.append(f"frontmatter needs space after colon: {line!r}")
    return fails


def verify_extract(zip_path: Path, *, expect_prefix: str) -> dict:
    report: dict = {"zip": str(zip_path), "ok": True, "errors": []}
    with tempfile.TemporaryDirectory(prefix="pr_wb_") as td:
        # Path with spaces
        dest = Path(td) / "PriceRadar Delivery Test"
        dest.mkdir()
        with zipfile.ZipFile(zip_path, "r") as zf:
            for info in zf.infolist():
                try:
                    assert_safe_member(info.filename)
                except ValueError as exc:
                    report["ok"] = False
                    report["errors"].append(str(exc))
            zf.extractall(dest)
        root = dest / expect_prefix
        if not root.is_dir():
            report["ok"] = False
            report["errors"].append(f"missing extracted root {expect_prefix}")
            return report
        report["extract_root"] = str(root)
        if expect_prefix == "priceradar" and (root / "SKILL.md").is_file():
            fails = validate_skill_frontmatter(
                (root / "SKILL.md").read_text(encoding="utf-8")
            )
            report["frontmatter_failures"] = fails
            if fails:
                report["ok"] = False
                report["errors"].extend(fails)
        if expect_prefix == "priceradar" and (root / "scripts" / "p4d_run_product.py").is_file():
            # dynamic deps present
            needed = [
                "scripts/p2d_jd_auto_capture.py",
                "scripts/p4a_local_ocr.py",
                "scripts/p4b_compare_from_ocr.py",
                "scripts/p4c_process_capture_run.py",
                "scripts/p4e_report_history.py",
                "scripts/priceradar_env_check.py",
                "config/products/b913a.json",
            ]
            for rel in needed:
                if not (root / rel).is_file():
                    report["ok"] = False
                    report["errors"].append(f"project zip missing {rel}")
            # forbidden dirs must not exist inside extract
            for bad in (".venv", "browser_profiles", "artifacts", ".git"):
                if (root / bad).exists():
                    report["ok"] = False
                    report["errors"].append(f"forbidden extracted path {bad}")
    return report


def write_delivery_notes(
    *,
    skill_zip: Path,
    project_zip: Path,
    skill_sha: str,
    project_sha: str,
    skill_members: list[str],
    project_members: list[str],
    verify: dict,
) -> Path:
    path = DIST / "交付说明.md"
    text = f"""# PriceRadar WorkBuddy 交付说明（v1）

生成时间（UTC）：{utc_stamp()}

## 两个 ZIP 怎么用

| 文件 | 用途 |
| --- | --- |
| `{SKILL_ZIP_NAME}` | 在 WorkBuddy **上传技能**导入 |
| `{PROJECT_ZIP_NAME}` | 解压为可运行项目（创建本机 `.venv` 与 Profile） |

绝对路径：

- Skill：`{skill_zip.resolve()}`
- 项目：`{project_zip.resolve()}`

SHA256 见同目录 `SHA256SUMS.txt`。

## 首次环境准备（摘要）

1. 解压**项目包**到固定目录；WorkBuddy 工作空间指向该项目根。
2. 上传并启用**技能包**（顶层可见 `priceradar/SKILL.md`）。
3. 按项目内 `docs/macos_first_setup.md`（Mac）或 `README.md`（Windows）创建 `.venv`、安装 `requirements.txt` + `requirements-ocr.txt`、安装 Google Chrome。
4. **不要**复制旧 `.venv` 或 `browser_profiles`。
5. 运行 `scripts/p1b_jd_login.py` 在可见窗口亲自登录京东。
6. 运行 `scripts/p4d_run_product.py --product-config config/products/b913a.json`。

## 可粘贴给 WorkBuddy

### 首次设置

请使用PriceRadar技能检查当前项目的Mac运行环境。先完成只读检查，列出缺失依赖和具体设置步骤；环境就绪后引导我在独立Chrome窗口登录京东。

### 日常使用

使用PriceRadar检查已登记的B913A，执行一次实时检查，输出比价表和本次报告路径。证据不足时标记待核验。

## 第一版能力范围

- 已登记商品单次检查；当前真实验证商品：**B913A**（Windows）。
- 不包含：自动搜索、批量、多店最低价、新商品自动登记、定时通知。
- **168.00 / 41.00 / NOT_BREAK** 仅为 Windows 历史验收（`run_id=20261019T052017Z_caa0fa`），不是预期固定价，禁止实时兜底。
- **macOS 与 WorkBuddy 实机导入/运行：待接收方验收**。包结构已按开放平台文档检查；本交付未在 WorkBuddy 内完成真实导入。

## 打包纳入 / 排除

项目包按允许清单纳入脚本、配置、README、macOS 指南、Windows `run_b913a.cmd`、本地自检 fixtures。  
**排除**：`.venv`、`browser_profiles`、`artifacts`、旧截图/报告/历史、Cookie/Token/`.env`、`__pycache__`、`.git`、既有 dist 包、机器绝对路径配置。

Skill 成员数：{len(skill_members)}；项目成员数：{len(project_members)}。

## 验证摘要

```json
{json.dumps(verify, ensure_ascii=False, indent=2)}
```
"""
    path.write_text(text, encoding="utf-8")
    return path


def main() -> int:
    DIST.mkdir(parents=True, exist_ok=True)
    skill_zip, skill_members = build_skill_zip()
    project_zip, project_members = build_project_zip()
    skill_sha = sha256_file(skill_zip)
    project_sha = sha256_file(project_zip)

    sums = DIST / "SHA256SUMS.txt"
    sums.write_text(
        f"{skill_sha}  {SKILL_ZIP_NAME}\n{project_sha}  {PROJECT_ZIP_NAME}\n",
        encoding="utf-8",
    )

    verify = {
        "skill_extract": verify_extract(skill_zip, expect_prefix="priceradar"),
        "project_extract": verify_extract(project_zip, expect_prefix="priceradar"),
        "skill_size_bytes": skill_zip.stat().st_size,
        "project_size_bytes": project_zip.stat().st_size,
        "skill_sha256": skill_sha,
        "project_sha256": project_sha,
    }
    write_delivery_notes(
        skill_zip=skill_zip,
        project_zip=project_zip,
        skill_sha=skill_sha,
        project_sha=project_sha,
        skill_members=skill_members,
        project_members=project_members,
        verify=verify,
    )

    print(json.dumps(verify, ensure_ascii=False, indent=2))
    ok = verify["skill_extract"]["ok"] and verify["project_extract"]["ok"]
    print(f"skill_zip={skill_zip.resolve()}")
    print(f"project_zip={project_zip.resolve()}")
    print(f"delivery_ok={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
