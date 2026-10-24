"""P4-C: one-shot capture-run → OCR → compare table (no browser / no JD / no AI API)."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import re
import secrets
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PIPELINE_ROOT = (PROJECT_ROOT / "artifacts" / "pipeline_runs").resolve()
SCRIPTS_DIR = Path(__file__).resolve().parent

RULES_VERSION = "p4c-selection-core-v1"
ENGINE_IMPL = "rapidocr+p4a_extract+p4b_select_unit_compare+selection_core"

KNOWN_CAPTURE_FILENAMES = (
    "price.png",
    "product_context.png",
    "product_title.png",
    "selected_spec.png",
    "promotions.png",
    "shop.png",
)

CAPTURED_STATUSES = frozenset(
    {
        "CAPTURED",
        "CAPTURED_PENDING_USER_VERIFY",
        "CAPTURED_MANUAL_ASSISTED",
    }
)

PRODUCT_ID_IN_URL = re.compile(r"/product/(\d+)\.html", re.IGNORECASE)
PRODUCT_ID_IN_DIR = re.compile(r"_(\d{6,})_")


def load_sibling(module_stem: str) -> Any:
    path = SCRIPTS_DIR / f"{module_stem}.py"
    spec = importlib.util.spec_from_file_location(f"priceradar_{module_stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


p4a = load_sibling("p4a_local_ocr")
p4b = load_sibling("p4b_compare_from_ocr")
p2d = load_sibling("p2d_jd_auto_capture")


def normalize_selection_snapshot(snap: dict[str, Any] | None) -> dict[str, Any]:
    """Re-parse core option+qty from legacy noisy selected_spec_text blobs."""
    if not isinstance(snap, dict):
        return {"core_status": "UNKNOWN", "reason": "missing_snapshot"}
    if snap.get("error"):
        out = dict(snap)
        out["core_status"] = "UNKNOWN"
        return out
    if snap.get("core_status") == "EXTRACTED" and snap.get("core_selected_spec"):
        return snap
    raw = (
        snap.get("selected_spec_text")
        or snap.get("selected_spec_text_raw")
        or snap.get("selected_spec_text_stripped")
        or snap.get("raw_selected_blob")
    )
    parsed = p2d.parse_core_selected_spec(str(raw) if raw is not None else None)
    out = dict(snap)
    out.update(parsed)
    out["selected_spec_text"] = parsed.get("core_selected_spec")
    return out


def reevaluate_selection_state(
    selection_state: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """
    Offline-safe: compare core fields only; 保障服务 async text must not flip consistency.
    Does not rewrite capture metadata on disk.
    """
    if not isinstance(selection_state, dict):
        return selection_state
    before = normalize_selection_snapshot(selection_state.get("before"))
    after = normalize_selection_snapshot(selection_state.get("after"))
    cmp = p2d.compare_selection_states(before, after)
    out = dict(selection_state)
    out["before"] = before
    out["after"] = after
    out["consistent"] = cmp.get("consistent")
    out["consistency_status"] = cmp.get("consistency_status")
    out["inconsistency_reasons"] = cmp.get("inconsistency_reasons") or []
    out["compared_fields"] = cmp.get("compared_fields")
    out["reevaluated_with"] = RULES_VERSION
    return out


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat(timespec="seconds")


def utc_run_stamp(dt: datetime | None = None) -> str:
    return (dt or utc_now()).strftime("%Y%m%dT%H%M%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def make_pipeline_dir() -> tuple[str, Path]:
    run_id = f"{utc_run_stamp()}_{secrets.token_hex(3)}"
    out = PIPELINE_ROOT / run_id
    out.mkdir(parents=True, exist_ok=False)
    return run_id, out


def path_inside_run_dir(candidate: Path, run_dir: Path) -> bool:
    try:
        candidate.resolve().relative_to(run_dir.resolve())
        return True
    except ValueError:
        return False


def role_from_filename(filename: str) -> str:
    stem = Path(filename).stem.lower()
    mapping = {
        "price": "price",
        "product_context": "product_context",
        "product_title": "product_title",
        "selected_spec": "selected_spec",
        "promotions": "promotions",
        "shop": "shop",
    }
    return mapping.get(stem, "other")


def capture_method_of(entry: dict[str, Any] | None) -> str:
    if not entry:
        return "unknown"
    method = entry.get("capture_method")
    if method in {"automatic", "manual_assisted"}:
        return str(method)
    if entry.get("manual_assisted") is True:
        return "manual_assisted"
    return "unknown"


def extract_product_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = PRODUCT_ID_IN_URL.search(url)
    return m.group(1) if m else None


def extract_product_id_from_dirname(name: str) -> str | None:
    m = PRODUCT_ID_IN_DIR.search(name)
    return m.group(1) if m else None


def load_metadata(run_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    meta_path = run_dir / "metadata.json"
    if not meta_path.is_file():
        return None, f"缺少 metadata.json: {meta_path}"
    try:
        # utf-8-sig tolerates Windows BOM; content must still be valid JSON.
        return json.loads(meta_path.read_text(encoding="utf-8-sig")), None
    except json.JSONDecodeError as exc:
        return None, f"metadata.json 无法解析: {exc}"


def resolve_registered_images(
    run_dir: Path, metadata: dict[str, Any]
) -> dict[str, Any]:
    """
    Prefer metadata image inventory; legacy fallback = known capture filenames only.
    Does not scan arbitrary PNGs.
    """
    run_dir = run_dir.resolve()
    selected: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []
    mode: str

    images_list = metadata.get("images")
    regions = metadata.get("regions")

    if isinstance(images_list, list) and images_list:
        mode = "metadata_images_list"
        for entry in images_list:
            if not isinstance(entry, dict):
                errors.append("images 列表含非对象项")
                continue
            filename = entry.get("filename")
            if not filename:
                errors.append("images 项缺少 filename")
                continue
            raw_path = entry.get("path")
            if raw_path:
                candidate = Path(str(raw_path))
                if not candidate.is_absolute():
                    candidate = run_dir / candidate
            else:
                candidate = run_dir / str(filename)

            if not path_inside_run_dir(candidate, run_dir):
                errors.append(f"图片路径越界已拒绝: {raw_path or filename}")
                continue

            role = entry.get("role") or role_from_filename(str(filename))
            item = {
                "filename": str(filename),
                "path": candidate.resolve(),
                "role": role,
                "capture_method": capture_method_of(entry),
                "metadata_status": entry.get("status"),
                "metadata_entry": entry,
                "exists": candidate.is_file(),
            }
            if not item["exists"]:
                errors.append(f"元数据登记图片缺失: {filename}")
            selected.append(item)

    elif isinstance(regions, dict) and regions:
        mode = "metadata_regions"
        for region_key, entry in regions.items():
            if not isinstance(entry, dict):
                continue
            status = entry.get("status")
            filename = entry.get("filename")
            if not filename:
                warnings.append(f"regions.{region_key} 无 filename，跳过")
                continue
            if status not in CAPTURED_STATUSES:
                warnings.append(
                    f"regions.{region_key} status={status} 非已截取，跳过"
                )
                continue
            raw_path = entry.get("path")
            if raw_path:
                candidate = Path(str(raw_path))
                if not candidate.is_absolute():
                    candidate = run_dir / candidate
            else:
                candidate = run_dir / str(filename)
            if not path_inside_run_dir(candidate, run_dir):
                errors.append(
                    f"图片路径越界已拒绝: regions.{region_key} -> {raw_path or filename}"
                )
                continue
            item = {
                "filename": str(filename),
                "path": candidate.resolve(),
                "role": entry.get("region") or region_key or role_from_filename(str(filename)),
                "capture_method": capture_method_of(entry),
                "metadata_status": status,
                "metadata_entry": entry,
                "exists": candidate.is_file(),
            }
            if not item["exists"]:
                errors.append(f"元数据登记图片缺失: {filename}")
            selected.append(item)
    else:
        mode = "legacy_known_filenames"
        warnings.append(
            "旧版 metadata 缺少 images/regions 清单；仅选择已知截图文件名"
        )
        for name in KNOWN_CAPTURE_FILENAMES:
            candidate = run_dir / name
            if candidate.is_file():
                selected.append(
                    {
                        "filename": name,
                        "path": candidate.resolve(),
                        "role": role_from_filename(name),
                        "capture_method": "unknown",
                        "metadata_status": None,
                        "metadata_entry": None,
                        "exists": True,
                    }
                )
        if not selected:
            errors.append("兼容模式下未找到任何已知截图文件名")

    # Deduplicate by resolved path (keep first).
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in selected:
        key = str(item["path"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    return {
        "mode": mode,
        "selected": unique,
        "errors": errors,
        "warnings": warnings,
        "selection_list": [
            {
                "filename": i["filename"],
                "role": i["role"],
                "path": str(i["path"]),
                "exists": i["exists"],
                "capture_method": i["capture_method"],
                "metadata_status": i["metadata_status"],
            }
            for i in unique
        ],
    }


def identity_check(
    run_dir: Path, metadata: dict[str, Any]
) -> dict[str, Any]:
    expected = metadata.get("expected_product") or {}
    meta_id = (
        expected.get("jd_product_id")
        or metadata.get("target_product_id")
        or metadata.get("product_id")
    )
    url = metadata.get("actual_product_url")
    url_id = extract_product_id_from_url(url if isinstance(url, str) else None)
    dir_id = extract_product_id_from_dirname(run_dir.name)
    conflicts: list[str] = []
    if meta_id and url_id and str(meta_id) != str(url_id):
        conflicts.append(
            f"元数据商品ID({meta_id})与来源URL商品ID({url_id})不一致"
        )
    if meta_id and dir_id and str(meta_id) != str(dir_id):
        conflicts.append(
            f"元数据商品ID({meta_id})与目录名提示ID({dir_id})不一致（目录名不作唯一身份）"
        )
    return {
        "metadata_product_id": meta_id,
        "url_product_id": url_id,
        "dirname_hint_product_id": dir_id,
        "actual_product_url": url,
        "conflicts": conflicts,
        "product_identity_ok_flag": metadata.get("product_identity_ok"),
    }


def image_quality_failed(ocr_result: dict[str, Any]) -> tuple[bool, list[str]]:
    """
    Reject only bitmaps too small to hold a price. Normal browser clip screenshots
    of #main_price (often ~100–250px) must not be treated as truncated solely
    because a clip region was used.
    """
    risks = list(
        (ocr_result.get("extraction") or {}).get("input_quality_risks") or []
    )
    size = (ocr_result.get("input") or {}).get("pixel_size") or {}
    reasons: list[str] = list(risks)
    if size:
        w, h = int(size.get("width") or 0), int(size.get("height") or 0)
        if h < 36 or w < 48:
            msg = "输入像素过小，难以容纳完整价格数字；即使OCR读出金额也需待核验"
            if msg not in reasons:
                reasons.append(msg)
    # Legacy wording from older OCR runs: do not re-add the broad 120×50 rule.
    failed = bool(reasons)
    return failed, reasons


def ocr_one_image(
    image_item: dict[str, Any],
    ocr_dir: Path,
    *,
    expected_model: str | None = None,
) -> dict[str, Any]:
    img_out = ocr_dir / Path(image_item["filename"]).stem
    img_out.mkdir(parents=True, exist_ok=True)
    result = p4a.process_single_image(
        image_item["path"],
        img_out,
        also_preprocess=True,
        expected_model=expected_model,
    )
    quality_failed, quality_reasons = image_quality_failed(result)
    write_json(
        img_out / "ocr_result.json",
        {
            "image": {
                "filename": image_item["filename"],
                "role": image_item["role"],
                "capture_method": image_item["capture_method"],
                "path": str(image_item["path"]),
                "sha256": result["input"]["sha256"],
            },
            "ocr_ok": result["ocr_ok"],
            "ocr_error": result.get("ocr_error"),
            "quality_failed_for_final_price": quality_failed,
            "quality_reasons": quality_reasons,
            "preprocess": result.get("preprocess"),
            "blocks": result.get("blocks"),
            "extraction": result.get("extraction"),
        },
    )
    return {
        **image_item,
        "ocr": result,
        "quality_failed_for_final_price": quality_failed,
        "quality_reasons": quality_reasons,
        "ocr_subdir": str(img_out),
    }


def pick_field_from_roles(
    processed: list[dict[str, Any]],
    *,
    preferred_roles: list[str],
    extractor,
) -> dict[str, Any]:
    """Pick first non-empty extracted value from preferred roles, then others."""
    ordered = sorted(
        processed,
        key=lambda x: (
            preferred_roles.index(x["role"])
            if x["role"] in preferred_roles
            else len(preferred_roles) + 1
        ),
    )
    for item in ordered:
        ext = (item.get("ocr") or {}).get("extraction") or {}
        value, evidence = extractor(ext, item)
        if value:
            return {
                "value": value,
                "source_image": item["filename"],
                "source_role": item["role"],
                "evidence": evidence,
                "rule": f"prefer_roles={preferred_roles}",
            }
    return {
        "value": None,
        "source_image": None,
        "source_role": None,
        "evidence": None,
        "rule": f"prefer_roles={preferred_roles}",
    }


def aggregate_candidates(
    processed: list[dict[str, Any]], key: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in processed:
        ext = (item.get("ocr") or {}).get("extraction") or {}
        for cand in ext.get(key) or []:
            enriched = dict(cand)
            enriched["_from_filename"] = item["filename"]
            enriched["_from_role"] = item["role"]
            out.append(enriched)
    return out


def select_price_across_images(
    processed: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Per-image P4-B selection on non-quality-failed images.
    Multi-image amount conflict → REVIEW_REQUIRED.
    Quality-failed crops cannot supply final price (recorded as excluded).
    """
    excluded: list[dict[str, Any]] = []
    per_image_selections: list[dict[str, Any]] = []

    for item in processed:
        ext = (item.get("ocr") or {}).get("extraction") or {}
        candidates = list(ext.get("price_candidates") or [])
        if item.get("quality_failed_for_final_price"):
            excluded.append(
                {
                    "filename": item["filename"],
                    "role": item["role"],
                    "reason": "known_crop_or_quality_failure",
                    "quality_reasons": item.get("quality_reasons") or [],
                    "note": "不能仅因数字与其他图不一致就认定质量差；本项因质量/裁切风险排除",
                    "price_candidates_count": len(candidates),
                }
            )
            continue

        sel = p4b.select_primary_selling_price(
            candidates, input_quality_risks=[]
        )
        per_image_selections.append(
            {
                "filename": item["filename"],
                "role": item["role"],
                "selection": sel,
            }
        )

    selected_ok = [
        s
        for s in per_image_selections
        if s["selection"].get("selection_status") == "SELECTED"
        and s["selection"].get("selected_price") is not None
    ]

    # Prefer product_context among selected; still conflict-check amounts.
    amounts = {str(s["selection"]["selected_price"]) for s in selected_ok}

    if len(amounts) > 1:
        return {
            "selection_status": "REVIEW_REQUIRED",
            "selected_price": None,
            "price_basis": None,
            "currency": None,
            "review_reasons": [
                "同次任务多张有效图片主价格冲突：" + ",".join(sorted(amounts))
            ],
            "chosen_from": None,
            "per_image_selections": per_image_selections,
            "excluded_images": excluded,
            "chosen_candidate": None,
        }

    if len(selected_ok) == 1 or (len(amounts) == 1 and selected_ok):
        # Prefer product_context if present among consensus.
        preferred = [
            s for s in selected_ok if s["role"] == "product_context"
        ] or selected_ok
        chosen = preferred[0]
        sel = chosen["selection"]
        cand = sel.get("chosen_candidate") or {}
        currency = cand.get("currency_evidence")
        return {
            "selection_status": "SELECTED",
            "selected_price": sel.get("selected_price"),
            "price_basis": sel.get("price_basis"),
            "currency": currency,
            "review_reasons": list(sel.get("review_reasons") or []),
            "chosen_from": {
                "filename": chosen["filename"],
                "role": chosen["role"],
                "source_text": cand.get("source_text"),
                "box": cand.get("box"),
                "variant": cand.get("variant"),
                "rule": "p4b.select_primary_selling_price",
            },
            "per_image_selections": per_image_selections,
            "excluded_images": excluded,
            "chosen_candidate": cand,
            "eligible_candidates": sel.get("eligible_candidates"),
            "excluded_promo_thresholds": sel.get("excluded_promo_thresholds"),
        }

    # No SELECTED from eligible images.
    reasons = ["无可用的非裁切图片主售价"]
    for s in per_image_selections:
        reasons.extend(s["selection"].get("review_reasons") or [])
    if excluded and not per_image_selections:
        reasons.append("仅有质量失败/裁切图，保留待核验，不强行填金额")
    return {
        "selection_status": "REVIEW_REQUIRED",
        "selected_price": None,
        "price_basis": None,
        "currency": None,
        "review_reasons": list(dict.fromkeys(reasons)),
        "chosen_from": None,
        "per_image_selections": per_image_selections,
        "excluded_images": excluded,
        "chosen_candidate": None,
    }


def capture_time_range(processed: list[dict[str, Any]]) -> dict[str, Any]:
    times: list[str] = []
    for item in processed:
        entry = item.get("metadata_entry") or {}
        t = entry.get("captured_at_utc")
        if t:
            times.append(str(t))
    times_sorted = sorted(times)
    return {
        "earliest_captured_at_utc": times_sorted[0] if times_sorted else None,
        "latest_captured_at_utc": times_sorted[-1] if times_sorted else None,
        "multi_capture_not_absolutely_synchronized": len(set(times_sorted)) > 1,
        "risk_note": (
            "不同时间截图不是绝对同步"
            if len(set(times_sorted)) > 1
            else None
        ),
    }


def map_verdict(
    price_selection: dict[str, Any],
    comparison: dict[str, Any],
    *,
    box_comparable: bool,
) -> str:
    if price_selection.get("selection_status") == "REVIEW_REQUIRED":
        return "REVIEW_REQUIRED"
    status = comparison.get("comparison_status")
    verdict = comparison.get("verdict")
    if verdict == "BREAK_PRICE":
        return "BREAK"
    if verdict == "NOT_BREAK":
        return "NOT_BREAK"
    # Price recognized but packaging/unit not comparable → review, not NOT_BREAK.
    if (
        price_selection.get("selection_status") == "SELECTED"
        and price_selection.get("selected_price") is not None
        and (status == "NOT_EVALUATED" or not box_comparable)
    ):
        return "REVIEW_REQUIRED"
    if status == "NOT_EVALUATED":
        return "NOT_EVALUATED"
    return "NOT_EVALUATED"


def price_delta(
    selected: str | None,
    red: Decimal,
    *,
    box_comparable: bool,
    verdict: str,
) -> str | None:
    """Only when box-comparable and verdict evaluated — never pre-compute on REVIEW."""
    if not box_comparable or verdict not in {"BREAK", "NOT_BREAK"}:
        return None
    if selected is None:
        return None
    amount = p4b.to_decimal(selected)
    if amount is None:
        return None
    return format(amount - red, "f")


def overall_capture_source(processed: list[dict[str, Any]]) -> str:
    methods = {i.get("capture_method") or "unknown" for i in processed}
    if methods == {"automatic"}:
        return "automatic"
    if methods == {"manual_assisted"}:
        return "manual_assisted"
    if "manual_assisted" in methods and "automatic" in methods:
        return "manual_assisted"  # mixed ⇒ not fully automatic
    if methods == {"unknown"}:
        return "unknown"
    if "manual_assisted" in methods:
        return "manual_assisted"
    if "automatic" in methods:
        return "automatic"
    return "unknown"


def build_spec_display(unit_info: dict[str, Any], raw_specs: list[dict[str, Any]]) -> tuple[str, str]:
    raw_texts = []
    for s in raw_specs:
        t = s.get("source_text")
        if t and t not in raw_texts:
            raw_texts.append(t)
    raw = " | ".join(raw_texts) if raw_texts else ""
    parts = []
    ocr = unit_info.get("ocr_recognized") or {}
    if ocr.get("layers"):
        parts.append("层=" + ",".join(ocr["layers"]))
    if ocr.get("sheets_or_pulls"):
        parts.append("抽/张=" + ",".join(ocr["sheets_or_pulls"]))
    if ocr.get("packs"):
        parts.append("包(OCR)=" + ",".join(ocr["packs"]))
    biz = unit_info.get("business_config") or {}
    parts.append(f"业务包／箱={biz.get('packs_per_box')}")
    normalized = "; ".join(parts)
    return raw, normalized


def write_compare_csv(path: Path, row: dict[str, Any]) -> None:
    fields = [
        "pipeline_status",
        "product_id",
        "business_sku",
        "observed_model",
        "spec_raw",
        "spec_normalized",
        "unit_verify_basis",
        "packaging_match",
        "box_comparable",
        "normalized_price_cny_per_box",
        "shop",
        "shop_source",
        "price",
        "currency",
        "price_basis",
        "red_line_price",
        "red_line_unit",
        "price_delta",
        "verdict",
        "comparison_status",
        "evidence_quality",
        "capture_source",
        "review_reasons",
        "screenshot_paths",
        "processed_at_utc",
        "rules_version",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerow({k: csv_guard(row.get(k)) for k in fields})


def failure_row(
    *,
    pipeline_status: str,
    reason: str,
    run_dir: Path,
    processed_at: str,
    product_id: str | None = None,
    capture_source: str = "unknown",
) -> dict[str, Any]:
    return {
        "pipeline_status": pipeline_status,
        "product_id": product_id or "",
        "business_sku": p4b.BUSINESS["model"],
        "observed_model": "",
        "spec_raw": "",
        "spec_normalized": "",
        "unit_verify_basis": "",
        "packaging_match": "FAILED",
        "box_comparable": False,
        "normalized_price_cny_per_box": None,
        "shop": "",
        "shop_source": "",
        "price": None,
        "currency": "",
        "price_basis": "",
        "red_line_price": format(p4b.BUSINESS["red_line_price_cny_per_box"], "f"),
        "red_line_unit": "CNY_per_box",
        "price_delta": None,
        "verdict": "NOT_EVALUATED",
        "comparison_status": "NOT_EVALUATED",
        "evidence_quality": "FAILED",
        "capture_source": capture_source,
        "review_reasons": reason,
        "screenshot_paths": str(run_dir),
        "processed_at_utc": processed_at,
        "rules_version": RULES_VERSION,
    }


def process_capture_run(
    run_dir: Path,
    *,
    business_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    OCR + compare for one exact capture directory.
    business_override: optional packs/model/red_line from product config (not OCR evidence).
    """
    started = utc_now()
    run_dir = run_dir.resolve()
    run_id, out_dir = make_pipeline_dir()
    ocr_dir = out_dir / "ocr"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    processed_at = utc_iso(started)
    limitations = [
        "不启动浏览器、不访问京东、不调用 AI API",
        "不读取其他截图任务或历史 OCR 结果作为兜底",
        "expected_product 仅为业务配置，不是识别证据",
        "P3-A 自动搜索受限未通过；P2-D 自动截图裁切问题仍在",
        "含人工辅助上下文图的任务不能宣称全流程自动截图已通过",
    ]

    biz = dict(p4b.BUSINESS)
    if business_override:
        biz.update(business_override)
    red_line = biz.get("red_line_price_cny_per_box")
    if not isinstance(red_line, Decimal):
        red_line = p4b.to_decimal(red_line) or p4b.BUSINESS["red_line_price_cny_per_box"]
        biz["red_line_price_cny_per_box"] = red_line

    detail: dict[str, Any] = {
        "run_id": run_id,
        "phase": "P4-C",
        "rules_version": RULES_VERSION,
        "engine_impl": ENGINE_IMPL,
        "started_at_utc": processed_at,
        "input_run_dir": str(run_dir),
        "limitations": limitations,
        "errors": [],
        "warnings": [],
        "business_override_applied": bool(business_override),
    }

    if not run_dir.is_dir():
        reason = f"run-dir 不存在或不是目录: {run_dir}"
        row = failure_row(
            pipeline_status="FAILED",
            reason=reason,
            run_dir=run_dir,
            processed_at=processed_at,
        )
        detail["errors"].append(reason)
        detail["pipeline_status"] = "FAILED"
        detail["table_row"] = row
        detail["ended_at_utc"] = utc_iso()
        _write_outputs(out_dir, row, detail)
        return {"run_id": run_id, "out_dir": out_dir, "row": row, "detail": detail}

    metadata, meta_err = load_metadata(run_dir)
    if meta_err or metadata is None:
        reason = meta_err or "metadata 缺失"
        row = failure_row(
            pipeline_status="FAILED",
            reason=reason,
            run_dir=run_dir,
            processed_at=processed_at,
        )
        detail["errors"].append(reason)
        detail["pipeline_status"] = "FAILED"
        detail["table_row"] = row
        detail["ended_at_utc"] = utc_iso()
        _write_outputs(out_dir, row, detail)
        return {"run_id": run_id, "out_dir": out_dir, "row": row, "detail": detail}

    detail["metadata_sha256"] = sha256_file(run_dir / "metadata.json")
    expected_product = metadata.get("expected_product") or {}
    detail["expected_product_business_config_only"] = expected_product
    identity = identity_check(run_dir, metadata)
    detail["identity"] = identity
    if identity["conflicts"]:
        detail["warnings"].extend(identity["conflicts"])

    resolved = resolve_registered_images(run_dir, metadata)
    detail["image_resolution"] = {
        "mode": resolved["mode"],
        "selection_list": resolved["selection_list"],
        "errors": resolved["errors"],
        "warnings": resolved["warnings"],
    }
    detail["warnings"].extend(resolved["warnings"])
    detail["errors"].extend(resolved["errors"])

    existing = [i for i in resolved["selected"] if i["exists"]]
    if not existing:
        reason = "无可用输入图片：" + "; ".join(resolved["errors"] or ["清单为空"])
        row = failure_row(
            pipeline_status="FAILED",
            reason=reason,
            run_dir=run_dir,
            processed_at=processed_at,
            product_id=identity.get("metadata_product_id"),
        )
        detail["pipeline_status"] = "FAILED"
        detail["table_row"] = row
        detail["ended_at_utc"] = utc_iso()
        _write_outputs(out_dir, row, detail)
        return {"run_id": run_id, "out_dir": out_dir, "row": row, "detail": detail}

    # OCR each existing image (fresh; no historical OCR cache).
    # Model boundary match uses business override model when provided — not capture expected_product as evidence fill.
    ocr_expected_model = None
    if business_override and business_override.get("model"):
        ocr_expected_model = str(business_override.get("model"))
    elif isinstance(expected_product, dict) and expected_product.get("model"):
        ocr_expected_model = str(expected_product.get("model"))
    detail["ocr_expected_model"] = ocr_expected_model

    processed: list[dict[str, Any]] = []
    ocr_failures = 0
    for item in existing:
        try:
            processed.append(
                ocr_one_image(item, ocr_dir, expected_model=ocr_expected_model)
            )
        except Exception as exc:  # noqa: BLE001
            ocr_failures += 1
            detail["errors"].append(
                f"OCR失败 {item['filename']}: {type(exc).__name__}: {exc}"
            )

    if not processed:
        reason = "全部图片 OCR 失败"
        row = failure_row(
            pipeline_status="FAILED",
            reason=reason,
            run_dir=run_dir,
            processed_at=processed_at,
            product_id=identity.get("metadata_product_id"),
        )
        detail["pipeline_status"] = "FAILED"
        detail["table_row"] = row
        detail["ended_at_utc"] = utc_iso()
        _write_outputs(out_dir, row, detail)
        return {"run_id": run_id, "out_dir": out_dir, "row": row, "detail": detail}

    input_files = []
    for item in processed:
        input_files.append(
            {
                "filename": item["filename"],
                "role": item["role"],
                "path": str(item["path"]),
                "sha256": item["ocr"]["input"]["sha256"],
                "pixel_size": item["ocr"]["input"].get("pixel_size"),
                "capture_method": item["capture_method"],
                "ocr_ok": item["ocr"]["ocr_ok"],
                "quality_failed_for_final_price": item[
                    "quality_failed_for_final_price"
                ],
                "quality_reasons": item["quality_reasons"],
            }
        )
    detail["input_files"] = input_files
    detail["capture_time_range"] = capture_time_range(processed)

    # Field association within this task only.
    price_selection = select_price_across_images(processed)

    # Specs: prefer selected_spec, also allow product_context / title.
    spec_candidates = []
    for item in processed:
        ext = (item.get("ocr") or {}).get("extraction") or {}
        for s in ext.get("specification_candidates") or []:
            enriched = dict(s)
            enriched["_from_filename"] = item["filename"]
            enriched["_from_role"] = item["role"]
            spec_candidates.append(enriched)

    model_candidates = aggregate_candidates(processed, "model_candidates")
    shop_pick = pick_field_from_roles(
        processed,
        preferred_roles=["shop", "product_context", "product_title"],
        extractor=lambda ext, _item: (
            (ext.get("shop_candidates") or [{}])[0].get("shop_name")
            if ext.get("shop_candidates")
            else None,
            (ext.get("shop_candidates") or [None])[0],
        ),
    )

    # Selection-state: re-evaluate core option+qty (ignore 保障服务/送至 noise).
    # consistent True → ok; False → block; None UNKNOWN → block (never default ok).
    selection_state_raw = (
        metadata.get("selection_state") if isinstance(metadata, dict) else None
    )
    selection_state = reevaluate_selection_state(selection_state_raw)
    if isinstance(selection_state, dict):
        detail["selection_state_reevaluated"] = {
            "consistency_status": selection_state.get("consistency_status"),
            "consistent": selection_state.get("consistent"),
            "compared_fields": selection_state.get("compared_fields"),
            "inconsistency_reasons": selection_state.get("inconsistency_reasons"),
            "reevaluated_with": selection_state.get("reevaluated_with"),
        }
    selection_consistent = None
    if isinstance(selection_state, dict):
        selection_consistent = selection_state.get("consistent")
        if selection_consistent is False:
            detail["warnings"].append(
                "截图前后核心已选规格/商品状态不一致，不得跨状态拼接: "
                + "; ".join(selection_state.get("inconsistency_reasons") or [])
            )
        elif selection_consistent is None and (
            selection_state.get("before") is not None
            or selection_state.get("after") is not None
            or selection_state.get("consistency_status") == "UNKNOWN"
        ):
            detail["warnings"].append(
                "核心已选规格状态未知，不得默认一致: "
                + "; ".join(selection_state.get("inconsistency_reasons") or [])
            )

    unit_info = p4b.analyze_spec_and_unit(
        spec_candidates, model_candidates, business=biz
    )
    if isinstance(selection_state, dict) and selection_consistent is not True:
        # Only enforce when capture recorded a selection_state block.
        if (
            selection_state.get("before") is not None
            or selection_state.get("after") is not None
            or selection_state.get("consistency_status")
        ):
            unit_info = dict(unit_info)
            unit_info["box_comparable"] = False
            unit_info["unit_status"] = "PENDING_VERIFY"
            reasons = list(unit_info.get("unit_review_reasons") or [])
            if selection_consistent is False:
                reasons.append("核心已选规格/商品状态在截图前后变化，证据不可合并")
            else:
                reasons.append("核心已选规格状态未知，证据不可合并")
            unit_info["unit_review_reasons"] = reasons

    comparison = p4b.compare_to_red_line(
        price_selection.get("selected_price"),
        box_comparable=bool(unit_info.get("box_comparable")),
        selection_status=str(price_selection.get("selection_status")),
        red_line=red_line,
    )
    box_ok = bool(unit_info.get("box_comparable"))
    verdict = map_verdict(
        price_selection, comparison, box_comparable=box_ok
    )
    red = red_line

    # Consistency: model conflicts among OCR
    ocr_models = sorted(
        {m.get("model") for m in model_candidates if m.get("model")}
    )
    extra_review: list[str] = []
    if len(ocr_models) > 1:
        extra_review.append(f"OCR型号冲突: {ocr_models}")
    if identity["conflicts"]:
        extra_review.extend(identity["conflicts"])
    if detail["capture_time_range"].get("risk_note"):
        extra_review.append(detail["capture_time_range"]["risk_note"])
    if isinstance(selection_state, dict) and selection_consistent is not True:
        if (
            selection_state.get("before") is not None
            or selection_state.get("after") is not None
            or selection_state.get("consistency_status")
        ):
            extra_review.extend(selection_state.get("inconsistency_reasons") or [])

    # observed model — never fill from expected_product
    observed_model = ",".join(ocr_models) if ocr_models else ""
    spec_raw, spec_norm = build_spec_display(unit_info, spec_candidates)

    unit_basis_parts = [
        f"unit_status={unit_info.get('unit_status')}",
        f"page_spec_matched={unit_info.get('page_spec_matched')}",
        f"packs_match_business={unit_info.get('packs_match_business_config')}",
        f"model_match_business={unit_info.get('model_match_business_config')}",
        f"box_word_in_ocr={unit_info['ocr_recognized'].get('box_word_in_ocr')}",
        f"packaging_basis_applicable={unit_info.get('packaging_basis_applicable')}",
    ]
    if unit_info.get("packaging_basis_note"):
        unit_basis_parts.append("packaging_note=" + str(unit_info["packaging_basis_note"]))
    if unit_info.get("unit_review_reasons"):
        unit_basis_parts.append(
            "reasons=" + ";".join(unit_info["unit_review_reasons"])
        )

    review_reasons = list(
        dict.fromkeys(
            list(price_selection.get("review_reasons") or [])
            + list(unit_info.get("unit_review_reasons") or [])
            + extra_review
            + [e for e in resolved["errors"] if "缺失" in e]
        )
    )
    if (
        verdict == "REVIEW_REQUIRED"
        and price_selection.get("selected_price") is not None
        and not box_ok
    ):
        review_reasons = list(
            dict.fromkeys(
                ["已识别页面金额；商品／包装口径待核验"] + review_reasons
            )
        )

    # Evidence quality
    if price_selection.get("selection_status") == "SELECTED" and box_ok:
        evidence_quality = "ADEQUATE"
    elif price_selection.get("selected_price") is None:
        evidence_quality = "INSUFFICIENT"
    else:
        evidence_quality = "PARTIAL"

    capture_source = overall_capture_source(processed)
    screenshot_paths = ";".join(str(i["path"]) for i in processed)

    # Pipeline status: SUCCESS if we finished with a product conclusion row;
    # PARTIAL if missing registered images or some OCR failures; FAILED only above.
    if resolved["errors"] or ocr_failures or any(
        not r["ocr"]["ocr_ok"] for r in processed
    ):
        pipeline_status = "PARTIAL"
    else:
        pipeline_status = "SUCCESS"

    normalized = (
        price_selection.get("selected_price") if box_ok else None
    )
    delta = price_delta(
        price_selection.get("selected_price"),
        red,
        box_comparable=box_ok,
        verdict=verdict,
    )

    row = {
        "pipeline_status": pipeline_status,
        "product_id": identity.get("metadata_product_id")
        or identity.get("url_product_id")
        or "",
        "business_sku": expected_product.get("model")
        or biz.get("model")
        or p4b.BUSINESS["model"],
        "observed_model": observed_model,
        "spec_raw": spec_raw,
        "spec_normalized": spec_norm,
        "unit_verify_basis": "; ".join(unit_basis_parts),
        "packaging_match": (
            "PASSED" if unit_info.get("page_spec_matched") else "FAILED"
        ),
        "box_comparable": box_ok,
        "normalized_price_cny_per_box": normalized,
        "shop": shop_pick.get("value") or "",
        "shop_source": (
            f"{shop_pick.get('source_image')}|{shop_pick.get('rule')}"
            if shop_pick.get("value")
            else ""
        ),
        "price": price_selection.get("selected_price"),  # null if none
        "currency": price_selection.get("currency") or "",
        "price_basis": price_selection.get("price_basis") or "",
        "red_line_price": format(red, "f"),
        "red_line_unit": "CNY_per_box",
        "price_delta": delta,
        "verdict": verdict,
        "comparison_status": comparison.get("comparison_status"),
        "evidence_quality": evidence_quality,
        "capture_source": capture_source,
        "review_reasons": "; ".join(review_reasons),
        "screenshot_paths": screenshot_paths,
        "processed_at_utc": processed_at,
        "rules_version": RULES_VERSION,
    }

    field_sources = {
        "price": {
            "value": price_selection.get("selected_price"),
            "source": price_selection.get("chosen_from"),
            "rule": "p4b.select_primary_selling_price on non-quality-failed images",
            "excluded_images": price_selection.get("excluded_images"),
        },
        "observed_model": {
            "values": ocr_models,
            "candidates": model_candidates,
            "rule": "OCR MODEL_RE only; expected_product.model not used as evidence",
        },
        "specification": {
            "candidates": spec_candidates,
            "rule": "OCR packs/layers/sheets; business packs_per_box from config",
        },
        "shop": shop_pick,
        "business_packs_per_box": {
            "value": biz["packs_per_box"],
            "source": "business_config",
            "rule": "config/BUSINESS — not from image OCR",
        },
        "red_line_price": {
            "value": format(red, "f"),
            "source": "business_config",
            "rule": "config/BUSINESS — not from image OCR",
        },
    }

    detail.update(
        {
            "pipeline_status": pipeline_status,
            "ended_at_utc": utc_iso(),
            "price_selection": price_selection,
            "unit_analysis": unit_info,
            "comparison": comparison,
            "verdict_mapped": verdict,
            "field_sources": field_sources,
            "table_row": row,
            "engine": {
                "rapidocr": p4a.pkg_version("rapidocr"),
                "onnxruntime": p4a.pkg_version("onnxruntime"),
                "pillow": p4a.pkg_version("Pillow"),
                "impl": ENGINE_IMPL,
                "rules_version": RULES_VERSION,
            },
            "notes": [
                "一任务一条商品结论；候选证据保留在 pipeline_detail / ocr/",
                "流程 SUCCESS/PARTIAL 且 verdict=REVIEW_REQUIRED 表示待核验，非崩溃",
                "缺失金额保持 null，不填 0",
            ],
        }
    )
    _write_outputs(out_dir, row, detail)
    return {"run_id": run_id, "out_dir": out_dir, "row": row, "detail": detail}


def _write_outputs(out_dir: Path, row: dict[str, Any], detail: dict[str, Any]) -> None:
    write_json(out_dir / "pipeline_detail.json", detail)
    write_json(out_dir / "compare_table.json", [row])
    write_compare_csv(out_dir / "compare_table.csv", row)


def run_self_tests() -> list[str]:
    """Lightweight logic tests (no OCR engine / no real captures required)."""
    failures: list[str] = []

    # 0) Selection-core reevaluation (legacy noisy blobs)
    noisy = reevaluate_selection_state(
        {
            "before": {
                "product_id": "100005149674",
                "selected_spec_text": (
                    "已选 1层200抽*20包【超大尺寸】，1个 送至 北京朝阳区 "
                    "现货 活动 充多送多"
                ),
            },
            "after": {
                "product_id": "100005149674",
                "selected_spec_text": (
                    "已选 1层200抽*20包【超大尺寸】，1个 "
                    "本商品支持京东保障服务，点击可选服务 送至 北京朝阳区"
                ),
            },
            "consistent": False,
            "inconsistency_reasons": ["已选规格文本在截图前后变化"],
        }
    )
    if not noisy or noisy.get("consistent") is not True:
        failures.append("保障服务-only noise must reevaluate to consistent")
    changed = reevaluate_selection_state(
        {
            "before": {
                "product_id": "100005149674",
                "selected_spec_text": "已选 1层200抽*20包【超大尺寸】，1个",
            },
            "after": {
                "product_id": "100005149674",
                "selected_spec_text": "已选 1层200抽*10包【超大尺寸】，1个",
            },
        }
    )
    if not changed or changed.get("consistent") is not False:
        failures.append("20包→10包 must reevaluate inconsistent")
    unknown = reevaluate_selection_state(
        {
            "before": {"product_id": "1", "selected_spec_text": "已选"},
            "after": {"product_id": "1", "selected_spec_text": "已选"},
        }
    )
    if not unknown or unknown.get("consistent") is not None:
        failures.append("unparseable core must be UNKNOWN (consistent=None)")

    tmp = PIPELINE_ROOT / f"_selftest_{secrets.token_hex(3)}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        # 1) Path escape rejected
        meta = {
            "expected_product": {"jd_product_id": "1"},
            "images": [
                {
                    "filename": "evil.png",
                    "path": str(PROJECT_ROOT / "artifacts" / "price_captures"),
                }
            ],
        }
        (tmp / "metadata.json").write_text(
            json.dumps(meta), encoding="utf-8"
        )
        resolved = resolve_registered_images(tmp, meta)
        if not any("越界" in e for e in resolved["errors"]):
            failures.append("path escape should be rejected")

        # 2) Missing registered image
        meta2 = {
            "images": [
                {
                    "filename": "price.png",
                    "path": str(tmp / "price.png"),
                    "capture_method": "automatic",
                    "status": "CAPTURED",
                }
            ]
        }
        resolved2 = resolve_registered_images(tmp, meta2)
        if not any("缺失" in e for e in resolved2["errors"]):
            failures.append("missing registered image should error")

        # 3) Crop excluded + context selected
        processed = [
            {
                "filename": "price.png",
                "role": "price",
                "capture_method": "automatic",
                "quality_failed_for_final_price": True,
                "quality_reasons": ["输入像素偏小"],
                "ocr": {
                    "extraction": {
                        "price_candidates": [
                            {
                                "amount": "16.00",
                                "currency_evidence": "¥",
                                "role": "currency_tagged_price_candidate",
                                "variant": "original",
                                "score": 0.9,
                                "box": [[0, 0], [1, 0], [1, 1], [0, 1]],
                                "source_text": "¥16.00",
                            }
                        ]
                    }
                },
            },
            {
                "filename": "product_context.png",
                "role": "product_context",
                "capture_method": "manual_assisted",
                "quality_failed_for_final_price": False,
                "quality_reasons": [],
                "ocr": {
                    "extraction": {
                        "price_candidates": [
                            {
                                "amount": "168.00",
                                "currency_evidence": "¥",
                                "role": "currency_tagged_price_candidate",
                                "variant": "original",
                                "score": 0.97,
                                "box": [[10, 10], [80, 10], [80, 40], [10, 40]],
                                "source_text": "¥168.00",
                            },
                            {
                                "amount": "129",
                                "role": "promotion_threshold_or_gift",
                                "variant": "original",
                                "score": 0.9,
                                "box": [[10, 200], [20, 200], [20, 220], [10, 220]],
                                "source_text": "满129元即赠",
                            },
                        ]
                    }
                },
            },
        ]
        sel = select_price_across_images(processed)
        if sel.get("selected_price") != "168.00":
            failures.append(
                f"context should win after crop exclude, got {sel.get('selected_price')}"
            )
        if not sel.get("excluded_images"):
            failures.append("crop should be listed in excluded_images")

        # 4) Conflicting valid prices
        conflict = select_price_across_images(
            [
                {
                    "filename": "a.png",
                    "role": "product_context",
                    "quality_failed_for_final_price": False,
                    "quality_reasons": [],
                    "ocr": {
                        "extraction": {
                            "price_candidates": [
                                {
                                    "amount": "168.00",
                                    "currency_evidence": "¥",
                                    "role": "currency_tagged_price_candidate",
                                    "variant": "original",
                                    "score": 0.9,
                                    "box": [[0, 0], [1, 0], [1, 1], [0, 1]],
                                    "source_text": "¥168.00",
                                }
                            ]
                        }
                    },
                },
                {
                    "filename": "b.png",
                    "role": "product_title",
                    "quality_failed_for_final_price": False,
                    "quality_reasons": [],
                    "ocr": {
                        "extraction": {
                            "price_candidates": [
                                {
                                    "amount": "99.00",
                                    "currency_evidence": "¥",
                                    "role": "currency_tagged_price_candidate",
                                    "variant": "original",
                                    "score": 0.9,
                                    "box": [[0, 0], [1, 0], [1, 1], [0, 1]],
                                    "source_text": "¥99.00",
                                }
                            ]
                        }
                    },
                },
            ]
        )
        if conflict.get("selected_price") is not None:
            failures.append("conflicting valid prices must not auto-merge")
        if conflict.get("selection_status") != "REVIEW_REQUIRED":
            failures.append("conflicting valid prices should REVIEW_REQUIRED")

        # 5) Only crop → null price
        only_crop = select_price_across_images(
            [
                {
                    "filename": "price.png",
                    "role": "price",
                    "quality_failed_for_final_price": True,
                    "quality_reasons": ["输入像素偏小"],
                    "ocr": {
                        "extraction": {
                            "price_candidates": [
                                {
                                    "amount": "16",
                                    "currency_evidence": "¥",
                                    "role": "currency_tagged_price_candidate",
                                    "variant": "original",
                                    "score": 0.5,
                                    "box": None,
                                    "source_text": "¥16",
                                }
                            ]
                        }
                    },
                }
            ]
        )
        if only_crop.get("selected_price") is not None:
            failures.append("crop-only must keep price null")

    finally:
        # cleanup selftest dir
        try:
            for p in sorted(tmp.rglob("*"), reverse=True):
                if p.is_file():
                    p.unlink()
                elif p.is_dir():
                    p.rmdir()
            tmp.rmdir()
        except OSError:
            pass

    return failures


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="PriceRadar P4-C: capture run → OCR → compare table"
    )
    p.add_argument(
        "--run-dir",
        help="Single price_captures task directory (not artifacts root)",
    )
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args(argv)
    if not args.self_test and not args.run_dir:
        p.error("--run-dir is required unless --self-test")
    if args.self_test and args.run_dir:
        p.error("--self-test cannot be combined with --run-dir")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        fails = run_self_tests()
        safe_print(f"self_test_failures={fails}")
        return 0 if not fails else 1

    run_dir = Path(args.run_dir)
    # Refuse processing entire artifacts tree by accident.
    resolved = run_dir.resolve()
    captures_root = (PROJECT_ROOT / "artifacts" / "price_captures").resolve()
    if resolved == captures_root or resolved == (PROJECT_ROOT / "artifacts").resolve():
        safe_print("拒绝：--run-dir 必须指向单次截图任务目录，不能是 artifacts 根目录")
        return 2

    safe_print("=== PriceRadar P4-C ===")
    safe_print(f"run-dir: {resolved}")
    safe_print("说明: 重新 OCR，不读取历史 ocr_runs / compare_runs 作为兜底")
    result = process_capture_run(resolved)
    row = result["row"]
    safe_print(f"run_id: {result['run_id']}")
    safe_print(f"pipeline_status: {row.get('pipeline_status')}")
    safe_print(f"verdict: {row.get('verdict')} price={row.get('price')}")
    safe_print(f"evidence_quality: {row.get('evidence_quality')}")
    safe_print(f"capture_source: {row.get('capture_source')}")
    safe_print(f"observed_model: {row.get('observed_model')}")
    safe_print(f"shop: {row.get('shop')}")
    safe_print(f"review_reasons: {row.get('review_reasons')}")
    out = result["out_dir"]
    safe_print(f"pipeline_detail.json: {(out / 'pipeline_detail.json').resolve()}")
    safe_print(f"compare_table.csv: {(out / 'compare_table.csv').resolve()}")
    safe_print(f"compare_table.json: {(out / 'compare_table.json').resolve()}")
    safe_print(f"ocr/: {(out / 'ocr').resolve()}")
    if row.get("pipeline_status") == "FAILED":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
