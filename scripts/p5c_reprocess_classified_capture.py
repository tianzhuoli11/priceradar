"""
Offline reprocess: existing classified capture screenshots → p4c/p5a (no browser).

Does not modify the source run directory.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE_RUN = (
    PROJECT_ROOT
    / "artifacts"
    / "classified_detail_samples"
    / "20261102T021830Z_p5i_ca98de"
)
DEFAULT_POLICY = (
    PROJECT_ROOT
    / "config"
    / "policies"
    / "b913a_model_scope_include_other_20261027.json"
)
OUT_ROOT = PROJECT_ROOT / "artifacts" / "classified_detail_samples_offline"


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="P5-C offline reprocess classified capture")
    p.add_argument("--source-run-dir", type=Path, default=DEFAULT_SOURCE_RUN)
    p.add_argument("--product-id", default="1556260334")
    p.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    p.add_argument(
        "--business-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "products" / "b913a.json",
    )
    p.add_argument("--out-dir", type=Path, default=None)
    args = p.parse_args(argv)

    source_run = Path(args.source_run_dir).resolve()
    capture_dir = source_run / "items" / f"01_{args.product_id}" / "verify" / "capture"
    if not capture_dir.is_dir():
        # Allow 02_/03_ item folders from multi-sample runs.
        items_root = source_run / "items"
        if items_root.is_dir():
            for d in sorted(items_root.iterdir()):
                if d.is_dir() and d.name.endswith(f"_{args.product_id}"):
                    cand = d / "verify" / "capture"
                    if cand.is_dir():
                        capture_dir = cand
                        break
    candidates = source_run / "candidates.json"
    if not capture_dir.is_dir():
        print(f"capture dir missing: {capture_dir}")
        return 2
    if not candidates.is_file():
        print(f"candidates missing: {candidates}")
        return 2

    if args.out_dir:
        out_run = Path(args.out_dir).resolve()
    else:
        tag = secrets.token_hex(3)
        out_run = (
            OUT_ROOT / f"{utc_stamp()}_reprocess_{source_run.name}_{tag}"
        ).resolve()
    verify_out = out_run / "items" / f"01_{args.product_id}" / "verify"
    verify_out.mkdir(parents=True, exist_ok=True)

    policy = json.loads(Path(args.policy).read_text(encoding="utf-8-sig"))

    sys.path.insert(0, str(SCRIPTS_DIR))
    import p5a_verify_candidate as p5a  # noqa: E402

    print("=== offline self-checks (take-home + p5a) ===")
    th_fails = p5a.take_home.run_local_take_home_checks()
    if th_fails:
        print("take-home checks failed:", th_fails)
        return 1
    if p5a.run_self_checks() != 0:
        return 1

    print(f"=== reprocess capture (read-only source) ===")
    print(f"source_run={source_run}")
    print(f"capture_dir={capture_dir}")
    print(f"out={verify_out}")

    result = p5a.verify_one(
        business_config=Path(args.business_config),
        candidates_path=candidates,
        product_id=str(args.product_id),
        offline_capture_dir=capture_dir,
        model_scope_policy=policy,
        out_dir=verify_out,
    )

    manifest = {
        "phase": "P5-C-OFFLINE-REPROCESS",
        "source_run_dir": str(source_run),
        "source_capture_dir": str(capture_dir),
        "out_dir": str(out_run),
        "product_id": str(args.product_id),
        "unchanged_source": True,
    }
    (out_run / "reprocess_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    schemes_path = verify_out / "price_schemes.json"
    schemes_path.write_text(
        json.dumps(result.get("price_clues") or [], ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )

    compare_html = out_run / "compare_report.html"
    compare_html.write_text(
        _minimal_report_html(result, source_run=source_run, out_run=out_run),
        encoding="utf-8",
    )

    print(json.dumps({
        "task_status": result.get("task_status"),
        "match_status": result.get("match_status"),
        "price_evidence_status": result.get("price_evidence_status"),
        "system_verdict": result.get("system_verdict"),
        "system_verdict_zh": result.get("system_verdict_zh"),
        "take_home_clue_count": (result.get("take_home_rollup") or {}).get(
            "take_home_clue_count"
        ),
        "packaging_match": result.get("packaging_match"),
        "box_comparable": result.get("box_comparable"),
        "out_run": str(out_run),
        "verify_result": str(verify_out / "verify_result.json"),
        "price_schemes": str(schemes_path),
        "compare_report": str(compare_html),
    }, ensure_ascii=False, indent=2))
    return 0 if result.get("task_status") == "COMPLETED" else 1


def _minimal_report_html(result: dict, *, source_run: Path, out_run: Path) -> str:
    clues = result.get("price_clues") or []
    rows = ""
    for c in clues:
        rows += (
            f"<tr><td>{c.get('price_type')}</td><td>{c.get('amount')}</td>"
            f"<td>{c.get('raw_label')}</td><td>{c.get('conditions')}</td>"
            f"<td>{c.get('evidence_source')}</td>"
            f"<td>{c.get('system_verdict')}</td></tr>"
        )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>Offline reprocess</title></head>
<body>
<h1>Offline reprocess (capture unchanged)</h1>
<p>Source run: {source_run}</p>
<p>Output: {out_run}</p>
<ul>
<li>task_status: {result.get('task_status')}</li>
<li>match_status: {result.get('match_status')}</li>
<li>price_evidence_status: {result.get('price_evidence_status')}</li>
<li>system_verdict: {result.get('system_verdict')} / {result.get('system_verdict_zh')}</li>
<li>packaging_match: {result.get('packaging_match')} box_comparable={result.get('box_comparable')}</li>
<li>model_status: {result.get('model_status')}</li>
</ul>
<h2>Price schemes</h2>
<table border="1"><tr><th>type</th><th>amount</th><th>label</th><th>conditions</th><th>source</th><th>verdict</th></tr>
{rows}
</table>
</body></html>"""


if __name__ == "__main__":
    raise SystemExit(main())
