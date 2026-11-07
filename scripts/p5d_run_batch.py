"""
P5-D: multi-SKU Excel → sequential P5-B jobs → batch report + checkpoint/resume.

Does not auto-fallback to historical candidates on SEARCH_RESTRICTED.
Does not invent a second real business SKU.
Default live browser is for --run; --validate-only never opens JD.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import json
import secrets
import shutil
import sys
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
BATCH_ROOT = (PROJECT_ROOT / "artifacts" / "batch_jobs").resolve()

RULES_VERSION = "p5d-batch-v1"
LOCK_NAME = "batch.lock"


def load_sibling(stem: str) -> Any:
    path = SCRIPTS_DIR / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(f"priceradar_{stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


excel_in = load_sibling("priceradar_excel_input")
p5b = load_sibling("p5b_run_sku")
adapt = load_sibling("priceradar_candidate_adapt")
take_home = load_sibling("priceradar_take_home_prices")


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
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def fingerprint_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def make_batch_id() -> str:
    return f"{utc_stamp()}_batch_{secrets.token_hex(3)}"


class BatchLock:
    def __init__(self, batch_dir: Path):
        self.path = batch_dir / LOCK_NAME
        self.held = False

    def acquire(self) -> None:
        if self.path.exists():
            try:
                meta = read_json(self.path)
            except (OSError, json.JSONDecodeError):
                meta = {}
            raise RuntimeError(
                f"批次锁已存在（可能正在运行）: {self.path} meta={meta}"
            )
        write_json_atomic(
            self.path,
            {"pid": None, "acquired_at_utc": utc_iso(), "note": "batch exclusive lock"},
        )
        self.held = True

    def release(self) -> None:
        if self.held and self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass
        self.held = False


def empty_checkpoint(
    *,
    batch_id: str,
    input_fingerprint: str,
    excel_path: str,
    budgets: dict[str, Any],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "rules_version": RULES_VERSION,
        "p5b_rules_version": p5b.RULES_VERSION,
        "excel_rules_version": excel_in.RULES_VERSION_EXCEL,
        "input_fingerprint": input_fingerprint,
        "excel_path": excel_path,
        "budgets": budgets,
        "status": "PENDING",
        "pause_reason": None,
        "created_at_utc": utc_iso(),
        "updated_at_utc": utc_iso(),
        "items": [
            {
                "item_id": it["item_id"],
                "excel_row": it["excel_row"],
                "business_sku": it["business_sku"],
                "status": "PENDING",
                "sku_job_dir": None,
                "config_path": None,
                "config_fingerprint": None,
                "started_at_utc": None,
                "ended_at_utc": None,
                "evidence_captured_at_utc": None,
                "attempt": 0,
                "error": None,
                "summary_excerpt": None,
            }
            for it in items
        ],
    }


def save_checkpoint(batch_dir: Path, checkpoint: dict[str, Any]) -> None:
    checkpoint["updated_at_utc"] = utc_iso()
    write_json_atomic(batch_dir / "checkpoint.json", checkpoint)


def resolve_batch_budgets(
    *,
    max_candidates: int | None,
    max_pages: int | None,
    search_timeout_seconds: int | None,
    max_verify: int | None,
    max_verify_total: int | None,
) -> dict[str, Any]:
    p3a = p5b.p3a
    sb = p3a.resolve_search_budget(
        max_candidates=max_candidates,
        max_pages=max_pages,
        search_timeout_seconds=search_timeout_seconds,
        limit_explicit=False,
        max_candidates_explicit=max_candidates is not None,
        max_pages_explicit=max_pages is not None,
        search_timeout_explicit=search_timeout_seconds is not None,
    )
    mv = int(max_verify) if max_verify is not None else p5b.DEFAULT_MAX_VERIFY
    if mv < 1 or mv > p5b.MAX_VERIFY_CAP:
        raise ValueError(f"max-verify 必须是 1..{p5b.MAX_VERIFY_CAP}")
    mvt = int(max_verify_total) if max_verify_total is not None else max(mv * 5, mv)
    if mvt < 1:
        raise ValueError("max-verify-total 必须为正整数")
    return {
        **sb,
        "max_verify": mv,
        "max_verify_total": mvt,
    }


def aggregate_batch(
    *,
    batch_id: str,
    checkpoint: dict[str, Any],
    item_results: list[dict[str, Any]],
    parse_result: dict[str, Any],
) -> dict[str, Any]:
    """Build batch-level tables and stats. Per-SKU lowest only; no cross-SKU min."""
    sku_summaries: list[dict[str, Any]] = []
    product_rows: list[dict[str, Any]] = []
    price_options: list[dict[str, Any]] = []

    for ir in item_results:
        item = ir["item"]
        status = ir.get("status")
        summary = ir.get("sku_summary") or {}
        rows = ir.get("product_rows") or []
        opts = ir.get("price_options") or []
        for r in rows:
            r2 = dict(r)
            r2["batch_id"] = batch_id
            r2["item_id"] = item["item_id"]
            r2["excel_row"] = item.get("excel_row")
            r2["evidence_captured_at_utc"] = ir.get("evidence_captured_at_utc")
            r2["sku_job_status"] = status
            product_rows.append(r2)
        for o in opts:
            o2 = dict(o)
            o2["batch_id"] = batch_id
            o2["item_id"] = item["item_id"]
            price_options.append(o2)

        lowest = summary.get("lowest_comparable_take_home")
        sku_summaries.append(
            {
                "batch_id": batch_id,
                "item_id": item["item_id"],
                "excel_row": item.get("excel_row"),
                "business_sku": item["business_sku"],
                "row_kind": item.get("row_kind"),
                "is_synthetic_test": item.get("is_synthetic_test"),
                "status": status,
                "search_query": item.get("search_query"),
                "red_line": item.get("red_line"),
                "currency": item.get("currency"),
                "candidates_found": summary.get("candidates_found"),
                "detail_pages_opened": summary.get("detail_pages_opened"),
                "not_verified_budget": summary.get("not_verified_budget"),
                "products_possible_break": summary.get("products_possible_break"),
                "products_primary_low_price_signal": summary.get(
                    "products_primary_low_price_signal"
                ),
                "products_review_required": summary.get("products_review_required"),
                "lowest_comparable_take_home": (lowest or {}).get("normalized_price")
                if lowest
                else None,
                "lowest_conditions": (lowest or {}).get("conditions") if lowest else None,
                "sku_job_dir": ir.get("sku_job_dir"),
                "evidence_captured_at_utc": ir.get("evidence_captured_at_utc"),
                "error": ir.get("error"),
                "note": (
                    "最低可比到手价仅限本业务SKU内；不跨SKU混算。"
                    + (
                        " synthetic/offline"
                        if item.get("is_synthetic_test")
                        else ""
                    )
                ),
            }
        )

    # Stats — mutually exclusive process buckets for SKU items
    n_input_rows = (
        parse_result.get("counts", {}).get("valid", 0)
        + parse_result.get("counts", {}).get("rejected", 0)
        + parse_result.get("counts", {}).get("skipped", 0)
    )
    n_valid = parse_result.get("counts", {}).get("valid", 0)
    done = sum(1 for s in sku_summaries if s["status"] == "DONE")
    failed = sum(1 for s in sku_summaries if s["status"] == "FAILED")
    paused = sum(
        1
        for s in sku_summaries
        if s["status"] in {"PAUSED_UNSTARTED", "PAUSED_SESSION"}
    )
    interrupted = sum(1 for s in sku_summaries if s["status"] == "INTERRUPTED")
    pending = sum(1 for s in sku_summaries if s["status"] == "PENDING")
    # Business review among DONE sku jobs (not additive with process status)
    business_review_skus = sum(
        1
        for s in sku_summaries
        if s["status"] == "DONE"
        and int(s.get("products_review_required") or 0) > 0
        and int(s.get("products_possible_break") or 0) == 0
    )
    possible_break_products = sum(
        int(s.get("products_possible_break") or 0) for s in sku_summaries
    )
    primary_low = sum(
        int(s.get("products_primary_low_price_signal") or 0) for s in sku_summaries
    )
    detail_opens = sum(int(s.get("detail_pages_opened") or 0) for s in sku_summaries)
    candidates_total = sum(int(s.get("candidates_found") or 0) for s in sku_summaries)

    return {
        "batch_id": batch_id,
        "rules_version": RULES_VERSION,
        "sku_summaries": sku_summaries,
        "product_rows": product_rows,
        "price_options": price_options,
        "input_errors": parse_result.get("rejected") or [],
        "stats": {
            "input_rows_seen": n_input_rows,
            "valid_skus": n_valid,
            "sku_done": done,
            "sku_failed": failed,
            "sku_paused_unstarted": paused,
            "sku_interrupted": interrupted,
            "sku_pending": pending,
            "candidates_total": candidates_total,
            "detail_pages_opened_total": detail_opens,
            "products_possible_break_total": possible_break_products,
            "products_primary_low_price_signal_total": primary_low,
            "business_review_skus_among_done": business_review_skus,
            "stat_notes": [
                "sku_done/failed/paused/interrupted/pending 为流程状态，互斥可相加≈valid_skus",
                "business_review_skus_among_done 是业务待核验（子集），不要与 sku_done 相加",
                "products_possible_break 与 primary_low 按商品计数，跨SKU仅求和展示，最低价不跨SKU",
            ],
        },
        "checkpoint_status": checkpoint.get("status"),
        "pause_reason": checkpoint.get("pause_reason"),
        "budgets": checkpoint.get("budgets"),
    }


def render_batch_html(agg: dict[str, Any]) -> str:
    esc = html.escape
    st = agg.get("stats") or {}
    rows = agg.get("sku_summaries") or []

    def table(rows_in: list[dict[str, Any]], keys: list[str]) -> str:
        if not rows_in:
            return "<p>（无）</p>"
        head = "".join(f"<th>{esc(k)}</th>" for k in keys)
        body = []
        for r in rows_in:
            body.append(
                "<tr>"
                + "".join(
                    f"<td>{esc(str(r.get(k) if r.get(k) is not None else ''))}</td>"
                    for k in keys
                )
                + "</tr>"
            )
        return f"<table><tr>{head}</tr>{''.join(body)}</table>"

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<title>PriceRadar Batch {esc(str(agg.get('batch_id')))}</title>
<style>
body{{font-family:"Segoe UI","Microsoft YaHei",sans-serif;margin:24px;color:#222}}
table{{border-collapse:collapse;width:100%;font-size:0.85em}}
th,td{{border:1px solid #ccc;padding:4px 6px;vertical-align:top}}
th{{background:#f5f5f5}}
.card{{border:1px solid #ddd;padding:12px 16px;margin:12px 0;border-radius:6px}}
.muted{{color:#666}}
</style></head><body>
<h1>PriceRadar 多SKU批量报告</h1>
<p class="muted">batch_id={esc(str(agg.get('batch_id')))} · rules={esc(RULES_VERSION)}</p>
<div class="card">
<h2>汇总统计</h2>
<ul>
<li>输入有效SKU：{esc(str(st.get('valid_skus')))}</li>
<li>流程：完成 {esc(str(st.get('sku_done')))} / 失败 {esc(str(st.get('sku_failed')))} /
暂停未执行 {esc(str(st.get('sku_paused_unstarted')))} /
中断 {esc(str(st.get('sku_interrupted')))} / 待办 {esc(str(st.get('sku_pending')))}</li>
<li>候选总数：{esc(str(st.get('candidates_total')))} · 详情访问：{esc(str(st.get('detail_pages_opened_total')))}</li>
<li>疑似破价商品数：{esc(str(st.get('products_possible_break_total')))}</li>
<li>主展示低价线索数：{esc(str(st.get('products_primary_low_price_signal_total')))}</li>
<li>业务待核验SKU数（完成子集）：{esc(str(st.get('business_review_skus_among_done')))}</li>
</ul>
<p class="muted">{esc('; '.join(st.get('stat_notes') or []))}</p>
<p>批次状态：{esc(str(agg.get('checkpoint_status')))}
 pause={esc(str(agg.get('pause_reason')))}</p>
</div>
<div class="card"><h2>SKU汇总（最低价不跨SKU）</h2>
{table(rows, ['item_id','business_sku','status','candidates_found','detail_pages_opened','products_possible_break','products_primary_low_price_signal','lowest_comparable_take_home','lowest_conditions','evidence_captured_at_utc','error'])}
</div>
<div class="card"><h2>输入错误</h2>
{table(agg.get('input_errors') or [], ['excel_row','raw_sku','errors'])}
</div>
<p class="muted">不得称全平台最低价；manual_verdict 系统不代填；历史复用不得自动兜底。</p>
</body></html>
"""


def write_batch_workbook(path: Path, agg: dict[str, Any], parse_result: dict[str, Any]) -> None:
    wb = Workbook()

    def fill(name: str, rows: list[dict[str, Any]], preferred_keys: list[str] | None = None):
        ws = wb.create_sheet(name)
        if not rows:
            ws.cell(1, 1, "（无数据）")
            return
        keys = preferred_keys or list(rows[0].keys())
        for c, k in enumerate(keys, start=1):
            ws.cell(1, c, k)
            ws.column_dimensions[get_column_letter(c)].width = 16
        for r_i, row in enumerate(rows, start=2):
            for c, k in enumerate(keys, start=1):
                val = row.get(k)
                if isinstance(val, (list, dict)):
                    val = json.dumps(val, ensure_ascii=False)
                text = "" if val is None else str(val)
                if text[:1] in {"=", "+", "-", "@"}:
                    text = "'" + text
                ws.cell(r_i, c, text)

    # remove default
    default = wb.active
    wb.remove(default)

    fill("SKU汇总", agg.get("sku_summaries") or [])
    fill("商品明细", agg.get("product_rows") or [])
    fill("价格方案", agg.get("price_options") or [])
    err_rows = []
    for e in agg.get("input_errors") or []:
        err_rows.append(
            {
                "excel_row": e.get("excel_row"),
                "raw_sku": e.get("raw_sku"),
                "errors": "; ".join(e.get("errors") or []),
            }
        )
    fill("输入错误", err_rows)
    notes = [
        {"项": "rules_version", "值": RULES_VERSION},
        {"项": "batch_id", "值": agg.get("batch_id")},
        {"项": "说明", "值": "最低可比到手价不跨SKU；synthetic/test 行不得伪称京东实测"},
        {
            "项": "统计互斥",
            "值": "sku_done/failed/paused/interrupted/pending 互斥；业务待核验是完成子集",
        },
        {
            "项": "复用政策",
            "值": "历史候选不得在搜索受限后自动兜底；仅显式离线/复用模式",
        },
    ]
    fill("运行说明", notes)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def run_validate_only(excel_path: Path, out_dir: Path | None = None) -> dict[str, Any]:
    parsed = excel_in.parse_batch_excel(excel_path)
    stamp = utc_stamp()
    out = out_dir or (
        PROJECT_ROOT
        / "artifacts"
        / "batch_jobs"
        / f"_validate_{stamp}_{secrets.token_hex(2)}"
    )
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(excel_path, out / "input_snapshot.xlsx")
    write_json_atomic(out / "validation_result.json", parsed)
    preview_rows = []
    for it in parsed.get("valid_items") or []:
        preview_rows.append(
            {
                "item_id": it.get("item_id"),
                "excel_row": it.get("excel_row"),
                "business_sku": it.get("business_sku"),
                "expected_model": it.get("expected_model"),
                "red_line": it.get("red_line"),
                "currency": it.get("currency"),
                "comparison_unit": it.get("comparison_unit"),
                "packs_per_box": it.get("packs_per_box"),
                "target_spec": it.get("target_spec"),
                "packaging_complete": it.get("packaging_complete"),
                "search_query": it.get("search_query"),
                "row_kind": it.get("row_kind"),
                "warnings": "; ".join(it.get("warnings") or []),
            }
        )
    write_json_atomic(out / "input_preview.json", preview_rows)
    # tiny html
    (out / "validation_preview.html").write_text(
        "<html><meta charset='utf-8'/><body><h1>输入预览（未访问京东）</h1>"
        f"<pre>{html.escape(json.dumps(parsed.get('counts'), ensure_ascii=False, indent=2))}</pre>"
        f"<pre>{html.escape(json.dumps(preview_rows, ensure_ascii=False, indent=2))}</pre>"
        f"<h2>拒绝</h2><pre>{html.escape(json.dumps(parsed.get('rejected'), ensure_ascii=False, indent=2))}</pre>"
        "</body></html>",
        encoding="utf-8",
    )
    return {
        "ok": bool(parsed.get("has_valid_items")),
        "out_dir": str(out),
        "parsed": parsed,
    }


def run_batch(
    *,
    excel_path: Path,
    max_candidates: int | None = None,
    max_pages: int | None = None,
    search_timeout_seconds: int | None = None,
    max_verify: int | None = None,
    max_verify_total: int | None = None,
    resume_batch_id: str | None = None,
    search_fn: Callable[..., dict[str, Any]] | None = None,
    verify_fn: Callable[..., dict[str, Any]] | None = None,
    allow_browser: bool = True,
) -> dict[str, Any]:
    excel_path = Path(excel_path).resolve()
    parsed = excel_in.parse_batch_excel(excel_path)
    if not parsed.get("has_valid_items"):
        raise ValueError(
            "没有任何有效启用行，不启动浏览器。"
            f" rejected={parsed.get('rejected')} skipped={parsed.get('skipped')}"
        )

    budgets = resolve_batch_budgets(
        max_candidates=max_candidates,
        max_pages=max_pages,
        search_timeout_seconds=search_timeout_seconds,
        max_verify=max_verify,
        max_verify_total=max_verify_total,
    )

    if resume_batch_id:
        batch_dir = BATCH_ROOT / resume_batch_id
        if not batch_dir.is_dir():
            raise FileNotFoundError(f"恢复批次不存在: {batch_dir}")
        checkpoint = read_json(batch_dir / "checkpoint.json")
        if checkpoint.get("input_fingerprint") != parsed.get("input_fingerprint"):
            raise RuntimeError(
                "输入指纹变更，不能静默混用旧结果；请新建批次或修正输入后重试"
            )
        if checkpoint.get("rules_version") != RULES_VERSION:
            raise RuntimeError(
                f"规则版本变更（checkpoint={checkpoint.get('rules_version')} "
                f"current={RULES_VERSION}），请新建批次"
            )
        batch_id = resume_batch_id
        # rebuild item list from checkpoint order + current parse by sku
        by_sku = {it["business_sku"]: it for it in parsed["valid_items"]}
        items: list[dict[str, Any]] = []
        for ci in checkpoint["items"]:
            src = by_sku.get(ci["business_sku"])
            if not src:
                raise RuntimeError(
                    f"恢复失败：checkpoint 中的 SKU {ci['business_sku']} 不在当前输入"
                )
            # config fingerprint check
            items.append(src)
    else:
        batch_id = make_batch_id()
        batch_dir = BATCH_ROOT / batch_id
        batch_dir.mkdir(parents=True, exist_ok=False)
        items = list(parsed["valid_items"])
        checkpoint = empty_checkpoint(
            batch_id=batch_id,
            input_fingerprint=parsed["input_fingerprint"],
            excel_path=str(excel_path),
            budgets=budgets,
            items=items,
        )
        shutil.copy2(excel_path, batch_dir / "input_snapshot.xlsx")
        write_json_atomic(batch_dir / "validation_result.json", parsed)
        save_checkpoint(batch_dir, checkpoint)

    lock = BatchLock(batch_dir)
    lock.acquire()
    detail_used_total = 0
    item_results: list[dict[str, Any]] = []
    # Load prior results for DONE items when resuming
    prior_results_path = batch_dir / "item_results.json"
    prior_by_id: dict[str, dict[str, Any]] = {}
    if prior_results_path.exists():
        try:
            for row in read_json(prior_results_path):
                prior_by_id[row["item"]["item_id"]] = row
        except (OSError, json.JSONDecodeError, KeyError):
            prior_by_id = {}

    try:
        checkpoint["status"] = "RUNNING"
        save_checkpoint(batch_dir, checkpoint)

        for idx, item in enumerate(items):
            cp_item = checkpoint["items"][idx]
            item_id = item["item_id"]

            if cp_item.get("status") == "DONE" and item_id in prior_by_id:
                # Keep original evidence time; do not re-scrape
                kept = deepcopy(prior_by_id[item_id])
                kept["resumed_skipped_rerun"] = True
                item_results.append(kept)
                detail_used_total += int(
                    (kept.get("sku_summary") or {}).get("detail_pages_opened") or 0
                )
                continue

            if checkpoint.get("status") == "PAUSED_SESSION":
                cp_item["status"] = "PAUSED_UNSTARTED"
                item_results.append(
                    {
                        "item": item,
                        "status": "PAUSED_UNSTARTED",
                        "error": f"批次已暂停: {checkpoint.get('pause_reason')}",
                        "sku_summary": {},
                        "product_rows": [],
                        "price_options": [],
                        "evidence_captured_at_utc": None,
                    }
                )
                continue

            if detail_used_total >= int(budgets["max_verify_total"]):
                cp_item["status"] = "PAUSED_UNSTARTED"
                item_results.append(
                    {
                        "item": item,
                        "status": "PAUSED_UNSTARTED",
                        "error": (
                            f"整批详情预算耗尽 max_verify_total="
                            f"{budgets['max_verify_total']}"
                        ),
                        "sku_summary": {},
                        "product_rows": [],
                        "price_options": [],
                        "evidence_captured_at_utc": None,
                    }
                )
                continue

            item_dir = batch_dir / "items" / item_id
            item_dir.mkdir(parents=True, exist_ok=True)
            cfg_path = item_dir / "business_config.json"
            excel_in.write_item_config(item, cfg_path)
            cfg_fp = fingerprint_file(cfg_path)

            if (
                resume_batch_id
                and cp_item.get("config_fingerprint")
                and cp_item["config_fingerprint"] != cfg_fp
            ):
                raise RuntimeError(
                    f"item {item_id} 业务配置指纹变更，拒绝恢复混用；请新建批次"
                )

            # New attempt directory for interrupted retries
            attempt = int(cp_item.get("attempt") or 0) + 1
            sku_job_dir = item_dir / f"attempt_{attempt:02d}"
            if sku_job_dir.exists():
                sku_job_dir = item_dir / f"attempt_{attempt:02d}_{secrets.token_hex(2)}"

            cp_item["status"] = "RUNNING"
            cp_item["attempt"] = attempt
            cp_item["started_at_utc"] = utc_iso()
            cp_item["config_path"] = str(cfg_path)
            cp_item["config_fingerprint"] = cfg_fp
            cp_item["sku_job_dir"] = str(sku_job_dir)
            # Preserve any manual fields file if present
            manual_path = item_dir / "manual_overrides.json"
            save_checkpoint(batch_dir, checkpoint)

            print(
                f"=== batch item {item_id} sku={item['business_sku']} "
                f"attempt={attempt} ==="
            )
            try:
                if not allow_browser and search_fn is None:
                    raise RuntimeError(
                        "allow_browser=False 且未注入 search_fn；离线模式需要注入"
                    )
                result = p5b.run_sku_job(
                    business_config=cfg_path,
                    query=item.get("search_query"),
                    max_candidates=budgets["max_candidates"],
                    max_pages=budgets["max_pages"],
                    search_timeout_seconds=budgets["search_timeout_seconds"],
                    max_verify=budgets["max_verify"],
                    max_candidates_explicit=True,
                    max_verify_explicit=True,
                    search_fn=search_fn,
                    verify_fn=verify_fn,
                    job_dir=sku_job_dir,
                    job_id=f"{batch_id}_{item_id}",
                )
                summary = result.get("summary") or {}
                # Restore manual overrides if any
                if manual_path.exists():
                    try:
                        overrides = read_json(manual_path)
                        for row in result.get("product_rows") or []:
                            key = str(row.get("jd_product_id") or "")
                            if key in overrides and "manual_verdict" in overrides[key]:
                                row["manual_verdict"] = overrides[key]["manual_verdict"]
                    except (OSError, json.JSONDecodeError):
                        pass

                evidence_time = summary.get("ended_at_utc") or utc_iso()
                status = "DONE"
                search_status = (result.get("search_result") or {}).get("run_status")
                if search_status in p5b.SEARCH_BLOCK_STATUSES:
                    checkpoint["status"] = "PAUSED_SESSION"
                    checkpoint["pause_reason"] = search_status
                    status = "DONE"  # this item finished with restricted search
                # Also pause if verify paused remaining inside sku job
                if int(summary.get("candidates_paused_unvisited") or 0) > 0:
                    if checkpoint.get("status") != "PAUSED_SESSION":
                        for r in result.get("product_rows") or []:
                            if r.get("match_status") in p5b.SESSION_PAUSE_MATCH:
                                checkpoint["status"] = "PAUSED_SESSION"
                                checkpoint["pause_reason"] = r.get("match_status")
                                break

                detail_used_total += int(summary.get("detail_pages_opened") or 0)
                ir = {
                    "item": item,
                    "status": status,
                    "sku_job_dir": str(sku_job_dir),
                    "sku_summary": summary,
                    "product_rows": result.get("product_rows") or [],
                    "price_options": result.get("price_options") or [],
                    "evidence_captured_at_utc": evidence_time,
                    "error": None,
                    "search_status": search_status,
                }
                item_results.append(ir)
                cp_item["status"] = status
                cp_item["ended_at_utc"] = utc_iso()
                cp_item["evidence_captured_at_utc"] = evidence_time
                cp_item["summary_excerpt"] = {
                    "candidates_found": summary.get("candidates_found"),
                    "detail_pages_opened": summary.get("detail_pages_opened"),
                    "products_possible_break": summary.get("products_possible_break"),
                }
                cp_item["error"] = None
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
                print(f"item failed: {err}")
                # Session-level?
                msg = str(exc)
                if any(
                    x in msg
                    for x in (
                        "LOGIN_REQUIRED",
                        "VERIFICATION_REQUIRED",
                        "SEARCH_RESTRICTED",
                    )
                ):
                    checkpoint["status"] = "PAUSED_SESSION"
                    checkpoint["pause_reason"] = msg
                    cp_item["status"] = "FAILED"
                else:
                    cp_item["status"] = "FAILED"
                cp_item["ended_at_utc"] = utc_iso()
                cp_item["error"] = err
                item_results.append(
                    {
                        "item": item,
                        "status": "FAILED",
                        "sku_job_dir": str(sku_job_dir),
                        "sku_summary": {},
                        "product_rows": [],
                        "price_options": [],
                        "evidence_captured_at_utc": None,
                        "error": err,
                    }
                )

            save_checkpoint(batch_dir, checkpoint)
            write_json_atomic(batch_dir / "item_results.json", item_results)

            if checkpoint.get("status") == "PAUSED_SESSION":
                # mark remaining
                for j in range(idx + 1, len(items)):
                    if checkpoint["items"][j]["status"] in {"PENDING", "RUNNING"}:
                        checkpoint["items"][j]["status"] = "PAUSED_UNSTARTED"
                save_checkpoint(batch_dir, checkpoint)
                break

        else:
            checkpoint["status"] = "DONE"

        # Append PAUSED_UNSTARTED placeholders not yet in item_results
        have = {r["item"]["item_id"] for r in item_results}
        for ci, item in zip(checkpoint["items"], items):
            if item["item_id"] in have:
                continue
            if ci["status"] == "PAUSED_UNSTARTED":
                item_results.append(
                    {
                        "item": item,
                        "status": "PAUSED_UNSTARTED",
                        "error": checkpoint.get("pause_reason") or "paused",
                        "sku_summary": {},
                        "product_rows": [],
                        "price_options": [],
                        "evidence_captured_at_utc": None,
                    }
                )

        if checkpoint["status"] == "RUNNING":
            checkpoint["status"] = "DONE"
        save_checkpoint(batch_dir, checkpoint)
        write_json_atomic(batch_dir / "item_results.json", item_results)

        agg = aggregate_batch(
            batch_id=batch_id,
            checkpoint=checkpoint,
            item_results=item_results,
            parse_result=parsed,
        )
        write_json_atomic(batch_dir / "batch_summary.json", agg)
        write_json_atomic(batch_dir / "sku_summaries.json", agg["sku_summaries"])
        write_json_atomic(batch_dir / "product_rows.json", agg["product_rows"])
        write_json_atomic(batch_dir / "price_options.json", agg["price_options"])
        (batch_dir / "report.html").write_text(
            render_batch_html(agg), encoding="utf-8"
        )
        write_batch_workbook(batch_dir / "batch_results.xlsx", agg, parsed)

        return {
            "ok": checkpoint["status"] == "DONE",
            "batch_id": batch_id,
            "batch_dir": str(batch_dir),
            "checkpoint": checkpoint,
            "aggregate": agg,
        }
    except Exception:
        # Mark running item interrupted
        for ci in checkpoint.get("items") or []:
            if ci.get("status") == "RUNNING":
                ci["status"] = "INTERRUPTED"
                ci["ended_at_utc"] = utc_iso()
                ci["error"] = "process interrupted; retry will use new evidence dir"
        checkpoint["status"] = "INTERRUPTED"
        save_checkpoint(batch_dir, checkpoint)
        raise
    finally:
        lock.release()


def run_offline_self_checks() -> int:
    failures: list[str] = []
    failures.extend(excel_in.run_excel_self_checks())

    tmp = PROJECT_ROOT / "artifacts" / "_tmp_batch_checks"
    tmp.mkdir(parents=True, exist_ok=True)
    tpl = excel_in.write_batch_input_template(tmp / "batch_tpl.xlsx")
    # Offline two-SKU uses fixtures (not business template defaults)
    offline_xlsx = excel_in.write_synthetic_batch_fixture(tmp / "two_sku_offline.xlsx")
    from openpyxl import load_workbook

    wb = load_workbook(offline_xlsx)
    ws = wb["输入"]
    for col in range(1, ws.max_column + 1):
        key = excel_in.HEADER_TO_KEY.get(str(ws.cell(1, col).value or "").strip())
        if key == "business_sku":
            ws.cell(3, col, "TEST_SYNTH_BATCH")
        if key == "expected_model":
            ws.cell(3, col, "TEST_SYNTH_BATCH")
        if key == "red_line":
            ws.cell(3, col, "50.00")
        if key == "packs_per_box":
            ws.cell(3, col, 10)
        if key == "search_query":
            ws.cell(3, col, "synthetic offline query")
        if key == "row_kind":
            ws.cell(3, col, "test")
        if key == "enabled":
            ws.cell(3, col, "Y")
        if key == "currency":
            ws.cell(3, col, "CNY")
        if key == "comparison_unit":
            ws.cell(3, col, "箱")
        if key == "target_spec":
            ws.cell(3, col, "测试100张×10包")
    wb.save(offline_xlsx)

    parsed = excel_in.parse_batch_excel(offline_xlsx)
    if parsed["counts"]["valid"] != 2:
        failures.append(f"expected 2 valid skus, got {parsed['counts']}")
    skus = {x["business_sku"]: x for x in parsed["valid_items"]}
    if skus["B913A"]["red_line"] == skus["TEST_SYNTH_BATCH"]["red_line"]:
        failures.append("red lines must differ across SKUs")
    if skus["B913A"]["packs_per_box"] == skus["TEST_SYNTH_BATCH"]["packs_per_box"]:
        failures.append("packs_per_box must differ")

    # Fake search/verify — no browser
    calls: list[str] = []

    def fake_search(query, limit=None, max_candidates=None, **kwargs):
        out = Path(kwargs["out_dir"])
        n = int(max_candidates or limit or 3)
        # Fail second SKU's search as ordinary empty? or success
        cands = []
        if "synthetic" in str(query).lower() or "TEST" in str(query):
            # ordinary failure path: one candidate then verify fails
            cands = [
                {
                    "query": query,
                    "rank": 1,
                    "title": f"{query} B913A-like",
                    "product_id": "900001",
                    "shop_name": "测试店",
                    "ad_marked": False,
                    "normalized_url": "https://item.jd.com/900001.html",
                    "observed_url": "https://item.jd.com/900001.html",
                }
            ]
        else:
            cands = [
                {
                    "query": query,
                    "rank": 1,
                    "title": "清风 B913A 20包",
                    "product_id": "800001",
                    "shop_name": "店",
                    "ad_marked": False,
                    "normalized_url": "https://item.jd.com/800001.html",
                    "observed_url": "https://item.jd.com/800001.html",
                }
            ][:n]
        p5b.p3a.write_candidates_json(out / "candidates.json", cands)
        meta = {
            "run_status": "SUCCESS",
            "candidate_count": len(cands),
            "unique_candidates_collected": len(cands),
            "coverage_stop_reason": "HIT_CANDIDATE_LIMIT",
            "max_candidates": n,
            "query": query,
            "extract_stats": {},
            "scroll": {},
        }
        p5b.p3a.write_metadata(out / "run_metadata.json", meta)
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

    def fake_verify(**kwargs):
        pid = str(kwargs["product_id"])
        calls.append(pid)
        out = Path(kwargs["out_dir"])
        out.mkdir(parents=True, exist_ok=True)
        if pid == "900001":
            # ordinary failure — continue batch
            return {
                "task_status": "FAILED",
                "match_status": "OCR_FAILED",
                "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                "system_verdict_zh": "待核验",
                "manual_verdict": None,
                "price_clues": [],
                "ended_at_utc": utc_iso(),
                "verify_dir": str(out),
                "candidate": {"product_id": pid, "title": "x"},
            }
        return {
            "task_status": "COMPLETED",
            "match_status": "PACKAGING_PASSED",
            "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
            "system_verdict_zh": "到手价待核验",
            "manual_verdict": None,
            "page_primary_price": "168.00",
            "packaging_match": "PASSED",
            "box_comparable": True,
            "price_clues": [
                {
                    "price_type": take_home.PRICE_TYPE_PAGE_PRIMARY,
                    "amount": "168.00",
                    "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                    "manual_verdict": None,
                    "normalized_price": None,
                    "review_reasons": ["到手价待核验"],
                }
            ],
            "ended_at_utc": utc_iso(),
            "verify_dir": str(out),
            "candidate": {"product_id": pid, "title": "清风 B913A"},
            "url_adapt": {
                "adapted_url": f"https://item.m.jd.com/product/{pid}.html",
                "final_url_sanitized": f"https://item.m.jd.com/product/{pid}.html",
            },
        }

    batch1 = run_batch(
        excel_path=offline_xlsx,
        max_candidates=3,
        max_pages=1,
        max_verify=2,
        max_verify_total=10,
        search_fn=fake_search,
        verify_fn=fake_verify,
        allow_browser=False,
    )
    if not batch1.get("batch_id"):
        failures.append("batch id missing")
    agg = batch1["aggregate"]
    if agg["stats"]["valid_skus"] != 2:
        failures.append("valid_skus")
    if agg["stats"]["sku_done"] != 2:
        failures.append(f"both skus should complete process, got {agg['stats']}")
    # red lines not crossed in summaries
    by = {s["business_sku"]: s for s in agg["sku_summaries"]}
    if by["B913A"]["red_line"] == by["TEST_SYNTH_BATCH"]["red_line"]:
        failures.append("aggregate red_line leak")

    # Session pause: search restricted on first sku pauses rest
    def fake_search_restrict(query, **kwargs):
        out = Path(kwargs["out_dir"])
        p5b.p3a.write_candidates_json(out / "candidates.json", [])
        meta = {
            "run_status": "SEARCH_RESTRICTED",
            "candidate_count": 0,
            "unique_candidates_collected": 0,
            "coverage_stop_reason": "SEARCH_RESTRICTED",
            "query": query,
            "extract_stats": {},
            "scroll": {},
        }
        p5b.p3a.write_metadata(out / "run_metadata.json", meta)
        return {
            "ok": False,
            "exit_code": 1,
            "run_id": out.name,
            "out_dir": out,
            "candidates_path": out / "candidates.json",
            "candidates": [],
            "run_status": "SEARCH_RESTRICTED",
            "coverage_stop_reason": "SEARCH_RESTRICTED",
            "metadata": meta,
            "close_ok": True,
        }

    batch2 = run_batch(
        excel_path=offline_xlsx,
        max_verify=1,
        max_verify_total=5,
        search_fn=fake_search_restrict,
        verify_fn=fake_verify,
        allow_browser=False,
    )
    cp2 = batch2["checkpoint"]
    if cp2.get("status") != "PAUSED_SESSION":
        failures.append("restricted search should pause batch")
    paused_n = sum(
        1 for it in cp2["items"] if it["status"] == "PAUSED_UNSTARTED"
    )
    if paused_n < 1:
        failures.append("remaining sku should be PAUSED_UNSTARTED")

    # Resume fingerprint change rejection
    try:
        run_batch(
            excel_path=tpl,  # different fingerprint (only 1 valid)
            resume_batch_id=batch1["batch_id"],
            search_fn=fake_search,
            verify_fn=fake_verify,
            allow_browser=False,
        )
        failures.append("fingerprint change should refuse resume")
    except RuntimeError:
        pass

    # DONE items not re-run on resume of batch1
    calls.clear()
    batch1_resume = run_batch(
        excel_path=offline_xlsx,
        resume_batch_id=batch1["batch_id"],
        search_fn=fake_search,
        verify_fn=fake_verify,
        allow_browser=False,
    )
    if calls:
        failures.append("resume should not re-verify DONE items")
    # evidence time preserved
    for ir in read_json(Path(batch1["batch_dir"]) / "item_results.json"):
        if ir.get("status") == "DONE" and not ir.get("evidence_captured_at_utc"):
            failures.append("evidence time missing on done item")

    # Total budget
    def fake_search_many(query, max_candidates=None, **kwargs):
        out = Path(kwargs["out_dir"])
        cands = [
            {
                "query": query,
                "rank": i,
                "title": f"清风 B913A {i}",
                "product_id": str(700000 + i),
                "shop_name": "店",
                "ad_marked": False,
                "normalized_url": f"https://item.jd.com/{700000+i}.html",
                "observed_url": f"https://item.jd.com/{700000+i}.html",
            }
            for i in range(1, 4)
        ]
        p5b.p3a.write_candidates_json(out / "candidates.json", cands)
        meta = {
            "run_status": "SUCCESS",
            "candidate_count": 3,
            "unique_candidates_collected": 3,
            "coverage_stop_reason": "HIT_CANDIDATE_LIMIT",
            "query": query,
            "extract_stats": {},
            "scroll": {},
        }
        p5b.p3a.write_metadata(out / "run_metadata.json", meta)
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

    batch3 = run_batch(
        excel_path=offline_xlsx,
        max_verify=2,
        max_verify_total=2,  # first sku consumes 2, second paused
        search_fn=fake_search_many,
        verify_fn=fake_verify,
        allow_browser=False,
    )
    st3 = batch3["aggregate"]["stats"]
    if st3.get("detail_pages_opened_total", 0) > 2:
        failures.append("max_verify_total should cap detail opens")
    if st3.get("sku_paused_unstarted", 0) < 1:
        failures.append("second sku should pause on total budget")

    # validate-only
    v = run_validate_only(tpl, out_dir=tmp / "validate_out")
    if not v.get("ok"):
        failures.append("validate-only should succeed for template")

    print("=== P5-D offline self-check ===")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK")
    print(f"sample_batch_dir={batch1['batch_dir']}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="P5-D multi-SKU Excel batch")
    p.add_argument("--excel", type=Path, default=None, help="Input .xlsx path")
    p.add_argument(
        "--write-template",
        action="store_true",
        help="Write templates/batch_sku_input_template.xlsx and exit",
    )
    p.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate Excel and write preview; no JD browser",
    )
    p.add_argument("--resume", type=str, default=None, help="Resume batch_id")
    p.add_argument("--max-candidates", type=int, default=None)
    p.add_argument("--max-pages", type=int, default=None)
    p.add_argument("--search-timeout-seconds", type=int, default=None)
    p.add_argument("--max-verify", type=int, default=None)
    p.add_argument(
        "--max-verify-total",
        type=int,
        default=None,
        help="Whole-batch detail visit budget",
    )
    p.add_argument("--self-check", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_check:
        return run_offline_self_checks()
    if args.write_template:
        path = excel_in.write_batch_input_template()
        print(path)
        return 0
    if args.validate_only:
        if not args.excel:
            # default to template
            excel_in.write_batch_input_template()
            args.excel = excel_in.DEFAULT_TEMPLATE_PATH
        result = run_validate_only(Path(args.excel))
        print(f"out_dir={result['out_dir']}")
        print(f"valid={result['parsed'].get('counts')}")
        return 0 if result.get("ok") else 2
    if not args.excel and not args.resume:
        print("需要 --excel 或 --resume", file=sys.stderr)
        return 2
    try:
        # Live browser only when not self-check/validate; user may run when ready
        result = run_batch(
            excel_path=Path(args.excel)
            if args.excel
            else Path(
                read_json(BATCH_ROOT / args.resume / "checkpoint.json")["excel_path"]
            ),
            max_candidates=args.max_candidates,
            max_pages=args.max_pages,
            search_timeout_seconds=args.search_timeout_seconds,
            max_verify=args.max_verify,
            max_verify_total=args.max_verify_total,
            resume_batch_id=args.resume,
            allow_browser=True,
        )
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        print(f"错误: {exc}")
        return 2
    print(f"batch_dir={result['batch_dir']}")
    print(f"status={result['checkpoint'].get('status')}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
