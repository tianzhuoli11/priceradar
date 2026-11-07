"""
P5-H: Offline complete-model reclassification of an existing collect-all run.

Does not re-search, open browser, OCR, or overwrite the source collect run.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import secrets
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from priceradar_candidate_adapt import load_business_baseline  # noqa: E402
from priceradar_model_match import (  # noqa: E402
    CLASS_MULTI,
    CLASS_OTHER,
    CLASS_TARGET,
    RULES_VERSION,
    classify_title_models,
    run_offline_model_match_checks,
)

AUDIT_ROOT = (PROJECT_ROOT / "artifacts" / "model_classify_audits").resolve()
SOURCE_DEFAULT = (
    PROJECT_ROOT
    / "artifacts"
    / "collect_all_runs"
    / "20261027T052248Z_collect_134737"
)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            flat = {}
            for k in fields:
                v = row.get(k)
                if isinstance(v, (dict, list)):
                    flat[k] = json.dumps(v, ensure_ascii=False)
                else:
                    flat[k] = "" if v is None else v
            w.writerow(flat)


def load_candidates(source_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cand_path = source_dir / "all_candidates.json"
    cov_path = source_dir / "coverage.json"
    if not cand_path.is_file():
        raise FileNotFoundError(cand_path)
    candidates = json.loads(cand_path.read_text(encoding="utf-8-sig"))
    coverage = {}
    if cov_path.is_file():
        coverage = json.loads(cov_path.read_text(encoding="utf-8-sig"))
    if not isinstance(candidates, list):
        raise ValueError("all_candidates.json must be a list")
    return candidates, coverage


def reclassify_row(
    row: dict[str, Any],
    *,
    expected_model: str,
    source_run_id: str,
    source_collected_at: str | None,
) -> dict[str, Any]:
    title = str(row.get("title") or "")
    pid = str(row.get("jd_product_id") or row.get("product_id") or "")
    old_match = row.get("model_match")
    old_tag = row.get("model_tag")
    result = classify_title_models(title, expected_model=expected_model)
    return {
        "jd_product_id": pid,
        "business_product_id_config": None,  # filled by caller from config
        "title": title,
        "source_page": row.get("page"),
        "normalized_url": row.get("normalized_url"),
        "expected_model": expected_model,
        "observed_model_tokens": result.get("observed_model_tokens"),
        "classification": result.get("classification"),
        "reason": result.get("reason"),
        "rule_version": RULES_VERSION,
        "detail_verification_required": result.get("detail_verification_required"),
        "red_line_applicable": False,  # never from title alone
        "red_line_note": (
            "OTHER_MODEL明确不适用目标红线；"
            "其余仅详情核验通过后才允许参与红线比较"
        ),
        "old_model_match": old_match,
        "old_model_tag": old_tag,
        "old_match_reason": row.get("match_reason"),
        "transition": f"{old_match}→{result.get('classification')}",
        "source_run_id": source_run_id,
        "source_collected_at_utc": source_collected_at or row.get("discovered_at_utc"),
        "conflict_token": result.get("conflict_token"),
        "other_models_normalized": result.get("other_models_normalized"),
        "title_source": row.get("title_source"),
        "result_region": row.get("result_region"),
        "ad_status": row.get("ad_status"),
    }


def render_report(
    *,
    audit_id: str,
    source_run_id: str,
    expected_model: str,
    counts: dict[str, int],
    transitions: list[tuple[str, int]],
    examples: dict[str, list[dict[str, Any]]],
    false_hit_note: str,
    total: int,
) -> str:
    esc = html.escape

    def ex_table(rows: list[dict[str, Any]]) -> str:
        if not rows:
            return "<p>（无）</p>"
        body = []
        for r in rows:
            toks = json.dumps(r.get("observed_model_tokens"), ensure_ascii=False)
            body.append(
                "<tr>"
                f"<td>{esc(str(r.get('jd_product_id')))}</td>"
                f"<td>{esc(str(r.get('classification')))}</td>"
                f"<td>{esc(str(r.get('old_model_match')))}</td>"
                f"<td>{esc(str(r.get('reason')))}</td>"
                f"<td>{esc(str(r.get('title') or '')[:100])}</td>"
                f"<td><code>{esc(toks[:180])}</code></td>"
                "</tr>"
            )
        return (
            "<table><tr><th>id</th><th>新分类</th><th>旧model_match</th>"
            "<th>reason</th><th>title</th><th>tokens</th></tr>"
            + "".join(body)
            + "</table>"
        )

    tr_rows = "".join(
        f"<tr><td>{esc(a)}</td><td>{n}</td></tr>" for a, n in transitions
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<title>Model classify {esc(audit_id)}</title>
<style>
body{{font-family:"Segoe UI","Microsoft YaHei",sans-serif;margin:24px}}
table{{border-collapse:collapse;width:100%;font-size:0.85em}}
th,td{{border:1px solid #ccc;padding:4px 6px;vertical-align:top}}
th{{background:#f5f5f5}} .warn{{color:#a40}} .ok{{color:#060}}
</style></head><body>
<h1>型号完整匹配离线重分类</h1>
<p class="ok">标题筛选通过 ≠ 详情型号/规格核验通过。红线不可用直至详情确认。</p>
<p>audit_id={esc(audit_id)} · source_run={esc(source_run_id)}
 · expected_model={esc(expected_model)} · rules={esc(RULES_VERSION)}</p>
<p>总数={total} · TARGET={counts.get(CLASS_TARGET,0)}
 · OTHER={counts.get(CLASS_OTHER,0)}
 · MULTI/UNKNOWN={counts.get(CLASS_MULTI,0)}
 · 合计核验={sum(counts.values())}</p>
<p class="warn">{esc(false_hit_note)}</p>
<h2>旧 model_match → 新 classification</h2>
<table><tr><th>transition</th><th>count</th></tr>{tr_rows}</table>
<h2>TARGET 示例</h2>{ex_table(examples.get('target', []))}
<h2>OTHER（含 B913AC/AE）示例</h2>{ex_table(examples.get('other', []))}
<h2>MULTI/UNKNOWN 示例</h2>{ex_table(examples.get('multi', []))}
</body></html>
"""


def run_reclassify(
    *,
    source_dir: Path,
    business_config: Path,
) -> dict[str, Any]:
    cfg, errs = load_business_baseline(business_config)
    if errs or cfg is None:
        raise ValueError("; ".join(errs or ["invalid config"]))
    expected_model = str(cfg.get("expected_model") or "").strip()
    business_pid = str(cfg.get("product_id") or "")
    if not expected_model:
        raise ValueError("config expected_model 为空")

    candidates, coverage = load_candidates(source_dir)
    source_run_id = str(coverage.get("run_id") or source_dir.name)
    source_collected = coverage.get("ended_at_utc") or coverage.get("started_at_utc")

    audit_id = f"{utc_stamp()}_modelcls_{secrets.token_hex(3)}"
    out_dir = AUDIT_ROOT / audit_id
    out_dir.mkdir(parents=True, exist_ok=False)

    classified: list[dict[str, Any]] = []
    for row in candidates:
        item = reclassify_row(
            row,
            expected_model=expected_model,
            source_run_id=source_run_id,
            source_collected_at=source_collected,
        )
        item["business_product_id_config"] = business_pid
        # Keep JD id separate from business model
        item["jd_product_id"] = str(row.get("jd_product_id") or row.get("product_id") or "")
        item["business_model"] = expected_model
        classified.append(item)

    ids = [c["jd_product_id"] for c in classified]
    if len(ids) != len(set(ids)):
        raise RuntimeError("输入候选存在重复 jd_product_id")
    if len(classified) != len(candidates):
        raise RuntimeError("分类条数与输入不一致")

    counts = Counter(c["classification"] for c in classified)
    if sum(counts.values()) != len(classified):
        raise RuntimeError("三类计数之和不等于总数")

    target = [c for c in classified if c["classification"] == CLASS_TARGET]
    other = [c for c in classified if c["classification"] == CLASS_OTHER]
    multi = [c for c in classified if c["classification"] == CLASS_MULTI]

    # Transition matrix
    tr = Counter(c["transition"] for c in classified)
    transitions = sorted(tr.items(), key=lambda x: (-x[1], x[0]))

    # Assess whether old "精确命中" included AC/AE substring false positives
    old_hits = [c for c in classified if c.get("old_model_match") == "精确命中"]
    old_hit_now_other = [
        c
        for c in old_hits
        if c["classification"] == CLASS_OTHER
    ]
    old_near_ac = [
        c
        for c in classified
        if c.get("old_model_match") == "明确近型号"
        and any(
            (t.get("normalized") or "").startswith("b913ac")
            or (t.get("normalized") or "").startswith("b913ae")
            for t in (c.get("observed_model_tokens") or [])
        )
    ]
    false_hit_note = (
        f"旧「精确命中」{len(old_hits)} 条中，重分类为 OTHER_MODEL 的有 "
        f"{len(old_hit_now_other)} 条。"
        f"旧「明确近型号」且含 B913AC/AE 类 token 约 {len(old_near_ac)} 条"
        "（旧逻辑已用边界冲突排除进精确命中，但仍未拆成 OTHER/MULTI 业务类）。"
        "不得把旧精确命中直接当作红线可比。"
    )

    fields = [
        "jd_product_id",
        "business_product_id_config",
        "business_model",
        "title",
        "source_page",
        "normalized_url",
        "expected_model",
        "observed_model_tokens",
        "classification",
        "reason",
        "rule_version",
        "detail_verification_required",
        "red_line_applicable",
        "red_line_note",
        "old_model_match",
        "old_model_tag",
        "transition",
        "conflict_token",
        "other_models_normalized",
        "source_run_id",
        "source_collected_at_utc",
        "title_source",
        "result_region",
        "ad_status",
    ]
    write_json(out_dir / "all_classified.json", classified)
    write_csv(out_dir / "all_classified.csv", classified, fields)
    write_csv(out_dir / "target_candidates.csv", target, fields)
    write_csv(out_dir / "other_models.csv", other, fields)
    write_csv(out_dir / "review_required.csv", multi, fields)
    write_json(out_dir / "target_candidates.json", target)
    write_json(out_dir / "other_models.json", other)
    write_json(out_dir / "review_required.json", multi)

    # Examples for report: prefer AC/AE in OTHER
    def pick_other_examples(n: int = 8) -> list[dict[str, Any]]:
        ac = [
            c
            for c in other
            if any(
                (t.get("normalized") or "").startswith("b913ac")
                for t in (c.get("observed_model_tokens") or [])
            )
        ]
        ae = [
            c
            for c in other
            if any(
                (t.get("normalized") or "").startswith("b913ae")
                for t in (c.get("observed_model_tokens") or [])
            )
        ]
        rest = [c for c in other if c not in ac and c not in ae]
        out = ac[:4] + ae[:2] + rest[:2]
        return out[:n]

    examples = {
        "target": target[:6],
        "other": pick_other_examples(),
        "multi": multi[:6],
    }
    summary = {
        "audit_id": audit_id,
        "rules_version": RULES_VERSION,
        "source_run_id": source_run_id,
        "source_dir": str(source_dir.resolve()),
        "source_collected_at_utc": source_collected,
        "business_config": str(business_config.resolve()),
        "expected_model": expected_model,
        "business_product_id_config": business_pid,
        "input_unique_count": len(candidates),
        "output_unique_count": len(classified),
        "counts": dict(counts),
        "counts_sum_ok": sum(counts.values()) == len(classified),
        "transitions": [{"from_to": k, "count": v} for k, v in transitions],
        "old_exact_hit_count": len(old_hits),
        "old_exact_hit_now_other": len(old_hit_now_other),
        "false_hit_note": false_hit_note,
        "statement": "标题筛选通过≠详情型号/规格核验通过；红线不可用直至详情确认",
        "created_at_utc": utc_iso(),
    }
    write_json(out_dir / "summary.json", summary)
    (out_dir / "report.html").write_text(
        render_report(
            audit_id=audit_id,
            source_run_id=source_run_id,
            expected_model=expected_model,
            counts=dict(counts),
            transitions=transitions,
            examples=examples,
            false_hit_note=false_hit_note,
            total=len(classified),
        ),
        encoding="utf-8",
    )
    print(f"audit_id={audit_id}")
    print(f"out_dir={out_dir}")
    print(f"counts={dict(counts)} total={len(classified)}")
    print(f"report={out_dir / 'report.html'}")
    return {"audit_id": audit_id, "out_dir": str(out_dir), "summary": summary}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="P5-H offline model reclassification")
    p.add_argument("--self-check", action="store_true")
    p.add_argument(
        "--source-dir",
        type=Path,
        default=SOURCE_DEFAULT,
        help="Existing collect_all_runs/<run_id> directory",
    )
    p.add_argument(
        "--business-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "products" / "b913a.json",
    )
    args = p.parse_args(argv)
    if args.self_check:
        fails = run_offline_model_match_checks()
        # Also adapt legacy checks still valuable
        from priceradar_candidate_adapt import run_local_adapt_checks

        fails.extend(run_local_adapt_checks())
        print("=== P5-H / model-match offline self-check ===")
        if fails:
            for f in fails:
                print(" -", f)
            return 1
        print("OK")
        return 0
    try:
        run_reclassify(
            source_dir=Path(args.source_dir),
            business_config=Path(args.business_config),
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
