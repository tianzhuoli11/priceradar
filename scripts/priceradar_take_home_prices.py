"""
Take-home / conditional price clues from existing OCR extractions (no new selectors).

Rules version: p5a-take-home-v1
- Page primary selling price is reference only; never renamed to take-home.
- Declared 到手/券后/会员/新人 may yield POSSIBLE_BREAK when comparable & below red line.
- 预估 keeps estimate attribute.
- Threshold / coupon face amounts are not selling prices.
- manual_verdict always left empty by system.
- Evidence gaps → REVIEW_REQUIRED; never treat as NOT_BREAK / confirmed break.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

RULES_VERSION_TAKE_HOME = "p5a-take-home-v1"

PRICE_TYPE_PAGE_PRIMARY = "PAGE_PRIMARY_SELLING"
PRICE_TYPE_DECLARED_CONDITIONAL = "PAGE_DECLARED_CONDITIONAL_TAKE_HOME"
PRICE_TYPE_ESTIMATED = "PAGE_ESTIMATED_TAKE_HOME"
PRICE_TYPE_RULE_CALCULATED = "RULE_CALCULATED_TAKE_HOME"
PRICE_TYPE_VERIFIED_CHECKOUT = "VERIFIED_CHECKOUT_PAYABLE"  # never produced this phase

SYSTEM_POSSIBLE_BREAK = "POSSIBLE_BREAK"
SYSTEM_NO_BREAK_IN_SCOPE = "NO_BREAK_IN_SCOPE"
SYSTEM_REVIEW_REQUIRED = "REVIEW_REQUIRED"
SYSTEM_NOT_EVALUATED = "NOT_EVALUATED"

# Human-facing labels for reports
SYSTEM_VERDICT_ZH = {
    SYSTEM_POSSIBLE_BREAK: "疑似破价，待人工确认",
    SYSTEM_NO_BREAK_IN_SCOPE: "本次已识别价格未低于红线",
    SYSTEM_REVIEW_REQUIRED: "待核验（非未破价）",
    SYSTEM_NOT_EVALUATED: "未评价",
    "BREAK": "破价（旧规则）",
    "BREAK_PRICE": "破价（旧规则）",
    "NOT_BREAK": "未破价（旧规则）",
}

ESTIMATE_HINTS = ("预估到手", "预计到手", "预估价", "预估")
DECLARED_TAKE_HOME_HINTS = (
    "到手价",
    "券后价",
    "券后",
    "会员价",
    "新人价",
    "学生价",
    "plus价",
    "PLUS价",
    "京东价",  # weak — only if with 到手 context handled elsewhere
)
THRESHOLD_HINTS = ("满", "赠", "即赠", "满减", "满送")
COUPON_FACE_HINTS = ("优惠券", "满减券", "券面额", "元券")

QTY_HINT_RE = re.compile(
    r"(?:最低|满|每|买|购)?\s*(\d+)\s*(?:件|份|箱|包|套)|"
    r"(\d+)\s*件起|"
    r"起购\s*(\d+)",
    re.IGNORECASE,
)


def to_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def classify_ocr_amount_role(source_text: str, existing_role: str | None) -> str:
    """
    Map OCR block text + prior role into price_type / keep threshold exclusion.
    """
    t = source_text or ""
    role = existing_role or ""

    if role == "promotion_threshold_or_gift":
        return "EXCLUDED_THRESHOLD"
    if any(h in t for h in THRESHOLD_HINTS) and ("满" in t or "赠" in t or "减" in t):
        # e.g. 满129赠 — amount is not take-home
        if re.search(r"满\s*\d+", t) or "赠" in t:
            return "EXCLUDED_THRESHOLD"
    if any(h in t for h in COUPON_FACE_HINTS) and "到手" not in t and "券后" not in t:
        return "EXCLUDED_COUPON_FACE"

    if role == "estimated_take_home_candidate" or any(h in t for h in ESTIMATE_HINTS):
        return PRICE_TYPE_ESTIMATED

    if role == "declared_take_home_candidate" or any(
        h in t for h in ("到手价", "券后价", "券后", "会员价", "新人价", "学生价")
    ):
        return PRICE_TYPE_DECLARED_CONDITIONAL

    if role in {"currency_tagged_price_candidate", "labeled_price_candidate"}:
        return PRICE_TYPE_PAGE_PRIMARY

    return "UNLABELED_OR_UNKNOWN"


def parse_minimum_quantity(text: str) -> int | None:
    m = QTY_HINT_RE.search(text or "")
    if not m:
        return None
    for g in m.groups():
        if g:
            try:
                q = int(g)
                if q >= 1:
                    return q
            except ValueError:
                continue
    return None


def eligibility_from_text(text: str) -> str:
    t = text or ""
    if any(x in t for x in ("会员", "PLUS", "plus", "新人", "学生", "企业")):
        return "CONDITIONAL_UNVERIFIED"
    if any(x in t for x in ("券后", "领券", "优惠券")):
        return "COUPON_REQUIRED_UNVERIFIED"
    if "预估" in t or "预计" in t:
        return "ESTIMATE_ONLY"
    return "UNKNOWN"


def empty_clue_template() -> dict[str, Any]:
    return {
        "price_type": None,
        "amount": None,
        "raw_label": None,
        "conditions": None,
        "minimum_quantity": None,
        "eligibility_status": None,
        "evidence_source": None,
        "evidence_path": None,
        "calculation_basis": None,
        "normalized_price": None,
        "red_line": None,
        "price_delta": None,
        "system_verdict": SYSTEM_NOT_EVALUATED,
        "system_verdict_zh": SYSTEM_VERDICT_ZH[SYSTEM_NOT_EVALUATED],
        "manual_verdict": None,  # system must never fill
        "review_reasons": [],
        "low_price_hint": False,
    }


def _normalize_box_price(
    amount: Decimal,
    *,
    minimum_quantity: int | None,
    packs_per_box: int | None,
    box_comparable: bool,
    page_spec_matched: bool,
) -> tuple[Decimal | None, str | None, list[str]]:
    """
    Convert observed amount to comparison unit when rules are explicit.
    Does not invent packaging. Multi-piece: amount is for minimum_quantity pieces;
    if each piece is one box (matched packs_per_box), divide by qty.
    """
    reasons: list[str] = []
    if not page_spec_matched or not box_comparable:
        reasons.append("型号/规格/箱规证据不足，不能换算有效箱价")
        return None, None, reasons
    qty = minimum_quantity or 1
    if qty < 1:
        reasons.append("最低购买数量无效")
        return None, None, reasons
    if qty == 1:
        return amount, "1件价格按可比箱规直接作为箱价（假设当前已选=业务箱规）", reasons
    # Explicit multi-qty: price is for qty units; per-box = amount/qty when each unit is one box.
    per = (amount / Decimal(qty)).quantize(Decimal("0.01"))
    basis = (
        f"明确购买数量={qty}；金额{amount}÷{qty}={per}；"
        f"假设每件对应业务箱规({packs_per_box}包/箱)且规格已匹配"
    )
    return per, basis, reasons


def evaluate_clue_against_red_line(
    clue: dict[str, Any],
    *,
    red_line: Decimal,
    box_comparable: bool,
    page_spec_matched: bool,
    packs_per_box: int | None,
) -> dict[str, Any]:
    out = dict(clue)
    out["red_line"] = format(red_line, "f")
    out["manual_verdict"] = None
    reasons = list(out.get("review_reasons") or [])
    amount = to_decimal(out.get("amount"))
    ptype = out.get("price_type")

    if ptype == PRICE_TYPE_VERIFIED_CHECKOUT:
        reasons.append("本轮未进入结算，不能生成已验证应付价")
        out["system_verdict"] = SYSTEM_REVIEW_REQUIRED
        out["review_reasons"] = reasons
        out["system_verdict_zh"] = SYSTEM_VERDICT_ZH[SYSTEM_REVIEW_REQUIRED]
        return out

    if ptype == PRICE_TYPE_PAGE_PRIMARY:
        # Reference only — never claim as take-home; still may note low number.
        out["calculation_basis"] = (
            out.get("calculation_basis")
            or "页面主展示价（参考）；不得改名为到手价"
        )
        if amount is not None and amount < red_line:
            out["low_price_hint"] = True
            reasons.append(
                "主展示价数字低于红线，但非到手价证据；到手价待核验"
            )
        out["normalized_price"] = None
        out["price_delta"] = None
        out["system_verdict"] = SYSTEM_REVIEW_REQUIRED
        out["review_reasons"] = list(dict.fromkeys(reasons + ["到手价待核验"]))
        out["system_verdict_zh"] = SYSTEM_VERDICT_ZH[SYSTEM_REVIEW_REQUIRED]
        return out

    if ptype not in {
        PRICE_TYPE_DECLARED_CONDITIONAL,
        PRICE_TYPE_ESTIMATED,
        PRICE_TYPE_RULE_CALCULATED,
    }:
        out["system_verdict"] = SYSTEM_REVIEW_REQUIRED
        reasons.append("价格类型不足以作为到手价线索")
        out["review_reasons"] = reasons
        out["system_verdict_zh"] = SYSTEM_VERDICT_ZH[SYSTEM_REVIEW_REQUIRED]
        return out

    if amount is None:
        out["system_verdict"] = SYSTEM_REVIEW_REQUIRED
        reasons.append("金额缺失")
        out["review_reasons"] = reasons
        out["system_verdict_zh"] = SYSTEM_VERDICT_ZH[SYSTEM_REVIEW_REQUIRED]
        return out

    if not page_spec_matched or not box_comparable:
        if amount < red_line:
            out["low_price_hint"] = True
            reasons.append(
                "观察金额疑似偏低，但型号/规格/换算不足；保留低价线索标记，"
                "不伪造有效箱价"
            )
        out["normalized_price"] = None
        out["price_delta"] = None
        out["system_verdict"] = SYSTEM_REVIEW_REQUIRED
        out["review_reasons"] = reasons
        out["system_verdict_zh"] = SYSTEM_VERDICT_ZH[SYSTEM_REVIEW_REQUIRED]
        return out

    norm, basis, n_reasons = _normalize_box_price(
        amount,
        minimum_quantity=out.get("minimum_quantity"),
        packs_per_box=packs_per_box,
        box_comparable=box_comparable,
        page_spec_matched=page_spec_matched,
    )
    reasons.extend(n_reasons)
    if basis:
        out["calculation_basis"] = basis
    if norm is None:
        out["system_verdict"] = SYSTEM_REVIEW_REQUIRED
        if amount < red_line:
            out["low_price_hint"] = True
        out["review_reasons"] = reasons
        out["system_verdict_zh"] = SYSTEM_VERDICT_ZH[SYSTEM_REVIEW_REQUIRED]
        return out

    out["normalized_price"] = format(norm, "f")
    delta = norm - red_line
    out["price_delta"] = format(delta, "f")

    if ptype == PRICE_TYPE_ESTIMATED:
        reasons.append("预估到手价：保留预估属性，非已验证成交价")

    if out.get("eligibility_status") in {
        "CONDITIONAL_UNVERIFIED",
        "COUPON_REQUIRED_UNVERIFIED",
    }:
        reasons.append("账号资格/领券条件未验证")

    # Strictly below → possible break; equal is NOT below.
    if norm < red_line:
        out["system_verdict"] = SYSTEM_POSSIBLE_BREAK
        out["system_verdict_zh"] = SYSTEM_VERDICT_ZH[SYSTEM_POSSIBLE_BREAK]
    else:
        out["system_verdict"] = SYSTEM_NO_BREAK_IN_SCOPE
        out["system_verdict_zh"] = SYSTEM_VERDICT_ZH[SYSTEM_NO_BREAK_IN_SCOPE]
        reasons.append("本次已识别价格未低于红线；不能证明全部优惠方案未破价")

    out["review_reasons"] = list(dict.fromkeys(reasons))
    return out


def build_price_clues_from_extraction(
    extraction: dict[str, Any] | None,
    *,
    red_line: Decimal,
    box_comparable: bool,
    page_spec_matched: bool,
    packs_per_box: int | None,
    evidence_path: str | None = None,
) -> list[dict[str, Any]]:
    """Build one clue per OCR price candidate (dedupe by type+amount+label)."""
    ext = extraction or {}
    candidates = list(ext.get("price_candidates") or [])
    clues: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    for p in candidates:
        text = str(p.get("source_text") or "")
        mapped = classify_ocr_amount_role(text, p.get("role"))
        if mapped in {"EXCLUDED_THRESHOLD", "EXCLUDED_COUPON_FACE", "UNLABELED_OR_UNKNOWN"}:
            continue
        amount = p.get("amount")
        key = (mapped, str(amount), text[:80])
        if key in seen:
            continue
        seen.add(key)

        raw_label = p.get("nearby_label") or mapped
        for h in DECLARED_TAKE_HOME_HINTS + ESTIMATE_HINTS + ("售价", "价格", "¥", "￥"):
            if h in text:
                raw_label = h
                break

        clue = empty_clue_template()
        clue.update(
            {
                "price_type": mapped,
                "amount": amount,
                "raw_label": raw_label,
                "conditions": text[:200],
                "minimum_quantity": parse_minimum_quantity(text),
                "eligibility_status": eligibility_from_text(text),
                "evidence_source": f"ocr:{p.get('source_image')}|{p.get('variant')}",
                "evidence_path": evidence_path,
                "calculation_basis": None,
            }
        )
        clue = evaluate_clue_against_red_line(
            clue,
            red_line=red_line,
            box_comparable=box_comparable,
            page_spec_matched=page_spec_matched,
            packs_per_box=packs_per_box,
        )
        clues.append(clue)

    return clues


def aggregate_system_verdict(clues: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Job-level rollup for take-home layer (does not rewrite legacy BREAK/NOT_BREAK).
    """
    if not clues:
        return {
            "system_verdict": SYSTEM_REVIEW_REQUIRED,
            "system_verdict_zh": "到手价待核验（无可用到手价线索）",
            "manual_verdict": None,
            "has_possible_break": False,
            "take_home_clue_count": 0,
            "note": "未发现明确到手价线索；主展示价若存在仅作参考",
        }
    has_possible = any(c.get("system_verdict") == SYSTEM_POSSIBLE_BREAK for c in clues)
    take_homes = [
        c
        for c in clues
        if c.get("price_type")
        in {
            PRICE_TYPE_DECLARED_CONDITIONAL,
            PRICE_TYPE_ESTIMATED,
            PRICE_TYPE_RULE_CALCULATED,
        }
    ]
    if has_possible:
        verdict = SYSTEM_POSSIBLE_BREAK
        zh = SYSTEM_VERDICT_ZH[SYSTEM_POSSIBLE_BREAK]
    elif take_homes and all(
        c.get("system_verdict") == SYSTEM_NO_BREAK_IN_SCOPE for c in take_homes
    ):
        verdict = SYSTEM_NO_BREAK_IN_SCOPE
        zh = SYSTEM_VERDICT_ZH[SYSTEM_NO_BREAK_IN_SCOPE]
    else:
        verdict = SYSTEM_REVIEW_REQUIRED
        zh = "到手价待核验"
    return {
        "system_verdict": verdict,
        "system_verdict_zh": zh,
        "manual_verdict": None,
        "has_possible_break": has_possible,
        "take_home_clue_count": len(take_homes),
        "primary_ref_count": sum(
            1 for c in clues if c.get("price_type") == PRICE_TYPE_PAGE_PRIMARY
        ),
        "note": "多方案分别保留；未发现低价≠全部优惠未破价",
    }


def run_local_take_home_checks() -> list[str]:
    failures: list[str] = []
    red = Decimal("127")

    # Primary must not become take-home / possible break by itself
    primary = evaluate_clue_against_red_line(
        {
            **empty_clue_template(),
            "price_type": PRICE_TYPE_PAGE_PRIMARY,
            "amount": "100.00",
            "raw_label": "¥",
        },
        red_line=red,
        box_comparable=True,
        page_spec_matched=True,
        packs_per_box=20,
    )
    if primary["system_verdict"] != SYSTEM_REVIEW_REQUIRED:
        failures.append("primary below red must stay REVIEW, not POSSIBLE_BREAK")
    if primary.get("normalized_price") is not None:
        failures.append("primary must not get normalized take-home box price")

    # Declared member price below → possible break, eligibility unverified
    member = evaluate_clue_against_red_line(
        {
            **empty_clue_template(),
            "price_type": PRICE_TYPE_DECLARED_CONDITIONAL,
            "amount": "120.00",
            "raw_label": "会员价",
            "conditions": "会员价 ¥120",
            "eligibility_status": "CONDITIONAL_UNVERIFIED",
            "minimum_quantity": 1,
        },
        red_line=red,
        box_comparable=True,
        page_spec_matched=True,
        packs_per_box=20,
    )
    if member["system_verdict"] != SYSTEM_POSSIBLE_BREAK:
        failures.append("member below red should POSSIBLE_BREAK")
    if member["manual_verdict"] is not None:
        failures.append("manual_verdict must be empty")
    if "资格" not in ";".join(member.get("review_reasons") or []):
        failures.append("should note eligibility unverified")

    # Equal red → not possible break
    eq = evaluate_clue_against_red_line(
        {
            **empty_clue_template(),
            "price_type": PRICE_TYPE_DECLARED_CONDITIONAL,
            "amount": "127.00",
            "raw_label": "到手价",
            "conditions": "到手价127",
            "eligibility_status": "UNKNOWN",
            "minimum_quantity": 1,
        },
        red_line=red,
        box_comparable=True,
        page_spec_matched=True,
        packs_per_box=20,
    )
    if eq["system_verdict"] != SYSTEM_NO_BREAK_IN_SCOPE:
        failures.append("equal red must not be POSSIBLE_BREAK")

    # Estimate keeps attribute
    est = evaluate_clue_against_red_line(
        {
            **empty_clue_template(),
            "price_type": PRICE_TYPE_ESTIMATED,
            "amount": "100.00",
            "raw_label": "预估到手",
            "conditions": "预估到手价100",
            "eligibility_status": "ESTIMATE_ONLY",
            "minimum_quantity": 1,
        },
        red_line=red,
        box_comparable=True,
        page_spec_matched=True,
        packs_per_box=20,
    )
    if est["system_verdict"] != SYSTEM_POSSIBLE_BREAK:
        failures.append("estimate below red can be POSSIBLE_BREAK")
    if not any("预估" in r for r in est.get("review_reasons") or []):
        failures.append("estimate must keep 预估 note")

    # Threshold excluded from clues
    ext = {
        "price_candidates": [
            {
                "amount": "129.00",
                "role": "promotion_threshold_or_gift",
                "source_text": "满129赠抽纸",
                "source_image": "promotions.png",
                "variant": "original",
            },
            {
                "amount": "20.00",
                "role": "unlabeled_number_candidate",
                "source_text": "优惠券20元券",
                "source_image": "promotions.png",
                "variant": "original",
            },
            {
                "amount": "168.00",
                "role": "currency_tagged_price_candidate",
                "source_text": "¥168",
                "source_image": "price.png",
                "variant": "original",
                "nearby_label": None,
            },
        ]
    }
    clues = build_price_clues_from_extraction(
        ext,
        red_line=red,
        box_comparable=True,
        page_spec_matched=True,
        packs_per_box=20,
    )
    types = {c["price_type"] for c in clues}
    if PRICE_TYPE_PAGE_PRIMARY not in types:
        failures.append("should keep primary as clue type")
    if any(c["amount"] == "129.00" for c in clues):
        failures.append("threshold 129 must not become price clue")
    if any(c["amount"] == "20.00" for c in clues):
        failures.append("coupon face must not become price clue")

    # Spec insufficient → REVIEW even if amount low
    low_no_spec = evaluate_clue_against_red_line(
        {
            **empty_clue_template(),
            "price_type": PRICE_TYPE_DECLARED_CONDITIONAL,
            "amount": "90.00",
            "raw_label": "到手价",
            "conditions": "到手价90",
            "minimum_quantity": 1,
        },
        red_line=red,
        box_comparable=False,
        page_spec_matched=False,
        packs_per_box=20,
    )
    if low_no_spec["system_verdict"] != SYSTEM_REVIEW_REQUIRED:
        failures.append("missing spec must REVIEW")
    if not low_no_spec.get("low_price_hint"):
        failures.append("low amount without spec should set low_price_hint")
    if low_no_spec.get("normalized_price") is not None:
        failures.append("must not forge box price without spec")

    # Multi-qty explicit
    multi = evaluate_clue_against_red_line(
        {
            **empty_clue_template(),
            "price_type": PRICE_TYPE_RULE_CALCULATED,
            "amount": "240.00",
            "raw_label": "2件价",
            "conditions": "2件240元",
            "minimum_quantity": 2,
            "eligibility_status": "UNKNOWN",
        },
        red_line=red,
        box_comparable=True,
        page_spec_matched=True,
        packs_per_box=20,
    )
    if multi.get("normalized_price") != "120.00":
        failures.append(f"2件240 should normalize to 120, got {multi.get('normalized_price')}")
    if multi["system_verdict"] != SYSTEM_POSSIBLE_BREAK:
        failures.append("120 box equiv should POSSIBLE_BREAK vs 127")

    # Historical-style legacy verdicts not rewritten here
    if SYSTEM_VERDICT_ZH.get("NOT_BREAK") != "未破价（旧规则）":
        failures.append("legacy NOT_BREAK label should remain distinct")

    return failures
