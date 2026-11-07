"""
Excel → internal business baseline rows for P5-D batch.

Uses openpyxl only. Does not invent a second real SKU.
Synthetic/test rows must be explicitly marked.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE_PATH = (
    PROJECT_ROOT / "templates" / "batch_sku_input_template.xlsx"
)
SYNTHETIC_FIXTURE_PATH = (
    PROJECT_ROOT / "scripts" / "fixtures" / "batch_sku_input_synthetic.xlsx"
)

RULES_VERSION_EXCEL = "p5d-excel-input-v1"

# Column headers (Chinese + English key mapping)
COLUMN_SPECS: list[tuple[str, str]] = [
    ("business_sku", "业务SKU*"),
    ("brand", "品牌"),
    ("product_name", "品名"),
    ("expected_model", "期望型号*"),
    ("red_line", "红线金额*"),
    ("currency", "币种*"),
    ("comparison_unit", "比较单位*"),
    ("layers", "层数"),
    ("sheets_or_pulls", "张/抽数"),
    ("sheet_unit", "张抽单位"),
    ("packs_per_box", "每箱包数"),
    ("packaging_basis", "箱规说明/依据"),
    ("target_spec", "目标规格文案"),
    ("search_query", "搜索词"),
    ("enabled", "启用(Y/N)"),
    ("row_kind", "行类型(business/test)"),
    ("notes", "备注"),
]

HEADER_TO_KEY = {zh: key for key, zh in COLUMN_SPECS}
KEY_TO_HEADER = {key: zh for key, zh in COLUMN_SPECS}

INSTRUCTION_TEXT = """PriceRadar 批量SKU输入说明（业务验收入口）

1. 仅填写「输入」工作表；勿改表头行。本模板只含已确认业务行，不含虚构测试商品。
2. 带 * 的列为必填（启用行为 Y 时）。
3. business_sku / expected_model 按字符串保存（勿改成数字丢前导零）。
4. red_line 必须为有限、非负金额；currency 与 comparison_unit 必须填写（如 CNY、箱）。
5. 每一行独立填写型号、规格、箱规、红线与搜索词；不得默认继承 B913A。
6. 缺 packs_per_box 或规格时仍可搜索，但不能生成无依据可比箱价。
7. 空行跳过。重复 SKU 且规则冲突的行将被拒绝，不会静默覆盖。
8. enabled=N 的行不进入执行。
9. 追加第二业务SKU时请完整填写该行全部字段；未提供前不要编造真实SKU。
10. 离线/虚构测试请用 scripts/fixtures/batch_sku_input_synthetic.xlsx，勿写入本业务模板。
11. 使用 --validate-only 可只校验、不访问京东。

第二SKU最小信息清单（启用前必填）：
business_sku, expected_model, red_line, currency, comparison_unit,
以及建议填写：brand, product_name, packs_per_box, target_spec 或 sheets/layers,
packaging_basis, search_query, enabled=Y, row_kind=business。
"""


def _cell_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value).strip()


def _parse_bool_enabled(value: Any) -> bool | None:
    if value is None or value == "":
        return True  # blank → enabled by default
    s = _cell_str(value).upper()
    if s in {"Y", "YES", "TRUE", "1", "是", "启用"}:
        return True
    if s in {"N", "NO", "FALSE", "0", "否", "停用"}:
        return False
    return None


def _parse_decimal_amount(value: Any, *, field: str, excel_row: int) -> tuple[Decimal | None, str | None]:
    if value is None or _cell_str(value) == "":
        return None, f"Excel行{excel_row}: 缺少{field}"
    try:
        # Avoid float binary noise when Excel stores number
        if isinstance(value, float):
            text = format(value, "f").rstrip("0").rstrip(".")
            if "." not in text:
                text = f"{text}.00"
            d = Decimal(text)
        else:
            d = Decimal(_cell_str(value).replace(",", ""))
    except (InvalidOperation, TypeError, ValueError):
        return None, f"Excel行{excel_row}: {field}不是有效金额: {value!r}"
    if not d.is_finite():
        return None, f"Excel行{excel_row}: {field}必须为有限数字"
    if d < 0:
        return None, f"Excel行{excel_row}: {field}不能为负"
    return d, None


def _parse_optional_int(value: Any, *, field: str, excel_row: int) -> tuple[int | None, str | None]:
    if value is None or _cell_str(value) == "":
        return None, None
    try:
        n = int(Decimal(_cell_str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return None, f"Excel行{excel_row}: {field}不是整数: {value!r}"
    if n <= 0:
        return None, f"Excel行{excel_row}: {field}必须为正整数"
    return n, None


def fingerprint_payload(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def write_batch_input_template(path: Path | None = None) -> Path:
    """Create fillable .xlsx with instructions + one confirmed B913A business row."""
    out = Path(path) if path else DEFAULT_TEMPLATE_PATH
    out.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    ws_help = wb.active
    ws_help.title = "填写说明"
    ws_help["A1"] = INSTRUCTION_TEXT
    ws_help["A1"].alignment = Alignment(wrap_text=True, vertical="top")
    ws_help.column_dimensions["A"].width = 100
    ws_help.row_dimensions[1].height = 220

    ws = wb.create_sheet("输入", 0)
    headers = [zh for _, zh in COLUMN_SPECS]
    for col, h in enumerate(headers, start=1):
        cell = ws.cell(1, col, h)
        cell.font = Font(bold=True)
        ws.column_dimensions[get_column_letter(col)].width = min(18, max(12, len(h) + 2))

    # Confirmed B913A business row (from config/products/b913a.json)
    b913a = {
        "business_sku": "B913A",
        "brand": "清风",
        "product_name": "1层三折200张擦手纸",
        "expected_model": "B913A",
        "red_line": "127.00",
        "currency": "CNY",
        "comparison_unit": "箱",
        "layers": 1,
        "sheets_or_pulls": 200,
        "sheet_unit": "张",
        "packs_per_box": 20,
        "packaging_basis": "AFH监控品项与红线价.xlsx；仅当页面型号与已选规格匹配本依据时按20包=1箱",
        "target_spec": "单层200抽×20包",
        "search_query": "清风 B913A",
        "enabled": "Y",
        "row_kind": "business",
        "notes": "已确认业务输入；红线127元/箱",
    }
    for col, (key, _) in enumerate(COLUMN_SPECS, start=1):
        ws.cell(2, col, b913a.get(key))

    # Row 3 left blank for the next real business SKU (no invented values).
    for col in range(1, len(COLUMN_SPECS) + 1):
        ws.cell(3, col, None)

    dv = DataValidation(type="list", formula1='"Y,N"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add("O2:O500")
    dv2 = DataValidation(type="list", formula1='"business,test"', allow_blank=True)
    ws.add_data_validation(dv2)
    dv2.add("P2:P500")

    wb.save(out)
    return out.resolve()


def write_synthetic_batch_fixture(path: Path | None = None) -> Path:
    """
    Offline/synthetic workbook only. Must not be used as business default or
    claimed as live JD multi-SKU acceptance.
    """
    out = Path(path) if path else SYNTHETIC_FIXTURE_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    # Start from business template shape, then add explicit synthetic rows.
    write_batch_input_template(out)
    wb = load_workbook(out)
    ws = wb["输入"]
    help_ws = wb["填写说明"]
    help_ws["A1"] = (
        "synthetic/offline fixture — 虚构测试数据；"
        "不得写入真实价格历史或伪称京东实测；业务验收请用 templates/batch_sku_input_template.xlsx"
    )
    test_row = {
        "business_sku": "TEST_SYNTH_001",
        "brand": "测试品牌",
        "product_name": "离线测试品（非真实业务）",
        "expected_model": "TEST_SYNTH_001",
        "red_line": "99.00",
        "currency": "CNY",
        "comparison_unit": "箱",
        "layers": 1,
        "sheets_or_pulls": 100,
        "sheet_unit": "张",
        "packs_per_box": 10,
        "packaging_basis": "synthetic/offline only",
        "target_spec": "测试规格100张×10包",
        "search_query": "测试 请勿用于京东",
        "enabled": "Y",
        "row_kind": "test",
        "notes": "虚构测试行；仅 fixtures；不得当作业务默认",
    }
    for col, (key, _) in enumerate(COLUMN_SPECS, start=1):
        ws.cell(3, col, test_row.get(key))
    wb.save(out)
    return out.resolve()


def _row_dict_from_sheet(ws, excel_row: int, header_map: dict[int, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col, key in header_map.items():
        out[key] = ws.cell(excel_row, col).value
    return out


def _normalize_row(
    raw: dict[str, Any], *, excel_row: int
) -> tuple[dict[str, Any] | None, list[str]]:
    """Return (normalized_item or None if skip), errors."""
    errors: list[str] = []
    # Skip completely empty rows
    if all(_cell_str(v) == "" for v in raw.values()):
        return None, []

    enabled = _parse_bool_enabled(raw.get("enabled"))
    if enabled is None:
        errors.append(f"Excel行{excel_row}: enabled 只能是 Y/N")
        return None, errors
    if not enabled:
        return {
            "excel_row": excel_row,
            "enabled": False,
            "skipped": True,
            "skip_reason": "enabled=N",
            "business_sku": _cell_str(raw.get("business_sku")),
        }, []

    sku = _cell_str(raw.get("business_sku"))
    model = _cell_str(raw.get("expected_model"))
    if not sku:
        errors.append(f"Excel行{excel_row}: 缺少 business_sku")
    if not model:
        errors.append(f"Excel行{excel_row}: 缺少 expected_model")

    currency = _cell_str(raw.get("currency")).upper()
    unit = _cell_str(raw.get("comparison_unit"))
    if not currency:
        errors.append(f"Excel行{excel_row}: 缺少 currency（红线币种必须明确）")
    if not unit:
        errors.append(f"Excel行{excel_row}: 缺少 comparison_unit（比较单位必须明确）")

    red, err = _parse_decimal_amount(raw.get("red_line"), field="red_line", excel_row=excel_row)
    if err:
        errors.append(err)

    packs, err_p = _parse_optional_int(
        raw.get("packs_per_box"), field="packs_per_box", excel_row=excel_row
    )
    if err_p:
        errors.append(err_p)
    layers, err_l = _parse_optional_int(raw.get("layers"), field="layers", excel_row=excel_row)
    if err_l:
        errors.append(err_l)
    sheets, err_s = _parse_optional_int(
        raw.get("sheets_or_pulls"), field="sheets_or_pulls", excel_row=excel_row
    )
    if err_s:
        errors.append(err_s)

    row_kind = _cell_str(raw.get("row_kind")).lower() or "business"
    if row_kind not in {"business", "test"}:
        errors.append(f"Excel行{excel_row}: row_kind 只能是 business 或 test")

    if errors:
        return None, errors

    brand = _cell_str(raw.get("brand"))
    product_name = _cell_str(raw.get("product_name"))
    sheet_unit = _cell_str(raw.get("sheet_unit"))
    packaging_basis_text = _cell_str(raw.get("packaging_basis"))
    target_spec = _cell_str(raw.get("target_spec"))
    if not target_spec and (sheets or packs):
        # Build descriptive spec only from this row's fields — never inherit B913A.
        parts = []
        if layers:
            parts.append(f"{layers}层")
        if sheets:
            parts.append(f"{sheets}{sheet_unit or '张'}")
        if packs:
            parts.append(f"×{packs}包")
        target_spec = "".join(parts) if parts else ""

    search_query = _cell_str(raw.get("search_query"))
    if not search_query:
        search_query = " ".join(p for p in (brand, model) if p)

    packaging_complete = bool(packs and target_spec)
    aliases = []
    if sheet_unit:
        aliases.append(sheet_unit)
        if sheet_unit == "张":
            aliases.append("抽")
        elif sheet_unit == "抽":
            aliases.append("张")

    packaging_basis = None
    if packaging_complete:
        packaging_basis = {
            "applicable_model": model,
            "product_name": product_name or None,
            "layers": layers,
            "sheets_per_pack": sheets,
            "sheet_unit_business": sheet_unit or None,
            "sheet_unit_page_aliases": aliases or None,
            "packs_per_box": packs,
            "comparison_unit": unit,
            "red_line_cny_per_box": format(red, "f") if currency == "CNY" else None,
            "break_rule": "strictly_below_red_line",
            "source_note": packaging_basis_text or "Excel输入",
            "scope": (
                "仅当本次页面型号与已选规格匹配本依据时可换算；"
                "仅有配置、无本次页面证据时不得通过核验。"
            ),
        }

    item = {
        "excel_row": excel_row,
        "enabled": True,
        "skipped": False,
        "row_kind": row_kind,
        "is_synthetic_test": row_kind == "test",
        "business_sku": sku,
        "expected_model": model,
        "expected_brand": brand or None,
        "expected_product_name": product_name or None,
        "red_line": format(red, "f"),
        "currency": currency,
        "comparison_unit": unit,
        "layers": layers,
        "sheets_or_pulls": sheets,
        "sheet_unit": sheet_unit or None,
        "sheet_unit_aliases": aliases or None,
        "packs_per_box": packs,
        "target_spec": target_spec or "",
        "packaging_basis_text": packaging_basis_text or None,
        "packaging_basis": packaging_basis,
        "packaging_complete": packaging_complete,
        "search_query": search_query,
        "notes": _cell_str(raw.get("notes")) or None,
        "warnings": [],
    }
    if not packaging_complete:
        item["warnings"].append(
            "箱规/规格不完整：允许搜索，但不能生成无依据的可比箱价"
        )
    if item["is_synthetic_test"]:
        item["warnings"].append(
            "synthetic/offline 测试行：不得写入真实价格历史或伪称京东实测"
        )
    return item, []


def _rule_signature(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        item.get("expected_model"),
        item.get("red_line"),
        item.get("currency"),
        item.get("comparison_unit"),
        item.get("packs_per_box"),
        item.get("layers"),
        item.get("sheets_or_pulls"),
        item.get("target_spec"),
        item.get("search_query"),
    )


def parse_batch_excel(path: Path) -> dict[str, Any]:
    """
    Parse and validate batch Excel.
    Returns valid_items, rejected_errors, skipped, fingerprint.
    """
    path = Path(path).resolve()
    if not path.is_file():
        return {
            "ok": False,
            "errors": [f"文件不存在: {path}"],
            "valid_items": [],
            "rejected": [],
            "skipped": [],
            "input_fingerprint": None,
        }

    wb = load_workbook(path, data_only=True)
    if "输入" in wb.sheetnames:
        ws = wb["输入"]
    else:
        ws = wb[wb.sheetnames[0]]

    header_map: dict[int, str] = {}
    for col in range(1, ws.max_column + 1):
        h = _cell_str(ws.cell(1, col).value)
        if not h:
            continue
        key = HEADER_TO_KEY.get(h) or (h if h in KEY_TO_HEADER else None)
        if key:
            header_map[col] = key

    required_headers = {
        "business_sku",
        "expected_model",
        "red_line",
        "currency",
        "comparison_unit",
    }
    missing = required_headers - set(header_map.values())
    if missing:
        return {
            "ok": False,
            "errors": [f"表头缺少必要列: {sorted(missing)}"],
            "valid_items": [],
            "rejected": [],
            "skipped": [],
            "input_fingerprint": None,
        }

    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen_sku: dict[str, dict[str, Any]] = {}

    for excel_row in range(2, ws.max_row + 1):
        raw = _row_dict_from_sheet(ws, excel_row, header_map)
        item, errs = _normalize_row(raw, excel_row=excel_row)
        if errs:
            rejected.append({"excel_row": excel_row, "errors": errs, "raw_sku": _cell_str(raw.get("business_sku"))})
            continue
        if item is None:
            continue
        if item.get("skipped"):
            skipped.append(item)
            continue

        sku = item["business_sku"]
        if sku in seen_sku:
            prev = seen_sku[sku]
            if _rule_signature(prev) != _rule_signature(item):
                rejected.append(
                    {
                        "excel_row": excel_row,
                        "errors": [
                            f"Excel行{excel_row}: 重复SKU {sku} 与行{prev['excel_row']}规则冲突，已拒绝本行（不静默覆盖）"
                        ],
                        "raw_sku": sku,
                    }
                )
                continue
            # identical rules → skip duplicate
            skipped.append(
                {
                    **item,
                    "skipped": True,
                    "skip_reason": f"与行{prev['excel_row']}重复且规则一致，跳过",
                }
            )
            continue

        seen_sku[sku] = item
        item_id = f"item_{len(valid)+1:03d}_{re.sub(r'[^A-Za-z0-9_-]+', '_', sku)[:24]}"
        item["item_id"] = item_id
        valid.append(item)

    fp = fingerprint_payload(
        [
            {
                k: v
                for k, v in it.items()
                if k
                not in {
                    "item_id",
                    "warnings",
                }
            }
            for it in valid
        ]
    )
    return {
        "ok": bool(valid),
        "has_valid_items": bool(valid),
        "errors": [],
        "valid_items": valid,
        "rejected": rejected,
        "skipped": skipped,
        "input_fingerprint": fp,
        "rules_version": RULES_VERSION_EXCEL,
        "source_path": str(path),
        "counts": {
            "valid": len(valid),
            "rejected": len(rejected),
            "skipped": len(skipped),
        },
    }


def item_to_business_config_dict(item: dict[str, Any]) -> dict[str, Any]:
    """Convert Excel item to same shape as config/products/*.json (no historical JD id)."""
    aliases = item.get("sheet_unit_aliases")
    cfg = {
        "business_sku": item["business_sku"],
        "expected_brand": item.get("expected_brand"),
        "expected_model": item["expected_model"],
        "expected_product_name": item.get("expected_product_name"),
        "target_spec": item.get("target_spec") or "",
        "layers": item.get("layers"),
        "sheets_or_pulls": item.get("sheets_or_pulls"),
        "sheet_unit_aliases": aliases,
        "packs_per_box": item.get("packs_per_box") or 0,
        "red_line": item["red_line"],
        "currency": item["currency"],
        "comparison_unit": item["comparison_unit"],
        "packaging_basis": item.get("packaging_basis"),
        "note": (
            "来自Excel批量输入；无历史京东商品ID覆盖。"
            + (" synthetic/offline测试行。" if item.get("is_synthetic_test") else "")
        ),
        "_from_excel_row": item.get("excel_row"),
        "_packaging_complete": bool(item.get("packaging_complete")),
        "_search_query": item.get("search_query"),
        "_row_kind": item.get("row_kind"),
    }
    # load_business_baseline requires packs_per_box > 0 — for incomplete packaging
    # write a placeholder file that batch will handle via soft baseline.
    return cfg


def write_item_config(item: dict[str, Any], path: Path) -> Path:
    """
    Write JSON config for one item.
    If packaging incomplete, still write packs_per_box=1 placeholder with flag
    so search can run but compare stays non-comparable (handled in notes).
    Prefer real packs when present.
    """
    cfg = item_to_business_config_dict(item)
    if not item.get("packaging_complete"):
        # Satisfy schema; packaging_basis null → cannot claim box comparable.
        cfg["packs_per_box"] = int(item.get("packs_per_box") or 1)
        cfg["target_spec"] = cfg.get("target_spec") or "规格待补全"
        cfg["packaging_basis"] = None
        cfg["note"] = (cfg.get("note") or "") + " 箱规不完整：禁止无依据可比价。"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def run_excel_self_checks() -> list[str]:
    failures: list[str] = []
    tmp = PROJECT_ROOT / "artifacts" / "_tmp_excel_checks"
    tmp.mkdir(parents=True, exist_ok=True)
    tpl = write_batch_input_template(tmp / "template_check.xlsx")

    parsed = parse_batch_excel(tpl)
    # Business template: only B913A (+ blank row skipped)
    if parsed["counts"]["valid"] != 1:
        failures.append(f"template should yield 1 valid row, got {parsed['counts']}")
    if any(
        (it.get("business_sku") or "").startswith("TEST_")
        for it in parsed.get("valid_items") or []
    ):
        failures.append("business template must not contain TEST_ synthetic SKUs")
    if parsed["valid_items"][0]["business_sku"] != "B913A":
        failures.append("B913A row missing")
    if parsed["valid_items"][0]["red_line"] != "127.00":
        failures.append("B913A red_line wrong")

    fix = write_synthetic_batch_fixture(tmp / "synthetic_fixture.xlsx")
    pf = parse_batch_excel(fix)
    if pf["counts"]["valid"] < 2:
        failures.append("synthetic fixture should include B913A + TEST row")
    if not any(it.get("is_synthetic_test") for it in pf["valid_items"]):
        failures.append("synthetic fixture must mark test rows")

    # Conflict duplicate SKU
    wb = load_workbook(tpl)
    ws = wb["输入"]
    # copy B913A with different red line
    for col in range(1, ws.max_column + 1):
        ws.cell(4, col, ws.cell(2, col).value)
    ws.cell(4, 5, "100.00")  # red_line column — find by header
    # Find red_line col
    for col in range(1, ws.max_column + 1):
        if HEADER_TO_KEY.get(_cell_str(ws.cell(1, col).value)) == "red_line":
            ws.cell(4, col, "100.00")
        if HEADER_TO_KEY.get(_cell_str(ws.cell(1, col).value)) == "enabled":
            ws.cell(4, col, "Y")
        if HEADER_TO_KEY.get(_cell_str(ws.cell(1, col).value)) == "row_kind":
            ws.cell(4, col, "business")
    conflict_path = tmp / "conflict.xlsx"
    wb.save(conflict_path)
    p2 = parse_batch_excel(conflict_path)
    if not p2["rejected"]:
        failures.append("conflicting duplicate SKU should be rejected")

    # Amount / unit validation
    wb2 = Workbook()
    ws2 = wb2.active
    ws2.title = "输入"
    for col, (_, zh) in enumerate(COLUMN_SPECS, start=1):
        ws2.cell(1, col, zh)
    # negative red line
    row = {
        "business_sku": "X1",
        "expected_model": "X1",
        "red_line": "-1",
        "currency": "CNY",
        "comparison_unit": "箱",
        "enabled": "Y",
        "row_kind": "test",
    }
    for col, (key, _) in enumerate(COLUMN_SPECS, start=1):
        ws2.cell(2, col, row.get(key))
    bad = tmp / "bad_amount.xlsx"
    wb2.save(bad)
    p3 = parse_batch_excel(bad)
    if not p3["rejected"]:
        failures.append("negative red_line should reject")

    # Two configs must not share red line / packs when different
    a = deepcopy(parsed["valid_items"][0])
    b = deepcopy(a)
    b["business_sku"] = "TEST_SYNTH_002"
    b["expected_model"] = "TEST_SYNTH_002"
    b["red_line"] = "50.00"
    b["packs_per_box"] = 10
    b["row_kind"] = "test"
    b["is_synthetic_test"] = True
    b["search_query"] = "test only"
    ca = item_to_business_config_dict(a)
    cb = item_to_business_config_dict(b)
    if ca["red_line"] == cb["red_line"]:
        failures.append("different SKUs must keep distinct red_line")
    if int(ca.get("packs_per_box") or 0) == int(cb.get("packs_per_box") or 0):
        failures.append("packs_per_box must not silently share across SKUs")

    return failures


if __name__ == "__main__":
    import sys

    if "--write-template" in sys.argv:
        p = write_batch_input_template()
        print(p)
        raise SystemExit(0)
    if "--write-synthetic-fixture" in sys.argv:
        p = write_synthetic_batch_fixture()
        print(p)
        raise SystemExit(0)
    fails = run_excel_self_checks()
    print("excel self-check:", "OK" if not fails else fails)
    raise SystemExit(0 if not fails else 1)
