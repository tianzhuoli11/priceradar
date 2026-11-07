"""
Shared product-model matching for search titles and OCR gates.

Rules (model-match-v1):
- Compare complete normalized model tokens — never treat expected as a substring
  of a longer code (B913A ⊄ B913AC / B913AE / B913A1).
- Do not truncate longer OCR tokens down to expected.
- Compatibility / negation phrases are not product-model hits.
- Ambiguous joins → AMBIGUOUS / MULTI, never guess.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

RULES_VERSION = "model-match-v1"

# Letter + digits mixed alphanumeric SKU-like codes (B913A, B913AC, AFH123…).
# Pure digit runs (sheet counts, JD ids) are intentionally excluded.
MODEL_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]*\d[A-Za-z0-9]*)(?![A-Za-z0-9])"
)

# Look behind a small window for non-product usage.
COMPAT_BEFORE_RE = re.compile(
    r"(适用|兼容|适配|配用|用于|可用於|可用于|替用).{0,4}$"
)
NEGATION_BEFORE_RE = re.compile(
    r"(非|不是|并非|并非是|不适用于|不适合|除外).{0,4}$"
)

# Joiner between two model-like codes → multi/ambiguous.
JOIN_MULTI_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9]*\d[A-Za-z0-9]*)\s*[/／|｜、,，与和或及]\s*"
    r"([A-Za-z][A-Za-z0-9]*\d[A-Za-z0-9]*)",
    re.IGNORECASE,
)

CLASS_TARGET = "TARGET_MODEL_CANDIDATE"
CLASS_OTHER = "OTHER_MODEL"
CLASS_MULTI = "MULTI_MODEL_OR_UNKNOWN"


def normalize_model_text(text: str) -> str:
    """NFKC (fullwidth→halfwidth) + casefold. Does not delete internal chars."""
    return unicodedata.normalize("NFKC", text or "").casefold()


def normalize_model_token(token: str) -> str:
    return normalize_model_text(token).strip()


def model_boundary_regex(model: str) -> re.Pattern[str]:
    """Whole-token match after caller normalizes haystack if needed."""
    m = (model or "").strip()
    if not m:
        raise ValueError("model 不能为空")
    esc = re.escape(normalize_model_token(m))
    return re.compile(rf"(?<![A-Za-z0-9]){esc}(?![A-Za-z0-9])", re.IGNORECASE)


def _context_kind(norm_title: str, start: int) -> str:
    before = norm_title[max(0, start - 12) : start]
    # Strip spaces for window checks
    before_compact = re.sub(r"\s+", "", before)
    if NEGATION_BEFORE_RE.search(before_compact):
        return "negation"
    if COMPAT_BEFORE_RE.search(before_compact):
        return "compatibility"
    return "product"


def _looks_like_product_model(token_norm: str) -> bool:
    """
    Reject pack counts / tiny codes (x1, x2) and pure short letter+digit noise.
    Keep real SKUs like B913A / B913AC (letter + ≥2 digits, length≥4).
    """
    t = (token_norm or "").strip()
    if len(t) < 4:
        return False
    digits = sum(ch.isdigit() for ch in t)
    letters = sum(ch.isalpha() for ch in t)
    if digits < 2 or letters < 1:
        return False
    # e.g. x12 still too weak without enough body
    if letters == 1 and digits <= 2 and len(t) <= 4:
        return False
    return True


def extract_model_tokens(title: str) -> list[dict[str, Any]]:
    """
    Extract SKU-like model tokens from a title.

    Each item: raw (from NFKC text), normalized, start/end in normalized title,
    context_kind: product | compatibility | negation.
    """
    raw_title = title or ""
    norm = normalize_model_text(raw_title)
    out: list[dict[str, Any]] = []
    seen_spans: set[tuple[int, int]] = set()
    for m in MODEL_TOKEN_RE.finditer(norm):
        span = (m.start(1), m.end(1))
        if span in seen_spans:
            continue
        seen_spans.add(span)
        token_raw = m.group(1)
        token_norm = normalize_model_token(token_raw)
        if not _looks_like_product_model(token_norm):
            continue
        kind = _context_kind(norm, m.start(1))
        out.append(
            {
                "raw": token_raw,
                "normalized": token_norm,
                "start": m.start(1),
                "end": m.end(1),
                "context_kind": kind,
            }
        )
    return out


def detect_multi_join(title: str) -> list[dict[str, str]]:
    norm = normalize_model_text(title or "")
    joins = []
    for m in JOIN_MULTI_RE.finditer(norm):
        a, b = m.group(1), m.group(2)
        if normalize_model_token(a) != normalize_model_token(b):
            joins.append(
                {
                    "left_raw": a,
                    "right_raw": b,
                    "left_normalized": normalize_model_token(a),
                    "right_normalized": normalize_model_token(b),
                }
            )
    return joins


def classify_title_models(
    title: str,
    *,
    expected_model: str,
) -> dict[str, Any]:
    """
    Classify a listing title against business expected_model.

    Returns classification in {TARGET_MODEL_CANDIDATE, OTHER_MODEL, MULTI_MODEL_OR_UNKNOWN}.
    detail_verification_required is always True for TARGET (title ≠ detail confirmed).
    """
    expected = (expected_model or "").strip()
    expected_norm = normalize_model_token(expected) if expected else ""
    tokens = extract_model_tokens(title or "")
    joins = detect_multi_join(title or "")

    observed = [
        {
            "raw": t["raw"],
            "normalized": t["normalized"],
            "context_kind": t["context_kind"],
        }
        for t in tokens
    ]

    base = {
        "rules_version": RULES_VERSION,
        "expected_model": expected or None,
        "expected_model_normalized": expected_norm or None,
        "observed_model_tokens": observed,
        "multi_joins": joins,
        "title_normalized_preview": normalize_model_text(title or "")[:200],
    }

    if not expected_norm:
        return {
            **base,
            "classification": CLASS_MULTI,
            "reason": "business_expected_model_missing",
            "detail_verification_required": True,
            "red_line_applicable": False,
            "gate_status": "REVIEW_REQUIRED",
            "skip_browse": False,
        }

    if not (title or "").strip():
        return {
            **base,
            "classification": CLASS_MULTI,
            "reason": "empty_title",
            "detail_verification_required": True,
            "red_line_applicable": False,
            "gate_status": "TITLE_MODEL_UNCLEAR",
            "skip_browse": False,
        }

    if joins:
        return {
            **base,
            "classification": CLASS_MULTI,
            "reason": "multi_model_join_ambiguous",
            "detail_verification_required": True,
            "red_line_applicable": False,
            "gate_status": "TITLE_MODEL_UNCLEAR",
            "skip_browse": False,
        }

    product_tokens = [t for t in tokens if t["context_kind"] == "product"]
    compat_tokens = [t for t in tokens if t["context_kind"] == "compatibility"]
    neg_tokens = [t for t in tokens if t["context_kind"] == "negation"]

    # Unreliable segmentation: very long alphanumeric blob
    if any(len(t["normalized"]) > 24 for t in tokens):
        return {
            **base,
            "classification": CLASS_MULTI,
            "reason": "token_too_long_unreliable_split",
            "detail_verification_required": True,
            "red_line_applicable": False,
            "gate_status": "TITLE_MODEL_UNCLEAR",
            "skip_browse": False,
        }

    target_product = [t for t in product_tokens if t["normalized"] == expected_norm]
    other_product = [t for t in product_tokens if t["normalized"] != expected_norm]

    # Expected only appears under compat/negation — not a sellable-model hit
    target_compat = [t for t in compat_tokens if t["normalized"] == expected_norm]
    target_neg = [t for t in neg_tokens if t["normalized"] == expected_norm]

    if target_product and other_product:
        return {
            **base,
            "classification": CLASS_MULTI,
            "reason": "target_and_other_product_models",
            "detail_verification_required": True,
            "red_line_applicable": False,
            "gate_status": "TITLE_MODEL_UNCLEAR",
            "skip_browse": False,
        }

    if target_product and not other_product:
        # Clean target candidate
        return {
            **base,
            "classification": CLASS_TARGET,
            "reason": "standalone_expected_model_token",
            "detail_verification_required": True,
            "red_line_applicable": False,  # only after detail verification
            "gate_status": "TITLE_MODEL_OK",
            "skip_browse": False,
            "note": "标题候选线索，不能覆盖详情型号核验；红线仅详情通过后可比",
        }

    if other_product and not target_product:
        others = sorted({t["normalized"] for t in other_product})
        # Prefix-conflict note for reporting (expected is proper prefix of other)
        prefix_conflicts = [
            o for o in others if o.startswith(expected_norm) and o != expected_norm
        ]
        return {
            **base,
            "classification": CLASS_OTHER,
            "reason": (
                "other_model_prefix_extension"
                if prefix_conflicts
                else "other_model_only"
            ),
            "other_models_normalized": others,
            "prefix_conflict_models": prefix_conflicts,
            "detail_verification_required": False,
            "red_line_applicable": False,
            "gate_status": "MODEL_MISMATCH_SKIPPED",
            "skip_browse": True,
            "conflict_token": prefix_conflicts[0] if prefix_conflicts else others[0],
            "note": "明确其他型号，不适用目标红线",
        }

    # No product-model tokens
    if target_compat or target_neg:
        return {
            **base,
            "classification": CLASS_MULTI,
            "reason": "expected_only_in_compat_or_negation",
            "detail_verification_required": True,
            "red_line_applicable": False,
            "gate_status": "TITLE_MODEL_UNCLEAR",
            "skip_browse": False,
        }

    if compat_tokens or neg_tokens:
        return {
            **base,
            "classification": CLASS_MULTI,
            "reason": "only_non_product_model_mentions",
            "detail_verification_required": True,
            "red_line_applicable": False,
            "gate_status": "TITLE_MODEL_UNCLEAR",
            "skip_browse": False,
        }

    return {
        **base,
        "classification": CLASS_MULTI,
        "reason": "no_model_token_found",
        "detail_verification_required": True,
        "red_line_applicable": False,
        "gate_status": "TITLE_MODEL_UNCLEAR",
        "skip_browse": False,
    }


def title_contains_exact_model(title: str, model: str) -> bool:
    """True iff a product-context complete token equals model (not compat/neg)."""
    try:
        result = classify_title_models(title, expected_model=model)
    except Exception:
        return False
    return result.get("classification") == CLASS_TARGET


def title_model_gate(title: str, *, expected_model: str) -> dict[str, Any]:
    """Drop-in gate used by search / browse screening."""
    result = classify_title_models(title, expected_model=expected_model)
    status = result.get("gate_status") or "TITLE_MODEL_UNCLEAR"
    out = {
        "status": status,
        "reason": result.get("reason"),
        "skip_browse": bool(result.get("skip_browse")),
        "classification": result.get("classification"),
        "rules_version": RULES_VERSION,
        "observed_model_tokens": result.get("observed_model_tokens"),
        "detail_verification_required": result.get("detail_verification_required"),
        "red_line_applicable": result.get("red_line_applicable"),
    }
    if result.get("conflict_token"):
        out["conflict_token"] = result["conflict_token"]
    return out


def ocr_model_match_blocks(
    texts: list[str],
    *,
    expected_model: str,
) -> dict[str, Any]:
    """
    OCR helper: find complete expected tokens across blocks.
    Never truncates a longer token (B913AC) into expected (B913A).
    """
    expected_norm = normalize_model_token(expected_model)
    found_expected = []
    found_other = []
    ambiguous = []
    for text in texts:
        cls = classify_title_models(text, expected_model=expected_model)
        for tok in cls.get("observed_model_tokens") or []:
            if tok["normalized"] == expected_norm and tok["context_kind"] == "product":
                found_expected.append(tok)
            elif tok["context_kind"] == "product":
                found_other.append(tok)
        if cls.get("classification") == CLASS_MULTI and cls.get("reason") not in {
            "no_model_token_found",
            "empty_title",
        }:
            ambiguous.append({"text": text[:120], "reason": cls.get("reason")})

    if found_other and not found_expected:
        return {
            "status": "MODEL_MISMATCH",
            "matched_expected": False,
            "other_tokens": found_other,
            "rules_version": RULES_VERSION,
            "note": "OCR longer/other model must not be truncated to expected",
        }
    if found_expected and not found_other and not ambiguous:
        return {
            "status": "MODEL_OK_PENDING_DETAIL",
            "matched_expected": True,
            "expected_tokens": found_expected,
            "rules_version": RULES_VERSION,
        }
    if found_expected and found_other:
        return {
            "status": "MODEL_AMBIGUOUS",
            "matched_expected": False,
            "expected_tokens": found_expected,
            "other_tokens": found_other,
            "rules_version": RULES_VERSION,
        }
    if ambiguous:
        return {
            "status": "MODEL_AMBIGUOUS",
            "matched_expected": False,
            "ambiguous": ambiguous,
            "rules_version": RULES_VERSION,
        }
    return {
        "status": "MODEL_NOT_FOUND",
        "matched_expected": False,
        "rules_version": RULES_VERSION,
        "note": "keep pending review; do not invent expected from partial OCR",
    }


def run_offline_model_match_checks() -> list[str]:
    failures: list[str] = []
    exp = "B913A"

    def expect(title: str, classification: str, *, exp_model: str = exp) -> None:
        r = classify_title_models(title, expected_model=exp_model)
        if r["classification"] != classification:
            failures.append(
                f"{title!r} expected {classification} got {r['classification']} "
                f"reason={r.get('reason')}"
            )

    expect("B913A", CLASS_TARGET)
    expect("清风B913A擦手纸", CLASS_TARGET)
    expect("（B913A）", CLASS_TARGET)
    expect("b913a", CLASS_TARGET)
    expect("清风Ｂ９１３Ａ擦手纸", CLASS_TARGET)  # fullwidth

    expect("清风B913AC三折", CLASS_OTHER)
    expect("B913AE 16包", CLASS_OTHER)
    expect("B913A1测试", CLASS_OTHER)

    expect("B913A/B913AC", CLASS_MULTI)
    expect("B913A与B913AE", CLASS_MULTI)

    expect("非B913A", CLASS_MULTI)
    expect("适用B913A机型", CLASS_MULTI)

    expect("清风擦手纸商用200抽20包", CLASS_MULTI)  # no model
    # Pack multiplier must not become a second model
    expect("清风擦手纸B913A 1层200抽x2包实惠装", CLASS_TARGET)
    expect("清风擦手纸B913A 1层200抽x1包试用装", CLASS_TARGET)

    # Other business target can match independently
    r_ac = classify_title_models("清风B913AC三折", expected_model="B913AC")
    if r_ac["classification"] != CLASS_TARGET:
        failures.append(f"B913AC as expected should TARGET, got {r_ac}")

    # Comparable gate: OTHER / MULTI cannot be red-line comparable
    for title in ("B913AC", "B913A/B913AC", "适用B913A", ""):
        r = classify_title_models(title, expected_model=exp)
        if r.get("red_line_applicable"):
            failures.append(f"red_line must stay false before detail: {title!r}")

    target = classify_title_models("清风B913A", expected_model=exp)
    if target.get("red_line_applicable"):
        failures.append("TARGET title still must not enable red_line pre-detail")
    if not target.get("detail_verification_required"):
        failures.append("TARGET must require detail verification")

    # Gate mapping
    g = title_model_gate("B913AE", expected_model=exp)
    if g["status"] != "MODEL_MISMATCH_SKIPPED" or not g["skip_browse"]:
        failures.append(f"gate OTHER failed: {g}")

    g2 = title_model_gate("清风B913A", expected_model=exp)
    if g2["status"] != "TITLE_MODEL_OK":
        failures.append(f"gate TARGET failed: {g2}")

    # OCR must not truncate
    ocr = ocr_model_match_blocks(["包装型号 B913AC"], expected_model=exp)
    if ocr.get("matched_expected") or ocr.get("status") != "MODEL_MISMATCH":
        failures.append(f"OCR must not accept B913AC as B913A: {ocr}")

    ocr2 = ocr_model_match_blocks(["型号 B913A"], expected_model=exp)
    if not ocr2.get("matched_expected"):
        failures.append(f"OCR should pending-ok on complete B913A: {ocr2}")

    if title_contains_exact_model("批发B913AE", exp):
        failures.append("B913A must not match inside B913AE")
    if title_contains_exact_model("B913AC", exp):
        failures.append("B913A must not match B913AC")

    return failures


if __name__ == "__main__":
    fails = run_offline_model_match_checks()
    if fails:
        print("FAILED")
        for f in fails:
            print(" -", f)
        raise SystemExit(1)
    print("OK", RULES_VERSION)
