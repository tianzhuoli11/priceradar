"""P4-B: select selling price from OCR candidates, separate units, Decimal compare."""

from __future__ import annotations

import argparse
import csv
import json
import secrets
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPARE_ROOT = (PROJECT_ROOT / "artifacts" / "compare_runs").resolve()

# Business baselines — not OCR results.
BUSINESS = {
    "model": "B913A",
    "spec_label": "单层200抽×20包／箱",
    "packs_per_box": 20,
    "layers": 1,
    "sheets_or_pulls": 200,
    "red_line_price_cny_per_box": Decimal("127"),
    "note": "业务配置；不得回填为 OCR 识别结果",
}
RULES_VERSION = "p4b-packaging-v2"

MAIN_ROLES = frozenset(
    {"currency_tagged_price_candidate", "labeled_price_candidate"}
)
EXCLUDED_ROLES = frozenset({"promotion_threshold_or_gift"})


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_run_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def make_run_dir() -> tuple[str, Path]:
    run_id = f"{utc_run_stamp()}_{secrets.token_hex(3)}"
    out = COMPARE_ROOT / run_id
    out.mkdir(parents=True, exist_ok=False)
    return run_id, out


def to_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def box_top(box: Any) -> float:
    if not box or not isinstance(box, list):
        return 1e9
    try:
        ys = [float(pt[1]) for pt in box if isinstance(pt, (list, tuple)) and len(pt) >= 2]
        return min(ys) if ys else 1e9
    except (TypeError, ValueError):
        return 1e9


def load_extraction(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def csv_guard(value: Any) -> str:
    text = "" if value is None else str(value)
    if text[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def select_primary_selling_price(
    price_candidates: list[dict[str, Any]],
    *,
    input_quality_risks: list[str],
) -> dict[str, Any]:
    """
    Exclude explicit promo thresholds; prefer currency/label + top position.
    Ambiguity or risky crop input → no forced amount.
    """
    review: list[str] = []
    if input_quality_risks:
        review.extend(input_quality_risks)
        return {
            "selected_price": None,
            "price_basis": None,
            "selection_status": "REVIEW_REQUIRED",
            "review_reasons": review
            + ["异常/裁切样本：保留待核验，不强行填金额"],
            "eligible_candidates": [],
            "excluded_promo_thresholds": [
                p
                for p in price_candidates
                if p.get("role") in EXCLUDED_ROLES
            ],
        }

    excluded = [p for p in price_candidates if p.get("role") in EXCLUDED_ROLES]
    eligible = [
        p
        for p in price_candidates
        if p.get("role") in MAIN_ROLES and p.get("amount")
    ]
    # Prefer original variant when available.
    originals = [p for p in eligible if p.get("variant") == "original"]
    pool = originals or eligible

    if not pool:
        review.append("排除优惠门槛后无可用币种/标签主售价候选")
        return {
            "selected_price": None,
            "price_basis": None,
            "selection_status": "REVIEW_REQUIRED",
            "review_reasons": review,
            "eligible_candidates": eligible,
            "excluded_promo_thresholds": excluded,
        }

    # Group by amount
    by_amount: dict[str, list[dict[str, Any]]] = {}
    for p in pool:
        by_amount.setdefault(str(p["amount"]), []).append(p)

    if len(by_amount) > 1:
        # Position evidence: pick top-most group only if clearly separated?
        # Safer: REVIEW when multiple distinct main amounts.
        review.append(
            "多个币种/标签主售价金额歧义："
            + ",".join(sorted(by_amount.keys()))
        )
        return {
            "selected_price": None,
            "price_basis": None,
            "selection_status": "REVIEW_REQUIRED",
            "review_reasons": review,
            "eligible_candidates": pool,
            "excluded_promo_thresholds": excluded,
        }

    amount_s = next(iter(by_amount.keys()))
    # Among same amount, prefer currency evidence + higher score + top box.
    ranked = sorted(
        by_amount[amount_s],
        key=lambda p: (
            0 if p.get("currency_evidence") else 1,
            -float(p["score"] or 0),
            box_top(p.get("box")),
        ),
    )
    chosen = ranked[0]
    basis_parts = []
    if chosen.get("currency_evidence"):
        basis_parts.append(f"currency={chosen['currency_evidence']}")
    if chosen.get("nearby_label"):
        basis_parts.append(f"label={chosen['nearby_label']}")
    else:
        basis_parts.append("label=none")
    basis_parts.append(f"role={chosen.get('role')}")
    basis_parts.append(f"variant={chosen.get('variant')}")
    basis_parts.append("excluded_promotion_thresholds=yes")

    return {
        "selected_price": amount_s,
        "price_basis": ";".join(basis_parts),
        "selection_status": "SELECTED",
        "review_reasons": review,
        "chosen_candidate": chosen,
        "eligible_candidates": pool,
        "excluded_promo_thresholds": excluded,
    }


def analyze_spec_and_unit(
    specification_candidates: list[dict[str, Any]],
    model_candidates: list[dict[str, Any]],
    business: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Separate observed OCR vs business packaging basis.

    Box compare requires this-run page evidence (model + target 20-pack spec).
    User-confirmed packaging_basis may supply 20包→1箱 without OCR「箱」,
    but config alone without page match must not pass.
    """
    biz = business or BUSINESS
    packaging = biz.get("packaging_basis") if isinstance(biz.get("packaging_basis"), dict) else None

    ocr_models = sorted(
        {m.get("model") for m in model_candidates if m.get("model")}
    )
    ocr_packs = sorted(
        {
            str(s.get("packs"))
            for s in specification_candidates
            if s.get("packs") is not None
        }
    )
    ocr_layers = sorted(
        {
            str(s.get("layers"))
            for s in specification_candidates
            if s.get("layers") is not None
        }
    )
    ocr_sheets = sorted(
        {
            str(s.get("sheets_or_pulls"))
            for s in specification_candidates
            if s.get("sheets_or_pulls") is not None
        }
    )
    ocr_qty = sorted(
        {
            str(s.get("purchase_qty"))
            for s in specification_candidates
            if s.get("purchase_qty") is not None
        }
    )
    box_evidence = any(
        (s.get("unit_box_evidence") or "").startswith("text_contains_箱")
        for s in specification_candidates
    )

    business_packs = str(biz.get("packs_per_box") or "")
    biz_model = str(biz.get("model") or "")
    biz_layers = str(biz.get("layers")) if biz.get("layers") is not None else None
    biz_sheets = (
        str(biz.get("sheets_or_pulls"))
        if biz.get("sheets_or_pulls") is not None
        else None
    )

    packs_match_config = ocr_packs == [business_packs] if business_packs else False
    model_match = (
        len(ocr_models) == 1 and ocr_models[0] == biz_model
    )
    layers_match = (
        bool(biz_layers)
        and ocr_layers == [biz_layers]
    )
    # Limited alias: for confirmed B913A packaging, 抽/张 both map to sheets_per_pack.
    alias_ok = False
    if packaging and str(packaging.get("applicable_model")) == biz_model:
        aliases = packaging.get("sheet_unit_page_aliases") or []
        if aliases and biz_sheets and ocr_sheets == [biz_sheets]:
            alias_ok = True
    sheets_match = bool(biz_sheets) and (
        ocr_sheets == [biz_sheets] or alias_ok
    )

    page_spec_matched = bool(
        model_match and packs_match_config and layers_match and sheets_match
    )

    packaging_applicable = False
    packaging_reason = None
    if packaging and str(packaging.get("applicable_model")) == biz_model:
        if page_spec_matched and int(packaging.get("packs_per_box") or 0) == int(
            biz.get("packs_per_box") or 0
        ):
            packaging_applicable = True
            packaging_reason = (
                "页面型号+已选规格匹配用户确认箱规依据；"
                f"来源={packaging.get('source_name')}；"
                f"确认日={packaging.get('confirmed_date')}；"
                "不要求网页OCR出现「箱」"
            )
        else:
            packaging_reason = (
                "存在箱规依据，但本次页面型号/规格未核验匹配，不得仅凭配置换算"
            )

    # Separate records — never treat OCR packs as automatic box unit.
    unit = {
        "rules_version": RULES_VERSION,
        "ocr_recognized": {
            "models": ocr_models,
            "layers": ocr_layers,
            "sheets_or_pulls": ocr_sheets,
            "packs": ocr_packs,
            "purchase_qty_个": ocr_qty,
            "box_word_in_ocr": box_evidence,
            "note_purchase_qty": "「N个」不是包数，也不能单独证明1箱",
        },
        "business_config": {
            "model": biz.get("model"),
            "spec_label": biz.get("spec_label", biz.get("target_spec")),
            "packs_per_box": biz.get("packs_per_box"),
            "layers": biz.get("layers"),
            "sheets_or_pulls": biz.get("sheets_or_pulls"),
        },
        "packaging_basis": packaging,
        "packs_match_business_config": packs_match_config,
        "model_match_business_config": model_match,
        "layers_match_business_config": layers_match,
        "sheets_match_business_config": sheets_match,
        "page_spec_matched": page_spec_matched,
        "packaging_basis_applicable": packaging_applicable,
        "packaging_basis_note": packaging_reason,
        "box_comparable": False,
        "unit_status": "PENDING_VERIFY",
        "unit_review_reasons": [],
        "conversion": None,
    }

    reasons: list[str] = []
    if not ocr_models:
        reasons.append("OCR 未识别到型号")
    elif not model_match:
        reasons.append(f"OCR型号{ocr_models}与业务型号{biz_model}未核验匹配")
    if not ocr_packs:
        reasons.append("OCR 未识别到包数")
    elif not packs_match_config:
        reasons.append(
            f"OCR包数{ocr_packs}不是目标{business_packs}包装；不按比例换算为箱"
        )
    if not layers_match:
        reasons.append(f"OCR层数{ocr_layers}与业务层数{biz_layers}未匹配")
    if not sheets_match:
        reasons.append(
            f"OCR张/抽数{ocr_sheets}与业务{biz_sheets}未匹配"
            "（抽/张仅在已确认B913A箱规别名范围内互通）"
        )
    if not box_evidence and not packaging_applicable:
        reasons.append(
            "无网页「箱」字证据，且业务箱规依据不可应用于本次页面匹配"
        )
    if ocr_qty:
        reasons.append(
            f"识别到购买数量{ocr_qty}个；不解释为包数或箱数"
        )

    # Comparable when: page model+target-20-pack spec matched, AND
    # (OCR has 箱 OR user packaging basis applies for this matched page).
    if page_spec_matched and (box_evidence or packaging_applicable):
        unit["box_comparable"] = True
        unit["unit_status"] = "MATCHED_FOR_BOX_COMPARE"
        unit["conversion"] = {
            "rule": "target_20_packs_equals_1_box",
            "packs": business_packs,
            "boxes": "1",
            "basis": (
                "ocr_box_word"
                if box_evidence and not packaging_applicable
                else (
                    "packaging_basis_user_confirmed"
                    if packaging_applicable
                    else "ocr_box_word+packaging_basis"
                )
            ),
            "note": packaging_reason
            or "网页规格含箱单位证据且与目标20包装匹配",
        }
        # Keep informational reasons that are non-blocking (e.g. 个).
        unit["unit_review_reasons"] = [
            r for r in reasons if "购买数量" in r
        ]
    else:
        unit["box_comparable"] = False
        unit["unit_status"] = "PENDING_VERIFY"
        unit["unit_review_reasons"] = reasons

    return unit


def compare_to_red_line(
    selected_price: str | None,
    *,
    box_comparable: bool,
    selection_status: str,
    red_line: Decimal | None = None,
) -> dict[str, Any]:
    red = red_line if red_line is not None else BUSINESS["red_line_price_cny_per_box"]
    if selection_status != "SELECTED" or selected_price is None:
        return {
            "comparison_status": "NOT_EVALUATED",
            "verdict": "UNKNOWN",
            "reason": "无已选定主售价，不做破价判定",
            "red_line": format(red, "f"),
            "compared_price": None,
        }
    if not box_comparable:
        return {
            "comparison_status": "NOT_EVALUATED",
            "verdict": "UNKNOWN",
            "reason": "规格/单位尚未核验匹配，不能按箱比较",
            "red_line": format(red, "f"),
            "compared_price": selected_price,
        }

    amount = to_decimal(selected_price)
    if amount is None:
        return {
            "comparison_status": "NOT_EVALUATED",
            "verdict": "UNKNOWN",
            "reason": "主售价无法解析为 Decimal",
            "red_line": format(red, "f"),
            "compared_price": selected_price,
        }

    # Strict: below red line = break; equal does NOT count.
    if amount < red:
        verdict = "BREAK_PRICE"
        detail = f"{amount} < {red}"
    elif amount == red:
        verdict = "NOT_BREAK"
        detail = f"{amount} == {red}（等于不算破价）"
    else:
        verdict = "NOT_BREAK"
        detail = f"{amount} > {red}"

    return {
        "comparison_status": "EVALUATED",
        "verdict": verdict,
        "reason": detail,
        "red_line": format(red, "f"),
        "compared_price": format(amount, "f"),
        "currency": "CNY",
        "unit": "per_box",
    }


def build_output_row(
    *,
    model_display: str,
    spec_display: str,
    price_selection: dict[str, Any],
    unit_info: dict[str, Any],
    comparison: dict[str, Any],
    screenshot_path: str | None,
    extra_review: list[str],
) -> dict[str, Any]:
    reasons = list(
        dict.fromkeys(
            list(price_selection.get("review_reasons") or [])
            + list(unit_info.get("unit_review_reasons") or [])
            + list(extra_review)
        )
    )
    return {
        "model": model_display,
        "specification": spec_display,
        "price": price_selection.get("selected_price"),
        "price_basis": price_selection.get("price_basis"),
        "red_line_price": comparison.get("red_line"),
        "verdict": comparison.get("verdict"),
        "comparison_status": comparison.get("comparison_status"),
        "comparison_reason": comparison.get("reason"),
        "unit_status": unit_info.get("unit_status"),
        "ocr_packs": ",".join(unit_info["ocr_recognized"].get("packs") or []),
        "business_packs_per_box": unit_info["business_config"]["packs_per_box"],
        "review_reasons": "; ".join(reasons) if reasons else "",
        "screenshot_path": screenshot_path or "",
    }


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_table_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "model",
        "specification",
        "price",
        "price_basis",
        "red_line_price",
        "verdict",
        "comparison_status",
        "comparison_reason",
        "unit_status",
        "ocr_packs",
        "business_packs_per_box",
        "review_reasons",
        "screenshot_path",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: csv_guard(row.get(k)) for k in fields})


def safe_print(msg: Any) -> None:
    text = str(msg)
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def run_self_tests() -> list[str]:
    failures: list[str] = []

    # Promo threshold excluded; currency price selected.
    prices = [
        {
            "amount": "168.00",
            "currency_evidence": "¥",
            "nearby_label": None,
            "role": "currency_tagged_price_candidate",
            "variant": "original",
            "score": 0.97,
            "box": [[10, 10], [80, 10], [80, 40], [10, 40]],
            "source_text": "¥168.00",
        },
        {
            "amount": "129",
            "currency_evidence": None,
            "nearby_label": "满送",
            "role": "promotion_threshold_or_gift",
            "variant": "original",
            "score": 0.99,
            "box": [[10, 200], [200, 200], [200, 220], [10, 220]],
            "source_text": "满129元即赠",
        },
    ]
    sel = select_primary_selling_price(prices, input_quality_risks=[])
    if sel["selected_price"] != "168.00":
        failures.append(f"expected 168.00 selected, got {sel['selected_price']}")
    if sel["selection_status"] != "SELECTED":
        failures.append("expected SELECTED")

    # Cropped risk: no forced amount
    risky = select_primary_selling_price(
        prices, input_quality_risks=["输入像素偏小"]
    )
    if risky["selected_price"] is not None:
        failures.append("risky crop must not force amount")

    # Ambiguous mains
    amb = select_primary_selling_price(
        [
            {
                "amount": "168.00",
                "currency_evidence": "¥",
                "role": "currency_tagged_price_candidate",
                "variant": "original",
                "score": 0.9,
                "box": [[0, 0], [1, 0], [1, 1], [0, 1]],
            },
            {
                "amount": "99.00",
                "currency_evidence": "¥",
                "role": "currency_tagged_price_candidate",
                "variant": "original",
                "score": 0.9,
                "box": [[0, 50], [1, 50], [1, 60], [0, 60]],
            },
        ],
        input_quality_risks=[],
    )
    if amb["selected_price"] is not None or amb["selection_status"] != "REVIEW_REQUIRED":
        failures.append("ambiguous mains should REVIEW without selection")

    # Unit separation + compare (OCR 箱 word path)
    unit = analyze_spec_and_unit(
        [
            {
                "packs": "20",
                "layers": "1",
                "sheets_or_pulls": "200",
                "unit_box_evidence": "text_contains_箱_near_packs",
            }
        ],
        [{"model": "B913A"}],
    )
    if not unit["box_comparable"]:
        failures.append("matched OCR+config+箱 should be box_comparable")

    # Packaging basis path: no 箱 word, but page matched + user basis
    basis_biz = {
        **BUSINESS,
        "packaging_basis": {
            "applicable_model": "B913A",
            "packs_per_box": 20,
            "sheets_per_pack": 200,
            "sheet_unit_page_aliases": ["抽", "张"],
            "source_name": "AFH监控品项与红线价.xlsx",
            "confirmed_date": "2026-10-19",
        },
    }
    unit_basis = analyze_spec_and_unit(
        [
            {
                "packs": "20",
                "layers": "1",
                "sheets_or_pulls": "200",
                "unit_box_evidence": "20包不自动等于一箱；无明确箱单位证据",
                "purchase_qty": "1",
            }
        ],
        [{"model": "B913A"}],
        business=basis_biz,
    )
    if not unit_basis["box_comparable"]:
        failures.append("matched page + packaging_basis should be box_comparable")
    if not unit_basis.get("packaging_basis_applicable"):
        failures.append("packaging_basis_applicable should be true")

    # Config alone / missing model → not comparable
    unit_nomodel = analyze_spec_and_unit(
        [{"packs": "20", "layers": "1", "sheets_or_pulls": "200"}],
        [],
        business=basis_biz,
    )
    if unit_nomodel["box_comparable"]:
        failures.append("missing model must not be box_comparable")

    # Non-20 packs → no proportional conversion
    unit_other = analyze_spec_and_unit(
        [{"packs": "10", "layers": "1", "sheets_or_pulls": "200"}],
        [{"model": "B913A"}],
        business=basis_biz,
    )
    if unit_other["box_comparable"]:
        failures.append("non-20 packs must not auto-convert to box")

    # Config present but page model conflict → not comparable
    unit_conflict = analyze_spec_and_unit(
        [{"packs": "20", "layers": "1", "sheets_or_pulls": "200"}],
        [{"model": "B913A"}, {"model": "B913AB"}],
        business=basis_biz,
    )
    if unit_conflict["box_comparable"]:
        failures.append("model conflict must not be box_comparable")

    # Basis alone without page packs match → not comparable
    unit_config_only = analyze_spec_and_unit(
        [],
        [],
        business=basis_biz,
    )
    if unit_config_only["box_comparable"] or unit_config_only.get(
        "packaging_basis_applicable"
    ):
        failures.append("config-only must not apply packaging basis")

    cmp_ok = compare_to_red_line(
        "168.00", box_comparable=True, selection_status="SELECTED"
    )
    if cmp_ok["verdict"] != "NOT_BREAK":
        failures.append("168 > 127 should NOT_BREAK")
    cmp_break = compare_to_red_line(
        "126.99", box_comparable=True, selection_status="SELECTED"
    )
    if cmp_break["verdict"] != "BREAK_PRICE":
        failures.append("126.99 < 127 should BREAK_PRICE")
    cmp_eq = compare_to_red_line(
        "127", box_comparable=True, selection_status="SELECTED"
    )
    if cmp_eq["verdict"] != "NOT_BREAK":
        failures.append("equal 127 must NOT_BREAK")
    cmp_hi = compare_to_red_line(
        "127.01", box_comparable=True, selection_status="SELECTED"
    )
    if cmp_hi["verdict"] != "NOT_BREAK":
        failures.append("127.01 > 127 should NOT_BREAK")
    cmp_unit = compare_to_red_line(
        "100", box_comparable=False, selection_status="SELECTED"
    )
    if cmp_unit["verdict"] != "UNKNOWN":
        failures.append("without unit match must UNKNOWN")

    return failures


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PriceRadar P4-B compare from OCR extraction")
    p.add_argument(
        "--extraction",
        help="Path to extraction.json from an ocr_runs directory",
    )
    p.add_argument(
        "--screenshot",
        default="",
        help="Optional screenshot path to record in the output table",
    )
    p.add_argument(
        "--metadata",
        default="",
        help="Optional OCR run_metadata.json (for input quality risks / paths)",
    )
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args(argv)
    if not args.self_test and not args.extraction:
        p.error("--extraction is required unless --self-test")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        fails = run_self_tests()
        safe_print(f"self_test_failures={fails}")
        return 0 if not fails else 1

    extraction_path = Path(args.extraction).resolve()
    if not extraction_path.is_file():
        safe_print(f"extraction 不存在: {extraction_path}")
        return 2

    extraction = load_extraction(extraction_path)
    # Support single-image payload or nested per_image[0]
    if extraction.get("mode") == "run_dir_separate_then_merged_candidates":
        price_candidates = extraction.get("price_candidates") or []
        spec_candidates = extraction.get("specification_candidates") or []
        model_candidates = extraction.get("model_candidates") or []
        input_quality_risks = list(extraction.get("input_quality_risks") or [])
        # Also fold risks from per_image
        for item in extraction.get("per_image") or []:
            input_quality_risks.extend(
                (item.get("extraction") or {}).get("input_quality_risks") or []
            )
    else:
        price_candidates = extraction.get("price_candidates") or []
        spec_candidates = extraction.get("specification_candidates") or []
        model_candidates = extraction.get("model_candidates") or []
        input_quality_risks = list(extraction.get("input_quality_risks") or [])

    meta_risks: list[str] = []
    screenshot_path = args.screenshot or None
    if args.metadata:
        meta_path = Path(args.metadata).resolve()
        if meta_path.is_file():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            for inp in meta.get("inputs") or []:
                if not screenshot_path:
                    screenshot_path = inp.get("path")
            # size-based risk already in extraction; keep meta note only

    # Deduplicate risks
    input_quality_risks = list(dict.fromkeys(input_quality_risks + meta_risks))

    safe_print("=== PriceRadar P4-B ===")
    safe_print(f"extraction: {extraction_path}")
    safe_print(
        f"业务红线: {BUSINESS['red_line_price_cny_per_box']} 元／箱（低于才算破价）"
    )

    price_selection = select_primary_selling_price(
        price_candidates, input_quality_risks=input_quality_risks
    )
    unit_info = analyze_spec_and_unit(spec_candidates, model_candidates)
    comparison = compare_to_red_line(
        price_selection.get("selected_price"),
        box_comparable=bool(unit_info.get("box_comparable")),
        selection_status=str(price_selection.get("selection_status")),
    )

    ocr_model = ",".join(unit_info["ocr_recognized"].get("models") or []) or "UNKNOWN"
    ocr_spec_parts = []
    if unit_info["ocr_recognized"].get("layers"):
        ocr_spec_parts.append(
            "层=" + ",".join(unit_info["ocr_recognized"]["layers"])
        )
    if unit_info["ocr_recognized"].get("sheets_or_pulls"):
        ocr_spec_parts.append(
            "抽/张=" + ",".join(unit_info["ocr_recognized"]["sheets_or_pulls"])
        )
    if unit_info["ocr_recognized"].get("packs"):
        ocr_spec_parts.append(
            "包(OCR)=" + ",".join(unit_info["ocr_recognized"]["packs"])
        )
    ocr_spec_parts.append(
        f"业务包／箱={unit_info['business_config']['packs_per_box']}"
    )
    spec_display = "; ".join(ocr_spec_parts) if ocr_spec_parts else "UNKNOWN"

    row = build_output_row(
        model_display=ocr_model,
        spec_display=spec_display,
        price_selection=price_selection,
        unit_info=unit_info,
        comparison=comparison,
        screenshot_path=screenshot_path,
        extra_review=[],
    )

    run_id, out_dir = make_run_dir()
    detail = {
        "run_id": run_id,
        "phase": "P4-B",
        "created_at_utc": utc_iso(),
        "source_extraction": str(extraction_path),
        "business_config": {
            **{k: (str(v) if isinstance(v, Decimal) else v) for k, v in BUSINESS.items()}
        },
        "price_selection": price_selection,
        "unit_analysis": unit_info,
        "comparison": comparison,
        "table_row": row,
        "notes": [
            "优惠门槛已排除出主售价选择",
            "OCR包数与业务包／箱分列；核验匹配前不做按箱破价",
            "等于红线不算破价",
            "裁切/质量风险样本不强行填金额",
        ],
    }
    detail_path = out_dir / "compare_detail.json"
    table_json = out_dir / "compare_table.json"
    table_csv = out_dir / "compare_table.csv"
    write_json(detail_path, detail)
    write_json(table_json, [row])
    write_table_csv(table_csv, [row])

    safe_print(f"run_id: {run_id}")
    safe_print(f"主售价选择: {price_selection.get('selection_status')} "
               f"price={price_selection.get('selected_price')} "
               f"basis={price_selection.get('price_basis')}")
    safe_print(
        f"单位: {unit_info.get('unit_status')} "
        f"ocr_packs={unit_info['ocr_recognized'].get('packs')} "
        f"business_packs_per_box={unit_info['business_config']['packs_per_box']} "
        f"box_comparable={unit_info.get('box_comparable')}"
    )
    safe_print(
        f"比价: {comparison.get('comparison_status')} "
        f"verdict={comparison.get('verdict')} "
        f"reason={comparison.get('reason')}"
    )
    safe_print(f"compare_detail.json: {detail_path.resolve()}")
    safe_print(f"compare_table.csv: {table_csv.resolve()}")
    safe_print(f"compare_table.json: {table_json.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
