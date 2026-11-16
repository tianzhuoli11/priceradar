"""
Build PriceRadar v2 delivery: WorkBuddy Skill ZIP + macOS project ZIP (allowlist only).

- Never overwrites an existing output (v1 artifacts stay untouched).
- Does not visit JD, does not launch a browser, does not touch artifacts/.
- Static checks only; runtime self-checks are run separately on an extracted copy.

Usage:  <python> scripts/fixtures/build_workbuddy_delivery_v2.py [--report <json>]
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DIST = PROJECT_ROOT / "dist"
VERSION = "2.0.0"
TAG = "v2"

PROJECT_PREFIX = "priceradar/"

# Per-platform delivery settings. macOS is the default (original v2 outputs).
PLATFORMS: dict[str, dict] = {
    "macos": {
        "skill_src": "delivery/workbuddy-skill-v2/priceradar",
        "readme": "delivery/project-v2/README.md",
        "guide": "docs/macos_first_setup.md",
        "suffix": "",
        "guide_out": f"PriceRadar-{TAG}-macOS安装说明.md",
        "extra": (),
    },
    "windows": {
        "skill_src": "delivery/workbuddy-skill-v2-windows/priceradar",
        "readme": "delivery/project-v2-windows/README.md",
        "guide": "docs/windows_setup.md",
        "suffix": "-windows",
        "guide_out": f"PriceRadar-{TAG}-Windows安装说明.md",
        # Windows launchers: root-relative (%~dp0 / $env:USERPROFILE), no personal
        # paths or historic browser IDs. The port54100 duplicate is excluded.
        "extra": (
            ("run_b913a.cmd", "run_b913a.cmd"),
            ("scripts/fixtures/run_launcher_selftest.cmd", "scripts/fixtures/run_launcher_selftest.cmd"),
            ("scripts/fixtures/mock_launcher_entry.py", "scripts/fixtures/mock_launcher_entry.py"),
            ("scripts/fixtures/start_bsk_daemon.ps1", "scripts/fixtures/start_bsk_daemon.ps1"),
        ),
    },
}

# (source relative to repo root, archive path relative to PROJECT_PREFIX)
# README and platform guide are added per platform in main().
PROJECT_ALLOWLIST: tuple[tuple[str, str], ...] = (
    (".gitignore", ".gitignore"),
    ("requirements.txt", "requirements.txt"),
    ("requirements-ocr.txt", "requirements-ocr.txt"),
    ("config/products/b913a.json", "config/products/b913a.json"),
    (
        "config/policies/b913a_model_scope_include_other_20261027.json",
        "config/policies/b913a_model_scope_include_other_20261027.json",
    ),
    ("templates/batch_sku_input_template.xlsx", "templates/batch_sku_input_template.xlsx"),
    # --- default path: env check, login, p4d chain ---
    *((f"scripts/{n}.py", f"scripts/{n}.py") for n in (
        "priceradar_env_check",
        "p1a_open_chrome",
        "p1b_jd_login",
        "p2d_jd_auto_capture",
        "p4a_local_ocr",
        "p4b_compare_from_ocr",
        "p4c_process_capture_run",
        "p4d_run_product",
        "p4e_report_history",
        "priceradar_selected_spec",
        # --- advanced: p5b / p5d / p5f + BSK ---
        "p3a_jd_search",
        "p5a_verify_candidate",
        "p5b_run_sku",
        "p5c_offline_reprocess_job",
        "p5d_run_batch",
        "p5f_collect_all_links",
        "p5g_bsk_search_page2_probe",
        "p5_bsk_detail",
        "p5_bsk_runtime",
        "priceradar_candidate_adapt",
        "priceradar_excel_input",
        "priceradar_model_match",
        "priceradar_take_home_prices",
    )),
    # --- offline self-check fixtures ---
    *((f"scripts/fixtures/{n}", f"scripts/fixtures/{n}") for n in (
        "p2d_discovery_fixture.html",
        "p2d_geometry_fixture.html",
        "p2d_price_bottom_bar_fixture.html",
        "p2d_region_scroll_fixture.html",
        "p2d_selected_spec_label_only_fixture.html",
        "p2d_selected_spec_label_sibling_fixture.html",
        "bsk_card_visibility_fixture.html",
        "batch_sku_input_synthetic.xlsx",
    )),
)

FORBIDDEN_PARTS = {".venv", "browser_profiles", "artifacts", "__pycache__", ".git", "dist", "models"}
FORBIDDEN_NAME_RE = re.compile(r"(^\.env($|\.)|cookie|token|storage_state|\.pyc$|\.png$|\.jpe?g$|\.webp$|\.log$|\.zip$)", re.I)
# .ps1/.cmd only allowed when explicitly listed in a platform's "extra".
SCRIPT_SUFFIX_RE = re.compile(r"\.(ps1|cmd|bat)$", re.I)

SENSITIVE_PATTERNS = {
    "windows_user_path": re.compile(r"C:[\\/]+Users[\\/]", re.I),
    "personal_name": re.compile(r"tianz", re.I),
    "historic_bsk_browser_id": re.compile(r"2636aa80"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.(com|cn|net)\b"),
    "jd_cookie_value": re.compile(r"\b(pt_key|pt_pin|thor|__jda|unick)\s*=\s*[^\s;\"']{6,}", re.I),
    "bearer_value": re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}"),
    "cn_mobile": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
}

SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,62})[a-z0-9]$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
SKILL_TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".py", ".js"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_member(name: str) -> None:
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in name.split("/"):
        raise ValueError(f"unsafe path: {name}")
    parts = name.split("/")
    bad = FORBIDDEN_PARTS.intersection(parts)
    if bad:
        raise ValueError(f"forbidden dir {bad} in {name}")
    if FORBIDDEN_NAME_RE.search(parts[-1]):
        raise ValueError(f"forbidden file name: {name}")


def write_zip(out: Path, pairs: list[tuple[Path, str]], extra: dict[str, bytes] | None = None) -> list[str]:
    if out.exists():
        raise FileExistsError(f"refuse to overwrite {out}")
    members: list[str] = []
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for src, arc in pairs:
            check_member(arc)
            if not src.is_file():
                raise FileNotFoundError(src)
            zf.write(src, arcname=arc)
            members.append(arc)
        for arc, data in (extra or {}).items():
            check_member(arc)
            zf.writestr(arc, data)
            members.append(arc)
    return members


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md must start with ---")
    end = text.find("\n---", 4)
    if end < 0:
        raise ValueError("SKILL.md frontmatter not closed")
    fm: dict[str, str] = {}
    section = ""
    for line in text[4:end].splitlines():
        if not line.strip():
            continue
        if line.strip() == "---":
            raise ValueError("extra --- inside frontmatter")
        key, _, val = line.partition(":")
        if not _:
            raise ValueError(f"bad frontmatter line {line!r}")
        if line.startswith("  "):
            fm[f"{section}.{key.strip()}"] = val.strip()
        else:
            section = key.strip()
            fm[section] = val.strip()
    return fm


def validate_skill_dir(root: Path) -> list[str]:
    errs: list[str] = []
    skill = root / "SKILL.md"
    if not skill.is_file():
        return ["SKILL.md not at zip root"]
    text = skill.read_text(encoding="utf-8")
    try:
        fm = parse_frontmatter(text)
    except ValueError as exc:
        return [str(exc)]
    if not SKILL_NAME_RE.match(fm.get("name", "")) or "--" in fm.get("name", ""):
        errs.append(f"name invalid: {fm.get('name')!r}")
    desc = fm.get("description", "")
    if not (1 <= len(desc) <= 1024):
        errs.append(f"description length {len(desc)} not in 1..1024")
    if not SEMVER_RE.match(fm.get("metadata.version", "")):
        errs.append(f"metadata.version not SemVer: {fm.get('metadata.version')!r}")
    files = [p for p in root.rglob("*") if p.is_file()]
    if len(files) > 300:
        errs.append("more than 300 files")
    for p in files:
        if p.suffix.lower() not in SKILL_TEXT_SUFFIXES:
            errs.append(f"non-text file in skill: {p.relative_to(root)}")
    # every references/*.md mentioned in SKILL.md must exist
    for ref in sorted(set(re.findall(r"references/[A-Za-z0-9_-]+\.md", text))):
        if not (root / ref).is_file():
            errs.append(f"SKILL.md references missing {ref}")
    return errs


def scripts_dependency_closure(project: Path) -> list[str]:
    """Every sibling module loaded via load_sibling()/import must be shipped."""
    scripts = project / "scripts"
    shipped = {p.stem for p in scripts.glob("*.py")}
    missing: list[str] = []
    for py in sorted(scripts.glob("*.py")):
        src = py.read_text(encoding="utf-8")
        wanted = set(re.findall(r"load_sibling\(\s*[\"']([A-Za-z0-9_]+)[\"']", src))
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                wanted.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                wanted.update(a.name.split(".")[0] for a in node.names)
        local_like = {w for w in wanted if re.match(r"^(p\d|priceradar_)", w)}
        for w in sorted(local_like - shipped):
            missing.append(f"{py.name} -> {w}")
    # fixtures referenced from shipped code
    for py in sorted(scripts.glob("*.py")):
        src = py.read_text(encoding="utf-8")
        for fx in re.findall(r"[\"']([A-Za-z0-9_]+_fixture\.html|batch_sku_input_[a-z]+\.xlsx)[\"']", src):
            if not any(project.rglob(fx)):
                missing.append(f"{py.name} -> fixture {fx}")
    return missing


def sensitive_scan(root: Path) -> list[str]:
    hits: list[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() in {".xlsx"}:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for label, rx in SENSITIVE_PATTERNS.items():
            for m in rx.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                hits.append(f"{p.relative_to(root).as_posix()}:{line}: {label}: {m.group(0)[:40]}")
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default="", help="optional JSON report path")
    ap.add_argument("--platform", choices=sorted(PLATFORMS), default="macos")
    args = ap.parse_args(argv)

    plat = PLATFORMS[args.platform]
    SKILL_SRC = PROJECT_ROOT / plat["skill_src"]
    sfx = plat["suffix"]
    SKILL_ZIP = DIST / f"priceradar-workbuddy-skill-{TAG}{sfx}.zip"
    PROJECT_ZIP = DIST / f"priceradar-project-{TAG}{sfx}.zip"
    GUIDE_OUT = DIST / plat["guide_out"]
    SUMS_OUT = DIST / f"SHA256SUMS-{TAG}{sfx}.txt"
    MAC_GUIDE = PROJECT_ROOT / plat["guide"]
    allowlist = (
        (plat["readme"], "README.md"),
        (plat["guide"], plat["guide"]),
        *PROJECT_ALLOWLIST,
        *plat["extra"],
    )
    allowed_scripts = {arc for _, arc in plat["extra"]}
    for _, arc in allowlist:
        if SCRIPT_SUFFIX_RE.search(arc) and arc not in allowed_scripts:
            print(f"REFUSE: shell script {arc} not in platform extra list")
            return 2

    for out in (SKILL_ZIP, PROJECT_ZIP, GUIDE_OUT, SUMS_OUT):
        if out.exists():
            print(f"REFUSE: {out} already exists (bump TAG instead of overwriting)")
            return 2
    DIST.mkdir(parents=True, exist_ok=True)

    # Skill ZIP: SKILL.md directly at archive root (Tencent Skills ZIP spec).
    skill_pairs = [
        (p, p.relative_to(SKILL_SRC).as_posix())
        for p in sorted(SKILL_SRC.rglob("*"))
        if p.is_file()
    ]
    skill_errs = validate_skill_dir(SKILL_SRC)
    if skill_errs:
        print("skill source invalid:", skill_errs)
        return 1
    skill_members = write_zip(SKILL_ZIP, skill_pairs)

    project_pairs = [
        (PROJECT_ROOT / src, PROJECT_PREFIX + arc) for src, arc in allowlist
    ]
    project_members = write_zip(
        PROJECT_ZIP,
        project_pairs,
        extra={PROJECT_PREFIX + "VERSION": f"{VERSION}\n".encode()},
    )

    shutil.copyfile(MAC_GUIDE, GUIDE_OUT)

    sums = {p.name: sha256(p) for p in (SKILL_ZIP, PROJECT_ZIP, GUIDE_OUT)}
    SUMS_OUT.write_text(
        "".join(f"{h}  {n}\n" for n, h in sums.items()), encoding="utf-8"
    )

    report = {
        "built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version": VERSION,
        "platform": args.platform,
        "skill_zip": str(SKILL_ZIP),
        "project_zip": str(PROJECT_ZIP),
        "guide": str(GUIDE_OUT),
        "sha256sums": str(SUMS_OUT),
        "sizes": {p.name: p.stat().st_size for p in (SKILL_ZIP, PROJECT_ZIP, GUIDE_OUT)},
        "sha256": sums,
        "skill_members": skill_members,
        "project_members": project_members,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
