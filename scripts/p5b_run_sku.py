"""
P5-B: single business SKU — search → verify each candidate → unified tables.

Reuses p3a.run_search_collect and p5a.verify_one (no shell, no “latest” dirs).
Does not implement multi-SKU Excel, pagination, checkout, or coupon claiming.
"""

from __future__ import annotations

import argparse
import csv
import html
import importlib.util
import json
import secrets
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
JOB_ROOT = (PROJECT_ROOT / "artifacts" / "sku_jobs").resolve()

RULES_VERSION = "p5b-sku-job-v2"
DEFAULT_MAX_VERIFY = 5
MAX_VERIFY_CAP = 30
STATUS_NOT_VERIFIED_BUDGET = "NOT_VERIFIED_BUDGET"

SESSION_PAUSE_TASK = "PAUSED_SESSION"
SESSION_PAUSE_MATCH = frozenset(
    {
        "PAUSED_SESSION",
        "LOGIN_REQUIRED",
        "VERIFICATION_REQUIRED",
        "SEARCH_RESTRICTED",
        "WRONG_PAGE_OR_CHALLENGE",
        "DETAIL_RATE_LIMITED",
    }
)
SEARCH_BLOCK_STATUSES = frozenset(
    {
        "LOGIN_REQUIRED",
        "VERIFICATION_REQUIRED",
        "SEARCH_RESTRICTED",
    }
)

TAKE_HOME_TYPES = frozenset(
    {
        "PAGE_DECLARED_CONDITIONAL_TAKE_HOME",
        "PAGE_ESTIMATED_TAKE_HOME",
        "RULE_CALCULATED_TAKE_HOME",
    }
)


def load_sibling(stem: str) -> Any:
    path = SCRIPTS_DIR / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(f"priceradar_{stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


p3a = load_sibling("p3a_jd_search")
p5a = load_sibling("p5a_verify_candidate")
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


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: csv_guard(row.get(k)) for k in fields})


def to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def make_job_dir(business_sku: str) -> tuple[str, Path]:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in business_sku)[:32]
    job_id = f"{utc_stamp()}_{safe}_{secrets.token_hex(3)}"
    out = JOB_ROOT / job_id
    out.mkdir(parents=True, exist_ok=False)
    return job_id, out


def default_query_from_baseline(cfg: dict[str, Any]) -> tuple[str, str]:
    brand = str(cfg.get("expected_brand") or "").strip()
    model = str(cfg.get("expected_model") or cfg.get("business_sku") or "").strip()
    parts = [p for p in (brand, model) if p]
    query = " ".join(parts)
    basis = "expected_brand + expected_model（配置生成；非历史商品ID）"
    if not query:
        query = str(cfg.get("business_sku") or "").strip()
        basis = "business_sku only"
    return query, basis


def save_progress(job_dir: Path, progress: dict[str, Any]) -> None:
    progress["updated_at_utc"] = utc_iso()
    write_json(job_dir / "job_progress.json", progress)


PRODUCT_ROW_FIELDS = [
    "business_sku",
    "jd_product_id",
    "search_rank",
    "title",
    "url",
    "shop",
    "ad_marked",
    "observed_model",
    "observed_spec",
    "primary_display_price",
    "take_home_status",
    "lowest_observed_take_home_price",
    "lowest_take_home_option_id",
    "lowest_take_home_price_type",
    "lowest_take_home_conditions",
    "normalized_price",
    "red_line",
    "price_delta",
    "low_price_signal",
    "signal_strength",
    "signal_price_type",
    "signal_amount",
    "signal_normalized_price",
    "signal_conditions",
    "signal_review_reasons",
    "task_status",
    "match_status",
    "system_verdict",
    "system_verdict_zh",
    "manual_verdict",
    "legacy_page_price_verdict",
    "review_reasons",
    "checked_at",
    "candidate_run_dir",
    "evidence_paths",
    "packaging_match",
    "box_comparable",
]

PRICE_OPTION_FIELDS = [
    "option_id",
    "business_sku",
    "jd_product_id",
    "search_rank",
    "price_type",
    "amount",
    "raw_label",
    "conditions",
    "minimum_quantity",
    "eligibility_status",
    "normalized_price",
    "red_line",
    "price_delta",
    "system_verdict",
    "system_verdict_zh",
    "manual_verdict",
    "comparable_for_min",
    "is_estimate",
    "evidence_source",
    "evidence_path",
    "calculation_basis",
    "review_reasons",
]


def is_comparable_take_home(clue: dict[str, Any]) -> bool:
    if clue.get("price_type") not in TAKE_HOME_TYPES:
        return False
    if clue.get("normalized_price") in (None, ""):
        return False
    # Must have completed evaluation with box evidence (POSSIBLE or NO_BREAK_IN_SCOPE)
    return clue.get("system_verdict") in {
        take_home.SYSTEM_POSSIBLE_BREAK,
        take_home.SYSTEM_NO_BREAK_IN_SCOPE,
    }


def build_price_options(
    *,
    business_sku: str,
    product_id: str,
    search_rank: Any,
    clues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for i, clue in enumerate(clues, start=1):
        ptype = clue.get("price_type")
        opt_id = f"{product_id}_opt{i}_{ptype or 'UNKNOWN'}"
        comparable = is_comparable_take_home(clue)
        options.append(
            {
                "option_id": opt_id,
                "business_sku": business_sku,
                "jd_product_id": product_id,
                "search_rank": search_rank,
                "price_type": ptype,
                "amount": clue.get("amount"),
                "raw_label": clue.get("raw_label"),
                "conditions": clue.get("conditions"),
                "minimum_quantity": clue.get("minimum_quantity"),
                "eligibility_status": clue.get("eligibility_status"),
                "normalized_price": clue.get("normalized_price"),
                "red_line": clue.get("red_line"),
                "price_delta": clue.get("price_delta"),
                "system_verdict": clue.get("system_verdict"),
                "system_verdict_zh": clue.get("system_verdict_zh"),
                "manual_verdict": None,
                "comparable_for_min": comparable,
                "is_estimate": ptype == take_home.PRICE_TYPE_ESTIMATED,
                "evidence_source": clue.get("evidence_source"),
                "evidence_path": clue.get("evidence_path"),
                "calculation_basis": clue.get("calculation_basis"),
                "review_reasons": "; ".join(clue.get("review_reasons") or []),
            }
        )
    return options


def product_row_from_verify(
    *,
    business_sku: str,
    red_line: str,
    candidate: dict[str, Any],
    verify: dict[str, Any],
    options: list[dict[str, Any]],
) -> dict[str, Any]:
    pid = str(candidate.get("product_id") or verify.get("candidate_product_id") or "")
    comparable_opts = [o for o in options if o.get("comparable_for_min")]
    lowest = None
    lowest_opt = None
    for o in comparable_opts:
        amt = to_decimal(o.get("normalized_price"))
        if amt is None:
            continue
        if lowest is None or amt < lowest:
            lowest = amt
            lowest_opt = o

    take_home_status = verify.get("price_evidence_status") or verify.get(
        "system_verdict_zh"
    )
    if verify.get("system_verdict") == take_home.SYSTEM_POSSIBLE_BREAK:
        take_home_status = "POSSIBLE_BREAK"
    elif verify.get("system_verdict") == take_home.SYSTEM_NO_BREAK_IN_SCOPE:
        take_home_status = "NO_BREAK_IN_SCOPE"
    elif verify.get("system_verdict") == "MODEL_MISMATCH_SKIPPED":
        take_home_status = "SKIPPED_MODEL_MISMATCH"
    elif verify.get("task_status") == SESSION_PAUSE_TASK:
        take_home_status = "PAUSED_SESSION"
    elif (verify.get("take_home_rollup") or {}).get("take_home_clue_count", 0) == 0:
        take_home_status = "TAKE_HOME_PENDING"

    delta = None
    if lowest is not None:
        red = to_decimal(red_line)
        if red is not None:
            delta = format(lowest - red, "f")

    evidence = []
    if verify.get("screenshot_dir"):
        evidence.append(str(verify["screenshot_dir"]))
    if verify.get("pipeline_dir"):
        evidence.append(str(verify["pipeline_dir"]))
    if verify.get("verify_dir"):
        evidence.append(str(verify["verify_dir"]))

    url_adapt = verify.get("url_adapt") or {}
    row = {
        "business_sku": business_sku,
        "jd_product_id": pid,
        "search_rank": candidate.get("rank"),
        "title": candidate.get("title")
        or (verify.get("candidate") or {}).get("title"),
        "url": url_adapt.get("final_url_sanitized")
        or url_adapt.get("adapted_url")
        or candidate.get("normalized_url")
        or candidate.get("observed_url"),
        "shop": verify.get("shop_observed")
        or candidate.get("shop_name")
        or "",
        "ad_marked": candidate.get("ad_marked"),
        "observed_model": verify.get("observed_model") or "",
        "observed_spec": "",  # filled if pipeline row available later
        "primary_display_price": verify.get("page_primary_price"),
        "take_home_status": take_home_status,
        "lowest_observed_take_home_price": format(lowest, "f") if lowest is not None else None,
        "lowest_take_home_option_id": (lowest_opt or {}).get("option_id"),
        "lowest_take_home_price_type": (lowest_opt or {}).get("price_type"),
        "lowest_take_home_conditions": (lowest_opt or {}).get("conditions"),
        "normalized_price": format(lowest, "f") if lowest is not None else None,
        "red_line": red_line,
        "price_delta": delta,
        "task_status": verify.get("task_status"),
        "match_status": verify.get("match_status"),
        "system_verdict": verify.get("system_verdict"),
        "system_verdict_zh": verify.get("system_verdict_zh"),
        "manual_verdict": None,
        "legacy_page_price_verdict": verify.get("legacy_page_price_verdict"),
        "review_reasons": "; ".join(
            r for r in (verify.get("review_reasons") or []) if r
        )
        if isinstance(verify.get("review_reasons"), list)
        else (verify.get("review_reasons") or verify.get("failure_reason") or ""),
        "checked_at": verify.get("ended_at_utc") or verify.get("started_at_utc"),
        "candidate_run_dir": verify.get("verify_dir")
        or ((verify.get("capture") or {}).get("out_dir")),
        "evidence_paths": ";".join(evidence),
        "packaging_match": verify.get("packaging_match"),
        "box_comparable": verify.get("box_comparable"),
    }
    return take_home.apply_low_price_signal_fields(row)


def paused_placeholder_row(
    *,
    business_sku: str,
    red_line: str,
    candidate: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    return take_home.apply_low_price_signal_fields(
        {
            "business_sku": business_sku,
            "jd_product_id": candidate.get("product_id"),
            "search_rank": candidate.get("rank"),
            "title": candidate.get("title"),
            "url": candidate.get("normalized_url") or candidate.get("observed_url"),
            "shop": candidate.get("shop_name"),
            "ad_marked": candidate.get("ad_marked"),
            "observed_model": "",
            "observed_spec": "",
            "primary_display_price": None,
            "take_home_status": "PAUSED_SESSION",
            "lowest_observed_take_home_price": None,
            "lowest_take_home_option_id": None,
            "lowest_take_home_price_type": None,
            "lowest_take_home_conditions": None,
            "normalized_price": None,
            "red_line": red_line,
            "price_delta": None,
            "task_status": SESSION_PAUSE_TASK,
            "match_status": "PAUSED_BEFORE_VISIT",
            "system_verdict": SESSION_PAUSE_TASK,
            "system_verdict_zh": "会话暂停，候选未访问（非普通采集失败）",
            "manual_verdict": None,
            "legacy_page_price_verdict": None,
            "review_reasons": reason,
            "checked_at": utc_iso(),
            "candidate_run_dir": "",
            "evidence_paths": "",
            "packaging_match": None,
            "box_comparable": None,
        }
    )


def not_verified_budget_row(
    *,
    business_sku: str,
    red_line: str,
    candidate: dict[str, Any],
    max_verify: int,
) -> dict[str, Any]:
    """Candidate kept from search but not detail-verified due to max-verify budget."""
    return take_home.apply_low_price_signal_fields(
        {
            "business_sku": business_sku,
            "jd_product_id": candidate.get("product_id"),
            "search_rank": candidate.get("rank"),
            "title": candidate.get("title"),
            "url": candidate.get("normalized_url") or candidate.get("observed_url"),
            "shop": candidate.get("shop_name"),
            "ad_marked": candidate.get("ad_marked"),
            "observed_model": "",
            "observed_spec": "",
            "primary_display_price": None,
            "take_home_status": STATUS_NOT_VERIFIED_BUDGET,
            "lowest_observed_take_home_price": None,
            "lowest_take_home_option_id": None,
            "lowest_take_home_price_type": None,
            "lowest_take_home_conditions": None,
            "normalized_price": None,
            "red_line": red_line,
            "price_delta": None,
            "task_status": STATUS_NOT_VERIFIED_BUDGET,
            "match_status": STATUS_NOT_VERIFIED_BUDGET,
            "system_verdict": STATUS_NOT_VERIFIED_BUDGET,
            "system_verdict_zh": (
                f"搜索已收录，但未核验（详情访问预算 max-verify={max_verify}）"
            ),
            "manual_verdict": None,
            "legacy_page_price_verdict": None,
            "review_reasons": (
                "超出详情核验预算，未打开详情；"
                "不得当作失败、已核验或无低价；不得继承其他候选价格"
            ),
            "checked_at": utc_iso(),
            "candidate_run_dir": "",
            "evidence_paths": "",
            "packaging_match": None,
            "box_comparable": None,
            "first_seen_load_batch": candidate.get("first_seen_load_batch"),
        }
    )


def resolve_job_budgets(
    *,
    limit: int | None,
    max_candidates: int | None,
    max_pages: int | None,
    search_timeout_seconds: int | None,
    max_verify: int | None,
    limit_explicit: bool,
    max_candidates_explicit: bool,
    max_verify_explicit: bool,
) -> dict[str, Any]:
    search_budget = p3a.resolve_search_budget(
        limit=limit,
        max_candidates=max_candidates,
        max_pages=max_pages,
        search_timeout_seconds=search_timeout_seconds,
        limit_explicit=limit_explicit,
        max_candidates_explicit=max_candidates_explicit,
        max_pages_explicit=max_pages is not None,
        search_timeout_explicit=search_timeout_seconds is not None,
    )
    # Legacy --limit alone meant both search and verify caps.
    if limit_explicit and not max_candidates_explicit and not max_verify_explicit:
        mv = int(limit)  # type: ignore[arg-type]
    elif max_verify_explicit:
        mv = int(max_verify)  # type: ignore[arg-type]
    else:
        mv = DEFAULT_MAX_VERIFY

    if (
        limit_explicit
        and max_verify_explicit
        and not max_candidates_explicit
        and int(limit) != mv  # type: ignore[arg-type]
    ):
        raise ValueError(
            f"旧 --limit={limit} 同时表示搜索与核验上限，与 --max-verify={mv} 冲突"
        )
    if mv < 1 or mv > MAX_VERIFY_CAP:
        raise ValueError(f"max-verify 必须是 1..{MAX_VERIFY_CAP}")

    return {
        **search_budget,
        "max_verify": mv,
    }


def fallback_no_candidate_row(
    *,
    business_sku: str,
    red_line: str,
    reason: str,
    search_status: str | None,
) -> dict[str, Any]:
    return take_home.apply_low_price_signal_fields(
        {
            "business_sku": business_sku,
            "jd_product_id": "",
            "search_rank": None,
            "title": "",
            "url": "",
            "shop": "",
            "ad_marked": None,
            "observed_model": "",
            "observed_spec": "",
            "primary_display_price": None,
            "take_home_status": "NO_CANDIDATES",
            "lowest_observed_take_home_price": None,
            "lowest_take_home_option_id": None,
            "lowest_take_home_price_type": None,
            "lowest_take_home_conditions": None,
            "normalized_price": None,
            "red_line": red_line,
            "price_delta": None,
            "task_status": "NO_CANDIDATES",
            "match_status": search_status or "NO_CANDIDATES",
            "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
            "system_verdict_zh": "本次搜索无可用候选",
            "manual_verdict": None,
            "legacy_page_price_verdict": None,
            "review_reasons": reason,
            "checked_at": utc_iso(),
            "candidate_run_dir": "",
            "evidence_paths": "",
            "packaging_match": None,
            "box_comparable": None,
        }
    )


def build_sku_summary(
    *,
    business_sku: str,
    product_rows: list[dict[str, Any]],
    price_options: list[dict[str, Any]],
    search_meta: dict[str, Any],
    query: str,
    query_basis: str,
    limit: int,
    max_verify: int | None = None,
) -> dict[str, Any]:
    # Exclude synthetic no-candidate placeholder from “candidate” counts when jd empty
    real_rows = [r for r in product_rows if r.get("jd_product_id")]
    placeholder = [r for r in product_rows if not r.get("jd_product_id")]

    candidates_found = int(
        search_meta.get("unique_candidates_collected")
        or search_meta.get("candidate_count")
        or len(real_rows)
    )
    # Visited/processed = entered verify (incl. mismatch skip / OCR fail);
    # excludes pause-before-visit and budget-not-verified.
    paused_unvisited = sum(
        1
        for r in real_rows
        if r.get("task_status") == SESSION_PAUSE_TASK
        and r.get("match_status") == "PAUSED_BEFORE_VISIT"
    )
    not_verified_budget = sum(
        1
        for r in real_rows
        if r.get("task_status") == STATUS_NOT_VERIFIED_BUDGET
        or r.get("match_status") == STATUS_NOT_VERIFIED_BUDGET
    )
    visited = len(real_rows) - paused_unvisited - not_verified_budget

    model_skip = sum(
        1
        for r in real_rows
        if r.get("system_verdict") == "MODEL_MISMATCH_SKIPPED"
        or r.get("match_status") == "MODEL_MISMATCH_SKIPPED"
    )
    comparable_products = sum(
        1
        for r in real_rows
        if r.get("lowest_observed_take_home_price") not in (None, "")
    )
    possible_break_products = sum(
        1 for r in real_rows if r.get("system_verdict") == take_home.SYSTEM_POSSIBLE_BREAK
    )
    review_products = sum(
        1
        for r in real_rows
        if r.get("system_verdict") == take_home.SYSTEM_REVIEW_REQUIRED
        or r.get("take_home_status") == "TAKE_HOME_PENDING"
    )
    failed = sum(1 for r in real_rows if r.get("task_status") == "FAILED")
    completed = sum(1 for r in real_rows if r.get("task_status") == "COMPLETED")
    # detail_pages_opened: actual detail visits (exclude title-gate skip & budget-skip)
    detail_pages_opened = max(0, visited - model_skip)
    primary_low_signal = sum(
        1 for r in real_rows if r.get("low_price_signal") is True
    )
    valid_options = sum(1 for o in price_options if o.get("comparable_for_min"))
    mv = max_verify if max_verify is not None else search_meta.get("max_verify")

    # Job-level lowest from comparable options only
    best = None
    best_opt = None
    for o in price_options:
        if not o.get("comparable_for_min"):
            continue
        amt = to_decimal(o.get("normalized_price"))
        if amt is None:
            continue
        if best is None or amt < best:
            best = amt
            best_opt = o

    # If all products are review / no comparable → lowest must be null
    if comparable_products == 0:
        best = None
        best_opt = None

    extract = search_meta.get("extract_stats") or {}
    scroll = search_meta.get("scroll") or {}
    coverage = {
        "scope_statement": (
            "本次搜索范围内（首屏有限滚动 + 去重候选上限）；"
            "不是全京东、不是全部链接、不是全平台最低价"
        ),
        "query": query,
        "query_generation_basis": query_basis,
        "limit": limit,
        "max_candidates": search_meta.get("max_candidates") or limit,
        "max_pages": search_meta.get("max_pages"),
        "search_timeout_seconds": search_meta.get("search_timeout_seconds"),
        "max_verify": mv,
        "raw_card_count": search_meta.get("raw_cards_observed")
        if search_meta.get("raw_cards_observed") is not None
        else (
            extract.get("data_sku_card_count")
            if extract.get("data_sku_card_count") is not None
            else extract.get("anchor_count_raw")
        ),
        "deduped_candidate_count": search_meta.get("unique_candidates_collected")
        or search_meta.get("candidate_count"),
        "scroll_count": search_meta.get("scroll_count")
        if search_meta.get("scroll_count") is not None
        else scroll.get("scroll_count"),
        "pages_confirmed": search_meta.get("pages_confirmed"),
        "load_batches": search_meta.get("load_batches") or [],
        "elapsed_seconds": search_meta.get("elapsed_seconds"),
        "pagination_mechanism": search_meta.get("pagination_mechanism"),
        "coverage_stop_reason": search_meta.get("coverage_stop_reason")
        or search_meta.get("stop_reason")
        or search_meta.get("status_reason"),
        "related_stop_reasons": search_meta.get("related_stop_reasons") or [],
        "search_run_status": search_meta.get("run_status")
        or search_meta.get("search_status"),
        "pages_visited": search_meta.get("pages_confirmed"),
        "coverage_note": search_meta.get("coverage_note")
        or (
            "本次搜索范围内有限页/加载批次收集；不是全京东、不是全平台最低价"
        ),
        "hit_limit_means": "达到候选上限属于覆盖限制，不等于全部商品",
        "scope_line": (
            f"本次搜索范围内收集{candidates_found}个候选，"
            f"实际访问{detail_pages_opened}个，"
            f"{not_verified_budget}个尚未核验。"
        ),
    }

    return {
        "business_sku": business_sku,
        "rules_version": RULES_VERSION,
        "candidates_found": candidates_found,
        "candidates_processed": visited,
        "candidates_visited": visited,  # alias of candidates_processed
        "detail_pages_opened": detail_pages_opened,
        "model_mismatch_skipped": model_skip,
        "not_verified_budget": not_verified_budget,
        "completed": completed,
        "failed": failed,
        "paused_unvisited": paused_unvisited,
        "products_with_comparable_take_home": comparable_products,
        "products_possible_break": possible_break_products,
        "products_primary_low_price_signal": primary_low_signal,
        "products_review_required": review_products,
        "products_failed": failed,
        "candidates_paused_unvisited": paused_unvisited,
        "valid_comparable_price_option_count": valid_options,
        "price_option_count_total": len(price_options),
        "max_verify": mv,
        "stat_definitions": {
            "candidates_found": "本次搜索去重后的候选商品数（非价格方案数）",
            "candidates_processed": (
                "已进入核验流程的候选（含型号跳过/失败；"
                "不含暂停未访问、不含预算未核验）"
            ),
            "candidates_visited": "同 candidates_processed（兼容旧字段）",
            "detail_pages_opened": (
                "实际打开详情页数＝processed−model_mismatch_skipped；"
                "型号不符跳过不消耗详情访问预算"
            ),
            "model_mismatch_skipped": "标题门控明确型号不符而跳过的商品数（未开详情）",
            "not_verified_budget": (
                "因 max-verify 预算未打开详情的候选数（≠失败、≠已核验、≠无低价）"
            ),
            "completed": "task_status=COMPLETED 的商品数",
            "failed": "task_status=FAILED 的商品数（普通失败）",
            "paused_unvisited": "会话暂停后未打开的候选数（非普通失败）",
            "products_with_comparable_take_home": "至少有一个有效可比到手价方案的商品数",
            "products_possible_break": "商品级 system_verdict=POSSIBLE_BREAK（疑似破价）",
            "products_primary_low_price_signal": (
                "主展示低价线索商品数（≠已确认破价；与疑似破价分开统计）"
            ),
            "products_review_required": "商品级待核验（含无到手价证据）",
            "products_failed": "同 failed（兼容）",
            "candidates_paused_unvisited": "同 paused_unvisited（兼容）",
            "valid_comparable_price_option_count": "可比到手价方案条数（≠商品数）",
        },
        "search_coverage": coverage,
        "verify_coverage": {
            "max_verify": mv,
            "detail_pages_opened": detail_pages_opened,
            "model_mismatch_skipped": model_skip,
            "not_verified_budget": not_verified_budget,
            "paused_unvisited": paused_unvisited,
            "completed": completed,
            "failed": failed,
            "scope_line": coverage["scope_line"],
        },
        "lowest_comparable_take_home": (
            {
                "normalized_price": format(best, "f"),
                "jd_product_id": best_opt.get("jd_product_id"),
                "option_id": best_opt.get("option_id"),
                "price_type": best_opt.get("price_type"),
                "conditions": best_opt.get("conditions"),
                "url": next(
                    (
                        r.get("url")
                        for r in real_rows
                        if str(r.get("jd_product_id"))
                        == str(best_opt.get("jd_product_id"))
                    ),
                    None,
                ),
                "note": "仅来自本次已核验且有效的可比到手价方案；不回退主展示价；含优惠条件",
            }
            if best is not None and best_opt is not None
            else None
        ),
        "placeholder_rows": len(placeholder),
        "notes": [
            coverage["scope_line"],
            "最低价为空表示本次无有效可比到手价方案（例如全部待核验）",
            "legacy/主展示价不参与到手价结论与最低价",
            "主展示低价线索 ≠ 疑似破价 ≠ 已确认破价",
            "NOT_VERIFIED_BUDGET 不得当作失败或无低价",
            "manual_verdict 系统不代填",
            "不得称全部京东链接或全平台最低价",
        ],
    }


def render_report_html(
    *,
    job_id: str,
    summary: dict[str, Any],
    product_rows: list[dict[str, Any]],
    price_options: list[dict[str, Any]],
) -> str:
    esc = html.escape
    cov = summary.get("search_coverage") or {}
    lowest = summary.get("lowest_comparable_take_home")

    def rows_html(rows: list[dict[str, Any]], keys: list[str]) -> str:
        if not rows:
            return "<tr><td colspan='99'>（无）</td></tr>"
        head = "".join(f"<th>{esc(k)}</th>" for k in keys)
        body = []
        for r in rows:
            body.append(
                "<tr>"
                + "".join(f"<td>{esc(str(r.get(k) if r.get(k) is not None else ''))}</td>" for k in keys)
                + "</tr>"
            )
        return f"<tr>{head}</tr>" + "".join(body)

    lowest_block = "<p>本次无有效可比到手价方案（最低价为空；不回退主展示价）。</p>"
    if lowest:
        lowest_block = f"""
        <ul>
          <li>换算价：{esc(str(lowest.get('normalized_price')))}</li>
          <li>商品ID：{esc(str(lowest.get('jd_product_id')))}</li>
          <li>方案ID：{esc(str(lowest.get('option_id')))}</li>
          <li>类型：{esc(str(lowest.get('price_type')))}</li>
          <li>条件：{esc(str(lowest.get('conditions')))}</li>
          <li>链接：{esc(str(lowest.get('url') or ''))}</li>
        </ul>"""

    # Prefer suspected break + primary-low signals at top of product table
    ordered_rows = sorted(
        product_rows,
        key=lambda r: (
            0 if r.get("system_verdict") == take_home.SYSTEM_POSSIBLE_BREAK else 1,
            0 if r.get("low_price_signal") else 1,
            r.get("search_rank") if r.get("search_rank") is not None else 999,
        ),
    )
    prod_keys = [
        "search_rank",
        "jd_product_id",
        "title",
        "task_status",
        "system_verdict",
        "system_verdict_zh",
        "primary_display_price",
        "low_price_signal",
        "signal_amount",
        "signal_conditions",
        "signal_review_reasons",
        "lowest_observed_take_home_price",
        "lowest_take_home_conditions",
        "manual_verdict",
    ]
    opt_keys = [
        "option_id",
        "jd_product_id",
        "price_type",
        "amount",
        "normalized_price",
        "conditions",
        "is_estimate",
        "comparable_for_min",
        "system_verdict_zh",
    ]

    cov_note = str((summary.get("search_coverage") or {}).get("coverage_note") or "")
    reuse_mode = bool(
        summary.get("reuse_mode")
        or (summary.get("search_coverage") or {}).get("reuse_mode")
        or "复用" in cov_note
        or "reuse" in cov_note.lower()
    )
    evidence_note_html = (
        f"<p class='warn'>证据来源说明：{esc(cov_note)}</p>" if cov_note else ""
    )
    reuse_html = (
        "<p class='warn'>复用模式：搜索证据非本轮实时翻页采集；"
        "请分别核对搜索证据时间、来源任务、复用时间及详情核验时间；"
        "详见 search/run_metadata.json 的 reuse 字段。"
        "历史复用不得在现网搜索受限后自动兜底。</p>"
        if reuse_mode
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>PriceRadar SKU Job {esc(job_id)}</title>
<style>
body {{ font-family: "Segoe UI", "Microsoft YaHei", sans-serif; margin: 24px; color: #222; }}
h1,h2 {{ color: #111; }}
.card {{ border: 1px solid #ddd; padding: 12px 16px; margin: 12px 0; border-radius: 6px; }}
.muted {{ color: #666; font-size: 0.92em; }}
table {{ border-collapse: collapse; width: 100%; font-size: 0.85em; }}
th, td {{ border: 1px solid #ccc; padding: 4px 6px; vertical-align: top; }}
th {{ background: #f5f5f5; }}
.warn {{ color: #a40; }}
</style>
</head>
<body>
<h1>PriceRadar 单 SKU 搜索核验报告</h1>
<p class="muted">job_id={esc(job_id)} · rules={esc(RULES_VERSION)} ·
<strong>本次搜索范围内</strong>（非全京东 / 非全平台最低价）</p>
{evidence_note_html}
{reuse_html}

<div class="card">
  <h2>覆盖说明</h2>
  <p>{esc(str(cov.get('scope_statement')))}</p>
  <ul>
    <li>搜索词：{esc(str(cov.get('query')))}（{esc(str(cov.get('query_generation_basis')))}）</li>
    <li>limit：{esc(str(cov.get('limit')))} · 去重候选：{esc(str(cov.get('deduped_candidate_count')))}</li>
    <li>原始卡片数：{esc(str(cov.get('raw_card_count')))} · 滚动次数：{esc(str(cov.get('scroll_count')))}</li>
    <li>停止原因：{esc(str(cov.get('coverage_stop_reason')))} · 搜索状态：{esc(str(cov.get('search_run_status')))}</li>
    <li>pages_visited：{esc(str(cov.get('pages_visited')))}（未实测分页则保持 UNKNOWN）</li>
  </ul>
</div>

<div class="card">
  <h2>SKU 汇总 · {esc(str(summary.get('business_sku')))}</h2>
  <p><strong>{esc(str((summary.get('search_coverage') or {}).get('scope_line') or (summary.get('verify_coverage') or {}).get('scope_line') or ''))}</strong></p>
  <ul>
    <li>候选数（搜索去重）：{esc(str(summary.get('candidates_found')))}</li>
    <li>已处理（processed）：{esc(str(summary.get('candidates_processed')))}</li>
    <li>详情页打开：{esc(str(summary.get('detail_pages_opened')))}</li>
    <li>型号不符跳过：{esc(str(summary.get('model_mismatch_skipped')))}</li>
    <li>预算未核验：{esc(str(summary.get('not_verified_budget')))}</li>
    <li>completed / failed / paused_unvisited：
        {esc(str(summary.get('completed')))} /
        {esc(str(summary.get('failed')))} /
        {esc(str(summary.get('paused_unvisited')))}</li>
    <li>可比到手价商品数：{esc(str(summary.get('products_with_comparable_take_home')))}</li>
    <li>疑似破价商品数：{esc(str(summary.get('products_possible_break')))}</li>
    <li>主展示低价线索数：{esc(str(summary.get('products_primary_low_price_signal')))}
        （与疑似破价分开，不冒充已确认破价）</li>
    <li>待核验商品数：{esc(str(summary.get('products_review_required')))}</li>
    <li>有效价格方案数：{esc(str(summary.get('valid_comparable_price_option_count')))}
        （总方案 {esc(str(summary.get('price_option_count_total')))}，≠商品数）</li>
    <li>搜索停止原因：{esc(str(cov.get('coverage_stop_reason')))}
        · pages_confirmed={esc(str(cov.get('pages_confirmed')))}
        · mechanism={esc(str(cov.get('pagination_mechanism')))}</li>
  </ul>
  <h3>范围内最低可比到手价</h3>
  {lowest_block}
  <p class="muted">POSSIBLE_BREAK = 疑似破价，待人工确认；主展示低价线索 ≠ 到手价破价；manual_verdict 留空。</p>
</div>

<div class="card">
  <h2>商品明细（优先疑似破价与低价线索）</h2>
  <table>{rows_html(ordered_rows, prod_keys)}</table>
</div>

<div class="card">
  <h2>价格方案明细（一行一个方案）</h2>
  <table>{rows_html(price_options, opt_keys)}</table>
</div>
</body>
</html>
"""


def run_sku_job(
    *,
    business_config: Path,
    query: str | None = None,
    limit: int | None = None,
    max_candidates: int | None = None,
    max_pages: int | None = None,
    search_timeout_seconds: int | None = None,
    max_verify: int | None = None,
    limit_explicit: bool = False,
    max_candidates_explicit: bool = False,
    max_verify_explicit: bool = False,
    search_fn: Callable[..., dict[str, Any]] | None = None,
    verify_fn: Callable[..., dict[str, Any]] | None = None,
    job_dir: Path | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    started = utc_now()
    cfg, errs = adapt.load_business_baseline(business_config)
    if errs or cfg is None:
        raise ValueError("; ".join(errs or ["config invalid"]))

    # Compat: bare limit= int from old callers without explicit flags
    if limit is not None and not limit_explicit and not max_candidates_explicit:
        limit_explicit = True

    budgets = resolve_job_budgets(
        limit=limit,
        max_candidates=max_candidates,
        max_pages=max_pages,
        search_timeout_seconds=search_timeout_seconds,
        max_verify=max_verify,
        limit_explicit=limit_explicit,
        max_candidates_explicit=max_candidates_explicit,
        max_verify_explicit=max_verify_explicit,
    )
    max_cand = int(budgets["max_candidates"])
    max_pg = int(budgets["max_pages"])
    timeout_s = int(budgets["search_timeout_seconds"])
    max_ver = int(budgets["max_verify"])
    limit = max_cand

    if query and query.strip():
        use_query = query.strip()
        query_basis = "CLI --query（显式传入）"
    else:
        use_query, query_basis = default_query_from_baseline(cfg)

    business_sku = cfg["business_sku"]
    red_line = cfg["red_line"]
    if job_dir is not None:
        job_dir = Path(job_dir).resolve()
        job_dir.mkdir(parents=True, exist_ok=True)
        job_id = job_id or job_dir.name
    else:
        job_id, job_dir = make_job_dir(business_sku)
    search_dir = job_dir / "search"
    cand_verify_root = job_dir / "candidates"
    search_dir.mkdir(parents=True, exist_ok=True)
    cand_verify_root.mkdir(parents=True, exist_ok=True)

    snapshot = {k: v for k, v in cfg.items() if not str(k).startswith("_")}
    write_json(job_dir / "business_baseline_snapshot.json", snapshot)

    progress: dict[str, Any] = {
        "job_id": job_id,
        "phase": "P5-B",
        "rules_version": RULES_VERSION,
        "business_sku": business_sku,
        "query": use_query,
        "query_generation_basis": query_basis,
        "limit": limit,
        "max_candidates": max_cand,
        "max_pages": max_pg,
        "search_timeout_seconds": timeout_s,
        "max_verify": max_ver,
        "started_at_utc": utc_iso(started),
        "stage": "SEARCH",
        "completed_product_ids": [],
        "session_paused": False,
        "pause_reason": None,
        "detail_pages_opened": 0,
        "historical_anchors_not_used_for_search_or_identity": cfg.get(
            "historical_anchors"
        ),
    }
    save_progress(job_dir, progress)

    search_impl = search_fn or p3a.run_search_collect
    verify_impl = verify_fn or p5a.verify_one

    print(f"=== PriceRadar P5-B job {job_id} ===")
    print(
        f"business_sku={business_sku} query={use_query!r} "
        f"max_candidates={max_cand} max_pages={max_pg} "
        f"search_timeout={timeout_s}s max_verify={max_ver}"
    )
    print(f"job_dir={job_dir}")

    search_result = search_impl(
        use_query,
        limit=max_cand,
        max_candidates=max_cand,
        max_pages=max_pg,
        search_timeout_seconds=timeout_s,
        auto_close=True,
        out_dir=search_dir,
        quiet=False,
        limit_explicit=False,
        max_candidates_explicit=True,
    )
    # Ensure we only consume THIS search result
    candidates = list(search_result.get("candidates") or [])
    search_meta = dict(search_result.get("metadata") or {})
    search_meta.setdefault("candidate_count", len(candidates))
    search_meta.setdefault("unique_candidates_collected", len(candidates))
    search_meta.setdefault("run_status", search_result.get("run_status"))
    search_meta.setdefault(
        "coverage_stop_reason", search_result.get("coverage_stop_reason")
    )
    search_meta["max_candidates"] = max_cand
    search_meta["max_pages"] = max_pg
    search_meta["search_timeout_seconds"] = timeout_s
    search_meta["max_verify"] = max_ver
    # Search browser must release profile before detail verify (run_search_collect closes).
    progress["search_browser_released"] = True
    progress["search_close_ok"] = search_result.get("close_ok")
    candidates_path = Path(
        search_result.get("candidates_path") or (search_dir / "candidates.json")
    )
    # Re-read from written file to guarantee exact handoff (no historical anchors)
    if candidates_path.is_file():
        try:
            loaded = json.loads(candidates_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, list):
                candidates = loaded
        except json.JSONDecodeError:
            pass

    progress["stage"] = "VERIFY"
    progress["search_run_id"] = search_result.get("run_id")
    progress["search_status"] = search_result.get("run_status")
    progress["search_dir"] = str(search_dir)
    progress["candidates_path"] = str(candidates_path)
    save_progress(job_dir, progress)

    product_rows: list[dict[str, Any]] = []
    price_options: list[dict[str, Any]] = []
    verify_results: list[dict[str, Any]] = []

    search_status = str(search_result.get("run_status") or "")
    # Session block at search stage: do not invent candidates; may still have list
    if search_status in SEARCH_BLOCK_STATUSES and not candidates:
        product_rows.append(
            fallback_no_candidate_row(
                business_sku=business_sku,
                red_line=red_line,
                reason=f"搜索状态={search_status}: {search_result.get('status_reason')}",
                search_status=search_status,
            )
        )
        progress["session_paused"] = True
        progress["pause_reason"] = search_status
        save_progress(job_dir, progress)
    elif not candidates:
        product_rows.append(
            fallback_no_candidate_row(
                business_sku=business_sku,
                red_line=red_line,
                reason=f"无候选: {search_result.get('status_reason') or search_status}",
                search_status=search_status,
            )
        )
    else:
        session_paused = False
        pause_reason = None
        detail_opens = 0
        # Search-stage login/captcha/restriction: keep candidates, pause all detail verify.
        if search_status in SEARCH_BLOCK_STATUSES:
            session_paused = True
            pause_reason = search_status
            progress["session_paused"] = True
            progress["pause_reason"] = pause_reason
            save_progress(job_dir, progress)

        expected_model = str(cfg.get("expected_model") or "")

        for cand in candidates:
            pid = str(cand.get("product_id") or "").strip()
            if not pid:
                continue
            rank = cand.get("rank")
            if session_paused:
                row = paused_placeholder_row(
                    business_sku=business_sku,
                    red_line=red_line,
                    candidate=cand,
                    reason=f"会话已暂停({pause_reason})，未访问本候选",
                )
                product_rows.append(row)
                progress["completed_product_ids"].append(pid)
                save_progress(job_dir, progress)
                write_json(job_dir / "product_rows.json", product_rows)
                continue

            # Offline title gate: model mismatch does not consume detail budget.
            title = str(cand.get("title") or "")
            gate = adapt.title_model_gate(title, expected_model=expected_model)
            needs_detail = not bool(gate.get("skip_browse"))

            if needs_detail and detail_opens >= max_ver:
                row = not_verified_budget_row(
                    business_sku=business_sku,
                    red_line=red_line,
                    candidate=cand,
                    max_verify=max_ver,
                )
                product_rows.append(row)
                progress["completed_product_ids"].append(pid)
                save_progress(job_dir, progress)
                write_json(job_dir / "product_rows.json", product_rows)
                continue

            verify_dir = cand_verify_root / f"rank{rank}_{pid}"
            print(
                f"--- verify rank={rank} id={pid} "
                f"detail_budget={detail_opens}/{max_ver} "
                f"needs_detail={needs_detail} ---"
            )
            try:
                vres = verify_impl(
                    business_config=business_config,
                    candidates_path=candidates_path,
                    product_id=pid,
                    out_dir=verify_dir,
                )
            except Exception as exc:  # noqa: BLE001
                vres = {
                    "task_status": "FAILED",
                    "match_status": "VERIFY_EXCEPTION",
                    "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                    "system_verdict_zh": "核验异常",
                    "manual_verdict": None,
                    "candidate_product_id": pid,
                    "price_clues": [],
                    "review_reasons": [f"{type(exc).__name__}: {exc}"],
                    "failure_reason": f"{type(exc).__name__}: {exc}",
                    "ended_at_utc": utc_iso(),
                    "verify_dir": str(verify_dir),
                }

            # Count only actual detail opens (model skip does not consume budget).
            match = str(vres.get("match_status") or "")
            if needs_detail and match != "MODEL_MISMATCH_SKIPPED":
                detail_opens += 1
                progress["detail_pages_opened"] = detail_opens

            verify_results.append(vres)
            opts = build_price_options(
                business_sku=business_sku,
                product_id=pid,
                search_rank=rank,
                clues=list(vres.get("price_clues") or []),
            )
            price_options.extend(opts)
            row = product_row_from_verify(
                business_sku=business_sku,
                red_line=red_line,
                candidate=cand,
                verify=vres,
                options=opts,
            )
            product_rows.append(row)

            progress["completed_product_ids"].append(pid)
            progress["last_verify_status"] = vres.get("task_status")
            save_progress(job_dir, progress)
            write_json(job_dir / "product_rows.json", product_rows)
            write_json(job_dir / "price_options.json", price_options)

            # Ordinary candidate failure continues; session-level pause stops rest.
            task = str(vres.get("task_status") or "")
            if task == SESSION_PAUSE_TASK or match in SESSION_PAUSE_MATCH:
                session_paused = True
                pause_reason = match or task
                progress["session_paused"] = True
                progress["pause_reason"] = pause_reason
                save_progress(job_dir, progress)

    def _finalize_outputs(*, stage: str = "DONE") -> dict[str, Any]:
        summary_local = build_sku_summary(
            business_sku=business_sku,
            product_rows=product_rows,
            price_options=price_options,
            search_meta=search_meta,
            query=use_query,
            query_basis=query_basis,
            limit=limit,
            max_verify=max_ver,
        )
        summary_local["job_id"] = job_id
        summary_local["started_at_utc"] = utc_iso(started)
        summary_local["ended_at_utc"] = utc_iso()
        summary_local["job_dir"] = str(job_dir)
        summary_local["search_dir"] = str(search_dir)
        summary_local["partial"] = stage != "DONE"
        write_json(job_dir / "product_rows.json", product_rows)
        write_csv(job_dir / "product_rows.csv", product_rows, PRODUCT_ROW_FIELDS)
        write_json(job_dir / "price_options.json", price_options)
        write_csv(job_dir / "price_options.csv", price_options, PRICE_OPTION_FIELDS)
        write_json(job_dir / "sku_summary.json", summary_local)
        report_html = render_report_html(
            job_id=job_id,
            summary=summary_local,
            product_rows=product_rows,
            price_options=price_options,
        )
        (job_dir / "report.html").write_text(report_html, encoding="utf-8")
        progress["stage"] = stage
        progress["ended_at_utc"] = summary_local["ended_at_utc"]
        progress["summary_path"] = str(job_dir / "sku_summary.json")
        save_progress(job_dir, progress)
        return summary_local

    try:
        summary = _finalize_outputs(stage="DONE")
    except Exception as exc:  # noqa: BLE001
        progress["fatal_error"] = f"{type(exc).__name__}: {exc}"
        save_progress(job_dir, progress)
        summary = _finalize_outputs(stage="PARTIAL_ERROR")
        print(f"job finalize warning: {type(exc).__name__}: {exc}")

    print()
    print(f"job_dir={job_dir}")
    print(
        f"candidates={summary.get('candidates_found')} "
        f"processed={summary.get('candidates_processed')} "
        f"detail_opened={summary.get('detail_pages_opened')} "
        f"model_skip={summary.get('model_mismatch_skipped')} "
        f"budget_skip={summary.get('not_verified_budget')} "
        f"possible_break={summary.get('products_possible_break')} "
        f"primary_low={summary.get('products_primary_low_price_signal')} "
        f"paused={summary.get('candidates_paused_unvisited')}"
    )
    print(f"report={job_dir / 'report.html'}")

    return {
        "job_id": job_id,
        "job_dir": job_dir,
        "summary": summary,
        "product_rows": product_rows,
        "price_options": price_options,
        "search_result": {
            "run_id": search_result.get("run_id"),
            "run_status": search_result.get("run_status"),
            "candidates_path": str(candidates_path),
            "candidate_count": len(candidates),
            "coverage_stop_reason": search_meta.get("coverage_stop_reason"),
        },
    }


def run_offline_self_checks() -> int:
    failures: list[str] = []

    # --- Fake search + verify injections ---
    cfg_path = PROJECT_ROOT / "config" / "products" / "b913a.json"
    cfg, errs = adapt.load_business_baseline(cfg_path)
    if errs or not cfg:
        print("SKIP offline job tests: baseline missing", errs)
        return 1

    def fake_search_ok(query, limit, **kwargs):
        out = Path(kwargs["out_dir"])
        cands = [
            {
                "query": query,
                "rank": 1,
                "title": "清风擦手纸 B913A 20包",
                "product_id": "111",
                "shop_name": "店A",
                "ad_marked": False,
                "normalized_url": "https://item.jd.com/111.html",
                "observed_url": "https://item.jd.com/111.html",
            },
            {
                "query": query,
                "rank": 2,
                "title": "清风 B913AE 16包",
                "product_id": "222",
                "shop_name": "店B",
                "ad_marked": True,
                "normalized_url": "https://item.jd.com/222.html",
                "observed_url": "https://item.jd.com/222.html",
            },
            {
                "query": query,
                "rank": 3,
                "title": "清风 B913A 箱装",
                "product_id": "333",
                "shop_name": "店C",
                "ad_marked": False,
                "normalized_url": "https://item.jd.com/333.html",
                "observed_url": "https://item.jd.com/333.html",
            },
        ][:limit]
        p3a.write_candidates_json(out / "candidates.json", cands)
        meta = {
            "run_status": "SUCCESS",
            "status_reason": "test",
            "coverage_stop_reason": "HIT_CANDIDATE_LIMIT",
            "candidate_count": len(cands),
            "extract_stats": {"data_sku_card_count": 30},
            "scroll": {"scroll_count": 2},
            "pages_visited": "UNKNOWN",
            "query": query,
            "limit": limit,
        }
        p3a.write_metadata(out / "run_metadata.json", meta)
        return {
            "ok": True,
            "exit_code": 0,
            "run_id": out.name,
            "out_dir": out,
            "candidates_path": out / "candidates.json",
            "candidates": cands,
            "run_status": "SUCCESS",
            "status_reason": "test",
            "coverage_stop_reason": "HIT_CANDIDATE_LIMIT",
            "metadata": meta,
        }

    calls: list[str] = []

    def fake_verify(**kwargs):
        pid = kwargs["product_id"]
        calls.append(pid)
        out = Path(kwargs["out_dir"])
        out.mkdir(parents=True, exist_ok=True)
        # Ensure candidate is from THIS file not historical
        cpath = Path(kwargs["candidates_path"])
        data = json.loads(cpath.read_text(encoding="utf-8"))
        ids = {str(x.get("product_id")) for x in data}
        if pid not in ids:
            failures.append(f"verify got id {pid} not in search file")
        if "100005149674" in ids and pid == "100005149674" and "111" in calls:
            pass  # historical must not appear in fake search

        if pid == "222":
            # model mismatch path
            title = next(x["title"] for x in data if str(x["product_id"]) == pid)
            gate = adapt.title_model_gate(title, expected_model=cfg["expected_model"])
            if not gate.get("skip_browse"):
                failures.append("222 should gate skip")
            res = {
                "task_status": "SKIPPED",
                "match_status": "MODEL_MISMATCH_SKIPPED",
                "system_verdict": "MODEL_MISMATCH_SKIPPED",
                "system_verdict_zh": "型号不符已跳过（未打开详情）",
                "manual_verdict": None,
                "candidate_product_id": pid,
                "price_clues": [],
                "review_reasons": [gate.get("reason")],
                "ended_at_utc": utc_iso(),
                "verify_dir": str(out),
                "candidate": {"title": title, "product_id": pid},
            }
            write_json(out / "verify_result.json", res)
            return res

        if pid == "111":
            # OCR-ish failure continue
            res = {
                "task_status": "FAILED",
                "match_status": "OCR_FAILED",
                "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                "system_verdict_zh": "待核验",
                "manual_verdict": None,
                "page_primary_price": "100.00",  # low primary must NOT drive POSSIBLE_BREAK
                "price_clues": [
                    {
                        "price_type": take_home.PRICE_TYPE_PAGE_PRIMARY,
                        "amount": "100.00",
                        "raw_label": "¥",
                        "conditions": "¥100",
                        "normalized_price": None,
                        "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                        "system_verdict_zh": "待核验（非未破价）",
                        "manual_verdict": None,
                        "review_reasons": ["到手价待核验"],
                    }
                ],
                "price_evidence_status": "TAKE_HOME_PENDING_NO_DECLARED_CLUE",
                "take_home_rollup": {"take_home_clue_count": 0, "has_possible_break": False},
                "ended_at_utc": utc_iso(),
                "verify_dir": str(out),
                "candidate": {"title": "清风擦手纸 B913A 20包", "product_id": pid},
                "url_adapt": {
                    "adapted_url": "https://item.m.jd.com/product/111.html",
                    "final_url_sanitized": "https://item.m.jd.com/product/111.html",
                },
            }
            write_json(out / "verify_result.json", res)
            return res

        if pid == "333":
            # session pause
            res = {
                "task_status": SESSION_PAUSE_TASK,
                "match_status": "LOGIN_REQUIRED",
                "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                "system_verdict_zh": "会话受限",
                "manual_verdict": None,
                "price_clues": [],
                "ended_at_utc": utc_iso(),
                "verify_dir": str(out),
                "candidate": {"title": "清风 B913A 箱装", "product_id": pid},
            }
            write_json(out / "verify_result.json", res)
            return res

        failures.append(f"unexpected pid {pid}")
        return {"task_status": "FAILED", "price_clues": [], "verify_dir": str(out)}

    # Extend fake search to 4 candidates to test pause remaining
    def fake_search_4(query, limit, **kwargs):
        base = fake_search_ok(query, min(limit, 4), **kwargs)
        cands = list(base["candidates"])
        if limit >= 4 and len(cands) < 4:
            cands.append(
                {
                    "query": query,
                    "rank": 4,
                    "title": "清风 B913A 剩余",
                    "product_id": "444",
                    "shop_name": "店D",
                    "ad_marked": False,
                    "normalized_url": "https://item.jd.com/444.html",
                    "observed_url": "https://item.jd.com/444.html",
                }
            )
            out = Path(kwargs["out_dir"])
            p3a.write_candidates_json(out / "candidates.json", cands)
            base["candidates"] = cands
            base["metadata"]["candidate_count"] = len(cands)
        return base

    job = run_sku_job(
        business_config=cfg_path,
        query="清风 B913A",
        limit=4,
        search_fn=fake_search_4,
        verify_fn=fake_verify,
    )
    rows = job["product_rows"]
    by_id = {str(r.get("jd_product_id")): r for r in rows}

    if "100005149674" in by_id:
        failures.append("historical product id must not appear from search handoff")
    if by_id.get("222", {}).get("system_verdict") != "MODEL_MISMATCH_SKIPPED":
        failures.append("model mismatch row missing")
    if by_id.get("111", {}).get("system_verdict") != take_home.SYSTEM_REVIEW_REQUIRED:
        failures.append("primary-low without take-home must REVIEW")
    if by_id.get("111", {}).get("system_verdict") == take_home.SYSTEM_POSSIBLE_BREAK:
        failures.append("must not POSSIBLE_BREAK on primary alone")
    if not by_id.get("111", {}).get("low_price_signal"):
        failures.append("111 primary below red should keep low_price_signal")
    if by_id.get("111", {}).get("signal_normalized_price") is not None:
        failures.append("111 without packaging must not forge signal_normalized_price")
    if by_id.get("222", {}).get("low_price_signal"):
        failures.append("model mismatch must not set low_price_signal")
    if by_id.get("333", {}).get("task_status") != SESSION_PAUSE_TASK:
        failures.append("333 should pause session")
    if by_id.get("444", {}).get("task_status") != SESSION_PAUSE_TASK:
        failures.append("444 should be paused unvisited after session pause")
    if by_id.get("444", {}).get("match_status") != "PAUSED_BEFORE_VISIT":
        failures.append("444 must not look like ordinary capture failure")
    if calls != ["111", "222", "333"]:
        failures.append(f"verify call order wrong: {calls}")

    summ = job["summary"]
    if summ.get("candidates_paused_unvisited", 0) < 1:
        failures.append("paused unvisited count")
    if summ.get("paused_unvisited", 0) < 1:
        failures.append("paused_unvisited alias missing")
    if summ.get("candidates_processed") != 3:
        failures.append(
            f"candidates_processed should be 3 (excl paused), got {summ.get('candidates_processed')}"
        )
    if summ.get("detail_pages_opened") != 2:
        failures.append(
            f"detail_pages_opened should be 2 (excl model skip), got {summ.get('detail_pages_opened')}"
        )
    if summ.get("model_mismatch_skipped") != 1:
        failures.append("model_mismatch_skipped should be 1")
    if summ.get("failed") != 1:
        failures.append("failed should be 1 (111 OCR fail)")
    if summ.get("products_primary_low_price_signal", 0) < 1:
        failures.append("should count primary low-price signal separately")
    if summ.get("lowest_comparable_take_home") is not None:
        failures.append("lowest must be null when no comparable take-home")

    # Multi price options + possible break + min price
    def fake_search_one(query, limit, **kwargs):
        out = Path(kwargs["out_dir"])
        cands = [
            {
                "query": query,
                "rank": 1,
                "title": "清风 B913A",
                "product_id": "555",
                "shop_name": "店",
                "ad_marked": False,
                "normalized_url": "https://item.jd.com/555.html",
                "observed_url": "https://item.jd.com/555.html",
            }
        ]
        p3a.write_candidates_json(out / "candidates.json", cands)
        meta = {
            "run_status": "SUCCESS",
            "candidate_count": 1,
            "coverage_stop_reason": "HIT_CANDIDATE_LIMIT",
            "extract_stats": {"data_sku_card_count": 1},
            "scroll": {"scroll_count": 0},
            "pages_visited": "UNKNOWN",
            "limit": limit,
            "query": query,
        }
        p3a.write_metadata(out / "run_metadata.json", meta)
        return {
            "ok": True,
            "exit_code": 0,
            "run_id": out.name,
            "out_dir": out,
            "candidates_path": out / "candidates.json",
            "candidates": cands,
            "run_status": "SUCCESS",
            "coverage_stop_reason": "HIT_CANDIDATE_LIMIT",
            "metadata": meta,
        }

    def fake_verify_multi(**kwargs):
        out = Path(kwargs["out_dir"])
        out.mkdir(parents=True, exist_ok=True)
        clues = [
            {
                "price_type": take_home.PRICE_TYPE_PAGE_PRIMARY,
                "amount": "168.00",
                "raw_label": "¥",
                "conditions": "¥168",
                "normalized_price": None,
                "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                "manual_verdict": None,
                "review_reasons": ["主展示参考"],
            },
            {
                "price_type": take_home.PRICE_TYPE_DECLARED_CONDITIONAL,
                "amount": "120.00",
                "raw_label": "会员价",
                "conditions": "会员价120 需PLUS",
                "minimum_quantity": 1,
                "eligibility_status": "CONDITIONAL_UNVERIFIED",
                "normalized_price": "120.00",
                "red_line": "127.00",
                "price_delta": "-7.00",
                "system_verdict": take_home.SYSTEM_POSSIBLE_BREAK,
                "system_verdict_zh": take_home.SYSTEM_VERDICT_ZH[
                    take_home.SYSTEM_POSSIBLE_BREAK
                ],
                "manual_verdict": None,
                "review_reasons": ["资格未验证"],
            },
            {
                "price_type": take_home.PRICE_TYPE_ESTIMATED,
                "amount": "110.00",
                "raw_label": "预估到手",
                "conditions": "预估到手110",
                "minimum_quantity": 1,
                "eligibility_status": "ESTIMATE_ONLY",
                "normalized_price": "110.00",
                "red_line": "127.00",
                "price_delta": "-17.00",
                "system_verdict": take_home.SYSTEM_POSSIBLE_BREAK,
                "system_verdict_zh": take_home.SYSTEM_VERDICT_ZH[
                    take_home.SYSTEM_POSSIBLE_BREAK
                ],
                "manual_verdict": None,
                "review_reasons": ["预估属性"],
            },
        ]
        return {
            "task_status": "COMPLETED",
            "match_status": "PACKAGING_PASSED",
            "system_verdict": take_home.SYSTEM_POSSIBLE_BREAK,
            "system_verdict_zh": take_home.SYSTEM_VERDICT_ZH[
                take_home.SYSTEM_POSSIBLE_BREAK
            ],
            "manual_verdict": None,
            "page_primary_price": "168.00",
            "legacy_page_price_verdict": "NOT_BREAK",
            "price_clues": clues,
            "price_evidence_status": "TAKE_HOME_CLUES_PRESENT",
            "take_home_rollup": {
                "take_home_clue_count": 2,
                "has_possible_break": True,
            },
            "packaging_match": "PASSED",
            "box_comparable": True,
            "ended_at_utc": utc_iso(),
            "verify_dir": str(out),
            "candidate": {"title": "清风 B913A", "product_id": "555"},
            "url_adapt": {
                "adapted_url": "https://item.m.jd.com/product/555.html",
                "final_url_sanitized": "https://item.m.jd.com/product/555.html",
            },
        }

    job2 = run_sku_job(
        business_config=cfg_path,
        query="清风 B913A",
        limit=1,
        search_fn=fake_search_one,
        verify_fn=fake_verify_multi,
    )
    opts = job2["price_options"]
    if len(opts) != 3:
        failures.append(f"expected 3 price options, got {len(opts)}")
    if not any(o.get("is_estimate") for o in opts):
        failures.append("estimate attribute missing")
    if not any(o.get("price_type") == take_home.PRICE_TYPE_DECLARED_CONDITIONAL for o in opts):
        failures.append("member conditional missing")
    lowest = job2["summary"].get("lowest_comparable_take_home")
    if not lowest or lowest.get("normalized_price") != "110.00":
        failures.append(f"lowest should be 110 estimate, got {lowest}")
    if "预估" not in str(lowest.get("conditions") or "") and lowest.get("price_type") != take_home.PRICE_TYPE_ESTIMATED:
        failures.append("lowest must carry estimate type/conditions")
    row555 = job2["product_rows"][0]
    if row555.get("manual_verdict") is not None:
        failures.append("manual_verdict must be null")
    if row555.get("legacy_page_price_verdict") != "NOT_BREAK":
        failures.append("legacy verdict should remain for reference")
    if row555.get("system_verdict") != take_home.SYSTEM_POSSIBLE_BREAK:
        failures.append("product system_verdict should POSSIBLE_BREAK")
    if row555.get("low_price_signal"):
        failures.append("168 primary above red must not set low_price_signal")
    if job2["summary"].get("products_possible_break") != 1:
        failures.append("possible_break count should be 1")
    if job2["summary"].get("products_primary_low_price_signal", 0) != 0:
        failures.append("primary-low count must stay 0 when primary above red")
    if job2["summary"].get("completed") != 1:
        failures.append("completed should be 1 for multi-option job")
    if job2["summary"].get("detail_pages_opened") != 1:
        failures.append("detail_pages_opened should be 1")

    # Comparable primary below red → strong low_price_signal, still REVIEW take-home
    def fake_verify_primary_low(**kwargs):
        out = Path(kwargs["out_dir"])
        out.mkdir(parents=True, exist_ok=True)
        return {
            "task_status": "COMPLETED",
            "match_status": "PACKAGING_PASSED",
            "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
            "system_verdict_zh": "到手价待核验",
            "manual_verdict": None,
            "page_primary_price": "120.00",
            "packaging_match": "PASSED",
            "box_comparable": True,
            "price_clues": [
                {
                    "price_type": take_home.PRICE_TYPE_PAGE_PRIMARY,
                    "amount": "120.00",
                    "normalized_price": None,
                    "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                    "manual_verdict": None,
                    "review_reasons": ["主展示价低于红线，到手价待核验"],
                }
            ],
            "take_home_rollup": {"take_home_clue_count": 0, "has_possible_break": False},
            "ended_at_utc": utc_iso(),
            "verify_dir": str(out),
            "candidate": {"title": "清风 B913A", "product_id": "555"},
            "url_adapt": {
                "adapted_url": "https://item.m.jd.com/product/555.html",
                "final_url_sanitized": "https://item.m.jd.com/product/555.html",
            },
        }

    job2b = run_sku_job(
        business_config=cfg_path,
        query="清风 B913A",
        limit=1,
        search_fn=fake_search_one,
        verify_fn=fake_verify_primary_low,
    )
    row_low = job2b["product_rows"][0]
    if not row_low.get("low_price_signal"):
        failures.append("comparable primary below red needs low_price_signal")
    if row_low.get("system_verdict") != take_home.SYSTEM_REVIEW_REQUIRED:
        failures.append("primary-low must keep take-home REVIEW_REQUIRED")
    if row_low.get("signal_normalized_price") != "120.00":
        failures.append("comparable primary signal may keep amount as signal_normalized")
    if "主展示价低于红线" not in str(row_low.get("signal_conditions") or ""):
        failures.append("signal_conditions wording missing")
    if job2b["summary"].get("products_possible_break", 0) != 0:
        failures.append("must not count primary-low as possible_break")
    if job2b["summary"].get("products_primary_low_price_signal") != 1:
        failures.append("primary-low signal count should be 1")

    # Empty search → fallback row
    def fake_search_empty(query, limit, **kwargs):
        out = Path(kwargs["out_dir"])
        p3a.write_candidates_json(out / "candidates.json", [])
        meta = {
            "run_status": "NO_RESULTS",
            "candidate_count": 0,
            "coverage_stop_reason": "NO_RESULTS",
            "extract_stats": {},
            "scroll": {},
            "pages_visited": "UNKNOWN",
            "limit": limit,
            "query": query,
        }
        p3a.write_metadata(out / "run_metadata.json", meta)
        return {
            "ok": True,
            "exit_code": 0,
            "run_id": out.name,
            "out_dir": out,
            "candidates_path": out / "candidates.json",
            "candidates": [],
            "run_status": "NO_RESULTS",
            "coverage_stop_reason": "NO_RESULTS",
            "metadata": meta,
        }

    job3 = run_sku_job(
        business_config=cfg_path,
        query="清风 B913A",
        limit=5,
        search_fn=fake_search_empty,
        verify_fn=fake_verify,
    )
    if not job3["product_rows"] or job3["product_rows"][0].get("task_status") != "NO_CANDIDATES":
        failures.append("empty search must emit fallback row")

    # Phase C: max-verify budget vs model-skip; unverified has no price inheritance
    def fake_search_budget(query, limit=None, max_candidates=None, **kwargs):
        out = Path(kwargs["out_dir"])
        n = int(max_candidates or limit or 5)
        cands = []
        for i in range(1, n + 1):
            pid = str(1000 + i)
            title = (
                "清风 B913AE 16包"
                if i == 2
                else f"清风 B913A 箱装候选{i}"
            )
            cands.append(
                {
                    "query": query,
                    "rank": i,
                    "title": title,
                    "product_id": pid,
                    "shop_name": f"店{i}",
                    "ad_marked": False,
                    "normalized_url": f"https://item.jd.com/{pid}.html",
                    "observed_url": f"https://item.jd.com/{pid}.html",
                    "first_seen_load_batch": 0 if i <= 3 else 1,
                }
            )
        p3a.write_candidates_json(out / "candidates.json", cands)
        meta = {
            "run_status": "SUCCESS",
            "candidate_count": len(cands),
            "unique_candidates_collected": len(cands),
            "coverage_stop_reason": "HIT_CANDIDATE_LIMIT",
            "stop_reason": "HIT_CANDIDATE_LIMIT",
            "pages_confirmed": 2,
            "load_batches": [
                {"load_batch": 0, "new_id_count": 3},
                {"load_batch": 1, "new_id_count": max(0, len(cands) - 3)},
            ],
            "pagination_mechanism": "observed_next_page_control",
            "raw_cards_observed": 40,
            "scroll_count": 2,
            "elapsed_seconds": 12.5,
            "extract_stats": {"data_sku_card_count": 30},
            "scroll": {"scroll_count": 2},
            "pages_visited": 2,
            "limit": n,
            "max_candidates": n,
            "query": query,
        }
        p3a.write_metadata(out / "run_metadata.json", meta)
        return {
            "ok": True,
            "exit_code": 0,
            "run_id": out.name,
            "out_dir": out,
            "candidates_path": out / "candidates.json",
            "candidates": cands,
            "run_status": "SUCCESS",
            "coverage_stop_reason": "HIT_CANDIDATE_LIMIT",
            "metadata": meta,
            "close_ok": True,
        }

    verify_calls_budget: list[str] = []

    def fake_verify_budget(**kwargs):
        pid = str(kwargs["product_id"])
        verify_calls_budget.append(pid)
        out = Path(kwargs["out_dir"])
        out.mkdir(parents=True, exist_ok=True)
        cpath = Path(kwargs["candidates_path"])
        data = json.loads(cpath.read_text(encoding="utf-8"))
        title = next(x["title"] for x in data if str(x["product_id"]) == pid)
        gate = adapt.title_model_gate(title, expected_model=cfg["expected_model"])
        if gate.get("skip_browse"):
            return {
                "task_status": "SKIPPED",
                "match_status": "MODEL_MISMATCH_SKIPPED",
                "system_verdict": "MODEL_MISMATCH_SKIPPED",
                "system_verdict_zh": "型号不符已跳过（未打开详情）",
                "manual_verdict": None,
                "candidate_product_id": pid,
                "price_clues": [],
                "ended_at_utc": utc_iso(),
                "verify_dir": str(out),
                "candidate": {"title": title, "product_id": pid},
            }
        return {
            "task_status": "COMPLETED",
            "match_status": "PACKAGING_PASSED",
            "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
            "system_verdict_zh": "到手价待核验",
            "manual_verdict": None,
            "page_primary_price": "150.00",
            "packaging_match": "PASSED",
            "box_comparable": True,
            "price_clues": [
                {
                    "price_type": take_home.PRICE_TYPE_PAGE_PRIMARY,
                    "amount": "150.00",
                    "normalized_price": None,
                    "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                    "manual_verdict": None,
                    "review_reasons": ["到手价待核验"],
                }
            ],
            "take_home_rollup": {"take_home_clue_count": 0},
            "ended_at_utc": utc_iso(),
            "verify_dir": str(out),
            "candidate": {"title": title, "product_id": pid},
            "url_adapt": {
                "adapted_url": f"https://item.m.jd.com/product/{pid}.html",
                "final_url_sanitized": f"https://item.m.jd.com/product/{pid}.html",
            },
        }

    job4 = run_sku_job(
        business_config=cfg_path,
        query="清风 B913A",
        max_candidates=5,
        max_pages=2,
        max_verify=2,
        max_candidates_explicit=True,
        max_verify_explicit=True,
        search_fn=fake_search_budget,
        verify_fn=fake_verify_budget,
    )
    rows4 = job4["product_rows"]
    by4 = {str(r.get("jd_product_id")): r for r in rows4}
    # rank2 is B913AE → model skip, should still be verified(skipped) and not consume budget
    if by4.get("1002", {}).get("system_verdict") != "MODEL_MISMATCH_SKIPPED":
        failures.append("budget job: model mismatch row missing")
    # With max_verify=2: detail opens for 1001 and 1003 (skip 1002), rest budget
    budget_rows = [
        r for r in rows4 if r.get("task_status") == STATUS_NOT_VERIFIED_BUDGET
    ]
    if len(budget_rows) < 1:
        failures.append("expected NOT_VERIFIED_BUDGET rows when max_verify < candidates")
    if any(r.get("primary_display_price") for r in budget_rows):
        failures.append("budget-unverified must not inherit prices")
    if any(r.get("low_price_signal") for r in budget_rows):
        failures.append("budget-unverified must not get low_price_signal")
    summ4 = job4["summary"]
    if summ4.get("detail_pages_opened") != 2:
        failures.append(
            f"detail_pages_opened should be 2, got {summ4.get('detail_pages_opened')}"
        )
    if summ4.get("model_mismatch_skipped") != 1:
        failures.append("model_mismatch_skipped should be 1 in budget job")
    if summ4.get("not_verified_budget", 0) < 1:
        failures.append("not_verified_budget count missing")
    # Conflict: --limit vs --max-candidates
    try:
        resolve_job_budgets(
            limit=5,
            max_candidates=10,
            max_pages=None,
            search_timeout_seconds=None,
            max_verify=None,
            limit_explicit=True,
            max_candidates_explicit=True,
            max_verify_explicit=False,
        )
        failures.append("limit/max-candidates conflict should raise")
    except ValueError:
        pass

    print("=== P5-B offline self-check ===")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: all offline checks passed")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="P5-B single SKU search→verify job")
    p.add_argument(
        "--business-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "products" / "b913a.json",
    )
    p.add_argument(
        "--query",
        default=None,
        help="Optional search query; default from expected_brand+expected_model",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Legacy alias: alone sets search max-candidates and max-verify",
    )
    p.add_argument(
        "--max-candidates",
        type=int,
        default=None,
        help="Search unique-candidate budget (default 20)",
    )
    p.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Search page/load-batch budget (default 2)",
    )
    p.add_argument(
        "--search-timeout-seconds",
        type=int,
        default=None,
        help="Whole-search timeout seconds (default 60)",
    )
    p.add_argument(
        "--max-verify",
        type=int,
        default=None,
        help="Max detail visits (default 5; model-skip does not consume)",
    )
    p.add_argument("--self-check", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_check:
        return run_offline_self_checks()
    try:
        result = run_sku_job(
            business_config=args.business_config,
            query=args.query,
            limit=args.limit,
            max_candidates=args.max_candidates,
            max_pages=args.max_pages,
            search_timeout_seconds=args.search_timeout_seconds,
            max_verify=args.max_verify,
            limit_explicit=args.limit is not None,
            max_candidates_explicit=args.max_candidates is not None,
            max_verify_explicit=args.max_verify is not None,
        )
    except ValueError as exc:
        print(f"参数/配置错误: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"运行失败: {type(exc).__name__}: {exc}")
        return 1
    summary = result.get("summary") or {}
    if summary.get("candidates_paused_unvisited"):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
