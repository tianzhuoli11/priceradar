"""P4-D: known product URL → auto capture → OCR → compare (no manual interaction)."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import secrets
import shutil
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
E2E_ROOT = (PROJECT_ROOT / "artifacts" / "end_to_end_runs").resolve()
SCRIPTS_DIR = Path(__file__).resolve().parent
RULES_VERSION = "p4d-selection-core-v1"


def load_sibling(module_stem: str) -> Any:
    path = SCRIPTS_DIR / f"{module_stem}.py"
    spec = importlib.util.spec_from_file_location(f"priceradar_{module_stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


p2d = load_sibling("p2d_jd_auto_capture")
p4c = load_sibling("p4c_process_capture_run")
p4e = load_sibling("p4e_report_history")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat(timespec="seconds")


def utc_run_stamp(dt: datetime | None = None) -> str:
    return (dt or utc_now()).strftime("%Y%m%dT%H%M%SZ")


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def csv_guard(value: Any) -> str:
    text = "" if value is None else str(value)
    if text[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def safe_print(msg: Any) -> None:
    text = str(msg)
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def make_e2e_dir() -> tuple[str, Path]:
    run_id = f"{utc_run_stamp()}_{secrets.token_hex(3)}"
    out = E2E_ROOT / run_id
    out.mkdir(parents=True, exist_ok=False)
    return run_id, out


def product_id_from_url(url: str) -> str | None:
    parts = urlsplit(url)
    path = parts.path or ""
    # /product/100005149674.html
    segs = [s for s in path.split("/") if s]
    if len(segs) >= 2 and segs[0] == "product":
        return segs[1].removesuffix(".html")
    return None


def load_product_config(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    if not path.is_file():
        return None, [f"商品配置不存在: {path}"]
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        return None, [f"商品配置 JSON 无效: {exc}"]
    if not isinstance(raw, dict):
        return None, ["商品配置必须是 JSON 对象"]

    required = [
        "business_sku",
        "product_id",
        "url",
        "expected_model",
        "target_spec",
        "packs_per_box",
        "red_line",
        "currency",
        "comparison_unit",
    ]
    for key in required:
        if key not in raw or raw[key] in (None, ""):
            errors.append(f"缺少必要字段: {key}")

    if errors:
        return None, errors

    product_id = str(raw["product_id"]).strip()
    url = str(raw["url"]).strip()
    url_id = product_id_from_url(url)
    if url_id is None:
        errors.append(f"无法从 URL 解析商品 ID: {url}")
    elif url_id != product_id:
        errors.append(
            f"URL 商品 ID({url_id}) 与配置 product_id({product_id}) 不一致"
        )

    try:
        red = Decimal(str(raw["red_line"]))
        if red <= 0:
            errors.append("red_line 必须为正数金额")
    except (InvalidOperation, TypeError, ValueError):
        errors.append(f"red_line 不是有效金额: {raw.get('red_line')!r}")
        red = None

    try:
        packs = int(raw["packs_per_box"])
        if packs <= 0:
            errors.append("packs_per_box 必须为正整数")
    except (TypeError, ValueError):
        errors.append(f"packs_per_box 无效: {raw.get('packs_per_box')!r}")
        packs = None

    if not str(raw.get("target_spec") or "").strip():
        errors.append("target_spec 缺失")

    if errors:
        return None, errors

    cfg = dict(raw)
    cfg["product_id"] = product_id
    cfg["url"] = url
    cfg["_red_line_decimal"] = red
    cfg["_packs_per_box_int"] = packs
    cfg["_config_path"] = str(path.resolve())
    return cfg, []


def business_override_from_config(cfg: dict[str, Any]) -> dict[str, Any]:
    packaging = cfg.get("packaging_basis")
    return {
        "model": cfg.get("expected_model") or cfg.get("business_sku"),
        "spec_label": f"{cfg.get('target_spec')}／箱"
        if cfg.get("target_spec")
        else None,
        "target_spec": cfg.get("target_spec"),
        "packs_per_box": cfg["_packs_per_box_int"],
        "layers": cfg.get("layers"),
        "sheets_or_pulls": cfg.get("sheets_or_pulls"),
        "red_line_price_cny_per_box": cfg["_red_line_decimal"],
        "packaging_basis": packaging if isinstance(packaging, dict) else None,
        "note": "来自 product-config；不是 OCR 识别结果",
    }


def expected_product_for_capture(cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "jd_product_id": cfg["product_id"],
        "model": cfg.get("expected_model") or cfg.get("business_sku"),
        "spec": f"{cfg.get('target_spec')}／箱"
        if cfg.get("target_spec")
        else cfg.get("target_spec"),
        "shop": cfg.get("expected_shop"),
        "red_line_price_cny_per_box": float(cfg["_red_line_decimal"]),
        "note": "预先登记的业务基准，不是本次自动识别结果",
    }


def write_compare_csv(path: Path, row: dict[str, Any]) -> None:
    fields = [
        "workflow_status",
        "capture_status",
        "ocr_status",
        "comparison_status",
        "verdict",
        "failure_stage",
        "failure_reason",
        "product_id",
        "business_sku",
        "observed_model",
        "price",
        "currency",
        "price_basis",
        "red_line_price",
        "red_line_unit",
        "price_delta",
        "normalized_price_cny_per_box",
        "packaging_match",
        "box_comparable",
        "shop",
        "evidence_quality",
        "capture_source",
        "review_reasons",
        "screenshot_dir",
        "pipeline_dir",
        "processed_at_utc",
        "rules_version",
        "offline_verification",
        "manual_assisted_input",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerow({k: csv_guard(row.get(k)) for k in fields})


def capture_has_usable_images(capture_dir: Path) -> bool:
    meta_path = capture_dir / "metadata.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            meta = {}
        resolved = p4c.resolve_registered_images(capture_dir, meta)
        return any(i.get("exists") for i in resolved.get("selected") or [])
    # Fallback: known filenames only (no arbitrary PNG scan beyond known set)
    return any(
        (capture_dir / name).is_file() for name in p4c.KNOWN_CAPTURE_FILENAMES
    )


def build_failure_row(
    *,
    cfg: dict[str, Any] | None,
    workflow_status: str,
    capture_status: str,
    ocr_status: str,
    comparison_status: str,
    verdict: str,
    failure_stage: str | None,
    failure_reason: str,
    screenshot_dir: str = "",
    pipeline_dir: str = "",
    offline: bool = False,
    processed_at: str,
) -> dict[str, Any]:
    red = ""
    if cfg and cfg.get("_red_line_decimal") is not None:
        red = format(cfg["_red_line_decimal"], "f")
    return {
        "workflow_status": workflow_status,
        "capture_status": capture_status,
        "ocr_status": ocr_status,
        "comparison_status": comparison_status,
        "verdict": verdict,
        "failure_stage": failure_stage or "",
        "failure_reason": failure_reason,
        "product_id": (cfg or {}).get("product_id", ""),
        "business_sku": (cfg or {}).get("business_sku", ""),
        "observed_model": "",
        "price": None,
        "currency": (cfg or {}).get("currency", ""),
        "price_basis": "",
        "red_line_price": red,
        "red_line_unit": (
            f"{(cfg or {}).get('currency', 'CNY')}_per_{(cfg or {}).get('comparison_unit', '箱')}"
        ),
        "price_delta": None,
        "normalized_price_cny_per_box": None,
        "packaging_match": "FAILED",
        "box_comparable": False,
        "shop": "",
        "evidence_quality": "FAILED" if workflow_status == "FAILED" else "INSUFFICIENT",
        "capture_source": "unknown",
        "review_reasons": failure_reason,
        "screenshot_dir": screenshot_dir,
        "pipeline_dir": pipeline_dir,
        "processed_at_utc": processed_at,
        "rules_version": RULES_VERSION,
        "offline_verification": offline,
        "manual_assisted_input": False,
    }


def merge_pipeline_row(
    p4c_row: dict[str, Any],
    *,
    cfg: dict[str, Any],
    workflow_status: str,
    capture_status: str,
    ocr_status: str,
    failure_stage: str | None,
    failure_reason: str,
    screenshot_dir: str,
    pipeline_dir: str,
    offline: bool,
    processed_at: str,
) -> dict[str, Any]:
    verdict = p4c_row.get("verdict") or "NOT_EVALUATED"
    comparison_status = p4c_row.get("comparison_status") or "NOT_EVALUATED"
    # Guard: never rewrite REVIEW_REQUIRED into a break conclusion.
    if verdict == "REVIEW_REQUIRED":
        comparison_status = "NOT_EVALUATED"
    elif verdict in {"BREAK", "NOT_BREAK"}:
        comparison_status = "EVALUATED"
    delta = p4c_row.get("price_delta")
    if verdict not in {"BREAK", "NOT_BREAK"}:
        delta = None
    return {
        "workflow_status": workflow_status,
        "capture_status": capture_status,
        "ocr_status": ocr_status,
        "comparison_status": comparison_status,
        "verdict": verdict,
        "failure_stage": failure_stage or "",
        "failure_reason": failure_reason,
        "product_id": p4c_row.get("product_id") or cfg.get("product_id", ""),
        "business_sku": cfg.get("business_sku", ""),
        "observed_model": p4c_row.get("observed_model") or "",
        "price": p4c_row.get("price"),
        "currency": p4c_row.get("currency") or cfg.get("currency", ""),
        "price_basis": p4c_row.get("price_basis") or "",
        "red_line_price": p4c_row.get("red_line_price")
        or format(cfg["_red_line_decimal"], "f"),
        "red_line_unit": p4c_row.get("red_line_unit")
        or f"{cfg.get('currency', 'CNY')}_per_{cfg.get('comparison_unit', '箱')}",
        "price_delta": delta,
        "normalized_price_cny_per_box": p4c_row.get("normalized_price_cny_per_box"),
        "packaging_match": p4c_row.get("packaging_match") or "",
        "box_comparable": p4c_row.get("box_comparable"),
        "shop": p4c_row.get("shop") or "",
        "evidence_quality": p4c_row.get("evidence_quality") or "",
        "capture_source": p4c_row.get("capture_source") or "automatic",
        "review_reasons": p4c_row.get("review_reasons") or failure_reason,
        "screenshot_dir": screenshot_dir,
        "pipeline_dir": pipeline_dir,
        "processed_at_utc": processed_at,
        "rules_version": RULES_VERSION,
        "offline_verification": offline,
        "manual_assisted_input": False,
    }


def attach_report_and_history(
    out_dir: Path, summary: dict[str, Any]
) -> dict[str, Any]:
    """
    After original e2e artifacts are written: build HTML report + history.
    Failures here must not alter comparison verdict or destroy prior outputs.
    """
    try:
        result = p4e.generate_report_and_history(out_dir)
        stage = {
            "status": "OK" if result.get("ok") else "FAILED",
            "report_path": str(result.get("report_path") or ""),
            "history_dir": str(result.get("history_dir") or ""),
            "history_inserted_new": result.get("history_inserted_new"),
            "history_record_count": result.get("history_record_count"),
            "verdict_unchanged": result.get("verdict_unchanged"),
            "warning": result.get("warning"),
            "ended_at_utc": utc_iso(),
        }
    except Exception as exc:  # noqa: BLE001 — isolate report stage
        stage = {
            "status": "FAILED",
            "error": f"{type(exc).__name__}: {exc}",
            "report_path": "",
            "history_dir": "",
            "ended_at_utc": utc_iso(),
        }
        # Persist report failure into summary without touching verdict.
        try:
            summary_path = out_dir / "run_summary.json"
            if summary_path.is_file():
                data = json.loads(summary_path.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    data["report"] = stage
                    # Keep original comparison fields intact.
                    write_json(summary_path, data)
                    summary.clear()
                    summary.update(data)
        except (OSError, json.JSONDecodeError, TypeError):
            summary["report"] = stage
        return stage

    summary["report"] = stage
    # Reload summary if p4e rewrote it.
    try:
        summary_path = out_dir / "run_summary.json"
        if summary_path.is_file():
            data = json.loads(summary_path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                summary.clear()
                summary.update(data)
    except (OSError, json.JSONDecodeError):
        pass
    return stage


def run_workflow(
    *,
    config_path: Path,
    offline_capture_dir: Path | None = None,
    capture_fn=None,
    process_fn=None,
    generate_report: bool = True,
) -> dict[str, Any]:
    """
    Full known-URL workflow. capture_fn/process_fn injectable for tests.
    Never scans “latest” directories.
    """
    started = utc_now()
    processed_at = utc_iso(started)
    run_id, out_dir = make_e2e_dir()
    stages: dict[str, Any] = {}
    summary: dict[str, Any] = {
        "run_id": run_id,
        "phase": "P4-D",
        "rules_version": RULES_VERSION,
        "started_at_utc": processed_at,
        "manual_assisted_input": False,
        "offline_verification": bool(offline_capture_dir),
        "product_config_path": str(config_path.resolve()),
        "stages": stages,
        "limitations": [
            "已知链接单商品自动流程；不是输入 SKU 自动搜索",
            "不解决截图裁切；不解决搜索受限",
            "不从历史人工截图或其他任务回填结果",
            "历史观察价禁止作为兜底",
        ],
    }

    cfg, cfg_errors = load_product_config(config_path)
    stages["config"] = {
        "started_at_utc": processed_at,
        "ended_at_utc": utc_iso(),
        "status": "FAILED" if cfg_errors else "OK",
        "errors": cfg_errors,
    }
    if cfg_errors or cfg is None:
        row = build_failure_row(
            cfg=None,
            workflow_status="FAILED",
            capture_status="NOT_STARTED",
            ocr_status="SKIPPED",
            comparison_status="NOT_EVALUATED",
            verdict="NOT_EVALUATED",
            failure_stage="config",
            failure_reason="; ".join(cfg_errors),
            offline=bool(offline_capture_dir),
            processed_at=processed_at,
        )
        summary.update(
            {
                "workflow_status": "FAILED",
                "failure_stage": "config",
                "failure_reason": "; ".join(cfg_errors),
                "ended_at_utc": utc_iso(),
                "table_row": row,
                "browser_launched": False,
            }
        )
        return _finalize_e2e(
            run_id=run_id,
            out_dir=out_dir,
            row=row,
            summary=summary,
            exit_code=2,
            generate_report=generate_report,
        )

    # Snapshot config without internal Decimal helpers for JSON.
    cfg_snapshot = {
        k: v
        for k, v in cfg.items()
        if not str(k).startswith("_")
    }
    cfg_snapshot["red_line"] = format(cfg["_red_line_decimal"], "f")
    summary["product_config_snapshot"] = cfg_snapshot

    capture_dir: Path | None = None
    capture_result: dict[str, Any] | None = None
    capture_status = "NOT_STARTED"
    browser_launched = False

    cap_started = utc_iso()
    if offline_capture_dir is not None:
        capture_dir = offline_capture_dir.resolve()
        capture_status = "OFFLINE_REUSED"
        stages["capture"] = {
            "started_at_utc": cap_started,
            "ended_at_utc": utc_iso(),
            "status": capture_status,
            "run_id": capture_dir.name,
            "out_dir": str(capture_dir),
            "note": "离线验证：复用既有截图目录，不冒充新采集",
            "browser_launched": False,
        }
        summary["capture_run_id"] = capture_dir.name
        summary["capture_dir"] = str(capture_dir)
    else:
        try:
            fn = capture_fn or p2d.run_auto_capture
            capture_result = fn(
                product_url=cfg["url"],
                product_id=cfg["product_id"],
                expected_product=expected_product_for_capture(cfg),
                interactive=False,
            )
            browser_launched = bool(capture_result.get("browser_started"))
            capture_dir = Path(capture_result["out_dir"]).resolve()
            capture_status = str(capture_result.get("capture_status") or "FAILED")
            stages["capture"] = {
                "started_at_utc": capture_result.get("started_at_utc") or cap_started,
                "ended_at_utc": capture_result.get("ended_at_utc") or utc_iso(),
                "status": capture_status,
                "run_id": capture_result.get("run_id"),
                "out_dir": str(capture_dir),
                "error": capture_result.get("error"),
                "close_ok": capture_result.get("close_ok"),
                "interrupted": capture_result.get("interrupted"),
                "navigation_status": capture_result.get("navigation_status"),
                "has_any_image": capture_result.get("has_any_image"),
                "browser_launched": browser_launched,
            }
            summary["capture_run_id"] = capture_result.get("run_id")
            summary["capture_dir"] = str(capture_dir)
            if capture_result.get("interrupted"):
                row = build_failure_row(
                    cfg=cfg,
                    workflow_status="INTERRUPTED",
                    capture_status=capture_status,
                    ocr_status="SKIPPED",
                    comparison_status="NOT_EVALUATED",
                    verdict="NOT_EVALUATED",
                    failure_stage="capture",
                    failure_reason="用户中断 (Ctrl+C)",
                    screenshot_dir=str(capture_dir),
                    offline=False,
                    processed_at=processed_at,
                )
                summary.update(
                    {
                        "workflow_status": "INTERRUPTED",
                        "failure_stage": "capture",
                        "failure_reason": "用户中断 (Ctrl+C)",
                        "ended_at_utc": utc_iso(),
                        "table_row": row,
                        "browser_launched": browser_launched,
                    }
                )
                return _finalize_e2e(
                    run_id=run_id,
                    out_dir=out_dir,
                    row=row,
                    summary=summary,
                    exit_code=130,
                    generate_report=generate_report,
                )
        except KeyboardInterrupt:
            row = build_failure_row(
                cfg=cfg,
                workflow_status="INTERRUPTED",
                capture_status=capture_status,
                ocr_status="SKIPPED",
                comparison_status="NOT_EVALUATED",
                verdict="NOT_EVALUATED",
                failure_stage="capture",
                failure_reason="用户中断 (Ctrl+C)",
                screenshot_dir=str(capture_dir) if capture_dir else "",
                offline=False,
                processed_at=processed_at,
            )
            stages["capture"] = {
                "started_at_utc": cap_started,
                "ended_at_utc": utc_iso(),
                "status": "INTERRUPTED",
                "browser_launched": browser_launched,
            }
            summary.update(
                {
                    "workflow_status": "INTERRUPTED",
                    "failure_stage": "capture",
                    "failure_reason": "用户中断 (Ctrl+C)",
                    "ended_at_utc": utc_iso(),
                    "table_row": row,
                    "browser_launched": browser_launched,
                }
            )
            return _finalize_e2e(
                run_id=run_id,
                out_dir=out_dir,
                row=row,
                summary=summary,
                exit_code=130,
                generate_report=generate_report,
            )
        except Exception as exc:  # noqa: BLE001
            stages["capture"] = {
                "started_at_utc": cap_started,
                "ended_at_utc": utc_iso(),
                "status": "FAILED",
                "error": f"{type(exc).__name__}: {exc}",
                "browser_launched": browser_launched,
            }
            row = build_failure_row(
                cfg=cfg,
                workflow_status="FAILED",
                capture_status="FAILED",
                ocr_status="SKIPPED",
                comparison_status="NOT_EVALUATED",
                verdict="NOT_EVALUATED",
                failure_stage="capture",
                failure_reason=f"{type(exc).__name__}: {exc}",
                screenshot_dir=str(capture_dir) if capture_dir else "",
                offline=False,
                processed_at=processed_at,
            )
            summary.update(
                {
                    "workflow_status": "FAILED",
                    "failure_stage": "capture",
                    "failure_reason": f"{type(exc).__name__}: {exc}",
                    "ended_at_utc": utc_iso(),
                    "table_row": row,
                    "browser_launched": browser_launched,
                }
            )
            return _finalize_e2e(
                run_id=run_id,
                out_dir=out_dir,
                row=row,
                summary=summary,
                exit_code=1,
                generate_report=generate_report,
            )

    assert capture_dir is not None

    # Decide whether OCR can proceed.
    usable = capture_has_usable_images(capture_dir)
    if capture_result and capture_result.get("error") and not usable:
        reason = str(capture_result.get("error"))
        row = build_failure_row(
            cfg=cfg,
            workflow_status="FAILED",
            capture_status=capture_status,
            ocr_status="SKIPPED",
            comparison_status="NOT_EVALUATED",
            verdict="NOT_EVALUATED",
            failure_stage="capture",
            failure_reason=reason,
            screenshot_dir=str(capture_dir),
            offline=bool(offline_capture_dir),
            processed_at=processed_at,
        )
        stages["ocr"] = {
            "started_at_utc": utc_iso(),
            "ended_at_utc": utc_iso(),
            "status": "SKIPPED",
            "reason": "无可用商品截图",
        }
        summary.update(
            {
                "workflow_status": "FAILED",
                "failure_stage": "capture",
                "failure_reason": reason,
                "ended_at_utc": utc_iso(),
                "table_row": row,
                "browser_launched": browser_launched,
            }
        )
        return _finalize_e2e(
            run_id=run_id,
            out_dir=out_dir,
            row=row,
            summary=summary,
            exit_code=1,
            generate_report=generate_report,
        )

    if not usable:
        reason = "无可用商品截图，跳过 OCR"
        row = build_failure_row(
            cfg=cfg,
            workflow_status="FAILED",
            capture_status=capture_status,
            ocr_status="SKIPPED",
            comparison_status="NOT_EVALUATED",
            verdict="NOT_EVALUATED",
            failure_stage="capture",
            failure_reason=reason,
            screenshot_dir=str(capture_dir),
            offline=bool(offline_capture_dir),
            processed_at=processed_at,
        )
        stages["ocr"] = {
            "started_at_utc": utc_iso(),
            "ended_at_utc": utc_iso(),
            "status": "SKIPPED",
            "reason": reason,
        }
        summary.update(
            {
                "workflow_status": "FAILED",
                "failure_stage": "capture",
                "failure_reason": reason,
                "ended_at_utc": utc_iso(),
                "table_row": row,
                "browser_launched": browser_launched,
            }
        )
        return _finalize_e2e(
            run_id=run_id,
            out_dir=out_dir,
            row=row,
            summary=summary,
            exit_code=1,
            generate_report=generate_report,
        )

    # Partial capture still proceeds when images exist.
    ocr_started = utc_iso()
    try:
        pfn = process_fn or p4c.process_capture_run
        # Exact directory only — never latest-dir scan.
        pipeline = pfn(
            capture_dir,
            business_override=business_override_from_config(cfg),
        )
    except Exception as exc:  # noqa: BLE001
        stages["ocr"] = {
            "started_at_utc": ocr_started,
            "ended_at_utc": utc_iso(),
            "status": "FAILED",
            "error": f"{type(exc).__name__}: {exc}",
            "input_capture_dir": str(capture_dir),
        }
        row = build_failure_row(
            cfg=cfg,
            workflow_status="FAILED",
            capture_status=capture_status,
            ocr_status="FAILED",
            comparison_status="NOT_EVALUATED",
            verdict="NOT_EVALUATED",
            failure_stage="ocr",
            failure_reason=f"{type(exc).__name__}: {exc}",
            screenshot_dir=str(capture_dir),
            offline=bool(offline_capture_dir),
            processed_at=processed_at,
        )
        summary.update(
            {
                "workflow_status": "FAILED",
                "failure_stage": "ocr",
                "failure_reason": f"{type(exc).__name__}: {exc}",
                "ended_at_utc": utc_iso(),
                "table_row": row,
                "browser_launched": browser_launched,
                "capture_dir": str(capture_dir),
            }
        )
        return _finalize_e2e(
            run_id=run_id,
            out_dir=out_dir,
            row=row,
            summary=summary,
            exit_code=1,
            generate_report=generate_report,
        )

    pipeline_dir = Path(pipeline["out_dir"]).resolve()
    p4c_row = pipeline.get("row") or {}
    p4c_status = (pipeline.get("detail") or {}).get("pipeline_status") or p4c_row.get(
        "pipeline_status"
    )
    ocr_status = str(p4c_status or "UNKNOWN")
    stages["ocr"] = {
        "started_at_utc": ocr_started,
        "ended_at_utc": utc_iso(),
        "status": ocr_status,
        "pipeline_run_id": pipeline.get("run_id"),
        "pipeline_dir": str(pipeline_dir),
        "input_capture_dir": str(capture_dir),
    }
    summary["pipeline_run_id"] = pipeline.get("run_id")
    summary["pipeline_dir"] = str(pipeline_dir)

    verdict = p4c_row.get("verdict") or "NOT_EVALUATED"
    # Hard guard against accidental NOT_BREAK when review required.
    if (
        p4c_row.get("price") in (None, "")
        and verdict == "NOT_BREAK"
    ):
        verdict = "REVIEW_REQUIRED"
        p4c_row = dict(p4c_row)
        p4c_row["verdict"] = "REVIEW_REQUIRED"

    failure_stage = None
    failure_reason = ""
    if capture_status in {"FAILED"} and usable:
        failure_reason = "截图部分失败，已基于现有证据继续分析"
    if ocr_status == "FAILED":
        failure_stage = "ocr"
        failure_reason = p4c_row.get("review_reasons") or "OCR/比价阶段失败"

    workflow_status = "COMPLETED"
    if ocr_status == "FAILED":
        workflow_status = "FAILED"
    elif capture_status == "FAILED" and not usable:
        workflow_status = "FAILED"
    elif capture_status in {"PARTIAL", "FAILED"} and usable:
        workflow_status = "COMPLETED_WITH_CAPTURE_GAPS"

    row = merge_pipeline_row(
        p4c_row,
        cfg=cfg,
        workflow_status=workflow_status,
        capture_status=capture_status,
        ocr_status=ocr_status,
        failure_stage=failure_stage,
        failure_reason=failure_reason,
        screenshot_dir=str(capture_dir),
        pipeline_dir=str(pipeline_dir),
        offline=bool(offline_capture_dir),
        processed_at=processed_at,
    )
    # Ensure verdict field reflects guarded value.
    row["verdict"] = verdict
    if verdict == "REVIEW_REQUIRED":
        row["comparison_status"] = "NOT_EVALUATED"
        if not row.get("price"):
            row["price"] = None

    stages["compare"] = {
        "started_at_utc": ocr_started,
        "ended_at_utc": utc_iso(),
        "status": row["comparison_status"],
        "verdict": verdict,
        "price": row.get("price"),
    }

    evidence_paths = []
    for name in p4c.KNOWN_CAPTURE_FILENAMES:
        p = capture_dir / name
        if p.is_file():
            evidence_paths.append(str(p.resolve()))

    summary.update(
        {
            "workflow_status": workflow_status,
            "capture_status": capture_status,
            "ocr_status": ocr_status,
            "comparison_status": row["comparison_status"],
            "verdict": verdict,
            "failure_stage": failure_stage,
            "failure_reason": failure_reason or None,
            "ended_at_utc": utc_iso(),
            "table_row": row,
            "browser_launched": browser_launched,
            "capture_dir": str(capture_dir),
            "evidence_image_paths": evidence_paths,
            "price_collection_success_claim": False,
            "notes": [
                "流程完成且证据不足时 comparison=REVIEW_REQUIRED，不得称为价格采集成功",
                "截图质量与自动搜索问题仍未解决",
            ],
        }
    )
    exit_code = 0
    if workflow_status == "FAILED":
        exit_code = 1
    return _finalize_e2e(
        run_id=run_id,
        out_dir=out_dir,
        row=row,
        summary=summary,
        exit_code=exit_code,
        generate_report=generate_report,
    )


def _write_e2e(out_dir: Path, row: dict[str, Any], summary: dict[str, Any]) -> None:
    write_json(out_dir / "run_summary.json", summary)
    write_json(out_dir / "compare_table.json", [row])
    write_compare_csv(out_dir / "compare_table.csv", row)
    # Convenience copy of pipeline detail pointer if present.
    pipeline_dir = summary.get("pipeline_dir")
    if pipeline_dir:
        src = Path(pipeline_dir) / "pipeline_detail.json"
        if src.is_file():
            try:
                shutil.copy2(src, out_dir / "pipeline_detail_ref.json")
            except OSError:
                pass


def build_run_result_payload(
    *,
    run_id: str,
    out_dir: Path,
    row: dict[str, Any],
    summary: dict[str, Any],
    exit_code: int,
    report: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Machine-readable pointer for this exact run (WorkBuddy / automation).
    Never points at a guessed "latest" directory; always this out_dir.
    """
    report = report or {}
    return {
        "schema": "priceradar.run_result.v1",
        "run_id": run_id,
        "result_dir": str(out_dir.resolve()),
        "run_summary_path": str((out_dir / "run_summary.json").resolve()),
        "compare_table_csv": str((out_dir / "compare_table.csv").resolve()),
        "compare_table_json": str((out_dir / "compare_table.json").resolve()),
        "report_html": report.get("report_path")
        or ((summary.get("report") or {}).get("report_path")),
        "capture_dir": summary.get("capture_dir") or row.get("screenshot_dir") or "",
        "pipeline_dir": summary.get("pipeline_dir") or row.get("pipeline_dir") or "",
        "workflow_status": row.get("workflow_status") or summary.get("workflow_status"),
        "verdict": row.get("verdict"),
        "comparison_status": row.get("comparison_status"),
        "box_comparable": row.get("box_comparable"),
        "packaging_match": row.get("packaging_match"),
        "observed_model": row.get("observed_model"),
        "shop": row.get("shop"),
        "price": row.get("price"),
        "normalized_price_cny_per_box": row.get("normalized_price_cny_per_box"),
        "red_line_price": row.get("red_line_price"),
        "price_delta": row.get("price_delta"),
        "currency": row.get("currency"),
        "review_reasons": row.get("review_reasons"),
        "processed_at_utc": row.get("processed_at_utc"),
        "rules_version": row.get("rules_version"),
        "exit_code": exit_code,
        "note": (
            "exit_code 0 does not mean a reliable comparable price; "
            "read verdict/box_comparable. Historical amounts are not fallbacks."
        ),
    }


def write_run_result_file(
    out_dir: Path, payload: dict[str, Any], *, extra_path: Path | None = None
) -> Path:
    path = out_dir / "run_result.json"
    write_json(path, payload)
    if extra_path is not None:
        extra_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(extra_path, payload)
    return path.resolve()


def _finalize_e2e(
    *,
    run_id: str,
    out_dir: Path,
    row: dict[str, Any],
    summary: dict[str, Any],
    exit_code: int,
    generate_report: bool,
) -> dict[str, Any]:
    _write_e2e(out_dir, row, summary)
    report_stage: dict[str, Any] | None = None
    if generate_report:
        report_stage = attach_report_and_history(out_dir, summary)
        stages = summary.get("stages")
        if isinstance(stages, dict):
            stages["report"] = report_stage
    return {
        "run_id": run_id,
        "out_dir": out_dir,
        "row": row,
        "summary": summary,
        "exit_code": exit_code,
        "report": report_stage,
    }


def run_self_tests() -> list[str]:
    failures: list[str] = []
    tmp_root = E2E_ROOT.parent / f"_p4d_selftest_{secrets.token_hex(3)}"
    tmp_root.mkdir(parents=True, exist_ok=True)
    try:
        # 1) Config error must not launch browser
        bad_cfg = tmp_root / "bad.json"
        bad_cfg.write_text(
            json.dumps(
                {
                    "business_sku": "B913A",
                    "product_id": "111",
                    "url": "https://item.m.jd.com/product/222.html",
                    "expected_model": "B913A",
                    "target_spec": "单层200抽×20包",
                    "packs_per_box": 20,
                    "red_line": "127.00",
                    "currency": "CNY",
                    "comparison_unit": "箱",
                }
            ),
            encoding="utf-8",
        )
        launched = {"browser": False}

        def capture_should_not_run(**_kwargs):
            launched["browser"] = True
            raise AssertionError("browser must not start on bad config")

        result = run_workflow(
            config_path=bad_cfg,
            capture_fn=capture_should_not_run,
            generate_report=False,
        )
        if result["summary"].get("browser_launched"):
            failures.append("bad config should not launch browser")
        if launched["browser"]:
            failures.append("capture_fn invoked despite bad config")
        if result["exit_code"] == 0:
            failures.append("bad config should non-zero exit")
        if result["row"].get("failure_stage") != "config":
            failures.append("failure_stage should be config")

        # 2) Exact capture dir passed to process_fn
        good_cfg = tmp_root / "good.json"
        good_cfg.write_text(
            (PROJECT_ROOT / "config" / "products" / "b913a.json").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )
        fake_capture = tmp_root / "capture_exact"
        fake_capture.mkdir()
        # minimal metadata + tiny png header so “exists”
        (fake_capture / "metadata.json").write_text(
            json.dumps(
                {
                    "run_id": "fake",
                    "expected_product": {
                        "jd_product_id": "100005149674",
                        "model": "B913A",
                    },
                    "actual_product_url": (
                        "https://item.m.jd.com/product/100005149674.html"
                    ),
                    "images": [
                        {
                            "filename": "price.png",
                            "path": str(fake_capture / "price.png"),
                            "status": "CAPTURED",
                            "capture_method": "automatic",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        # Minimal valid 1x1 PNG
        png_1x1 = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0c"
            b"IDAT\x08\x1dc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00"
            b"\x00\x00IEND\xaeB`\x82"
        )
        (fake_capture / "price.png").write_bytes(png_1x1)

        seen_dirs: list[str] = []

        def fake_capture_fn(**_kwargs):
            return {
                "ok": True,
                "exit_code": 0,
                "run_id": "fake_cap",
                "out_dir": fake_capture,
                "capture_status": "PARTIAL",
                "has_any_image": True,
                "browser_started": True,
                "close_ok": True,
                "navigation_status": "NAVIGATION_COMMAND_DONE",
                "product_identity_ok": True,
                "error": None,
                "interrupted": False,
                "started_at_utc": utc_iso(),
                "ended_at_utc": utc_iso(),
            }

        def fake_process(run_dir: Path, business_override=None):
            seen_dirs.append(str(Path(run_dir).resolve()))
            return {
                "run_id": "fake_pipe",
                "out_dir": tmp_root / "pipe_out",
                "row": {
                    "pipeline_status": "SUCCESS",
                    "verdict": "REVIEW_REQUIRED",
                    "price": None,
                    "comparison_status": "NOT_EVALUATED",
                    "product_id": "100005149674",
                    "observed_model": "",
                    "evidence_quality": "INSUFFICIENT",
                    "capture_source": "automatic",
                    "review_reasons": "证据不足",
                    "red_line_price": "127.00",
                },
                "detail": {"pipeline_status": "SUCCESS"},
            }

        (tmp_root / "pipe_out").mkdir(exist_ok=True)
        result2 = run_workflow(
            config_path=good_cfg,
            capture_fn=fake_capture_fn,
            process_fn=fake_process,
                    generate_report=False,
        )
        if seen_dirs != [str(fake_capture.resolve())]:
            failures.append(
                f"process must receive exact capture dir, got {seen_dirs}"
            )
        if result2["row"].get("verdict") != "REVIEW_REQUIRED":
            failures.append("REVIEW_REQUIRED must be preserved")
        if result2["row"].get("price") not in (None, ""):
            failures.append("REVIEW_REQUIRED must keep empty price")
        if result2["row"].get("verdict") == "NOT_BREAK":
            failures.append("must not rewrite REVIEW to NOT_BREAK")

        # 3) No images → skip OCR
        empty_cap = tmp_root / "empty_cap"
        empty_cap.mkdir()
        (empty_cap / "metadata.json").write_text(
            json.dumps(
                {
                    "run_id": "empty",
                    "regions": {
                        "price": {
                            "filename": "price.png",
                            "path": str(empty_cap / "price.png"),
                            "status": "CAPTURED",
                        }
                    },
                    "expected_product": {"jd_product_id": "100005149674"},
                    "actual_product_url": (
                        "https://item.m.jd.com/product/100005149674.html"
                    ),
                }
            ),
            encoding="utf-8",
        )
        process_called = {"n": 0}

        def capture_empty(**_kwargs):
            return {
                "ok": False,
                "exit_code": 1,
                "run_id": "empty",
                "out_dir": empty_cap,
                "capture_status": "FAILED",
                "has_any_image": False,
                "browser_started": True,
                "close_ok": True,
                "error": "无区域截图成功",
                "interrupted": False,
                "started_at_utc": utc_iso(),
                "ended_at_utc": utc_iso(),
            }

        def process_must_not_run(*_a, **_k):
            process_called["n"] += 1
            raise AssertionError("OCR should be skipped")

        result3 = run_workflow(
            config_path=good_cfg,
            capture_fn=capture_empty,
            process_fn=process_must_not_run,
                    generate_report=False,
        )
        if process_called["n"] != 0:
            failures.append("OCR must be skipped when no images")
        if result3["row"].get("ocr_status") != "SKIPPED":
            failures.append("ocr_status should be SKIPPED")
        if result3["exit_code"] == 0:
            failures.append("no-image failure should non-zero exit")

        # 4) Partial capture continues to OCR
        process_called2 = {"n": 0}

        def capture_partial(**_kwargs):
            return {
                "ok": True,
                "exit_code": 0,
                "run_id": "partial",
                "out_dir": fake_capture,
                "capture_status": "PARTIAL",
                "has_any_image": True,
                "browser_started": True,
                "close_ok": True,
                "error": None,
                "interrupted": False,
                "started_at_utc": utc_iso(),
                "ended_at_utc": utc_iso(),
            }

        def process_count(run_dir, business_override=None):
            process_called2["n"] += 1
            return fake_process(run_dir, business_override=business_override)

        result4 = run_workflow(
            config_path=good_cfg,
            capture_fn=capture_partial,
            process_fn=process_count,
                    generate_report=False,
        )
        if process_called2["n"] != 1:
            failures.append("partial capture should still run OCR")
        if "CAPTURE" not in str(result4["row"].get("workflow_status")) and result4[
            "row"
        ].get("workflow_status") not in {
            "COMPLETED",
            "COMPLETED_WITH_CAPTURE_GAPS",
        }:
            failures.append(
                f"unexpected workflow_status={result4['row'].get('workflow_status')}"
            )

    finally:
        try:
            shutil.rmtree(tmp_root, ignore_errors=True)
        except OSError:
            pass

    return failures


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="PriceRadar P4-D: known URL → capture → OCR → compare"
    )
    p.add_argument(
        "--product-config",
        help="Path to product JSON config (e.g. config/products/b913a.json)",
    )
    p.add_argument(
        "--offline-capture-dir",
        default="",
        help="Offline verification only: exact existing capture dir (no browser)",
    )
    p.add_argument(
        "--emit-result-json",
        default="",
        help=(
            "Optional extra copy of run_result.json (absolute or relative path). "
            "A copy is always written under the e2e result dir as run_result.json."
        ),
    )
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args(argv)
    if args.self_test:
        return args
    if not args.product_config:
        p.error("--product-config is required unless --self-test")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        fails = run_self_tests()
        safe_print(f"self_test_failures={fails}")
        return 0 if not fails else 1

    config_path = Path(args.product_config)
    offline = Path(args.offline_capture_dir) if args.offline_capture_dir else None
    emit_extra = Path(args.emit_result_json) if args.emit_result_json else None

    safe_print("=== PriceRadar P4-D ===")
    safe_print(f"product-config: {config_path.resolve()}")
    if offline:
        safe_print(f"OFFLINE 验证模式 capture-dir: {offline.resolve()}")
        safe_print("说明: 不启动浏览器；不冒充新采集")
    else:
        safe_print("模式: 已知链接全自动（无手动框选/回车/目录传递）")
        safe_print("限制: 不解决裁切；不解决搜索；非 SKU 自动搜索成品")

    try:
        # Default generate_report=True so P4-E HTML/history run after e2e artifacts.
        result = run_workflow(
            config_path=config_path,
            offline_capture_dir=offline,
        )
    except KeyboardInterrupt:
        safe_print("用户中断")
        return 130

    row = result["row"]
    out = Path(result["out_dir"]).resolve()
    verdict = row.get("verdict")
    workflow = row.get("workflow_status")
    exit_code = int(result.get("exit_code") or 0)
    report = result.get("report") or result["summary"].get("report") or {}

    payload = build_run_result_payload(
        run_id=str(result.get("run_id") or out.name),
        out_dir=out,
        row=row,
        summary=result["summary"],
        exit_code=exit_code,
        report=report if isinstance(report, dict) else None,
    )
    result_path = write_run_result_file(out, payload, extra_path=emit_extra)

    safe_print("")
    safe_print("=== 结果摘要 ===")
    if workflow in {"COMPLETED", "COMPLETED_WITH_CAPTURE_GAPS"}:
        if verdict == "REVIEW_REQUIRED":
            safe_print("任务是否完成: 是 — 流程完成，结论待核验")
        else:
            safe_print(f"任务是否完成: 是 — workflow={workflow}")
    elif workflow == "INTERRUPTED":
        safe_print("任务是否完成: 否 — 用户中断")
    else:
        safe_print(f"任务是否完成: 否 — workflow={workflow}")

    if verdict == "REVIEW_REQUIRED":
        safe_print(
            f"比价结论: REVIEW_REQUIRED；原因: {row.get('review_reasons') or row.get('failure_reason')}"
        )
        safe_print("提醒: 不得称为「价格采集成功」")
    else:
        safe_print(
            f"比价结论: {verdict}；comparison_status={row.get('comparison_status')}；"
            f"price={row.get('price')}"
        )

    safe_print(f"结果表: {(out / 'compare_table.csv').resolve()}")
    cap = row.get("screenshot_dir") or result["summary"].get("capture_dir") or ""
    safe_print(f"截图目录: {cap}")
    safe_print(f"run_summary: {(out / 'run_summary.json').resolve()}")
    if report:
        safe_print(
            f"报告/历史状态: {report.get('status')}"
            + (f"；错误={report.get('error')}" if report.get("error") else "")
        )
        if report.get("report_path"):
            safe_print(f"HTML 报告: {report.get('report_path')}")
        if report.get("history_dir"):
            safe_print(f"历史目录: {report.get('history_dir')}")
    else:
        safe_print("报告/历史状态: 未生成")
    # Machine-readable pointer for WorkBuddy / automation (exact run, not "latest").
    safe_print(f"PRICERADAR_RUN_RESULT={result_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
