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
    p.add_argument(
        "--model-scope-policy",
        type=Path,
        default=None,
        help="Optional auditable model-scope policy JSON (SKU-scoped OTHER auth)",
    )
    p.add_argument(
        "--backend",
        choices=["p2d", "bsk"],
        default="p2d",
        help="Capture backend: p2d (default) or bsk (BrowserSkill). No auto-fallback.",
    )
    p.add_argument(
        "--bsk-browser",
        default=None,
        help="Optional BrowserSkill browser instance id/label",
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

    # ---- OBSERVATION_FAILED diagnostics: capture -> final result (synthetic) ----
    import tempfile
    import types

    real_bsk = load_sibling("p5_bsk_detail")
    obs_page = {
        "page_kind": "OBSERVATION_FAILED",
        "code": "permission_denied",
        "message": "The strict observation changed before this action",
        "reason": None,
        "data": {"reason": "observation_changed", "phase": "observation"},
        "settle_attempts": 8,
        "observation_resync_attempted": True,
        "observation_resync_sync_ok": True,
        "observation_resync_recovered": False,
    }
    ident_obs = real_bsk.identity_from_page(obs_page, candidate_product_id="999001")
    oe = ident_obs.get("observation_error") or {}
    if ident_obs.get("reason") != "OBSERVATION_FAILED" or oe.get("stage") != "PAGE_CLASSIFY":
        failures.append(f"observation_error must carry stage: {ident_obs}")
    if oe.get("reason") != "observation_changed" or (oe.get("same_page_resync") or {}).get("recovered") is not False:
        failures.append(f"observation_error must carry reason and resync outcome: {oe}")

    cfg_obs = PROJECT_ROOT / "config" / "products" / "b913a.json"
    if cfg_obs.is_file():
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            cand_path = td_p / "candidates.json"
            cand_path.write_text(
                json.dumps(
                    [{
                        "query": "清风 B913A", "rank": 1, "title": "清风商用擦手纸 B913A 整箱",
                        "product_id": "999001", "shop_name": "店", "ad_marked": False,
                        "normalized_url": "https://item.jd.com/999001.html",
                        "observed_url": "https://item.jd.com/999001.html",
                    }],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            def _fake_capture(**kw):
                out = Path(kw["out_dir"])
                out.mkdir(parents=True, exist_ok=True)
                (out / "metadata.json").write_text("{}", encoding="utf-8")
                (out / "page_classify.json").write_text("{}", encoding="utf-8")
                kind = fake_state["kind"]
                base = {
                    "out_dir": str(out), "run_id": "capture", "backend": "bsk",
                    "browser_started": True, "regions": {}, "actual_product_url": "",
                    "product_identity_ok": False, "page_classify": obs_page,
                }
                if kind == "obs":
                    base.update(
                        navigation_status="OBSERVATION_FAILED", capture_status="FAILED",
                        error="OBSERVATION_FAILED", identity=ident_obs,
                        observation_failure={
                            **oe,
                            "diagnostic_files": {
                                "metadata": str(out / "metadata.json"),
                                "page_classify": str(out / "page_classify.json"),
                            },
                            "navigation_retried": False,
                        },
                    )
                else:  # rate limit with an observation error also present
                    base.update(
                        navigation_status="DETAIL_RATE_LIMITED", capture_status="FAILED",
                        error="DETAIL_RATE_LIMITED", session_block="DETAIL_RATE_LIMITED",
                        identity=ident_obs,
                    )
                return base

            fake_state = {"kind": "obs"}
            fake_mod = types.SimpleNamespace(run_bsk_detail_capture=_fake_capture)
            _orig_ls = globals()["load_sibling"]
            globals()["load_sibling"] = lambda stem: fake_mod if stem == "p5_bsk_detail" else _orig_ls(stem)
            try:
                r_obs = verify_one(
                    business_config=cfg_obs, candidates_path=cand_path, product_id="999001",
                    out_dir=td_p / "v_obs", backend="bsk",
                )
                fake_state["kind"] = "rate"
                r_rate = verify_one(
                    business_config=cfg_obs, candidates_path=cand_path, product_id="999001",
                    out_dir=td_p / "v_rate", backend="bsk",
                )
            finally:
                globals()["load_sibling"] = _orig_ls
            if r_obs.get("match_status") != "OBSERVATION_FAILED" or r_obs.get("task_status") != "FAILED":
                failures.append(f"observation failure must stay OBSERVATION_FAILED: {r_obs.get('match_status')}")
            if r_obs.get("failure_stage") != "PAGE_CLASSIFY":
                failures.append("failure_stage must reach final result")
            rr = " ".join(r_obs.get("review_reasons") or [])
            if "PAGE_CLASSIFY" not in rr or "metadata.json" not in rr or "未增加导航" not in rr:
                failures.append(f"review_reasons must show stage and diagnostic path: {rr}")
            if r_rate.get("task_status") != "PAUSED_SESSION" or r_rate.get("match_status") != "DETAIL_RATE_LIMITED":
                failures.append(
                    f"rate limit must still win over observation error: {r_rate.get('task_status')}/{r_rate.get('match_status')}"
                )

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
    ocr_image_texts: dict[str, list[str]] = {}
    ocr_root = pipeline_dir / "ocr"
    if not ocr_root.is_dir():
        return {
            "price_candidates": price_candidates,
            "model_candidates": model_candidates,
            "specification_candidates": specification_candidates,
            "promotion_text": promotion_text,
            "ocr_image_texts": ocr_image_texts,
        }
    for result_path in sorted(ocr_root.glob("*/ocr_result.json")):
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        image_key = result_path.parent.name
        texts: list[str] = []
        for b in payload.get("blocks") or []:
            if isinstance(b, dict):
                t = str(b.get("text") or b.get("source_text") or "").strip()
                if t:
                    texts.append(t)
        if texts:
            ocr_image_texts[image_key] = texts
            # Also key by common filenames used in evidence_source.
            ocr_image_texts[f"{image_key}.png"] = texts
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
        "ocr_image_texts": ocr_image_texts,
    }


def observation_failure_summary(obs: dict[str, Any] | None) -> str:
    """One-line sanitized summary for reports (no raw command lines / ids)."""
    o = obs or {}
    rs = o.get("same_page_resync") or {}
    if rs.get("attempted"):
        resync = f"同页重新同步已做(sync_ok={rs.get('sync_ok')}, 恢复={rs.get('recovered')})"
    else:
        resync = "未做同页重新同步"
    files = o.get("diagnostic_files") or {}
    return (
        f"页面观察失败(非身份不符、非会话暂停): 阶段={o.get('stage') or '未知'}; "
        f"code={o.get('code')}; reason={o.get('reason')}; 观察重试={o.get('settle_attempts')}次; "
        f"{resync}; 未增加导航; 诊断文件={files.get('metadata') or files.get('page_classify') or '无'}"
    )


def load_capture_panel_contexts(capture_dir: Path) -> list[dict[str, Any]]:
    """Non-OCR price panel text from capture metadata (DOM discover / selection)."""
    contexts: list[dict[str, Any]] = []
    capture_dir = Path(capture_dir).resolve()
    product_id = None
    captured_at = None
    selected_spec = None
    purchase_qty = None
    meta_path = capture_dir / "metadata.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            product_id = meta.get("target_product_id") or meta.get("product_id")
            captured_at = meta.get("ended_at_utc") or meta.get("started_at_utc")
        except (OSError, json.JSONDecodeError):
            pass
    for name in ("selection_state_after.json", "selection_state_before.json"):
        p = capture_dir / name
        if not p.is_file():
            continue
        try:
            st = json.loads(p.read_text(encoding="utf-8-sig"))
            if isinstance(st, dict):
                product_id = product_id or st.get("url_product_id")
                selected_spec = selected_spec or st.get("selected_spec_text")
                if purchase_qty is None:
                    purchase_qty = st.get("purchase_quantity")
        except (OSError, json.JSONDecodeError):
            pass

    def add(text: str | None, source: str) -> None:
        t = (text or "").strip()
        if t and not any(c.get("text") == t for c in contexts):
            contexts.append(
                {
                    "text": t,
                    "source": source,
                    "evidence_kind": "DOM_PANEL_TEXT",
                    "product_id": product_id,
                    "captured_at_utc": captured_at,
                    "selected_spec_text": selected_spec,
                    "purchase_quantity": purchase_qty,
                    "note": "DOM/capture 文本；不得描述为图片 OCR 识别",
                }
            )

    rd = capture_dir / "region_discover.json"
    if rd.is_file():
        try:
            data = json.loads(rd.read_text(encoding="utf-8-sig"))
            if isinstance(data.get("price"), dict):
                add(data["price"].get("text"), "capture:region_discover.price")
        except (OSError, json.JSONDecodeError):
            pass
    for name in ("selection_state_before.json", "selection_state_after.json"):
        p = capture_dir / name
        if not p.is_file():
            continue
        try:
            st = json.loads(p.read_text(encoding="utf-8-sig"))
            add(st.get("price_panel_preview"), f"capture:{name}")
        except (OSError, json.JSONDecodeError):
            pass
    pc = capture_dir / "page_classify.json"
    if pc.is_file():
        try:
            pg = json.loads(pc.read_text(encoding="utf-8-sig"))
            add(pg.get("price_preview"), "capture:page_classify.price_preview")
        except (OSError, json.JSONDecodeError):
            pass
    # Same-capture DOM price-role evidence (amount/label nodes + line-through).
    role_files: dict[str, Any] = {}
    for key, name in (
        ("before", "price_role_evidence_before.json"),
        ("after", "price_role_evidence_after.json"),
    ):
        rp = capture_dir / name
        if rp.is_file():
            try:
                role_files[key] = json.loads(rp.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                role_files[key] = {"status": "READ_ERROR"}
    if role_files:
        if not contexts:
            ev_any = role_files.get("after") or role_files.get("before") or {}
            add(ev_any.get("panel_text"), "capture:price_role_evidence.panel_text")
        if contexts:
            contexts[0]["role_evidence"] = {
                "before": role_files.get("before"),
                "after": role_files.get("after"),
                "target_product_id": product_id,
                "selected_spec_text": selected_spec,
            }
    return contexts


def detect_session_block_from_capture(capture_result: dict[str, Any]) -> str | None:
    """Return pause reason if login/captcha/rate-limit; identity mismatch handled separately.

    Session restrictions must win over ordinary product-id mismatch labeling.
    """
    explicit = capture_result.get("session_block")
    if explicit:
        return str(explicit)
    status = str(capture_result.get("navigation_status") or "")
    err = str(capture_result.get("error") or "")
    final_u = str(
        capture_result.get("actual_product_url")
        or capture_result.get("final_url")
        or ""
    )
    final_l = final_u.lower()
    page = capture_result.get("page_classify") or {}
    if isinstance(page, dict):
        page_block = page.get("session_block")
        if page_block:
            return str(page_block)
        kind = str(page.get("page_kind") or "")
        if kind in {"SESSION_LIMIT", "ACCESS_RESTRICTED"}:
            return "DETAIL_RATE_LIMITED"
        if kind == "LOGIN":
            return "LOGIN_REQUIRED"
        if kind == "VERIFICATION":
            return "VERIFICATION_REQUIRED"
    blob = f"{status} {err} {final_u}".lower()
    if any(x in blob for x in ("passport", "plogin")):
        return "LOGIN_REQUIRED"
    if "login" in blob and "item." not in final_l:
        return "LOGIN_REQUIRED"
    if any(x in err for x in ("验证码", "安全验证", "滑动验证", "DETAIL_RATE_LIMITED")):
        if "DETAIL_RATE_LIMITED" in err:
            return "DETAIL_RATE_LIMITED"
        if any(x in err for x in ("验证码", "安全验证", "滑动验证")):
            return "VERIFICATION_REQUIRED"
    # JD detail rate-limit / challenge sink (not a product identity miss).
    # Prefer DETAIL_RATE_LIMITED over ordinary WRONG_PAGE / identity mismatch.
    if (
        "limit.html" in final_l
        or "/common/limit" in final_l
        or "pc-frequent-pro.pf.jd.com" in final_l
        or ("reason=403" in final_l and "item." not in final_l)
    ):
        return "DETAIL_RATE_LIMITED"
    if isinstance(page, dict):
        page_href = str(page.get("href") or "").lower()
        page_title = str(page.get("title") or "")
        if (
            "pc-frequent-pro.pf.jd.com" in page_href
            or ("reason=403" in page_href and "item." not in page_href)
            or "频控" in page_title
        ):
            return "DETAIL_RATE_LIMITED"
    if "访问受限" in err or "频繁访问" in err or "频控" in err:
        return "DETAIL_RATE_LIMITED"
    if status == "WRONG_PAGE_OR_CHALLENGE" and "item." not in final_l:
        return "WRONG_PAGE_OR_CHALLENGE"
    if status in {"LOGIN_REQUIRED", "DETAIL_RATE_LIMITED", "VERIFICATION_REQUIRED"}:
        return status
    return None


def model_scope_allows_title_browse(
    model_scope_policy: dict[str, Any] | None,
    *,
    gate: dict[str, Any],
    business_sku: str,
) -> dict[str, Any]:
    """
    Explicit authorization may waive title MODEL_MISMATCH skip for included
    classifications only. Never waives identity / packaging / unit gates.
    """
    out = {
        "allowed": False,
        "reason": "no_policy",
        "policy_id": None,
        "classification": gate.get("classification"),
    }
    if not model_scope_policy:
        return out
    out["policy_id"] = model_scope_policy.get("policy_id")
    if str(model_scope_policy.get("model_scope_policy") or "") != "user_approved_include_other":
        out["reason"] = "policy_not_user_approved_include_other"
        return out
    policy_sku = str(
        model_scope_policy.get("business_sku")
        or model_scope_policy.get("expected_model")
        or ""
    ).upper()
    if not policy_sku or policy_sku != str(business_sku or "").upper():
        out["reason"] = "policy_sku_mismatch"
        return out
    include = list(model_scope_policy.get("include_classifications") or [])
    cls = str(gate.get("classification") or "")
    if cls not in include:
        out["reason"] = f"classification_not_included:{cls or 'EMPTY'}"
        return out
    exclude = list(model_scope_policy.get("exclude_classifications") or [])
    if cls in exclude:
        out["reason"] = f"classification_excluded:{cls}"
        return out
    out["allowed"] = True
    out["reason"] = "user_approved_include_other"
    out["other_model_label_zh"] = (
        (model_scope_policy.get("authorization") or {}).get("other_model_label_zh")
    )
    return out


def verify_one(
    *,
    business_config: Path,
    candidates_path: Path,
    product_id: str,
    offline_capture_dir: Path | None = None,
    skip_title_gate: bool = False,
    out_dir: Path | None = None,
    model_scope_policy: dict[str, Any] | None = None,
    backend: str = "p2d",
    bsk_browser: str | None = None,
) -> dict[str, Any]:
    """
    Verify one candidate from an exact candidates.json path (no “latest” inference).

    out_dir: optional explicit result directory (Phase B nests under job).
    model_scope_policy: optional auditable OTHER-model authorization (SKU-scoped).
    backend: ``p2d`` (default Playwright profile) or ``bsk`` (BrowserSkill / daily Chrome).
             Never auto-fallback between backends.
    """
    started = utc_now()
    processed_at = utc_iso(started)
    pid = str(product_id).strip()
    if out_dir is not None:
        out_dir = Path(out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        run_id = out_dir.name
    else:
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
        "model_scope_policy_id": (
            model_scope_policy.get("policy_id") if model_scope_policy else None
        ),
        "notes": [
            "业务配置与候选身份分离；配置历史链接不得覆盖候选",
            "主展示价不作到手价；无到手价证据时标记到手价待核验",
            "POSSIBLE_BREAK=疑似破价待人工确认；系统不代填 manual_verdict",
            "未进入结算，不生成 VERIFIED_CHECKOUT_PAYABLE",
            "历史 BREAK/NOT_BREAK 记录不改写",
            "显式授权仅放行型号差异浏览/比较，不放行商品ID冲突或规格不明",
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
    # Prefer explicit classification from classified candidates when present.
    if cand.get("classification"):
        gate = dict(gate)
        gate["classification"] = cand.get("classification")
        gate["observed_model_tokens"] = (
            cand.get("observed_model_tokens") or gate.get("observed_model_tokens")
        )
        gate["source_classification"] = "candidate_record"
    result["title_gate"] = gate
    title_auth = model_scope_allows_title_browse(
        model_scope_policy,
        gate=gate,
        business_sku=str(cfg.get("business_sku") or cfg["expected_model"]),
    )
    result["title_gate_authorization"] = title_auth
    if gate.get("skip_browse") and not skip_title_gate and not title_auth.get("allowed"):
        result.update(
            {
                "task_status": "SKIPPED",
                "match_status": gate["status"],
                "price_evidence_status": "SKIPPED",
                "system_verdict": "MODEL_MISMATCH_SKIPPED",
                "system_verdict_zh": "型号不符已跳过（未打开详情）",
                "review_reasons": [gate.get("reason"), title_auth.get("reason")],
                "ended_at_utc": utc_iso(),
            }
        )
        _finalize(out_dir, result)
        return result
    if gate.get("skip_browse") and title_auth.get("allowed"):
        result["title_gate_note"] = (
            "标题型号与期望不符，但本次用户授权纳入业务基准比较；"
            "仍须核验商品ID、包装与单位，且不得改写实际型号"
        )

    source_url = str(
        cand.get("normalized_url") or cand.get("observed_url") or ""
    ).strip()
    backend_norm = str(backend or "p2d").strip().lower()
    if backend_norm not in {"p2d", "bsk"}:
        result.update(
            {
                "task_status": "FAILED",
                "match_status": "BACKEND_INVALID",
                "failure_reason": f"unsupported backend={backend!r}; use p2d|bsk",
                "ended_at_utc": utc_iso(),
            }
        )
        _finalize(out_dir, result)
        return result
    result["capture_backend"] = backend_norm
    # BSK desktop detail: prefer item.jd.com; p2d keeps mobile entry that was validated.
    adapted = adapt.adapt_candidate_url(
        source_url=source_url,
        candidate_product_id=pid,
        prefer_mobile=(backend_norm != "bsk"),
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
        # Honor original capture failure labels; do not OCR empty/blocked captures
        # into a fake COMPLETED packaging result.
        meta_path = capture_dir / "metadata.json"
        page_path = capture_dir / "page_classify.json"
        offline_meta: dict[str, Any] = {}
        if meta_path.is_file():
            try:
                offline_meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                offline_meta = {}
        if page_path.is_file() and "page_classify" not in offline_meta:
            try:
                offline_meta["page_classify"] = json.loads(
                    page_path.read_text(encoding="utf-8-sig")
                )
            except (OSError, json.JSONDecodeError):
                pass
        if offline_meta:
            result["capture"]["source_navigation_status"] = offline_meta.get(
                "navigation_status"
            )
            result["capture"]["source_capture_status"] = offline_meta.get(
                "capture_status"
            )
            result["capture"]["source_error"] = offline_meta.get("error")
            result["capture"]["page_classify"] = offline_meta.get("page_classify")
            result["capture"]["actual_product_url"] = offline_meta.get(
                "actual_product_url"
            )
            result["capture"]["session_block"] = offline_meta.get("session_block")
            result["capture"]["identity"] = offline_meta.get("identity")
            result["capture"]["product_identity_ok"] = offline_meta.get(
                "product_identity_ok"
            )
            # Build a capture_result-shaped dict for shared detectors.
            offline_cap = {
                "navigation_status": offline_meta.get("navigation_status"),
                "actual_product_url": offline_meta.get("actual_product_url"),
                "final_url": offline_meta.get("actual_product_url"),
                "error": offline_meta.get("error"),
                "session_block": offline_meta.get("session_block"),
                "page_classify": offline_meta.get("page_classify"),
                "product_identity_ok": offline_meta.get("product_identity_ok"),
                "identity": offline_meta.get("identity"),
                "observation_failure": offline_meta.get("observation_failure"),
            }
            paused = detect_session_block_from_capture(offline_cap)
            if paused:
                result.update(
                    {
                        "task_status": "PAUSED_SESSION",
                        "match_status": paused,
                        "price_evidence_status": "NOT_EVALUATED",
                        "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                        "system_verdict_zh": "源采集会话受限（离线保留，不伪造详情结论）",
                        "failure_reason": offline_meta.get("error") or paused,
                        "ended_at_utc": utc_iso(),
                    }
                )
                _finalize(out_dir, result)
                return result
            nav = str(offline_meta.get("navigation_status") or "")
            err = str(offline_meta.get("error") or "")
            ident_reason = str((offline_meta.get("identity") or {}).get("reason") or "")
            page = offline_meta.get("page_classify") or {}
            obs_like = (
                nav == "OBSERVATION_FAILED"
                or err == "OBSERVATION_FAILED"
                or ident_reason == "OBSERVATION_FAILED"
                or (
                    isinstance(page, dict)
                    and (
                        str(page.get("code") or "") == "permission_denied"
                        or "observation_changed" in json.dumps(page, ensure_ascii=False)
                    )
                    and not str(offline_meta.get("actual_product_url") or "").strip()
                )
            )
            if obs_like:
                result.update(
                    {
                        "task_status": "FAILED",
                        "match_status": "OBSERVATION_FAILED",
                        "price_evidence_status": "NOT_EVALUATED",
                        "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                        "system_verdict_zh": "源采集观察失败（离线无法补采页面，不得计身份不符）",
                        "failure_reason": "offline_reused_observation_failed_capture",
                        "ended_at_utc": utc_iso(),
                    }
                )
                _obs_hist = offline_meta.get("observation_failure") or {}
                result["review_reasons"] = [
                    observation_failure_summary(_obs_hist)
                    + ("" if _obs_hist.get("stage") else "（历史产物无失败阶段字段：历史原因未知）")
                ]
                result["failure_stage"] = _obs_hist.get("stage")
                _finalize(out_dir, result)
                return result
            if (
                nav == "IDENTITY_FAILED"
                or offline_meta.get("product_identity_ok") is False
            ) and str(offline_meta.get("actual_product_url") or "").strip():
                # Trusted URL present but identity failed in source — keep identity label.
                result.update(
                    {
                        "task_status": "FAILED",
                        "match_status": "IDENTITY_FAILED",
                        "price_evidence_status": "NOT_EVALUATED",
                        "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                        "system_verdict_zh": "源采集身份核验未通过（离线不重开详情）",
                        "failure_reason": offline_meta.get("error") or ident_reason,
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

        print(f"=== P5-A capture candidate {pid} backend={backend_norm} ===")
        print(f"adapted_url={adapted['adapted_url']}")
        print(f"business_model={cfg['expected_model']} (not candidate title alone)")
        try:
            if backend_norm == "bsk":
                bsk_detail = load_sibling("p5_bsk_detail")
                capture_result = bsk_detail.run_bsk_detail_capture(
                    product_id=pid,
                    product_url=adapted["adapted_url"],
                    out_dir=(out_dir / "capture") if out_dir else None,
                    browser=bsk_browser,
                    keep_session_on_block=True,
                )
            else:
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
            "backend": backend_norm,
            "out_dir": str(capture_dir),
            "run_id": capture_result.get("run_id"),
            "capture_status": capture_result.get("capture_status")
            or capture_result.get("overall_capture_status"),
            "navigation_status": capture_result.get("navigation_status"),
            "error": capture_result.get("error"),
            "final_url": capture_result.get("actual_product_url"),
            "product_identity_ok": capture_result.get("product_identity_ok"),
            "browser_started": capture_result.get("browser_started"),
            "session_block": capture_result.get("session_block"),
            "page_classify": capture_result.get("page_classify"),
            "regions": capture_result.get("regions"),
            "user_action_required": capture_result.get("user_action_required"),
        }
        # Identity on final URL
        final_u = str(capture_result.get("actual_product_url") or "")
        ident = adapt.final_url_identity_ok(final_u, pid)
        result["final_identity"] = ident
        result["url_adapt"]["final_url_sanitized"] = ident.get("final_url_sanitized")

        nav_status = str(capture_result.get("navigation_status") or "")
        if nav_status in {
            "NO_BROWSER_CONNECTED",
            "DAEMON_NOT_READY",
            "SESSION_START_FAILED",
            "NAVIGATION_FAILED",
        }:
            result.update(
                {
                    "task_status": "FAILED",
                    "match_status": nav_status,
                    "price_evidence_status": "NOT_EVALUATED",
                    "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                    "system_verdict_zh": "详情采集前置失败（连接/导航）",
                    "failure_reason": capture_result.get("error") or nav_status,
                    "user_action_required": capture_result.get("user_action_required"),
                    "ended_at_utc": utc_iso(),
                }
            )
            _finalize(out_dir, result)
            return result

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

        # observation_changed / evaluate sync failure ≠ product-id mismatch
        cap_err = str(capture_result.get("error") or "")
        ident_reason = str((capture_result.get("identity") or {}).get("reason") or "")
        if (
            nav_status == "OBSERVATION_FAILED"
            or cap_err == "OBSERVATION_FAILED"
            or ident_reason == "OBSERVATION_FAILED"
        ):
            result.update(
                {
                    "task_status": "FAILED",
                    "match_status": "OBSERVATION_FAILED",
                    "price_evidence_status": "NOT_EVALUATED",
                    "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                    "system_verdict_zh": "页面观察/采集失败，不得计为商品身份不符",
                    "failure_reason": capture_result.get("error") or "OBSERVATION_FAILED",
                    "observation_failure": capture_result.get("observation_failure")
                    or (capture_result.get("identity") or {}).get("observation_error"),
                    "ended_at_utc": utc_iso(),
                }
            )
            result["review_reasons"] = [observation_failure_summary(result["observation_failure"])]
            result["failure_stage"] = (result["observation_failure"] or {}).get("stage")
            _finalize(out_dir, result)
            return result

        if not ident.get("ok") or not capture_result.get("product_identity_ok"):
            # Refuse to label observation errors as IDENTITY_FAILED even if older captures
            # left navigation_status=IDENTITY_FAILED with empty URL.
            page = capture_result.get("page_classify") or {}
            if isinstance(page, dict) and (
                str(page.get("code") or "") == "permission_denied"
                or "observation_changed"
                in json.dumps(page, ensure_ascii=False)
            ) and not str(capture_result.get("actual_product_url") or "").strip():
                result.update(
                    {
                        "task_status": "FAILED",
                        "match_status": "OBSERVATION_FAILED",
                        "price_evidence_status": "NOT_EVALUATED",
                        "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
                        "system_verdict_zh": "页面观察/采集失败，不得计为商品身份不符",
                        "failure_reason": "legacy_identity_label_corrected_to_observation_failed",
                        "ended_at_utc": utc_iso(),
                    }
                )
                _finalize(out_dir, result)
                return result
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
    pipe = p4c.process_capture_run(
        capture_dir,
        business_override=biz_ov,
        model_scope_policy=model_scope_policy,
    )
    pipeline_dir = Path(pipe["out_dir"]).resolve()
    row = pipe.get("row") or {}
    detail = pipe.get("detail") or {}
    unit_analysis = (
        (detail.get("unit_analysis") if isinstance(detail, dict) else None)
        or {}
    )
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
    result["model_status"] = unit_analysis.get("model_status")
    result["model_match_business_config"] = unit_analysis.get(
        "model_match_business_config"
    )
    result["model_authorized_other"] = unit_analysis.get("model_authorized_other")
    result["packaging_basis_note"] = unit_analysis.get("packaging_basis_note")
    result["unit_status"] = unit_analysis.get("unit_status")
    result["current_selection"] = (
        detail.get("current_selection") if isinstance(detail, dict) else None
    )
    pm = str(row.get("packaging_match") or "")
    if pm == "PASSED":
        result["match_status"] = "PACKAGING_PASSED"
    elif pm == "SELECTED_SPEC_UNKNOWN":
        result["match_status"] = "SELECTED_SPEC_UNKNOWN"
    elif pm == "OBSERVED_SPEC_MISMATCH":
        result["match_status"] = "OBSERVED_SPEC_MISMATCH"
    else:
        result["match_status"] = "PACKAGING_FAILED_OR_UNCLEAR"

    extraction = load_pipeline_extractions(pipeline_dir)
    result["ocr_aggregate_counts"] = {
        "price_candidates": len(extraction.get("price_candidates") or []),
        "model_candidates": len(extraction.get("model_candidates") or []),
        "promotion_text": len(extraction.get("promotion_text") or []),
    }

    red = cfg["_red_line_decimal"]
    box_ok = bool(row.get("box_comparable"))
    page_matched = bool(unit_analysis.get("page_spec_matched"))
    if unit_analysis.get("page_spec_matched") is None:
        page_matched = row.get("packaging_match") == "PASSED"
    role_status: dict[str, Any] = {}
    clues = take_home.build_price_clues_from_extraction(
        extraction,
        red_line=red,
        box_comparable=box_ok,
        page_spec_matched=page_matched,
        packs_per_box=cfg["_packs_per_box_int"],
        evidence_path=str(pipeline_dir),
        panel_contexts=load_capture_panel_contexts(capture_dir),
        role_status_out=role_status,
    )
    result["price_role_evidence"] = role_status or {"status": "NOT_AVAILABLE"}
    rollup = take_home.aggregate_system_verdict(clues)
    result["price_clues"] = clues
    result["take_home_rollup"] = rollup
    result["system_verdict"] = rollup["system_verdict"]
    result["system_verdict_zh"] = rollup["system_verdict_zh"]
    result["manual_verdict"] = None
    if rollup.get("take_home_clue_count", 0) > 0:
        result["price_evidence_status"] = "TAKE_HOME_CLUES_PRESENT"
    elif int(rollup.get("conditional_candidate_count") or 0) > 0:
        result["price_evidence_status"] = "TAKE_HOME_ASSOCIATION_PENDING"
    else:
        result["price_evidence_status"] = "TAKE_HOME_PENDING_NO_DECLARED_CLUE"
    if rollup.get("take_home_clue_count", 0) == 0 and result.get(
        "system_verdict"
    ) == take_home.SYSTEM_REVIEW_REQUIRED:
        if int(rollup.get("conditional_candidate_count") or 0) > 0:
            result["system_verdict_zh"] = "条件到手价关联待核验"
        else:
            result["system_verdict_zh"] = "到手价待核验"
    result["task_status"] = "COMPLETED"
    # Keep primary-below-red clue even when packaging unknown (not a comparable box).
    primary_for_signal = result.get("page_primary_price")
    if primary_for_signal is None:
        for c in clues:
            if c.get("price_type") != take_home.PRICE_TYPE_PAGE_PRIMARY:
                continue
            amt = take_home.to_decimal(c.get("amount"))
            if amt is not None and amt < red:
                primary_for_signal = c.get("amount")
                break
    low_signal = take_home.derive_product_low_price_signal(
        primary_display_price=primary_for_signal,
        red_line=red,
        packaging_match=result.get("packaging_match"),
        box_comparable=box_ok,
        match_status=result.get("match_status"),
        task_status=result.get("task_status"),
        system_verdict=result.get("system_verdict"),
    )
    result["low_price_signal"] = bool(low_signal.get("low_price_signal"))
    result["low_price_signal_detail"] = low_signal
    result["review_reasons"] = list(
        dict.fromkeys(
            [str(row.get("review_reasons") or "")]
            + [r for c in clues for r in (c.get("review_reasons") or [])]
            + [rollup.get("note") or ""]
            + list(low_signal.get("signal_review_reasons") or [])
            + [
                "价格角色证据(" + str(role_status.get("status")) + "): " + str(x)
                for x in (role_status.get("reasons") or [])
                if role_status.get("status") not in {"RESOLVED", "NOT_AVAILABLE"}
            ]
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

    policy = None
    if args.model_scope_policy is not None:
        policy = json.loads(
            Path(args.model_scope_policy).read_text(encoding="utf-8-sig")
        )
        if not isinstance(policy, dict):
            print("参数错误: --model-scope-policy 必须是 JSON 对象")
            return 2

    result = verify_one(
        business_config=args.business_config,
        candidates_path=args.candidates,
        product_id=args.product_id,
        offline_capture_dir=args.offline_capture_dir,
        skip_title_gate=args.skip_title_gate,
        model_scope_policy=policy,
        backend=args.backend,
        bsk_browser=args.bsk_browser,
    )
    status = result.get("task_status")
    if status in {"COMPLETED", "SKIPPED"}:
        return 0
    if status == "PAUSED_SESSION":
        return 3
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
