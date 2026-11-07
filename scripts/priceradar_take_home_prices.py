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

RULES_VERSION_TAKE_HOME = "p5a-take-home-v4"

SIGNAL_STRENGTH_COMPARABLE_PRIMARY = "PRIMARY_BELOW_RED_COMPARABLE"
SIGNAL_STRENGTH_WEAK_AMOUNT = "AMOUNT_BELOW_RED_MATCH_UNCLEAR"

PRICE_TYPE_PAGE_PRIMARY = "PAGE_PRIMARY_SELLING"
PRICE_TYPE_DECLARED_CONDITIONAL = "PAGE_DECLARED_CONDITIONAL_TAKE_HOME"
PRICE_TYPE_ESTIMATED = "PAGE_ESTIMATED_TAKE_HOME"
PRICE_TYPE_RULE_CALCULATED = "RULE_CALCULATED_TAKE_HOME"
PRICE_TYPE_CONDITIONAL_CANDIDATE = "PAGE_CONDITIONAL_AMOUNT_CANDIDATE"
PRICE_TYPE_UNVERIFIED_PANEL = "PAGE_UNVERIFIED_PANEL_AMOUNT"
PRICE_TYPE_VERIFIED_CHECKOUT = "VERIFIED_CHECKOUT_PAYABLE"  # never produced this phase

# Types that never enter primary / take-home / lowest-price rollups.
NON_STAT_PRICE_TYPES = {
    PRICE_TYPE_UNVERIFIED_PANEL,
    PRICE_TYPE_CONDITIONAL_CANDIDATE,
}

QTY_DEPENDENT_HINTS = (
    "件价",
    "件折扣",
    "件优惠",
    "满",
    "每满",
    "买",
    "组合价",
    "套装价",
    "总价",
    "合计",
    "共",
)
UNIT_OR_TOTAL_AMBIGUOUS_HINTS = ("总价", "合计", "共", "打包价", "组合总价")

SYSTEM_POSSIBLE_BREAK = "POSSIBLE_BREAK"
SYSTEM_NO_BREAK_IN_SCOPE = "NO_BREAK_IN_SCOPE"
SYSTEM_REVIEW_REQUIRED = "REVIEW_REQUIRED"
SYSTEM_NOT_EVALUATED = "NOT_EVALUATED"

# Human-facing labels for reports
SYSTEM_VERDICT_ZH = {
    SYSTEM_POSSIBLE_BREAK: "疑似破价，待人工确认",
    SYSTEM_NO_BREAK_IN_SCOPE: "本次已识别的可比价格未低于红线",
    SYSTEM_REVIEW_REQUIRED: "待核验（非未破价）",
    SYSTEM_NOT_EVALUATED: "未评价",
    "BREAK": "破价（旧规则）",
    "BREAK_PRICE": "破价（旧规则）",
    "NOT_BREAK": "未破价（旧规则）",
}

ESTIMATE_HINTS = ("预估到手", "预计到手", "预估价", "预估")
DECLARED_TAKE_HOME_HINTS = (
    "首单到手价",
    "首单到手",
    "首单",
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
        h in t
        for h in (
            "首单到手价",
            "首单到手",
            "首单",
            "到手价",
            "券后价",
            "券后",
            "会员价",
            "新人价",
            "学生价",
        )
    ):
        return PRICE_TYPE_DECLARED_CONDITIONAL

    if role in {"currency_tagged_price_candidate", "labeled_price_candidate"}:
        return PRICE_TYPE_PAGE_PRIMARY

    return "UNLABELED_OR_UNKNOWN"


PANEL_NON_PRICE_HINTS = ("累计评价", "降价通知", "评价", "条评论")
PANEL_REFERENCE_HINTS = ("原价", "划线", "京东价", "吊牌")
PANEL_TAKE_HOME_LABEL_RE = re.compile(
    r"(首单到手价|首单到手|到手价|券后价|会员价|新人价|学生价|预估到手价|预估到手|预计到手)"
)


def _panel_window(text: str, start: int, end: int, *, before: int = 48, after: int = 64) -> str:
    return text[max(0, start - before) : min(len(text), end + after)]


def _amount_label_distance(
    amount_start: int, amount_end: int, label_start: int, label_end: int
) -> int:
    if amount_end <= label_start:
        return label_start - amount_end
    if label_end <= amount_start:
        return amount_start - label_end
    return 0


def parse_panel_price_clues(
    panel_text: str,
    *,
    evidence_source: str,
    evidence_path: str | None = None,
    evidence_meta: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Parse multi-amount price panel text (DOM discover / selection_state).

    Does not hard-code amounts. Does not assign roles solely by label left/right
    position. When a take-home label is nearest to more than one amount (tie),
    keep candidates with PANEL_LABEL_NON_UNIQUE and do not force a winner.
    """
    raw = (panel_text or "").replace("\n", " ").strip()
    if not raw:
        return []
    meta = dict(evidence_meta or {})
    amounts: list[dict[str, Any]] = []
    for m in re.finditer(r"[¥￥]\s*(\d+(?:\.\d+)?)", raw):
        amounts.append(
            {
                "amount": m.group(1),
                "start": m.start(),
                "end": m.end(),
                "window": _panel_window(raw, m.start(), m.end()),
            }
        )
    labels: list[dict[str, Any]] = []
    for m in PANEL_TAKE_HOME_LABEL_RE.finditer(raw):
        labels.append(
            {"label": m.group(1), "start": m.start(), "end": m.end(), "kind": m.group(1)}
        )

    # Nearest-label ownership; ties → non-unique for that label.
    ownership: dict[int, dict[str, Any]] = {}
    for li, lab in enumerate(labels):
        scored: list[tuple[int, int]] = []
        for ai, am in enumerate(amounts):
            dist = _amount_label_distance(
                am["start"], am["end"], lab["start"], lab["end"]
            )
            scored.append((dist, ai))
        scored.sort(key=lambda x: x[0])
        if not scored:
            continue
        best_d = scored[0][0]
        tied = [ai for d, ai in scored if d == best_d or (best_d <= 8 and d <= best_d + 4)]
        # Tight cluster around the same label (common: ¥A 首单到手价 ¥B).
        if len(tied) > 1:
            for ai in tied:
                ownership[ai] = {
                    "label": lab["label"],
                    "unique": False,
                    "distance": best_d,
                    "label_index": li,
                }
        else:
            ai = scored[0][1]
            prev = ownership.get(ai)
            if prev is None or (prev.get("unique") and scored[0][0] < int(prev.get("distance") or 10**9)):
                ownership[ai] = {
                    "label": lab["label"],
                    "unique": True,
                    "distance": scored[0][0],
                    "label_index": li,
                }
            elif prev and prev.get("unique"):
                # Competing unique claims from different labels → demote.
                ownership[ai] = {
                    "label": lab["label"],
                    "unique": False,
                    "distance": scored[0][0],
                    "label_index": li,
                }

    clues: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for ai, am in enumerate(amounts):
        window = am["window"]
        if any(h in window for h in PANEL_NON_PRICE_HINTS) and not any(
            h in window for h in DECLARED_TAKE_HOME_HINTS + ESTIMATE_HINTS
        ):
            # Amount sitting only in evaluation chrome (e.g. after 累计评价).
            if "累计评价" in window or "降价通知" in window:
                continue
        own = ownership.get(ai)
        label = (own or {}).get("label")
        unique = bool((own or {}).get("unique"))
        is_estimate = any(h in window for h in ESTIMATE_HINTS) or (
            "预估" in (label or "") or "预计" in (label or "")
        )
        if is_estimate and unique:
            ptype = PRICE_TYPE_ESTIMATED
            assoc = "PANEL_LABEL_UNIQUE"
            basis = "价区面板唯一邻近预估标签（DOM 文本；非图片 OCR）"
        elif own and unique:
            ptype = PRICE_TYPE_DECLARED_CONDITIONAL
            assoc = "PANEL_LABEL_UNIQUE"
            basis = (
                "价区面板唯一邻近条件到手标签（DOM 文本；非图片 OCR）；"
                "资格是否满足仍未知"
            )
        elif own and not unique:
            ptype = PRICE_TYPE_CONDITIONAL_CANDIDATE
            assoc = "PANEL_LABEL_NON_UNIQUE"
            basis = (
                "同区多个金额与同一条件标签距离接近，关联不唯一；"
                "保留候选待核验，不强行选定"
            )
            label = label or "条件到手价（关联待核验）"
        elif any(h in window for h in PANEL_REFERENCE_HINTS):
            ptype = PRICE_TYPE_UNVERIFIED_PANEL
            assoc = "PANEL_REFERENCE_HINT"
            label = label or "面板参考金额（未证实）"
            basis = "面板参考/划线类提示；角色未证实，不计入主售价或到手价统计"
        else:
            ptype = PRICE_TYPE_UNVERIFIED_PANEL
            assoc = "PANEL_AMOUNT_UNASSIGNED"
            label = label or "面板未证实金额"
            basis = "面板金额无唯一标签归属；不计入主售价/到手价/最低价统计"
        key = (ptype, am["amount"], label or "")
        if key in seen:
            continue
        seen.add(key)
        cond = (
            f"panel={raw[:180]} | amount_span=[{am['start']},{am['end']}]"
            f" | label={label or 'none'} | assoc={assoc}"
        )[:240]
        clue = empty_clue_template()
        clue.update(
            {
                "price_type": ptype,
                "amount": am["amount"],
                "raw_label": label or ptype,
                "conditions": cond,
                "minimum_quantity": parse_minimum_quantity(window),
                "eligibility_status": eligibility_from_text(window),
                "evidence_source": evidence_source,
                "evidence_path": evidence_path,
                "evidence_kind": meta.get("evidence_kind") or "DOM_PANEL_TEXT",
                "evidence_parts": [
                    {
                        "kind": "DOM_PANEL_TEXT",
                        "source": evidence_source,
                        "text": raw[:240],
                        "amount": am["amount"],
                        "amount_span": [am["start"], am["end"]],
                        "label": label,
                        "association": assoc,
                        "product_id": meta.get("product_id"),
                        "captured_at_utc": meta.get("captured_at_utc"),
                        "selected_spec_text": meta.get("selected_spec_text"),
                        "purchase_quantity": meta.get("purchase_quantity"),
                    }
                ],
                "calculation_basis": basis,
                "association_confidence": assoc,
                "panel_amount_index": ai,
                "panel_full_text": raw[:240],
            }
        )
        clues.append(clue)
    return clues


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
    if any(x in t for x in ("会员", "PLUS", "plus", "新人", "学生", "企业", "首单")):
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


def text_implies_qty_dependent_deal(text: str | None, minimum_quantity: int | None) -> bool:
    t = text or ""
    if minimum_quantity is not None and minimum_quantity > 1:
        return True
    if re.search(r"\d+\s*件", t) and any(
        h in t for h in ("件价", "件折", "满", "买", "优惠", "折扣")
    ):
        return True
    if any(h in t for h in ("N件", "多件", "件起")):
        return True
    return False


def text_unit_or_total_ambiguous(text: str | None) -> bool:
    t = text or ""
    if any(h in t for h in UNIT_OR_TOTAL_AMBIGUOUS_HINTS):
        # Explicit per-piece language cancels ambiguity.
        if re.search(r"(单价|每件|每箱|一件)", t):
            return False
        return True
    return False


def _normalize_box_price(
    amount: Decimal,
    *,
    minimum_quantity: int | None,
    packs_per_box: int | None,
    box_comparable: bool,
    page_spec_matched: bool,
    conditions_text: str | None = None,
) -> tuple[Decimal | None, str | None, list[str]]:
    """
    Convert observed amount to comparison unit when rules are explicit.
    Does not invent packaging or invent purchase qty=1 for qty-dependent deals.
    """
    reasons: list[str] = []
    if not page_spec_matched or not box_comparable:
        reasons.append("型号/规格/箱规证据不足，不能换算有效箱价")
        return None, None, reasons
    if text_unit_or_total_ambiguous(conditions_text):
        reasons.append("无法判断金额是单价还是总价，不做箱价换算")
        return None, None, reasons
    cond = conditions_text or ""
    if re.search(r"满\s*\d+\s*件", cond):
        reasons.append(
            "满N件类优惠：文案门槛不等于已证购买数量，不得直接换算可比箱价"
        )
        return None, None, reasons
    if text_implies_qty_dependent_deal(conditions_text, minimum_quantity):
        if minimum_quantity is None or minimum_quantity < 1:
            reasons.append(
                "数量依赖优惠（多件价等）但购买数量未知，不得直接换算可比箱价"
            )
            return None, None, reasons
        qty = minimum_quantity
        per = (amount / Decimal(qty)).quantize(Decimal("0.01"))
        basis = (
            f"明确购买数量={qty}；金额{amount}÷{qty}={per}；"
            f"假设每件对应业务箱规({packs_per_box}包/箱)且规格已匹配"
        )
        return per, basis, reasons
    # Single-piece / non-qty-dependent display: purchase_qty UNKNOWN is allowed
    # when packaging+spec already prove the selected unit is one business box.
    return (
        amount,
        "当前已选箱规的单件售价（购买数量未知可保留；不以默认1冒充证据）",
        reasons,
    )


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

    if ptype in {PRICE_TYPE_UNVERIFIED_PANEL, PRICE_TYPE_CONDITIONAL_CANDIDATE}:
        out["normalized_price"] = None
        out["price_delta"] = None
        out["system_verdict"] = SYSTEM_REVIEW_REQUIRED
        if ptype == PRICE_TYPE_CONDITIONAL_CANDIDATE:
            reasons.append("条件价标签与金额关联不唯一，待核验，不参与破价判定")
        else:
            reasons.append("面板未证实金额；不计入主售价/到手价/最低价统计")
        if amount is not None and amount < red_line:
            out["low_price_hint"] = True
            reasons.append("金额数值低于红线，但角色未证实，不得作为可比到手价")
        out["review_reasons"] = list(dict.fromkeys(reasons))
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

    if out.get("association_confidence") == "PANEL_LABEL_NON_UNIQUE":
        out["normalized_price"] = None
        out["price_delta"] = None
        out["system_verdict"] = SYSTEM_REVIEW_REQUIRED
        reasons.append("条件标签关联不唯一，不做可比箱价换算")
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
        conditions_text=str(out.get("conditions") or out.get("raw_label") or ""),
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
    panel_contexts: list[dict[str, Any]] | None = None,
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
                "association_confidence": "OCR_BLOCK",
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

    # Same-image OCR: label and amount often split into separate blocks.
    # Only promote when exactly one currency amount + one take-home label in that image.
    image_texts = ext.get("ocr_image_texts") or {}
    if isinstance(image_texts, dict) and image_texts:
        by_image: dict[str, list[dict[str, Any]]] = {}
        for c in clues:
            src = str(c.get("evidence_source") or "")
            if not src.startswith("ocr:"):
                continue
            # ocr:price.png|original
            img = src.split(":", 1)[-1].split("|", 1)[0]
            by_image.setdefault(img, []).append(c)
        for img, img_clues in by_image.items():
            texts = image_texts.get(img) or image_texts.get(img.replace(".png", "")) or []
            label_hits = list(
                dict.fromkeys(
                    t
                    for t in texts
                    if any(
                        h in t
                        for h in (
                            "首单到手价",
                            "首单到手",
                            "到手价",
                            "券后价",
                            "会员价",
                            "新人价",
                        )
                    )
                    and not re.search(r"[¥￥]\s*\d", t)
                )
            )
            currency_clues = [
                c
                for c in img_clues
                if c.get("price_type") == PRICE_TYPE_PAGE_PRIMARY and c.get("amount")
            ]
            distinct_amounts = {str(c.get("amount")) for c in currency_clues}
            if len(label_hits) == 1 and len(distinct_amounts) == 1 and currency_clues:
                # Keep PAGE_PRIMARY intact; add a separate declared clue (no rename).
                base = currency_clues[0]
                declared = dict(base)
                declared["price_type"] = PRICE_TYPE_DECLARED_CONDITIONAL
                declared["raw_label"] = label_hits[0]
                declared["association_confidence"] = "OCR_CROSS_BLOCK_UNIQUE"
                declared["evidence_kind"] = "OCR_IMAGE_CROSS_BLOCK"
                declared["conditions"] = (
                    f"ocr_image={img}; amount={base.get('amount')}; "
                    f"label_block={label_hits[0]}; assoc=OCR_CROSS_BLOCK_UNIQUE"
                )[:240]
                declared["calculation_basis"] = (
                    "同图 OCR 金额块与条件标签块唯一配对（非 DOM 位置硬编码；"
                    "主展示价线索仍单独保留）"
                )
                declared["eligibility_status"] = eligibility_from_text(label_hits[0])
                key = (
                    declared.get("price_type"),
                    str(declared.get("amount")),
                    str(declared.get("raw_label")),
                    str(declared.get("association_confidence")),
                )
                if key not in seen:
                    seen.add(key)
                    clues.append(
                        evaluate_clue_against_red_line(
                            declared,
                            red_line=red_line,
                            box_comparable=box_comparable,
                            page_spec_matched=page_spec_matched,
                            packs_per_box=packs_per_box,
                        )
                    )

    for ctx in panel_contexts or []:
        if not isinstance(ctx, dict):
            continue
        panel_text = str(ctx.get("text") or "").strip()
        if not panel_text:
            continue
        src = str(ctx.get("source") or "capture:panel")
        for clue in parse_panel_price_clues(
            panel_text,
            evidence_source=src,
            evidence_path=evidence_path,
            evidence_meta={
                "evidence_kind": ctx.get("evidence_kind") or "DOM_PANEL_TEXT",
                "product_id": ctx.get("product_id"),
                "captured_at_utc": ctx.get("captured_at_utc"),
                "selected_spec_text": ctx.get("selected_spec_text"),
                "purchase_quantity": ctx.get("purchase_quantity"),
            },
        ):
            key = (
                clue.get("price_type"),
                str(clue.get("amount")),
                str(clue.get("raw_label")),
                str(clue.get("association_confidence")),
            )
            if key in seen:
                continue
            seen.add(key)
            # Never let a DOM panel unverified amount collide into OCR primary stats.
            clue = evaluate_clue_against_red_line(
                clue,
                red_line=red_line,
                box_comparable=box_comparable,
                page_spec_matched=page_spec_matched,
                packs_per_box=packs_per_box,
            )
            clues.append(clue)

    return clues


def empty_low_price_signal() -> dict[str, Any]:
    return {
        "low_price_signal": False,
        "signal_strength": None,
        "signal_price_type": None,
        "signal_amount": None,
        "signal_normalized_price": None,
        "signal_conditions": None,
        "signal_review_reasons": [],
    }


def derive_product_low_price_signal(
    *,
    primary_display_price: Any,
    red_line: Any,
    packaging_match: str | None = None,
    box_comparable: bool | None = None,
    match_status: str | None = None,
    task_status: str | None = None,
    system_verdict: str | None = None,
    extra_gaps: list[str] | None = None,
) -> dict[str, Any]:
    """
    Independent of take-home POSSIBLE_BREAK.

    Comparable primary below red → keep low-price signal with message
    「主展示价低于红线，到手价待核验」; never rename primary to take-home.
    Match/unit insufficient → weak clue only; no forged normalized price.
    """
    out = empty_low_price_signal()
    if task_status in {"SKIPPED", "NO_CANDIDATES"} or match_status in {
        "MODEL_MISMATCH_SKIPPED",
        "PAUSED_BEFORE_VISIT",
    }:
        return out
    if system_verdict == "MODEL_MISMATCH_SKIPPED":
        return out

    primary = to_decimal(primary_display_price)
    red = to_decimal(red_line)
    if primary is None or red is None:
        return out
    if primary >= red:
        return out

    gaps = [g for g in (extra_gaps or []) if g]
    packaging_ok = str(packaging_match or "").upper() == "PASSED"
    box_ok = bool(box_comparable)
    comparable = packaging_ok and box_ok

    out["low_price_signal"] = True
    out["signal_price_type"] = PRICE_TYPE_PAGE_PRIMARY
    out["signal_amount"] = format(primary, "f")

    if comparable:
        out["signal_strength"] = SIGNAL_STRENGTH_COMPARABLE_PRIMARY
        out["signal_normalized_price"] = format(primary, "f")
        out["signal_conditions"] = "主展示价低于红线，到手价待核验"
        reasons = [
            "可靠主展示价在型号/规格/单位可比时低于红线",
            "主展示价低于红线，到手价待核验",
            "不得将主展示价改名为到手价；system_verdict 仍按到手价层判定",
        ]
        out["signal_review_reasons"] = list(dict.fromkeys(reasons + gaps))
        return out

    out["signal_strength"] = SIGNAL_STRENGTH_WEAK_AMOUNT
    out["signal_normalized_price"] = None
    out["signal_conditions"] = "金额低于红线数值、口径待核验"
    reasons = [
        "观察金额低于红线数值，但型号/规格/单位可比证据不足",
        "不生成无依据的归一化箱价或有效破价结论",
        "到手价待核验",
    ]
    if not packaging_ok:
        reasons.append(f"packaging_match={packaging_match or '缺失'}")
    if not box_ok:
        reasons.append("box_comparable=false")
    out["signal_review_reasons"] = list(dict.fromkeys(reasons + gaps))
    return out


def apply_low_price_signal_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Mutate/return product row with low_price_signal fields; keep manual_verdict empty."""
    signal = derive_product_low_price_signal(
        primary_display_price=row.get("primary_display_price"),
        red_line=row.get("red_line"),
        packaging_match=row.get("packaging_match"),
        box_comparable=row.get("box_comparable"),
        match_status=row.get("match_status"),
        task_status=row.get("task_status"),
        system_verdict=row.get("system_verdict"),
    )
    reasons = signal.get("signal_review_reasons") or []
    if isinstance(reasons, list):
        signal["signal_review_reasons"] = "; ".join(str(r) for r in reasons if r)
    row.update(signal)
    row["manual_verdict"] = None
    return row


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
        and c.get("association_confidence") != "PANEL_LABEL_NON_UNIQUE"
    ]
    pending_assoc = [
        c
        for c in clues
        if c.get("price_type") == PRICE_TYPE_CONDITIONAL_CANDIDATE
        or c.get("association_confidence") == "PANEL_LABEL_NON_UNIQUE"
    ]
    if has_possible:
        verdict = SYSTEM_POSSIBLE_BREAK
        zh = SYSTEM_VERDICT_ZH[SYSTEM_POSSIBLE_BREAK]
    elif take_homes and all(
        c.get("system_verdict") == SYSTEM_NO_BREAK_IN_SCOPE for c in take_homes
    ):
        verdict = SYSTEM_NO_BREAK_IN_SCOPE
        zh = "本次已识别的可比价格未低于红线"
    else:
        verdict = SYSTEM_REVIEW_REQUIRED
        zh = "到手价待核验"
    note_bits = ["多方案分别保留；未发现低价≠全部优惠未破价"]
    if pending_assoc:
        note_bits.append("存在条件标签关联不唯一的金额候选，待人工核验")
    return {
        "system_verdict": verdict,
        "system_verdict_zh": zh,
        "manual_verdict": None,
        "has_possible_break": has_possible,
        "take_home_clue_count": len(take_homes),
        "conditional_candidate_count": len(pending_assoc),
        "primary_ref_count": sum(
            1 for c in clues if c.get("price_type") == PRICE_TYPE_PAGE_PRIMARY
        ),
        "unverified_panel_count": sum(
            1 for c in clues if c.get("price_type") == PRICE_TYPE_UNVERIFIED_PANEL
        ),
        "note": "；".join(note_bits),
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

    # --- Product-level low_price_signal (separate from take-home POSSIBLE_BREAK) ---
    comparable_signal = derive_product_low_price_signal(
        primary_display_price="120.00",
        red_line=red,
        packaging_match="PASSED",
        box_comparable=True,
    )
    if not comparable_signal.get("low_price_signal"):
        failures.append("comparable primary below red must set low_price_signal")
    if comparable_signal.get("signal_normalized_price") != "120.00":
        failures.append("comparable primary may carry signal_normalized_price as box ref")
    if "主展示价低于红线" not in str(comparable_signal.get("signal_conditions") or ""):
        failures.append("comparable signal must state 主展示价低于红线，到手价待核验")

    weak_signal = derive_product_low_price_signal(
        primary_display_price="125.00",
        red_line=red,
        packaging_match="FAILED",
        box_comparable=False,
    )
    if not weak_signal.get("low_price_signal"):
        failures.append("weak amount-below-red must keep low_price_signal")
    if weak_signal.get("signal_normalized_price") is not None:
        failures.append("weak signal must not forge normalized price")
    if "口径待核验" not in str(weak_signal.get("signal_conditions") or ""):
        failures.append("weak signal conditions should note 口径待核验")

    above = derive_product_low_price_signal(
        primary_display_price="168.00",
        red_line=red,
        packaging_match="PASSED",
        box_comparable=True,
    )
    if above.get("low_price_signal"):
        failures.append("primary above red must not set low_price_signal")

    skipped = derive_product_low_price_signal(
        primary_display_price="100.00",
        red_line=red,
        match_status="MODEL_MISMATCH_SKIPPED",
        task_status="SKIPPED",
    )
    if skipped.get("low_price_signal"):
        failures.append("model mismatch must not emit low_price_signal")

    # Price-type separation: primary REVIEW vs declared POSSIBLE_BREAK
    if primary["system_verdict"] == SYSTEM_POSSIBLE_BREAK:
        failures.append("primary must never be POSSIBLE_BREAK")
    if member["price_type"] == PRICE_TYPE_PAGE_PRIMARY:
        failures.append("declared member must not be classified as primary")

    row_probe = apply_low_price_signal_fields(
        {
            "primary_display_price": "125.00",
            "red_line": "127.00",
            "packaging_match": "FAILED",
            "box_comparable": False,
            "task_status": "COMPLETED",
            "match_status": "PACKAGING_FAILED_OR_UNCLEAR",
            "system_verdict": SYSTEM_REVIEW_REQUIRED,
            "manual_verdict": "should_be_cleared",
        }
    )
    if row_probe.get("manual_verdict") is not None:
        failures.append("apply_low_price_signal_fields must clear manual_verdict")
    if not row_probe.get("low_price_signal"):
        failures.append("apply_low_price_signal_fields should set signal on weak row")

    panel = "¥163 首单到手价 ¥199 累计评价 500+ 降价通知"
    panel_clues = parse_panel_price_clues(
        panel, evidence_source="test:panel", evidence_path="/tmp"
    )
    if any(c.get("price_type") == PRICE_TYPE_PAGE_PRIMARY for c in panel_clues):
        failures.append("panel secondary amounts must not be PAGE_PRIMARY_SELLING")
    if any(c.get("amount") == "199" and c.get("price_type") == PRICE_TYPE_PAGE_PRIMARY for c in panel_clues):
        failures.append("199 must not be PAGE_PRIMARY_SELLING")
    non_unique = [
        c
        for c in panel_clues
        if c.get("association_confidence") == "PANEL_LABEL_NON_UNIQUE"
    ]
    if len(non_unique) < 2:
        failures.append("¥A 首单到手价 ¥B must mark both amounts non-unique pending review")
    if any(c.get("price_type") == PRICE_TYPE_DECLARED_CONDITIONAL for c in panel_clues):
        failures.append("non-unique panel must not force PAGE_DECLARED_CONDITIONAL")
    unique_panel = parse_panel_price_clues(
        "首单到手价¥118", evidence_source="test:unique", evidence_path="/tmp"
    )
    if not any(
        c.get("price_type") == PRICE_TYPE_DECLARED_CONDITIONAL
        and c.get("amount") == "118"
        and c.get("association_confidence") == "PANEL_LABEL_UNIQUE"
        for c in unique_panel
    ):
        failures.append("unique label+amount panel must DECLARED with PANEL_LABEL_UNIQUE")

    built = build_price_clues_from_extraction(
        {
            "price_candidates": [
                {
                    "amount": "163",
                    "role": "currency_tagged_price_candidate",
                    "source_text": "¥163",
                    "source_image": "price.png",
                    "variant": "original",
                }
            ]
        },
        red_line=red,
        box_comparable=True,
        page_spec_matched=True,
        packs_per_box=20,
        panel_contexts=[
            {
                "text": panel,
                "source": "capture:region_discover.price",
                "evidence_kind": "DOM_PANEL_TEXT",
            }
        ],
    )
    if not any(c.get("price_type") == PRICE_TYPE_PAGE_PRIMARY and c.get("amount") == "163" for c in built):
        failures.append("OCR primary 163 must remain PAGE_PRIMARY_SELLING")
    if not any(
        c.get("evidence_kind") == "DOM_PANEL_TEXT"
        or (c.get("evidence_source") or "").startswith("capture:")
        for c in built
        if c.get("amount") == "199"
    ):
        failures.append("199 must keep DOM evidence source, not OCR")
    roll = aggregate_system_verdict(built)
    if roll.get("take_home_clue_count", 0) != 0:
        failures.append("non-unique panel candidates must not count as take_home_clue")
    if int(roll.get("conditional_candidate_count") or 0) < 1:
        failures.append("rollup should count conditional association candidates")

    # Qty-dependent without qty → no box normalize
    qty_unknown_deal = evaluate_clue_against_red_line(
        {
            **empty_clue_template(),
            "price_type": PRICE_TYPE_DECLARED_CONDITIONAL,
            "amount": "200.00",
            "raw_label": "满2件优惠",
            "conditions": "满2件享优惠 ¥200",
            "minimum_quantity": None,
            "eligibility_status": "UNKNOWN",
        },
        red_line=red,
        box_comparable=True,
        page_spec_matched=True,
        packs_per_box=20,
    )
    if qty_unknown_deal.get("normalized_price") is not None:
        failures.append("qty-dependent deal without qty must not normalize")
    if qty_unknown_deal["system_verdict"] != SYSTEM_REVIEW_REQUIRED:
        failures.append("qty-dependent unknown must REVIEW_REQUIRED")

    # Unit vs total ambiguous
    total_amb = evaluate_clue_against_red_line(
        {
            **empty_clue_template(),
            "price_type": PRICE_TYPE_DECLARED_CONDITIONAL,
            "amount": "300.00",
            "raw_label": "组合总价",
            "conditions": "组合总价¥300",
            "minimum_quantity": None,
        },
        red_line=red,
        box_comparable=True,
        page_spec_matched=True,
        packs_per_box=20,
    )
    if total_amb.get("normalized_price") is not None:
        failures.append("ambiguous unit/total must not normalize")
    if total_amb["system_verdict"] != SYSTEM_REVIEW_REQUIRED:
        failures.append("ambiguous unit/total must REVIEW_REQUIRED")

    # Single-piece declared, purchase qty unknown, packaging matched → may normalize
    single_qty_unknown = evaluate_clue_against_red_line(
        {
            **empty_clue_template(),
            "price_type": PRICE_TYPE_DECLARED_CONDITIONAL,
            "amount": "163.00",
            "raw_label": "首单到手价",
            "conditions": "首单到手价¥163",
            "minimum_quantity": None,
            "eligibility_status": "CONDITIONAL_UNVERIFIED",
            "association_confidence": "PANEL_LABEL_UNIQUE",
        },
        red_line=red,
        box_comparable=True,
        page_spec_matched=True,
        packs_per_box=20,
    )
    if single_qty_unknown.get("normalized_price") != "163.00":
        failures.append("single-piece declared with unknown purchase qty may still box-normalize")
    if single_qty_unknown["system_verdict"] != SYSTEM_NO_BREAK_IN_SCOPE:
        failures.append("163 vs 127 unique declared should NO_BREAK_IN_SCOPE")

    # Explicit multi still works (separate from 163 fixture)
    multi2 = evaluate_clue_against_red_line(
        {
            **empty_clue_template(),
            "price_type": PRICE_TYPE_RULE_CALCULATED,
            "amount": "240.00",
            "raw_label": "2件价",
            "conditions": "2件240元",
            "minimum_quantity": 2,
        },
        red_line=red,
        box_comparable=True,
        page_spec_matched=True,
        packs_per_box=20,
    )
    if multi2.get("normalized_price") != "120.00":
        failures.append("explicit 2件240 must still normalize to 120")

    cross = build_price_clues_from_extraction(
        {
            "price_candidates": [
                {
                    "amount": "163",
                    "role": "currency_tagged_price_candidate",
                    "source_text": "¥163",
                    "source_image": "price.png",
                    "variant": "original",
                }
            ],
            "ocr_image_texts": {
                "price.png": ["¥163", "首单到手价", "累计评价 500+"],
            },
        },
        red_line=red,
        box_comparable=True,
        page_spec_matched=True,
        packs_per_box=20,
    )
    if not any(
        c.get("price_type") == PRICE_TYPE_DECLARED_CONDITIONAL
        and c.get("association_confidence") == "OCR_CROSS_BLOCK_UNIQUE"
        and c.get("amount") == "163"
        for c in cross
    ):
        failures.append("OCR cross-block unique label+amount must DECLARED")

    return failures
