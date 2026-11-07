"""
P5-E: human-assisted take-home price evidence diagnosis (single product).

- Opens one explicit product URL / candidate ID with business config.
- Keeps the browser window; user points to 到手价 or opens 优惠说明 manually.
- On confirm, saves local price/promo crops + sanitized diagnosis.
- Does NOT claim coupons, add-to-cart, checkout, or place orders.
- Does NOT count as full-auto pipeline pass.
- Does NOT read/write account, address, Cookie, Token, or full-page HTML.
- Default CLI does not start a new search; browser opens only when user runs this entry.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import json
import re
import secrets
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
PROFILE_DIR = (PROJECT_ROOT / "browser_profiles" / "jd_persistent").resolve()
DIAG_ROOT = (PROJECT_ROOT / "artifacts" / "take_home_diagnose").resolve()

RULES_VERSION = "p5e-take-home-diagnose-v1"
MODE = "HUMAN_ASSISTED_DIAGNOSE"
SENSITIVE_KEY_RE = re.compile(
    r"(cookie|token|authorization|password|passwd|secret|address|phone|"
    r"mobile|身份证|收货|完整html|innerHTML|outerHTML)",
    re.IGNORECASE,
)
PRODUCT_ID_RE = re.compile(r"/product/(\d+)", re.IGNORECASE)


def load_sibling(stem: str) -> Any:
    path = SCRIPTS_DIR / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(f"priceradar_{stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


p2d = load_sibling("p2d_jd_auto_capture")
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
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def sanitize_diag_value(value: Any, *, depth: int = 0) -> Any:
    """Drop sensitive keys/values; truncate long text; never keep full HTML."""
    if depth > 8:
        return "[truncated_depth]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            ks = str(k)
            if SENSITIVE_KEY_RE.search(ks):
                out[ks] = "[redacted]"
                continue
            out[ks] = sanitize_diag_value(v, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [sanitize_diag_value(v, depth=depth + 1) for v in value[:40]]
    if isinstance(value, str):
        if SENSITIVE_KEY_RE.search(value) and (
            "cookie" in value.lower() or "token" in value.lower() or len(value) > 80
        ):
            return "[redacted_text]"
        if "<html" in value.lower() or "<!doctype" in value.lower():
            return "[redacted_html]"
        if len(value) > 240:
            return value[:240] + "…[truncated]"
        return value
    return value


def product_id_from_url(url: str) -> str | None:
    m = PRODUCT_ID_RE.search(url or "")
    return m.group(1) if m else None


def resolve_target(
    *,
    business_config: Path,
    url: str | None,
    product_id: str | None,
) -> dict[str, Any]:
    cfg, errs = adapt.load_business_baseline(business_config)
    if errs or cfg is None:
        raise ValueError("; ".join(errs or ["invalid business config"]))
    pid = (product_id or "").strip() or None
    use_url = (url or "").strip() or None
    if not pid and use_url:
        pid = product_id_from_url(use_url)
    if not use_url and pid:
        use_url = f"https://item.m.jd.com/product/{pid}.html"
    if not use_url:
        # Prefer explicit config url only when user did not pass product_id/url
        # that conflicts — still require user-facing clarity.
        hist = (cfg.get("historical_anchors") or {}).get("historical_product_id")
        cfg_pid = str(cfg.get("product_id") or hist or "").strip() or None
        cfg_url = str(cfg.get("url") or "").strip() or None
        if cfg_url:
            use_url = cfg_url
            pid = pid or product_id_from_url(cfg_url) or cfg_pid
        elif cfg_pid:
            pid = cfg_pid
            use_url = f"https://item.m.jd.com/product/{pid}.html"
    if not use_url or not pid:
        raise ValueError(
            "需要 --url 或 --product-id（或业务配置中的 url/product_id）"
        )
    return {
        "business_config": cfg,
        "business_sku": cfg.get("business_sku"),
        "expected_model": cfg.get("expected_model"),
        "target_spec": cfg.get("target_spec"),
        "red_line": cfg.get("red_line"),
        "currency": cfg.get("currency"),
        "product_id": str(pid),
        "url": use_url,
    }


def snapshot_page_identity(page) -> dict[str, Any]:
    safe_url = p2d.url_without_query_or_fragment(page.url)
    pid = product_id_from_url(safe_url)
    try:
        snap = p2d.snapshot_selection_state(page)
    except Exception as exc:  # noqa: BLE001
        snap = {"error": f"{type(exc).__name__}: {exc}"}
    # Lightweight title/promo samples (truncated, no HTML)
    samples = page.evaluate(
        """() => {
            const pick = (sel) => {
                const n = document.querySelector(sel);
                return n ? (n.innerText || '').trim().slice(0, 120) : null;
            };
            const body = (document.body && document.body.innerText || '').slice(0, 800);
            const hasTakeHome = /到手|券后|预估到手|会员价/.test(body);
            const hasPromo = /优惠|满减|满送|促销/.test(body);
            return {
                title_sample: pick('#itemName') || pick('h1') || pick('.sku-name'),
                has_take_home_hint_in_visible_text: hasTakeHome,
                has_promo_hint_in_visible_text: hasPromo,
            };
        }"""
    )
    return sanitize_diag_value(
        {
            "captured_at_utc": utc_iso(),
            "url_safe": safe_url,
            "product_id_from_url": pid or (snap.get("product_id") if isinstance(snap, dict) else None),
            "core_selected_spec": (snap or {}).get("core_selected_spec")
            if isinstance(snap, dict)
            else None,
            "purchase_quantity": (snap or {}).get("purchase_qty")
            if isinstance(snap, dict)
            else None,
            "selected_spec_locator": {
                "status": (snap or {}).get("locator_status")
                if isinstance(snap, dict)
                else None,
            },
            "visible_text_hints": samples,
        }
    )


def assert_identity_compatible(
    before: dict[str, Any], after: dict[str, Any]
) -> tuple[bool, list[str]]:
    """Refuse splicing pre-navigation identity with post-change prices."""
    reasons: list[str] = []
    b_pid = before.get("product_id_from_url")
    a_pid = after.get("product_id_from_url")
    if b_pid and a_pid and str(b_pid) != str(a_pid):
        reasons.append(
            f"商品ID变化：{b_pid} → {a_pid}；拒绝将导航前身份与导航后价格关联"
        )
    b_spec = before.get("core_selected_spec")
    a_spec = after.get("core_selected_spec")
    if b_spec and a_spec and str(b_spec) != str(a_spec):
        reasons.append(
            f"已选规格变化：{b_spec!r} → {a_spec!r}；须重新核验身份，不能拼接"
        )
    b_qty = before.get("purchase_quantity")
    a_qty = after.get("purchase_quantity")
    if b_qty is not None and a_qty is not None and int(b_qty) != int(a_qty):
        reasons.append(
            f"购买数量变化：{b_qty} → {a_qty}；价格条件可能不同，拒绝直接拼接"
        )
    return (len(reasons) == 0), reasons


def capture_visible_promo_panel(page, out_path: Path) -> dict[str, Any]:
    """Capture a visible promo/take-home panel if already open (no auto-claim)."""
    info: dict[str, Any] = {
        "region": "promo_or_take_home_panel",
        "filename": out_path.name,
        "status": "NOT_FOUND",
        "capture_method": "human_opened_panel_clip",
        "notes": [],
    }
    panel = page.evaluate(
        """() => {
            const nodes = Array.from(document.querySelectorAll('div, section, aside'));
            const scored = [];
            for (const n of nodes) {
                const st = window.getComputedStyle(n);
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                const r = n.getBoundingClientRect();
                if (r.width < 180 || r.height < 80) continue;
                if (r.width > innerWidth * 0.98 && r.height > innerHeight * 0.92) continue;
                const t = (n.innerText || '').slice(0, 400);
                if (!t) continue;
                let score = 0;
                if (/到手|券后|预估到手|预估价/.test(t)) score += 4;
                if (/优惠|满减|满送|促销|券/.test(t)) score += 2;
                if (/收货地址|微信支付|立即购买|加入购物车|结算/.test(t)) score -= 6;
                if (score <= 0) continue;
                scored.push({
                    score,
                    x: r.left, y: r.top, width: r.width, height: r.height,
                    sample: t.slice(0, 160),
                });
            }
            scored.sort((a, b) => b.score - a.score);
            return scored[0] || null;
        }"""
    )
    if not panel:
        info["notes"].append("未发现已打开的优惠/到手价面板；请先手动打开后再 capture")
        return info
    clip = {
        "x": max(0, float(panel["x"]) - 4),
        "y": max(0, float(panel["y"]) - 4),
        "width": min(float(panel["width"]) + 8, page.viewport_size["width"]),
        "height": min(float(panel["height"]) + 8, page.viewport_size["height"]),
    }
    try:
        page.screenshot(path=str(out_path), clip=clip)
        info["status"] = "CAPTURED"
        info["save_status"] = "SAVED"
        info["path"] = str(out_path.resolve())
        info["panel_sample_sanitized"] = sanitize_diag_value(panel.get("sample"))
        info["clip_box_css_viewport"] = clip
        info["captured_at_utc"] = utc_iso()
    except PlaywrightError as exc:
        info["status"] = "FAILED"
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def build_price_scheme_stub(
    *,
    product_id: str,
    expected_model: str | None,
    identity: dict[str, Any],
    capture_meta: dict[str, Any],
    user_notes: str | None,
) -> dict[str, Any]:
    """
    Evidence association record. Amounts stay null until OCR/human labels them;
    never invent take-home from primary display.
    """
    return {
        "scheme_id": f"diag_{secrets.token_hex(3)}",
        "jd_product_id": product_id,
        "expected_model": expected_model,
        "observed_selected_spec": identity.get("core_selected_spec"),
        "purchase_quantity": identity.get("purchase_quantity"),
        "price_raw_label": None,
        "promo_conditions": None,
        "amount": None,
        "amount_kind": None,  # page_declared | estimated | rule_calculated | payable | primary_ref
        "captured_at_utc": capture_meta.get("captured_at_utc") or utc_iso(),
        "source_page_url_safe": identity.get("url_safe"),
        "acquisition_mode": MODE,
        "auto_or_human": "human_assisted",
        "screenshots": capture_meta.get("screenshots") or [],
        "user_notes_sanitized": sanitize_diag_value(user_notes or ""),
        "system_verdict": take_home.SYSTEM_REVIEW_REQUIRED,
        "manual_verdict": None,
        "notes": [
            "人工诊断模式；不算全自动流程通过",
            "主展示价仅参考；无到手价证据时保持待核验",
            "页面声明 / 预估 / 规则计算 / 实际应付须分别记录（本记录金额待补）",
        ],
    }


def write_report_html(out_dir: Path, bundle: dict[str, Any]) -> Path:
    esc = html.escape
    path = out_dir / "report.html"
    shots = bundle.get("captures") or []
    img_links = []
    for c in shots:
        for s in c.get("screenshots") or []:
            rel = s.get("filename")
            if rel and (out_dir / rel).is_file():
                img_links.append(
                    f"<li>{esc(str(s.get('region')))}: "
                    f"<a href='{esc(rel)}'>{esc(rel)}</a> "
                    f"status={esc(str(s.get('status')))}</li>"
                )
    path.write_text(
        f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<title>到手价证据诊断 {esc(str(bundle.get('run_id')))}</title>
<style>
body{{font-family:"Segoe UI","Microsoft YaHei",sans-serif;margin:24px;color:#222}}
.card{{border:1px solid #ddd;padding:12px 16px;margin:12px 0;border-radius:6px}}
.warn{{color:#a40}} .muted{{color:#666}}
</style></head><body>
<h1>到手价证据人工诊断</h1>
<p class="muted">run_id={esc(str(bundle.get('run_id')))} · mode={esc(MODE)} ·
<strong>不算全自动通过</strong></p>
<div class="card">
<h2>目标</h2>
<ul>
<li>商品ID：{esc(str(bundle.get('product_id')))}</li>
<li>业务SKU：{esc(str(bundle.get('business_sku')))}</li>
<li>期望型号：{esc(str(bundle.get('expected_model')))}</li>
<li>红线：{esc(str(bundle.get('red_line')))} {esc(str(bundle.get('currency') or ''))}</li>
<li>链接（脱敏路径）：{esc(str(bundle.get('url_safe')))}</li>
</ul>
</div>
<div class="card">
<h2>已有证据缺口结论（启动时）</h2>
<pre>{esc(json.dumps(bundle.get('prior_gap_summary') or {{}}, ensure_ascii=False, indent=2))}</pre>
</div>
<div class="card">
<h2>本次采集</h2>
<ul>{''.join(img_links) or '<li>（尚无截图）</li>'}</ul>
<p>身份拒绝关联次数：{esc(str(bundle.get('identity_reject_count', 0)))}</p>
</div>
<p class="warn">禁止领券/加购/结算/下单；不输出 Cookie/Token/地址/整页HTML。</p>
</body></html>
""",
        encoding="utf-8",
    )
    return path


def prior_gap_summary_from_artifacts() -> dict[str, Any]:
    """Evidence-backed gap summary from existing real runs (no new guesses)."""
    return {
        "rules_version": RULES_VERSION,
        "source": "existing artifacts inventory (P5-E prep)",
        "categories": {
            "A_screenshot_gap": {
                "supported": True,
                "evidence": [
                    "artifacts/price_captures/*/metadata.json 常见 price_label_coverage="
                    "价格标签可能未覆盖 / label_included=false",
                    "早期 run 无 price_context.png；较新 run 有 price_context 仍未见到手标签OCR",
                ],
            },
            "B_ocr_missed_visible_text": {
                "supported": False,
                "evidence": [
                    "现有 JSON 无法证明像素含「到手/券后」而 OCR blocks 漏掉；需人工对照截图"
                ],
            },
            "C_parse_association_gap": {
                "supported": False,
                "evidence": [
                    "artifacts 内 ocr_result.json 无 declared_take_home_candidate / "
                    "estimated_take_home_candidate；无标签则无「漏关联」可证"
                ],
            },
            "D_no_take_home_in_saved_evidence": {
                "supported": True,
                "evidence": [
                    "全部真实 price_clues 仅 PAGE_PRIMARY_SELLING；"
                    "take_home_clue_count=0；promotions OCR 多为满赠门槛"
                ],
            },
            "E_needs_other_page_or_action": {
                "supported": True,
                "evidence": [
                    "promo_detail 常见 STOPPED_PAGE_CHANGED（点优惠跳店铺）或面板无到手价数字",
                    "需用户手动打开优惠说明并确认到手价位置后再局部截图"
                ],
            },
        },
        "design_note": (
            "主展示价按规则永不当作到手价；即使金额低于红线也只作低价线索/待核验"
        ),
        "not_claimed": [
            "未证明页面永远不存在到手价",
            "未完成真实多SKU验收",
            "分页跨页真实采集仍待验收",
        ],
    }


def run_interactive(
    *,
    business_config: Path,
    url: str | None,
    product_id: str | None,
) -> dict[str, Any]:
    target = resolve_target(
        business_config=business_config, url=url, product_id=product_id
    )
    run_id = f"{utc_stamp()}_{target['product_id']}_{secrets.token_hex(3)}"
    out_dir = DIAG_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=False)

    bundle: dict[str, Any] = {
        "run_id": run_id,
        "phase": "P5-E",
        "rules_version": RULES_VERSION,
        "mode": MODE,
        "not_full_auto_pass": True,
        "started_at_utc": utc_iso(),
        "business_config_path": str(business_config.resolve()),
        "business_sku": target["business_sku"],
        "expected_model": target["expected_model"],
        "target_spec": target["target_spec"],
        "red_line": target["red_line"],
        "currency": target["currency"],
        "product_id": target["product_id"],
        "url_requested": target["url"],
        "url_safe": p2d.url_without_query_or_fragment(target["url"]),
        "prior_gap_summary": prior_gap_summary_from_artifacts(),
        "captures": [],
        "price_schemes": [],
        "identity_rejects": [],
        "identity_reject_count": 0,
        "profile_dir": str(PROFILE_DIR),
        "notes": [
            "人工辅助诊断；不算全自动流程通过",
            "不自动领券/加购/结算/下单",
            "不读取 Cookie/Token/地址/整页HTML",
        ],
    }
    write_json_atomic(out_dir / "prior_gap_summary.json", bundle["prior_gap_summary"])
    write_json_atomic(out_dir / "run_bundle.json", bundle)

    print("=== PriceRadar P5-E 到手价证据诊断（人工辅助）===")
    print(f"解释器: {sys.executable}")
    print(f"输出目录: {out_dir}")
    print(f"目标: product_id={target['product_id']} url={target['url']}")
    print(f"业务: sku={target['business_sku']} model={target['expected_model']}")
    print("本模式不算全自动通过。请先结束占用同一 Profile 的其他会话。")
    print("")

    identity_baseline: dict[str, Any] | None = None
    close_ok = False

    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel="chrome",
                headless=False,
                chromium_sandbox=True,
            )
        except PlaywrightError as exc:
            bundle["error"] = f"浏览器启动失败 {type(exc).__name__}: {exc}"
            write_json_atomic(out_dir / "run_bundle.json", sanitize_diag_value(bundle))
            raise

        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(
                target["url"],
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            identity_baseline = snapshot_page_identity(page)
            bundle["identity_on_open"] = identity_baseline
            if identity_baseline.get("product_id_from_url") and str(
                identity_baseline["product_id_from_url"]
            ) != str(target["product_id"]):
                print(
                    "警告: 打开后 URL 商品ID与请求不一致 "
                    f"{identity_baseline.get('product_id_from_url')} "
                    f"vs {target['product_id']}"
                )
            write_json_atomic(out_dir / "run_bundle.json", sanitize_diag_value(bundle))

            print("")
            print("浏览器已打开。请你：")
            print("  1) 确认是目标商品与已选规格；")
            print("  2) 在页面上找到「到手价/券后/预估到手」位置，或手动打开优惠说明；")
            print("  3) 不要领券、加购、进入结算或下单；")
            print("  4) 准备好后在本终端输入 capture 回车（保存局部截图）；")
            print("  5) 可多次 capture；输入 quit 结束并关闭浏览器。")
            print("")

            capture_idx = 0
            while True:
                try:
                    cmd = input("命令 [capture|quit]> ").strip().lower()
                except EOFError:
                    cmd = "quit"
                if cmd in {"", "capture", "c", "y", "yes"}:
                    capture_idx += 1
                    now_id = snapshot_page_identity(page)
                    ok, reasons = assert_identity_compatible(
                        identity_baseline or {}, now_id
                    )
                    if not ok:
                        bundle["identity_reject_count"] += 1
                        reject = {
                            "at_utc": utc_iso(),
                            "reasons": reasons,
                            "before": identity_baseline,
                            "after": now_id,
                        }
                        bundle["identity_rejects"].append(reject)
                        write_json_atomic(
                            out_dir / f"identity_reject_{capture_idx:02d}.json",
                            sanitize_diag_value(reject),
                        )
                        print("拒绝关联：" + "；".join(reasons))
                        print(
                            "请把规格/数量改回诊断开始时的状态，或输入 quit 后用新会话重开。"
                        )
                        write_json_atomic(
                            out_dir / "run_bundle.json", sanitize_diag_value(bundle)
                        )
                        continue

                    shot_dir_prefix = f"capture_{capture_idx:02d}"
                    shots: list[dict[str, Any]] = []
                    # Reuse P2-D automatic crops (no claim/checkout)
                    price_path = out_dir / f"{shot_dir_prefix}_price.png"
                    ctx_path = out_dir / f"{shot_dir_prefix}_price_context.png"
                    panel_path = out_dir / f"{shot_dir_prefix}_promo_panel.png"
                    print("正在截取 price / price_context / 可见优惠面板…")
                    price_info = p2d.capture_price(page, price_path)
                    price_info["filename"] = price_path.name
                    shots.append(sanitize_diag_value(price_info))
                    ctx_info = p2d.capture_price_context(page, ctx_path)
                    ctx_info["filename"] = ctx_path.name
                    shots.append(sanitize_diag_value(ctx_info))
                    panel_info = capture_visible_promo_panel(page, panel_path)
                    panel_info["filename"] = panel_path.name
                    shots.append(sanitize_diag_value(panel_info))

                    user_note = ""
                    try:
                        user_note = input(
                            "可选：用一句话描述到手价位置/标签（勿填地址账号）> "
                        ).strip()
                    except EOFError:
                        user_note = ""

                    capture_meta = {
                        "capture_index": capture_idx,
                        "captured_at_utc": utc_iso(),
                        "identity": now_id,
                        "screenshots": shots,
                        "association_ok": True,
                    }
                    scheme = build_price_scheme_stub(
                        product_id=str(
                            now_id.get("product_id_from_url") or target["product_id"]
                        ),
                        expected_model=target["expected_model"],
                        identity=now_id,
                        capture_meta=capture_meta,
                        user_notes=user_note,
                    )
                    bundle["captures"].append(capture_meta)
                    bundle["price_schemes"].append(scheme)
                    write_json_atomic(
                        out_dir / f"{shot_dir_prefix}_evidence.json",
                        sanitize_diag_value(
                            {"capture": capture_meta, "price_scheme": scheme}
                        ),
                    )
                    write_json_atomic(
                        out_dir / "run_bundle.json", sanitize_diag_value(bundle)
                    )
                    write_report_html(out_dir, bundle)
                    print(
                        f"已保存 capture_{capture_idx:02d} → {out_dir} "
                        f"(panel={panel_info.get('status')})"
                    )
                    continue

                if cmd in {"quit", "q", "exit", "done"}:
                    break
                print("未知命令。可用: capture | quit")

        finally:
            try:
                context.close()
                close_ok = True
            except PlaywrightError:
                close_ok = False

    bundle["ended_at_utc"] = utc_iso()
    bundle["close_ok"] = close_ok
    bundle = sanitize_diag_value(bundle)
    write_json_atomic(out_dir / "run_bundle.json", bundle)
    write_json_atomic(out_dir / "price_schemes.json", bundle.get("price_schemes") or [])
    report = write_report_html(out_dir, bundle)
    print(f"close_ok={close_ok}")
    print(f"out_dir={out_dir}")
    print(f"report={report}")
    return {"ok": True, "out_dir": str(out_dir), "run_id": run_id, "bundle": bundle}


def run_offline_self_checks() -> int:
    failures: list[str] = []
    tmp = DIAG_ROOT / "_offline_selfcheck"
    tmp.mkdir(parents=True, exist_ok=True)

    # 1) sanitize
    dirty = {
        "cookie": "abc",
        "ok": "safe",
        "nested": {"Authorization": "Bearer x", "note": "到手价约见截图"},
        "html": "<!DOCTYPE html><html>secret</html>",
    }
    clean = sanitize_diag_value(dirty)
    if clean.get("cookie") != "[redacted]":
        failures.append("sanitize cookie")
    if clean["nested"].get("Authorization") != "[redacted]":
        failures.append("sanitize auth")
    if clean.get("html") != "[redacted_html]":
        failures.append("sanitize html")

    # 2) identity reject on spec change
    before = {
        "product_id_from_url": "100005149674",
        "core_selected_spec": "1层200抽*20包",
        "purchase_quantity": 1,
    }
    after_ok = deepcopy(before)
    after_bad = {
        **before,
        "core_selected_spec": "1层200抽*10包",
    }
    ok1, _ = assert_identity_compatible(before, after_ok)
    ok2, reasons = assert_identity_compatible(before, after_bad)
    if not ok1:
        failures.append("identical identity should pass")
    if ok2 or not reasons:
        failures.append("spec change should reject association")

    after_pid = {**before, "product_id_from_url": "999"}
    ok3, _ = assert_identity_compatible(before, after_pid)
    if ok3:
        failures.append("product_id change should reject")

    # 3) evidence write (synthetic offline — not live take-home)
    syn = {
        "run_id": "synthetic_offline_diag",
        "mode": MODE,
        "product_id": "900001",
        "note": "synthetic/offline only — 非京东实测到手价",
        "price_schemes": [
            build_price_scheme_stub(
                product_id="900001",
                expected_model="TEST_SYNTH",
                identity={
                    "core_selected_spec": "测试规格",
                    "purchase_quantity": 1,
                    "url_safe": "https://item.m.jd.com/product/900001.html",
                },
                capture_meta={
                    "captured_at_utc": utc_iso(),
                    "screenshots": [
                        {"region": "price", "filename": "synthetic_price.png", "status": "SYNTHETIC"}
                    ],
                },
                user_notes="synthetic offline",
            )
        ],
    }
    write_json_atomic(tmp / "synthetic_bundle.json", sanitize_diag_value(syn))
    if syn["price_schemes"][0].get("amount") is not None:
        failures.append("must not invent take-home amount in synthetic stub")
    if syn["price_schemes"][0].get("manual_verdict") is not None:
        failures.append("manual_verdict must stay null")

    # 4) prior gap summary present
    gap = prior_gap_summary_from_artifacts()
    if not gap["categories"]["A_screenshot_gap"]["supported"]:
        failures.append("A should be supported by existing evidence")
    if gap["categories"]["C_parse_association_gap"]["supported"]:
        failures.append("C must not be claimed without declared labels")

    # 5) resolve target from b913a config
    cfg_path = PROJECT_ROOT / "config" / "products" / "b913a.json"
    if cfg_path.is_file():
        t = resolve_target(business_config=cfg_path, url=None, product_id=None)
        if t["product_id"] != "100005149674":
            failures.append("b913a resolve product_id")
    else:
        failures.append("missing b913a.json")

    # 6) excel template still business-only
    excel = load_sibling("priceradar_excel_input")
    fails_xlsx = excel.run_excel_self_checks()
    failures.extend(fails_xlsx)

    print("=== P5-E offline self-check ===")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK")
    print(f"synthetic_out={tmp}")
    write_json_atomic(
        PROJECT_ROOT / "artifacts" / "take_home_diagnose" / "_evidence_gap_conclusion.json",
        gap,
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="P5-E human-assisted take-home diagnose")
    p.add_argument(
        "--business-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "products" / "b913a.json",
    )
    p.add_argument("--url", type=str, default=None, help="Explicit product detail URL")
    p.add_argument("--product-id", type=str, default=None, help="JD product id")
    p.add_argument(
        "--self-check",
        action="store_true",
        help="Offline tests only; no browser",
    )
    p.add_argument(
        "--write-gap-conclusion",
        action="store_true",
        help="Write evidence gap JSON from existing artifacts and exit",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_check:
        return run_offline_self_checks()
    if args.write_gap_conclusion:
        gap = prior_gap_summary_from_artifacts()
        path = DIAG_ROOT / "_evidence_gap_conclusion.json"
        write_json_atomic(path, gap)
        print(path)
        return 0
    try:
        run_interactive(
            business_config=Path(args.business_config),
            url=args.url,
            product_id=args.product_id,
        )
    except (ValueError, PlaywrightError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
