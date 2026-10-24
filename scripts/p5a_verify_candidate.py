"""
P5-A: thin candidate verification — business baseline + one search candidate.

Does not invent candidates. Does not start Phase B batch. Does not claim take-home
OCR is complete when evidence is missing.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import secrets
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
VERIFY_ROOT = (PROJECT_ROOT / "artifacts" / "candidate_verify_runs").resolve()

RULES_VERSION = "p5a-verify-candidate-v1"


def load_sibling(stem: str) -> Any:
    path = SCRIPTS_DIR / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(f"priceradar_{stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


adapt = load_sibling("priceradar_candidate_adapt")
take_home = load_sibling("priceradar_take_home_prices")
p2d = load_sibling("p2d_jd_auto_capture")
p4c = load_sibling("p4c_process_capture_run")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat(timespec="seconds")


def utc_stamp() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%SZ")


def make_out_dir(product_id: str) -> tuple[str, Path]:
    run_id = f"{utc_stamp()}_{product_id}_{secrets.token_hex(3)}"
    out = VERIFY_ROOT / run_id
    out.mkdir(parents=True, exist_ok=False)
    return run_id, out


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def csv_guard(value: Any) -> str:
    text = "" if value is None else str(value)
    if text[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def write_result_csv(path: Path, row: dict[str, Any]) -> None:
    fields = [
        "task_status",
        "match_status",
        "price_evidence_status",
        "system_verdict",
        "system_verdict_zh",
        "manual_verdict",
        "legacy_page_price_verdict",
        "business_sku",
        "candidate_product_id",
        "observed_model",
        "title",
        "shop_observed",
        "source_url",
        "adapted_url",
        "final_url",
        "packaging_match",
        "box_comparable",
        "page_primary_price",
        "take_home_clue_count",
        "has_possible_break",
        "red_line",
        "review_reasons",
        "screenshot_dir",
        "pipeline_dir",
        "verify_dir",
        "rules_version",
        "processed_at_utc",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerow({k: csv_guard(row.get(k)) for k in fields})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="P5-A verify one search candidate")
    p.add_argument(
        "--business-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "products" / "b913a.json",
    )
    p.add_argument(
        "--candidates",
        type=Path,
        default=None,
        help="candidates.json from a search run (required unless --self-check)",
    )
    p.add_argument(
        "--product-id",
        default=None,
        help="JD numeric id from candidates.json (required unless --self-check)",
    )
    p.add_argument(
        "--offline-capture-dir",
        type=Path,
        default=None,
        help="Reuse existing capture dir (no browser)",
    )
    p.add_argument(
        "--self-check",
        action="store_true",
        help="Run offline unit checks only (no browser / no JD)",
    )
    p.add_argument(
        "--skip-title-gate",
        action="store_true",
        help="Do not skip browse on title model mismatch (still records gate)",
    )
    return p.parse_args(argv)


def run_self_checks() -> int:
    failures: list[str] = []
    failures.extend(adapt.run_local_adapt_checks())
    failures.extend(take_home.run_local_take_home_checks())

    # Business baseline must not force historical product_id onto candidate.
    cfg_path = PROJECT_ROOT / "config" / "products" / "b913a.json"
    if cfg_path.is_file():
        cfg, errs = adapt.load_business_baseline(cfg_path)
        if errs or cfg is None:
            failures.append(f"load baseline failed: {errs}")
        else:
            hist = (cfg.get("historical_anchors") or {}).get("historical_product_id")
            exp = adapt.expected_product_for_candidate_capture(
                cfg, candidate_product_id="1143036"
            )
            if exp["jd_product_id"] != "1143036":
                failures.append("candidate id overridden by config")
            if hist and exp["jd_product_id"] == str(hist) and str(hist) != "1143036":
                failures.append("historical id leaked into capture expected")
            if exp.get("shop") is not None:
                failures.append("config shop must not be passed as observed shop")
            ov = adapt.business_override_from_baseline(cfg)
            if ov.get("model") != cfg["expected_model"]:
                failures.append("override model mismatch")
            # Ensure override red line comes from config not other defaults silently
            if ov["red_line_price_cny_per_box"] != cfg["_red_line_decimal"]:
                failures.append("red line not from baseline")

    # Primary cannot be renamed: classify path
    mapped = take_home.classify_ocr_amount_role(
        "¥168", "currency_tagged_price_candidate"
    )
    if mapped != take_home.PRICE_TYPE_PAGE_PRIMARY:
        failures.append("¥168 should stay PAGE_PRIMARY")

    print("=== P5-A offline self-check ===")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: all offline checks passed")
    return 0


def load_pipeline_extractions(pipeline_dir: Path) -> dict[str, Any]:
    price_candidates: list[dict[str, Any]] = []
    model_candidates: list[dict[str, Any]] = []
    specification_candidates: list[dict[str, Any]] = []
    promotion_text: list[dict[str, Any]] = []
    ocr_root = pipeline_dir / "ocr"
    if not ocr_root.is_dir():
        return {
            "price_candidates": price_candidates,
            "model_candidates": model_candidates,
            "specification_candidates": specification_candidates,
            "promotion_text": promotion_text,
        }
    for result_path in sorted(ocr_root.glob("*/ocr_result.json")):
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        ext = payload.get("extraction") or {}
        for p in ext.get("price_candidates") or []:
            price_candidates.append(dict(p))
        for m in ext.get("model_candidates") or []:
            model_candidates.append(dict(m))
        for s in ext.get("specification_candidates") or []:
            specification_candidates.append(dict(s))
        for t in ext.get("promotion_text") or []:
            promotion_text.append(dict(t))
    return {
        "price_candidates": price_candidates,
        "model_candidates": model_candidates,
        "specification_candidates": specification_candidates,
        "promotion_text": promotion_text,
    }


def detect_session_block_from_capture(capture_result: dict[str, Any]) -> str | None:
    """Return pause reason if login/captcha-like; identity mismatch handled separately."""
    status = str(capture_result.get("navigation_status") or "")
    err = str(capture_result.get("error") or "")
    final_u = str(
        capture_result.get("actual_product_url")
        or capture_result.get("final_url")
        or ""
    )
    blob = f"{status} {err} {final_u}".lower()
    if any(x in blob for x in ("passport", "plogin")):
        return "LOGIN_REQUIRED"
    if "login" in blob and "item." not in final_u.lower():
        return "LOGIN_REQUIRED"
    if any(x in err for x in ("验证码", "安全验证", "滑动验证")):
        return "VERIFICATION_REQUIRED"
    return None


def verify_one(
    *,
    business_config: Path,
    candidates_path: Path,
    product_id: str,
    offline_capture_dir: Path | None = None,
    skip_title_gate: bool = False,
) -> dict[str, Any]:
    started = utc_now()
    processed_at = utc_iso(started)
    pid = str(product_id).strip()
    run_id, out_dir = make_out_dir(pid)

    result: dict[str, Any] = {
        "run_id": run_id,
        "phase": "P5-A",
        "rules_version": RULES_VERSION,
        "take_home_rules_version": take_home.RULES_VERSION_TAKE_HOME,
        "adapt_rules_version": adapt.RULES_VERSION_ADAPT,
        "started_at_utc": processed_at,
        "business_config_path": str(business_config.resolve()),
        "candidates_path": str(candidates_path.resolve()),
        "candidate_product_id": pid,
        "task_status": "NOT_STARTED",
        "match_status": None,
        "price_evidence_status": None,
        "system_verdict": take_home.SYSTEM_NOT_EVALUATED,
        "system_verdict_zh": take_home.SYSTEM_VERDICT_ZH[
            take_home.SYSTEM_NOT_EVALUATED
        ],
        "manual_verdict": None,
        "legacy_page_price_verdict": None,
        "price_clues": [],
        "notes": [
            "业务配置与候选身份分离；配置历史链接不得覆盖候选",
            "主展示价不作到手价；无到手价证据时标记到手价待核验",
            "POSSIBLE_BREAK=疑似破价待人工确认；系统不代填 manual_verdict",
            "未进入结算，不生成 VERIFIED_CHECKOUT_PAYABLE",
            "历史 BREAK/NOT_BREAK 记录不改写",
        ],
    }

    cfg, cfg_errors = adapt.load_business_baseline(business_config)
    if cfg_errors or cfg is None:
        result.update(
            {
                "task_status": "FAILED",
                "match_status": "CONFIG_INVALID",
                "price_evidence_status": "NOT_EVALUATED",
                "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                "system_verdict_zh": "配置无效",
                "failure_reason": "; ".join(cfg_errors),
                "ended_at_utc": utc_iso(),
            }
        )
        _finalize(out_dir, result)
        return result

    result["business_baseline"] = {
        k: v for k, v in cfg.items() if not str(k).startswith("_")
    }
    result["historical_anchors_ignored_for_identity"] = cfg.get("historical_anchors")

    cand, cand_err = adapt.load_candidate_from_file(candidates_path, pid)
    if cand_err or cand is None:
        result.update(
            {
                "task_status": "FAILED",
                "match_status": "CANDIDATE_NOT_FOUND",
                "price_evidence_status": "NOT_EVALUATED",
                "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                "system_verdict_zh": "候选不存在",
                "failure_reason": cand_err,
                "ended_at_utc": utc_iso(),
            }
        )
        _finalize(out_dir, result)
        return result

    result["candidate"] = {
        "product_id": cand.get("product_id"),
        "title": cand.get("title"),
        "shop_name_from_search": cand.get("shop_name"),
        "ad_marked": cand.get("ad_marked"),
        "observed_url": cand.get("observed_url") or cand.get("normalized_url"),
        "discovery_source": cand.get("discovery_source"),
        "note": "店铺来自搜索卡，仅作线索；详情店名以本次识别为准",
    }

    gate = adapt.title_model_gate(
        str(cand.get("title") or ""),
        expected_model=cfg["expected_model"],
    )
    result["title_gate"] = gate
    if gate.get("skip_browse") and not skip_title_gate:
        result.update(
            {
                "task_status": "SKIPPED",
                "match_status": gate["status"],
                "price_evidence_status": "SKIPPED",
                "system_verdict": "MODEL_MISMATCH_SKIPPED",
                "system_verdict_zh": "型号不符已跳过（未打开详情）",
                "review_reasons": [gate.get("reason")],
                "ended_at_utc": utc_iso(),
            }
        )
        _finalize(out_dir, result)
        return result

    source_url = str(
        cand.get("normalized_url") or cand.get("observed_url") or ""
    ).strip()
    adapted = adapt.adapt_candidate_url(
        source_url=source_url, candidate_product_id=pid, prefer_mobile=True
    )
    result["url_adapt"] = adapted
    if not adapted.get("ok"):
        result.update(
            {
                "task_status": "FAILED",
                "match_status": "URL_ADAPT_FAILED",
                "price_evidence_status": "NOT_EVALUATED",
                "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                "system_verdict_zh": "URL 适配失败",
                "failure_reason": adapted.get("error"),
                "ended_at_utc": utc_iso(),
            }
        )
        _finalize(out_dir, result)
        return result

    capture_dir: Path | None = None
    capture_result: dict[str, Any] | None = None
    browser_paused = None

    if offline_capture_dir is not None:
        capture_dir = offline_capture_dir.resolve()
        result["capture"] = {
            "mode": "OFFLINE_REUSED",
            "out_dir": str(capture_dir),
            "note": "离线复用截图；不冒充新采集",
        }
        if not capture_dir.is_dir():
            result.update(
                {
                    "task_status": "FAILED",
                    "match_status": "CAPTURE_MISSING",
                    "failure_reason": f"offline capture dir missing: {capture_dir}",
                    "ended_at_utc": utc_iso(),
                }
            )
            _finalize(out_dir, result)
            return result
    else:
        expected_cap = adapt.expected_product_for_candidate_capture(
            cfg, candidate_product_id=pid
        )
        # Guard: never use historical config product id
        hist_id = (cfg.get("historical_anchors") or {}).get("historical_product_id")
        if hist_id and str(hist_id) != pid and expected_cap["jd_product_id"] == str(hist_id):
            result.update(
                {
                    "task_status": "FAILED",
                    "match_status": "IDENTITY_GUARD",
                    "failure_reason": "内部错误：历史商品ID覆盖了候选",
                    "ended_at_utc": utc_iso(),
                }
            )
            _finalize(out_dir, result)
            return result

        print(f"=== P5-A capture candidate {pid} ===")
        print(f"adapted_url={adapted['adapted_url']}")
        print(f"business_model={cfg['expected_model']} (not candidate title alone)")
        try:
            capture_result = p2d.run_auto_capture(
                product_url=adapted["adapted_url"],
                product_id=pid,
                expected_product=expected_cap,
                interactive=False,
                quiet=False,
            )
        except Exception as exc:  # noqa: BLE001
            result.update(
                {
                    "task_status": "FAILED",
                    "match_status": "CAPTURE_EXCEPTION",
                    "failure_reason": f"{type(exc).__name__}: {exc}",
                    "ended_at_utc": utc_iso(),
                }
            )
            _finalize(out_dir, result)
            return result

        capture_dir = Path(capture_result["out_dir"]).resolve()
        result["capture"] = {
            "mode": "LIVE",
            "out_dir": str(capture_dir),
            "run_id": capture_result.get("run_id"),
            "capture_status": capture_result.get("capture_status"),
            "navigation_status": capture_result.get("navigation_status"),
            "error": capture_result.get("error"),
            "final_url": capture_result.get("actual_product_url"),
            "product_identity_ok": capture_result.get("product_identity_ok"),
            "browser_started": capture_result.get("browser_started"),
        }
        # Identity on final URL
        final_u = str(capture_result.get("actual_product_url") or "")
        ident = adapt.final_url_identity_ok(final_u, pid)
        result["final_identity"] = ident
        result["url_adapt"]["final_url_sanitized"] = ident.get("final_url_sanitized")

        browser_paused = detect_session_block_from_capture(capture_result)
        if browser_paused:
            result.update(
                {
                    "task_status": "PAUSED_SESSION",
                    "match_status": browser_paused,
                    "price_evidence_status": "NOT_EVALUATED",
                    "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                    "system_verdict_zh": "会话受限，已停止浏览器验证",
                    "failure_reason": capture_result.get("error") or browser_paused,
                    "ended_at_utc": utc_iso(),
                }
            )
            _finalize(out_dir, result)
            return result
        if not ident.get("ok") or not capture_result.get("product_identity_ok"):
            result.update(
                {
                    "task_status": "FAILED",
                    "match_status": "IDENTITY_FAILED",
                    "price_evidence_status": "NOT_EVALUATED",
                    "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                    "system_verdict_zh": "最终商品ID不符，身份核验未通过",
                    "failure_reason": capture_result.get("error")
                    or "final URL product id mismatch",
                    "ended_at_utc": utc_iso(),
                }
            )
            _finalize(out_dir, result)
            return result

    # OCR + legacy compare via p4c
    assert capture_dir is not None
    biz_ov = adapt.business_override_from_baseline(cfg)
    pipe = p4c.process_capture_run(capture_dir, business_override=biz_ov)
    pipeline_dir = Path(pipe["out_dir"]).resolve()
    row = pipe.get("row") or {}
    detail = pipe.get("detail") or {}
    result["pipeline"] = {
        "out_dir": str(pipeline_dir),
        "run_id": pipe.get("run_id"),
        "pipeline_status": row.get("pipeline_status") or detail.get("pipeline_status"),
    }
    result["legacy_page_price_verdict"] = row.get("verdict")
    result["legacy_rules_version"] = row.get("rules_version")
    result["observed_model"] = row.get("observed_model")
    result["shop_observed"] = row.get("shop")
    result["packaging_match"] = row.get("packaging_match")
    result["box_comparable"] = row.get("box_comparable")
    result["page_primary_price"] = row.get("price")
    result["match_status"] = (
        "PACKAGING_PASSED"
        if row.get("packaging_match") == "PASSED"
        else "PACKAGING_FAILED_OR_UNCLEAR"
    )

    extraction = load_pipeline_extractions(pipeline_dir)
    result["ocr_aggregate_counts"] = {
        "price_candidates": len(extraction.get("price_candidates") or []),
        "model_candidates": len(extraction.get("model_candidates") or []),
        "promotion_text": len(extraction.get("promotion_text") or []),
    }

    red = cfg["_red_line_decimal"]
    box_ok = bool(row.get("box_comparable"))
    page_matched = row.get("packaging_match") == "PASSED"
    clues = take_home.build_price_clues_from_extraction(
        extraction,
        red_line=red,
        box_comparable=box_ok,
        page_spec_matched=page_matched,
        packs_per_box=cfg["_packs_per_box_int"],
        evidence_path=str(pipeline_dir),
    )
    rollup = take_home.aggregate_system_verdict(clues)
    result["price_clues"] = clues
    result["take_home_rollup"] = rollup
    result["system_verdict"] = rollup["system_verdict"]
    result["system_verdict_zh"] = rollup["system_verdict_zh"]
    result["manual_verdict"] = None
    result["price_evidence_status"] = (
        "TAKE_HOME_CLUES_PRESENT"
        if rollup.get("take_home_clue_count", 0) > 0
        else "TAKE_HOME_PENDING_NO_DECLARED_CLUE"
    )
    if rollup.get("take_home_clue_count", 0) == 0:
        result["system_verdict_zh"] = "到手价待核验"
        # Keep system_verdict REVIEW_REQUIRED from rollup
    result["task_status"] = "COMPLETED"
    result["review_reasons"] = list(
        dict.fromkeys(
            [str(row.get("review_reasons") or "")]
            + [r for c in clues for r in (c.get("review_reasons") or [])]
            + [rollup.get("note") or ""]
        )
    )
    result["screenshot_dir"] = str(capture_dir)
    result["pipeline_dir"] = str(pipeline_dir)
    result["verify_dir"] = str(out_dir)
    result["ended_at_utc"] = utc_iso()
    result["limitations"] = [
        "候选适配验收可完成；到手价识别未宣称已通过（除非本轮 OCR 给出声明到手价线索）",
        "不自动领券/加购/结算",
        "价格以本次证据为准，禁用历史168",
    ]
    _finalize(out_dir, result)
    return result


def _finalize(out_dir: Path, result: dict[str, Any]) -> None:
    table_row = {
        "task_status": result.get("task_status"),
        "match_status": result.get("match_status"),
        "price_evidence_status": result.get("price_evidence_status"),
        "system_verdict": result.get("system_verdict"),
        "system_verdict_zh": result.get("system_verdict_zh"),
        "manual_verdict": result.get("manual_verdict"),
        "legacy_page_price_verdict": result.get("legacy_page_price_verdict"),
        "business_sku": (result.get("business_baseline") or {}).get("business_sku"),
        "candidate_product_id": result.get("candidate_product_id"),
        "observed_model": result.get("observed_model"),
        "title": (result.get("candidate") or {}).get("title"),
        "shop_observed": result.get("shop_observed"),
        "source_url": (result.get("url_adapt") or {}).get("source_url"),
        "adapted_url": (result.get("url_adapt") or {}).get("adapted_url"),
        "final_url": (result.get("url_adapt") or {}).get("final_url_sanitized")
        or (result.get("final_identity") or {}).get("final_url_sanitized"),
        "packaging_match": result.get("packaging_match"),
        "box_comparable": result.get("box_comparable"),
        "page_primary_price": result.get("page_primary_price"),
        "take_home_clue_count": (result.get("take_home_rollup") or {}).get(
            "take_home_clue_count"
        ),
        "has_possible_break": (result.get("take_home_rollup") or {}).get(
            "has_possible_break"
        ),
        "red_line": (result.get("business_baseline") or {}).get("red_line"),
        "review_reasons": "; ".join(
            r for r in (result.get("review_reasons") or []) if r
        ),
        "screenshot_dir": result.get("screenshot_dir")
        or ((result.get("capture") or {}).get("out_dir")),
        "pipeline_dir": result.get("pipeline_dir")
        or ((result.get("pipeline") or {}).get("out_dir")),
        "verify_dir": str(out_dir),
        "rules_version": RULES_VERSION,
        "processed_at_utc": result.get("ended_at_utc") or result.get("started_at_utc"),
    }
    write_json(out_dir / "verify_result.json", result)
    write_result_csv(out_dir / "verify_result.csv", table_row)
    write_json(out_dir / "price_clues.json", result.get("price_clues") or [])
    print(f"verify_dir={out_dir}")
    print(
        f"task_status={result.get('task_status')} "
        f"match={result.get('match_status')} "
        f"system_verdict={result.get('system_verdict')} "
        f"zh={result.get('system_verdict_zh')!r}"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_check:
        return run_self_checks()
    if args.candidates is None or not args.product_id:
        print("参数错误: 需要 --candidates 与 --product-id（或使用 --self-check）")
        return 2

    result = verify_one(
        business_config=args.business_config,
        candidates_path=args.candidates,
        product_id=args.product_id,
        offline_capture_dir=args.offline_capture_dir,
        skip_title_gate=args.skip_title_gate,
    )
    status = result.get("task_status")
    if status in {"COMPLETED", "SKIPPED"}:
        return 0
    if status == "PAUSED_SESSION":
        return 3
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
