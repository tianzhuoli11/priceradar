"""
Current selected-spec evidence vs available options.

Rules:
- Multiple pack counts in a panel ≠ current selection.
- Missing selected-state → SELECTED_SPEC_UNKNOWN (not OBSERVED_SPEC_MISMATCH).
- Only an explicit selected option that conflicts with target → OBSERVED_SPEC_MISMATCH.
"""

from __future__ import annotations

import re
from typing import Any

RULES_VERSION_SELECTED_SPEC = "selected-spec-v1"

SELECTED_EXTRACTED = "SELECTED_SPEC_EXTRACTED"
SELECTED_UNKNOWN = "SELECTED_SPEC_UNKNOWN"
OBSERVED_MISMATCH = "OBSERVED_SPEC_MISMATCH"
PACKAGING_PASSED = "PASSED"

PACK_RE = re.compile(r"(\d+)\s*包")
LAYER_RE = re.compile(r"(\d+)\s*层")
SHEET_RE = re.compile(r"(\d+)\s*(?:抽|张)")


def parse_spec_tokens(text: str | None) -> dict[str, Any]:
    t = (text or "").strip()
    out: dict[str, Any] = {
        "raw": t or None,
        "packs": None,
        "layers": None,
        "sheets_or_pulls": None,
        "has_box_word": "箱" in t if t else False,
    }
    if not t:
        return out
    m = PACK_RE.search(t)
    if m:
        out["packs"] = m.group(1)
    m = LAYER_RE.search(t)
    if m:
        out["layers"] = m.group(1)
    m = SHEET_RE.search(t)
    if m:
        out["sheets_or_pulls"] = m.group(1)
    return out


def selection_snapshot_from_state(snap: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize capture selection_state_* / enriched snapshot."""
    if not isinstance(snap, dict):
        return {
            "selected_status": SELECTED_UNKNOWN,
            "selected_spec_text": None,
            "selected_options": [],
            "available_options": [],
            "selection_evidence": {"reason": "missing_snapshot"},
            "variant_id": None,
            "purchase_quantity": None,
            "purchase_quantity_status": "UNKNOWN",
        }
    status = str(snap.get("selected_status") or "").strip()
    selected_text = snap.get("selected_spec_text")
    selected_options = list(snap.get("selected_options") or [])
    available_options = list(snap.get("available_options") or [])
    if not status:
        if selected_options or selected_text:
            status = SELECTED_EXTRACTED
        else:
            status = SELECTED_UNKNOWN
    if status == SELECTED_EXTRACTED and not selected_text and selected_options:
        selected_text = " | ".join(
            str(o.get("text") or "") for o in selected_options if o.get("text")
        )
    return {
        "selected_status": status,
        "selected_spec_text": selected_text,
        "selected_options": selected_options,
        "available_options": available_options,
        "selection_evidence": snap.get("selection_evidence")
        or {"reason": "legacy_or_partial_snapshot"},
        "variant_id": snap.get("variant_id"),
        "purchase_quantity": snap.get("purchase_quantity"),
        "purchase_quantity_status": snap.get("purchase_quantity_status")
        or ("OBSERVED" if snap.get("purchase_quantity") not in (None, "") else "UNKNOWN"),
        "url_product_id": snap.get("url_product_id"),
        "title_text": snap.get("title_text"),
        "price_value_text": snap.get("price_value_text"),
        "price_panel_preview": snap.get("price_panel_preview"),
    }


def prefer_selection_side(
    selection_state: dict[str, Any] | None,
) -> dict[str, Any]:
    """Prefer after snapshot; fall back to before."""
    if not isinstance(selection_state, dict):
        return selection_snapshot_from_state(None)
    after = selection_snapshot_from_state(selection_state.get("after"))
    before = selection_snapshot_from_state(selection_state.get("before"))
    if after.get("selected_status") == SELECTED_EXTRACTED:
        out = dict(after)
        out["snapshot_side"] = "after"
        return out
    if before.get("selected_status") == SELECTED_EXTRACTED:
        out = dict(before)
        out["snapshot_side"] = "before"
        return out
    # Both unknown — keep after for product_id/qty fields
    out = dict(after)
    out["snapshot_side"] = "after"
    if before.get("available_options") and not out.get("available_options"):
        out["available_options"] = before.get("available_options")
    return out


def selected_spec_candidates_for_unit(
    selection: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Build specification_candidates ONLY from selected options.
    Available (unselected) options are never included.
    """
    meta = {
        "selected_status": selection.get("selected_status"),
        "used_selected_only": True,
        "ignored_available_options": True,
    }
    if selection.get("selected_status") != SELECTED_EXTRACTED:
        meta["reason"] = "selected_state_unknown"
        return [], meta
    texts: list[str] = []
    if selection.get("selected_options"):
        texts = [
            str(o.get("text") or "")
            for o in selection["selected_options"]
            if o.get("text")
        ]
    elif selection.get("selected_spec_text"):
        texts = [str(selection["selected_spec_text"])]
    cands: list[dict[str, Any]] = []
    for t in texts:
        tok = parse_spec_tokens(t)
        cands.append(
            {
                "source": "dom_selected_option",
                "source_text": t,
                "packs": tok["packs"],
                "layers": tok["layers"],
                "sheets_or_pulls": tok["sheets_or_pulls"],
                "unit_box_evidence": (
                    "text_contains_箱" if tok["has_box_word"] else None
                ),
                "selection_scope": "selected_only",
            }
        )
    meta["selected_texts"] = texts
    return cands, meta


def classify_packaging_match(
    *,
    selected_status: str,
    page_spec_matched: bool,
    selected_tokens: dict[str, Any] | None,
    business_packs: str | None,
    business_layers: str | None,
    business_sheets: str | None,
) -> tuple[str, list[str]]:
    """
    Return packaging_match label + reasons.
    UNKNOWN selected → SELECTED_SPEC_UNKNOWN (never OBSERVED_SPEC_MISMATCH).
    """
    reasons: list[str] = []
    if selected_status != SELECTED_EXTRACTED:
        reasons.append("当前选中规格未确认；可选规格文本不得当作已选")
        return SELECTED_UNKNOWN, reasons
    if page_spec_matched:
        return PACKAGING_PASSED, reasons
    tok = selected_tokens or {}
    conflicts: list[str] = []
    if business_packs and tok.get("packs") and str(tok["packs"]) != str(business_packs):
        conflicts.append(f"选中包数{tok['packs']}≠目标{business_packs}")
    if business_layers and tok.get("layers") and str(tok["layers"]) != str(business_layers):
        conflicts.append(f"选中层数{tok['layers']}≠目标{business_layers}")
    if (
        business_sheets
        and tok.get("sheets_or_pulls")
        and str(tok["sheets_or_pulls"]) != str(business_sheets)
    ):
        conflicts.append(
            f"选中张/抽{tok['sheets_or_pulls']}≠目标{business_sheets}"
        )
    if conflicts:
        reasons.extend(conflicts)
        return OBSERVED_MISMATCH, reasons
    reasons.append("已提取选中规格，但未满足可比箱规匹配条件")
    return "FAILED", reasons


def run_selected_spec_offline_checks() -> list[str]:
    fails: list[str] = []
    # 1) 10 available, 20 selected
    sel20 = {
        "selected_status": SELECTED_EXTRACTED,
        "selected_options": [
            {"text": "【230*225mm】200抽*20包整箱", "selected": True}
        ],
        "available_options": [
            {"text": "【230*225mm】200抽*20包整箱", "selected": True},
            {"text": "【230*225mm】200抽*10包整箱", "selected": False},
        ],
    }
    cands, meta = selected_spec_candidates_for_unit(sel20)
    packs = {c.get("packs") for c in cands}
    if packs != {"20"}:
        fails.append("selected-only must keep 20 when 10 is unselected")
    if any(c.get("packs") == "10" for c in cands):
        fails.append("unselected 10 must not enter unit candidates")

    # 2) 20 available, 10 selected
    sel10 = {
        "selected_status": SELECTED_EXTRACTED,
        "selected_options": [
            {"text": "【230*225mm】200抽*10包整箱", "selected": True}
        ],
        "available_options": [
            {"text": "【230*225mm】200抽*20包整箱", "selected": False},
            {"text": "【230*225mm】200抽*10包整箱", "selected": True},
        ],
    }
    cands10, _ = selected_spec_candidates_for_unit(sel10)
    if {c.get("packs") for c in cands10} != {"10"}:
        fails.append("selected-only must keep 10 when 20 unselected")
    match, _ = classify_packaging_match(
        selected_status=SELECTED_EXTRACTED,
        page_spec_matched=False,
        selected_tokens=parse_spec_tokens(sel10["selected_options"][0]["text"]),
        business_packs="20",
        business_layers="1",
        business_sheets="200",
    )
    if match != OBSERVED_MISMATCH:
        fails.append("explicit selected 10 vs target 20 must be OBSERVED_SPEC_MISMATCH")

    # 3) missing selected state
    unknown = {
        "selected_status": SELECTED_UNKNOWN,
        "selected_options": [],
        "available_options": [
            {"text": "200抽*20包", "selected": False},
            {"text": "200抽*10包", "selected": False},
        ],
    }
    cands_u, _ = selected_spec_candidates_for_unit(unknown)
    if cands_u:
        fails.append("unknown selected must not emit unit candidates from available")
    match_u, _ = classify_packaging_match(
        selected_status=SELECTED_UNKNOWN,
        page_spec_matched=False,
        selected_tokens=None,
        business_packs="20",
        business_layers="1",
        business_sheets="200",
    )
    if match_u != SELECTED_UNKNOWN:
        fails.append("missing selected must be SELECTED_SPEC_UNKNOWN not mismatch")

    # 4) multi-dimension selected combination
    multi = {
        "selected_status": SELECTED_EXTRACTED,
        "selected_options": [
            {"text": "擦手纸", "dimension": "系列品", "selected": True},
            {"text": "1层200张*20包", "dimension": "规格", "selected": True},
        ],
        "available_options": [],
    }
    cands_m, _ = selected_spec_candidates_for_unit(multi)
    if len(cands_m) != 2:
        fails.append("multi-dimension selected should keep each selected option")
    if not any(c.get("packs") == "20" for c in cands_m):
        fails.append("multi-dimension should parse packs from selected spec dim")

    # 5) before/after option change → caller compares; snapshot prefer after
    before = selection_snapshot_from_state(
        {
            "selected_status": SELECTED_EXTRACTED,
            "selected_spec_text": "10包",
            "selected_options": [{"text": "10包"}],
        }
    )
    after = selection_snapshot_from_state(
        {
            "selected_status": SELECTED_EXTRACTED,
            "selected_spec_text": "20包",
            "selected_options": [{"text": "20包"}],
        }
    )
    if before["selected_spec_text"] == after["selected_spec_text"]:
        fails.append("before/after different selected text must remain distinct")

    return fails
