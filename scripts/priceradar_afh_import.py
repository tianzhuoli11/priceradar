"""
AFH 监控品项与红线价.xlsx → P5-D batch input (offline, read-only source).

- Reuses templates/batch_sku_input_template.xlsx (columns + 说明 sheet) and the
  existing validator (priceradar_excel_input.parse_batch_excel).
- Each imported row is written at its ORIGINAL Excel row number, so the batch
  validator's excel_row traces back to the AFH source row.
- Only clear rows are imported: one full model, explicit layers, N 张/抽 per 包,
  a directly stated packs-per-box (no inner pack), a valid red line, priced per 箱.
- Everything else is listed with a reason (never converted, never 0 元).
- No browser, no JD, no network. Never overwrites an existing output.

Usage:
  python scripts/priceradar_afh_import.py --source <AFH.xlsx> --out-dir <new dir>
  python scripts/priceradar_afh_import.py --self-check
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import re
import shutil
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
TEMPLATE = PROJECT_ROOT / "templates" / "batch_sku_input_template.xlsx"
PRODUCT_CONFIG_DIR = PROJECT_ROOT / "config" / "products"
RULES_VERSION = "afh-import-v1"
DEFAULT_SHEET = "AFH监控SKU"


def _load(stem: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"priceradar_{stem}", SCRIPTS_DIR / f"{stem}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


excel_in = _load("priceradar_excel_input")

# Decision codes
IMPORTED = "IMPORTED"
BANNED = "BANNED_NO_PRICE_BATCH"
MID_PACK_PENDING = "PENDING_MID_PACK_CONFIRM"
ALIAS_PENDING = "PENDING_MODEL_MAPPING"
SPEC_UNSUPPORTED = "SPEC_UNSUPPORTED"
SPEC_UNCLEAR = "PENDING_SPEC_UNCLEAR"
DUPLICATE = "REJECTED_DUPLICATE_MODEL"
INVALID_AMOUNT = "REJECTED_INVALID_AMOUNT"
INVALID_FIELD = "REJECTED_INVALID_FIELD"

DECISION_ZH = {
    IMPORTED: "已导入价格批次",
    BANNED: "禁售品项：不进入价格批次，不填红线",
    MID_PACK_PENDING: "中包/提入数=1，待确认箱规",
    ALIAS_PENDING: "型号映射待确认",
    SPEC_UNSUPPORTED: "规格待支持（卷/米/克/段或多级包装）",
    SPEC_UNCLEAR: "规格不明确，待确认",
    DUPLICATE: "重复型号，未导入",
    INVALID_AMOUNT: "红线金额非法，未导入",
    INVALID_FIELD: "字段缺失/非法，未导入",
}

MODEL_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*\d[A-Za-z0-9]*$")
SPEC_PER_PACK_RE = re.compile(r"^\s*(\d+)\s*(张|抽)\s*/\s*包\s*$")
SPEC_SHEETS_NO_PACK_RE = re.compile(r"^\s*(\d+)\s*(张|抽)\s*$")
UNSUPPORTED_UNIT_RE = re.compile(r"(卷|米|克|段)")
BANNED_TEXT = "不允许线上售卖"

SRC_HEADERS = {
    "sku": "监控SKU",
    "category": "品类",
    "name": "品名",
    "layers": "层数",
    "spec": "单包抽数/克重",
    "mid": "中包/提入数",
    "box": "箱规数量",
    "unit": "单位",
    "red": "26年新管控价",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _s(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v).strip()


def _pos_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v > 0 else None
    if isinstance(v, float) and v == int(v) and v > 0:
        return int(v)
    s = _s(v)
    return int(s) if s.isdigit() and int(s) > 0 else None


def _amount(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(format(v, "f")) if isinstance(v, float) else Decimal(_s(v).replace(",", ""))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not d.is_finite() or d <= 0:
        return None
    return d.quantize(Decimal("0.01"))


def registered_configs() -> dict[str, dict[str, Any]]:
    """config/products/*.json keyed by business_sku (explicit business basis)."""
    out: dict[str, dict[str, Any]] = {}
    if not PRODUCT_CONFIG_DIR.is_dir():
        return out
    for p in sorted(PRODUCT_CONFIG_DIR.glob("*.json")):
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sku = str(cfg.get("business_sku") or "").strip().upper()
        if sku:
            out[sku] = {**cfg, "_path": p.relative_to(PROJECT_ROOT).as_posix()}
    return out


def _basis_confirms(cfg: dict[str, Any] | None, *, layers: int, sheets: int, unit: str, packs: int) -> bool:
    basis = (cfg or {}).get("packaging_basis")
    if not isinstance(basis, dict):
        return False
    return (
        basis.get("layers") == layers
        and basis.get("sheets_per_pack") == sheets
        and str(basis.get("sheet_unit_business") or "") == unit
        and basis.get("packs_per_box") == packs
    )


def read_source_rows(src: Path, sheet: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (item_rows, non_item_rows). Header repeats / blanks / notes are non-items."""
    wb = load_workbook(src, read_only=True, data_only=True)
    try:
        if sheet not in wb.sheetnames:
            raise ValueError(f"工作表不存在: {sheet}; 可用: {wb.sheetnames}")
        rows = list(wb[sheet].iter_rows(values_only=True))
    finally:
        wb.close()  # read-only mode keeps the file handle open (locks it on Windows)
    header = [_s(c) for c in rows[0]] if rows else []
    col = {}
    for key, title in SRC_HEADERS.items():
        if title not in header:
            raise ValueError(f"表头缺少列: {title}")
        col[key] = header.index(title)
    items: list[dict[str, Any]] = []
    non_items: list[dict[str, Any]] = []
    section = "price"
    for n, r in enumerate(rows, start=1):
        cells = list(r) + [None] * (len(header) - len(r))
        vals = [_s(c) for c in cells]
        if n == 1:
            non_items.append({"source_row": n, "kind": "HEADER"})
            continue
        if not any(vals):
            non_items.append({"source_row": n, "kind": "BLANK"})
            continue
        if vals[col["sku"]] == SRC_HEADERS["sku"] and vals[col["red"]] == SRC_HEADERS["red"]:
            non_items.append({"source_row": n, "kind": "HEADER_REPEAT"})
            continue
        filled = [i for i, v in enumerate(vals) if v]
        if filled == [col["sku"]] and not MODEL_TOKEN_RE.match(vals[col["sku"]].split("\n")[0].strip()):
            non_items.append({"source_row": n, "kind": "NOTE_TEXT", "text": vals[col["sku"]][:80]})
            if BANNED_TEXT[:2] in vals[col["sku"]] or "不得上架" in vals[col["sku"]]:
                section = "banned_section"
            continue
        items.append(
            {
                "source_row": n,
                "section": section,
                **{k: cells[i] for k, i in col.items()},
            }
        )
    return items, non_items


def classify(items: list[dict[str, Any]], configs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One manifest row per source item row."""
    price_skus = Counter(
        _s(it["sku"]).upper()
        for it in items
        if BANNED_TEXT not in _s(it["red"])
    )
    out: list[dict[str, Any]] = []
    for it in items:
        sku_raw = _s(it["sku"])
        sku = sku_raw.upper()
        spec = _s(it["spec"])
        mid_raw = _s(it["mid"])
        reasons: list[str] = []
        row = {
            "source_row": it["source_row"],
            "sku": sku_raw.replace("\n", " "),
            "category": _s(it["category"]),
            "name": _s(it["name"]),
            "layers_raw": _s(it["layers"]),
            "spec_raw": spec,
            "mid_pack_raw": mid_raw,
            "box_count_raw": _s(it["box"]),
            "box_unit_raw": _s(it["unit"]),
            "red_line_raw": _s(it["red"]),
            "decision": None,
            "decision_zh": None,
            "reason": None,
            "import": None,
        }

        def done(code: str, why: str) -> None:
            row["decision"], row["decision_zh"], row["reason"] = code, DECISION_ZH[code], why
            out.append(row)

        red_text = _s(it["red"])
        if BANNED_TEXT in red_text:
            done(BANNED, f"红线列为「{red_text}」：单列，不填 0 元，不进入价格批次；后续另做上架线索检查")
            continue
        red = _amount(it["red"])
        if red is None:
            done(INVALID_AMOUNT, f"红线「{red_text}」不是有效正金额")
            continue
        if "\n" in sku_raw or "（" in sku_raw or "(" in sku_raw or not MODEL_TOKEN_RE.match(sku_raw):
            done(ALIAS_PENDING, f"SKU 单元格不是单一完整型号「{row['sku']}」；需确认型号映射（新/老款是否同红线、同规格、是否计入）")
            continue
        if price_skus[sku] > 1:
            done(DUPLICATE, f"型号 {sku_raw} 在价格监控区出现 {price_skus[sku]} 次，需确认保留哪一行")
            continue
        if row["box_unit_raw"] != "箱":
            done(INVALID_FIELD, f"计价单位「{row['box_unit_raw']}」不是「箱」")
            continue
        layers = _pos_int(it["layers"])
        box = _pos_int(it["box"])
        if layers is None or box is None:
            done(INVALID_FIELD, f"层数「{row['layers_raw']}」或箱规数量「{row['box_count_raw']}」不是正整数")
            continue
        unit_m = UNSUPPORTED_UNIT_RE.search(spec)
        mid = None if mid_raw in ("", "-") else _pos_int(it["mid"])
        if mid_raw not in ("", "-") and mid is None:
            done(INVALID_FIELD, f"中包/提入数「{mid_raw}」不可解析")
            continue
        if unit_m:
            extra = f"；另含中包层级 {mid}×{box}" if mid and mid > 1 else ""
            done(SPEC_UNSUPPORTED, f"规格「{spec}」按{unit_m.group(1)}计量，现有格式只支持张/抽每包；不换算{extra}")
            continue
        if mid and mid > 1:
            done(SPEC_UNSUPPORTED, f"多级包装 中包/提入数={mid} × 箱规数量={box}；现有格式只有每箱包数，不把 {box} 当包数")
            continue
        m = SPEC_PER_PACK_RE.match(spec)
        if not m:
            if SPEC_SHEETS_NO_PACK_RE.match(spec):
                done(SPEC_UNCLEAR, f"规格「{spec}」未写明「/包」，不能确认为每包张/抽数" + (f"；且中包/提入数={mid}" if mid else ""))
            else:
                done(SPEC_UNCLEAR, f"规格「{spec}」无法解析为 N张/包 或 N抽/包")
            continue
        sheets, sheet_unit = int(m.group(1)), m.group(2)
        cfg = configs.get(sku)
        basis_ok = _basis_confirms(cfg, layers=layers, sheets=sheets, unit=sheet_unit, packs=box)
        if mid == 1 and not basis_ok:
            done(MID_PACK_PENDING, f"中包/提入数=1、箱规数量={box}：按 {box}包/箱 需业务确认（无该SKU既有依据）")
            continue
        brand = str((cfg or {}).get("expected_brand") or "").strip() if cfg else ""
        reasons.append("单一完整型号、明确层数、张/抽每包、直接每箱包数、有效红线、箱计价")
        if cfg:
            reasons.append(f"已有业务配置 {cfg['_path']}（品牌取自该配置）")
        else:
            reasons.append("无品牌依据：品牌留空，搜索词=完整型号")
        row["import"] = {
            "business_sku": sku_raw,
            "brand": brand or None,
            "product_name": row["name"],
            "expected_model": sku_raw,
            "red_line": format(red, "f"),
            "currency": "CNY",
            "comparison_unit": "箱",
            "layers": layers,
            "sheets_or_pulls": sheets,
            "sheet_unit": sheet_unit,
            "packs_per_box": box,
            "packaging_basis": (
                f"AFH监控品项与红线价.xlsx / AFH监控SKU 原表第{it['source_row']}行："
                f"{layers}层、{sheets}{sheet_unit}/包、{box}包/箱、{format(red, 'f')}元/箱"
            ),
            "search_query": f"{brand} {sku_raw}" if brand else sku_raw,
            "notes": (
                f"原表第{it['source_row']}行；严格完整型号；不使用 OTHER 授权；"
                "币种：原表未列，按现有业务配置 CNY"
            ),
        }
        done(IMPORTED, "；".join(reasons))
    return out


TEMPLATE_KEYS = {
    "business_sku": "业务SKU*",
    "brand": "品牌",
    "product_name": "品名",
    "expected_model": "期望型号*",
    "red_line": "红线金额*",
    "currency": "币种*",
    "comparison_unit": "比较单位*",
    "layers": "层数",
    "sheets_or_pulls": "张/抽数",
    "sheet_unit": "张抽单位",
    "packs_per_box": "每箱包数",
    "packaging_basis": "箱规说明/依据",
    "search_query": "搜索词",
    "notes": "备注",
}


def write_batch_input(manifest: list[dict[str, Any]], out_xlsx: Path) -> int:
    shutil.copyfile(TEMPLATE, out_xlsx)
    wb = load_workbook(out_xlsx)
    ws = wb["输入"]
    hdr = {ws.cell(1, c).value: c for c in range(1, ws.max_column + 1)}
    for r in range(2, ws.max_row + 1):
        for c in range(1, ws.max_column + 1):
            ws.cell(r, c).value = None
    n = 0
    for row in manifest:
        imp = row.get("import")
        if row["decision"] != IMPORTED or not imp:
            continue
        rn = int(row["source_row"])
        for key, title in TEMPLATE_KEYS.items():
            ws.cell(rn, hdr[title]).value = imp.get(key)
        ws.cell(rn, hdr["启用(Y/N)"]).value = "Y"
        ws.cell(rn, hdr["行类型(business/test)"]).value = "business"
        n += 1
    wb.save(out_xlsx)
    return n


def run_import(src: Path, out_dir: Path, *, sheet: str = DEFAULT_SHEET) -> dict[str, Any]:
    src = Path(src).resolve()
    out_dir = Path(out_dir).resolve()
    if out_dir.exists():
        raise FileExistsError(f"输出目录已存在，拒绝覆盖: {out_dir}")
    if not src.is_file():
        raise FileNotFoundError(src)
    sha_before = sha256_file(src)
    items, non_items = read_source_rows(src, sheet)
    manifest = classify(items, registered_configs())
    out_dir.mkdir(parents=True)
    xlsx = out_dir / "batch_input.xlsx"
    n_imported = write_batch_input(manifest, xlsx)

    # Validate the generated table with the existing p5d validator.
    parsed = excel_in.parse_batch_excel(xlsx)
    valid_rows = sorted(int(v["excel_row"]) for v in parsed.get("valid_items") or [])
    want_rows = sorted(int(r["source_row"]) for r in manifest if r["decision"] == IMPORTED)

    fields = ["source_row", "sku", "decision", "decision_zh", "reason", "category", "name",
              "layers_raw", "spec_raw", "mid_pack_raw", "box_count_raw", "box_unit_raw", "red_line_raw"]
    with (out_dir / "import_manifest.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(manifest)
    (out_dir / "import_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    sha_after = sha256_file(src)
    counts = Counter(r["decision"] for r in manifest)
    price_rows = [r for r in manifest if r["decision"] != BANNED]
    summary = {
        "rules_version": RULES_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": str(src),
        "sheet": sheet,
        "source_sha256_before": sha_before,
        "source_sha256_after": sha_after,
        "source_unchanged": sha_before == sha_after,
        "item_rows": len(manifest),
        "price_monitor_rows": len(price_rows),
        "banned_rows": counts.get(BANNED, 0),
        "non_item_rows": non_items,
        "counts": dict(counts),
        "imported_written": n_imported,
        "validate": {
            "counts": parsed.get("counts"),
            "rejected": parsed.get("rejected"),
            "valid_excel_rows": valid_rows,
            "rows_match_manifest": valid_rows == want_rows,
        },
        "outputs": {
            "batch_input_xlsx": str(xlsx),
            "manifest_csv": str(out_dir / "import_manifest.csv"),
            "manifest_json": str(out_dir / "import_manifest.json"),
        },
        "policy": {
            "model_scope_policy": None,
            "sheet_unit": "张/抽严格区分；仅 config/products 中有明确依据的 SKU（B913A）继承页面别名",
            "brand": "仅取自已登记业务配置，否则留空；搜索词=完整型号",
        },
    }
    (out_dir / "import_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return summary


def run_self_check() -> list[str]:
    """Synthetic workbook covering each decision (no real data)."""
    fails: list[str] = []
    with tempfile.TemporaryDirectory(prefix="afh_imp_") as td:
        src = Path(td) / "synthetic_afh.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = DEFAULT_SHEET
        ws.append(list(SRC_HEADERS.values()))
        rows = [
            ("TEST100A", "HT", "合成 1层200张", 1, "200张/包", "-", 20, "箱", 99),      # 2 imported
            ("TEST200B", "NK", "合成 2层50张", 2, "50张/包", 1, 72, "箱", 88),          # 3 mid=1 pending
            ("TEST300C", "JRT", "合成卷纸", 3, "200米/卷", "-", 12, "箱", 100),         # 4 unsupported
            ("TEST400D\n（含老款TEST400E）", "IF", "合成", 3, "130抽", 4, 6, "箱", 49.9),  # 5 alias
            ("TEST500E", "IF", "合成", 3, "100抽/包", 6, 4, "箱", 50),                  # 6 multi-level
            ("TEST600F", "HT", "合成", 1, "200张/包", "-", 20, "箱", "待定"),           # 7 invalid amount
            ("TEST700G", "HT", "合成", 1, "200张/包", "-", 20, "箱", 90),               # 8 dup
            ("TEST700G", "HT", "合成", 1, "200张/包", "-", 20, "箱", 91),               # 9 dup
            ("TEST800H", "IF", "合成", 2, "100抽", 1, 96, "箱", 169),                   # 10 unclear
            ("TEST900J", "HT", "合成 抽", 1, "120抽/包", "-", 10, "箱", 0),             # 11 invalid (0)
        ]
        for r in rows:
            ws.append(list(r))
        ws.append([None] * 9)                                                         # 12 blank
        ws.append(["另，下述SKU线上均不得上架。"] + [None] * 8)                         # 13 note
        ws.append(list(SRC_HEADERS.values()))                                         # 14 header repeat
        ws.append(["TEST999Z", "HT", "合成禁售", 1, "200张/包", "-", 20, "箱", "不允许线上售卖"])  # 15
        wb.save(src)
        sha0 = sha256_file(src)
        out = Path(td) / "out"
        s = run_import(src, out)
        expect = {2: IMPORTED, 3: MID_PACK_PENDING, 4: SPEC_UNSUPPORTED, 5: ALIAS_PENDING,
                  6: SPEC_UNSUPPORTED, 7: INVALID_AMOUNT, 8: DUPLICATE, 9: DUPLICATE,
                  10: SPEC_UNCLEAR, 11: INVALID_AMOUNT, 15: BANNED}
        man = {r["source_row"]: r for r in json.loads((out / "import_manifest.json").read_text(encoding="utf-8"))}
        for rn, code in expect.items():
            got = (man.get(rn) or {}).get("decision")
            if got != code:
                fails.append(f"row {rn}: want {code} got {got}")
        if set(man) != set(expect):
            fails.append(f"item rows mismatch: {sorted(man)}")
        kinds = {n["source_row"]: n["kind"] for n in s["non_item_rows"]}
        if kinds != {1: "HEADER", 12: "BLANK", 13: "NOTE_TEXT", 14: "HEADER_REPEAT"}:
            fails.append(f"non-item rows: {kinds}")
        if s["validate"]["counts"] != {"valid": 1, "rejected": 0, "skipped": 0} or s["validate"]["valid_excel_rows"] != [2]:
            fails.append(f"validate: {s['validate']}")
        imp = man[2]["import"]
        if imp["brand"] is not None or imp["search_query"] != "TEST100A" or imp["sheet_unit"] != "张":
            fails.append(f"no-basis row must have blank brand + model query: {imp}")
        if sha256_file(src) != sha0 or not s["source_unchanged"]:
            fails.append("source must stay unchanged")
        try:
            run_import(src, out)
            fails.append("existing out_dir must be refused")
        except FileExistsError:
            pass
    return fails


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AFH 原表 → P5-D 批量输入表（离线）")
    ap.add_argument("--source", type=Path)
    ap.add_argument("--sheet", default=DEFAULT_SHEET)
    ap.add_argument("--out-dir", type=Path)
    ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args(argv)
    if a.self_check:
        f = run_self_check()
        print("=== AFH import self-check ===")
        print("OK" if not f else "FAILED:\n  " + "\n  ".join(f))
        return 0 if not f else 1
    if not a.source or not a.out_dir:
        ap.error("--source 和 --out-dir 必填（或 --self-check）")
    try:
        s = run_import(a.source, a.out_dir, sheet=a.sheet)
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        print(f"参数/文件错误: {exc}")
        return 2
    print(f"source_unchanged={s['source_unchanged']} sha256={s['source_sha256_after']}")
    print(f"item_rows={s['item_rows']} price_monitor_rows={s['price_monitor_rows']} banned_rows={s['banned_rows']}")
    print(f"counts={s['counts']}")
    print(f"non_item_rows={[(n['source_row'], n['kind']) for n in s['non_item_rows']]}")
    print(f"validate={s['validate']['counts']} rows_match_manifest={s['validate']['rows_match_manifest']}")
    for k, v in s["outputs"].items():
        print(f"{k}={v}")
    return 0 if s["validate"]["rows_match_manifest"] and s["source_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
