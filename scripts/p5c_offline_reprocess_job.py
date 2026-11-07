"""
P5-C: offline reprocess of an existing P5-B sku job.

- Does not overwrite the source job directory.
- Writes evidence diagnosis + low_price_signal enrichment + corrected stats.
- Marks offline_reprocess + source_job_id.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import secrets
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
JOB_ROOT = (PROJECT_ROOT / "artifacts" / "sku_jobs").resolve()

TAKE_HOME_LABEL_HINTS = ("到手", "券后", "会员", "新人", "预估")


def load_sibling(stem: str) -> Any:
    path = SCRIPTS_DIR / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(f"priceradar_{stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


p5b = load_sibling("p5b_run_sku")
take_home = load_sibling("priceradar_take_home_prices")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def collect_ocr_texts(pipeline_dir: Path | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not pipeline_dir or not pipeline_dir.is_dir():
        return out
    ocr_root = pipeline_dir / "ocr"
    if not ocr_root.is_dir():
        return out
    for role_dir in sorted(ocr_root.iterdir()):
        if not role_dir.is_dir():
            continue
        result = role_dir / "ocr_result.json"
        if not result.exists():
            continue
        try:
            data = read_json(result)
        except (OSError, json.JSONDecodeError):
            continue
        texts = []
        for block in data.get("blocks") or []:
            t = str(block.get("text") or "").strip()
            if t:
                texts.append(t)
        out[role_dir.name] = "\n".join(texts)
    return out


def parse_evidence_paths(row: dict[str, Any]) -> dict[str, Path | None]:
    paths = str(row.get("evidence_paths") or "").split(";")
    capture = pipeline = verify = None
    for p in paths:
        p = p.strip()
        if not p:
            continue
        path = Path(p)
        s = str(path).replace("\\", "/").lower()
        if "price_captures" in s:
            capture = path
        elif "pipeline_runs" in s:
            pipeline = path
        elif "candidates" in s or "sku_jobs" in s:
            verify = path
    return {"capture": capture, "pipeline": pipeline, "verify": verify}


def list_capture_files(capture_dir: Path | None) -> list[str]:
    if not capture_dir or not capture_dir.is_dir():
        return []
    return sorted(p.name for p in capture_dir.iterdir() if p.is_file() and p.suffix == ".png")


def diagnose_product(row: dict[str, Any]) -> dict[str, Any]:
    """
    Per-product offline evidence diagnosis.
    Does not invent page discounts when evidence is absent.
    """
    pid = str(row.get("jd_product_id") or "")
    rank = row.get("search_rank")
    ev = parse_evidence_paths(row)
    capture_files = list_capture_files(ev["capture"])
    ocr_texts = collect_ocr_texts(ev["pipeline"])
    all_ocr = "\n".join(ocr_texts.values())
    price_ocr = ocr_texts.get("price") or ""
    promo_ocr = ocr_texts.get("promotions") or ""

    labels_in_ocr = [h for h in TAKE_HOME_LABEL_HINTS if h in all_ocr]
    price_has_digit = bool(re.search(r"\d", price_ocr))
    price_png = "price.png" in capture_files
    title_png = "product_title.png" in capture_files
    promo_png = "promotions.png" in capture_files
    price_context_png = "price_context.png" in capture_files

    # Root-cause classification (priority order)
    root_cause = "UNKNOWN_NO_TAKE_HOME_TEXT_IN_EVIDENCE"
    detail = (
        "已保存证据中未见到手/券后/会员/新人/预估文案；"
        "不能据此断言页面不存在优惠，仅能判定本次证据不足。"
    )
    category = "evidence_absent_keep_unknown"

    if row.get("system_verdict") == "MODEL_MISMATCH_SKIPPED":
        root_cause = "MODEL_MISMATCH_SKIPPED"
        detail = "标题门控型号不符，未打开详情，无价格证据。"
        category = "skipped"
    elif not price_png and row.get("task_status") == "COMPLETED":
        root_cause = "PRICE_SCREENSHOT_MISSING"
        detail = "详情已访问但缺少 price.png。"
        category = "screenshot_gap"
    elif price_png and price_has_digit and not labels_in_ocr:
        # Tight digit crop observed historically; labels may exist off-crop.
        root_cause = "SCREENSHOT_CONTEXT_GAP"
        detail = (
            "price.png 主要为清晰数字（及少量角标），OCR 未读出到手类标签；"
            "优惠区若仅有满赠门槛则不属于到手价。"
            "属截图证据覆盖不足（非已证实的 OCR 漏读标签）。"
        )
        category = "screenshot_gap"
        if not title_png:
            detail += " 另：缺少 product_title.png，型号核验证据不完整。"
    elif labels_in_ocr and not (
        (row.get("price_options_note") or "")  # placeholder
    ):
        # If OCR has take-home text but product only has PAGE_PRIMARY → parse gap.
        # Confirmed later via price_options inspect.
        pass

    clues_path = None
    if ev["verify"]:
        candidate = ev["verify"] / "price_clues.json"
        if candidate.exists():
            clues_path = candidate
    price_types: list[str] = []
    if clues_path and clues_path.exists():
        try:
            clues = read_json(clues_path)
            if isinstance(clues, list):
                price_types = [str(c.get("price_type") or "") for c in clues]
        except (OSError, json.JSONDecodeError):
            pass

    if labels_in_ocr and price_types and all(
        t == "PAGE_PRIMARY_SELLING" or not t for t in price_types
    ):
        root_cause = "PARSE_GAP"
        detail = "OCR 已出现到手类文案，但未生成到手价方案 → 解析归属问题。"
        category = "parse_gap"
    elif (
        not labels_in_ocr
        and price_png
        and any(h in (price_ocr + promo_ocr) for h in ())  # noop keep branch clear
    ):
        pass

    # If screenshot likely contains labels but OCR missed — would need visual proof.
    # We only flag OCR_GAP when price_context exists and OCR still misses while
    # metadata claims label coverage (not available offline for old jobs).

    return {
        "jd_product_id": pid,
        "search_rank": rank,
        "title": row.get("title"),
        "primary_display_price": row.get("primary_display_price"),
        "task_status": row.get("task_status"),
        "match_status": row.get("match_status"),
        "packaging_match": row.get("packaging_match"),
        "box_comparable": row.get("box_comparable"),
        "capture_dir": str(ev["capture"]) if ev["capture"] else None,
        "pipeline_dir": str(ev["pipeline"]) if ev["pipeline"] else None,
        "capture_pngs": capture_files,
        "ocr_roles": {k: (v[:240] + ("…" if len(v) > 240 else "")) for k, v in ocr_texts.items()},
        "take_home_labels_in_ocr": labels_in_ocr,
        "price_clue_types": price_types,
        "has_price_context_png": price_context_png,
        "has_product_title_png": title_png,
        "has_promotions_png": promo_png,
        "diagnosis_category": category,
        "root_cause": root_cause,
        "detail": detail,
        "answers": {
            "1_screenshot_price_labels_or_digits_only": (
                "主要为数字+角标；未见到手/券后标签覆盖"
                if price_png and not labels_in_ocr
                else (
                    "含标签线索" if labels_in_ocr else "无价格截图或不适用"
                )
            ),
            "2_take_home_copy_visible_in_saved_shots": (
                "已保存截图/OCR 未出现到手/券后/会员/新人/预估"
                if not labels_in_ocr
                else f"OCR 出现: {','.join(labels_in_ocr)}"
            ),
            "3_ocr_miss_if_visible": (
                "本次离线无法证明截图内有而 OCR 未读；"
                "现有 price 裁切偏紧，优先判截图上下文不足"
            ),
            "4_parse_gap_if_ocr_has_text": (
                "否（OCR 无到手类文案可解析）"
                if not labels_in_ocr
                else "若有文案但无方案则属解析问题"
            ),
            "5_screenshot_coverage_gap": category == "screenshot_gap",
            "6_cannot_infer_page_has_no_promo": True,
        },
    }


def reprocess_job(source_job: Path) -> dict[str, Any]:
    source_job = source_job.resolve()
    if not source_job.is_dir():
        raise FileNotFoundError(source_job)
    product_rows = read_json(source_job / "product_rows.json")
    price_options = read_json(source_job / "price_options.json")
    summary_src = read_json(source_job / "sku_summary.json")
    search_meta = {}
    search_meta_path = source_job / "search" / "run_metadata.json"
    if search_meta_path.exists():
        search_meta = read_json(search_meta_path)

    diagnoses = [diagnose_product(r) for r in product_rows]
    enriched_rows = []
    for row in product_rows:
        new_row = dict(row)
        # Preserve packaging/box fields; recompute signal only.
        take_home.apply_low_price_signal_fields(new_row)
        enriched_rows.append(new_row)

    business_sku = str(summary_src.get("business_sku") or "UNKNOWN")
    query = (summary_src.get("search_coverage") or {}).get("query") or ""
    query_basis = (summary_src.get("search_coverage") or {}).get(
        "query_generation_basis"
    ) or "offline_reprocess"
    limit = int((summary_src.get("search_coverage") or {}).get("limit") or 5)

    summary = p5b.build_sku_summary(
        business_sku=business_sku,
        product_rows=enriched_rows,
        price_options=price_options,
        search_meta=search_meta or summary_src.get("search_coverage") or {},
        query=query,
        query_basis=query_basis,
        limit=limit,
    )
    source_id = source_job.name
    new_id = f"{utc_stamp()}_{business_sku}_re_{secrets.token_hex(2)}"
    out_dir = JOB_ROOT / new_id
    out_dir.mkdir(parents=True, exist_ok=False)

    # Copy search + candidate verify artifacts by reference paths (do not mutate source).
    # Shallow-copy key JSON from source for audit trail.
    for name in (
        "business_baseline_snapshot.json",
        "price_options.json",
        "price_options.csv",
    ):
        src = source_job / name
        if src.exists():
            shutil.copy2(src, out_dir / name)

    # Link/copy search folder
    search_src = source_job / "search"
    if search_src.is_dir():
        shutil.copytree(search_src, out_dir / "search")

    summary["job_id"] = new_id
    summary["offline_reprocess"] = True
    summary["source_job_id"] = source_id
    summary["source_job_dir"] = str(source_job)
    summary["started_at_utc"] = utc_iso()
    summary["ended_at_utc"] = utc_iso()
    summary["job_dir"] = str(out_dir)
    summary["search_dir"] = str(out_dir / "search")
    summary["notes"] = list(summary.get("notes") or []) + [
        f"离线重处理自 {source_id}；未覆盖源目录",
        "low_price_signal 为新增字段；到手价 system_verdict 未把主展示价改名",
    ]

    # Actual call-record stats cross-check from source rows
    stats_note = {
        "from_source_rows": {
            "candidates_found": summary.get("candidates_found"),
            "candidates_processed": summary.get("candidates_processed"),
            "detail_pages_opened": summary.get("detail_pages_opened"),
            "model_mismatch_skipped": summary.get("model_mismatch_skipped"),
            "completed": summary.get("completed"),
            "failed": summary.get("failed"),
            "paused_unvisited": summary.get("paused_unvisited"),
            "products_possible_break": summary.get("products_possible_break"),
            "products_primary_low_price_signal": summary.get(
                "products_primary_low_price_signal"
            ),
        },
        "interpretation": (
            "detail_pages_opened = processed − model_mismatch_skipped；"
            "型号跳过不计详情访问"
        ),
    }

    write_json(out_dir / "product_rows.json", enriched_rows)
    p5b.write_csv(out_dir / "product_rows.csv", enriched_rows, p5b.PRODUCT_ROW_FIELDS)
    write_json(out_dir / "price_options.json", price_options)
    if (source_job / "price_options.csv").exists():
        shutil.copy2(source_job / "price_options.csv", out_dir / "price_options.csv")
    write_json(out_dir / "sku_summary.json", summary)
    write_json(out_dir / "evidence_diagnosis.json", diagnoses)
    write_json(out_dir / "stats_audit.json", stats_note)

    report = p5b.render_report_html(
        job_id=new_id,
        summary=summary,
        product_rows=enriched_rows,
        price_options=price_options,
    )
    # Append diagnosis section
    diag_rows = []
    for d in diagnoses:
        diag_rows.append(
            "<tr>"
            f"<td>{d.get('search_rank')}</td>"
            f"<td>{d.get('jd_product_id')}</td>"
            f"<td>{d.get('primary_display_price')}</td>"
            f"<td>{d.get('root_cause')}</td>"
            f"<td>{d.get('detail')}</td>"
            "</tr>"
        )
    report = report.replace(
        "</body>",
        "<div class='card'><h2>离线证据诊断</h2>"
        "<p class='muted'>不能从缺失证据推断页面无优惠；截图过紧优先于盲目加词。</p>"
        "<table><tr><th>rank</th><th>id</th><th>主展示价</th><th>根因</th><th>说明</th></tr>"
        + "".join(diag_rows)
        + "</table></div></body>",
    )
    (out_dir / "report.html").write_text(report, encoding="utf-8")

    return {
        "ok": True,
        "source_job": str(source_job),
        "out_dir": str(out_dir),
        "job_id": new_id,
        "summary": summary,
        "diagnoses": diagnoses,
        "product_rows": enriched_rows,
    }


def run_self_checks() -> int:
    failures = take_home.run_local_take_home_checks()
    # Unit-ish: weak vs strong signal without hardcoding production SKU prices into logic
    # (values are fixtures only).
    red = "100.00"
    strong = take_home.derive_product_low_price_signal(
        primary_display_price="90.00",
        red_line=red,
        packaging_match="PASSED",
        box_comparable=True,
    )
    weak = take_home.derive_product_low_price_signal(
        primary_display_price="90.00",
        red_line=red,
        packaging_match="FAILED",
        box_comparable=False,
    )
    if strong.get("signal_strength") != take_home.SIGNAL_STRENGTH_COMPARABLE_PRIMARY:
        failures.append("strong signal strength")
    if weak.get("signal_normalized_price") is not None:
        failures.append("weak must null normalized")
    if strong.get("manual_verdict") is not None:
        failures.append("signal object should not invent manual_verdict")

    # Stats: model skip not counted as detail open
    rows = [
        {
            "jd_product_id": "1",
            "task_status": "COMPLETED",
            "match_status": "PACKAGING_PASSED",
            "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
            "primary_display_price": "90.00",
            "red_line": "100.00",
            "packaging_match": "PASSED",
            "box_comparable": True,
            "low_price_signal": True,
        },
        {
            "jd_product_id": "2",
            "task_status": "SKIPPED",
            "match_status": "MODEL_MISMATCH_SKIPPED",
            "system_verdict": "MODEL_MISMATCH_SKIPPED",
            "primary_display_price": None,
            "red_line": "100.00",
            "packaging_match": None,
            "box_comparable": None,
            "low_price_signal": False,
        },
    ]
    for r in rows:
        take_home.apply_low_price_signal_fields(r)
    summary = p5b.build_sku_summary(
        business_sku="T",
        product_rows=rows,
        price_options=[],
        search_meta={"candidate_count": 2, "run_status": "SUCCESS"},
        query="t",
        query_basis="test",
        limit=2,
    )
    if summary.get("candidates_processed") != 2:
        failures.append("processed should include skip")
    if summary.get("detail_pages_opened") != 1:
        failures.append("detail open excludes model skip")
    if summary.get("products_primary_low_price_signal") != 1:
        failures.append("primary low signal count")
    if summary.get("products_possible_break", 0) != 0:
        failures.append("must not conflate signal with possible_break")

    print("=== P5-C offline self-check ===")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Offline reprocess P5-B sku job")
    p.add_argument("--source-job", type=Path, default=None)
    p.add_argument("--self-check", action="store_true")
    args = p.parse_args(argv)
    if args.self_check:
        return run_self_checks()
    if not args.source_job:
        print("--source-job required", file=sys.stderr)
        return 2
    result = reprocess_job(args.source_job)
    print(f"out_dir={result['out_dir']}")
    print(
        "stats "
        f"processed={result['summary'].get('candidates_processed')} "
        f"detail_opened={result['summary'].get('detail_pages_opened')} "
        f"model_skip={result['summary'].get('model_mismatch_skipped')} "
        f"completed={result['summary'].get('completed')} "
        f"failed={result['summary'].get('failed')} "
        f"possible_break={result['summary'].get('products_possible_break')} "
        f"primary_low_signal={result['summary'].get('products_primary_low_price_signal')}"
    )
    for d in result["diagnoses"]:
        print(
            f"rank{d.get('search_rank')} {d.get('jd_product_id')} "
            f"{d.get('primary_display_price')} → {d.get('root_cause')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
