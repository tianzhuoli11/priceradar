"""
Unified acceptance report for fixed 3 samples (selected-spec + capture stability).

Does not re-search. Sources prior runs / optional live verify dirs without overwriting them.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
OUT_ROOT = (PROJECT_ROOT / "artifacts" / "acceptance_runs").resolve()

FIXED_IDS = ("1556260334", "100278834412", "10181313217018")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def find_item_dir(run_dir: Path, product_id: str) -> Path | None:
    items = run_dir / "items"
    if not items.is_dir():
        return None
    for p in sorted(items.iterdir()):
        if p.is_dir() and p.name.endswith(f"_{product_id}"):
            return p
    return None


def summarize_item(
    *,
    product_id: str,
    source_kind: str,
    source_run: Path,
    verify: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    capture = verify.get("capture") or {}
    capture_dir = Path(
        str(
            capture.get("out_dir")
            or verify.get("screenshot_dir")
            or ""
        )
    )
    sel = {}
    pipe_detail = {}
    pipe_dir = verify.get("pipeline_dir") or (verify.get("pipeline") or {}).get("out_dir")
    if pipe_dir:
        detail_path = Path(pipe_dir) / "detail.json"
        if detail_path.is_file():
            try:
                pipe_detail = load_json(detail_path)
            except (OSError, json.JSONDecodeError):
                pipe_detail = {}
    sel = pipe_detail.get("current_selection") or {}
    if not sel and capture_dir.is_dir():
        for name in ("selection_state_after.json", "selection_state_before.json"):
            p = capture_dir / name
            if p.is_file():
                try:
                    sel = load_json(p)
                    break
                except (OSError, json.JSONDecodeError):
                    pass
    clues = verify.get("price_clues") or []
    primary = [
        c
        for c in clues
        if c.get("price_type") == "PAGE_PRIMARY_SELLING"
    ]
    declared = [
        c
        for c in clues
        if c.get("price_type")
        in {
            "PAGE_DECLARED_CONDITIONAL_TAKE_HOME",
            "PAGE_ESTIMATED_TAKE_HOME",
            "RULE_CALCULATED_TAKE_HOME",
        }
    ]
    pending = [
        c
        for c in clues
        if c.get("price_type")
        in {"PAGE_CONDITIONAL_AMOUNT_CANDIDATE", "PAGE_UNVERIFIED_PANEL_AMOUNT"}
    ]
    low = verify.get("low_price_signal")
    if low is None:
        # derive from clues / page primary
        for c in primary:
            if c.get("low_price_hint"):
                low = True
                break
    shots = {}
    if capture_dir.is_dir():
        for name in (
            "price.png",
            "product_title.png",
            "selected_spec.png",
            "viewport_diagnostic.png",
        ):
            p = capture_dir / name
            if p.is_file():
                shots[name] = str(p)
    return {
        "product_id": product_id,
        "source_kind": source_kind,
        "source_run_id": source_run.name,
        "source_run_dir": str(source_run),
        "capture_run_id": capture.get("run_id")
        or (verify.get("capture") or {}).get("run_id")
        or source_run.name,
        "rules_versions": rules,
        "task_status": verify.get("task_status"),
        "match_status": verify.get("match_status"),
        "classification": (verify.get("title_gate") or {}).get("classification"),
        "authorization": (verify.get("title_gate_authorization") or {}).get("reason"),
        "observed_model": verify.get("observed_model"),
        "model_status": verify.get("model_status"),
        "model_authorized_other": verify.get("model_authorized_other"),
        "selected_status": sel.get("selected_status")
        or ("SELECTED_SPEC_UNKNOWN" if not sel.get("selected_spec_text") else "LEGACY"),
        "selected_spec_text": sel.get("selected_spec_text"),
        "selected_options": sel.get("selected_options") or [],
        "available_options": sel.get("available_options") or [],
        "variant_id": sel.get("variant_id"),
        "purchase_quantity": sel.get("purchase_quantity"),
        "purchase_quantity_status": sel.get("purchase_quantity_status") or "UNKNOWN",
        "selection_evidence": sel.get("selection_evidence"),
        "page_primary_price": verify.get("page_primary_price"),
        "primary_clues": [
            {
                "amount": c.get("amount"),
                "label": c.get("raw_label"),
                "source": c.get("evidence_source"),
            }
            for c in primary
        ],
        "declared_clues": [
            {
                "amount": c.get("amount"),
                "label": c.get("raw_label"),
                "type": c.get("price_type"),
                "assoc": c.get("association_confidence"),
                "verdict": c.get("system_verdict"),
                "normalized": c.get("normalized_price"),
            }
            for c in declared
        ],
        "pending_amount_clues": [
            {
                "amount": c.get("amount"),
                "type": c.get("price_type"),
                "assoc": c.get("association_confidence"),
            }
            for c in pending
        ],
        "packaging_match": verify.get("packaging_match"),
        "box_comparable": verify.get("box_comparable"),
        "system_verdict": verify.get("system_verdict"),
        "system_verdict_zh": verify.get("system_verdict_zh"),
        "manual_verdict": verify.get("manual_verdict"),
        "low_price_signal": bool(low) if low is not None else False,
        "review_reasons": verify.get("review_reasons"),
        "gaps": [
            g
            for g in [
                None
                if sel.get("selected_status") == "SELECTED_SPEC_EXTRACTED"
                else "选中规格未确认或仅有可选列表",
                None
                if sel.get("purchase_quantity_status") == "OBSERVED"
                else "购买数量 UNKNOWN（不默认1）",
                None if verify.get("manual_verdict") is None else "manual_verdict 非空",
            ]
            if g
        ],
        "screenshots": shots,
        "verify_dir": verify.get("verify_dir"),
        "pipeline_dir": pipe_dir,
        "capture_dir": str(capture_dir) if capture_dir else None,
    }


def write_html(path: Path, rows: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    parts = [
        "<!doctype html><meta charset=utf-8>",
        "<title>PriceRadar acceptance</title>",
        "<style>body{font-family:Segoe UI,sans-serif;margin:24px;max-width:1100px}"
        "table{border-collapse:collapse;width:100%;font-size:13px}"
        "th,td{border:1px solid #ccc;padding:6px 8px;vertical-align:top}"
        "th{background:#f4f4f4;text-align:left}"
        "code{font-size:12px}.gap{color:#a40}.ok{color:#064}</style>",
        "<h1>PriceRadar 统一验收</h1>",
        f"<p>run=<code>{html.escape(meta.get('run_id',''))}</code> "
        f"rules=<code>{html.escape(json.dumps(meta.get('rules_versions',{}),ensure_ascii=False))}</code></p>",
        f"<p>{html.escape(meta.get('summary_note',''))}</p>",
    ]
    for r in rows:
        parts.append(f"<h2>{html.escape(r['product_id'])} · {html.escape(str(r.get('source_kind')))}</h2>")
        parts.append("<table>")
        fields = [
            ("source_run_id", r.get("source_run_id")),
            ("capture_run_id", r.get("capture_run_id")),
            ("task/match", f"{r.get('task_status')} / {r.get('match_status')}"),
            ("classification/auth", f"{r.get('classification')} / {r.get('authorization')}"),
            ("observed_model", f"{r.get('observed_model')} ({r.get('model_status')})"),
            ("selected_status", r.get("selected_status")),
            ("selected_spec", r.get("selected_spec_text")),
            ("available_options", json.dumps(r.get("available_options") or [], ensure_ascii=False)[:500]),
            ("qty", f"{r.get('purchase_quantity')} / {r.get('purchase_quantity_status')}"),
            ("primary", json.dumps(r.get("primary_clues") or [], ensure_ascii=False)),
            ("declared", json.dumps(r.get("declared_clues") or [], ensure_ascii=False)),
            ("pending_amounts", json.dumps(r.get("pending_amount_clues") or [], ensure_ascii=False)),
            ("packaging_match", r.get("packaging_match")),
            ("box_comparable", r.get("box_comparable")),
            ("system_verdict", f"{r.get('system_verdict')} | {r.get('system_verdict_zh')}"),
            ("low_price_signal", r.get("low_price_signal")),
            ("manual_verdict", r.get("manual_verdict")),
            ("gaps", "; ".join(r.get("gaps") or [])),
        ]
        for k, v in fields:
            parts.append(
                f"<tr><th>{html.escape(k)}</th><td><code>{html.escape(str(v))}</code></td></tr>"
            )
        shots = r.get("screenshots") or {}
        links = " ".join(
            f'<a href="{html.escape(p)}">{html.escape(n)}</a>' for n, p in shots.items()
        )
        parts.append(f"<tr><th>screenshots</th><td>{links or '（无）'}</td></tr>")
        if r.get("verify_dir"):
            parts.append(
                f"<tr><th>verify_dir</th><td><code>{html.escape(str(r['verify_dir']))}</code></td></tr>"
            )
        parts.append("</table>")
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    cols = [
        "product_id",
        "source_kind",
        "source_run_id",
        "capture_run_id",
        "task_status",
        "observed_model",
        "selected_status",
        "selected_spec_text",
        "purchase_quantity_status",
        "packaging_match",
        "box_comparable",
        "page_primary_price",
        "system_verdict",
        "system_verdict_zh",
        "low_price_signal",
        "manual_verdict",
        "capture_dir",
        "verify_dir",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--sources-json",
        type=Path,
        required=True,
        help="JSON map product_id -> {kind, run_dir}",
    )
    p.add_argument("--out-dir", type=Path, default=None)
    args = p.parse_args(argv)

    sources = load_json(Path(args.sources_json).resolve())
    out = (
        Path(args.out_dir).resolve()
        if args.out_dir
        else OUT_ROOT / f"{utc_stamp()}_acceptance_{secrets.token_hex(3)}"
    )
    out.mkdir(parents=True, exist_ok=False)

    sys.path.insert(0, str(SCRIPTS))
    import priceradar_selected_spec as sel  # noqa: E402
    import priceradar_take_home_prices as th  # noqa: E402

    offline = sel.run_selected_spec_offline_checks()
    th_fails = th.run_local_take_home_checks()
    (out / "offline_checks.json").write_text(
        json.dumps(
            {
                "selected_spec": offline,
                "take_home": th_fails,
                "ok": not offline and not th_fails,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if offline or th_fails:
        print("OFFLINE CHECKS FAILED", offline, th_fails)
        return 1

    rows: list[dict[str, Any]] = []
    rules = {
        "selected_spec": sel.RULES_VERSION_SELECTED_SPEC,
        "take_home": th.RULES_VERSION_TAKE_HOME,
    }
    for pid in FIXED_IDS:
        src = sources.get(pid) or sources.get(str(pid))
        if not src:
            rows.append(
                {
                    "product_id": pid,
                    "source_kind": "MISSING_SOURCE",
                    "gaps": ["sources-json 未提供该商品"],
                    "manual_verdict": None,
                    "screenshots": {},
                }
            )
            continue
        run_dir = Path(src["run_dir"]).resolve()
        kind = str(src.get("kind") or "unknown")
        item = find_item_dir(run_dir, pid)
        if not item:
            rows.append(
                {
                    "product_id": pid,
                    "source_kind": kind,
                    "source_run_id": run_dir.name,
                    "gaps": [f"items 下找不到 {pid}"],
                    "manual_verdict": None,
                    "screenshots": {},
                }
            )
            continue
        vr_path = item / "verify" / "verify_result.json"
        verify = load_json(vr_path) if vr_path.is_file() else {}
        rules_i = dict(rules)
        rules_i["verify"] = verify.get("rules_version")
        rules_i["take_home_run"] = verify.get("take_home_rules_version")
        rows.append(
            summarize_item(
                product_id=pid,
                source_kind=kind,
                source_run=run_dir,
                verify=verify,
                rules=rules_i,
            )
        )

    meta = {
        "run_id": out.name,
        "phase": "P5-J-UNIFIED-ACCEPTANCE",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "rules_versions": rules,
        "sources": sources,
        "summary_note": (
            "主报告汇总固定3条；source_kind 区分旧证据重处理 / 现场新采 / 补采；"
            "不覆盖源目录；manual_verdict 必须为 null。"
        ),
    }
    (out / "acceptance_manifest.json").write_text(
        json.dumps({"meta": meta, "rows": rows}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_html(out / "acceptance_report.html", rows, meta)
    write_csv(out / "acceptance_report.csv", rows)
    print(json.dumps({"out": str(out), "rows": len(rows)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
