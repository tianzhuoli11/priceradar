"""
P5-I: classified candidate list → detail verify sample → compare report.

Reuses P5-A capture/OCR/compare and take-home clues. Does not re-search.
Does not start full 577-candidate batch. Stops after the sample.
"""

from __future__ import annotations

import argparse
import csv
import html
import importlib.util
import json
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
SAMPLE_ROOT = (PROJECT_ROOT / "artifacts" / "classified_detail_samples").resolve()

RULES_VERSION = "p5i-classified-detail-sample-v1"
DEFAULT_POLICY = (
    PROJECT_ROOT
    / "config"
    / "policies"
    / "b913a_model_scope_include_other_20261027.json"
)
DEFAULT_AUDIT = (
    PROJECT_ROOT
    / "artifacts"
    / "model_classify_audits"
    / "20261027T055454Z_modelcls_b4e4a0"
)
DEFAULT_BUSINESS = PROJECT_ROOT / "config" / "products" / "b913a.json"

CLASS_TARGET = "TARGET_MODEL_CANDIDATE"
CLASS_OTHER = "OTHER_MODEL"
CLASS_MULTI = "MULTI_MODEL_OR_UNKNOWN"

PACK_HINT_RE = re.compile(
    r"(\d+\s*包|\d+\s*层|\d+\s*张|\d+\s*抽|整箱|单层|双层|三层)",
    re.IGNORECASE,
)


def load_sibling(stem: str) -> Any:
    path = SCRIPTS_DIR / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(f"priceradar_{stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


p5a = load_sibling("p5a_verify_candidate")
p4b = load_sibling("p4b_compare_from_ocr")
adapt = load_sibling("priceradar_candidate_adapt")
take_home = load_sibling("priceradar_take_home_prices")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat(timespec="seconds")


def utc_stamp() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%SZ")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def csv_guard(value: Any) -> str:
    text = "" if value is None else str(value)
    if text[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def title_pack_signature(title: str) -> str:
    hits = PACK_HINT_RE.findall(title or "")
    if not hits:
        return "NO_PACK_HINT"
    return "|".join(h.replace(" ", "") for h in hits[:6])


def observed_other_key(row: dict[str, Any]) -> str:
    others = row.get("other_models_normalized") or []
    if isinstance(others, list) and others:
        return ",".join(sorted(str(x).upper() for x in others))
    conflict = row.get("conflict_token")
    if conflict:
        return str(conflict).upper()
    tokens = row.get("observed_model_tokens") or []
    vals = []
    for t in tokens:
        if isinstance(t, dict) and t.get("normalized"):
            vals.append(str(t["normalized"]).upper())
    return ",".join(sorted(vals)) if vals else "UNKNOWN_OTHER"


def select_samples(
    classified: list[dict[str, Any]],
    *,
    policy: dict[str, Any],
    max_total: int = 10,
    max_target: int = 5,
    max_other: int = 5,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Deterministic sample: sort by product_id, cover OTHER models/pack titles."""
    include = set(policy.get("include_classifications") or [CLASS_TARGET, CLASS_OTHER])
    exclude = set(policy.get("exclude_classifications") or [CLASS_MULTI])

    targets = [
        r
        for r in classified
        if r.get("classification") == CLASS_TARGET and CLASS_TARGET in include
    ]
    others = [
        r
        for r in classified
        if r.get("classification") == CLASS_OTHER and CLASS_OTHER in include
    ]
    # Stable sort by numeric product id
    def pid_key(r: dict[str, Any]) -> tuple[int, str]:
        pid = str(r.get("jd_product_id") or r.get("product_id") or "")
        try:
            return (int(pid), pid)
        except ValueError:
            return (10**18, pid)

    targets = sorted(targets, key=pid_key)
    others = sorted(others, key=pid_key)

    selected: list[dict[str, Any]] = []
    reasons: list[dict[str, Any]] = []

    # TARGET: first N by id (deterministic)
    for row in targets[:max_target]:
        pid = str(row.get("jd_product_id") or "")
        selected.append(row)
        reasons.append(
            {
                "product_id": pid,
                "classification": CLASS_TARGET,
                "selection_reason": (
                    f"deterministic_target_by_product_id_rank;"
                    f"pack_sig={title_pack_signature(str(row.get('title') or ''))}"
                ),
            }
        )

    # OTHER: cover distinct observed models + pack signatures
    seen_models: set[str] = set()
    seen_packs: set[str] = set()
    other_picked: list[dict[str, Any]] = []
    for row in others:
        if len(other_picked) >= max_other:
            break
        mkey = observed_other_key(row)
        pkey = title_pack_signature(str(row.get("title") or ""))
        prefer_new = (mkey not in seen_models) or (pkey not in seen_packs)
        # Always take first remaining if we still need fill and prefer_new, OR
        # if we haven't filled and this expands coverage; for deterministic fill
        # take when new model OR (same model but new pack) OR backlog fill.
        if prefer_new or len(other_picked) < max_other:
            if mkey in seen_models and pkey in seen_packs and len(
                [x for x in other_picked if observed_other_key(x) == mkey]
            ) >= 1:
                # Skip near-duplicates until coverage exhausted; fill later.
                continue
            other_picked.append(row)
            seen_models.add(mkey)
            seen_packs.add(pkey)

    # Fill OTHER if coverage filter left gaps
    if len(other_picked) < max_other:
        picked_ids = {str(r.get("jd_product_id")) for r in other_picked}
        for row in others:
            if len(other_picked) >= max_other:
                break
            pid = str(row.get("jd_product_id") or "")
            if pid in picked_ids:
                continue
            other_picked.append(row)
            picked_ids.add(pid)

    for row in other_picked:
        pid = str(row.get("jd_product_id") or "")
        selected.append(row)
        reasons.append(
            {
                "product_id": pid,
                "classification": CLASS_OTHER,
                "selection_reason": (
                    f"deterministic_other_cover;"
                    f"observed={observed_other_key(row)};"
                    f"pack_sig={title_pack_signature(str(row.get('title') or ''))};"
                    f"auth={((policy.get('authorization') or {}).get('other_model_label_zh'))}"
                ),
            }
        )

    selected = selected[:max_total]
    reasons = reasons[:max_total]
    meta = {
        "requested_max_total": max_total,
        "requested_max_target": max_target,
        "requested_max_other": max_other,
        "available_target": len(targets),
        "available_other": len(others),
        "selected_target": sum(
            1 for r in selected if r.get("classification") == CLASS_TARGET
        ),
        "selected_other": sum(
            1 for r in selected if r.get("classification") == CLASS_OTHER
        ),
        "excluded_multi_count": sum(
            1 for r in classified if r.get("classification") in exclude
        ),
        "selection_reasons": reasons,
        "shop_evidence": "none_in_classify_or_collect_payload_no_guess",
        "note": "不足时如实记录，不编造候选；未回填历史价格",
    }
    return selected, meta


def to_verify_candidate(row: dict[str, Any]) -> dict[str, Any]:
    pid = str(row.get("jd_product_id") or row.get("product_id") or "").strip()
    url = str(row.get("normalized_url") or row.get("observed_url") or "").strip()
    if not url and pid:
        url = f"https://item.jd.com/{pid}.html"
    return {
        "product_id": pid,
        "jd_product_id": pid,
        "title": row.get("title"),
        "shop_name": row.get("shop_name"),  # usually absent — do not invent
        "ad_marked": row.get("ad_marked"),
        "ad_status": row.get("ad_status"),
        "observed_url": row.get("observed_url"),
        "normalized_url": url,
        "discovery_source": "classified_audit_sample",
        "classification": row.get("classification"),
        "observed_model_tokens": row.get("observed_model_tokens"),
        "other_models_normalized": row.get("other_models_normalized"),
        "conflict_token": row.get("conflict_token"),
        "reason": row.get("reason"),
        "rule_version": row.get("rule_version"),
        "source_run_id": row.get("source_run_id"),
        "source_page": row.get("source_page"),
    }


def clue_summary(clues: list[dict[str, Any]]) -> dict[str, Any]:
    take_homes = []
    primaries = []
    for c in clues or []:
        ptype = str(c.get("price_type") or "")
        if "TAKE_HOME" in ptype:
            take_homes.append(c)
        if ptype == take_home.PRICE_TYPE_PAGE_PRIMARY or "PRIMARY" in ptype:
            primaries.append(c)
    possible = [c for c in take_homes if c.get("below_red_line") or c.get("possible_break")]
    primary_low = [
        c
        for c in primaries
        if c.get("below_red_line") or c.get("low_price_clue") or c.get("clue_kind") == "PRIMARY_LOW"
    ]
    # Fallback: any clue marked primary display compare
    if not primary_low:
        primary_low = [
            c
            for c in clues or []
            if str(c.get("role") or "").startswith("page_primary")
            and (c.get("below_red_line") or c.get("delta_vs_red_line") is not None)
            and str(c.get("comparable_status") or "") in {"COMPARABLE", "BOX_COMPARABLE", "true", "True"}
        ]
    return {
        "take_home_count": len(take_homes),
        "primary_count": len(primaries),
        "possible_break_count": len(possible),
        "primary_low_clue_count": len(primary_low),
        "best_take_home": take_homes[0] if take_homes else None,
        "best_primary": primaries[0] if primaries else None,
    }


def build_report_row(
    *,
    sample: dict[str, Any],
    verify: dict[str, Any],
    policy: dict[str, Any],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    cand = verify.get("candidate") or {}
    clues = verify.get("price_clues") or []
    cs = clue_summary(clues)
    th = cs.get("best_take_home") or {}
    prim = cs.get("best_primary") or {}
    auth = policy.get("authorization") or {}
    cls = sample.get("classification")
    observed = verify.get("observed_model") or observed_other_key(sample)
    model_status = verify.get("model_status")
    if cls == CLASS_OTHER:
        # Never display as page-confirmed expected model.
        if model_status in {None, "PAGE_MODEL_CONFIRMED_EXPECTED"}:
            model_status = "PAGE_MODEL_OTHER_USER_AUTHORIZED_PENDING_OCR"
        user_auth_note = auth.get("other_model_label_zh") or "用户指定按B913A基准比较"
    elif cls == CLASS_TARGET:
        user_auth_note = "TARGET纳入本次业务基准样本"
    else:
        user_auth_note = "未授权自动纳入"

    page_primary = verify.get("page_primary_price")
    if page_primary is None and prim:
        page_primary = prim.get("amount") or prim.get("price")

    take_home_price = th.get("amount") or th.get("price")
    take_home_type = th.get("price_type")
    promo = th.get("condition_text") or th.get("promo_condition") or ""

    normalized = None
    delta = None
    # Prefer pipeline comparison when box comparable
    for c in clues:
        if c.get("normalized_box_price") is not None:
            normalized = c.get("normalized_box_price")
            delta = c.get("delta_vs_red_line")
            break

    system_verdict = verify.get("system_verdict")
    packaging = verify.get("packaging_match")
    box_ok = verify.get("box_comparable")
    unit_comparable = (
        "COMPARABLE"
        if box_ok
        else (
            "NOT_COMPARABLE"
            if packaging == "FAILED"
            else "REVIEW_REQUIRED"
        )
    )

    return {
        "business_sku": cfg.get("business_sku"),
        "expected_model": cfg.get("expected_model"),
        "observed_model": observed,
        "original_classification": cls,
        "user_authorization_note": user_auth_note,
        "model_scope_policy": policy.get("model_scope_policy"),
        "model_scope_policy_id": policy.get("policy_id"),
        "product_id": sample.get("jd_product_id") or sample.get("product_id"),
        "link": (verify.get("url_adapt") or {}).get("adapted_url")
        or sample.get("normalized_url"),
        "final_url": (verify.get("final_identity") or {}).get("final_url_sanitized")
        or (verify.get("url_adapt") or {}).get("final_url_sanitized"),
        "shop": verify.get("shop_observed") or cand.get("shop_name_from_search"),
        "selected_spec": verify.get("packaging_basis_note"),
        "title": sample.get("title") or cand.get("title"),
        "page_primary_price": page_primary,
        "page_primary_price_type": "PAGE_PRIMARY_DISPLAY",
        "take_home_price": take_home_price,
        "take_home_price_type": take_home_type or "TAKE_HOME_PENDING",
        "promo_condition": promo,
        "normalized_box_price": normalized,
        "red_line": cfg.get("red_line"),
        "delta_vs_red_line": delta,
        "model_status": model_status,
        "model_match_business_config": verify.get("model_match_business_config"),
        "unit_comparable_status": unit_comparable,
        "packaging_match": packaging,
        "box_comparable": box_ok,
        "system_verdict": system_verdict,
        "system_verdict_zh": verify.get("system_verdict_zh"),
        "manual_verdict": None,
        "task_status": verify.get("task_status"),
        "match_status": verify.get("match_status"),
        "price_evidence_status": verify.get("price_evidence_status"),
        "has_possible_break": (verify.get("take_home_rollup") or {}).get(
            "has_possible_break"
        ),
        "primary_low_clue": bool(cs.get("primary_low_clue_count")),
        "screenshot_dir": verify.get("screenshot_dir")
        or ((verify.get("capture") or {}).get("out_dir")),
        "pipeline_dir": verify.get("pipeline_dir")
        or ((verify.get("pipeline") or {}).get("out_dir")),
        "verify_dir": verify.get("verify_dir"),
        "review_reasons": "; ".join(
            r for r in (verify.get("review_reasons") or []) if r
        ),
        "capture_backend": "p2d_jd_auto_capture",
        "notes": (
            "主展示价仅作参考比较，不冒充成交价；"
            "无到手价证据时保持到手价待核验"
        ),
    }


def write_report_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "business_sku",
        "observed_model",
        "original_classification",
        "user_authorization_note",
        "product_id",
        "link",
        "shop",
        "selected_spec",
        "page_primary_price",
        "page_primary_price_type",
        "take_home_price",
        "take_home_price_type",
        "promo_condition",
        "normalized_box_price",
        "red_line",
        "delta_vs_red_line",
        "model_status",
        "unit_comparable_status",
        "system_verdict",
        "manual_verdict",
        "task_status",
        "has_possible_break",
        "primary_low_clue",
        "verify_dir",
        "screenshot_dir",
        "pipeline_dir",
        "review_reasons",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: csv_guard(row.get(k)) for k in fields})


def write_price_scheme_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """One row per price clue / scheme."""
    fields = [
        "product_id",
        "observed_model",
        "original_classification",
        "price_type",
        "amount",
        "condition_text",
        "normalized_box_price",
        "red_line",
        "delta_vs_red_line",
        "below_red_line",
        "comparable_status",
        "system_role",
        "evidence_path",
    ]
    flat: list[dict[str, Any]] = []
    for row in rows:
        clues = row.get("_clues") or []
        if not clues:
            flat.append(
                {
                    "product_id": row.get("product_id"),
                    "observed_model": row.get("observed_model"),
                    "original_classification": row.get("original_classification"),
                    "price_type": "NONE",
                    "amount": None,
                    "condition_text": "",
                    "normalized_box_price": row.get("normalized_box_price"),
                    "red_line": row.get("red_line"),
                    "delta_vs_red_line": row.get("delta_vs_red_line"),
                    "below_red_line": None,
                    "comparable_status": row.get("unit_comparable_status"),
                    "system_role": "placeholder",
                    "evidence_path": row.get("pipeline_dir"),
                }
            )
            continue
        for c in clues:
            flat.append(
                {
                    "product_id": row.get("product_id"),
                    "observed_model": row.get("observed_model"),
                    "original_classification": row.get("original_classification"),
                    "price_type": c.get("price_type"),
                    "amount": c.get("amount") or c.get("price"),
                    "condition_text": c.get("condition_text") or c.get("promo_condition"),
                    "normalized_box_price": c.get("normalized_box_price"),
                    "red_line": row.get("red_line"),
                    "delta_vs_red_line": c.get("delta_vs_red_line"),
                    "below_red_line": c.get("below_red_line"),
                    "comparable_status": c.get("comparable_status"),
                    "system_role": c.get("role") or c.get("clue_kind"),
                    "evidence_path": c.get("evidence_path") or row.get("pipeline_dir"),
                }
            )
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in flat:
            w.writerow({k: csv_guard(row.get(k)) for k in fields})


def write_report_html(
    path: Path,
    *,
    run_id: str,
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
    policy: dict[str, Any],
) -> None:
    esc = html.escape
    auth = policy.get("authorization") or {}
    body_rows = []
    for r in rows:
        body_rows.append(
            "<tr>"
            + "".join(
                f"<td>{esc(str(r.get(k) if r.get(k) is not None else ''))}</td>"
                for k in (
                    "product_id",
                    "original_classification",
                    "observed_model",
                    "user_authorization_note",
                    "shop",
                    "page_primary_price",
                    "take_home_price",
                    "take_home_price_type",
                    "normalized_box_price",
                    "red_line",
                    "delta_vs_red_line",
                    "model_status",
                    "unit_comparable_status",
                    "system_verdict",
                    "manual_verdict",
                    "task_status",
                )
            )
            + f"<td><code>{esc(str(r.get('verify_dir') or ''))}</code></td>"
            + "</tr>"
        )
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>P5-I 分类样本详情核验 {esc(run_id)}</title>
<style>
body {{ font-family: "Segoe UI", "PingFang SC", sans-serif; margin: 24px; color: #1a1a1a; }}
h1 {{ font-size: 1.4rem; }}
table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
th, td {{ border: 1px solid #ccc; padding: 6px 8px; vertical-align: top; }}
th {{ background: #f3f3f3; position: sticky; top: 0; }}
.meta {{ background: #faf7f2; padding: 12px 16px; margin-bottom: 16px; }}
code {{ font-size: 11px; word-break: break-all; }}
.note {{ color: #555; font-size: 13px; }}
</style>
</head>
<body>
<h1>P5-I 明确候选清单 → 详情核验样本</h1>
<div class="meta">
<p><b>run_id</b>: {esc(run_id)}</p>
<p><b>policy</b>: {esc(str(policy.get('policy_id')))} / {esc(str(policy.get('model_scope_policy')))}</p>
<p><b>授权</b>: {esc(str(auth.get('statement_zh') or ''))}</p>
<p><b>范围</b>: {esc(str(auth.get('scope_note_zh') or ''))}</p>
<p><b>红线</b>: 来自业务配置（非程序硬编码）</p>
<p><b>统计</b>: 访问={summary.get('attempted')} 成功={summary.get('completed')}
失败={summary.get('failed')} 暂停={summary.get('paused')}
跳过={summary.get('skipped')} 可比箱价={summary.get('box_comparable_count')}
疑似破价={summary.get('possible_break_count')} 主展示低价线索={summary.get('primary_low_clue_count')}</p>
<p class="note">主展示价仅作参考比较，不冒充成交价。人工结论默认空。本轮小样本结束后停止，不启动全量。</p>
</div>
<table>
<thead><tr>
<th>商品ID</th><th>原分类</th><th>实际型号</th><th>授权说明</th><th>店铺</th>
<th>主展示价</th><th>到手价</th><th>到手类型</th><th>归一化箱价</th>
<th>红线</th><th>差额</th><th>型号状态</th><th>单位可比</th>
<th>系统判定</th><th>人工结论</th><th>任务状态</th><th>证据路径</th>
</tr></thead>
<tbody>
{''.join(body_rows)}
</tbody>
</table>
</body>
</html>
"""
    path.write_text(doc, encoding="utf-8")


def run_offline_checks(policy: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    failures.extend(p4b.run_self_tests())

    basis_biz = {
        **p4b.BUSINESS,
        "business_sku": "B913A",
        "packaging_basis": {
            "applicable_model": "B913A",
            "packs_per_box": 20,
            "sheets_per_pack": 200,
            "sheet_unit_page_aliases": ["抽", "张"],
            "source_name": "test",
            "confirmed_date": "2026-10-27",
        },
    }
    strict = p4b.analyze_spec_and_unit(
        [{"packs": "20", "layers": "1", "sheets_or_pulls": "200"}],
        [{"model": "B913AC"}],
        business=basis_biz,
    )
    if strict.get("box_comparable"):
        failures.append("offline: strict OTHER must not compare")

    auth = p4b.analyze_spec_and_unit(
        [{"packs": "20", "layers": "1", "sheets_or_pulls": "200"}],
        [{"model": "B913AC"}],
        business=basis_biz,
        model_scope_policy=policy,
    )
    if not auth.get("box_comparable"):
        failures.append("offline: authorized OTHER with match should compare")
    if auth.get("model_match_business_config"):
        failures.append("offline: must keep model_match false for OTHER")
    if auth.get("ocr_recognized", {}).get("models") != ["B913AC"]:
        failures.append("offline: must keep observed B913AC")

    g = adapt.title_model_gate("清风擦手纸B913AC 20包", expected_model="B913A")
    if not g.get("skip_browse"):
        failures.append("offline: default OTHER title should skip_browse")
    auth_browse = p5a.model_scope_allows_title_browse(
        policy, gate={**g, "classification": CLASS_OTHER}, business_sku="B913A"
    )
    if not auth_browse.get("allowed"):
        failures.append("offline: policy should allow OTHER browse")
    multi_block = p5a.model_scope_allows_title_browse(
        policy,
        gate={"classification": CLASS_MULTI, "skip_browse": True},
        business_sku="B913A",
    )
    if multi_block.get("allowed"):
        failures.append("offline: MULTI must not be auto-included")
    leak = p5a.model_scope_allows_title_browse(
        policy, gate={**g, "classification": CLASS_OTHER}, business_sku="OTHERSKU"
    )
    if leak.get("allowed"):
        failures.append("offline: policy must not leak to other SKU")

    bad = adapt.adapt_candidate_url(
        source_url="https://item.jd.com/111.html", candidate_product_id="222"
    )
    if bad.get("ok"):
        failures.append("offline: id conflict must not adapt ok")

    limit_pause = p5a.detect_session_block_from_capture(
        {
            "navigation_status": "WRONG_PAGE_OR_CHALLENGE",
            "actual_product_url": "https://trade.m.jd.com/common/limit.html",
            "error": "",
        }
    )
    if limit_pause != "DETAIL_RATE_LIMITED":
        failures.append("offline: limit.html must pause as DETAIL_RATE_LIMITED")
    # Session block field must beat a misleading identity failure URL.
    priority = p5a.detect_session_block_from_capture(
        {
            "session_block": "DETAIL_RATE_LIMITED",
            "navigation_status": "IDENTITY_FAILED",
            "actual_product_url": "https://trade.m.jd.com/common/limit.html",
            "product_identity_ok": False,
        }
    )
    if priority != "DETAIL_RATE_LIMITED":
        failures.append("offline: session_block must outrank identity labeling")
    failures.extend(run_pause_navigation_offline_test())
    try:
        bsk_detail = load_sibling("p5_bsk_detail")
        failures.extend(bsk_detail.run_offline_bsk_detail_checks())
    except Exception as exc:  # noqa: BLE001
        failures.append(f"offline: bsk_detail checks load failed: {exc}")

    fake = []
    for i in range(8):
        fake.append(
            {
                "jd_product_id": str(1000 + i),
                "title": f"B913A 1层200张*20包 sample{i}",
                "classification": CLASS_TARGET,
                "observed_model_tokens": [
                    {"raw": "b913a", "normalized": "b913a", "context_kind": "product"}
                ],
            }
        )
    for i, model in enumerate(
        ["B913AC", "B913AE", "B913A2K", "B913A1C", "B913A2W", "X"]
    ):
        fake.append(
            {
                "jd_product_id": str(2000 + i),
                "title": f"清风 {model} {'双层' if i % 2 else '1层'}{200 + i}张*20包",
                "classification": CLASS_OTHER,
                "other_models_normalized": [model.lower()],
                "conflict_token": model.lower(),
                "observed_model_tokens": [
                    {
                        "raw": model.lower(),
                        "normalized": model.lower(),
                        "context_kind": "product",
                    }
                ],
            }
        )
    fake.append(
        {
            "jd_product_id": "9999",
            "title": "未知多型号",
            "classification": CLASS_MULTI,
        }
    )
    selected, meta = select_samples(
        fake, policy=policy, max_total=10, max_target=5, max_other=5
    )
    if meta["selected_target"] != 5 or meta["selected_other"] != 5:
        failures.append(
            f"offline: sample counts expected 5+5 got "
            f"{meta['selected_target']}+{meta['selected_other']}"
        )
    if any(r.get("classification") == CLASS_MULTI for r in selected):
        failures.append("offline: MULTI must not appear in sample")
    selected2, _ = select_samples(
        fake, policy=policy, max_total=10, max_target=5, max_other=5
    )
    if [r.get("jd_product_id") for r in selected] != [
        r.get("jd_product_id") for r in selected2
    ]:
        failures.append("offline: sample selection must be deterministic")

    row = build_report_row(
        sample=selected[0],
        verify={
            "candidate": {},
            "price_clues": [],
            "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
            "system_verdict_zh": "到手价待核验",
            "manual_verdict": None,
            "task_status": "COMPLETED",
            "packaging_match": "FAILED",
            "box_comparable": False,
            "take_home_rollup": {"has_possible_break": False},
            "url_adapt": {},
        },
        policy=policy,
        cfg={
            "business_sku": "B913A",
            "expected_model": "B913A",
            "red_line": "127.00",
        },
    )
    if row.get("manual_verdict") is not None:
        failures.append("offline: manual_verdict must default null")
    if row.get("take_home_price_type") != "TAKE_HOME_PENDING":
        failures.append("offline: missing take-home must stay pending type")

    return failures


def run_pause_navigation_offline_test() -> list[str]:
    """Prove: first PAUSED_SESSION → zero further navigations (not post-hoc relabel)."""
    failures: list[str] = []
    nav_calls: list[str] = []

    def fake_verify(*, product_id: str, **_kwargs: Any) -> dict[str, Any]:
        nav_calls.append(str(product_id))
        if len(nav_calls) == 1:
            return {
                "task_status": "PAUSED_SESSION",
                "match_status": "DETAIL_RATE_LIMITED",
                "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                "price_clues": [],
                "manual_verdict": None,
                "candidate": {},
                "url_adapt": {},
                "packaging_match": None,
                "box_comparable": False,
                "take_home_rollup": {},
            }
        return {
            "task_status": "COMPLETED",
            "match_status": "PACKAGING_PASSED",
            "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
            "price_clues": [],
            "manual_verdict": None,
            "candidate": {},
            "url_adapt": {},
            "packaging_match": "PASSED",
            "box_comparable": True,
            "take_home_rollup": {},
        }

    samples = [
        {"jd_product_id": "111", "classification": CLASS_TARGET, "title": "t1"},
        {"jd_product_id": "222", "classification": CLASS_TARGET, "title": "t2"},
        {"jd_product_id": "333", "classification": CLASS_OTHER, "title": "t3"},
    ]
    progress = {"paused": False, "pause_reason": None, "items": []}
    for sample in samples:
        if progress.get("paused"):
            break
        verify = fake_verify(product_id=str(sample["jd_product_id"]))
        if verify.get("task_status") == "PAUSED_SESSION":
            progress["paused"] = True
            progress["pause_reason"] = verify.get("match_status")
        progress["items"].append(sample["jd_product_id"])

    if nav_calls != ["111"]:
        failures.append(
            f"offline: pause must stop navigations after first; got nav={nav_calls}"
        )
    if progress.get("pause_reason") != "DETAIL_RATE_LIMITED":
        failures.append("offline: pause reason must be DETAIL_RATE_LIMITED")
    if len(progress["items"]) != 1:
        failures.append("offline: only first item should be recorded before pause break")
    return failures


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="P5-I classified → detail sample")
    p.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    p.add_argument("--classify-audit-dir", type=Path, default=DEFAULT_AUDIT)
    p.add_argument("--business-config", type=Path, default=DEFAULT_BUSINESS)
    p.add_argument("--max-total", type=int, default=10)
    p.add_argument("--max-target", type=int, default=5)
    p.add_argument("--max-other", type=int, default=5)
    p.add_argument("--offline-only", action="store_true")
    p.add_argument(
        "--live",
        action="store_true",
        help="Run live detail verify for the selected sample (default with no --offline-only)",
    )
    p.add_argument(
        "--backend",
        choices=["p2d", "bsk"],
        default="p2d",
        help="Capture backend (explicit). No auto-fallback between p2d and bsk.",
    )
    p.add_argument("--bsk-browser", default=None, help="Optional BSK browser selector")
    p.add_argument(
        "--product-id",
        default=None,
        help="If set, only verify this product id from the selected/manifest pool",
    )
    p.add_argument(
        "--from-manifest",
        type=Path,
        default=None,
        help="Reuse samples from an existing sample_manifest.json (no re-search)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    policy_path = Path(args.policy).resolve()
    audit_dir = Path(args.classify_audit_dir).resolve()
    business_config = Path(args.business_config).resolve()

    if not policy_path.is_file():
        print(f"policy missing: {policy_path}")
        return 2
    policy = load_json(policy_path)
    if not isinstance(policy, dict):
        print("policy must be object")
        return 2

    print("=== P5-I offline checks ===")
    fails = list(dict.fromkeys(run_offline_checks(policy)))
    if fails:
        print(f"OFFLINE FAILED ({len(fails)}):")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("OK: offline gate/policy/sample checks passed")

    if args.offline_only and not args.live:
        return 0

    classified_path = audit_dir / "all_classified.json"
    if not classified_path.is_file():
        print(f"missing classified file: {classified_path}")
        return 2
    classified = load_json(classified_path)
    if not isinstance(classified, list):
        print("all_classified.json must be a list")
        return 2

    cfg, cfg_errs = adapt.load_business_baseline(business_config)
    if cfg_errs or cfg is None:
        print(f"business config invalid: {cfg_errs}")
        return 2

    # Policy SKU must match this run's business
    if str(policy.get("business_sku") or "").upper() != str(cfg["business_sku"]).upper():
        print("policy business_sku mismatch vs business config — refuse")
        return 2

    limits = policy.get("sample_limits") or {}
    max_total = int(args.max_total or limits.get("max_total") or 10)
    max_target = int(args.max_target or limits.get("max_target") or 5)
    max_other = int(args.max_other or limits.get("max_other") or 5)

    backend = str(args.backend or "p2d").strip().lower()
    if args.from_manifest is not None:
        prev = load_json(Path(args.from_manifest).resolve())
        prev_samples = prev.get("samples") or []
        selected = []
        for s in prev_samples:
            selected.append(
                {
                    "jd_product_id": s.get("product_id"),
                    "title": s.get("title"),
                    "classification": s.get("classification"),
                    "observed_model_tokens": s.get("observed_model_tokens"),
                    "other_models_normalized": s.get("other_models_normalized"),
                    "normalized_url": s.get("normalized_url"),
                    "reason": "from_prior_manifest",
                }
            )
        sel_meta = {
            "source": "from_manifest",
            "manifest_path": str(Path(args.from_manifest).resolve()),
            "selected_target": sum(
                1 for r in selected if r.get("classification") == CLASS_TARGET
            ),
            "selected_other": sum(
                1 for r in selected if r.get("classification") == CLASS_OTHER
            ),
            "selection_reasons": [
                {
                    "product_id": r.get("jd_product_id"),
                    "classification": r.get("classification"),
                    "selection_reason": "reused_prior_sample_manifest",
                }
                for r in selected
            ],
        }
    else:
        selected, sel_meta = select_samples(
            classified,
            policy=policy,
            max_total=max_total,
            max_target=max_target,
            max_other=max_other,
        )

    if args.product_id:
        want = str(args.product_id).strip()
        selected = [
            r
            for r in selected
            if str(r.get("jd_product_id") or r.get("product_id") or "") == want
        ]
        sel_meta = dict(sel_meta)
        sel_meta["product_id_filter"] = want
        sel_meta["selected_after_filter"] = len(selected)
        if not selected:
            print(f"product-id {want} not in selected/manifest pool — refuse substitute")
            return 2

    run_id = f"{utc_stamp()}_p5i_{secrets.token_hex(3)}"
    out_dir = SAMPLE_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=False)
    items_dir = out_dir / "items"
    items_dir.mkdir(parents=True, exist_ok=True)

    candidates = [to_verify_candidate(r) for r in selected]
    candidates_path = out_dir / "candidates.json"
    write_json(candidates_path, candidates)

    manifest = {
        "run_id": run_id,
        "phase": "P5-I",
        "rules_version": RULES_VERSION,
        "created_at_utc": utc_iso(),
        "policy_path": str(policy_path),
        "policy_id": policy.get("policy_id"),
        "model_scope_policy": policy.get("model_scope_policy"),
        "source_classify_audit": str(audit_dir),
        "source_classify_audit_id": audit_dir.name,
        "business_config": str(business_config),
        "business_sku": cfg["business_sku"],
        "red_line_from_config": cfg["red_line"],
        "capture_backend": (
            "p5_bsk_detail" if backend == "bsk" else "p2d_jd_auto_capture"
        ),
        "backend": backend,
        "selection": sel_meta,
        "samples": [
            {
                "product_id": c["product_id"],
                "title": c.get("title"),
                "classification": c.get("classification"),
                "observed_model_tokens": c.get("observed_model_tokens"),
                "other_models_normalized": c.get("other_models_normalized"),
                "normalized_url": c.get("normalized_url"),
                "selection_reason": next(
                    (
                        x["selection_reason"]
                        for x in sel_meta["selection_reasons"]
                        if x["product_id"] == c["product_id"]
                    ),
                    None,
                ),
                "authorization_policy": policy.get("model_scope_policy"),
                "authorization_note": (
                    (policy.get("authorization") or {}).get("other_model_label_zh")
                    if c.get("classification") == CLASS_OTHER
                    else "TARGET纳入本次业务基准样本"
                ),
            }
            for c in candidates
        ],
        "notes": [
            "不重新搜索；不启动全量；MULTI本轮不纳入",
            "授权仅放行型号差异；商品ID/包装/单位仍须核验",
            "不自动领券/加购/结算/切规格",
            "无店铺字段时不猜测店铺",
        ],
    }
    write_json(out_dir / "sample_manifest.json", manifest)
    write_json(out_dir / "policy_snapshot.json", policy)
    print(f"run_dir={out_dir}")
    print(
        f"sample: target={sel_meta['selected_target']} "
        f"other={sel_meta['selected_other']} total={len(candidates)}"
    )

    progress: dict[str, Any] = {
        "run_id": run_id,
        "started_at_utc": utc_iso(),
        "items": [],
        "paused": False,
        "pause_reason": None,
    }
    report_rows: list[dict[str, Any]] = []
    summary = {
        "attempted": 0,
        "completed": 0,
        "failed": 0,
        "paused": 0,
        "skipped": 0,
        "box_comparable_count": 0,
        "possible_break_count": 0,
        "primary_low_clue_count": 0,
        "take_home_pending_count": 0,
    }

    for idx, sample in enumerate(selected, start=1):
        if progress.get("paused"):
            break
        pid = str(sample.get("jd_product_id") or "")
        item_dir = items_dir / f"{idx:02d}_{pid}"
        item_dir.mkdir(parents=True, exist_ok=True)
        summary["attempted"] += 1
        print(f"\n=== [{idx}/{len(selected)}] verify {pid} ({sample.get('classification')}) ===")
        try:
            verify = p5a.verify_one(
                business_config=business_config,
                candidates_path=candidates_path,
                product_id=pid,
                out_dir=item_dir / "verify",
                model_scope_policy=policy,
                backend=backend,
                bsk_browser=args.bsk_browser,
            )
        except Exception as exc:  # noqa: BLE001
            verify = {
                "task_status": "FAILED",
                "match_status": "EXCEPTION",
                "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                "system_verdict_zh": "异常",
                "failure_reason": f"{type(exc).__name__}: {exc}",
                "candidate": {"title": sample.get("title")},
                "price_clues": [],
                "manual_verdict": None,
                "url_adapt": {"adapted_url": sample.get("normalized_url")},
            }

        status = verify.get("task_status")
        if status == "COMPLETED":
            summary["completed"] += 1
        elif status == "SKIPPED":
            summary["skipped"] += 1
        elif status == "PAUSED_SESSION":
            summary["paused"] += 1
            progress["paused"] = True
            progress["pause_reason"] = verify.get("match_status") or verify.get(
                "failure_reason"
            )
        else:
            summary["failed"] += 1

        row = build_report_row(
            sample=sample, verify=verify, policy=policy, cfg=cfg
        )
        row["_clues"] = verify.get("price_clues") or []
        if row.get("box_comparable"):
            summary["box_comparable_count"] += 1
        if row.get("has_possible_break"):
            summary["possible_break_count"] += 1
        if row.get("primary_low_clue"):
            summary["primary_low_clue_count"] += 1
        if row.get("take_home_price_type") in {None, "TAKE_HOME_PENDING"} or not row.get(
            "take_home_price"
        ):
            summary["take_home_pending_count"] += 1

        item_rec = {
            "index": idx,
            "product_id": pid,
            "classification": sample.get("classification"),
            "task_status": status,
            "system_verdict": verify.get("system_verdict"),
            "verify_dir": str(item_dir / "verify"),
            "row": {k: v for k, v in row.items() if k != "_clues"},
        }
        progress["items"].append(item_rec)
        report_rows.append(row)

        # Persist immediately after each item
        write_json(out_dir / "progress.json", progress)
        write_json(item_dir / "item_result.json", item_rec)
        write_report_csv(
            out_dir / "compare_report.csv",
            [{k: v for k, v in r.items() if k != "_clues"} for r in report_rows],
        )
        write_price_scheme_csv(out_dir / "price_schemes.csv", report_rows)
        write_report_html(
            out_dir / "compare_report.html",
            run_id=run_id,
            summary=summary,
            rows=[{k: v for k, v in r.items() if k != "_clues"} for r in report_rows],
            policy=policy,
        )

        if progress.get("paused"):
            print(f"PAUSED: {progress.get('pause_reason')} — remaining items not started")
            break

    summary["ended_at_utc"] = utc_iso()
    summary["run_id"] = run_id
    summary["out_dir"] = str(out_dir)
    summary["sample_count"] = len(selected)
    summary["report_rows"] = len(report_rows)
    summary["html"] = str(out_dir / "compare_report.html")
    summary["csv"] = str(out_dir / "compare_report.csv")
    summary["manifest"] = str(out_dir / "sample_manifest.json")
    write_json(out_dir / "summary.json", summary)
    write_json(out_dir / "progress.json", {**progress, "ended_at_utc": utc_iso()})

    print("\n=== P5-I done ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nInvoke-Item '{out_dir / 'compare_report.html'}'")
    return 3 if progress.get("paused") else 0


if __name__ == "__main__":
    # Ensure scripts dir import for p4b direct import fallback
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    raise SystemExit(main())
