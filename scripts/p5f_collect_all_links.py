"""
P5-F: collect-all search result links (list only — no detail / OCR).

Why prior jobs only kept a few candidates (code evidence):
- DEFAULT_MAX_CANDIDATES=20 / often CLI 5–10; same-page ~30 cards truncated in
  dedupe_candidates / merge_first_seen_candidates.
- DEFAULT_MAX_PAGES=2 and DEFAULT_SEARCH_TIMEOUT_SECONDS=60 stop coverage early.
- Phase C pagination probe saw「下一页/共12页」but did not confirm page 2
  (ancestor click → timeout_no_progress, identical IDs).

collect-all: no soft 5/10/20 truncate; walk reachable result pages with
safety max_pages + total timeout → PARTIAL if budget hits; COMPLETE only with
explicit last-page evidence.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import importlib.util
import json
import secrets
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
PROFILE_DIR = (PROJECT_ROOT / "browser_profiles" / "jd_persistent").resolve()
COLLECT_ROOT = (PROJECT_ROOT / "artifacts" / "collect_all_runs").resolve()

RULES_VERSION = "p5f-collect-all-v3-title"
MODE = "COLLECT_ALL_LINKS"
BACKEND_PLAYWRIGHT = "playwright"
BACKEND_BSK = "bsk"

# Safety defaults (anti-loop). Observed pager text is evidence, never hard-coded.
DEFAULT_MAX_PAGES = 30
DEFAULT_SEARCH_TIMEOUT_SECONDS = 900
# Soft safety only — reaching this is PARTIAL (HIT_SAFETY_CANDIDATE_CAP), not COMPLETE.
SAFETY_CANDIDATE_CAP = 2000
# Per-page scroll-while-collect (virtual list).
PAGE_MAX_SCROLLS = 16
PAGE_SCROLL_PAUSE_MS = 700
PAGE_STABLE_ROUNDS_NEEDED = 2
PAGE_EXTRACT_LIMIT = 500  # per extract pass; accumulate across scrolls
LOCK_NAME = "collect.lock"


def load_sibling(stem: str) -> Any:
    path = SCRIPTS_DIR / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(f"priceradar_{stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


p3a = load_sibling("p3a_jd_search")
adapt = load_sibling("priceradar_candidate_adapt")
bsk_rt = load_sibling("p5_bsk_runtime")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat(timespec="seconds")


def utc_stamp() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%SZ")


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: p3a.csv_guard(row.get(k)) for k in fieldnames})


class CollectLock:
    def __init__(self, run_dir: Path):
        self.path = run_dir / LOCK_NAME
        self.held = False

    def acquire(self) -> None:
        if self.path.exists():
            try:
                meta = read_json(self.path)
            except (OSError, json.JSONDecodeError):
                meta = {}
            raise RuntimeError(f"收集任务锁已存在: {self.path} meta={meta}")
        write_json_atomic(
            self.path, {"acquired_at_utc": utc_iso(), "note": "collect-all exclusive"}
        )
        self.held = True

    def release(self) -> None:
        if self.held and self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass
        self.held = False


def tag_title_model(title: str, expected_model: str | None) -> dict[str, Any]:
    """Title classification via shared complete-model matcher. Not detail verification."""
    if not expected_model:
        return {
            "model_tag": "MODEL_TAG_SKIPPED_NO_EXPECTED",
            "model_tag_zh": "未提供期望型号，未打标签",
            "model_match": "未知",
            "match_reason": "no_expected_model",
            "classification": "MULTI_MODEL_OR_UNKNOWN",
            "near_model_token": None,
            "detail_verification_required": True,
            "red_line_applicable": False,
        }
    result = adapt.classify_title_models(title or "", expected_model=expected_model)
    classification = result.get("classification")
    if classification == "TARGET_MODEL_CANDIDATE":
        return {
            "model_tag": "TITLE_MODEL_HIT",
            "model_tag_zh": "标题完整型号候选（仅线索，非详情核验；红线不可用）",
            "model_match": "精确命中",
            "match_reason": result.get("reason"),
            "classification": classification,
            "near_model_token": None,
            "observed_model_tokens": result.get("observed_model_tokens"),
            "detail_verification_required": True,
            "red_line_applicable": False,
        }
    if classification == "OTHER_MODEL":
        return {
            "model_tag": "TITLE_NEAR_MODEL",
            "model_tag_zh": "明确其他型号（不适用目标红线）",
            "model_match": "明确近型号",
            "match_reason": result.get("reason"),
            "classification": classification,
            "near_model_token": result.get("conflict_token"),
            "observed_model_tokens": result.get("observed_model_tokens"),
            "detail_verification_required": False,
            "red_line_applicable": False,
        }
    return {
        "model_tag": "TITLE_MODEL_UNKNOWN",
        "model_tag_zh": "多型号/歧义/无型号/兼容否定（待核验，不丢弃）",
        "model_match": "未知",
        "match_reason": result.get("reason") or "no_model_token_in_title",
        "classification": "MULTI_MODEL_OR_UNKNOWN",
        "near_model_token": None,
        "observed_model_tokens": result.get("observed_model_tokens"),
        "detail_verification_required": True,
        "red_line_applicable": False,
    }


def id_set_fingerprint(ids: list[str] | set[str]) -> str:
    joined = ",".join(sorted(str(i) for i in ids if i))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def merge_page_rows(
    accumulated: dict[str, dict[str, Any]],
    records: list[dict[str, Any]],
    *,
    page_index: int,
    scroll_pass: int,
) -> int:
    """Merge by product_id; keep first position on this page. Return new count."""
    added = 0
    for rec in records:
        pid = str(rec.get("product_id") or "")
        if not pid:
            continue
        if pid in accumulated:
            # Keep first appearance; append occurrence note only
            prev = accumulated[pid]
            sources = prev.setdefault("scroll_passes_seen", [])
            if scroll_pass not in sources:
                sources.append(scroll_pass)
            continue
        item = dict(rec)
        item["page"] = page_index
        item["position_on_page"] = len(accumulated) + 1
        item["scroll_pass_first"] = scroll_pass
        item["scroll_passes_seen"] = [scroll_pass]
        item["jd_product_id"] = pid
        accumulated[pid] = item
        added += 1
    return added


def collect_one_page_with_scroll(
    page,
    *,
    query: str,
    page_index: int,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """
    Bounded scroll + extract passes so virtual-list cards are not lost.
    Does not open product details.
    """
    by_id: dict[str, dict[str, Any]] = {}
    scroll_meta: list[dict[str, Any]] = []
    restriction = None
    load_status = "IN_PROGRESS"
    stable_rounds = 0
    scrolls = 0

    wait_info = p3a.wait_for_search_content(page)
    restriction = p3a.detect_search_restriction(page)
    if restriction:
        return {
            "page": page_index,
            "load_status": "SEARCH_RESTRICTED",
            "restriction": restriction,
            "wait_info": wait_info,
            "rows": [],
            "scroll_meta": [],
            "raw_card_peaks": 0,
            "pager": p3a.discover_next_page_control(page),
        }

    for scroll_pass in range(PAGE_MAX_SCROLLS + 1):
        restriction = p3a.detect_search_restriction(page)
        if restriction:
            load_status = "SEARCH_RESTRICTED"
            break
        try:
            raw = p3a.extract_raw_candidates(page)
        except PlaywrightError as exc:
            load_status = "EXTRACTION_FAILED"
            scroll_meta.append({"scroll_pass": scroll_pass, "error": str(exc)})
            break
        rows = list(raw.get("candidate_rows") or [])
        records, stats = p3a.build_candidate_records(
            rows,
            query=query,
            limit=PAGE_EXTRACT_LIMIT,
            source_url=page.url,
            load_batch=page_index - 1,
        )
        added = merge_page_rows(
            by_id, records, page_index=page_index, scroll_pass=scroll_pass
        )
        metrics = p3a.page_scroll_metrics(page)
        scroll_meta.append(
            {
                "scroll_pass": scroll_pass,
                "raw_rows": len(rows),
                "new_on_page": added,
                "unique_on_page": len(by_id),
                "data_sku_card_count": raw.get("data_sku_card_count"),
                "at_bottom": metrics.get("at_bottom"),
                "stats": stats,
            }
        )
        if added == 0:
            stable_rounds += 1
        else:
            stable_rounds = 0
        at_bottom = bool(metrics.get("at_bottom"))
        if at_bottom and stable_rounds >= PAGE_STABLE_ROUNDS_NEEDED:
            load_status = "PAGE_LOAD_STABLE"
            break
        if scroll_pass >= PAGE_MAX_SCROLLS:
            load_status = (
                "PAGE_SCROLL_BUDGET"
                if not at_bottom
                else "PAGE_LOAD_STABLE_SOFT"
            )
            break
        try:
            page.evaluate(
                "() => window.scrollBy(0, Math.floor(window.innerHeight * 0.9))"
            )
            page.wait_for_timeout(PAGE_SCROLL_PAUSE_MS)
            scrolls += 1
        except PlaywrightError as exc:
            load_status = "SCROLL_ERROR"
            scroll_meta.append({"scroll_error": str(exc)})
            break

    # Scroll back toward top so pager is reachable for next click
    try:
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(400)
        pager = p3a.discover_next_page_control(page)
        if not pager.get("control"):
            page.evaluate("() => window.scrollTo(0, 0)")
            page.wait_for_timeout(300)
            pager = p3a.discover_next_page_control(page)
    except PlaywrightError:
        pager = p3a.discover_next_page_control(page)

    rows_out = sorted(by_id.values(), key=lambda r: int(r.get("position_on_page") or 0))
    peak = max((m.get("data_sku_card_count") or 0) for m in scroll_meta) if scroll_meta else 0
    log(
        f"页{page_index}: unique={len(rows_out)} scrolls={scrolls} "
        f"status={load_status} pager={pager.get('status')}"
    )
    return {
        "page": page_index,
        "load_status": load_status,
        "restriction": restriction,
        "wait_info": wait_info,
        "rows": rows_out,
        "scroll_meta": scroll_meta,
        "scroll_count": scrolls,
        "raw_card_peaks": peak,
        "id_fingerprint": id_set_fingerprint([r["jd_product_id"] for r in rows_out]),
        "pager": pager,
        "collected_at_utc": utc_iso(),
        "url_safe": p3a.sanitize_source_search_url(page.url),
    }


def classify_completeness(
    *,
    stop_reason: str,
    pages_visited: list[int],
    last_pager: dict[str, Any] | None,
    page_failures: list[str],
    budget_hit: bool,
    restricted: bool,
    page_loads_all_confirmed: bool | None = None,
) -> dict[str, Any]:
    """COMPLETE only with POSITIVE end-of-results evidence, no early budget stop, and
    (when supplied) every visited page's load confirmed.

    Three different facts are kept apart:
    - control NOT_FOUND  -> weak signal only (``weak_end_signals``); never end evidence;
    - control DISABLED   -> positive evidence (``next_page_disabled``);
    - pager text says current page >= total pages -> positive evidence.
    ``pagination_reached_end`` (end of paging) is separate from ``page_loads_confirmed``
    (each page fully loaded); the integrity needs both.
    """
    continuous = pages_visited == list(range(1, len(pages_visited) + 1))
    last = last_pager or {}
    end_evidence: list[str] = []
    weak_signals: list[str] = []
    if last.get("status") == "DISABLED" or last.get("disabled"):
        end_evidence.append("next_page_disabled")
    if last.get("status") == "NOT_FOUND" and pages_visited:
        weak_signals.append("no_next_control")
    hint_cur = last.get("current_page_hint")
    hint_total = last.get("total_pages_hint")
    if (
        isinstance(hint_cur, int)
        and isinstance(hint_total, int)
        and hint_cur >= hint_total
        and hint_total > 0
    ):
        end_evidence.append(
            f"pager_text_implies_last_page cur={hint_cur} total_hint={hint_total}"
        )

    reached_end = (
        continuous
        and not page_failures
        and not budget_hit
        and not restricted
        and bool(end_evidence)
        and stop_reason
        in {
            p3a.STOP_NO_MORE_RESULTS,
            "END_OF_RESULTS",
            "NEXT_DISABLED",
            "NO_NEXT_CONTROL",
        }
    )
    loads_ok = page_loads_all_confirmed is not False
    can_complete = reached_end and loads_ok
    incomplete_reasons: list[str] = []
    if not reached_end:
        if budget_hit:
            incomplete_reasons.append(f"budget_or_cap_stop:{stop_reason}")
        if restricted:
            incomplete_reasons.append("restricted")
        if page_failures:
            incomplete_reasons.append("page_failures")
        if not end_evidence:
            incomplete_reasons.append(
                "no_positive_end_evidence"
                + (" (only next control not found)" if weak_signals else "")
            )
        if not continuous:
            incomplete_reasons.append("pages_not_continuous")
    if reached_end and not loads_ok:
        incomplete_reasons.append("page_load_unconfirmed")
    integrity = "COMPLETE" if can_complete else "PARTIAL"
    return {
        "integrity": integrity,
        "statement_zh": (
            "本次关键词搜索结果页收集完成"
            if integrity == "COMPLETE"
            else "本次关键词搜索结果页部分完成（覆盖未确认）"
        ),
        "not_platform_wide": "不得写成京东全平台全部同款",
        "continuous_pages": continuous,
        "pagination_reached_end": reached_end,
        "page_loads_confirmed": page_loads_all_confirmed,
        "next_control_status": last.get("status"),
        "end_page_evidence": end_evidence,
        "weak_end_signals": weak_signals,
        "incomplete_reasons": incomplete_reasons,
        "page_failures": page_failures,
        "budget_hit": budget_hit,
        "restricted": restricted,
        "stop_reason": stop_reason,
        "pager_text_raw": last.get("pager_text"),
        "total_pages_hint_raw": hint_total,
        "current_page_hint_raw": hint_cur,
        "hint_note": "页数/商品总数文案只作参考，不编造链接或固定每页数量",
    }


def empty_checkpoint(
    *,
    run_id: str,
    query: str,
    max_pages: int,
    search_timeout_seconds: int,
    expected_model: str | None,
    backend: str = BACKEND_PLAYWRIGHT,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "mode": MODE,
        "rules_version": RULES_VERSION,
        "backend": backend,
        "query": query,
        "expected_model": expected_model,
        "max_pages": max_pages,
        "search_timeout_seconds": search_timeout_seconds,
        "status": "PENDING",
        "pages_completed": [],
        "next_page_to_collect": 1,
        "stop_reason": None,
        "created_at_utc": utc_iso(),
        "updated_at_utc": utc_iso(),
        "resume_segments": [],
    }


def apply_model_tags(
    rows: list[dict[str, Any]], expected_model: str | None
) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        item = dict(r)
        tag = tag_title_model(str(item.get("title") or ""), expected_model)
        item.update(tag)
        out.append(item)
    return out


def build_deduped_candidates(
    occurrences: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Dedupe by jd_product_id; keep all occurrence positions in occurrences list.
    First-seen becomes canonical candidate.
    """
    by_id: dict[str, dict[str, Any]] = {}
    occ_out: list[dict[str, Any]] = []
    for occ in occurrences:
        pid = str(occ.get("jd_product_id") or occ.get("product_id") or "")
        if not pid:
            continue
        row = dict(occ)
        row["jd_product_id"] = pid
        occ_out.append(row)
        if pid not in by_id:
            canon = dict(row)
            canon["occurrence_count"] = 1
            canon["occurrence_pages"] = [row.get("page")]
            canon["occurrence_ads"] = [row.get("ad_status")]
            by_id[pid] = canon
        else:
            c = by_id[pid]
            c["occurrence_count"] = int(c.get("occurrence_count") or 1) + 1
            pages = c.setdefault("occurrence_pages", [])
            if row.get("page") not in pages:
                pages.append(row.get("page"))
            c.setdefault("occurrence_ads", []).append(row.get("ad_status"))
    candidates = list(by_id.values())
    for i, c in enumerate(candidates, start=1):
        c["rank"] = i
    return candidates, occ_out


def render_report(coverage: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    esc = html.escape
    pages = coverage.get("pages") or []
    integrity = coverage.get("completeness") or {}
    stats = coverage.get("model_tag_stats") or {}

    def page_table() -> str:
        if not pages:
            return "<p>（无页）</p>"
        head = (
            "<tr><th>页</th><th>原始卡数</th><th>本页新增</th><th>相对全局重复</th>"
            "<th>load_status</th><th>页码提示</th><th>指纹</th></tr>"
        )
        body = []
        for p in pages:
            body.append(
                "<tr>"
                + "".join(
                    f"<td>{esc(str(p.get(k, '')))}</td>"
                    for k in (
                        "page",
                        "raw_observed",
                        "new_global",
                        "duplicate_global",
                        "load_status",
                        "page_hint",
                        "id_fingerprint",
                    )
                )
                + "</tr>"
            )
        return f"<table>{head}{''.join(body)}</table>"

    link_rows = []
    for c in candidates[:400]:
        link_rows.append(
            "<tr>"
            f"<td>{esc(str(c.get('rank')))}</td>"
            f"<td>{esc(str(c.get('jd_product_id')))}</td>"
            f"<td>{esc(str(c.get('model_tag')))}</td>"
            f"<td>{esc(str(c.get('title') or '')[:80])}</td>"
            f"<td><a href='{esc(str(c.get('normalized_url') or ''))}'>"
            f"{esc(str(c.get('normalized_url') or ''))}</a></td>"
            "</tr>"
        )
    more = (
        f"<p class='muted'>列表截断展示前 400 / 共 {len(candidates)}；完整见 CSV/JSON。</p>"
        if len(candidates) > 400
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<title>Collect-all {esc(str(coverage.get('run_id')))}</title>
<style>
body{{font-family:"Segoe UI","Microsoft YaHei",sans-serif;margin:24px}}
table{{border-collapse:collapse;width:100%;font-size:0.85em}}
th,td{{border:1px solid #ccc;padding:4px 6px;vertical-align:top}}
th{{background:#f5f5f5}} .warn{{color:#a40}} .muted{{color:#666}}
</style></head><body>
<h1>关键词搜索结果链接收集</h1>
<p><strong>{esc(str(integrity.get('statement_zh')))}</strong>
 · {esc(str(integrity.get('not_platform_wide')))}</p>
<p class="muted">run_id={esc(str(coverage.get('run_id')))} · query={esc(str(coverage.get('query')))}
 · backend={esc(str(coverage.get('backend')))}
 · integrity={esc(str(integrity.get('integrity')))} · stop={esc(str(integrity.get('stop_reason')))}
 · {esc(str(coverage.get('started_at_utc')))} → {esc(str(coverage.get('ended_at_utc')))}</p>
<div>
<h2>覆盖</h2>
<ul>
<li>访问页码：{esc(str(coverage.get('pages_visited')))}</li>
<li>去重链接：{esc(str(coverage.get('unique_candidates')))}</li>
<li>出现记录：{esc(str(coverage.get('occurrence_count')))}</li>
<li>标题型号命中：{esc(str(stats.get('TITLE_MODEL_HIT', 0)))}</li>
<li>近型号：{esc(str(stats.get('TITLE_NEAR_MODEL', 0)))}</li>
<li>型号未知：{esc(str(stats.get('TITLE_MODEL_UNKNOWN', 0)))}</li>
<li>标题覆盖：{esc(str(coverage.get('title_coverage')))}</li>
<li>pagination_complete：{esc(str(coverage.get('pagination_complete')))}</li>
<li>page_load_coverage：{esc(str(coverage.get('page_load_coverage')))}</li>
<li>overall_integrity：{esc(str(coverage.get('overall_integrity')))}</li>
</ul>
<p class="warn">标题命中只是线索，不能代替详情同款核验。最终条数以实测为准，100 不是通过门槛。不用搜索词填标题。</p>
<p>末页证据（正向）：{esc(json.dumps(integrity.get('end_page_evidence'), ensure_ascii=False))}
 · 弱信号（不算末页）：{esc(json.dumps(integrity.get('weak_end_signals'), ensure_ascii=False))}
 · 下一页控件：{esc(str(integrity.get('next_control_status')))}</p>
<p>分页到达末页：{esc(str(integrity.get('pagination_reached_end')))}
 · 各页加载完整：{esc(str(integrity.get('page_loads_confirmed')))}
 · 未完整原因：{esc(json.dumps(integrity.get('incomplete_reasons'), ensure_ascii=False))}</p>
<p>目标页数文案（参考）：{esc(str(integrity.get('pager_text_raw')))}</p>
</div>
<h2>逐页统计</h2>
{page_table()}
<h2>去重链接（摘要）</h2>
{more}
<table><tr><th>rank</th><th>id</th><th>model_tag</th><th>title</th><th>url</th></tr>
{''.join(link_rows)}</table>
</body></html>
"""


def run_collect_all(
    *,
    query: str,
    business_config: Path | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    search_timeout_seconds: int = DEFAULT_SEARCH_TIMEOUT_SECONDS,
    resume_run_id: str | None = None,
    allow_browser: bool = True,
    page_fn: Callable[..., dict[str, Any]] | None = None,
    next_page_fn: Callable[..., dict[str, Any]] | None = None,
    backend: str = BACKEND_PLAYWRIGHT,
    browser: str | None = None,
    bsk_exe: Path | None = None,
    reuse_search_tab: bool = True,
    focus_window: bool = False,
) -> dict[str, Any]:
    query = (query or "").strip()
    if not query:
        raise ValueError("query 不能为空")
    if max_pages < 1 or max_pages > 50:
        raise ValueError("max_pages 安全范围 1..50")
    if search_timeout_seconds < 30 or search_timeout_seconds > 1800:
        raise ValueError("search_timeout_seconds 安全范围 30..1800")
    backend = (backend or BACKEND_PLAYWRIGHT).strip().lower()
    if backend not in {BACKEND_PLAYWRIGHT, BACKEND_BSK}:
        raise ValueError(f"backend 仅支持 {BACKEND_PLAYWRIGHT}|{BACKEND_BSK}")

    expected_model = None
    if business_config:
        cfg, errs = adapt.load_business_baseline(business_config)
        if errs or cfg is None:
            raise ValueError("; ".join(errs or ["config invalid"]))
        expected_model = cfg.get("expected_model")

    if resume_run_id:
        run_dir = COLLECT_ROOT / resume_run_id
        if not run_dir.is_dir():
            raise FileNotFoundError(f"恢复任务不存在: {run_dir}")
        checkpoint = read_json(run_dir / "checkpoint.json")
        if checkpoint.get("query") != query:
            raise RuntimeError(
                f"恢复拒绝：query 不一致 checkpoint={checkpoint.get('query')!r} "
                f"current={query!r}"
            )
        if checkpoint.get("mode") != MODE:
            raise RuntimeError("恢复拒绝：mode 不一致")
        if checkpoint.get("rules_version") != RULES_VERSION:
            raise RuntimeError("恢复拒绝：规则版本变更，请新建任务")
        cp_backend = checkpoint.get("backend") or BACKEND_PLAYWRIGHT
        if cp_backend != backend:
            raise RuntimeError(
                f"恢复拒绝：backend 不一致 checkpoint={cp_backend!r} current={backend!r}"
            )
        run_id = resume_run_id
        checkpoint.setdefault("resume_segments", []).append(
            {"resumed_at_utc": utc_iso(), "from_page": checkpoint.get("next_page_to_collect")}
        )
        # Keep original page completion timestamps; do not rewrite completed pages.
        # Allow CLI to raise safety budgets on resume (never lower below checkpoint).
        max_pages = max(int(checkpoint.get("max_pages") or 1), max_pages)
        search_timeout_seconds = max(
            int(checkpoint.get("search_timeout_seconds") or 30),
            search_timeout_seconds,
        )
        checkpoint["max_pages"] = max_pages
        checkpoint["search_timeout_seconds"] = search_timeout_seconds
    else:
        run_id = f"{utc_stamp()}_collect_{secrets.token_hex(3)}"
        run_dir = COLLECT_ROOT / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "pages").mkdir(parents=True, exist_ok=True)
        checkpoint = empty_checkpoint(
            run_id=run_id,
            query=query,
            max_pages=max_pages,
            search_timeout_seconds=search_timeout_seconds,
            expected_model=expected_model,
            backend=backend,
        )
        write_json_atomic(run_dir / "checkpoint.json", checkpoint)

    lock = CollectLock(run_dir)
    lock.acquire()
    started = utc_now()
    deadline = started.timestamp() + search_timeout_seconds
    occurrences: list[dict[str, Any]] = []
    # Reload prior occurrences on resume
    occ_path = run_dir / "occurrences.json"
    if occ_path.exists():
        try:
            occurrences = list(read_json(occ_path))
        except (OSError, json.JSONDecodeError):
            occurrences = []
    pages_summary: list[dict[str, Any]] = []
    pages_path = run_dir / "pages_summary.json"
    if pages_path.exists():
        try:
            pages_summary = list(read_json(pages_path))
        except (OSError, json.JSONDecodeError):
            pages_summary = []

    seen_global: set[str] = {
        str(o.get("jd_product_id")) for o in occurrences if o.get("jd_product_id")
    }
    stop_reason = "NOT_STARTED"
    page_failures: list[str] = []
    budget_hit = False
    restricted = False
    last_pager: dict[str, Any] | None = None
    close_ok = False

    def timed_out() -> bool:
        return utc_now().timestamp() >= deadline

    def persist() -> None:
        checkpoint["updated_at_utc"] = utc_iso()
        write_json_atomic(run_dir / "checkpoint.json", checkpoint)
        write_json_atomic(run_dir / "occurrences.json", occurrences)
        write_json_atomic(run_dir / "pages_summary.json", pages_summary)

    try:
        checkpoint["status"] = "RUNNING"
        persist()

        def process_page_result(page_index: int, page_result: dict[str, Any]) -> bool:
            """Return False if should stop collection."""
            nonlocal stop_reason, restricted, last_pager, budget_hit
            last_pager = page_result.get("pager") or last_pager
            if page_result.get("load_status") == "SEARCH_RESTRICTED" or page_result.get(
                "restriction"
            ):
                restricted = True
                stop_reason = p3a.STOP_SEARCH_RESTRICTED
                page_failures.append(f"page_{page_index}_restricted")
                write_json_atomic(
                    run_dir / "pages" / f"page_{page_index:03d}.json",
                    {
                        **page_result,
                        "rows": page_result.get("rows") or [],
                        "stopped": p3a.STOP_SEARCH_RESTRICTED,
                    },
                )
                pages_summary.append(
                    {
                        "page": page_index,
                        "raw_observed": len(page_result.get("rows") or []),
                        "new_global": 0,
                        "duplicate_global": 0,
                        "load_status": "SEARCH_RESTRICTED",
                        "page_hint": (page_result.get("pager") or {}).get(
                            "current_page_hint"
                        ),
                        "id_fingerprint": page_result.get("id_fingerprint"),
                        "restriction_tip": (page_result.get("restriction") or {}).get(
                            "observed_tip"
                        ),
                    }
                )
                persist()
                return False
            if page_result.get("load_status") == "EXTRACTION_FAILED":
                page_failures.append(f"page_{page_index}_extract_failed")
                stop_reason = p3a.STOP_EXTRACTION_FAILED
                write_json_atomic(
                    run_dir / "pages" / f"page_{page_index:03d}.json",
                    page_result,
                )
                persist()
                return False
            if page_result.get("load_status") == "PAGE_HINT_MISMATCH":
                page_failures.append(f"page_{page_index}_hint_mismatch")
                stop_reason = p3a.STOP_NO_PROGRESS
                write_json_atomic(
                    run_dir / "pages" / f"page_{page_index:03d}.json",
                    {**page_result, "not_counted_success": True},
                )
                pages_summary.append(
                    {
                        "page": page_index,
                        "raw_observed": len(page_result.get("rows") or []),
                        "new_global": 0,
                        "duplicate_global": 0,
                        "load_status": "PAGE_HINT_MISMATCH",
                        "page_hint": (page_result.get("pager") or {}).get(
                            "current_page_hint"
                        ),
                        "id_fingerprint": page_result.get("id_fingerprint"),
                    }
                )
                persist()
                return False

            rows = apply_model_tags(list(page_result.get("rows") or []), expected_model)
            new_global = 0
            dup_global = 0
            for r in rows:
                pid = str(r.get("jd_product_id") or "")
                if not pid:
                    continue
                occ = {
                    **r,
                    "page": page_index,
                    "discovered_at_utc": r.get("discovered_at_utc") or utc_iso(),
                    "source": "search_serp_collect_all",
                }
                occurrences.append(occ)
                if pid in seen_global:
                    dup_global += 1
                else:
                    seen_global.add(pid)
                    new_global += 1

            # Full-page identical fingerprint vs previous page → stop
            if pages_summary:
                prev_fp = pages_summary[-1].get("id_fingerprint")
                cur_fp = page_result.get("id_fingerprint")
                if prev_fp and cur_fp and prev_fp == cur_fp and new_global == 0:
                    stop_reason = p3a.STOP_NO_PROGRESS
                    page_failures.append(f"page_{page_index}_duplicate_id_set")
                    write_json_atomic(
                        run_dir / "pages" / f"page_{page_index:03d}.json",
                        {**page_result, "rows": rows, "duplicate_page": True},
                    )
                    pages_summary.append(
                        {
                            "page": page_index,
                            "raw_observed": len(rows),
                            "new_global": new_global,
                            "duplicate_global": dup_global,
                            "load_status": "DUPLICATE_PAGE",
                            "page_hint": (last_pager or {}).get("current_page_hint"),
                            "id_fingerprint": cur_fp,
                        }
                    )
                    persist()
                    return False

            write_json_atomic(
                run_dir / "pages" / f"page_{page_index:03d}.json",
                {
                    **page_result,
                    "rows": rows,
                    "new_global": new_global,
                    "duplicate_global": dup_global,
                },
            )
            titled = sum(1 for r in rows if (r.get("title") or "").strip())
            pages_summary.append(
                {
                    "page": page_index,
                    "raw_observed": len(rows),
                    "new_global": new_global,
                    "duplicate_global": dup_global,
                    "load_status": page_result.get("load_status"),
                    "page_load_coverage": page_result.get("page_load_coverage")
                    or (
                        "CONFIRMED"
                        if page_result.get("load_status")
                        in {"PAGE_LOAD_STABLE", "PAGE_LOAD_STABLE_SOFT"}
                        else "UNCONFIRMED"
                    ),
                    "stop_reason_detail": page_result.get("stop_reason_detail"),
                    "titles_with_text": titled,
                    "title_ratio": (titled / len(rows)) if rows else None,
                    "page_hint": (page_result.get("pager") or {}).get("current_page_hint"),
                    "id_fingerprint": page_result.get("id_fingerprint"),
                    "pager_text": (page_result.get("pager") or {}).get("pager_text"),
                    "next_control_status": (page_result.get("pager") or {}).get("status"),
                    "total_pages_hint": (page_result.get("pager") or {}).get("total_pages_hint"),
                    "scroll_effect": page_result.get("scroll_effect"),
                    "scroll_error_count": len(page_result.get("scroll_errors") or []),
                    "hidden_cards_skipped": (page_result.get("card_stats_last") or {}).get("hidden_skipped"),
                    "visible_candidates": len(rows),
                    "animation_pending_final": page_result.get("animation_pending_final"),
                    "hidden_reasons_final": (page_result.get("card_stats_last") or {}).get("hidden_reasons"),
                    "document_visibility_final": (page_result.get("card_stats_last") or {}).get("document_visibility"),
                    "pending_rechecks": page_result.get("pending_rechecks"),
                    "collected_at_utc": page_result.get("collected_at_utc"),
                }
            )
            completed = list(checkpoint.get("pages_completed") or [])
            if page_index not in completed:
                completed.append(page_index)
            checkpoint["pages_completed"] = completed
            checkpoint["next_page_to_collect"] = page_index + 1
            persist()

            if len(seen_global) >= SAFETY_CANDIDATE_CAP:
                budget_hit = True
                stop_reason = "HIT_SAFETY_CANDIDATE_CAP"
                return False
            return True

        start_page = int(checkpoint.get("next_page_to_collect") or 1)

        if page_fn is not None:
            # Offline / injected path (no browser)
            page_index = start_page
            while page_index <= max_pages:
                if timed_out():
                    budget_hit = True
                    stop_reason = p3a.STOP_SEARCH_TIMEOUT
                    break
                pr = page_fn(page_index=page_index, query=query)
                if not process_page_result(page_index, pr):
                    break
                # Decide next
                pager = pr.get("pager") or {}
                if pager.get("disabled") or pager.get("status") == "DISABLED":
                    stop_reason = "NEXT_DISABLED"
                    break
                if pager.get("status") != "FOUND":
                    stop_reason = "NO_NEXT_CONTROL"
                    break
                if next_page_fn is None:
                    stop_reason = "NO_NEXT_FN"
                    break
                nav = next_page_fn(from_page=page_index, pager=pager)
                if not nav.get("ok"):
                    stop_reason = str(nav.get("reason") or p3a.STOP_NO_PROGRESS)
                    if stop_reason == p3a.STOP_SEARCH_RESTRICTED:
                        restricted = True
                    page_failures.append(f"nav_from_{page_index}:{stop_reason}")
                    break
                # Require page advance evidence
                if nav.get("page_after") is not None and int(nav["page_after"]) != page_index + 1:
                    if not nav.get("ids_changed"):
                        stop_reason = p3a.STOP_NO_PROGRESS
                        page_failures.append("page_label_or_ids_not_advanced")
                        break
                page_index += 1
            else:
                budget_hit = True
                stop_reason = p3a.STOP_HIT_PAGE_BUDGET

        elif backend == BACKEND_BSK:
            if not allow_browser:
                raise RuntimeError("allow_browser=False 需要 page_fn")
            client = bsk_rt.BskClient(bsk_exe or bsk_rt.DEFAULT_BSK)
            pre = bsk_rt.check_prereqs(client)
            write_json_atomic(run_dir / "bsk_prereq.json", pre)
            if not pre.get("daemon_ready"):
                raise RuntimeError(
                    "BrowserSkill daemon 未运行。请在独立 PowerShell（非 Cursor Agent）执行：\n"
                    + str((PROJECT_ROOT / "scripts" / "fixtures" / "start_bsk_daemon.ps1").resolve())
                    + "\n保持窗口打开；勿停止用户已启动的 daemon；勿回退独立 Profile。"
                )
            pick = bsk_rt.pick_browser(pre.get("browsers_list"), browser)
            write_json_atomic(run_dir / "bsk_browser_selection.json", pick)
            if not pick.get("selector"):
                raise RuntimeError(
                    "未绑定浏览器实例。运行: bsk browsers --json；"
                    "多个实例时加 --browser <instance_id>。"
                    f" options={pick.get('options')}"
                )
            sess = bsk_rt.start_session(client, str(pick["selector"]), focus=focus_window)
            write_json_atomic(run_dir / "bsk_session_start.json", sess)
            if not sess.get("ok") or not sess.get("session_id"):
                raise RuntimeError(f"session start 失败: {sess}")
            session_id = str(sess["session_id"])
            checkpoint["backend"] = BACKEND_BSK
            checkpoint["browser_instance"] = pick.get("selector")
            checkpoint["session_id"] = session_id
            persist()
            try:
                opened = bsk_rt.open_search(
                    client,
                    session_id,
                    query,
                    reuse_search_tab=reuse_search_tab,
                )
                write_json_atomic(run_dir / "bsk_open_search.json", bsk_rt.sanitize(opened))
                if opened.get("login_required") or (
                    opened.get("restriction_tip")
                    and "LOGIN" in str(opened.get("restriction_tip"))
                ):
                    print(
                        "检测到京东登录页。请在 BrowserSkill Agent 窗口完成登录"
                        "（或先在日常 Chrome 打开已登录的「清风 B913A」搜索页），"
                        "确认后继续；不会自动重开搜索或换号。"
                    )
                    help_res = client.run(
                        [
                            "request-help",
                            "--session",
                            session_id,
                            "--title",
                            "P5-F 请登录京东后继续",
                            "--prompt",
                            "Agent 窗口已停在京东登录页。请在本窗口登录日常账号，"
                            "登录后应回到搜索结果；或取消并先在日常 Chrome 打开"
                            "已登录的 search.jd.com「清风 B913A」结果页再 --resume。"
                            "不要改搜索词、不要连续重试。",
                            "--timeout",
                            "5m",
                        ],
                        timeout=320,
                    )
                    write_json_atomic(
                        run_dir / "bsk_login_help.json", bsk_rt.sanitize(help_res)
                    )
                    if help_res.get("ok"):
                        # Re-check current tab only — no fresh search storm
                        client.run(
                            [
                                "navigate",
                                bsk_rt.search_url_for(query),
                                "--session",
                                session_id,
                            ],
                            timeout=90,
                        )
                        client.run(
                            [
                                "wait-for-element",
                                "--session",
                                session_id,
                                "--selector",
                                "[data-sku]",
                                "--state",
                                "visible",
                                "--timeout",
                                "25s",
                            ],
                            timeout=40,
                        )
                        client.run(["wait-ms", "800"], timeout=10)
                        re_state = bsk_rt.eval_json(
                            client, session_id, bsk_rt.EXTRACT_STATE_JS
                        )
                        rv = (
                            re_state.get("value")
                            if isinstance(re_state.get("value"), dict)
                            else {}
                        )
                        write_json_atomic(
                            run_dir / "bsk_after_login.json",
                            bsk_rt.sanitize(rv),
                        )
                        opened = {
                            "ok": True,
                            "reused_tab": False,
                            "state": rv,
                            "restriction_tip": rv.get("restriction_tip"),
                            "login_required": "passport.jd.com"
                            in str(rv.get("host") or ""),
                            "page_hint": rv.get("current_page_hint"),
                            "after_login_help": True,
                        }
                if opened.get("login_required") or opened.get("restriction_tip"):
                    restricted = True
                    tip = str(opened.get("restriction_tip") or "")
                    stop_reason = (
                        p3a.STOP_LOGIN_REQUIRED
                        if opened.get("login_required") or "LOGIN" in tip
                        else p3a.STOP_SEARCH_RESTRICTED
                    )
                    checkpoint["status"] = "PAUSED"
                    checkpoint["stop_reason"] = stop_reason
                    persist()
                    return _finalize(
                        run_dir=run_dir,
                        run_id=run_id,
                        query=query,
                        expected_model=expected_model,
                        checkpoint=checkpoint,
                        occurrences=occurrences,
                        pages_summary=pages_summary,
                        stop_reason=stop_reason,
                        page_failures=page_failures,
                        budget_hit=True,
                        restricted=True,
                        last_pager=None,
                        max_pages=max_pages,
                        search_timeout_seconds=search_timeout_seconds,
                        started=started,
                        backend=BACKEND_BSK,
                        browser_instance=str(pick.get("selector")),
                    )
                # Verify search context before collecting
                st0 = opened.get("state") or {}
                sk = str(st0.get("search_keyword_input") or "")
                if sk and query.replace(" ", "") not in sk.replace(" ", "") and "B913A" not in sk:
                    print(
                        f"警告: 搜索框词={sk!r} 与 query={query!r} 不完全一致；"
                        "仍按当前任务页继续（不重新刷搜）。"
                    )

                # Resume locate: advance without re-counting completed pages
                current = 1
                observed_hint = st0.get("current_page_hint")
                if (
                    start_page > 1
                    and isinstance(observed_hint, int)
                    and observed_hint == start_page
                ):
                    current = start_page
                    print(f"恢复：任务页已在第{observed_hint}页，跳过定位翻页")
                while current < start_page:
                    print(f"恢复定位：从第{current}页翻到目标第{start_page}页…")
                    if timed_out():
                        budget_hit = True
                        stop_reason = p3a.STOP_SEARCH_TIMEOUT
                        break
                    before_state = bsk_rt.eval_json(
                        client, session_id, bsk_rt.EXTRACT_STATE_JS
                    )
                    bv = (
                        before_state.get("value")
                        if isinstance(before_state.get("value"), dict)
                        else {}
                    )
                    before_ids = [str(x) for x in (bv.get("unique_ids") or [])]
                    nav = bsk_rt.turn_to_next_page(
                        client,
                        session_id,
                        from_page=current,
                        before_ids=before_ids,
                    )
                    write_json_atomic(
                        run_dir / f"resume_turn_{current:03d}.json",
                        bsk_rt.sanitize(nav),
                    )
                    if not nav.get("ok"):
                        stop_reason = p3a.STOP_NO_PROGRESS
                        page_failures.append(
                            f"resume_cannot_advance:{nav.get('reason')}"
                        )
                        print(
                            "无法确认恢复位置，已停止（不猜翻页 URL）。"
                            f" reason={nav.get('reason')}"
                        )
                        break
                    current += 1
                else:
                    page_index = start_page
                    while page_index <= max_pages:
                        if timed_out():
                            budget_hit = True
                            stop_reason = p3a.STOP_SEARCH_TIMEOUT
                            break
                        print(f"=== BSK collect page {page_index} ===")
                        pr = bsk_rt.collect_one_page_bsk(
                            client,
                            session_id,
                            query=query,
                            page_index=page_index,
                            expected_page=page_index,
                        )
                        shot = bsk_rt.screenshot(
                            client,
                            session_id,
                            run_dir / "shots" / f"page_{page_index:03d}_pager.png",
                        )
                        pr["pager_screenshot"] = shot
                        if not process_page_result(page_index, pr):
                            break
                        if pr.get("load_status") in {
                            "EXTRACTION_FAILED",
                            "SCROLL_ERROR",
                            "PAGE_HINT_MISMATCH",
                        }:
                            stop_reason = pr["load_status"]
                            page_failures.append(
                                f"page_{page_index}_unstable:{pr['load_status']}"
                            )
                            break

                        pager = pr.get("pager") or {}
                        last_pager = pager
                        if pager.get("disabled") or pager.get("status") == "DISABLED":
                            stop_reason = "NEXT_DISABLED"
                            break
                        if pager.get("status") != "FOUND":
                            stop_reason = "NO_NEXT_CONTROL"
                            break
                        if not (pr.get("rows") or []):
                            # No visible candidate on this page: save a read-only
                            # visibility diagnostic and stop — never flip blindly and
                            # never count hidden cards as candidates.
                            diag = bsk_rt.eval_json(client, session_id, bsk_rt.VISIBILITY_DIAG_JS)
                            write_json_atomic(
                                run_dir / f"page_{page_index:03d}_visibility_diag.json",
                                bsk_rt.sanitize(
                                    diag.get("value") if diag.get("ok") else {"eval_error": diag.get("raw")}
                                ),
                            )
                            stop_reason = "NO_VISIBLE_CANDIDATES"
                            page_failures.append(f"page_{page_index}_no_visible_candidates")
                            break
                        if page_index >= max_pages:
                            budget_hit = True
                            stop_reason = p3a.STOP_HIT_PAGE_BUDGET
                            break

                        before_ids = [
                            str(r.get("jd_product_id"))
                            for r in (pr.get("rows") or [])
                            if r.get("jd_product_id")
                        ]
                        before_fp = id_set_fingerprint(before_ids)
                        print(
                            f"翻页 {page_index}→{page_index + 1} "
                            f"pager={pager.get('pager_text')!r} "
                            f"src={pager.get('page_source')}"
                        )
                        nav = bsk_rt.turn_to_next_page(
                            client,
                            session_id,
                            from_page=page_index,
                            before_ids=before_ids,
                        )
                        write_json_atomic(
                            run_dir / f"page_turn_{page_index:03d}.json",
                            bsk_rt.sanitize(nav),
                        )
                        if nav.get("reason") == "SEARCH_RESTRICTED":
                            restricted = True
                            stop_reason = p3a.STOP_SEARCH_RESTRICTED
                            break
                        if not nav.get("ok"):
                            stop_reason = str(nav.get("reason") or p3a.STOP_NO_PROGRESS)
                            page_failures.append(
                                f"nav_from_{page_index}:{stop_reason}"
                            )
                            break
                        if int(nav.get("page_after") or 0) != page_index + 1:
                            stop_reason = p3a.STOP_NO_PROGRESS
                            page_failures.append("page_label_not_sequential")
                            break
                        if not nav.get("ids_changed"):
                            stop_reason = p3a.STOP_NO_PROGRESS
                            page_failures.append("grid_not_updated_after_turn")
                            break
                        after_fp = id_set_fingerprint(
                            list(nav.get("after_ids_sample") or [])
                        )
                        # after_ids_sample is partial; prefer wait fingerprint via re-extract
                        _ = before_fp, after_fp
                        page_index += 1
                    else:
                        budget_hit = True
                        stop_reason = p3a.STOP_HIT_PAGE_BUDGET
                close_ok = True
            finally:
                bsk_rt.stop_session(client, session_id)

            return _finalize(
                run_dir=run_dir,
                run_id=run_id,
                query=query,
                expected_model=expected_model,
                checkpoint=checkpoint,
                occurrences=occurrences,
                pages_summary=pages_summary,
                stop_reason=stop_reason,
                page_failures=page_failures,
                budget_hit=budget_hit,
                restricted=restricted,
                last_pager=last_pager,
                max_pages=max_pages,
                search_timeout_seconds=search_timeout_seconds,
                started=started,
                close_ok=close_ok,
                backend=BACKEND_BSK,
                browser_instance=str(pick.get("selector")),
            )

        else:
            if not allow_browser:
                raise RuntimeError("allow_browser=False 需要 page_fn")
            if backend != BACKEND_PLAYWRIGHT:
                raise RuntimeError(f"未知 backend: {backend}")
            search_url = p3a.build_search_url(query)
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(PROFILE_DIR),
                    channel="chrome",
                    headless=False,
                    chromium_sandbox=True,
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(
                        search_url,
                        wait_until="domcontentloaded",
                        timeout=p3a.NAVIGATION_TIMEOUT_MS,
                    )
                    if p3a.detect_search_restriction(page):
                        restricted = True
                        stop_reason = p3a.STOP_SEARCH_RESTRICTED
                        checkpoint["status"] = "PAUSED"
                        checkpoint["stop_reason"] = stop_reason
                        persist()
                        return _finalize(
                            run_dir=run_dir,
                            run_id=run_id,
                            query=query,
                            expected_model=expected_model,
                            checkpoint=checkpoint,
                            occurrences=occurrences,
                            pages_summary=pages_summary,
                            stop_reason=stop_reason,
                            page_failures=page_failures,
                            budget_hit=True,
                            restricted=True,
                            last_pager=None,
                            max_pages=max_pages,
                            search_timeout_seconds=search_timeout_seconds,
                            started=started,
                        )

                    # On resume: walk from page 1 to start_page without re-saving
                    # completed pages (verify each next click).
                    current = 1
                    while current < start_page:
                        print(f"恢复定位：从第{current}页翻到目标第{start_page}页…")
                        if timed_out():
                            budget_hit = True
                            stop_reason = p3a.STOP_SEARCH_TIMEOUT
                            break
                        before_ids = set(p3a.collect_visible_product_ids(page))
                        before_url = page.url
                        pager = p3a.discover_next_page_control(page)
                        before_hint = pager.get("current_page_hint")
                        if pager.get("status") != "FOUND" or pager.get("disabled"):
                            stop_reason = p3a.STOP_NO_PROGRESS
                            page_failures.append("resume_cannot_advance")
                            break
                        click_info = p3a.click_observed_next_page(
                            page, pager.get("control") or {}
                        )
                        if not click_info.get("ok"):
                            stop_reason = p3a.STOP_LOAD_FAILED
                            page_failures.append("resume_click_failed")
                            break
                        progress = p3a.wait_for_search_batch_progress(
                            page,
                            before_ids=before_ids,
                            before_url=before_url,
                            before_page_hint=before_hint
                            if isinstance(before_hint, int)
                            else None,
                        )
                        if not progress.get("progressed"):
                            stop_reason = p3a.STOP_NO_PROGRESS
                            page_failures.append(
                                f"resume_no_progress:{progress.get('reason')}"
                            )
                            break
                        current += 1
                    else:
                        page_index = start_page
                        while page_index <= max_pages:
                            if timed_out():
                                budget_hit = True
                                stop_reason = p3a.STOP_SEARCH_TIMEOUT
                                break
                            print(f"=== collect page {page_index} ===")
                            # Unstable page: do not flip yet
                            pr = collect_one_page_with_scroll(
                                page, query=query, page_index=page_index
                            )
                            if pr.get("load_status") not in {
                                "PAGE_LOAD_STABLE",
                                "PAGE_LOAD_STABLE_SOFT",
                                "PAGE_SCROLL_BUDGET",
                            } and pr.get("load_status") not in {
                                "SEARCH_RESTRICTED",
                                "EXTRACTION_FAILED",
                                "SCROLL_ERROR",
                            }:
                                # still allow persist
                                pass
                            if not process_page_result(page_index, pr):
                                break
                            if pr.get("load_status") in {
                                "EXTRACTION_FAILED",
                                "SCROLL_ERROR",
                            }:
                                # Do not continue to next page if current unstable
                                stop_reason = pr["load_status"]
                                page_failures.append(
                                    f"page_{page_index}_unstable:{pr['load_status']}"
                                )
                                break

                            pager = pr.get("pager") or p3a.discover_next_page_control(
                                page
                            )
                            last_pager = pager
                            if pager.get("disabled") or pager.get("status") == "DISABLED":
                                stop_reason = "NEXT_DISABLED"
                                break
                            if pager.get("status") != "FOUND":
                                stop_reason = "NO_NEXT_CONTROL"
                                break
                            if page_index >= max_pages:
                                budget_hit = True
                                stop_reason = p3a.STOP_HIT_PAGE_BUDGET
                                break

                            before_ids = set(p3a.collect_visible_product_ids(page))
                            before_fp = id_set_fingerprint(before_ids)
                            before_url = page.url
                            before_hint = pager.get("current_page_hint")
                            print(
                                f"翻页 {page_index}→{page_index + 1} "
                                f"pager={pager.get('pager_text')!r}"
                            )
                            click_info = p3a.click_observed_next_page(
                                page, pager.get("control") or {}
                            )
                            if click_info.get("disabled"):
                                stop_reason = "NEXT_DISABLED"
                                break
                            if not click_info.get("ok"):
                                stop_reason = p3a.STOP_LOAD_FAILED
                                page_failures.append(
                                    f"click_fail:{click_info.get('error')}"
                                )
                                break
                            progress = p3a.wait_for_search_batch_progress(
                                page,
                                before_ids=before_ids,
                                before_url=before_url,
                                before_page_hint=before_hint
                                if isinstance(before_hint, int)
                                else None,
                            )
                            print(
                                f"翻页进展 progressed={progress.get('progressed')} "
                                f"reason={progress.get('reason')}"
                            )
                            if progress.get("reason") in {
                                p3a.STOP_SEARCH_RESTRICTED,
                                p3a.STATUS_SEARCH_RESTRICTED,
                            }:
                                restricted = True
                                stop_reason = p3a.STOP_SEARCH_RESTRICTED
                                break
                            if progress.get("reason") in {
                                p3a.STOP_LOGIN_REQUIRED,
                                p3a.STATUS_LOGIN_REQUIRED,
                                p3a.STOP_VERIFICATION_REQUIRED,
                                p3a.STATUS_VERIFICATION_REQUIRED,
                            }:
                                restricted = True
                                stop_reason = str(progress.get("reason"))
                                break
                            if not progress.get("progressed"):
                                stop_reason = p3a.STOP_NO_PROGRESS
                                page_failures.append(
                                    f"no_progress:{progress.get('reason')}"
                                )
                                write_json_atomic(
                                    run_dir / f"page_turn_diag_{page_index:03d}.json",
                                    {
                                        "before_fp": before_fp,
                                        "progress": progress,
                                        "click": click_info,
                                    },
                                )
                                break
                            after_ids = set(p3a.collect_visible_product_ids(page))
                            after_fp = id_set_fingerprint(after_ids)
                            if after_fp == before_fp and not progress.get("new_ids"):
                                stop_reason = p3a.STOP_NO_PROGRESS
                                page_failures.append("identical_id_set_after_turn")
                                write_json_atomic(
                                    run_dir / f"page_turn_diag_{page_index:03d}.json",
                                    {
                                        "before_fp": before_fp,
                                        "after_fp": after_fp,
                                        "progress": progress,
                                    },
                                )
                                break
                            page_index += 1
                        else:
                            budget_hit = True
                            stop_reason = p3a.STOP_HIT_PAGE_BUDGET
                finally:
                    try:
                        context.close()
                        close_ok = True
                    except PlaywrightError:
                        close_ok = False

        if stop_reason in {"NEXT_DISABLED", "NO_NEXT_CONTROL"}:
            # map to end-of-results family for completeness classifier
            pass
        elif stop_reason == "NOT_STARTED" and pages_summary:
            stop_reason = "UNKNOWN_STOP"

        return _finalize(
            run_dir=run_dir,
            run_id=run_id,
            query=query,
            expected_model=expected_model,
            checkpoint=checkpoint,
            occurrences=occurrences,
            pages_summary=pages_summary,
            stop_reason=stop_reason,
            page_failures=page_failures,
            budget_hit=budget_hit,
            restricted=restricted,
            last_pager=last_pager,
            max_pages=max_pages,
            search_timeout_seconds=search_timeout_seconds,
            started=started,
            close_ok=close_ok,
            backend=backend,
            browser_instance=checkpoint.get("browser_instance"),
        )
    except Exception:
        checkpoint["status"] = "INTERRUPTED"
        checkpoint["stop_reason"] = "INTERRUPTED"
        persist()
        raise
    finally:
        lock.release()


def _finalize(
    *,
    run_dir: Path,
    run_id: str,
    query: str,
    expected_model: str | None,
    checkpoint: dict[str, Any],
    occurrences: list[dict[str, Any]],
    pages_summary: list[dict[str, Any]],
    stop_reason: str,
    page_failures: list[str],
    budget_hit: bool,
    restricted: bool,
    last_pager: dict[str, Any] | None,
    max_pages: int,
    search_timeout_seconds: int,
    started: datetime,
    close_ok: bool = True,
    backend: str = BACKEND_PLAYWRIGHT,
    browser_instance: str | None = None,
) -> dict[str, Any]:
    candidates, occ_out = build_deduped_candidates(occurrences)
    # Ensure model tags on canonical rows
    candidates = apply_model_tags(candidates, expected_model)
    tag_stats = {
        "TITLE_MODEL_HIT": 0,
        "TITLE_NEAR_MODEL": 0,
        "TITLE_MODEL_UNKNOWN": 0,
        "OTHER": 0,
    }
    match_stats = {"精确命中": 0, "明确近型号": 0, "未知": 0}
    for c in candidates:
        t = c.get("model_tag") or "OTHER"
        if t in tag_stats:
            tag_stats[t] += 1
        else:
            tag_stats["OTHER"] += 1
        mm = c.get("model_match") or "未知"
        if mm in match_stats:
            match_stats[mm] += 1
        else:
            match_stats["未知"] += 1

    pages_visited = [int(p["page"]) for p in pages_summary]
    page_loads_all_confirmed = (
        bool(pages_summary)
        and all(p.get("page_load_coverage") == "CONFIRMED" for p in pages_summary)
        and not page_failures
    )
    completeness = classify_completeness(
        stop_reason=stop_reason,
        pages_visited=pages_visited,
        last_pager=last_pager
        or (
            {"pager_text": pages_summary[-1].get("pager_text")} if pages_summary else {}
        ),
        page_failures=page_failures,
        budget_hit=budget_hit,
        restricted=restricted,
        page_loads_all_confirmed=page_loads_all_confirmed,
    )
    # Enrich last pager from last page file if needed
    if pages_summary and not (completeness.get("end_page_evidence")):
        last_page_file = run_dir / "pages" / f"page_{pages_summary[-1]['page']:03d}.json"
        if last_page_file.exists():
            try:
                lp = read_json(last_page_file)
                completeness = classify_completeness(
                    stop_reason=stop_reason,
                    pages_visited=pages_visited,
                    last_pager=lp.get("pager"),
                    page_failures=page_failures,
                    budget_hit=budget_hit,
                    restricted=restricted,
                    page_loads_all_confirmed=page_loads_all_confirmed,
                )
            except (OSError, json.JSONDecodeError):
                pass

    titled_cand = sum(1 for c in candidates if (c.get("title") or "").strip())
    title_coverage = {
        "with_title": titled_cand,
        "total": len(candidates),
        "ratio": (titled_cand / len(candidates)) if candidates else None,
        "note": "标题为空则保留 UNKNOWN，不用搜索词填标题",
    }
    page_load_confirmed = [
        p["page"]
        for p in pages_summary
        if p.get("page_load_coverage") == "CONFIRMED"
    ]
    page_load_unconfirmed = [
        p["page"]
        for p in pages_summary
        if p.get("page_load_coverage") != "CONFIRMED"
    ]
    page_load_coverage = {
        "pages_confirmed": page_load_confirmed,
        "pages_unconfirmed": page_load_unconfirmed,
        "all_confirmed": bool(pages_summary)
        and len(page_load_unconfirmed) == 0
        and not page_failures,
        "note": "CONFIRMED 需到底部+空闲+分页证据；仅无新增不足",
    }
    # "reached the end of paging" is separate from "every page fully loaded"
    pagination_complete = bool(completeness.get("pagination_reached_end"))
    # overall: pagination + page loads + useful title rate (informational)
    if (
        pagination_complete
        and page_load_coverage["all_confirmed"]
        and (title_coverage["ratio"] or 0) >= 0.5
    ):
        overall = "COMPLETE_EXPLAINABLE"
    elif pagination_complete:
        overall = "PAGINATION_COMPLETE_WITH_COVERAGE_GAPS"
    else:
        overall = "PARTIAL"

    coverage = {
        "run_id": run_id,
        "rules_version": RULES_VERSION,
        "mode": MODE,
        "backend": backend,
        "browser_instance": browser_instance or checkpoint.get("browser_instance"),
        "query": query,
        "expected_model": expected_model,
        "max_pages_safety": max_pages,
        "search_timeout_seconds": search_timeout_seconds,
        "started_at_utc": utc_iso(started),
        "ended_at_utc": utc_iso(),
        "pages_visited": pages_visited,
        "pages": pages_summary,
        "unique_candidates": len(candidates),
        "occurrence_count": len(occ_out),
        "model_tag_stats": tag_stats,
        "model_match_stats": match_stats,
        "pagination_complete": pagination_complete,
        "page_load_coverage": page_load_coverage,
        "title_coverage": title_coverage,
        "overall_integrity": overall,
        "completeness": completeness,
        "missing_range_note": (
            None
            if completeness["integrity"] == "COMPLETE"
            else (
                f"未完成范围：自 page>={checkpoint.get('next_page_to_collect')} 起可能缺失；"
                f"stop={stop_reason}; failures={page_failures}; "
                f"reasons={completeness.get('incomplete_reasons')}"
            )
        ),
        "close_ok": close_ok,
        "statement_forbidden": "京东全平台全部同款",
        "statement_ok": "本次关键词搜索结果遍历完成"
        if completeness["integrity"] == "COMPLETE"
        else "本次关键词搜索结果部分遍历",
        "title_is_clue_only": "标题型号匹配只是候选线索，不代表详情规格已核验",
    }
    checkpoint["backend"] = backend
    write_json_atomic(run_dir / "coverage.json", coverage)
    write_json_atomic(run_dir / "all_candidates.json", candidates)
    write_json_atomic(run_dir / "occurrences.json", occ_out)
    cand_fields = [
        "rank",
        "jd_product_id",
        "title",
        "title_source",
        "result_region",
        "normalized_url",
        "ad_status",
        "ad_marked",
        "model_tag",
        "model_match",
        "match_reason",
        "model_tag_zh",
        "near_model_token",
        "page",
        "position_on_page",
        "page_load_status",
        "occurrence_count",
        "discovered_at_utc",
        "discovery_source",
        "parent_product_id",
    ]
    # Attach page_load_status from pages_summary
    pl_map = {
        int(p["page"]): p.get("page_load_coverage") or p.get("load_status")
        for p in pages_summary
    }
    for c in candidates:
        c["page_load_status"] = pl_map.get(int(c.get("page") or 0))
    write_csv(run_dir / "all_candidates.csv", candidates, cand_fields)
    occ_fields = [
        "page",
        "position_on_page",
        "jd_product_id",
        "title",
        "title_source",
        "result_region",
        "normalized_url",
        "ad_status",
        "ad_marked",
        "model_tag",
        "model_match",
        "match_reason",
        "discovered_at_utc",
        "source",
        "scroll_pass_first",
        "parent_product_id",
    ]
    write_csv(run_dir / "occurrences.csv", occ_out, occ_fields)
    (run_dir / "report.html").write_text(
        render_report(coverage, candidates), encoding="utf-8"
    )

    checkpoint["status"] = (
        "PAUSED"
        if restricted
        else ("DONE" if completeness["integrity"] == "COMPLETE" else "PARTIAL")
    )
    checkpoint["stop_reason"] = stop_reason
    checkpoint["integrity"] = completeness["integrity"]
    checkpoint["updated_at_utc"] = utc_iso()
    write_json_atomic(run_dir / "checkpoint.json", checkpoint)

    print(f"run_dir={run_dir}")
    print(f"integrity={completeness['integrity']} stop={stop_reason}")
    print(f"unique={len(candidates)} pages={pages_visited}")
    print(f"model_tags={tag_stats}")
    print(f"report={run_dir / 'report.html'}")
    return {
        "ok": completeness["integrity"] == "COMPLETE",
        "run_id": run_id,
        "run_dir": str(run_dir),
        "coverage": coverage,
        "checkpoint": checkpoint,
        "candidates": candidates,
    }


def run_card_visibility_fixture_check() -> tuple[list[str], str]:
    """Run EXTRACT_STATE_JS on scripts/fixtures/bsk_card_visibility_fixture.html in
    headless Chrome (local file, no network). Returns (failures, status)."""
    import html as _html
    import re as _re
    import subprocess
    import tempfile

    fixture = PROJECT_ROOT / "scripts" / "fixtures" / "bsk_card_visibility_fixture.html"
    chrome = next(
        (
            c
            for c in (
                Path(r"C:/Program Files/Google/Chrome/Application/chrome.exe"),
                Path(r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
            )
            if c.is_file()
        ),
        None,
    )
    if chrome is None:
        return [], "SKIPPED_NO_CHROME"
    page = fixture.read_text(encoding="utf-8").replace(
        "__EXTRACT_STATE_JS__", bsk_rt.EXTRACT_STATE_JS
    )
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "cards.html"
        f.write_text(page, encoding="utf-8")
        proc = subprocess.run(
            [str(chrome), "--headless=new", "--disable-gpu", "--window-size=1200,800",
             "--virtual-time-budget=500", "--dump-dom", f.as_uri()],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
        )
    m = _re.search(r'<pre id="out">(.*?)</pre>', proc.stdout or "", _re.S)
    if not m or not m.group(1).strip():
        return ["card visibility fixture produced no output"], "FAILED"
    d = json.loads(_html.unescape(m.group(1)))
    cs = d.get("card_stats") or {}
    fails: list[str] = []
    if d.get("unique_ids") != ["100001", "100002"]:
        fails.append(f"visible-now ids must be normal + out-of-viewport only: {d.get('unique_ids')}")
    if cs.get("visible_now") != 2:
        fails.append(f"visible_now must be 2: {cs.get('visible_now')}")
    if cs.get("animation_pending_ids") != ["100006"] or cs.get("animation_pending") != 1:
        fails.append(f"only the first-frame fade-in card is animation_pending: {cs.get('animation_pending_ids')}")
    if "100006" in (d.get("unique_ids") or []):
        fails.append("animation_pending card must not be counted as a visible candidate")
    reasons = cs.get("hidden_reasons") or {}
    if cs.get("hidden_skipped") != 5 or reasons.get("opacity_low;") != 3 or reasons.get("visibility_hidden;") != 1:
        fails.append(f"hidden cards/reasons wrong: {cs.get('hidden_skipped')} {reasons}")
    return fails, ("OK" if not fails else "FAILED")


def run_offline_self_checks() -> int:
    failures: list[str] = []
    tmp_root = COLLECT_ROOT / "_offline_selfcheck"
    tmp_root.mkdir(parents=True, exist_ok=True)

    # --- multi-page accumulate + dedupe + virtual scroll merge ---
    def fake_page(page_index: int, query: str) -> dict[str, Any]:
        # Page1: 5 ids; page2: 3 new + 2 overlap; page3 last
        catalogs = {
            1: [f"100{i}" for i in range(1, 6)],
            2: ["1004", "1005", "2001", "2002", "2003"],
            3: ["3001", "3002"],
        }
        ids = catalogs.get(page_index, [])
        rows = []
        for i, pid in enumerate(ids, start=1):
            title = (
                f"清风 B913A {pid}"
                if pid.endswith(("1", "2", "3"))
                else (f"清风 B913AE {pid}" if pid.endswith("4") else f"其他 {pid}")
            )
            rows.append(
                {
                    "product_id": pid,
                    "jd_product_id": pid,
                    "title": title,
                    "normalized_url": f"https://item.jd.com/{pid}.html",
                    "ad_marked": None,
                    "ad_status": "广告标识未知，不能写 false",
                    "position_on_page": i,
                    "discovered_at_utc": utc_iso(),
                    "discovery_source": "synthetic",
                }
            )
        pager = {
            1: {
                "status": "FOUND",
                "disabled": False,
                "current_page_hint": 1,
                "total_pages_hint": 3,
                "pager_text": "上一页 1 2 3 下一页 共3页",
                "control": {"className": "next"},
            },
            2: {
                "status": "FOUND",
                "disabled": False,
                "current_page_hint": 2,
                "total_pages_hint": 3,
                "pager_text": "上一页 1 2 3 下一页 共3页",
                "control": {"className": "next"},
            },
            3: {
                "status": "DISABLED",
                "disabled": True,
                "current_page_hint": 3,
                "total_pages_hint": 3,
                "pager_text": "上一页 1 2 3 下一页 共3页",
                "control": None,
            },
        }[page_index]
        return {
            "page": page_index,
            "load_status": "PAGE_LOAD_STABLE",
            "rows": rows,
            "scroll_meta": [{"scroll_pass": 0, "new_on_page": len(rows)}],
            "id_fingerprint": id_set_fingerprint(ids),
            "pager": pager,
            "collected_at_utc": utc_iso(),
            "raw_card_peaks": len(ids),
        }

    def fake_next(from_page: int, pager: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "page_after": from_page + 1, "ids_changed": True}

    # Isolate COLLECT_ROOT for this test by using unique query path via run
    r1 = run_collect_all(
        query="清风 B913A synthetic offline",
        max_pages=5,
        search_timeout_seconds=120,
        allow_browser=False,
        page_fn=fake_page,
        next_page_fn=fake_next,
        business_config=PROJECT_ROOT / "config" / "products" / "b913a.json",
    )
    cov = r1["coverage"]
    if cov["completeness"]["integrity"] != "COMPLETE":
        failures.append(f"expected COMPLETE, got {cov['completeness']}")
    if cov["unique_candidates"] != 8:  # 5 + 3 new on p2 + 2 on p3 = 10? 
        # p1:1001-1005 (5), p2 adds 2001-2003 (3), p3 adds 3001-3002 (2) = 10
        if cov["unique_candidates"] != 10:
            failures.append(f"unique expected 10 got {cov['unique_candidates']}")
    tags = cov["model_tag_stats"]
    if tags.get("TITLE_NEAR_MODEL", 0) < 1:
        failures.append("expected near-model tag for B913AE titles")
    if tags.get("TITLE_MODEL_HIT", 0) < 1:
        failures.append("expected model hit tags")

    # virtual-list merge unit
    acc: dict[str, dict[str, Any]] = {}
    merge_page_rows(
        acc,
        [{"product_id": "A", "title": "t1"}, {"product_id": "B", "title": "t2"}],
        page_index=1,
        scroll_pass=0,
    )
    merge_page_rows(
        acc,
        [{"product_id": "B", "title": "t2b"}, {"product_id": "C", "title": "t3"}],
        page_index=1,
        scroll_pass=1,
    )
    if set(acc) != {"A", "B", "C"}:
        failures.append("virtual scroll merge failed")
    if acc["B"]["title"] != "t2":
        failures.append("first title must be kept")

    # duplicate page stop
    def dup_page(page_index: int, query: str) -> dict[str, Any]:
        ids = ["9001", "9002"]
        rows = [
            {
                "product_id": pid,
                "jd_product_id": pid,
                "title": f"x {pid}",
                "normalized_url": f"https://item.jd.com/{pid}.html",
                "ad_status": "广告标识未知，不能写 false",
                "ad_marked": None,
                "position_on_page": i,
                "discovered_at_utc": utc_iso(),
            }
            for i, pid in enumerate(ids, start=1)
        ]
        return {
            "page": page_index,
            "load_status": "PAGE_LOAD_STABLE",
            "rows": rows,
            "id_fingerprint": id_set_fingerprint(ids),
            "pager": {
                "status": "FOUND",
                "disabled": False,
                "current_page_hint": page_index,
                "control": {"className": "n"},
            },
            "collected_at_utc": utc_iso(),
        }

    r_dup = run_collect_all(
        query="dup page synthetic offline",
        max_pages=5,
        search_timeout_seconds=60,
        allow_browser=False,
        page_fn=dup_page,
        next_page_fn=lambda **kw: {"ok": True, "page_after": kw["from_page"] + 1, "ids_changed": False},
    )
    if r_dup["coverage"]["completeness"]["integrity"] != "PARTIAL":
        failures.append("duplicate page should be PARTIAL")

    # restricted on page 1
    def restricted_page(page_index: int, query: str) -> dict[str, Any]:
        return {
            "page": page_index,
            "load_status": "SEARCH_RESTRICTED",
            "restriction": {"observed_tip": "访问频繁导致无法搜索"},
            "rows": [],
            "pager": {},
            "id_fingerprint": "",
        }

    r_res = run_collect_all(
        query="restricted synthetic offline",
        max_pages=5,
        search_timeout_seconds=60,
        allow_browser=False,
        page_fn=restricted_page,
        next_page_fn=fake_next,
    )
    if r_res["checkpoint"].get("status") not in {"PAUSED", "PARTIAL"}:
        failures.append("restricted should pause/partial")
    if r_res["coverage"]["completeness"]["integrity"] != "PARTIAL":
        failures.append("restricted cannot be COMPLETE")

    # budget page stop
    def many_pages(page_index: int, query: str) -> dict[str, Any]:
        pid = f"8{page_index:04d}"
        return {
            "page": page_index,
            "load_status": "PAGE_LOAD_STABLE",
            "rows": [
                {
                    "product_id": pid,
                    "jd_product_id": pid,
                    "title": f"清风 B913A {pid}",
                    "normalized_url": f"https://item.jd.com/{pid}.html",
                    "ad_status": "广告标识未知，不能写 false",
                    "ad_marked": None,
                    "position_on_page": 1,
                    "discovered_at_utc": utc_iso(),
                }
            ],
            "id_fingerprint": id_set_fingerprint([pid]),
            "pager": {
                "status": "FOUND",
                "disabled": False,
                "current_page_hint": page_index,
                "control": {"c": 1},
            },
            "collected_at_utc": utc_iso(),
        }

    r_bud = run_collect_all(
        query="budget synthetic offline",
        max_pages=2,
        search_timeout_seconds=60,
        allow_browser=False,
        page_fn=many_pages,
        next_page_fn=fake_next,
    )
    if r_bud["coverage"]["completeness"]["integrity"] != "PARTIAL":
        failures.append("max_pages budget should PARTIAL")
    if p3a.STOP_HIT_PAGE_BUDGET not in str(
        r_bud["coverage"]["completeness"].get("stop_reason")
    ) and r_bud["checkpoint"].get("stop_reason") != p3a.STOP_HIT_PAGE_BUDGET:
        # may stop with HIT_PAGE_BUDGET
        if r_bud["checkpoint"].get("stop_reason") not in {
            p3a.STOP_HIT_PAGE_BUDGET,
            "HIT_PAGE_BUDGET",
        }:
            # page 2 still has FOUND next → hits max_pages
            if len(r_bud["coverage"]["pages_visited"]) != 2:
                failures.append(
                    f"budget stop unexpected: {r_bud['checkpoint'].get('stop_reason')}"
                )

    # resume: complete first page only then resume
    state = {"n": 0}

    def resume_pages(page_index: int, query: str) -> dict[str, Any]:
        state["n"] += 1
        return fake_page(page_index, query)

    def resume_next(from_page: int, pager: dict[str, Any]) -> dict[str, Any]:
        # First run: only allow leaving page 1 then caller stops? 
        # Simpler: first run max_pages=1
        return {"ok": True, "page_after": from_page + 1, "ids_changed": True}

    r_a = run_collect_all(
        query="resume synthetic offline unique",
        max_pages=1,
        search_timeout_seconds=60,
        allow_browser=False,
        page_fn=resume_pages,
        next_page_fn=resume_next,
        business_config=PROJECT_ROOT / "config" / "products" / "b913a.json",
    )
    run_id = r_a["run_id"]
    # Fix checkpoint to allow resume from page 2 with higher max — 
    # rules require same max in checkpoint; for test, call resume with page_fn from 2
    cp = read_json(Path(r_a["run_dir"]) / "checkpoint.json")
    # After max_pages=1, next is 2, status PARTIAL
    r_b = run_collect_all(
        query="resume synthetic offline unique",
        max_pages=5,
        search_timeout_seconds=120,
        resume_run_id=run_id,
        allow_browser=False,
        page_fn=fake_page,
        next_page_fn=fake_next,
        business_config=PROJECT_ROOT / "config" / "products" / "b913a.json",
    )
    # Page 1 should not be duplicated in pages_summary beyond one entry
    pages = r_b["coverage"]["pages_visited"]
    if pages.count(1) > 1:
        failures.append("resume re-fetched page 1")
    if 1 not in pages or 2 not in pages:
        failures.append(f"resume should have pages 1 and 2+, got {pages}")
    segs = r_b["checkpoint"].get("resume_segments") or []
    if not segs:
        failures.append("resume_segments missing")

    # ad_status unknown must not be false
    for c in r1["candidates"]:
        if c.get("ad_marked") is False and "不能断言" not in str(c.get("ad_status") or ""):
            # fake data used None
            pass
        if c.get("ad_marked") is False:
            failures.append("synthetic used false ad_marked incorrectly")

    # page label advanced but grid stale → nav must fail (injected next_page_fn)
    def stale_grid_pages(page_index: int, query: str) -> dict[str, Any]:
        pid = f"7{page_index:04d}"
        return {
            "page": page_index,
            "load_status": "PAGE_LOAD_STABLE",
            "rows": [
                {
                    "product_id": pid,
                    "jd_product_id": pid,
                    "title": f"清风 B913A {pid}",
                    "normalized_url": f"https://item.jd.com/{pid}.html",
                    "ad_status": "广告标识未知，不能写 false",
                    "ad_marked": None,
                    "position_on_page": 1,
                    "discovered_at_utc": utc_iso(),
                }
            ],
            "id_fingerprint": id_set_fingerprint([pid]),
            "pager": {
                "status": "FOUND",
                "disabled": False,
                "current_page_hint": page_index,
                "total_pages_hint": 5,
                "control": {"c": 1},
            },
            "collected_at_utc": utc_iso(),
        }

    r_stale = run_collect_all(
        query="stale grid synthetic offline",
        max_pages=5,
        search_timeout_seconds=60,
        allow_browser=False,
        page_fn=stale_grid_pages,
        next_page_fn=lambda **kw: {
            "ok": False,
            "reason": "page_label_ok_grid_stale",
            "page_after": kw["from_page"] + 1,
            "ids_changed": False,
        },
    )
    if r_stale["coverage"]["completeness"]["integrity"] != "PARTIAL":
        failures.append("stale grid after page-label must be PARTIAL")
    if "page_label_ok_grid_stale" not in str(r_stale["checkpoint"].get("stop_reason")):
        failures.append(
            f"stale grid stop unexpected: {r_stale['checkpoint'].get('stop_reason')}"
        )

    # slash widget page parse (runtime offline mirror via p5g helper if present)
    try:
        p5g = load_sibling("p5g_bsk_search_page2_probe")
        parsed = p5g.parse_active_page_offline(
            "2/12", "上一页 1 2 3 下一页 共12页", None
        )
        if parsed.get("current_page_hint") != 2:
            failures.append(f"slash page parse failed: {parsed}")
        if not parsed.get("legacy_would_misread"):
            failures.append("legacy 上一页-first-digit should misread page2")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"p5g page parse import: {exc}")

    # backend mismatch on resume
    r_be = run_collect_all(
        query="backend gate synthetic offline",
        max_pages=1,
        search_timeout_seconds=60,
        allow_browser=False,
        page_fn=fake_page,
        next_page_fn=fake_next,
        backend=BACKEND_PLAYWRIGHT,
    )
    try:
        run_collect_all(
            query="backend gate synthetic offline",
            max_pages=5,
            search_timeout_seconds=60,
            resume_run_id=r_be["run_id"],
            allow_browser=False,
            page_fn=fake_page,
            next_page_fn=fake_next,
            backend=BACKEND_BSK,
        )
        failures.append("resume backend mismatch should raise")
    except RuntimeError as exc:
        if "backend" not in str(exc):
            failures.append(f"resume backend error unexpected: {exc}")

    # Title/ID association + no query-echo fill + B913A not in B913AE
    rows_t = bsk_rt.cards_to_rows(
        [
            {
                "jd_product_id": "111",
                "title": "清风擦手纸 B913A 200抽",
                "title_source": "attr_title",
                "result_region": "main_results",
                "ad_marked": None,
                "href": "",
                "discovery_source": "data_sku_card",
                "parent_product_id": None,
                "nested_product_ids": ["222"],
            },
            {
                "jd_product_id": "222",
                "title": "",
                "title_source": "empty",
                "result_region": "main_results",
                "ad_marked": None,
                "href": "",
                "discovery_source": "data_sku_variant",
                "parent_product_id": "111",
                "nested_product_ids": [],
            },
            {
                "jd_product_id": "333",
                "title": "清风 B913A",  # exact query echo → reject
                "title_source": "attr_title",
                "result_region": "recommend_or_related",
                "ad_marked": True,
                "href": "https://chat.jd.com/x",
                "discovery_source": "data_sku_card",
            },
        ],
        query="清风 B913A",
        page_index=1,
        source_url="https://search.jd.com/Search",
    )
    by_pid = {r["jd_product_id"]: r for r in rows_t}
    if by_pid["111"]["title"] != "清风擦手纸 B913A 200抽":
        failures.append("title must stay bound to same card id")
    if by_pid["222"]["parent_product_id"] != "111" or by_pid["222"]["title"] != "":
        failures.append("variant must keep parent link and not inherit title blindly")
    if by_pid["333"]["title"] != "" or by_pid["333"]["title_source"] != "rejected_query_echo":
        failures.append("must not fill title with search query")
    if by_pid["333"]["observed_url"] is not None:
        failures.append("chat.jd.com href must be dropped")
    if by_pid["333"]["result_region"] != "recommend_or_related":
        failures.append("recommend region must be preserved")

    hit = tag_title_model("清风商用擦手纸…B913A…20包", "B913A")
    near = tag_title_model("清风…B913AE…16包", "B913A")
    empty = tag_title_model("", "B913A")
    if hit.get("model_match") != "精确命中":
        failures.append(f"exact model_match failed: {hit}")
    if near.get("model_match") != "明确近型号":
        failures.append(f"near model_match failed: {near}")
    if empty.get("model_match") != "未知" or empty.get("match_reason") != "empty_title":
        failures.append(f"empty title tagging failed: {empty}")

    # Virtual-list accumulate already covered; scroll-insufficiency → UNCONFIRMED via page_fn
    def thin_unconfirmed(page_index: int, query: str) -> dict[str, Any]:
        return {
            "page": page_index,
            "load_status": "PAGE_SCROLL_BUDGET",
            "page_load_coverage": "UNCONFIRMED_SCROLL_BUDGET",
            "rows": [
                {
                    "product_id": "55501",
                    "jd_product_id": "55501",
                    "title": "清风 B913A 纸",
                    "title_source": "attr_title",
                    "result_region": "main_results",
                    "normalized_url": "https://item.jd.com/55501.html",
                    "ad_status": "广告标识未知，不能写 false",
                    "ad_marked": None,
                    "position_on_page": 1,
                    "discovered_at_utc": utc_iso(),
                }
            ],
            "id_fingerprint": id_set_fingerprint(["55501"]),
            "pager": {
                "status": "DISABLED",
                "disabled": True,
                "current_page_hint": 1,
                "total_pages_hint": 1,
            },
            "collected_at_utc": utc_iso(),
        }

    r_unc = run_collect_all(
        query="unconfirmed load synthetic",
        max_pages=1,
        search_timeout_seconds=60,
        allow_browser=False,
        page_fn=thin_unconfirmed,
        next_page_fn=fake_next,
        business_config=PROJECT_ROOT / "config" / "products" / "b913a.json",
    )
    if r_unc["coverage"].get("overall_integrity") == "COMPLETE_EXPLAINABLE":
        failures.append("unconfirmed page load must not be COMPLETE_EXPLAINABLE")
    if r_unc["coverage"]["page_load_coverage"].get("all_confirmed"):
        failures.append("UNCONFIRMED page must not all_confirmed")

    # ---- Coverage verdict regression (synthetic; reproduces collect_851578 state mix) ----
    def _mk_page(ids, load_status, load_cov, pager):
        def _f(page_index: int, query: str) -> dict[str, Any]:
            real_ids = [f"{page_index}{i:03d}" for i in ids]
            return {
                "page": page_index,
                "load_status": load_status[page_index],
                "page_load_coverage": load_cov[page_index],
                "rows": [
                    {
                        "product_id": pid, "jd_product_id": pid, "title": "清风 B913A 纸",
                        "title_source": "attr_title", "result_region": "main_results",
                        "normalized_url": f"https://item.jd.com/{pid}.html",
                        "ad_status": "广告标识未知，不能写 false", "ad_marked": None,
                        "position_on_page": n + 1, "discovered_at_utc": utc_iso(),
                    }
                    for n, pid in enumerate(real_ids)
                ],
                "id_fingerprint": id_set_fingerprint(real_ids),
                "pager": pager[page_index],
                "collected_at_utc": utc_iso(),
            }
        return _f

    def _cov_case(tag, page_fn, max_pages):
        res = run_collect_all(
            query=f"coverage regression {tag}", max_pages=max_pages, search_timeout_seconds=60,
            allow_browser=False, page_fn=page_fn, next_page_fn=fake_next,
            business_config=PROJECT_ROOT / "config" / "products" / "b913a.json",
        )
        cov_json = read_json(Path(res["run_dir"]) / "coverage.json")
        html_txt = (Path(res["run_dir"]) / "report.html").read_text(encoding="utf-8")
        cp = read_json(Path(res["run_dir"]) / "checkpoint.json")
        # JSON / HTML / checkpoint consistency
        comp = cov_json["completeness"]
        if comp["integrity"] != cp.get("integrity"):
            failures.append(f"{tag}: checkpoint integrity != coverage integrity")
        if f"integrity={comp['integrity']}" not in html_txt:
            failures.append(f"{tag}: html integrity differs from coverage.json")
        if f"pagination_complete：{cov_json['pagination_complete']}" not in html_txt:
            failures.append(f"{tag}: html pagination_complete differs")
        if f"overall_integrity：{cov_json['overall_integrity']}" not in html_txt:
            failures.append(f"{tag}: html overall_integrity differs")
        if cov_json["pagination_complete"] != comp["pagination_reached_end"]:
            failures.append(f"{tag}: pagination_complete must equal pagination_reached_end")
        return cov_json

    NF = {"status": "NOT_FOUND", "disabled": False, "current_page_hint": None, "total_pages_hint": None, "pager_text": None}
    # (1) 851578 mix: not scrolled to bottom + next control not found
    c1 = _cov_case("a_not_found_unscrolled", _mk_page(range(3), {1: "PAGE_SCROLL_BUDGET"}, {1: "UNCONFIRMED_SCROLL_BUDGET"}, {1: NF}), 2)
    if c1["completeness"]["integrity"] == "COMPLETE" or c1["pagination_complete"] or c1["overall_integrity"] != "PARTIAL":
        failures.append(f"851578 mix must not be COMPLETE: {c1['completeness']['integrity']} {c1['overall_integrity']}")
    if c1["completeness"]["end_page_evidence"] or c1["completeness"]["weak_end_signals"] != ["no_next_control"]:
        failures.append("missing control must be a weak signal, not end evidence")
    if c1["completeness"]["stop_reason"] != "NO_NEXT_CONTROL":
        failures.append("actual stop reason must be preserved")
    # (2) at bottom + stable + confirmed but control simply absent -> still not end
    c2 = _cov_case("b_not_found_at_bottom", _mk_page(range(3), {1: "PAGE_LOAD_STABLE"}, {1: "CONFIRMED"}, {1: NF}), 2)
    if c2["completeness"]["integrity"] == "COMPLETE" or c2["pagination_complete"]:
        failures.append("control not found alone must never prove the last page")
    # (3) positive end: found on p1, disabled + page text says last on p2
    FOUND = {"status": "FOUND", "disabled": False, "current_page_hint": 1, "total_pages_hint": 2, "pager_text": "1 2 下一页 共2页"}
    DIS = {"status": "DISABLED", "disabled": True, "current_page_hint": 2, "total_pages_hint": 2, "pager_text": "1 2 下一页 共2页"}
    ok_ls = {1: "PAGE_LOAD_STABLE", 2: "PAGE_LOAD_STABLE"}
    ok_cv = {1: "CONFIRMED", 2: "CONFIRMED"}
    c3 = _cov_case("c_positive_end", _mk_page(range(3), ok_ls, ok_cv, {1: FOUND, 2: DIS}), 5)
    if c3["completeness"]["integrity"] != "COMPLETE" or not c3["pagination_complete"] or c3["overall_integrity"] != "COMPLETE_EXPLAINABLE":
        failures.append(f"explicit last page with confirmed loads should be complete: {c3['completeness']['integrity']} {c3['overall_integrity']}")
    # (3b) reached the end but a page load unconfirmed -> end reached yet not complete
    c3b = _cov_case("c2_end_but_unconfirmed_load", _mk_page(range(3), {1: "PAGE_SCROLL_BUDGET", 2: "PAGE_LOAD_STABLE"}, {1: "UNCONFIRMED_SCROLL_BUDGET", 2: "CONFIRMED"}, {1: FOUND, 2: DIS}), 5)
    if not c3b["pagination_complete"] or c3b["completeness"]["integrity"] == "COMPLETE" or c3b["overall_integrity"] == "COMPLETE_EXPLAINABLE":
        failures.append("reached end + unconfirmed page load: end reached but must not claim complete")
    if "page_load_unconfirmed" not in c3b["completeness"]["incomplete_reasons"]:
        failures.append("unconfirmed page load must be listed in incomplete_reasons")
    # (4) page budget reached first
    c4 = _cov_case("d_page_budget", _mk_page(range(3), ok_ls, ok_cv, {1: FOUND, 2: {**FOUND, "current_page_hint": 2}}), 2)
    if c4["completeness"]["stop_reason"] != "HIT_PAGE_BUDGET" and "BUDGET" not in str(c4["completeness"]["stop_reason"]).upper():
        failures.append(f"page budget stop reason lost: {c4['completeness']['stop_reason']}")
    if c4["completeness"]["integrity"] == "COMPLETE" or c4["pagination_complete"] or not c4["completeness"]["budget_hit"]:
        failures.append("page budget must not look like a natural end")
    # (5) candidate cap truncation
    global SAFETY_CANDIDATE_CAP
    _cap = SAFETY_CANDIDATE_CAP
    SAFETY_CANDIDATE_CAP = 2
    try:
        c5 = _cov_case("e_candidate_cap", _mk_page(range(3), ok_ls, ok_cv, {1: FOUND, 2: DIS}), 5)
    finally:
        SAFETY_CANDIDATE_CAP = _cap
    if c5["completeness"]["stop_reason"] != "HIT_SAFETY_CANDIDATE_CAP" or c5["completeness"]["integrity"] == "COMPLETE" or c5["pagination_complete"]:
        failures.append(f"candidate cap truncation must stay partial with its own stop reason: {c5['completeness']}")

    # ---- BSK page actions must carry the cooperate context (synthetic client) ----
    class _FakeBsk:
        def __init__(self):
            self.calls: list[list[str]] = []

        def run(self, args, *, timeout=120):
            self.calls.append(list(args))
            if args[:2] == ["cooperate", "sync"]:
                return {"ok": True, "data": {"context_id": "CTX1", "observation_id": "OBS1"}}
            if args and args[0] == "evaluate":
                mark = {"ok": True, "selector": '[data-pr-next="1"]', "className": "n", "candidate_count": 1}
                return {"ok": True, "data": {"value": mark if "data-pr-next" in " ".join(args) else {"ok": True}}}
            return {"ok": True, "data": {}}

    fb = _FakeBsk()
    bsk_rt.click_next_page(fb, "S1")
    clicks = [c for c in fb.calls if c and c[0] == "click"]
    scrolls = [c for c in fb.calls if c and c[0] == "scroll-to"]
    if not clicks or "--context" not in clicks[0] or "CTX1" not in clicks[0]:
        failures.append(f"click must carry cooperate --context: {clicks}")
    if not scrolls or "--context" not in scrolls[0]:
        failures.append(f"scroll-to must carry cooperate --context: {scrolls}")
    syncs_before_click = [
        i for i, c in enumerate(fb.calls) if c[:2] == ["cooperate", "sync"]
    ]
    click_idx = next((i for i, c in enumerate(fb.calls) if c and c[0] == "click"), -1)
    if not any(i < click_idx for i in syncs_before_click):
        failures.append("a cooperate sync must precede the click")

    vis_fails, vis_status = run_card_visibility_fixture_check()
    failures.extend(vis_fails)
    print(f"card visibility fixture: {vis_status}")

    # ---- only animation-pending cards: bounded same-page recheck, no flip (synthetic) ----
    class _PendingBsk:
        def __init__(self):
            self.calls: list[list[str]] = []

        def run(self, args, *, timeout=120):
            self.calls.append(list(args))
            if args[:2] == ["cooperate", "sync"]:
                return {"ok": True, "data": {"context_id": "C", "observation_id": "O"}}
            if args and args[0] == "evaluate" and "card_stats" in args[3]:
                return {"ok": True, "data": {"value": {
                    "cards": [], "unique_ids": [], "data_sku_count": 30,
                    "card_stats": {"visible_now": 0, "animation_pending": 30, "hidden_skipped": 0,
                                   "hidden_reasons": {}, "document_visibility": "hidden", "has_focus": False},
                    "scroll_y": 2000, "scroll_max": 2000, "loading_hint": False, "pager_near_bottom": True,
                    "next_control": {"visible": True, "disabled": False}, "pager_text": "1 2 下一页 共16页",
                    "current_page_hint": 1, "total_pages_hint": 16,
                    "host": "search.jd.com", "url": "https://search.jd.com/Search",
                }}}
            return {"ok": True, "data": {"value": {"ok": True}}}

    pb = _PendingBsk()
    pres = bsk_rt.collect_one_page_bsk(pb, "S", query="q", page_index=1, expected_page=1, log=lambda *_a, **_k: None)
    if pres.get("rows"):
        failures.append("animation-pending cards must not become candidate rows")
    if len(pres.get("pending_rechecks") or []) != bsk_rt.PENDING_RECHECKS:
        failures.append(f"pending-only page must recheck exactly {bsk_rt.PENDING_RECHECKS} times: {len(pres.get('pending_rechecks') or [])}")
    if pres.get("animation_pending_final") != 30 or pres.get("page_load_coverage") == "CONFIRMED":
        failures.append(f"pending page must stay unconfirmed: {pres.get('page_load_coverage')}")
    if any(c and c[0] in {"click", "navigate", "reload"} for c in pb.calls):
        failures.append("pending recheck must not click/navigate/reload")

    print("=== P5-F offline self-check ===")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK")
    print(f"sample_complete_run={r1['run_dir']}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="P5-F collect-all search links")
    p.add_argument("--query", type=str, default=None)
    p.add_argument("--business-config", type=Path, default=None)
    p.add_argument(
        "--collect-all",
        action="store_true",
        help="Traverse reachable result pages (required for live run)",
    )
    p.add_argument(
        "--backend",
        choices=[BACKEND_PLAYWRIGHT, BACKEND_BSK],
        default=BACKEND_PLAYWRIGHT,
        help="playwright=jd_persistent (default); bsk=BrowserSkill daily Chrome",
    )
    p.add_argument(
        "--browser",
        default=None,
        help="BrowserSkill instance id/label (required when multiple connected)",
    )
    p.add_argument("--bsk-exe", type=Path, default=None)
    p.add_argument(
        "--force-navigate",
        action="store_true",
        help="BSK: do not borrow an existing user search tab",
    )
    p.add_argument(
        "--focus-window",
        action="store_true",
        help="BSK: open the Agent Window in the foreground (bsk session start --focus)",
    )
    p.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    p.add_argument(
        "--search-timeout-seconds",
        type=int,
        default=DEFAULT_SEARCH_TIMEOUT_SECONDS,
    )
    p.add_argument("--resume", type=str, default=None, help="Resume run_id")
    p.add_argument("--self-check", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_check:
        return run_offline_self_checks()
    if not args.collect_all and not args.resume:
        print("需要 --collect-all 或 --resume", file=sys.stderr)
        return 2
    if not args.query and not args.resume:
        print("需要 --query", file=sys.stderr)
        return 2
    query = args.query
    backend = args.backend
    if args.resume and not query:
        cp = read_json(COLLECT_ROOT / args.resume / "checkpoint.json")
        query = cp.get("query")
        if not args.backend or args.backend == BACKEND_PLAYWRIGHT:
            # Prefer checkpoint backend when resume omits explicit override intent
            backend = cp.get("backend") or backend
    # If user passes --backend on resume, it must match (enforced in run_collect_all)
    if args.resume and args.backend:
        backend = args.backend
    try:
        result = run_collect_all(
            query=str(query),
            business_config=args.business_config,
            max_pages=args.max_pages,
            search_timeout_seconds=args.search_timeout_seconds,
            resume_run_id=args.resume,
            allow_browser=True,
            backend=backend,
            browser=args.browser,
            bsk_exe=args.bsk_exe,
            reuse_search_tab=not args.force_navigate,
            focus_window=bool(args.focus_window),
        )
    except (ValueError, RuntimeError, FileNotFoundError, PlaywrightError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
