"""
P5-B: single business SKU — search/reuse → classify → verify → report → resume.

Reuses p3a/p5f search, p5a.verify_one, and explicit BSK/p2d backends (no auto-switch).
Candidate sources: live-search | existing-collection (explicit path; never “latest”).
Does not implement multi-SKU Excel, JDSpider, checkout, or coupon claiming.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import importlib.util
import json
import secrets
import shutil
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
JOB_ROOT = (PROJECT_ROOT / "artifacts" / "sku_jobs").resolve()

RULES_VERSION = "p5b-sku-job-v3"
DEFAULT_MAX_VERIFY = 5
MAX_VERIFY_CAP = 30
STATUS_NOT_VERIFIED_BUDGET = "NOT_VERIFIED_BUDGET"
STATUS_NOT_VISITED_BUDGET = "NOT_VISITED_BUDGET"  # alias in reports
STATUS_CLASSIFICATION_SKIPPED = "CLASSIFICATION_SKIPPED"

SOURCE_LIVE_SEARCH = "live-search"
SOURCE_EXISTING_COLLECTION = "existing-collection"
BACKEND_P2D = "p2d"
BACKEND_BSK = "bsk"

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
TERMINAL_OK_STATUSES = frozenset(
    {
        "COMPLETED",
        "SKIPPED",
        STATUS_NOT_VERIFIED_BUDGET,
        STATUS_CLASSIFICATION_SKIPPED,
        "NO_CANDIDATES",
    }
)
TERMINAL_FAIL_STATUSES = frozenset({"FAILED"})
RESUME_PENDING_MATCH = frozenset(
    {
        "PAUSED_BEFORE_VISIT",
        STATUS_NOT_VERIFIED_BUDGET,
        "NOT_VISITED",
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
p5f = load_sibling("p5f_collect_all_links")
adapt = load_sibling("priceradar_candidate_adapt")
take_home = load_sibling("priceradar_take_home_prices")
model_match = load_sibling("priceradar_model_match")


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


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def normalize_backend(backend: str | None) -> str:
    b = str(backend or "").strip().lower()
    if b in {"playwright", "p2d"}:
        return BACKEND_P2D
    if b == BACKEND_BSK:
        return BACKEND_BSK
    raise ValueError(f"backend 必须显式指定为 {BACKEND_P2D}|{BACKEND_BSK}，收到 {backend!r}")


def search_backend_for(backend: str) -> str:
    """Map detail backend to p5f search backend name (no auto-switch)."""
    if backend == BACKEND_BSK:
        return p5f.BACKEND_BSK
    return p5f.BACKEND_PLAYWRIGHT


def load_model_scope_policy(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"model-scope-policy 不存在: {p}")
    data = read_json(p)
    if not isinstance(data, dict):
        raise ValueError("model-scope-policy 必须是 JSON object")
    return data


def build_job_fingerprint(
    *,
    cfg: dict[str, Any],
    query: str,
    candidate_source: str,
    existing_collection_dir: str | None,
    backend: str,
    model_scope_policy: dict[str, Any] | None,
) -> dict[str, Any]:
    policy_id = None
    policy_hash = None
    if model_scope_policy:
        policy_id = model_scope_policy.get("policy_id")
        policy_hash = stable_hash(model_scope_policy)
    return {
        "rules_version": RULES_VERSION,
        "adapt_rules_version": adapt.RULES_VERSION_ADAPT,
        "take_home_rules_version": take_home.RULES_VERSION_TAKE_HOME,
        "model_match_rules_version": getattr(model_match, "RULES_VERSION", None),
        "business_sku": cfg.get("business_sku"),
        "expected_model": cfg.get("expected_model"),
        "red_line": str(cfg.get("red_line")),
        "query": query,
        "candidate_source": candidate_source,
        "existing_collection_dir": existing_collection_dir,
        "backend": backend,
        "model_scope_policy_id": policy_id,
        "model_scope_policy_hash": policy_hash,
    }


def fingerprint_mismatch_reasons(
    saved: dict[str, Any] | None, current: dict[str, Any]
) -> list[str]:
    if not saved:
        return ["缺少已保存 fingerprint，拒绝静默续跑"]
    reasons = []
    for key in (
        "rules_version",
        "adapt_rules_version",
        "take_home_rules_version",
        "model_match_rules_version",
        "business_sku",
        "expected_model",
        "red_line",
        "query",
        "candidate_source",
        "existing_collection_dir",
        "backend",
        "model_scope_policy_id",
        "model_scope_policy_hash",
    ):
        if saved.get(key) != current.get(key):
            reasons.append(f"{key}: saved={saved.get(key)!r} current={current.get(key)!r}")
    return reasons


def resolve_job_dir(job_id: str) -> Path:
    job_dir = (JOB_ROOT / job_id).resolve()
    if not job_dir.is_dir():
        raise FileNotFoundError(f"resume job 不存在: {job_dir}")
    return job_dir


def new_attempt_id(seq: int) -> str:
    return f"att{seq:03d}_{secrets.token_hex(2)}"


def classify_candidate(
    cand: dict[str, Any], *, expected_model: str
) -> dict[str, Any]:
    out = dict(cand)
    title = str(cand.get("title") or "")
    if cand.get("classification"):
        gate = adapt.title_model_gate(title, expected_model=expected_model)
        gate = dict(gate)
        gate["classification"] = cand.get("classification")
        gate["observed_model_tokens"] = (
            cand.get("observed_model_tokens") or gate.get("observed_model_tokens")
        )
        gate["source_classification"] = "candidate_record"
    else:
        gate = adapt.title_model_gate(title, expected_model=expected_model)
        gate = dict(gate)
        gate["source_classification"] = "live_title_rules"
    out["classification"] = gate.get("classification")
    out["title_gate"] = gate
    out["observed_model_tokens"] = gate.get("observed_model_tokens")
    out["classification_reason"] = gate.get("reason")
    return out


def candidate_detail_plan(
    cand: dict[str, Any],
    *,
    cfg: dict[str, Any],
    model_scope_policy: dict[str, Any] | None,
) -> dict[str, Any]:
    """Decide whether this candidate needs a detail navigation (budget-consuming)."""
    expected_model = str(cfg.get("expected_model") or "")
    business_sku = str(cfg.get("business_sku") or expected_model)
    gate = cand.get("title_gate") or adapt.title_model_gate(
        str(cand.get("title") or ""), expected_model=expected_model
    )
    auth = p5a.model_scope_allows_title_browse(
        model_scope_policy, gate=gate, business_sku=business_sku
    )
    skip_browse = bool(gate.get("skip_browse"))
    needs_detail = (not skip_browse) or bool(auth.get("allowed"))
    classification_skip = skip_browse and not auth.get("allowed")
    return {
        "gate": gate,
        "title_auth": auth,
        "needs_detail": needs_detail,
        "classification_skip": classification_skip,
        "authorization_note": (
            auth.get("other_model_label_zh")
            if auth.get("allowed")
            else (
                "TARGET纳入本次业务基准"
                if gate.get("classification") == model_match.CLASS_TARGET
                else "未授权自动纳入"
            )
        ),
    }


def collection_to_candidates(
    rows: list[dict[str, Any]],
    *,
    source_dir: Path,
    coverage: dict[str, Any],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, row in enumerate(rows, start=1):
        pid = str(row.get("product_id") or row.get("jd_product_id") or "").strip()
        if not pid:
            continue
        url = str(row.get("normalized_url") or row.get("observed_url") or "").strip()
        if not url:
            url = f"https://item.jd.com/{pid}.html"
        discovered = (
            row.get("discovered_at_utc")
            or row.get("source_collected_at_utc")
            or coverage.get("started_at_utc")
            or coverage.get("ended_at_utc")
        )
        out.append(
            {
                "product_id": pid,
                "jd_product_id": pid,
                "rank": row.get("rank") or row.get("source_page") or i,
                "title": row.get("title"),
                "shop_name": row.get("shop_name"),
                "ad_marked": row.get("ad_marked"),
                "ad_status": row.get("ad_status"),
                "normalized_url": url,
                "observed_url": row.get("observed_url") or url,
                "discovery_source": "existing-collection",
                "discovered_at_utc": discovered,
                "source_collection_dir": str(source_dir),
                "source_run_id": row.get("source_run_id")
                or coverage.get("run_id")
                or source_dir.name,
                "classification": row.get("classification"),
                "observed_model_tokens": row.get("observed_model_tokens"),
                "other_models_normalized": row.get("other_models_normalized"),
                "conflict_token": row.get("conflict_token"),
                "page": row.get("page") or row.get("source_page"),
                "query": row.get("query") or coverage.get("query"),
                "synthetic": row.get("synthetic"),
            }
        )
    return out


def load_existing_collection(
    collection_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any], Path]:
    """Load explicit collection dir. Never picks ‘latest’."""
    source = Path(collection_dir).resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"既有采集目录不存在: {source}")
    coverage: dict[str, Any] = {}
    for name in ("coverage.json", "run_metadata.json", "summary.json"):
        p = source / name
        if p.is_file():
            try:
                loaded = read_json(p)
                if isinstance(loaded, dict):
                    coverage = {**coverage, **loaded}
            except json.JSONDecodeError:
                pass

    rows: list[dict[str, Any]] | None = None
    used: Path | None = None
    for name in ("all_candidates.json", "all_classified.json", "candidates.json"):
        p = source / name
        if p.is_file():
            data = read_json(p)
            if isinstance(data, list):
                rows = data
                used = p
                break
            if isinstance(data, dict) and isinstance(data.get("candidates"), list):
                rows = data["candidates"]
                used = p
                break
    if rows is None or used is None:
        raise FileNotFoundError(
            f"{source} 缺少 all_candidates.json / all_classified.json / candidates.json"
        )

    candidates = collection_to_candidates(rows, source_dir=source, coverage=coverage)
    integrity = (
        (coverage.get("completeness") or {}).get("integrity")
        or coverage.get("overall_integrity")
        or coverage.get("search_run_status")
        or coverage.get("run_status")
    )
    stop_reason = (
        (coverage.get("completeness") or {}).get("stop_reason")
        or coverage.get("coverage_stop_reason")
        or coverage.get("stop_reason")
    )
    restricted = bool((coverage.get("completeness") or {}).get("restricted"))
    partial = bool(
        integrity
        and str(integrity).upper()
        not in {"COMPLETE", "COMPLETE_EXPLAINABLE", "SUCCESS"}
    ) or restricted
    meta = {
        "candidate_source": SOURCE_EXISTING_COLLECTION,
        "reuse_mode": True,
        "existing_collection_dir": str(source),
        "existing_candidates_file": str(used),
        "run_status": coverage.get("run_status") or ("PARTIAL" if partial else "REUSED"),
        "search_run_status": coverage.get("run_status")
        or coverage.get("overall_integrity")
        or ("PARTIAL" if partial else "REUSED_COLLECTION"),
        "coverage_stop_reason": stop_reason,
        "stop_reason": stop_reason,
        "overall_integrity": integrity,
        "query": coverage.get("query"),
        "pages_confirmed": coverage.get("pages_visited")
        or (coverage.get("completeness") or {}).get("continuous_pages"),
        "pages_visited": coverage.get("pages_visited"),
        "unique_candidates_collected": len(candidates),
        "candidate_count": len(candidates),
        "raw_cards_observed": coverage.get("unique_candidates")
        or coverage.get("raw_cards_observed"),
        "backend": coverage.get("backend"),
        "discovered_at_utc": coverage.get("started_at_utc")
        or coverage.get("ended_at_utc")
        or coverage.get("created_at_utc"),
        "coverage_note": (
            "复用显式既有采集目录的候选链接；"
            "候选发现时间来自历史采集，详情核验时间为本任务；"
            "复用链接 ≠ 复用历史价格；非本轮实时搜索。"
            + (" 源采集完整性非 COMPLETE，不得显示为完整覆盖。" if partial else "")
        ),
        "completeness": coverage.get("completeness"),
        "end_page_evidence": (coverage.get("completeness") or {}).get(
            "end_page_evidence"
        ),
        "synthetic": any(c.get("synthetic") for c in candidates),
    }
    return candidates, meta, used


def materialize_candidates_file(
    search_dir: Path, candidates: list[dict[str, Any]]
) -> Path:
    path = search_dir / "candidates.json"
    p3a.write_candidates_json(path, candidates)
    return path


def mark_candidate_budget_truncation(
    meta: dict[str, Any], *, collected: int, kept: int
) -> None:
    """Candidate budget cut the list: keep the real stop reason, never "naturally finished"."""
    meta["truncated_by_max_candidates"] = True
    meta["candidates_collected_before_cap"] = collected
    meta["candidates_kept_after_cap"] = kept
    meta.setdefault("source_overall_integrity", meta.get("overall_integrity"))
    meta.setdefault("source_stop_reason", meta.get("coverage_stop_reason"))
    meta["overall_integrity"] = "PARTIAL"
    meta["coverage_stop_reason"] = "HIT_CANDIDATE_BUDGET"
    meta["stop_reason"] = "HIT_CANDIDATE_BUDGET"


def run_live_search(
    *,
    query: str,
    backend: str,
    business_config: Path,
    max_cand: int,
    max_pg: int,
    timeout_s: int,
    search_dir: Path,
    bsk_browser: str | None,
    search_fn: Callable[..., dict[str, Any]] | None,
    focus_window: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any], Path, dict[str, Any]]:
    """Execute live search with explicit backend. Returns candidates, meta, path, raw."""
    if search_fn is not None:
        search_result = search_fn(
            query,
            focus_window=focus_window,
            limit=max_cand,
            max_candidates=max_cand,
            max_pages=max_pg,
            search_timeout_seconds=timeout_s,
            auto_close=True,
            out_dir=search_dir,
            quiet=False,
            limit_explicit=False,
            max_candidates_explicit=True,
            backend=backend,
            browser=bsk_browser,
        )
        candidates = list(search_result.get("candidates") or [])
        search_meta = dict(search_result.get("metadata") or {})
        candidates_path = Path(
            search_result.get("candidates_path") or (search_dir / "candidates.json")
        )
        if candidates_path.is_file():
            try:
                loaded = read_json(candidates_path)
                if isinstance(loaded, list):
                    candidates = loaded
            except json.JSONDecodeError:
                pass
        for c in candidates:
            c.setdefault("discovered_at_utc", utc_iso())
            c.setdefault("discovery_source", SOURCE_LIVE_SEARCH)
        search_meta["candidate_source"] = SOURCE_LIVE_SEARCH
        search_meta["reuse_mode"] = False
        search_meta.setdefault("candidate_count", len(candidates))
        search_meta.setdefault("unique_candidates_collected", len(candidates))
        search_meta.setdefault("run_status", search_result.get("run_status"))
        search_meta.setdefault(
            "coverage_stop_reason", search_result.get("coverage_stop_reason")
        )
        return candidates, search_meta, candidates_path, search_result

    if backend == BACKEND_BSK:
        # p5f writes under collect_all_runs; copy handoff into this job's search_dir.
        collect = p5f.run_collect_all(
            query=query,
            business_config=business_config,
            max_pages=max_pg,
            search_timeout_seconds=max(timeout_s, 30),
            allow_browser=True,
            backend=search_backend_for(backend),
            browser=bsk_browser,
            reuse_search_tab=True,
            focus_window=focus_window,
        )
        collect_dir = Path(collect.get("run_dir") or collect.get("out_dir") or "")
        if not collect_dir.is_dir():
            raise RuntimeError("BSK live-search 未返回有效 collect run_dir")
        # Mirror key artifacts into job search_dir for single-job audit trail.
        for name in (
            "all_candidates.json",
            "coverage.json",
            "checkpoint.json",
            "report.html",
        ):
            src = collect_dir / name
            if src.is_file():
                shutil.copy2(src, search_dir / name)
        write_json(
            search_dir / "live_search_collect_pointer.json",
            {
                "collect_run_dir": str(collect_dir),
                "backend": BACKEND_BSK,
                "query": query,
                "copied_at_utc": utc_iso(),
            },
        )
        candidates, meta, used = load_existing_collection(collect_dir)
        # Re-tag as live discovery (this job triggered the search).
        now = utc_iso()
        for c in candidates:
            c["discovery_source"] = SOURCE_LIVE_SEARCH
            c["discovered_at_utc"] = c.get("discovered_at_utc") or now
            c["live_collect_run_dir"] = str(collect_dir)
        _collected_n = len(candidates)
        if max_cand and len(candidates) > max_cand:
            candidates = candidates[:max_cand]
            mark_candidate_budget_truncation(meta, collected=_collected_n, kept=len(candidates))
        meta["candidate_source"] = SOURCE_LIVE_SEARCH
        meta["reuse_mode"] = False
        meta["live_collect_run_dir"] = str(collect_dir)
        meta["coverage_note"] = (
            "本轮 live-search（BSK/p5f）采集；详情核验另计时间。"
            + (
                " 搜索完整性非 COMPLETE，不得显示为完整覆盖。"
                if str(meta.get("overall_integrity") or "").upper()
                not in {"COMPLETE", "COMPLETE_EXPLAINABLE", "SUCCESS", ""}
                and meta.get("overall_integrity")
                else ""
            )
        )
        meta["unique_candidates_collected"] = len(candidates)
        meta["candidate_count"] = len(candidates)
        meta.setdefault("run_status", collect.get("run_status") or meta.get("run_status"))
        candidates_path = materialize_candidates_file(search_dir, candidates)
        write_json(search_dir / "run_metadata.json", meta)
        return candidates, meta, candidates_path, collect

    # p2d / Playwright search via p3a
    search_result = p3a.run_search_collect(
        query,
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
    candidates = list(search_result.get("candidates") or [])
    search_meta = dict(search_result.get("metadata") or {})
    candidates_path = Path(
        search_result.get("candidates_path") or (search_dir / "candidates.json")
    )
    if candidates_path.is_file():
        try:
            loaded = read_json(candidates_path)
            if isinstance(loaded, list):
                candidates = loaded
        except json.JSONDecodeError:
            pass
    now = utc_iso()
    for c in candidates:
        c.setdefault("discovered_at_utc", now)
        c.setdefault("discovery_source", SOURCE_LIVE_SEARCH)
    search_meta["candidate_source"] = SOURCE_LIVE_SEARCH
    search_meta["reuse_mode"] = False
    search_meta.setdefault("candidate_count", len(candidates))
    search_meta.setdefault("unique_candidates_collected", len(candidates))
    search_meta.setdefault("run_status", search_result.get("run_status"))
    search_meta.setdefault(
        "coverage_stop_reason", search_result.get("coverage_stop_reason")
    )
    search_meta["backend"] = BACKEND_P2D
    return candidates, search_meta, candidates_path, search_result


def classification_skip_row(
    *,
    business_sku: str,
    red_line: str,
    candidate: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    gate = plan.get("gate") or {}
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
            "classification": gate.get("classification")
            or candidate.get("classification"),
            "authorization_note": plan.get("authorization_note"),
            "primary_display_price": None,
            "take_home_status": STATUS_CLASSIFICATION_SKIPPED,
            "lowest_observed_take_home_price": None,
            "lowest_take_home_option_id": None,
            "lowest_take_home_price_type": None,
            "lowest_take_home_conditions": None,
            "normalized_price": None,
            "red_line": red_line,
            "price_delta": None,
            "task_status": "SKIPPED",
            "match_status": "MODEL_MISMATCH_SKIPPED",
            "system_verdict": "MODEL_MISMATCH_SKIPPED",
            "system_verdict_zh": "分类跳过（未打开详情；不占导航预算）",
            "manual_verdict": None,
            "legacy_page_price_verdict": None,
            "review_reasons": gate.get("reason") or plan.get("title_auth", {}).get("reason"),
            "discovered_at": candidate.get("discovered_at_utc"),
            "checked_at": utc_iso(),
            "candidate_run_dir": "",
            "evidence_paths": "",
            "screenshot_link": "",
            "packaging_match": None,
            "box_comparable": None,
            "attempt_id": None,
            "execution_status": STATUS_CLASSIFICATION_SKIPPED,
        }
    )


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
    "classification",
    "authorization_note",
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
    "execution_status",
    "system_verdict",
    "system_verdict_zh",
    "manual_verdict",
    "legacy_page_price_verdict",
    "review_reasons",
    "discovered_at",
    "checked_at",
    "attempt_id",
    "candidate_run_dir",
    "evidence_paths",
    "screenshot_link",
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
    plan: dict[str, Any] | None = None,
    attempt_id: str | None = None,
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
    shot = ""
    if verify.get("screenshot_dir"):
        evidence.append(str(verify["screenshot_dir"]))
        shot = str(verify["screenshot_dir"])
    if verify.get("pipeline_dir"):
        evidence.append(str(verify["pipeline_dir"]))
    if verify.get("verify_dir"):
        evidence.append(str(verify["verify_dir"]))
    capture = verify.get("capture") or {}
    if capture.get("screenshot_path"):
        shot = str(capture["screenshot_path"])
        evidence.append(shot)

    url_adapt = verify.get("url_adapt") or {}
    plan = plan or {}
    gate = plan.get("gate") or candidate.get("title_gate") or {}
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
        "observed_spec": verify.get("observed_spec") or "",
        "classification": gate.get("classification")
        or candidate.get("classification")
        or "",
        "authorization_note": plan.get("authorization_note")
        or (
            (verify.get("title_gate_authorization") or {}).get("other_model_label_zh")
        ),
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
        "execution_status": verify.get("task_status"),
        "system_verdict": verify.get("system_verdict"),
        "system_verdict_zh": verify.get("system_verdict_zh"),
        "manual_verdict": None,
        "legacy_page_price_verdict": verify.get("legacy_page_price_verdict"),
        "review_reasons": "; ".join(
            r for r in (verify.get("review_reasons") or []) if r
        )
        if isinstance(verify.get("review_reasons"), list)
        else (verify.get("review_reasons") or verify.get("failure_reason") or ""),
        "discovered_at": candidate.get("discovered_at_utc"),
        "checked_at": verify.get("ended_at_utc") or verify.get("started_at_utc"),
        "attempt_id": attempt_id or verify.get("attempt_id"),
        "candidate_run_dir": verify.get("verify_dir")
        or ((verify.get("capture") or {}).get("out_dir")),
        "evidence_paths": ";".join(evidence),
        "screenshot_link": shot,
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
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan = plan or {}
    gate = plan.get("gate") or candidate.get("title_gate") or {}
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
            "classification": gate.get("classification")
            or candidate.get("classification"),
            "authorization_note": plan.get("authorization_note"),
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
            "execution_status": "NOT_VISITED_SESSION_PAUSE",
            "system_verdict": SESSION_PAUSE_TASK,
            "system_verdict_zh": "会话暂停，候选未访问（非普通采集失败）",
            "manual_verdict": None,
            "legacy_page_price_verdict": None,
            "review_reasons": reason,
            "discovered_at": candidate.get("discovered_at_utc"),
            "checked_at": utc_iso(),
            "attempt_id": None,
            "candidate_run_dir": "",
            "evidence_paths": "",
            "screenshot_link": "",
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
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Candidate kept from search but not detail-verified due to max-verify budget."""
    plan = plan or {}
    gate = plan.get("gate") or candidate.get("title_gate") or {}
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
            "classification": gate.get("classification")
            or candidate.get("classification"),
            "authorization_note": plan.get("authorization_note"),
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
            "execution_status": STATUS_NOT_VISITED_BUDGET,
            "system_verdict": STATUS_NOT_VERIFIED_BUDGET,
            "system_verdict_zh": (
                f"搜索已收录，但未核验（详情访问预算 max-verify={max_verify}）"
            ),
            "manual_verdict": None,
            "legacy_page_price_verdict": None,
            "review_reasons": (
                "超出详情核验预算，未打开详情；"
                "不得当作失败、已核验或有低价；不得继承其他候选价格"
            ),
            "discovered_at": candidate.get("discovered_at_utc"),
            "checked_at": utc_iso(),
            "attempt_id": None,
            "candidate_run_dir": "",
            "evidence_paths": "",
            "screenshot_link": "",
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
            "discovered_at": None,
            "checked_at": utc_iso(),
            "attempt_id": None,
            "candidate_run_dir": "",
            "evidence_paths": "",
            "screenshot_link": "",
            "packaging_match": None,
            "box_comparable": None,
            "classification": None,
            "authorization_note": None,
            "execution_status": "NO_CANDIDATES",
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
    detail_pages_opened_actual: int | None = None,
    session_paused: bool = False,
    job_execution_complete: bool = True,
) -> dict[str, Any]:
    real_rows = [r for r in product_rows if r.get("jd_product_id")]
    placeholder = [r for r in product_rows if not r.get("jd_product_id")]

    seen_ids: set[str] = set()
    dedup_rows: list[dict[str, Any]] = []
    for r in real_rows:
        pid = str(r.get("jd_product_id"))
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        dedup_rows.append(r)
    real_rows = dedup_rows

    candidates_found = int(
        search_meta.get("unique_candidates_collected")
        or search_meta.get("candidate_count")
        or len(real_rows)
    )
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
    classification_skipped = sum(
        1
        for r in real_rows
        if r.get("system_verdict") == "MODEL_MISMATCH_SKIPPED"
        or r.get("match_status") == "MODEL_MISMATCH_SKIPPED"
        or r.get("execution_status") == STATUS_CLASSIFICATION_SKIPPED
    )
    visited = len(real_rows) - paused_unvisited - not_verified_budget
    model_skip = classification_skipped
    comparable_products = sum(
        1
        for r in real_rows
        if r.get("lowest_observed_take_home_price") not in (None, "")
    )
    possible_break_products = sum(
        1 for r in real_rows if r.get("system_verdict") == take_home.SYSTEM_POSSIBLE_BREAK
    )
    no_break_in_scope = sum(
        1
        for r in real_rows
        if r.get("system_verdict") == take_home.SYSTEM_NO_BREAK_IN_SCOPE
    )
    review_products = sum(
        1
        for r in real_rows
        if r.get("system_verdict") == take_home.SYSTEM_REVIEW_REQUIRED
        or r.get("take_home_status") == "TAKE_HOME_PENDING"
    )
    failed = sum(1 for r in real_rows if r.get("task_status") == "FAILED")
    completed = sum(1 for r in real_rows if r.get("task_status") == "COMPLETED")
    if detail_pages_opened_actual is not None:
        detail_pages_opened = int(detail_pages_opened_actual)
    else:
        detail_pages_opened = max(0, visited - model_skip)
    primary_low_signal = sum(1 for r in real_rows if r.get("low_price_signal") is True)
    valid_options = sum(1 for o in price_options if o.get("comparable_for_min"))
    mv = max_verify if max_verify is not None else search_meta.get("max_verify")

    pending_verify = paused_unvisited + not_verified_budget
    all_candidates_verified = pending_verify == 0 and not session_paused
    verified_in_scope = completed + failed + model_skip

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
    if comparable_products == 0:
        best = None
        best_opt = None

    extract = search_meta.get("extract_stats") or {}
    scroll = search_meta.get("scroll") or {}
    integrity = search_meta.get("overall_integrity") or (
        (search_meta.get("completeness") or {}).get("integrity")
    )
    search_status = (
        search_meta.get("run_status") or search_meta.get("search_status") or ""
    )
    partial_search = (
        str(integrity or "").upper() in {"PARTIAL", "RESTRICTED"}
        or str(search_status).upper() in {"PARTIAL", "SEARCH_RESTRICTED"}
        or bool((search_meta.get("completeness") or {}).get("restricted"))
    )
    reuse_mode = bool(search_meta.get("reuse_mode"))
    scope_prefix = (
        "既有采集目录候选"
        if reuse_mode
        else ("本次搜索（部分/受限）" if partial_search else "本次搜索范围内")
    )
    coverage = {
        "scope_statement": (
            f"{scope_prefix}；不是全京东、不是全部链接、不是全平台最低价"
        ),
        "query": query,
        "query_generation_basis": query_basis,
        "candidate_source": search_meta.get("candidate_source")
        or (SOURCE_EXISTING_COLLECTION if reuse_mode else SOURCE_LIVE_SEARCH),
        "reuse_mode": reuse_mode,
        "existing_collection_dir": search_meta.get("existing_collection_dir"),
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
        "search_run_status": search_status,
        "overall_integrity": integrity,
        "end_page_evidence": search_meta.get("end_page_evidence")
        or (search_meta.get("completeness") or {}).get("end_page_evidence"),
        "partial_or_restricted": partial_search,
        "pages_visited": search_meta.get("pages_visited")
        or search_meta.get("pages_confirmed"),
        "candidate_discovery_at_utc": search_meta.get("discovered_at_utc"),
        "coverage_note": search_meta.get("coverage_note")
        or ("本次搜索范围内有限页/加载批次收集；不是全京东、不是全平台最低价"),
        "hit_limit_means": "达到候选上限属于覆盖限制，不等于全部商品",
        "scope_line": (
            f"{scope_prefix}收集{candidates_found}个候选，"
            f"分类跳过{model_skip}个，实际导航{detail_pages_opened}个，"
            f"完成{completed}个，失败{failed}个，"
            f"预算未访问{not_verified_budget}个，"
            f"暂停未访问{paused_unvisited}个。"
            + (" 搜索为 PARTIAL/受限，不得视为完整覆盖。" if partial_search else "")
        ),
    }

    return {
        "business_sku": business_sku,
        "rules_version": RULES_VERSION,
        "candidates_found": candidates_found,
        "candidates_processed": visited,
        "candidates_visited": visited,
        "detail_pages_opened": detail_pages_opened,
        "model_mismatch_skipped": model_skip,
        "classification_skipped": classification_skipped,
        "not_verified_budget": not_verified_budget,
        "completed": completed,
        "failed": failed,
        "paused_unvisited": paused_unvisited,
        "products_with_comparable_take_home": comparable_products,
        "products_possible_break": possible_break_products,
        "products_no_break_in_scope": no_break_in_scope,
        "products_primary_low_price_signal": primary_low_signal,
        "products_review_required": review_products,
        "products_failed": failed,
        "candidates_paused_unvisited": paused_unvisited,
        "valid_comparable_price_option_count": valid_options,
        "price_option_count_total": len(price_options),
        "max_verify": mv,
        "job_execution_complete": job_execution_complete,
        "all_candidates_verified": all_candidates_verified,
        "verified_coverage_count": verified_in_scope,
        "verified_coverage_of_candidates": f"{verified_in_scope}/{candidates_found}",
        "session_paused": session_paused,
        "reuse_mode": reuse_mode,
        "no_break_in_scope_note": (
            "NO_BREAK_IN_SCOPE 仅针对已识别的有效可比方案，"
            "不代表整个 SKU / 全部候选没有破价"
        ),
        "stat_definitions": {
            "candidates_found": "搜索/既有采集去重后的候选商品数（非价格方案数）",
            "candidates_processed": (
                "已进入核验流程的候选（含型号跳过/失败；"
                "不含暂停未访问、不含预算未核验）"
            ),
            "candidates_visited": "同 candidates_processed（兼容旧字段）",
            "detail_pages_opened": "实际详情导航次数（分类跳过不占预算）",
            "model_mismatch_skipped": "分类跳过商品数（未开详情，不占导航预算）",
            "classification_skipped": "同 model_mismatch_skipped",
            "not_verified_budget": "因 max-verify 预算未打开详情的候选数",
            "completed": "task_status=COMPLETED 的商品数",
            "failed": "task_status=FAILED 的商品数（普通失败）",
            "paused_unvisited": "会话暂停后未打开的候选数（非普通失败）",
            "products_with_comparable_take_home": "至少有一个有效可比到手价方案的商品数",
            "products_possible_break": "商品级 system_verdict=POSSIBLE_BREAK（疑似破价）",
            "products_no_break_in_scope": "已核验可比范围内未破价商品数；≠整 SKU 无破价",
            "products_primary_low_price_signal": "主展示低价线索商品数（≠已确认破价）",
            "products_review_required": "商品级待核验（含无到手价证据）",
            "products_failed": "同 failed（兼容）",
            "candidates_paused_unvisited": "同 paused_unvisited（兼容）",
            "valid_comparable_price_option_count": "可比到手价方案条数（≠商品数）",
            "job_execution_complete": "本轮编排已正常收尾（可能仍有未核验候选）",
            "all_candidates_verified": "全部候选均已核验完毕（无预算/暂停残留）",
        },
        "search_coverage": coverage,
        "verify_coverage": {
            "max_verify": mv,
            "detail_pages_opened": detail_pages_opened,
            "model_mismatch_skipped": model_skip,
            "classification_skipped": classification_skipped,
            "not_verified_budget": not_verified_budget,
            "paused_unvisited": paused_unvisited,
            "completed": completed,
            "failed": failed,
            "scope_line": coverage["scope_line"],
            "job_execution_complete": job_execution_complete,
            "all_candidates_verified": all_candidates_verified,
            "verified_coverage_of_candidates": f"{verified_in_scope}/{candidates_found}",
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
            "NOT_VERIFIED_BUDGET 不得当作失败或有低价",
            "manual_verdict 系统不代填",
            "不得称全部京东链接或全平台最低价",
            "任务执行完成 ≠ 全部候选核验完毕",
            "NO_BREAK_IN_SCOPE 仅针对已识别有效可比方案",
            "候选发现时间与详情检查时间分开记录",
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
            return "<tr><td colspan='99'>（空）</td></tr>"
        head = "".join(f"<th>{esc(k)}</th>" for k in keys)
        body = []
        for r in rows:
            body.append(
                "<tr>"
                + "".join(
                    f"<td>{esc(str(r.get(k) if r.get(k) is not None else ''))}</td>"
                    for k in keys
                )
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
        "url",
        "title",
        "observed_model",
        "classification",
        "authorization_note",
        "observed_spec",
        "primary_display_price",
        "lowest_observed_take_home_price",
        "lowest_take_home_conditions",
        "box_comparable",
        "red_line",
        "system_verdict",
        "low_price_signal",
        "signal_review_reasons",
        "review_reasons",
        "execution_status",
        "task_status",
        "discovered_at",
        "checked_at",
        "screenshot_link",
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
        "请分别核对候选发现时间、来源任务、复用时间及详情核验时间；"
        "复用链接不等于复用历史价格。"
        "历史复用不得在现网搜索受限后自动兜底。</p>"
        if reuse_mode
        else ""
    )
    exec_line = (
        f"任务执行完成={esc(str(summary.get('job_execution_complete')))} · "
        f"全部候选核验完毕={esc(str(summary.get('all_candidates_verified')))} · "
        f"覆盖={esc(str(summary.get('verified_coverage_of_candidates')))}"
    )
    partial_flag = ""
    if cov.get("partial_or_restricted"):
        partial_flag = (
            "<p class='warn'>搜索 PARTIAL/受限：不得显示为完整覆盖。"
            f" integrity={esc(str(cov.get('overall_integrity')))}"
            f" stop={esc(str(cov.get('coverage_stop_reason')))}</p>"
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
<strong>本次范围内</strong>（非全京东 / 非全平台最低价）</p>
<p class="muted">{exec_line}</p>
{evidence_note_html}
{reuse_html}
{partial_flag}

<div class="card">
  <h2>覆盖说明</h2>
  <p>{esc(str(cov.get('scope_statement')))}</p>
  <ul>
    <li>候选来源：{esc(str(cov.get('candidate_source')))}
        · 复用={esc(str(cov.get('reuse_mode')))}</li>
    <li>搜索词：{esc(str(cov.get('query')))}（{esc(str(cov.get('query_generation_basis')))}）</li>
    <li>limit：{esc(str(cov.get('limit')))} · 去重候选：{esc(str(cov.get('deduped_candidate_count')))}</li>
    <li>页数：{esc(str(cov.get('pages_visited')))} · 停止原因：{esc(str(cov.get('coverage_stop_reason')))}</li>
    <li>完整性：{esc(str(cov.get('overall_integrity')))} · 搜索状态：{esc(str(cov.get('search_run_status')))}</li>
    <li>末页证据：{esc(str(cov.get('end_page_evidence')))}</li>
    <li>候选发现时间：{esc(str(cov.get('candidate_discovery_at_utc')))}</li>
  </ul>
</div>

<div class="card">
  <h2>SKU 汇总 · {esc(str(summary.get('business_sku')))}</h2>
  <p><strong>{esc(str((summary.get('search_coverage') or {}).get('scope_line') or ''))}</strong></p>
  <ul>
    <li>搜索候选：{esc(str(summary.get('candidates_found')))}</li>
    <li>分类跳过：{esc(str(summary.get('classification_skipped')))}</li>
    <li>实际导航：{esc(str(summary.get('detail_pages_opened')))}</li>
    <li>完成 / 失败：{esc(str(summary.get('completed')))} / {esc(str(summary.get('failed')))}</li>
    <li>预算未访问：{esc(str(summary.get('not_verified_budget')))}</li>
    <li>暂停未访问：{esc(str(summary.get('paused_unvisited')))}</li>
    <li>可比价商品：{esc(str(summary.get('products_with_comparable_take_home')))}</li>
    <li>疑似破价：{esc(str(summary.get('products_possible_break')))}</li>
    <li>范围内未破价（NO_BREAK_IN_SCOPE）：{esc(str(summary.get('products_no_break_in_scope')))}
        — {esc(str(summary.get('no_break_in_scope_note')))}</li>
    <li>主展示低价线索：{esc(str(summary.get('products_primary_low_price_signal')))}</li>
    <li>待核验商品：{esc(str(summary.get('products_review_required')))}</li>
    <li>有效价格方案数：{esc(str(summary.get('valid_comparable_price_option_count')))}
        （总方案 {esc(str(summary.get('price_option_count_total')))}）</li>
  </ul>
  <h3>范围内最低可比到手价</h3>
  {lowest_block}
  <p class="muted">POSSIBLE_BREAK = 疑似破价，待人工确认；manual_verdict 留空。</p>
</div>

<div class="card">
  <h2>商品明细（一行一个候选核验结果；价格方案另表）</h2>
  <table>{rows_html(ordered_rows, prod_keys)}</table>
</div>

<div class="card">
  <h2>价格方案明细（一行一个方案）</h2>
  <table>{rows_html(price_options, opt_keys)}</table>
</div>
</body>
</html>
"""


def _latest_attempt_by_pid(
    attempts: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for att in attempts:
        pid = str(att.get("product_id") or "")
        if not pid:
            continue
        prev = latest.get(pid)
        if prev is None or int(att.get("seq") or 0) >= int(prev.get("seq") or 0):
            latest[pid] = att
    return latest


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
    candidate_source: str = SOURCE_LIVE_SEARCH,
    existing_collection_dir: Path | None = None,
    resume_job_id: str | None = None,
    backend: str | None = None,
    bsk_browser: str | None = None,
    model_scope_policy: dict[str, Any] | Path | None = None,
    retry_failed: bool = False,
    search_fn: Callable[..., dict[str, Any]] | None = None,
    verify_fn: Callable[..., dict[str, Any]] | None = None,
    job_dir: Path | None = None,
    job_id: str | None = None,
    focus_window: bool = False,
) -> dict[str, Any]:
    started = utc_now()
    cfg, errs = adapt.load_business_baseline(business_config)
    if errs or cfg is None:
        raise ValueError("; ".join(errs or ["config invalid"]))

    if isinstance(model_scope_policy, (str, Path)):
        model_scope_policy = load_model_scope_policy(Path(model_scope_policy))

    backend_norm = normalize_backend(backend)

    source = str(candidate_source or SOURCE_LIVE_SEARCH).strip().lower()
    if source not in {SOURCE_LIVE_SEARCH, SOURCE_EXISTING_COLLECTION}:
        raise ValueError(
            f"candidate-source 必须是 {SOURCE_LIVE_SEARCH}|{SOURCE_EXISTING_COLLECTION}"
        )
    if source == SOURCE_EXISTING_COLLECTION and not existing_collection_dir and not resume_job_id:
        raise ValueError("existing-collection 模式必须显式提供 --existing-collection-dir")

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

    query_explicit = bool(query and str(query).strip())
    if query_explicit:
        use_query = str(query).strip()
        query_basis = "CLI --query（显式传入）"
    else:
        use_query, query_basis = default_query_from_baseline(cfg)

    business_sku = cfg["business_sku"]
    red_line = cfg["red_line"]

    existing_dir_resolved = (
        str(Path(existing_collection_dir).resolve())
        if existing_collection_dir
        else None
    )
    source_explicit = candidate_source is not None and str(candidate_source).strip() != ""
    # argparse always passes a default; track via sentinel only when resume needs it.
    # For fingerprint: explicit CLI query/collection/backend always checked.

    resuming = bool(resume_job_id)
    prior_attempts: list[dict[str, Any]] = []
    prior_rows_by_pid: dict[str, dict[str, Any]] = {}
    prior_options: list[dict[str, Any]] = []
    prior_detail_opens = 0

    if resuming:
        job_id = str(resume_job_id)
        if job_dir is not None:
            # Caller-owned directory (e.g. batch sub-job); same job_id/progress rules.
            job_dir = Path(job_dir).resolve()
            if not job_dir.is_dir():
                raise FileNotFoundError(f"resume job 不存在: {job_dir}")
        else:
            job_dir = resolve_job_dir(job_id)
        progress_path = job_dir / "job_progress.json"
        if not progress_path.is_file():
            raise FileNotFoundError(f"缺少 job_progress.json: {progress_path}")
        prior_progress = read_json(progress_path)
        saved_fp = prior_progress.get("fingerprint") or {}
        if prior_progress.get("backend"):
            if normalize_backend(prior_progress.get("backend")) != backend_norm:
                raise RuntimeError(
                    "恢复拒绝：backend 与原任务不一致 "
                    f"saved={prior_progress.get('backend')!r} current={backend_norm!r}"
                )
        # Explicit caller overrides must match saved; omitted fields adopt saved.
        fp_query = use_query if query_explicit else (saved_fp.get("query") or use_query)
        fp_source = (
            source
            if source_explicit and query_explicit
            else (saved_fp.get("candidate_source") or prior_progress.get("candidate_source") or source)
        )
        # Prefer: if existing_collection_dir explicitly passed, use it; else saved.
        fp_existing = existing_dir_resolved or saved_fp.get("existing_collection_dir")
        if query_explicit and fp_query != saved_fp.get("query"):
            raise RuntimeError(
                "恢复拒绝：输入/红线/授权/规则版本变化，不得静默沿用旧结论。"
                f"请新建任务或显式重处理。差异: query: saved={saved_fp.get('query')!r} "
                f"current={fp_query!r}"
            )
        if existing_collection_dir is not None and existing_dir_resolved != saved_fp.get(
            "existing_collection_dir"
        ):
            raise RuntimeError(
                "恢复拒绝：existing-collection-dir 与原任务不一致，不得静默沿用旧结论。"
            )
        current_fp = build_job_fingerprint(
            cfg=cfg,
            query=fp_query,
            candidate_source=str(
                saved_fp.get("candidate_source")
                or prior_progress.get("candidate_source")
                or source
            ),
            existing_collection_dir=fp_existing,
            backend=backend_norm,
            model_scope_policy=model_scope_policy,
        )
        mismatches = fingerprint_mismatch_reasons(saved_fp, current_fp)
        if mismatches:
            raise RuntimeError(
                "恢复拒绝：输入/红线/授权/规则版本变化，不得静默沿用旧结论。"
                "请新建任务或显式重处理。差异: "
                + "; ".join(mismatches)
            )
        # Adopt saved operational identity after checks pass
        source = prior_progress.get("candidate_source") or source
        use_query = prior_progress.get("query") or use_query
        query_basis = prior_progress.get("query_generation_basis") or query_basis
        existing_dir_resolved = (
            prior_progress.get("existing_collection_dir") or existing_dir_resolved
        )
        attempts_path = job_dir / "candidate_attempts.json"
        if attempts_path.is_file():
            loaded = read_json(attempts_path)
            if isinstance(loaded, list):
                prior_attempts = loaded
        rows_path = job_dir / "product_rows.json"
        if rows_path.is_file():
            loaded_rows = read_json(rows_path)
            if isinstance(loaded_rows, list):
                for r in loaded_rows:
                    pid = str(r.get("jd_product_id") or "")
                    if pid:
                        prior_rows_by_pid[pid] = r
        opts_path = job_dir / "price_options.json"
        if opts_path.is_file():
            loaded_opts = read_json(opts_path)
            if isinstance(loaded_opts, list):
                prior_options = loaded_opts
        prior_detail_opens = int(prior_progress.get("detail_pages_opened") or 0)
        if max_verify_explicit and max_verify is not None:
            # Resume: explicit --max-verify N = N MORE navigations this run
            # (cumulative cap recorded in progress = prior opens + N).
            max_ver = prior_detail_opens + int(max_verify)
        else:
            max_ver = int(prior_progress.get("max_verify") or max_ver)
        max_cand = int(prior_progress.get("max_candidates") or max_cand)
        max_pg = int(prior_progress.get("max_pages") or max_pg)
        timeout_s = int(prior_progress.get("search_timeout_seconds") or timeout_s)
        limit = max_cand
    else:
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
    if model_scope_policy:
        write_json(job_dir / "model_scope_policy_snapshot.json", model_scope_policy)

    fingerprint = build_job_fingerprint(
        cfg=cfg,
        query=use_query,
        candidate_source=source,
        existing_collection_dir=existing_dir_resolved,
        backend=backend_norm,
        model_scope_policy=model_scope_policy,
    )
    write_json(job_dir / "job_fingerprint.json", fingerprint)

    progress: dict[str, Any] = {
        "job_id": job_id,
        "phase": "P5-B",
        "rules_version": RULES_VERSION,
        "business_sku": business_sku,
        "query": use_query,
        "query_generation_basis": query_basis,
        "candidate_source": source,
        "existing_collection_dir": existing_dir_resolved,
        "backend": backend_norm,
        "bsk_browser": bsk_browser,
        "model_scope_policy_id": (model_scope_policy or {}).get("policy_id")
        if model_scope_policy
        else None,
        "retry_failed": bool(retry_failed),
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
        "detail_pages_opened": prior_detail_opens,
        "fingerprint": fingerprint,
        "historical_anchors_not_used_for_search_or_identity": cfg.get(
            "historical_anchors"
        ),
        "resuming": resuming,
    }
    save_progress(job_dir, progress)

    verify_impl = verify_fn or p5a.verify_one

    print(f"=== PriceRadar P5-B job {job_id} ===")
    print(
        f"business_sku={business_sku} query={use_query!r} source={source} "
        f"backend={backend_norm} max_candidates={max_cand} max_pages={max_pg} "
        f"search_timeout={timeout_s}s max_verify={max_ver} resume={resuming}"
    )
    print(f"job_dir={job_dir}")

    search_result: dict[str, Any]
    if resuming and (search_dir / "candidates.json").is_file():
        candidates = read_json(search_dir / "candidates.json")
        if not isinstance(candidates, list):
            raise RuntimeError("resume: search/candidates.json 非法")
        meta_path = search_dir / "run_metadata.json"
        search_meta = read_json(meta_path) if meta_path.is_file() else {}
        if not isinstance(search_meta, dict):
            search_meta = {}
        candidates_path = search_dir / "candidates.json"
        search_result = {
            "run_id": progress.get("search_run_id") or search_dir.name,
            "run_status": search_meta.get("run_status") or "REUSED",
            "candidates_path": str(candidates_path),
            "close_ok": True,
        }
        print(f"resume: reused candidates from {candidates_path} n={len(candidates)}")
    elif source == SOURCE_EXISTING_COLLECTION:
        assert existing_collection_dir is not None or existing_dir_resolved
        coll = Path(existing_dir_resolved or existing_collection_dir)  # type: ignore[arg-type]
        candidates, search_meta, used = load_existing_collection(coll)
        if max_cand and len(candidates) > max_cand:
            _collected_n = len(candidates)
            candidates = candidates[:max_cand]
            search_meta["unique_candidates_collected"] = len(candidates)
            search_meta["candidate_count"] = len(candidates)
            mark_candidate_budget_truncation(
                search_meta, collected=_collected_n, kept=len(candidates)
            )
        # Copy pointer + materialize handoff file (no “latest”)
        write_json(
            search_dir / "existing_collection_pointer.json",
            {
                "existing_collection_dir": str(coll.resolve()),
                "candidates_file": str(used),
                "loaded_at_utc": utc_iso(),
                "note": "synthetic" if search_meta.get("synthetic") else "historical_links",
            },
        )
        candidates_path = materialize_candidates_file(search_dir, candidates)
        write_json(search_dir / "run_metadata.json", search_meta)
        search_result = {
            "run_id": coll.name,
            "run_status": search_meta.get("run_status") or "REUSED",
            "candidates_path": str(candidates_path),
            "coverage_stop_reason": search_meta.get("coverage_stop_reason"),
            "close_ok": True,
            "status_reason": "existing-collection",
        }
    else:
        candidates, search_meta, candidates_path, search_result = run_live_search(
            query=use_query,
            backend=backend_norm,
            business_config=business_config,
            max_cand=max_cand,
            max_pg=max_pg,
            timeout_s=timeout_s,
            search_dir=search_dir,
            bsk_browser=bsk_browser,
            search_fn=search_fn,
            focus_window=focus_window,
        )

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
    search_meta["candidate_source"] = source
    search_meta.setdefault("backend", backend_norm)

    # Classify with current rules (preserve explicit classification if present).
    expected_model = str(cfg.get("expected_model") or "")
    classified: list[dict[str, Any]] = []
    for c in candidates:
        classified.append(classify_candidate(c, expected_model=expected_model))
    candidates = classified
    write_json(search_dir / "candidates_classified.json", candidates)
    materialize_candidates_file(search_dir, candidates)

    progress["search_browser_released"] = True
    progress["search_close_ok"] = search_result.get("close_ok")
    progress["stage"] = "VERIFY"
    progress["search_run_id"] = search_result.get("run_id")
    progress["search_status"] = search_result.get("run_status")
    progress["search_dir"] = str(search_dir)
    progress["candidates_path"] = str(candidates_path)
    save_progress(job_dir, progress)

    product_rows: list[dict[str, Any]] = []
    price_options: list[dict[str, Any]] = list(prior_options)
    verify_results: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = list(prior_attempts)
    latest_prior = _latest_attempt_by_pid(prior_attempts)
    attempt_seq = max((int(a.get("seq") or 0) for a in attempts), default=0)

    search_status = str(search_result.get("run_status") or "")
    session_paused = False
    pause_reason = None
    detail_opens = prior_detail_opens

    if search_status in SEARCH_BLOCK_STATUSES and not candidates:
        product_rows.append(
            fallback_no_candidate_row(
                business_sku=business_sku,
                red_line=red_line,
                reason=f"搜索状态={search_status}: {search_result.get('status_reason')}",
                search_status=search_status,
            )
        )
        session_paused = True
        pause_reason = search_status
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
        if search_status in SEARCH_BLOCK_STATUSES and not resuming:
            session_paused = True
            pause_reason = search_status
            progress["session_paused"] = True
            progress["pause_reason"] = pause_reason
            save_progress(job_dir, progress)

        for cand in candidates:
            pid = str(cand.get("product_id") or "").strip()
            if not pid:
                continue
            rank = cand.get("rank")
            plan = candidate_detail_plan(
                cand, cfg=cfg, model_scope_policy=model_scope_policy
            )

            # Resume: skip completed; optionally retry failed; resume pause/budget.
            prior_att = latest_prior.get(pid)
            prior_row = prior_rows_by_pid.get(pid)
            if resuming and prior_att:
                st = str(prior_att.get("status") or "")
                match = str(prior_att.get("match_status") or "")
                if st == "COMPLETED" or (
                    st == "SKIPPED" and match == "MODEL_MISMATCH_SKIPPED"
                ):
                    if prior_row:
                        product_rows.append(prior_row)
                    progress["completed_product_ids"].append(pid)
                    continue
                if st in TERMINAL_FAIL_STATUSES and not retry_failed:
                    if prior_row:
                        product_rows.append(prior_row)
                    progress["completed_product_ids"].append(pid)
                    continue
                # pause/budget pending → fall through to re-process
                if match in RESUME_PENDING_MATCH or st in {
                    SESSION_PAUSE_TASK,
                    STATUS_NOT_VERIFIED_BUDGET,
                }:
                    pass  # continue processing
                elif st in TERMINAL_OK_STATUSES and st != STATUS_NOT_VERIFIED_BUDGET:
                    if prior_row:
                        product_rows.append(prior_row)
                    progress["completed_product_ids"].append(pid)
                    continue

            if session_paused:
                row = paused_placeholder_row(
                    business_sku=business_sku,
                    red_line=red_line,
                    candidate=cand,
                    reason=f"会话已暂停({pause_reason})，未访问本候选",
                    plan=plan,
                )
                product_rows.append(row)
                attempt_seq += 1
                att = {
                    "product_id": pid,
                    "seq": attempt_seq,
                    "attempt_id": new_attempt_id(attempt_seq),
                    "status": SESSION_PAUSE_TASK,
                    "match_status": "PAUSED_BEFORE_VISIT",
                    "result_path": None,
                    "evidence_checked_at": row.get("checked_at"),
                    "discovered_at": cand.get("discovered_at_utc"),
                    "navigation_consumed": False,
                    "synthetic": bool(cand.get("synthetic")),
                }
                attempts.append(att)
                progress["completed_product_ids"].append(pid)
                save_progress(job_dir, progress)
                write_json(job_dir / "product_rows.json", product_rows)
                write_json(job_dir / "candidate_attempts.json", attempts)
                continue

            if plan.get("classification_skip"):
                row = classification_skip_row(
                    business_sku=business_sku,
                    red_line=red_line,
                    candidate=cand,
                    plan=plan,
                )
                product_rows.append(row)
                attempt_seq += 1
                att = {
                    "product_id": pid,
                    "seq": attempt_seq,
                    "attempt_id": new_attempt_id(attempt_seq),
                    "status": "SKIPPED",
                    "match_status": "MODEL_MISMATCH_SKIPPED",
                    "result_path": None,
                    "evidence_checked_at": row.get("checked_at"),
                    "discovered_at": cand.get("discovered_at_utc"),
                    "navigation_consumed": False,
                    "classification": plan.get("gate", {}).get("classification"),
                    "synthetic": bool(cand.get("synthetic")),
                }
                attempts.append(att)
                progress["completed_product_ids"].append(pid)
                save_progress(job_dir, progress)
                write_json(job_dir / "product_rows.json", product_rows)
                write_json(job_dir / "candidate_attempts.json", attempts)
                continue

            needs_detail = bool(plan.get("needs_detail"))
            if needs_detail and detail_opens >= max_ver:
                row = not_verified_budget_row(
                    business_sku=business_sku,
                    red_line=red_line,
                    candidate=cand,
                    max_verify=max_ver,
                    plan=plan,
                )
                product_rows.append(row)
                attempt_seq += 1
                att = {
                    "product_id": pid,
                    "seq": attempt_seq,
                    "attempt_id": new_attempt_id(attempt_seq),
                    "status": STATUS_NOT_VERIFIED_BUDGET,
                    "match_status": STATUS_NOT_VERIFIED_BUDGET,
                    "result_path": None,
                    "evidence_checked_at": row.get("checked_at"),
                    "discovered_at": cand.get("discovered_at_utc"),
                    "navigation_consumed": False,
                    "synthetic": bool(cand.get("synthetic")),
                }
                attempts.append(att)
                progress["completed_product_ids"].append(pid)
                save_progress(job_dir, progress)
                write_json(job_dir / "product_rows.json", product_rows)
                write_json(job_dir / "candidate_attempts.json", attempts)
                continue

            attempt_seq += 1
            attempt_id = new_attempt_id(attempt_seq)
            verify_dir = cand_verify_root / f"rank{rank}_{pid}" / attempt_id
            print(
                f"--- verify rank={rank} id={pid} attempt={attempt_id} "
                f"detail_budget={detail_opens}/{max_ver} "
                f"needs_detail={needs_detail} backend={backend_norm} ---"
            )
            try:
                vres = verify_impl(
                    business_config=business_config,
                    candidates_path=candidates_path,
                    product_id=pid,
                    out_dir=verify_dir,
                    model_scope_policy=model_scope_policy,
                    backend=backend_norm,
                    bsk_browser=bsk_browser,
                )
            except TypeError:
                # Older injectables may not accept new kwargs.
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

            match = str(vres.get("match_status") or "")
            navigated = False
            if needs_detail and match != "MODEL_MISMATCH_SKIPPED":
                detail_opens += 1
                navigated = True
                progress["detail_pages_opened"] = detail_opens

            verify_results.append(vres)
            opts = build_price_options(
                business_sku=business_sku,
                product_id=pid,
                search_rank=rank,
                clues=list(vres.get("price_clues") or []),
            )
            # On retry, drop prior options for this pid then append new.
            if retry_failed and prior_att:
                price_options = [
                    o for o in price_options if str(o.get("jd_product_id")) != pid
                ]
            price_options.extend(opts)
            row = product_row_from_verify(
                business_sku=business_sku,
                red_line=red_line,
                candidate=cand,
                verify=vres,
                options=opts,
                plan=plan,
                attempt_id=attempt_id,
            )
            product_rows.append(row)

            att = {
                "product_id": pid,
                "seq": attempt_seq,
                "attempt_id": attempt_id,
                "status": vres.get("task_status"),
                "match_status": match,
                "result_path": str(verify_dir),
                "evidence_checked_at": row.get("checked_at"),
                "discovered_at": cand.get("discovered_at_utc"),
                "navigation_consumed": navigated,
                "screenshot_link": row.get("screenshot_link"),
                "classification": plan.get("gate", {}).get("classification"),
                "synthetic": bool(cand.get("synthetic")),
            }
            attempts.append(att)

            progress["completed_product_ids"].append(pid)
            progress["last_verify_status"] = vres.get("task_status")
            save_progress(job_dir, progress)
            write_json(job_dir / "product_rows.json", product_rows)
            write_json(job_dir / "price_options.json", price_options)
            write_json(job_dir / "candidate_attempts.json", attempts)

            task = str(vres.get("task_status") or "")
            if task == SESSION_PAUSE_TASK or match in SESSION_PAUSE_MATCH:
                session_paused = True
                pause_reason = match or task
                progress["session_paused"] = True
                progress["pause_reason"] = pause_reason
                save_progress(job_dir, progress)

    def _finalize_outputs(*, stage: str = "DONE") -> dict[str, Any]:
        # Deduplicate product_rows by jd_product_id keeping last
        dedup: dict[str, dict[str, Any]] = {}
        ordered_ids: list[str] = []
        passthrough: list[dict[str, Any]] = []
        for r in product_rows:
            pid = str(r.get("jd_product_id") or "")
            if not pid:
                passthrough.append(r)
                continue
            if pid not in dedup:
                ordered_ids.append(pid)
            dedup[pid] = r
        final_rows = passthrough + [dedup[i] for i in ordered_ids]

        job_execution_complete = stage in {"DONE", "PAUSED", "BUDGET_REACHED"}
        summary_local = build_sku_summary(
            business_sku=business_sku,
            product_rows=final_rows,
            price_options=price_options,
            search_meta=search_meta,
            query=use_query,
            query_basis=query_basis,
            limit=limit,
            max_verify=max_ver,
            detail_pages_opened_actual=detail_opens,
            session_paused=session_paused,
            job_execution_complete=job_execution_complete,
        )
        summary_local["job_id"] = job_id
        summary_local["started_at_utc"] = utc_iso(started)
        summary_local["ended_at_utc"] = utc_iso()
        summary_local["job_dir"] = str(job_dir)
        summary_local["search_dir"] = str(search_dir)
        summary_local["backend"] = backend_norm
        summary_local["candidate_source"] = source
        summary_local["partial"] = stage != "DONE" or not summary_local.get(
            "all_candidates_verified"
        )
        summary_local["pause_reason"] = pause_reason
        write_json(job_dir / "product_rows.json", final_rows)
        write_csv(job_dir / "product_rows.csv", final_rows, PRODUCT_ROW_FIELDS)
        write_json(job_dir / "price_options.json", price_options)
        write_csv(job_dir / "price_options.csv", price_options, PRICE_OPTION_FIELDS)
        write_json(job_dir / "candidate_attempts.json", attempts)
        write_json(job_dir / "sku_summary.json", summary_local)
        report_html = render_report_html(
            job_id=job_id,
            summary=summary_local,
            product_rows=final_rows,
            price_options=price_options,
        )
        (job_dir / "report.html").write_text(report_html, encoding="utf-8")
        progress["stage"] = stage
        progress["ended_at_utc"] = summary_local["ended_at_utc"]
        progress["summary_path"] = str(job_dir / "sku_summary.json")
        progress["session_paused"] = session_paused
        progress["pause_reason"] = pause_reason
        progress["detail_pages_opened"] = detail_opens
        save_progress(job_dir, progress)
        return summary_local

    final_stage = "DONE"
    if session_paused:
        final_stage = "PAUSED"
    elif any(
        r.get("task_status") == STATUS_NOT_VERIFIED_BUDGET for r in product_rows
    ):
        final_stage = "BUDGET_REACHED"

    try:
        summary = _finalize_outputs(stage=final_stage)
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
        f"paused={summary.get('candidates_paused_unvisited')} "
        f"job_done={summary.get('job_execution_complete')} "
        f"all_verified={summary.get('all_candidates_verified')}"
    )
    print(f"report={job_dir / 'report.html'}")

    return {
        "job_id": job_id,
        "job_dir": job_dir,
        "summary": summary,
        "product_rows": summary and read_json(job_dir / "product_rows.json"),
        "price_options": price_options,
        "attempts": attempts,
        "search_result": {
            "run_id": search_result.get("run_id"),
            "run_status": search_result.get("run_status"),
            "candidates_path": str(candidates_path),
            "candidate_count": len(candidates),
            "coverage_stop_reason": search_meta.get("coverage_stop_reason"),
            "candidate_source": source,
            "backend": backend_norm,
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
        backend=BACKEND_P2D,
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
    if calls != ["111", "333"]:
        failures.append(f"verify call order wrong (222 classify-skip no nav): {calls}")

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
        backend=BACKEND_P2D,
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
        backend=BACKEND_P2D,
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
        backend=BACKEND_P2D,
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
        backend=BACKEND_P2D,
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


    # --- v3 orchestration (synthetic; not JD live) ---
    synth_root = JOB_ROOT / f"_synthetic_offline_{utc_stamp()}_{secrets.token_hex(2)}"
    synth_root.mkdir(parents=True, exist_ok=True)
    synth_collect = synth_root / "synthetic_collection"
    synth_collect.mkdir(parents=True, exist_ok=True)
    synth_cands = [
        {
            "product_id": "s1001",
            "jd_product_id": "s1001",
            "rank": 1,
            "title": "清风 B913A 20包整箱 synthetic",
            "normalized_url": "https://item.jd.com/s1001.html",
            "observed_url": "https://item.jd.com/s1001.html",
            "discovered_at_utc": "2026-01-01T00:00:00+00:00",
            "synthetic": True,
        },
        {
            "product_id": "s1002",
            "jd_product_id": "s1002",
            "rank": 2,
            "title": "清风 B913AC 近型号 synthetic",
            "normalized_url": "https://item.jd.com/s1002.html",
            "observed_url": "https://item.jd.com/s1002.html",
            "discovered_at_utc": "2026-01-01T00:00:00+00:00",
            "synthetic": True,
        },
        {
            "product_id": "s1003",
            "jd_product_id": "s1003",
            "rank": 3,
            "title": "清风 B913A 箱装 synthetic",
            "normalized_url": "https://item.jd.com/s1003.html",
            "observed_url": "https://item.jd.com/s1003.html",
            "discovered_at_utc": "2026-01-01T00:00:00+00:00",
            "synthetic": True,
        },
        {
            "product_id": "s1004",
            "jd_product_id": "s1004",
            "rank": 4,
            "title": "清风 B913A 剩余 synthetic",
            "normalized_url": "https://item.jd.com/s1004.html",
            "observed_url": "https://item.jd.com/s1004.html",
            "discovered_at_utc": "2026-01-01T00:00:00+00:00",
            "synthetic": True,
        },
    ]
    write_json(synth_collect / "all_candidates.json", synth_cands)
    write_json(
        synth_collect / "coverage.json",
        {
            "query": "清风 B913A",
            "backend": "bsk",
            "run_status": "SUCCESS",
            "overall_integrity": "COMPLETE_EXPLAINABLE",
            "unique_candidates": len(synth_cands),
            "pages_visited": [1],
            "completeness": {
                "integrity": "COMPLETE",
                "stop_reason": "SYNTHETIC_FIXTURE",
                "end_page_evidence": ["synthetic_end"],
                "restricted": False,
            },
            "started_at_utc": "2026-01-01T00:00:00+00:00",
            "synthetic": True,
        },
    )

    policy = load_model_scope_policy(
        PROJECT_ROOT / "config" / "policies" / "b913a_model_scope_include_other_20261027.json"
    )
    nav_calls: list[str] = []

    def fake_verify_v3(**kwargs):
        pid = str(kwargs["product_id"])
        nav_calls.append(pid)
        out = Path(kwargs["out_dir"])
        out.mkdir(parents=True, exist_ok=True)
        # Evidence path must be attempt-local (no cross-product shots)
        shot = out / "shot.png"
        shot.write_bytes(b"synthetic-not-jd")
        if pid == "s1001":
            # First detail hits rate-limit → pause; no further navigations
            res = {
                "task_status": SESSION_PAUSE_TASK,
                "match_status": "DETAIL_RATE_LIMITED",
                "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                "system_verdict_zh": "详情限流（synthetic）",
                "manual_verdict": None,
                "price_clues": [],
                "ended_at_utc": utc_iso(),
                "verify_dir": str(out),
                "screenshot_dir": str(out),
                "capture": {"screenshot_path": str(shot)},
                "candidate": {"title": "清风 B913A 20包整箱 synthetic", "product_id": pid},
            }
            write_json(out / "verify_result.json", res)
            return res
        if pid == "s1003":
            res = {
                "task_status": "COMPLETED",
                "match_status": "PACKAGING_PASSED",
                "system_verdict": take_home.SYSTEM_NO_BREAK_IN_SCOPE,
                "system_verdict_zh": take_home.SYSTEM_VERDICT_ZH[
                    take_home.SYSTEM_NO_BREAK_IN_SCOPE
                ],
                "manual_verdict": None,
                "page_primary_price": "130.00",
                "packaging_match": "PASSED",
                "box_comparable": True,
                "observed_model": "B913A",
                "observed_spec": "200张*20包",
                "price_clues": [
                    {
                        "price_type": take_home.PRICE_TYPE_DECLARED_CONDITIONAL,
                        "amount": "130.00",
                        "conditions": "synthetic条件",
                        "normalized_price": "130.00",
                        "system_verdict": take_home.SYSTEM_NO_BREAK_IN_SCOPE,
                        "system_verdict_zh": take_home.SYSTEM_VERDICT_ZH[
                            take_home.SYSTEM_NO_BREAK_IN_SCOPE
                        ],
                        "manual_verdict": None,
                        "review_reasons": [],
                    }
                ],
                "take_home_rollup": {"take_home_clue_count": 1, "has_possible_break": False},
                "ended_at_utc": utc_iso(),
                "verify_dir": str(out),
                "screenshot_dir": str(out),
                "capture": {"screenshot_path": str(shot)},
                "candidate": {"title": "清风 B913A 箱装 synthetic", "product_id": pid},
                "url_adapt": {
                    "adapted_url": f"https://item.jd.com/{pid}.html",
                    "final_url_sanitized": f"https://item.jd.com/{pid}.html",
                },
            }
            write_json(out / "verify_result.json", res)
            return res
        if pid == "s1004":
            res = {
                "task_status": "FAILED",
                "match_status": "OCR_FAILED",
                "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                "system_verdict_zh": "待核验",
                "manual_verdict": None,
                "price_clues": [],
                "ended_at_utc": utc_iso(),
                "verify_dir": str(out),
                "screenshot_dir": str(out),
                "capture": {"screenshot_path": str(shot)},
                "candidate": {"title": "清风 B913A 剩余 synthetic", "product_id": pid},
            }
            write_json(out / "verify_result.json", res)
            return res
        failures.append(f"v3 unexpected nav pid {pid}")
        return {"task_status": "FAILED", "price_clues": [], "verify_dir": str(out)}

    job_v3 = run_sku_job(
        business_config=cfg_path,
        query="清风 B913A",
        candidate_source=SOURCE_EXISTING_COLLECTION,
        existing_collection_dir=synth_collect,
        backend=BACKEND_P2D,
        max_candidates=4,
        max_verify=3,
        max_candidates_explicit=True,
        max_verify_explicit=True,
        model_scope_policy=policy,
        verify_fn=fake_verify_v3,
        job_dir=synth_root / "job_existing",
        job_id=(synth_root / "job_existing").name,
    )
    # Candidate source accurate
    if job_v3["search_result"].get("candidate_source") != SOURCE_EXISTING_COLLECTION:
        failures.append("v3: candidate_source not existing-collection")
    if not (job_v3["summary"].get("search_coverage") or {}).get("reuse_mode"):
        failures.append("v3: reuse_mode missing for existing-collection")
    # Discovery vs check time
    rows_v3 = {str(r["jd_product_id"]): r for r in job_v3["product_rows"]}
    if rows_v3.get("s1001", {}).get("discovered_at") != "2026-01-01T00:00:00+00:00":
        failures.append("v3: discovery time not preserved from collection")
    # Classification: OTHER authorized should need detail; without pause would nav
    # But rate-limit on first detail → zero further navigations
    if nav_calls != ["s1001"]:
        failures.append(f"v3: after first rate-limit expect only s1001 nav, got {nav_calls}")
    if rows_v3.get("s1003", {}).get("match_status") != "PAUSED_BEFORE_VISIT":
        failures.append("v3: s1003 must be pause-unvisited after rate-limit")
    if rows_v3.get("s1004", {}).get("match_status") != "PAUSED_BEFORE_VISIT":
        failures.append("v3: s1004 must be pause-unvisited")
    # s1002 OTHER with policy → would need detail, but paused before visit
    if rows_v3.get("s1002", {}).get("classification") != model_match.CLASS_OTHER:
        failures.append("v3: s1002 should classify OTHER_MODEL")
    if rows_v3.get("s1002", {}).get("match_status") != "PAUSED_BEFORE_VISIT":
        failures.append("v3: authorized OTHER still paused-unvisited after rate-limit")
    if job_v3["summary"].get("detail_pages_opened") != 1:
        failures.append(
            f"v3: detail_pages_opened should be 1, got {job_v3['summary'].get('detail_pages_opened')}"
        )
    if job_v3["summary"].get("job_execution_complete") is not True:
        failures.append("v3: job_execution_complete should be true even when paused")
    if job_v3["summary"].get("all_candidates_verified") is not False:
        failures.append("v3: all_candidates_verified must be false when paused remain")

    # No duplicate product rows
    ids = [r.get("jd_product_id") for r in job_v3["product_rows"] if r.get("jd_product_id")]
    if len(ids) != len(set(ids)):
        failures.append("v3: duplicate product rows")

    # Evidence not cross-used: paused rows have empty screenshot
    for pid in ("s1002", "s1003", "s1004"):
        if rows_v3[pid].get("screenshot_link"):
            failures.append(f"v3: {pid} must not inherit screenshot from s1001")

    # Resume: completed/paused — only unfinished; no auto retry of ordinary fail
    # Build a job with one completed + one failed + one budget skip, then resume
    synth2 = synth_root / "synthetic_collection2"
    synth2.mkdir(parents=True, exist_ok=True)
    c2 = [
        {
            "product_id": "r1",
            "jd_product_id": "r1",
            "rank": 1,
            "title": "清风 B913A r1 synthetic",
            "normalized_url": "https://item.jd.com/r1.html",
            "discovered_at_utc": "2026-02-02T00:00:00+00:00",
            "synthetic": True,
        },
        {
            "product_id": "r2",
            "jd_product_id": "r2",
            "rank": 2,
            "title": "清风 B913A r2 synthetic",
            "normalized_url": "https://item.jd.com/r2.html",
            "discovered_at_utc": "2026-02-02T00:00:00+00:00",
            "synthetic": True,
        },
        {
            "product_id": "r3",
            "jd_product_id": "r3",
            "rank": 3,
            "title": "清风 B913A r3 synthetic",
            "normalized_url": "https://item.jd.com/r3.html",
            "discovered_at_utc": "2026-02-02T00:00:00+00:00",
            "synthetic": True,
        },
    ]
    write_json(synth2 / "all_candidates.json", c2)
    write_json(
        synth2 / "coverage.json",
        {
            "query": "清风 B913A",
            "run_status": "PARTIAL",
            "overall_integrity": "PARTIAL",
            "completeness": {
                "integrity": "PARTIAL",
                "restricted": True,
                "stop_reason": "SYNTHETIC_PARTIAL",
                "end_page_evidence": ["synthetic_partial"],
            },
            "started_at_utc": "2026-02-02T00:00:00+00:00",
            "synthetic": True,
        },
    )
    resume_nav: list[str] = []

    def fake_verify_resume(**kwargs):
        pid = str(kwargs["product_id"])
        resume_nav.append(pid)
        out = Path(kwargs["out_dir"])
        out.mkdir(parents=True, exist_ok=True)
        if pid == "r1":
            return {
                "task_status": "COMPLETED",
                "match_status": "PACKAGING_PASSED",
                "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                "system_verdict_zh": "到手价待核验",
                "manual_verdict": None,
                "page_primary_price": "140.00",
                "packaging_match": "PASSED",
                "box_comparable": True,
                "price_clues": [],
                "take_home_rollup": {"take_home_clue_count": 0},
                "ended_at_utc": utc_iso(),
                "verify_dir": str(out),
                "candidate": {"title": "清风 B913A r1 synthetic", "product_id": pid},
                "url_adapt": {
                    "adapted_url": "https://item.jd.com/r1.html",
                    "final_url_sanitized": "https://item.jd.com/r1.html",
                },
            }
        if pid == "r2":
            return {
                "task_status": "FAILED",
                "match_status": "OCR_FAILED",
                "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                "manual_verdict": None,
                "price_clues": [],
                "ended_at_utc": utc_iso(),
                "verify_dir": str(out),
                "candidate": {"title": "清风 B913A r2 synthetic", "product_id": pid},
            }
        failures.append(f"resume unexpected first-pass pid {pid}")
        return {"task_status": "FAILED", "price_clues": [], "verify_dir": str(out)}

    resume_job_id = f"synth_resume_{utc_stamp()}_{secrets.token_hex(2)}"
    job_a = run_sku_job(
        business_config=cfg_path,
        query="清风 B913A",
        candidate_source=SOURCE_EXISTING_COLLECTION,
        existing_collection_dir=synth2,
        backend=BACKEND_P2D,
        max_candidates=3,
        max_verify=1,
        max_candidates_explicit=True,
        max_verify_explicit=True,
        verify_fn=fake_verify_resume,
        job_dir=JOB_ROOT / resume_job_id,
        job_id=resume_job_id,
    )
    # max_verify=1 → r1 completed, r2/r3 budget (r2 never failed in first pass if budget)
    # Actually order: r1 needs detail → open; then r2/r3 budget. So resume_nav=["r1"]
    if resume_nav != ["r1"]:
        failures.append(f"v3 budget first pass nav should be [r1], got {resume_nav}")
    if (job_a["summary"].get("search_coverage") or {}).get("partial_or_restricted") is not True:
        failures.append("v3: PARTIAL collection must flag partial_or_restricted")
    # Force a failed attempt into progress for r2 by second job with higher budget
    resume_nav.clear()

    def fake_verify_resume2(**kwargs):
        pid = str(kwargs["product_id"])
        resume_nav.append(pid)
        out = Path(kwargs["out_dir"])
        out.mkdir(parents=True, exist_ok=True)
        if pid == "r2":
            return {
                "task_status": "FAILED",
                "match_status": "OCR_FAILED",
                "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                "manual_verdict": None,
                "price_clues": [],
                "ended_at_utc": utc_iso(),
                "verify_dir": str(out),
                "candidate": {"title": "清风 B913A r2 synthetic", "product_id": pid},
            }
        if pid == "r3":
            return {
                "task_status": "COMPLETED",
                "match_status": "PACKAGING_PASSED",
                "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                "manual_verdict": None,
                "page_primary_price": "150.00",
                "packaging_match": "PASSED",
                "box_comparable": True,
                "price_clues": [],
                "take_home_rollup": {"take_home_clue_count": 0},
                "ended_at_utc": utc_iso(),
                "verify_dir": str(out),
                "candidate": {"title": "清风 B913A r3 synthetic", "product_id": pid},
                "url_adapt": {
                    "adapted_url": "https://item.jd.com/r3.html",
                    "final_url_sanitized": "https://item.jd.com/r3.html",
                },
            }
        failures.append(f"resume2 unexpected pid {pid}")
        return {"task_status": "FAILED", "price_clues": [], "verify_dir": str(out)}

    # Resume: explicit --max-verify N = N MORE navigations this run (cumulative cap).
    # Job already opened 1 detail (r1). Resume +1 → only r2; resume +1 again → only r3.
    prog_path = JOB_ROOT / resume_job_id / "job_progress.json"
    if int(read_json(prog_path).get("detail_pages_opened") or 0) != 1:
        failures.append("v3: precondition: 1 detail opened before resume")
    for step, expect_nav, expect_opened in ((1, ["r2"], 2), (2, ["r3"], 3)):
        resume_nav.clear()
        job_b = run_sku_job(
            business_config=cfg_path,
            query="清风 B913A",
            candidate_source=SOURCE_EXISTING_COLLECTION,
            existing_collection_dir=synth2,
            backend=BACKEND_P2D,
            resume_job_id=resume_job_id,
            max_verify=1,
            max_verify_explicit=True,
            verify_fn=fake_verify_resume2,
        )
        if resume_nav != expect_nav:
            failures.append(
                f"v3: resume step{step} (+1) should nav {expect_nav}, got {resume_nav}"
            )
        pg = read_json(prog_path)
        if int(pg.get("detail_pages_opened") or 0) != expect_opened or int(
            pg.get("max_verify") or 0
        ) != expect_opened:
            failures.append(
                f"v3: resume step{step} cumulative opens/cap should be {expect_opened}, got "
                f"{pg.get('detail_pages_opened')}/{pg.get('max_verify')}"
            )
    # Ordinary failure not auto-retried: run again without retry_failed
    resume_nav.clear()
    job_c = run_sku_job(
        business_config=cfg_path,
        query="清风 B913A",
        candidate_source=SOURCE_EXISTING_COLLECTION,
        existing_collection_dir=synth2,
        backend=BACKEND_P2D,
        resume_job_id=resume_job_id,
        verify_fn=fake_verify_resume2,
    )
    if resume_nav:
        failures.append(f"v3: without --retry-failed must not re-nav failed, got {resume_nav}")
    # Failed attempts retained
    attempts = read_json(JOB_ROOT / resume_job_id / "candidate_attempts.json")
    r2_attempts = [a for a in attempts if a.get("product_id") == "r2"]
    if len(r2_attempts) < 1:
        failures.append("v3: failed attempt for r2 must be retained")

    # Input change → refuse silent reuse
    try:
        run_sku_job(
            business_config=cfg_path,
            query="清风 B913A CHANGED",
            candidate_source=SOURCE_EXISTING_COLLECTION,
            existing_collection_dir=synth2,
            backend=BACKEND_P2D,
            resume_job_id=resume_job_id,
            verify_fn=fake_verify_resume2,
        )
        failures.append("v3: fingerprint/query change must refuse resume")
    except RuntimeError as exc:
        if "恢复拒绝" not in str(exc):
            failures.append(f"v3: unexpected resume refusal text: {exc}")

    # Write offline deliverable pointer
    write_json(
        synth_root / "OFFLINE_ORCHESTRATION_RESULT.json",
        {
            "synthetic": True,
            "note": "synthetic offline orchestration; not JD live verification",
            "job_existing": str(job_v3["job_dir"]),
            "job_resume": str(JOB_ROOT / resume_job_id),
            "report_existing": str(Path(job_v3["job_dir"]) / "report.html"),
            "report_resume": str(JOB_ROOT / resume_job_id / "report.html"),
        },
    )
    print(f"v3 synthetic offline dir: {synth_root}")


    # candidate budget truncation must stay visible as partial coverage (synthetic)
    _m = {"overall_integrity": "COMPLETE_EXPLAINABLE", "coverage_stop_reason": "NEXT_DISABLED"}
    mark_candidate_budget_truncation(_m, collected=30, kept=20)
    if (
        _m["overall_integrity"] != "PARTIAL"
        or _m["coverage_stop_reason"] != "HIT_CANDIDATE_BUDGET"
        or _m["source_overall_integrity"] != "COMPLETE_EXPLAINABLE"
        or _m["source_stop_reason"] != "NEXT_DISABLED"
        or _m["candidates_collected_before_cap"] != 30
    ):
        failures.append(f"candidate budget truncation must be partial with real stop reason: {_m}")

    print("=== P5-B offline self-check ===")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: all offline checks passed")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="P5-B single SKU: live-search|existing-collection → classify → verify → report → resume"
    )
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
        "--candidate-source",
        choices=[SOURCE_LIVE_SEARCH, SOURCE_EXISTING_COLLECTION],
        default=SOURCE_LIVE_SEARCH,
        help="live-search=本次搜索；existing-collection=显式既有采集目录",
    )
    p.add_argument(
        "--existing-collection-dir",
        type=Path,
        default=None,
        help="既有采集目录（existing-collection 必填；禁止自动选 latest）",
    )
    p.add_argument(
        "--resume-job-id",
        default=None,
        help="按明确 job_id 恢复（完成项不重抓；暂停项可续；失败需 --retry-failed）",
    )
    p.add_argument(
        "--backend",
        choices=[BACKEND_P2D, BACKEND_BSK],
        default=None,
        help="详情/搜索后端：p2d|bsk（必填；不自动切换）",
    )
    p.add_argument(
        "--bsk-browser",
        default=None,
        help="BSK 浏览器实例 id/label（多实例时显式指定）",
    )
    p.add_argument(
        "--focus-window",
        action="store_true",
        help="BSK live-search: open the Agent Window in the foreground (default: background)",
    )
    p.add_argument(
        "--model-scope-policy",
        type=Path,
        default=None,
        help="显式 OTHER 授权策略 JSON（仅读取；不改写真实型号）",
    )
    p.add_argument(
        "--retry-failed",
        action="store_true",
        help="恢复时显式重试普通失败（默认不自动重试；旧失败尝试保留）",
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
        help="Max detail navigations (default 5; classification-skip does not consume). With --resume-job-id: N MORE navigations this run (cumulative cap = already opened + N)",
    )
    p.add_argument("--self-check", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_check:
        return run_offline_self_checks()
    if not args.backend and not args.resume_job_id:
        print("参数/配置错误: 必须显式指定 --backend p2d|bsk（不自动切换）")
        return 2
    if (
        args.candidate_source == SOURCE_EXISTING_COLLECTION
        and not args.existing_collection_dir
        and not args.resume_job_id
    ):
        print("参数/配置错误: existing-collection 需要 --existing-collection-dir")
        return 2
    try:
        backend = args.backend
        if args.resume_job_id and not backend:
            # Resume may omit backend only if job_progress already recorded it;
            # run_sku_job still requires an explicit value — load from progress.
            prog = read_json(resolve_job_dir(args.resume_job_id) / "job_progress.json")
            backend = prog.get("backend")
            if not backend:
                print("参数/配置错误: resume 任务缺少 backend，请显式传入 --backend")
                return 2
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
            candidate_source=args.candidate_source,
            existing_collection_dir=args.existing_collection_dir,
            resume_job_id=args.resume_job_id,
            backend=backend,
            bsk_browser=args.bsk_browser,
            focus_window=bool(args.focus_window),
            model_scope_policy=args.model_scope_policy,
            retry_failed=bool(args.retry_failed),
        )
    except ValueError as exc:
        print(f"参数/配置错误: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"运行失败: {type(exc).__name__}: {exc}")
        return 1
    summary = result.get("summary") or {}
    if summary.get("candidates_paused_unvisited") or summary.get("session_paused"):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
