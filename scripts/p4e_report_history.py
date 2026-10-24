"""P4-E: Chinese HTML report + per-product run history (offline, no browser/OCR)."""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import secrets
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS_ROOT = (PROJECT_ROOT / "artifacts").resolve()
E2E_ROOT = (ARTIFACTS_ROOT / "end_to_end_runs").resolve()
HISTORY_ROOT = (ARTIFACTS_ROOT / "product_history").resolve()
RULES_VERSION = "p4e-packaging-v2"

KNOWN_REGION_LABELS = {
    "price": "价格",
    "product_title": "商品标题",
    "selected_spec": "已选规格",
    "promotions": "优惠",
    "shop": "店铺",
    "product_context": "商品上下文",
}


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def safe_print(msg: Any) -> None:
    text = str(msg)
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def csv_guard(value: Any) -> str:
    text = "" if value is None else str(value)
    if text[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    text = str(ts).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def path_under(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
    )


def verdict_zh(verdict: str | None) -> str:
    mapping = {
        "BREAK": "破价",
        "BREAK_PRICE": "破价",
        "NOT_BREAK": "未破价",
        "REVIEW_REQUIRED": "待核验（非未破价）",
        "NOT_EVALUATED": "未比价",
        "UNKNOWN": "未知",
    }
    key = str(verdict or "")
    return mapping.get(key, f"原始状态: {key or '（空）'}")


def workflow_completed(status: str | None) -> bool:
    return str(status or "") in {
        "COMPLETED",
        "COMPLETED_WITH_CAPTURE_GAPS",
        "SUCCESS",
    }


def has_reliable_price(row: dict[str, Any] | None) -> bool:
    """Comparable box price only — recognized page amount alone is not enough."""
    if not row:
        return False
    if row.get("verdict") == "REVIEW_REQUIRED":
        return False
    if row.get("verdict") not in {"BREAK", "NOT_BREAK", "BREAK_PRICE"}:
        return False
    if row.get("comparison_status") != "EVALUATED":
        return False
    # Require explicit True — missing/False means not box-verified.
    if row.get("box_comparable") is not True:
        return False
    price = row.get("normalized_price_cny_per_box")
    if price is None or price == "":
        # Legacy rows may only have price when already box-comparable.
        price = row.get("price")
    if price is None or price == "":
        return False
    return to_decimal(price) is not None


def relative_uri_from_report(report_dir: Path, target: Path) -> str | None:
    """POSIX-ish relative URL for local file:// opening; None if unsafe."""
    target = target.resolve()
    if not path_under(ARTIFACTS_ROOT, target):
        return None
    try:
        rel = os.path.relpath(str(target), start=str(report_dir.resolve()))
    except ValueError:
        return None
    # Normalize separators; encode each segment for Chinese/special chars.
    parts = Path(rel).parts
    encoded = "/".join(quote(p, safe="") if p not in (".", "..") else p for p in parts)
    return encoded


def split_reasons(text: str | None) -> list[str]:
    if not text:
        return []
    parts = re.split(r"[;；]\s*", str(text))
    return [p.strip() for p in parts if p.strip()]


def load_e2e_bundle(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run-dir 不存在: {run_dir}")
    if not path_under(E2E_ROOT, run_dir) and not path_under(ARTIFACTS_ROOT, run_dir):
        # Allow only under artifacts (end_to_end preferred).
        if not path_under(ARTIFACTS_ROOT, run_dir):
            raise ValueError(f"run-dir 必须位于 artifacts 下: {run_dir}")

    summary_path = run_dir / "run_summary.json"
    table_path = run_dir / "compare_table.json"
    if not summary_path.is_file():
        raise FileNotFoundError(f"缺少 run_summary.json: {summary_path}")
    if not table_path.is_file():
        raise FileNotFoundError(f"缺少 compare_table.json: {table_path}")

    try:
        summary = load_json(summary_path)
    except json.JSONDecodeError as exc:
        raise ValueError(f"run_summary.json 无法解析: {exc}") from exc
    try:
        table = load_json(table_path)
    except json.JSONDecodeError as exc:
        raise ValueError(f"compare_table.json 无法解析: {exc}") from exc

    if not isinstance(summary, dict):
        raise ValueError("run_summary.json 必须是对象")
    if not isinstance(table, list) or not table:
        raise ValueError("compare_table.json 必须是非空数组")
    row = table[0]
    if not isinstance(row, dict):
        raise ValueError("compare_table.json[0] 必须是对象")

    pipeline: dict[str, Any] | None = None
    for candidate in (
        run_dir / "pipeline_detail_ref.json",
        Path(str(summary.get("pipeline_dir") or "")) / "pipeline_detail.json",
    ):
        if candidate.is_file() and path_under(ARTIFACTS_ROOT, candidate):
            try:
                loaded = load_json(candidate)
                if isinstance(loaded, dict):
                    pipeline = loaded
                    break
            except json.JSONDecodeError:
                continue

    capture_meta: dict[str, Any] | None = None
    capture_dir_raw = summary.get("capture_dir") or row.get("screenshot_dir")
    capture_dir = Path(str(capture_dir_raw)) if capture_dir_raw else None
    if capture_dir and capture_dir.is_dir() and path_under(ARTIFACTS_ROOT, capture_dir):
        meta_path = capture_dir / "metadata.json"
        if meta_path.is_file():
            try:
                capture_meta = load_json(meta_path)
            except json.JSONDecodeError:
                capture_meta = None

    return {
        "run_dir": run_dir,
        "summary": summary,
        "row": row,
        "pipeline": pipeline,
        "capture_meta": capture_meta,
        "capture_dir": capture_dir.resolve() if capture_dir and capture_dir.is_dir() else None,
    }


def observed_fields(bundle: dict[str, Any]) -> dict[str, Any]:
    row = bundle["row"]
    pipeline = bundle.get("pipeline") or {}
    table_row = (pipeline.get("table_row") or {}) if isinstance(pipeline, dict) else {}
    return {
        "model": row.get("observed_model") or table_row.get("observed_model") or "",
        "spec_raw": table_row.get("spec_raw") or "",
        "spec_normalized": table_row.get("spec_normalized") or "",
        "shop": row.get("shop") or table_row.get("shop") or "",
        "price": row.get("price"),
        "currency": row.get("currency") or "",
        "price_basis": row.get("price_basis") or table_row.get("price_basis") or "",
        "red_line_unit": row.get("red_line_unit") or "",
    }


def expected_fields(bundle: dict[str, Any]) -> dict[str, Any]:
    summary = bundle["summary"]
    snap = summary.get("product_config_snapshot") or {}
    row = bundle["row"]
    return {
        "product_id": row.get("product_id") or snap.get("product_id") or "",
        "business_sku": row.get("business_sku") or snap.get("business_sku") or "",
        "expected_model": snap.get("expected_model") or snap.get("business_sku") or "",
        "target_spec": snap.get("target_spec") or "",
        "expected_shop": snap.get("expected_shop") or "",
        "red_line": row.get("red_line_price") or snap.get("red_line") or "",
        "currency": snap.get("currency") or row.get("currency") or "",
        "comparison_unit": snap.get("comparison_unit") or "",
        "packs_per_box": snap.get("packs_per_box"),
    }


def collect_evidence_slots(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """List evidence regions from this run only; never backfill old images."""
    summary = bundle["summary"]
    capture_dir = bundle.get("capture_dir")
    capture_meta = bundle.get("capture_meta") or {}
    slots: list[dict[str, Any]] = []

    regions = capture_meta.get("regions") if isinstance(capture_meta, dict) else None
    images = capture_meta.get("images") if isinstance(capture_meta, dict) else None

    if isinstance(regions, dict) and regions:
        for key, entry in regions.items():
            if not isinstance(entry, dict):
                continue
            filename = entry.get("filename") or f"{key}.png"
            status = str(entry.get("status") or "")
            path = None
            if capture_dir:
                candidate = capture_dir / str(filename)
                if candidate.is_file() and path_under(ARTIFACTS_ROOT, candidate):
                    path = candidate
            missing_reason = None
            if path is None:
                missing_reason = (
                    entry.get("error")
                    or (
                        f"状态={status}，本次无截图文件"
                        if status
                        else "本次无截图文件"
                    )
                )
            slots.append(
                {
                    "key": key,
                    "label": KNOWN_REGION_LABELS.get(key, key),
                    "filename": filename,
                    "status": status,
                    "path": path,
                    "missing_reason": missing_reason,
                    "captured_at_utc": entry.get("captured_at_utc"),
                }
            )
    elif isinstance(images, list) and images:
        for entry in images:
            if not isinstance(entry, dict):
                continue
            filename = entry.get("filename") or "unknown.png"
            key = Path(str(filename)).stem
            status = str(entry.get("status") or "")
            path = None
            if capture_dir:
                candidate = capture_dir / str(filename)
                if candidate.is_file() and path_under(ARTIFACTS_ROOT, candidate):
                    path = candidate
            slots.append(
                {
                    "key": key,
                    "label": KNOWN_REGION_LABELS.get(key, key),
                    "filename": filename,
                    "status": status,
                    "path": path,
                    "missing_reason": None if path else (entry.get("error") or "本次无截图文件"),
                    "captured_at_utc": entry.get("captured_at_utc"),
                }
            )
    else:
        # Fall back to summary evidence list only (still this-run paths).
        for p in summary.get("evidence_image_paths") or []:
            candidate = Path(str(p))
            if not candidate.is_file() or not path_under(ARTIFACTS_ROOT, candidate):
                continue
            key = candidate.stem
            slots.append(
                {
                    "key": key,
                    "label": KNOWN_REGION_LABELS.get(key, key),
                    "filename": candidate.name,
                    "status": "PRESENT",
                    "path": candidate.resolve(),
                    "missing_reason": None,
                    "captured_at_utc": None,
                }
            )

    # Also note title AMBIGUOUS from discovery even if no region file.
    discovery = (capture_meta or {}).get("discovery") or {}
    title = discovery.get("product_title") if isinstance(discovery, dict) else None
    if isinstance(title, dict) and title.get("status") == "AMBIGUOUS":
        if not any(s["key"] == "product_title" for s in slots):
            slots.append(
                {
                    "key": "product_title",
                    "label": "商品标题",
                    "filename": "product_title.png",
                    "status": "AMBIGUOUS",
                    "path": None,
                    "missing_reason": title.get("reason")
                    or "标题定位歧义，未截取",
                    "captured_at_utc": None,
                }
            )
    return slots


def history_record_from_bundle(
    bundle: dict[str, Any], *, report_path: Path | None
) -> dict[str, Any]:
    summary = bundle["summary"]
    row = bundle["row"]
    expected = expected_fields(bundle)
    observed = observed_fields(bundle)
    capture_times = []
    for slot in collect_evidence_slots(bundle):
        t = slot.get("captured_at_utc")
        if t:
            capture_times.append(str(t))
    capture_times.sort()
    reliable = has_reliable_price(row)
    return {
        "run_id": summary.get("run_id") or bundle["run_dir"].name,
        "product_id": expected["product_id"],
        "business_sku": expected["business_sku"],
        "run_started_at_utc": summary.get("started_at_utc"),
        "run_ended_at_utc": summary.get("ended_at_utc"),
        "earliest_capture_at_utc": capture_times[0] if capture_times else None,
        "latest_capture_at_utc": capture_times[-1] if capture_times else None,
        "workflow_status": summary.get("workflow_status") or row.get("workflow_status"),
        "capture_status": summary.get("capture_status") or row.get("capture_status"),
        "ocr_status": summary.get("ocr_status") or row.get("ocr_status"),
        "verdict": row.get("verdict"),
        "comparison_status": row.get("comparison_status"),
        "reliable_price": format(to_decimal(row.get("price")), "f")
        if reliable
        else None,
        "price_raw": row.get("price"),
        "currency": row.get("currency") or expected["currency"],
        "unit": expected["comparison_unit"] or row.get("red_line_unit"),
        "price_basis": observed.get("price_basis") or "",
        "red_line_price": row.get("red_line_price") or expected["red_line"],
        "observed_model": observed.get("model") or "",
        "observed_spec": observed.get("spec_normalized")
        or observed.get("spec_raw")
        or "",
        "expected_model": expected["expected_model"],
        "expected_spec": expected["target_spec"],
        "review_reasons": row.get("review_reasons") or "",
        "result_dir": str(bundle["run_dir"]),
        "report_path": str(report_path) if report_path else "",
        "business_baseline_snapshot": {
            "expected_model": expected["expected_model"],
            "target_spec": expected["target_spec"],
            "expected_shop": expected["expected_shop"],
            "red_line": expected["red_line"],
            "currency": expected["currency"],
            "comparison_unit": expected["comparison_unit"],
            "packs_per_box": expected["packs_per_box"],
        },
        "recorded_at_utc": utc_iso(),
        "rules_version": RULES_VERSION,
    }


def load_history(history_dir: Path) -> dict[str, Any]:
    path = history_dir / "history.json"
    if not path.exists():
        return {
            "product_id": history_dir.name,
            "updated_at_utc": None,
            "records": [],
        }
    try:
        data = load_json(path)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"历史文件损坏，拒绝静默覆盖: {path} ({exc})"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise ValueError(f"历史文件结构无效: {path}")
    return data


def sort_history_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(rec: dict[str, Any]) -> datetime:
        for field in (
            "run_ended_at_utc",
            "run_started_at_utc",
            "latest_capture_at_utc",
            "recorded_at_utc",
        ):
            dt = parse_iso(rec.get(field))
            if dt:
                return dt
        return datetime.min.replace(tzinfo=timezone.utc)

    return sorted(records, key=key)


def upsert_history(
    history_dir: Path, record: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Insert or replace by run_id. Returns (history, inserted_new)."""
    history_dir.mkdir(parents=True, exist_ok=True)
    data = load_history(history_dir)
    pid = str(record.get("product_id") or history_dir.name)
    if data.get("product_id") and str(data["product_id"]) != pid:
        raise ValueError(
            f"历史目录商品 ID({data.get('product_id')}) 与记录({pid}) 不一致"
        )
    data["product_id"] = pid
    records = [r for r in data["records"] if isinstance(r, dict)]
    existing_idx = next(
        (i for i, r in enumerate(records) if r.get("run_id") == record["run_id"]),
        None,
    )
    inserted_new = existing_idx is None
    if existing_idx is None:
        records.append(record)
    else:
        # Refresh paths/reasons but keep original business baseline if present.
        old = records[existing_idx]
        merged = dict(old)
        merged.update(record)
        if old.get("business_baseline_snapshot") and not record.get(
            "business_baseline_snapshot"
        ):
            merged["business_baseline_snapshot"] = old["business_baseline_snapshot"]
        # Do not rewrite past red_line with a different value silently —
        # keep the baseline snapshot from first insert.
        if old.get("business_baseline_snapshot"):
            merged["business_baseline_snapshot"] = old["business_baseline_snapshot"]
            merged["red_line_price"] = (
                old.get("red_line_price")
                or (old.get("business_baseline_snapshot") or {}).get("red_line")
                or merged.get("red_line_price")
            )
        records[existing_idx] = merged

    records = sort_history_records(records)
    data["records"] = records
    data["updated_at_utc"] = utc_iso()
    data["rules_version"] = RULES_VERSION

    atomic_write_json(history_dir / "history.json", data)
    write_history_csv(history_dir / "history.csv", records)
    return data, inserted_new


def write_history_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fields = [
        "run_id",
        "product_id",
        "run_started_at_utc",
        "run_ended_at_utc",
        "earliest_capture_at_utc",
        "latest_capture_at_utc",
        "workflow_status",
        "verdict",
        "reliable_price",
        "currency",
        "unit",
        "red_line_price",
        "review_reasons",
        "result_dir",
        "report_path",
    ]
    # Atomic CSV write
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for rec in records:
                writer.writerow({k: csv_guard(rec.get(k)) for k in fields})
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def comparable_pair(
    current: dict[str, Any], previous: dict[str, Any]
) -> tuple[bool, str]:
    if not current.get("reliable_price") or not previous.get("reliable_price"):
        return False, "缺少可靠价格"
    if str(current.get("product_id")) != str(previous.get("product_id")):
        return False, "商品 ID 不一致"
    cur_model = (current.get("observed_model") or current.get("expected_model") or "").strip()
    prev_model = (
        previous.get("observed_model") or previous.get("expected_model") or ""
    ).strip()
    # Prefer observed; if both empty, not comparable
    if not cur_model or not prev_model:
        return False, "型号证据不足，无法确认可比"
    if cur_model != prev_model:
        return False, f"型号不一致（{cur_model} vs {prev_model}）"
    if str(current.get("currency") or "") != str(previous.get("currency") or ""):
        return False, "币种不一致"
    if str(current.get("unit") or "") != str(previous.get("unit") or ""):
        return False, "单位不一致"
    # Spec: require both non-empty and equal when present
    cur_spec = (current.get("observed_spec") or "").strip()
    prev_spec = (previous.get("observed_spec") or "").strip()
    if cur_spec and prev_spec and cur_spec != prev_spec:
        return False, "规格不一致"
    if not cur_spec or not prev_spec:
        return False, "规格证据不足，无法确认可比"
    # Selling basis rough check
    cur_basis = (current.get("price_basis") or "").strip()
    prev_basis = (previous.get("price_basis") or "").strip()
    if cur_basis and prev_basis and cur_basis != prev_basis:
        return False, "售价口径不一致"
    return True, "可比"


def price_change_info(
    current_rec: dict[str, Any], history_records: list[dict[str, Any]]
) -> dict[str, Any]:
    """Only when current AND a prior record both have reliable comparable evidence."""
    if not current_rec.get("reliable_price"):
        return {
            "computed": False,
            "reason": "本次无可靠价格，不计算涨跌，不用历史价代替当前价",
            "previous": None,
            "delta": None,
        }
    priors = [
        r
        for r in history_records
        if r.get("run_id") != current_rec.get("run_id") and r.get("reliable_price")
    ]
    priors = sort_history_records(priors)
    if not priors:
        return {
            "computed": False,
            "reason": "暂无可比较的历史记录",
            "previous": None,
            "delta": None,
        }
    # Try latest prior that is comparable
    for prev in reversed(priors):
        ok, reason = comparable_pair(current_rec, prev)
        if ok:
            cur_p = to_decimal(current_rec["reliable_price"])
            prev_p = to_decimal(prev["reliable_price"])
            assert cur_p is not None and prev_p is not None
            delta = cur_p - prev_p
            return {
                "computed": True,
                "reason": reason,
                "previous": prev,
                "delta": format(delta, "f"),
                "direction": (
                    "上涨" if delta > 0 else ("下降" if delta < 0 else "持平")
                ),
            }
    return {
        "computed": False,
        "reason": "历史可靠价格存在，但型号/规格/币种/单位不可比",
        "previous": priors[-1],
        "delta": None,
    }


def last_reliable_record(
    history_records: list[dict[str, Any]], *, exclude_run_id: str | None
) -> dict[str, Any] | None:
    candidates = [
        r
        for r in sort_history_records(history_records)
        if r.get("reliable_price")
        and (exclude_run_id is None or r.get("run_id") != exclude_run_id)
    ]
    # Also allow current if it is reliable and we want "last including current"
    including = [
        r for r in sort_history_records(history_records) if r.get("reliable_price")
    ]
    return including[-1] if including else None


def render_html(
    bundle: dict[str, Any],
    *,
    history: dict[str, Any],
    current_rec: dict[str, Any],
    change: dict[str, Any],
) -> str:
    summary = bundle["summary"]
    row = bundle["row"]
    expected = expected_fields(bundle)
    observed = observed_fields(bundle)
    report_dir = bundle["run_dir"]
    slots = collect_evidence_slots(bundle)
    reasons = split_reasons(row.get("review_reasons") or row.get("failure_reason"))
    wf = summary.get("workflow_status") or row.get("workflow_status")
    completed = workflow_completed(wf)
    reliable = has_reliable_price(row)
    price_display = (
        esc(row.get("price"))
        if reliable
        else "未获得可靠价格"
    )
    verdict = row.get("verdict")
    last_rel = last_reliable_record(
        history.get("records") or [], exclude_run_id=None
    )

    evidence_html_parts = []
    for slot in slots:
        label = esc(slot["label"])
        status = esc(slot.get("status") or "")
        if slot.get("path"):
            uri = relative_uri_from_report(report_dir, slot["path"])
            if uri is None:
                evidence_html_parts.append(
                    f"<li><strong>{label}</strong>（{status}）：路径越界已拒绝引用</li>"
                )
            else:
                    fname = esc(slot.get("filename"))
                    evidence_html_parts.append(
                        "<li>"
                        f"<strong>{label}</strong>（{status}）"
                        f'<div class="thumb"><a href="{esc(uri)}" target="_blank" rel="noopener">'
                        f'<img src="{esc(uri)}" alt="{label}" loading="lazy" />'
                        f"</a></div>"
                        f'<div class="file">{fname}</div>'
                        "</li>"
                    )
        else:
            evidence_html_parts.append(
                "<li>"
                f"<strong>{label}</strong>（{status}）"
                f'<div class="missing">缺失：{esc(slot.get("missing_reason") or "未知原因")}</div>'
                "</li>"
            )
    if not evidence_html_parts:
        evidence_html_parts.append("<li>本次无关联截图证据</li>")

    reason_items = "".join(f"<li>{esc(r)}</li>" for r in reasons) or "<li>无额外待核验条目</li>"

    hist_rows = []
    for rec in reversed(sort_history_records(history.get("records") or [])):
        hist_rows.append(
            "<tr>"
            f"<td>{esc(rec.get('run_id'))}</td>"
            f"<td>{esc(rec.get('run_ended_at_utc') or rec.get('run_started_at_utc'))}</td>"
            f"<td>{esc(rec.get('workflow_status'))}</td>"
            f"<td>{esc(verdict_zh(rec.get('verdict')))}</td>"
            f"<td>{esc(rec.get('reliable_price') if rec.get('reliable_price') is not None else '（无可靠价格）')}</td>"
            f"<td>{esc(rec.get('red_line_price'))}</td>"
            f"<td>{esc(rec.get('review_reasons'))}</td>"
            "</tr>"
        )
    hist_table = (
        "".join(hist_rows)
        if hist_rows
        else '<tr><td colspan="7">暂无历史记录</td></tr>'
    )

    change_block = ""
    if not reliable:
        change_block = (
            "<p><strong>本次无可靠价格</strong>，不显示本次涨跌／破价／未破价结论的替代解释。</p>"
        )
        if last_rel and last_rel.get("run_id") != current_rec.get("run_id"):
            change_block += (
                "<p>历史最近一次可靠价格（仅供参考，不代替当前）："
                f"{esc(last_rel.get('reliable_price'))} "
                f"{esc(last_rel.get('currency'))}／"
                f"{esc(last_rel.get('unit'))}，"
                f"时间 {esc(last_rel.get('run_ended_at_utc') or last_rel.get('run_started_at_utc'))}"
                f"（run_id={esc(last_rel.get('run_id'))}）</p>"
            )
        elif last_rel and last_rel.get("run_id") == current_rec.get("run_id"):
            pass
        else:
            change_block += "<p>暂无可比较的历史可靠价格。</p>"
    elif change.get("computed"):
        prev = change.get("previous") or {}
        change_block = (
            f"<p>相对历史 run <code>{esc(prev.get('run_id'))}</code>："
            f"{esc(change.get('direction'))} "
            f"{esc(change.get('delta'))} "
            f"（{esc(prev.get('reliable_price'))} → {esc(current_rec.get('reliable_price'))}）</p>"
        )
    else:
        change_block = f"<p>{esc(change.get('reason') or '暂无可比较的历史记录')}</p>"

    limitations = summary.get("limitations") or []
    lim_html = "".join(f"<li>{esc(x)}</li>" for x in limitations)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>PriceRadar 运行报告 {esc(summary.get('run_id'))}</title>
<style>
:root {{
  --bg: #f6f4ef;
  --card: #fffdf8;
  --ink: #1c1a16;
  --muted: #5c574e;
  --line: #d9d2c5;
  --accent: #0b5f4b;
  --warn: #8a4b00;
  --bad: #8b1e1e;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  background: linear-gradient(180deg, #ece7dc, var(--bg));
  color: var(--ink);
  line-height: 1.5;
}}
main {{ max-width: 960px; margin: 0 auto; padding: 28px 18px 64px; }}
h1,h2 {{ margin: 0 0 12px; font-weight: 650; }}
h1 {{ font-size: 1.6rem; }}
h2 {{ font-size: 1.15rem; margin-top: 28px; color: var(--accent); }}
.card {{
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 16px 18px;
  margin-top: 12px;
}}
.muted {{ color: var(--muted); font-size: 0.92rem; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
@media (max-width: 720px) {{ .grid {{ grid-template-columns: 1fr; }} }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
th, td {{ border-bottom: 1px solid var(--line); padding: 8px 6px; text-align: left; vertical-align: top; }}
th {{ color: var(--muted); font-weight: 600; }}
.badge {{
  display: inline-block; padding: 2px 8px; border-radius: 999px;
  border: 1px solid var(--line); font-size: 0.85rem;
}}
.badge.warn {{ color: var(--warn); border-color: #e0b36a; }}
.badge.ok {{ color: var(--accent); }}
.badge.bad {{ color: var(--bad); }}
ul.evidence {{ list-style: none; padding: 0; margin: 0; display: grid; gap: 14px; }}
.thumb img {{
  max-width: 100%; max-height: 220px; border: 1px solid var(--line);
  border-radius: 8px; background: #fff;
}}
.missing {{ color: var(--warn); margin-top: 4px; }}
.file {{ font-size: 0.8rem; color: var(--muted); }}
.callout {{
  border-left: 4px solid var(--warn); padding: 8px 12px; background: #fff7ea;
  margin: 10px 0;
}}
code {{ background: #efeae0; padding: 1px 5px; border-radius: 4px; }}
</style>
</head>
<body>
<main>
  <h1>PriceRadar 单商品运行报告</h1>
  <p class="muted">本地离线报告 · 规则 {esc(RULES_VERSION)} · 不自动宣称价格采集成功</p>

  <h2>1. 本次运行概况</h2>
  <div class="card">
    <p><strong>run_id</strong>：<code>{esc(summary.get('run_id'))}</code></p>
    <p><strong>开始（UTC）</strong>：{esc(summary.get('started_at_utc'))}</p>
    <p><strong>结束（UTC）</strong>：{esc(summary.get('ended_at_utc'))}</p>
    <p><strong>流程是否完成</strong>：
      <span class="badge {'ok' if completed else 'bad'}">{'是' if completed else '否'}（{esc(wf)}）</span>
    </p>
    <p><strong>是否获得可靠价格</strong>：
      <span class="badge {'ok' if reliable else 'warn'}">{'是' if reliable else '否'}</span>
    </p>
    <div class="callout">
      「流程完成」仅表示截图→OCR→比价流水线跑完；
      若结论为待核验或金额为空，<strong>不能</strong>称为价格采集成功。
    </div>
    <p>截图状态：{esc(summary.get('capture_status') or row.get('capture_status'))}</p>
    <p>OCR 状态：{esc(summary.get('ocr_status') or row.get('ocr_status'))}</p>
    <p>比价结论：<strong>{esc(verdict_zh(verdict))}</strong>
      （原始 verdict=<code>{esc(verdict)}</code>，
      comparison_status=<code>{esc(row.get('comparison_status'))}</code>）</p>
  </div>

  <h2>2. 商品与价格</h2>
  <div class="grid">
    <div class="card">
      <h3>预先登记（业务配置）</h3>
      <p class="muted">不是本次页面识别结果</p>
      <p>商品 ID：{esc(expected['product_id'])}</p>
      <p>业务型号：{esc(expected['expected_model'])}</p>
      <p>目标规格：{esc(expected['target_spec'])}</p>
      <p>预期店铺：{esc(expected['expected_shop'])}</p>
      <p>红线价：{esc(expected['red_line'])} {esc(expected['currency'])}／{esc(expected['comparison_unit'])}</p>
    </div>
    <div class="card">
      <h3>本次实际识别</h3>
      <p class="muted">仅来自本次 OCR／比价输出</p>
      <p>识别型号：{esc(observed['model'] or '（未识别）')}</p>
      <p>识别规格原文：{esc(observed['spec_raw'] or '（未识别）')}</p>
      <p>规范化规格：{esc(observed['spec_normalized'] or '（无）')}</p>
      <p>识别店铺：{esc(observed['shop'] or '（未识别）')}</p>
      <p>页面识别金额：{esc(row.get('price') if row.get('price') not in (None, '') else '（无）')}</p>
      <p>可靠每箱价格：<strong>{price_display}</strong></p>
      <p>币种／口径：{esc(observed['currency'] or '（空）')}；
         单位提示：{esc(observed['red_line_unit'] or expected['comparison_unit'] or '（空）')}</p>
      <p>价格口径：{esc(observed['price_basis'] or '（空）')}</p>
      <p>包装匹配：{esc(row.get('packaging_match') or '（未记录）')}；
         可比价：{esc(row.get('box_comparable'))}</p>
      <p>换算后每箱价：{esc(row.get('normalized_price_cny_per_box') if row.get('normalized_price_cny_per_box') not in (None, '') else '（空）')}</p>
      <p>差额：{esc(row.get('price_delta') if row.get('price_delta') not in (None, '') else '（空／未比较）')}</p>
      <p>破价结论：{esc(verdict_zh(verdict))}</p>
      <p class="muted">单位核验：{esc(row.get('unit_verify_basis') or '（空）')}</p>
    </div>
  </div>
  <div class="card">
    <h3>用户确认的箱规依据（业务配置，非本次 OCR）</h3>
    <p>来源：{esc(((summary.get('product_config_snapshot') or {}).get('packaging_basis') or {}).get('source_name') or 'config/products')}</p>
    <p>确认说明：{esc(((summary.get('product_config_snapshot') or {}).get('packaging_basis') or {}).get('source_note') or '用户提供截图，经对话确认')}</p>
    <p>确认日期：{esc(((summary.get('product_config_snapshot') or {}).get('packaging_basis') or {}).get('confirmed_date') or '—')}</p>
    <p>适用型号／箱规：B913A；1层；200张／包；20包／箱；红线127元／箱（严格低于才破价）。</p>
    <p class="muted">仅当本次页面型号与已选规格匹配时才可引用；仅有配置不足以通过核验。网页「抽」与业务「张」仅在本型号已确认别名范围内互通。</p>
  </div>

  <h2>3. 截图证据</h2>
  <div class="card">
    <p class="muted">仅展示本次关联证据；缺失不使用旧图补齐。可点击查看原图。</p>
    <ul class="evidence">
      {''.join(evidence_html_parts)}
    </ul>
  </div>

  <h2>4. 待核验原因</h2>
  <div class="card">
    <ul>{reason_items}</ul>
    <p class="muted">原始 review_reasons 未改写比价判断。</p>
  </div>

  <h2>5. 历史与价格变化</h2>
  <div class="card">
    {change_block}
    <p class="muted">最近一次有可靠价格的记录：
      {esc((last_rel or {}).get('reliable_price') or '（无）')}
      @ {esc((last_rel or {}).get('run_ended_at_utc') or (last_rel or {}).get('run_started_at_utc') or '—')}
      （run_id={esc((last_rel or {}).get('run_id') or '—')}）
    </p>
    <table>
      <thead>
        <tr>
          <th>run_id</th><th>时间(UTC)</th><th>流程</th><th>结论</th>
          <th>可靠价格</th><th>当次红线</th><th>原因</th>
        </tr>
      </thead>
      <tbody>{hist_table}</tbody>
    </table>
  </div>

  <h2>限制说明</h2>
  <div class="card">
    <ul>{lim_html or '<li>无</li>'}</ul>
    <p class="muted">本报告不代表自动搜索、可靠价格采集或完整监控系统已完成。</p>
  </div>
</main>
</body>
</html>
"""


def generate_report_and_history(
    run_dir: Path,
    *,
    history_root: Path | None = None,
) -> dict[str, Any]:
    """
    Offline: read one end_to_end run dir → write report.html + update product history.
    Does not launch browser or re-run OCR. Does not change comparison verdict.
    """
    started = utc_iso()
    bundle = load_e2e_bundle(run_dir)
    summary = bundle["summary"]
    row = bundle["row"]
    expected = expected_fields(bundle)
    product_id = str(expected["product_id"] or "").strip()
    if not product_id:
        raise ValueError("无法确定商品 ID，拒绝写入历史")

    hist_root = (history_root or HISTORY_ROOT).resolve()
    history_dir = hist_root / product_id

    # Write report first (paths needed in history record).
    report_path = bundle["run_dir"] / "report.html"
    # Temporary placeholder history for rendering; upsert after.
    # Load existing history (may raise on corruption).
    existing = load_history(history_dir) if history_dir.exists() else {
        "product_id": product_id,
        "records": [],
    }

    draft_rec = history_record_from_bundle(bundle, report_path=report_path)
    # Preview history as if upserted (for HTML), without writing yet.
    preview_records = [
        r for r in (existing.get("records") or []) if isinstance(r, dict)
    ]
    preview_records = [
        r for r in preview_records if r.get("run_id") != draft_rec["run_id"]
    ] + [draft_rec]
    preview_history = {
        "product_id": product_id,
        "records": sort_history_records(preview_records),
    }
    change = price_change_info(draft_rec, preview_history["records"])

    html_text = render_html(
        bundle,
        history=preview_history,
        current_rec=draft_rec,
        change=change,
    )
    # Refuse script injection from dynamic fields via esc(); write atomically.
    atomic_write_text(report_path, html_text, encoding="utf-8")

    history_data, inserted_new = upsert_history(history_dir, draft_rec)

    # Annotate run_summary without changing verdict.
    try:
        summary_path = bundle["run_dir"] / "run_summary.json"
        summary_updated = dict(summary)
        summary_updated["report"] = {
            "status": "OK",
            "report_path": str(report_path.resolve()),
            "history_dir": str(history_dir.resolve()),
            "history_inserted_new": inserted_new,
            "history_record_count": len(history_data.get("records") or []),
            "generated_at_utc": utc_iso(),
            "rules_version": RULES_VERSION,
            "started_at_utc": started,
        }
        # Never alter comparison fields.
        atomic_write_json(summary_path, summary_updated)
    except OSError as exc:
        # Report file already written; surface annotation failure separately.
        return {
            "ok": True,
            "report_path": report_path.resolve(),
            "history_dir": history_dir.resolve(),
            "history_inserted_new": inserted_new,
            "history_record_count": len(history_data.get("records") or []),
            "verdict_unchanged": row.get("verdict"),
            "warning": f"报告已生成，但更新 run_summary 失败: {exc}",
        }

    return {
        "ok": True,
        "report_path": report_path.resolve(),
        "history_dir": history_dir.resolve(),
        "history_inserted_new": inserted_new,
        "history_record_count": len(history_data.get("records") or []),
        "verdict_unchanged": row.get("verdict"),
        "product_id": product_id,
        "change_computed": bool(change.get("computed")),
    }


def run_self_tests() -> list[str]:
    failures: list[str] = []
    tmp = ARTIFACTS_ROOT / f"_p4e_selftest_{secrets.token_hex(3)}"
    hist_root = tmp / "product_history"
    e2e = tmp / "end_to_end_runs" / "test_run_a"
    e2e.mkdir(parents=True, exist_ok=True)
    try:
        # Minimal REVIEW_REQUIRED fixture under artifacts (no real history write).
        capture = tmp / "price_captures" / "cap_a"
        capture.mkdir(parents=True)
        (capture / "price.png").write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0c"
            b"IDAT\x08\x1dc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00"
            b"\x00\x00IEND\xaeB`\x82"
        )
        (capture / "metadata.json").write_text(
            json.dumps(
                {
                    "run_id": "cap_a",
                    "regions": {
                        "price": {
                            "filename": "price.png",
                            "status": "CAPTURED",
                            "captured_at_utc": "2026-10-19T03:39:30+00:00",
                        },
                        "product_title": {
                            "filename": "product_title.png",
                            "status": "AMBIGUOUS",
                            "error": "multiple similar-score matches",
                        },
                    },
                    "discovery": {
                        "product_title": {
                            "status": "AMBIGUOUS",
                            "reason": "multiple similar-score matches",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        summary = {
            "run_id": "test_run_a",
            "started_at_utc": "2026-10-19T03:39:28+00:00",
            "ended_at_utc": "2026-10-19T03:40:15+00:00",
            "workflow_status": "COMPLETED_WITH_CAPTURE_GAPS",
            "capture_status": "PARTIAL",
            "ocr_status": "SUCCESS",
            "comparison_status": "NOT_EVALUATED",
            "verdict": "REVIEW_REQUIRED",
            "capture_dir": str(capture),
            "product_config_snapshot": {
                "business_sku": "B913A",
                "product_id": "999000001",
                "expected_model": "B913A",
                "target_spec": "单层200抽×20包",
                "expected_shop": "测试店",
                "red_line": "127.00",
                "currency": "CNY",
                "comparison_unit": "箱",
                "packs_per_box": 20,
            },
            "evidence_image_paths": [str(capture / "price.png")],
            "limitations": ["测试限制"],
        }
        row = {
            "workflow_status": "COMPLETED_WITH_CAPTURE_GAPS",
            "capture_status": "PARTIAL",
            "ocr_status": "SUCCESS",
            "comparison_status": "NOT_EVALUATED",
            "verdict": "REVIEW_REQUIRED",
            "product_id": "999000001",
            "business_sku": "B913A",
            "observed_model": "",
            "price": None,
            "currency": "CNY",
            "red_line_price": "127.00",
            "red_line_unit": "CNY_per_box",
            "shop": "",
            "review_reasons": "裁切风险; 型号未核验",
            "screenshot_dir": str(capture),
        }
        (e2e / "run_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (e2e / "compare_table.json").write_text(
            json.dumps([row], ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Monkeypatch roots by passing history_root; load_e2e allows under artifacts.
        r1 = generate_report_and_history(e2e, history_root=hist_root)
        if not (e2e / "report.html").is_file():
            failures.append("report.html not written")
        html_text = (e2e / "report.html").read_text(encoding="utf-8")
        if "未获得可靠价格" not in html_text:
            failures.append("REVIEW should show empty reliable price")
        if "未破价" in html_text and "待核验（非未破价）" not in html_text:
            failures.append("REVIEW must not be shown as 未破价 alone")
        if "multiple similar-score matches" not in html_text:
            failures.append("missing title reason should appear")
        if "<script>alert" in html_text:
            failures.append("unexpected script")

        # XSS escape
        row2 = dict(row)
        row2["review_reasons"] = '<script>alert(1)</script>; 测试'
        summary2 = dict(summary)
        summary2["run_id"] = "test_run_xss"
        e2e2 = tmp / "end_to_end_runs" / "test_run_xss"
        e2e2.mkdir(parents=True)
        # reuse capture
        summary2["capture_dir"] = str(capture)
        (e2e2 / "run_summary.json").write_text(
            json.dumps(summary2, ensure_ascii=False), encoding="utf-8"
        )
        (e2e2 / "compare_table.json").write_text(
            json.dumps([row2], ensure_ascii=False), encoding="utf-8"
        )
        generate_report_and_history(e2e2, history_root=hist_root)
        xss_html = (e2e2 / "report.html").read_text(encoding="utf-8")
        if "<script>alert(1)</script>" in xss_html:
            failures.append("HTML not escaped")
        if "&lt;script&gt;" not in xss_html:
            failures.append("expected escaped script entities")

        # Dedupe same run_id
        r2 = generate_report_and_history(e2e, history_root=hist_root)
        hist = load_history(hist_root / "999000001")
        count_a = sum(1 for r in hist["records"] if r.get("run_id") == "test_run_a")
        if count_a != 1:
            failures.append(f"run_id dedupe failed, count={count_a}")
        if r2.get("history_inserted_new") is not False:
            failures.append("second generate should not insert new")

        # Different product isolation
        e2e3 = tmp / "end_to_end_runs" / "test_run_other"
        e2e3.mkdir(parents=True)
        summary3 = dict(summary)
        summary3["run_id"] = "test_run_other"
        summary3["product_config_snapshot"] = dict(summary["product_config_snapshot"])
        summary3["product_config_snapshot"]["product_id"] = "999000002"
        summary3["capture_dir"] = str(capture)
        row3 = dict(row)
        row3["product_id"] = "999000002"
        (e2e3 / "run_summary.json").write_text(
            json.dumps(summary3, ensure_ascii=False), encoding="utf-8"
        )
        (e2e3 / "compare_table.json").write_text(
            json.dumps([row3], ensure_ascii=False), encoding="utf-8"
        )
        generate_report_and_history(e2e3, history_root=hist_root)
        if (hist_root / "999000002" / "history.json").is_file() is False:
            failures.append("other product history missing")
        hist1 = load_history(hist_root / "999000001")
        if any(r.get("product_id") == "999000002" for r in hist1["records"]):
            failures.append("products mixed in history")

        # Path escape rejection in URI helper
        outside = PROJECT_ROOT / "README.md"
        if relative_uri_from_report(e2e, outside) is not None:
            failures.append("outside artifacts path should be rejected")

        # Comparable price change with temp records (not real product history)
        cur = {
            "run_id": "c1",
            "product_id": "1",
            "reliable_price": "100.00",
            "currency": "CNY",
            "unit": "箱",
            "observed_model": "B913A",
            "observed_spec": "层=1;包=20",
            "price_basis": "currency=¥",
        }
        prev = {
            "run_id": "c0",
            "product_id": "1",
            "reliable_price": "110.00",
            "currency": "CNY",
            "unit": "箱",
            "observed_model": "B913A",
            "observed_spec": "层=1;包=20",
            "price_basis": "currency=¥",
            "run_ended_at_utc": "2026-10-18T00:00:00+00:00",
        }
        ch = price_change_info(cur, [prev, cur])
        if not ch.get("computed") or ch.get("delta") != "-10.00":
            failures.append(f"expected -10.00 delta, got {ch}")
        bad = dict(prev)
        bad["observed_model"] = "OTHER"
        ch2 = price_change_info(cur, [bad, cur])
        if ch2.get("computed"):
            failures.append("incomparable models should not compute change")
        review_cur = dict(cur)
        review_cur["reliable_price"] = None
        ch3 = price_change_info(review_cur, [prev])
        if ch3.get("computed"):
            failures.append("REVIEW current must not compute change")

        # Corrupt history should error
        bad_hist = hist_root / "corrupt_pid"
        bad_hist.mkdir(parents=True)
        (bad_hist / "history.json").write_text("{not-json", encoding="utf-8")
        try:
            load_history(bad_hist)
            failures.append("corrupt history should raise")
        except ValueError:
            pass

        if r1.get("verdict_unchanged") != "REVIEW_REQUIRED":
            failures.append("verdict should remain REVIEW_REQUIRED")

    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    return failures


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PriceRadar P4-E report + history")
    p.add_argument(
        "--run-dir",
        help="Exact end_to_end_runs directory (not guessed latest)",
    )
    p.add_argument(
        "--history-root",
        default="",
        help="Optional history root (tests only; default artifacts/product_history)",
    )
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args(argv)
    if not args.self_test and not args.run_dir:
        p.error("--run-dir is required unless --self-test")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        fails = run_self_tests()
        safe_print(f"self_test_failures={fails}")
        return 0 if not fails else 1

    run_dir = Path(args.run_dir)
    history_root = Path(args.history_root) if args.history_root else None
    safe_print("=== PriceRadar P4-E ===")
    safe_print(f"run-dir: {run_dir.resolve()}")
    try:
        result = generate_report_and_history(run_dir, history_root=history_root)
    except (OSError, ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        safe_print(f"报告/历史生成失败: {type(exc).__name__}: {exc}")
        return 1

    safe_print(f"报告状态: {'OK' if result.get('ok') else 'FAILED'}")
    safe_print(f"HTML 报告: {result.get('report_path')}")
    safe_print(f"历史目录: {result.get('history_dir')}")
    safe_print(
        f"历史记录数: {result.get('history_record_count')} "
        f"(本次新增={result.get('history_inserted_new')})"
    )
    safe_print(f"比价结论保持不变: {result.get('verdict_unchanged')}")
    if result.get("warning"):
        safe_print(f"警告: {result['warning']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
