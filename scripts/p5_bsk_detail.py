"""
BrowserSkill adapter for JD *desktop* detail access + region screenshots.

Does not replace p2d Playwright capture. Explicit backend only.
Never exports cookies/tokens/storage_state. Does not claim mobile selectors work.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from p5_bsk_runtime import (  # type: ignore
    BskClient,
    check_prereqs,
    cooperate_policy_from_prereqs,
    cooperate_sync,
    eval_json,
    observe_page,
    pick_browser,
    sanitize,
    screenshot_session,
    start_session,
    stop_session,
    utc_iso_now,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DETAIL_ROOT = (PROJECT_ROOT / "artifacts" / "bsk_detail_runs").resolve()

RULES_VERSION = "p5-bsk-detail-v4"
VIEWPORT_DIAGNOSTIC_FILENAME = "viewport_diagnostic.png"
# Prefer DOM-container crop over VOM leaf refs (yen digit / truncated title).
CAPTURE_METHOD_DOM_BOX_CROP = "bsk_viewport_crop_dom_box"
CAPTURE_METHOD_VOM_ELEMENT = "bsk_dom_vom_element"

# Desktop first — do not silently rewrite to m.jd.
DESKTOP_ITEM_TMPL = "https://item.jd.com/{pid}.html"

CLASSIFY_PAGE_JS = r"""(() => {
  const href = String(location.href || '');
  const path = String(location.pathname || '');
  const title = String(document.title || '');
  const bodyText = String((document.body && document.body.innerText) || '').slice(0, 4000);
  const visibleText = bodyText.replace(/\s+/g, ' ').trim();

  const pickIdFromUrl = () => {
    let m = href.match(/item\.jd\.com\/(\d+)\.html/i);
    if (m) return m[1];
    m = href.match(/item\.m\.jd\.com\/product\/(\d+)\.html/i);
    if (m) return m[1];
    m = path.match(/\/(\d{5,})\.html/);
    return m ? m[1] : null;
  };

  const urlProductId = pickIdFromUrl();
  let pageKind = 'UNKNOWN';
  let sessionBlock = null;

  if (/limit\.html/i.test(href) || /\/common\/limit/i.test(href)
      || /pc-frequent-pro\.pf\.jd\.com/i.test(href)
      || /reason=403/.test(href)) {
    pageKind = 'SESSION_LIMIT';
    sessionBlock = 'DETAIL_RATE_LIMITED';
  } else if (/passport|plogin|login\.jd\.com/i.test(href)) {
    pageKind = 'LOGIN';
    sessionBlock = 'LOGIN_REQUIRED';
  } else if (/验证码|安全验证|滑动验证/.test(visibleText) && !/item\.jd\.com/i.test(href)) {
    pageKind = 'VERIFICATION';
    sessionBlock = 'VERIFICATION_REQUIRED';
  } else if (/访问受限|频繁访问|操作过于频繁|系统繁忙.*稍后/.test(visibleText)
             && !/item\.jd\.com\/\d+\.html/i.test(href)) {
    pageKind = 'ACCESS_RESTRICTED';
    sessionBlock = 'DETAIL_RATE_LIMITED';
  } else if (/商品已下架|该商品已下架|下架/.test(visibleText)
             && (/item\.jd\.com/i.test(href) || /item\.m\.jd\.com/i.test(href))) {
    pageKind = 'PRODUCT_OFF_SHELF';
  } else if (/此商品不支持|配送.*不可|暂不支持配送|无货/.test(visibleText)
             && (/item\.jd\.com/i.test(href))) {
    // Soft signal — may still be a valid detail page with stock note.
    pageKind = 'DETAIL_MAYBE_UNAVAILABLE';
  } else if (/item\.jd\.com\/\d+\.html/i.test(href) || /item\.m\.jd\.com\/product\/\d+\.html/i.test(href)) {
    pageKind = 'PRODUCT_DETAIL';
  } else if (/WRONG|error|404|找不到/.test(title + visibleText.slice(0, 200))) {
    pageKind = 'PAGE_ERROR';
  }

  // DOM id hints (desktop)
  const skuName = document.querySelector(
    '.sku-title-text, .sku-title-name, .sku-name, #name h1, .product-intro .sku-name'
  );
  const priceEl = document.querySelector(
    '.product-price-panel, .page-right-price, .product-price--main, '
    + '.summary-price .p-price .price, .p-price .price, #jd-price'
  );
  const shopEl = document.querySelector(
    '.top-name, .J-hove-wrap .name a, .popbox .name a, #popbox .name a, .shopName a'
  );

  return {
    href: href.slice(0, 300),
    title: title.slice(0, 200),
    url_product_id: urlProductId,
    page_kind: pageKind,
    session_block: sessionBlock,
    has_sku_name: !!(skuName && (skuName.innerText||'').trim()),
    sku_name_preview: skuName ? String(skuName.innerText||'').replace(/\s+/g,' ').trim().slice(0,160) : null,
    has_price_candidate: !!priceEl,
    price_preview: priceEl ? String(priceEl.innerText||'').replace(/\s+/g,' ').trim().slice(0,80) : null,
    has_shop_candidate: !!shopEl,
    shop_preview: shopEl ? String(shopEl.innerText||'').replace(/\s+/g,' ').trim().slice(0,80) : null,
    body_preview: visibleText.slice(0, 240),
  };
})()"""

DISCOVER_REGIONS_JS = r"""(() => {
  const visible = (el) => {
    if (!el) return false;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return r.width > 8 && r.height > 8
      && st.visibility !== 'hidden' && st.display !== 'none'
      && Number(st.opacity||'1') > 0.05;
  };
  const boxOf = (el) => {
    const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, width: r.width, height: r.height,
             top: r.top, left: r.left, bottom: r.bottom, right: r.right };
  };
  const textOf = (el) => String(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim();

  const scoreUnique = (nodes) => {
    const vis = nodes.filter(visible);
    if (vis.length === 0) return { status: 'NOT_FOUND', count: 0, el: null };
    if (vis.length > 1 && vis.length > 3) {
      return { status: 'AMBIGUOUS', count: vis.length, el: null };
    }
    const el = vis.length === 1 ? vis[0] : vis.sort((a,b) =>
      (b.getBoundingClientRect().width*b.getBoundingClientRect().height)
      - (a.getBoundingClientRect().width*a.getBoundingClientRect().height))[0];
    return { status: vis.length === 1 ? 'FOUND' : 'AMBIGUOUS', count: vis.length, el };
  };

  const trySelectors = (sels, regionKey) => {
    for (const sel of sels) {
      try {
        const nodes = Array.from(document.querySelectorAll(sel));
        const scored = scoreUnique(nodes);
        if (scored.status === 'FOUND' && scored.el) {
          scored.el.setAttribute('data-pr-region', regionKey);
          return { status: 'FOUND', selector: sel, count: 1,
                   text: textOf(scored.el).slice(0,240), box: boxOf(scored.el) };
        }
        if (scored.status === 'AMBIGUOUS') {
          return { status: 'AMBIGUOUS', selector: sel, count: scored.count, text: null, box: null };
        }
      } catch (e) {
        return { status: 'EVAL_EXCEPTION', selector: sel, error: String(e), count: 0, text: null, box: null };
      }
    }
    return { status: 'NOT_FOUND', selector: null, count: 0, text: null, box: null };
  };

  const title = trySelectors([
    '.sku-title-text',
    '.sku-title-name',
    '.sku-title',
    '.page-right-skuname .sku-title-text',
    '.sku-name',
    '#name .sku-name',
    '#name h1',
  ], 'title');
  const price = trySelectors([
    '.product-price-panel',
    '.page-right-price',
    '.product-price--main',
    '.summary-price-wrap .p-price',
    '.summary-price .p-price',
    '.p-price',
    '#jd-price',
  ], 'price');
  const promo = trySelectors([
    '.page-right-benefit',
    '.benefit-panel',
    '#summary-promotion',
    '.summary-promotion',
    '.promo-item',
    '.p-promotions',
  ], 'promo');
  const shop = trySelectors([
    '.top-name',
    '.top-shop-info .top-name',
    '.J-hove-wrap .name a',
    '#popbox .name a',
    '.shopName a',
    '.sku-shopname',
  ], 'shop');

  // Evidence (2026-11-02 desktop probe 10181313217018): selected class is
  // .specification-item-sku--selected (NOT legacy --sel).
  let selected = trySelectors([
    '.specification-item-sku--selected',
    '.specification-item-sku.specification-item-sku--selected',
    '.page-right-spec .specification-item-sku--selected',
    '.specification-item-sku--sel',
    '.page-right-spec',
    '.specifications-panel-content',
  ], 'selected_spec');
  if (selected.status === 'FOUND' && selected.selector === '.page-right-spec') {
    selected.note = 'fell_back_to_full_spec_panel; selected item class may be missing';
  }
  if (selected.status !== 'FOUND') {
    const specCandidates = [];
    const labels = Array.from(document.querySelectorAll('div, dt, span, strong'));
    for (const el of labels) {
      if (!visible(el)) continue;
      const t = textOf(el);
      if (t === '已选' || t === '已选择') {
        const row = el.closest('.li, .item, dl, .p-choose, .page-right-spec') || el.parentElement;
        if (row && visible(row)) specCandidates.push(row);
      }
    }
    const uniq = [...new Set(specCandidates)];
    if (uniq.length === 1) {
      uniq[0].setAttribute('data-pr-region', 'selected_spec');
      selected = {
        status: 'FOUND', selector: 'selected_spec_heuristic', count: 1,
        text: textOf(uniq[0]).slice(0, 240), box: boxOf(uniq[0]),
      };
    } else if (uniq.length > 1) {
      selected = { status: 'AMBIGUOUS', selector: 'selected_spec_heuristic', count: uniq.length, text: null, box: null };
    }
  }

  const iframeCount = document.querySelectorAll('iframe').length;
  const shadowHosts = Array.from(document.querySelectorAll('*')).filter(el => el.shadowRoot).length;

  return {
    title, price, promo, shop, selected_spec: selected,
    note: 'desktop discovery v2; legacy .sku-name/.p-price + 2026 page-right-* react layout',
    dom_environment: { iframeCount, shadowHosts },
    viewport: { w: window.innerWidth, h: window.innerHeight, scrollY: window.scrollY },
  };
})()"""

SELECTION_STATE_JS = r"""(() => {
  const textOf = (el) => el ? String(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim() : null;
  const pickId = () => {
    const m = String(location.href||'').match(/item\.jd\.com\/(\d+)\.html/i);
    return m ? m[1] : null;
  };
  // Desktop JD 2026: live probe confirmed selected = .specification-item-sku--selected
  const SELECTED_CLASS_RE = /specification-item-sku--selected|--selected\b/;
  const root = document.querySelector('.page-right-spec');
  const groups = [];
  const available_options = [];
  const selected_options = [];
  const selection_evidence = {
    selector_basis: 'live_probe_20261102_specification-item-sku--selected',
    root_present: !!root,
    selected_class_used: 'specification-item-sku--selected',
  };
  if (root) {
    const groupEls = Array.from(root.querySelectorAll('.specification-group'));
    const scanGroups = groupEls.length ? groupEls : [root];
    for (const g of scanGroups) {
      const titleEl = g.querySelector('.specification-group-title, .specification-group-name, .title');
      const dim = textOf(titleEl) || (g === root ? '规格' : null);
      const skus = Array.from(g.querySelectorAll('.specification-item-sku'));
      const opts = [];
      for (const el of skus) {
        const t = textOf(el);
        if (!t) continue;
        const cls = String(el.className || '');
        const ariaChecked = el.getAttribute('aria-checked');
        const ariaSelected = el.getAttribute('aria-selected');
        const ariaPressed = el.getAttribute('aria-pressed');
        const selected = SELECTED_CLASS_RE.test(cls)
          || ariaChecked === 'true'
          || ariaSelected === 'true'
          || ariaPressed === 'true';
        const variant_id = el.getAttribute('data-sku')
          || el.getAttribute('data-sku-id')
          || el.getAttribute('skuid')
          || (el.dataset && (el.dataset.sku || el.dataset.skuId))
          || null;
        const rec = {
          dimension: dim,
          text: t.slice(0, 120),
          className: cls.slice(0, 160),
          selected,
          aria_checked: ariaChecked,
          aria_selected: ariaSelected,
          aria_pressed: ariaPressed,
          variant_id,
        };
        opts.push(rec);
        available_options.push(rec);
        if (selected) selected_options.push(rec);
      }
      groups.push({ dimension: dim, options: opts.map(o => ({ text: o.text, selected: o.selected, variant_id: o.variant_id })) });
    }
  }
  selection_evidence.group_count = groups.length;
  selection_evidence.available_count = available_options.length;
  selection_evidence.selected_count = selected_options.length;
  let selected_status = 'SELECTED_SPEC_UNKNOWN';
  let selected_spec_text = null;
  if (selected_options.length >= 1) {
    selected_status = 'SELECTED_SPEC_EXTRACTED';
    selected_spec_text = selected_options.map(o => o.text).join(' | ');
  } else if (available_options.length > 0) {
    selected_status = 'SELECTED_SPEC_UNKNOWN';
    selection_evidence.reason = 'options_present_but_no_selected_class_or_aria';
  } else if (!root) {
    selection_evidence.reason = 'page_right_spec_missing';
  } else {
    selection_evidence.reason = 'no_specification_item_sku_nodes';
  }
  const qtyInput = document.querySelector(
    '.page-right input[type="text"], .page-right-buy input, input[aria-label*="数量"], .buy-num input, '
    + 'input[class*="count"], input[class*="quantity"]'
  );
  let purchase_quantity = null;
  let purchase_quantity_status = 'UNKNOWN';
  if (qtyInput) {
    const raw = String(qtyInput.value || qtyInput.getAttribute('value') || '').trim();
    if (raw !== '') {
      purchase_quantity = raw;
      purchase_quantity_status = 'OBSERVED';
    }
  }
  const priceVal = document.querySelector('.product-price--value, .product-price--main');
  const priceLabel = document.querySelector('.product-price-panel, .page-right-price');
  const variant_ids = selected_options.map(o => o.variant_id).filter(Boolean);
  return {
    url_product_id: pickId(),
    selected_status,
    selected_spec_text,
    selected_options,
    available_options: available_options.map(o => ({
      dimension: o.dimension, text: o.text, selected: o.selected, variant_id: o.variant_id
    })),
    selection_evidence,
    spec_groups: groups,
    variant_id: variant_ids.length === 1 ? variant_ids[0] : (variant_ids.length ? variant_ids : null),
    purchase_quantity,
    purchase_quantity_status,
    price_value_text: textOf(priceVal),
    price_panel_preview: textOf(priceLabel) ? textOf(priceLabel).slice(0, 120) : null,
    shop_text: textOf(document.querySelector('.top-name, .top-shop-info .top-name')),
    title_text: textOf(document.querySelector('.sku-title-text, .sku-title-name')),
  };
})()"""

DOM_DISCOVER_DIAGNOSTIC_JS = r"""(() => {
  const q = (s) => { try { return document.querySelectorAll(s).length; } catch (e) { return -1; } };
  return {
    evaluate_ok: true,
    legacy_counts: { skuName: q('.sku-name'), pPrice: q('.p-price'), chooseAttrs: q('#choose-attrs') },
    modern_counts: {
      skuTitleText: q('.sku-title-text'),
      productPricePanel: q('.product-price-panel'),
      specSelectedLegacySel: q('.specification-item-sku--sel'),
      specSelected: q('.specification-item-sku--selected'),
      topName: q('.top-name'),
    },
    iframeCount: q('iframe'),
    bodyTextLen: String((document.body && document.body.innerText)||'').length,
  };
})()"""


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sanitize_final_url(url: str) -> str:
    parts = urlsplit(url or "")
    if not parts.scheme:
        return (url or "")[:300]
    # Drop query/fragment (may contain tracking; also avoid leaking addresses)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def classify_session_block(page_info: dict[str, Any]) -> str | None:
    """Session restriction beats ordinary identity mismatch."""
    block = page_info.get("session_block")
    if block:
        return str(block)
    kind = str(page_info.get("page_kind") or "")
    href = str(page_info.get("href") or "").lower()
    if kind in {"SESSION_LIMIT", "ACCESS_RESTRICTED"}:
        return "DETAIL_RATE_LIMITED"
    if kind == "LOGIN":
        return "LOGIN_REQUIRED"
    if kind == "VERIFICATION":
        return "VERIFICATION_REQUIRED"
    if (
        "limit.html" in href
        or "/common/limit" in href
        or "pc-frequent-pro.pf.jd.com" in href
        or "reason=403" in href
    ):
        return "DETAIL_RATE_LIMITED"
    return None


def identity_from_page(
    page_info: dict[str, Any], *, candidate_product_id: str
) -> dict[str, Any]:
    pid = str(candidate_product_id).strip()
    url_id = page_info.get("url_product_id")
    final_url = sanitize_final_url(str(page_info.get("href") or ""))
    block = classify_session_block(page_info)
    if block:
        return {
            "ok": False,
            "reason": block,
            "priority": "session_block_over_identity",
            "candidate_product_id": pid,
            "url_product_id": url_id,
            "final_url_sanitized": final_url,
            "page_kind": page_info.get("page_kind"),
        }
    kind = str(page_info.get("page_kind") or "")
    if kind == "PRODUCT_OFF_SHELF":
        return {
            "ok": False,
            "reason": "PRODUCT_OFF_SHELF",
            "candidate_product_id": pid,
            "url_product_id": url_id,
            "final_url_sanitized": final_url,
            "page_kind": kind,
        }
    if kind not in {"PRODUCT_DETAIL", "DETAIL_MAYBE_UNAVAILABLE"}:
        return {
            "ok": False,
            "reason": kind or "UNKNOWN_PAGE",
            "candidate_product_id": pid,
            "url_product_id": url_id,
            "final_url_sanitized": final_url,
            "page_kind": kind,
        }
    if not url_id:
        return {
            "ok": False,
            "reason": "URL_PRODUCT_ID_UNCONFIRMED",
            "candidate_product_id": pid,
            "url_product_id": None,
            "final_url_sanitized": final_url,
            "page_kind": kind,
        }
    if str(url_id) != pid:
        return {
            "ok": False,
            "reason": "PRODUCT_ID_MISMATCH",
            "candidate_product_id": pid,
            "url_product_id": url_id,
            "final_url_sanitized": final_url,
            "page_kind": kind,
        }
    return {
        "ok": True,
        "reason": "URL_ID_MATCH",
        "candidate_product_id": pid,
        "url_product_id": url_id,
        "final_url_sanitized": final_url,
        "page_kind": kind,
    }


def wait_page_settle(
    client: BskClient,
    session_id: str,
    *,
    attempts: int = 8,
    delay_s: float = 1.0,
    cooperate_policy: str = "strict",
) -> dict[str, Any]:
    last: dict[str, Any] = {}
    for i in range(attempts):
        raw = eval_json(
            client,
            session_id,
            CLASSIFY_PAGE_JS,
            cooperate_policy=cooperate_policy,
            cooperate_settle=(i == 0),
        )
        data = raw.get("value") if isinstance(raw, dict) else raw
        if not isinstance(data, dict):
            data = {"page_kind": "UNKNOWN", "eval_raw": sanitize(raw)}
        last = data
        kind = str(data.get("page_kind") or "")
        if kind in {
            "PRODUCT_DETAIL",
            "DETAIL_MAYBE_UNAVAILABLE",
            "SESSION_LIMIT",
            "LOGIN",
            "VERIFICATION",
            "ACCESS_RESTRICTED",
            "PRODUCT_OFF_SHELF",
            "PAGE_ERROR",
        }:
            # For detail, also want some content signal
            if kind.startswith("PRODUCT") or kind == "DETAIL_MAYBE_UNAVAILABLE":
                if (
                    data.get("has_sku_name")
                    or data.get("has_price_candidate")
                    or data.get("url_product_id")
                    or i >= 2
                ):
                    last["settle_attempts"] = i + 1
                    return last
            else:
                last["settle_attempts"] = i + 1
                return last
        time.sleep(delay_s)
    last["settle_attempts"] = attempts
    return last


def region_status_from_discover(info: dict[str, Any] | None) -> str:
    if not info:
        return "NOT_FOUND"
    st = str(info.get("status") or "")
    if st == "EVAL_EXCEPTION":
        return "EVAL_EXCEPTION"
    if st == "FOUND":
        return "LOCATED"
    if st == "AMBIGUOUS":
        return "AMBIGUOUS"
    return "NOT_FOUND"


def eval_value_or_error(raw: dict[str, Any] | None) -> tuple[Any, dict[str, Any] | None]:
    """Do not collapse BSK/evaluate failures into NOT_FOUND."""
    if not isinstance(raw, dict):
        return None, {"status": "EVAL_ERROR", "detail": "non_dict_raw"}
    if not raw.get("ok"):
        err = raw.get("raw") or raw
        if isinstance(err, dict) and err.get("code"):
            return None, {
                "status": "EVAL_ERROR",
                "code": err.get("code"),
                "message": err.get("message"),
                "reason": (err.get("data") or {}).get("reason"),
            }
        return None, {"status": "EVAL_ERROR", "detail": sanitize(raw)}
    return raw.get("value"), None


def vom_find_ref(
    vom_text: str,
    *,
    region: str,
    anchor: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Resolve @eN from fresh observe text. anchor is a substring of the control label;
    never hard-code ref numbers.
    """
    if not vom_text:
        return None, "empty_vom"
    lines = vom_text.splitlines()
    patterns: list[tuple[str, re.Pattern[str]]] = []
    if region == "product_title":
        patterns.append(
            (
                "root_title",
                re.compile(r'RootWebArea\s+"([^"]{8,200})"'),
            )
        )
        patterns.append(
            (
                "sku_title_line",
                re.compile(r'(@e\d+)\s+(?:button|link|StaticText)\s+"([^"]{8,120})"'),
            )
        )
    elif region == "price":
        patterns.append(
            ("yen_button", re.compile(r'(@e\d+)\s+button\s+"¥\s*\d+')),
        )
    elif region == "selected_spec":
        patterns.append(
            (
                "spec_button",
                re.compile(r'(@e\d+)\s+button\s+"(\d+层\s+\d+抽\*?\d+包[^"]*)"'),
            )
        )
    elif region == "shop":
        patterns.append(
            (
                "shop_ctx",
                re.compile(r'(@e\d+)\s+button\s+"([^"]+专营店)"\s+\[ctx:'),
            )
        )
        patterns.append(
            (
                "shop_short",
                re.compile(r'(@e\d+)\s+button\s+"([^"]+专营店)"'),
            )
        )
    elif region == "promotions":
        patterns.append(
            ("promo", re.compile(r'(@e\d+)\s+button\s+"官方直降')),
        )

    for _name, pat in patterns:
        m = pat.search(vom_text)
        if m:
            ref = m.group(1) if m.lastindex and m.group(1).startswith("@e") else None
            if not ref:
                for g in m.groups():
                    if g and str(g).startswith("@e"):
                        ref = str(g)
                        break
            if ref:
                return ref, None

    if anchor and len(anchor) >= 4:
        needle = anchor[: min(24, len(anchor))]
        for line in lines:
            if needle not in line or "@e" not in line:
                continue
            mref = re.search(r"(@e\d+)", line)
            if mref:
                return mref.group(1), None
    return None, "vom_ref_unresolved"


def capture_viewport_diagnostic(
    client: BskClient,
    session_id: str,
    out_dir: Path,
    *,
    cooperate_policy: str,
    reason: str,
) -> dict[str, Any]:
    path = out_dir / VIEWPORT_DIAGNOSTIC_FILENAME
    shot = screenshot_session(
        client,
        session_id,
        path,
        cooperate_policy=cooperate_policy,
        cooperate_settle=True,
    )
    entry = {
        "region": "viewport_diagnostic",
        "filename": VIEWPORT_DIAGNOSTIC_FILENAME,
        "path": str(path),
        "status": "CAPTURED" if shot.get("ok") and path.is_file() else "SCREENSHOT_FAILED",
        "capture_method": "viewport_fallback",
        "diagnostic_reason": reason,
        "screenshot": sanitize(shot),
        "note": "整页诊断图；不得作为 product_title 或主售价 OCR 输入",
    }
    return entry


def _crop_viewport_to_dom_box(
    viewport_png: Path,
    out_path: Path,
    box: dict[str, Any],
    *,
    device_pixel_ratio: float,
    pad_px: float = 4.0,
) -> dict[str, Any]:
    """Crop a BSK viewport PNG using CSS-pixel DOM box × devicePixelRatio."""
    from PIL import Image  # noqa: PLC0415

    if not viewport_png.is_file():
        return {"ok": False, "reason": "viewport_png_missing"}
    try:
        x = float(box.get("x", box.get("left", 0)))
        y = float(box.get("y", box.get("top", 0)))
        w = float(box.get("w", box.get("width", 0)))
        h = float(box.get("h", box.get("height", 0)))
    except (TypeError, ValueError):
        return {"ok": False, "reason": "invalid_box"}
    if w < 8 or h < 8:
        return {"ok": False, "reason": "box_too_small", "box": box}
    dpr = float(device_pixel_ratio or 1.0)
    if dpr <= 0:
        dpr = 1.0
    with Image.open(viewport_png) as im:
        left = max(0, int((x - pad_px) * dpr))
        top = max(0, int((y - pad_px) * dpr))
        right = min(im.width, int((x + w + pad_px) * dpr))
        bottom = min(im.height, int((y + h + pad_px) * dpr))
        if right - left < 8 or bottom - top < 8:
            return {
                "ok": False,
                "reason": "crop_out_of_bounds",
                "img": [im.width, im.height],
                "crop": [left, top, right, bottom],
            }
        cropped = im.crop((left, top, right, bottom))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cropped.save(out_path)
        return {
            "ok": True,
            "crop_box_css": {"x": x, "y": y, "w": w, "h": h},
            "crop_box_px": [left, top, right, bottom],
            "dpr": dpr,
            "viewport_size_px": [im.width, im.height],
            "out_size_px": [cropped.width, cropped.height],
        }


def capture_region_bsk(
    client: BskClient,
    session_id: str,
    *,
    meta_key: str,
    mark_key: str,
    filename: str,
    discover: dict[str, Any] | None,
    out_path: Path,
    cooperate_policy: str,
    selection_before: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Capture a region covering the discovered DOM *container*.

    BSK `screenshot --ref` crops to the VOM node; price often resolves to the
    yen digit leaf and title may miss `.sku-title-text`. Documented BSK help
    supports viewport / element / full-page only — no arbitrary clip flag —
    so the primary scheme is: viewport screenshot + DOM getBoundingClientRect crop.
    """
    from p5_bsk_runtime import scroll_to_target  # noqa: PLC0415

    info: dict[str, Any] = {
        "region": meta_key,
        "filename": filename,
        "path": str(out_path),
        "discover": sanitize(discover),
        "capture_method": CAPTURE_METHOD_DOM_BOX_CROP,
    }
    if not isinstance(discover, dict):
        info["status"] = "DISCOVER_MISSING"
        return info
    st = str(discover.get("status") or "")
    if st == "EVAL_EXCEPTION":
        info["status"] = "EVAL_EXCEPTION"
        info["error"] = discover.get("error")
        return info
    if st == "AMBIGUOUS":
        info["status"] = "AMBIGUOUS"
        return info
    if st != "FOUND":
        info["status"] = "DOM_NOT_FOUND"
        return info

    selector = discover.get("selector")
    preview = str(discover.get("text") or "")
    scroll_js = f"""(() => {{
      const el = document.querySelector('[data-pr-region="{mark_key}"]')
        || document.querySelector({json.dumps(selector)});
      if (!el) return {{ ok: false, reason: 'element_missing', selector: {json.dumps(selector)} }};
      el.scrollIntoView({{ block: 'center', inline: 'nearest' }});
      const r = el.getBoundingClientRect();
      return {{
        ok: true,
        selector: {json.dumps(selector)},
        text: String(el.innerText||'').replace(/\\s+/g,' ').trim().slice(0,240),
        box: {{ x:r.x, y:r.y, w:r.width, h:r.height, width:r.width, height:r.height }},
        dpr: window.devicePixelRatio || 1,
        viewport: {{ w: window.innerWidth, h: window.innerHeight, scrollY: window.scrollY }},
      }};
    }})()"""
    def _eval_once(expression: str) -> tuple[Any, dict[str, Any] | None]:
        return eval_value_or_error(
            eval_json(
                client,
                session_id,
                expression,
                cooperate_policy=cooperate_policy,
                cooperate_settle=True,
            )
        )

    def _eval_with_one_resync(
        expression: str,
    ) -> tuple[Any, dict[str, Any] | None, bool]:
        """One sync+retry on observation_changed only (not rate-limit/login)."""
        val, err = _eval_once(expression)
        retried = False
        reason = ""
        if isinstance(err, dict):
            reason = str(
                err.get("reason")
                or ((err.get("detail") or {}) if isinstance(err.get("detail"), dict) else {}).get(
                    "reason"
                )
                or ""
            )
            blob = json.dumps(err, ensure_ascii=False)
            if "observation_changed" in blob or reason == "observation_changed":
                cooperate_sync(
                    client, session_id, policy=cooperate_policy, settle=True
                )
                time.sleep(0.35)
                val, err = _eval_once(expression)
                retried = True
        return val, err, retried

    scrolled, scroll_err, scroll_retried = _eval_with_one_resync(scroll_js)
    info["scroll_dom"] = sanitize(scrolled)
    info["scroll_resync_retry"] = scroll_retried
    if scroll_err:
        # Fall through to discover.box crop if available (same page, prior discover).
        info["scroll_eval_error"] = scroll_err
    elif isinstance(scrolled, dict) and not scrolled.get("ok"):
        info["scroll_error"] = scrolled
    else:
        time.sleep(0.35)

    # Re-read box after scroll/layout settle (same observation cycle).
    box_js = f"""(() => {{
      const el = document.querySelector('[data-pr-region="{mark_key}"]')
        || document.querySelector({json.dumps(selector)});
      if (!el) return {{ ok: false, reason: 'element_missing' }};
      const r = el.getBoundingClientRect();
      return {{
        ok: true,
        text: String(el.innerText||'').replace(/\\s+/g,' ').trim().slice(0,240),
        box: {{ x:r.x, y:r.y, w:r.width, h:r.height, width:r.width, height:r.height }},
        dpr: window.devicePixelRatio || 1,
      }};
    }})()"""
    box_info, box_err, box_retried = _eval_with_one_resync(box_js)
    info["box_refresh"] = sanitize(box_info)
    info["box_resync_retry"] = box_retried
    disc_box = discover.get("box") if isinstance(discover.get("box"), dict) else {}
    discover_scroll_y = None
    if isinstance(discover.get("viewport"), dict):
        discover_scroll_y = discover["viewport"].get("scrollY")
    scroll_y_now = None
    if isinstance(scrolled, dict) and isinstance(scrolled.get("viewport"), dict):
        scroll_y_now = scrolled["viewport"].get("scrollY")
    # Never reuse pre-scroll discover.box after layout/scroll/observation change.
    stale_discover_box = False
    if scroll_y_now is not None and discover_scroll_y is not None:
        if abs(float(scroll_y_now) - float(discover_scroll_y)) > 1:
            stale_discover_box = True
    if scroll_err or scroll_retried or box_retried:
        stale_discover_box = True
    if (
        (box_err or not isinstance(box_info, dict) or not box_info.get("ok"))
        and disc_box
        and not stale_discover_box
        and isinstance(scrolled, dict)
        and scrolled.get("ok")
        and isinstance(scrolled.get("box"), dict)
    ):
        # Same-eval scroll receipt box only (not older discover snapshot).
        info["box_fallback"] = "scroll_dom.box_same_receipt"
        box_info = {
            "ok": True,
            "text": scrolled.get("text") or preview,
            "box": scrolled.get("box"),
            "dpr": scrolled.get("dpr") or 1.0,
        }
    elif box_err or not isinstance(box_info, dict) or not box_info.get("ok"):
        info["status"] = "BOX_REFRESH_FAILED"
        info["box_error"] = box_err or box_info
        info["stale_discover_box_refused"] = stale_discover_box
        info["note"] = (
            "拒绝使用滚动前 discover.box 与新截图拼接；需同次观察收据中的 DOM box"
        )
        return info

    live_box = box_info.get("box") if isinstance(box_info, dict) else None
    if not isinstance(live_box, dict):
        info["status"] = "BOX_MISSING"
        return info
    dpr = float(box_info.get("dpr") or 1.0)
    info["box"] = live_box
    info["dom_text_at_capture"] = box_info.get("text")
    info["discover_text"] = preview
    info["geometry"] = {
        "coordinate_space": "css_px_then_device_pixels",
        "device_pixel_ratio_source": "window.devicePixelRatio_at_box_refresh",
        "device_pixel_ratio": dpr,
        "box_css": live_box,
        "discover_scroll_y": discover_scroll_y,
        "scroll_y_at_capture": scroll_y_now,
        "stale_discover_box_refused": stale_discover_box,
    }

    sync_vp = cooperate_sync(client, session_id, policy=cooperate_policy, settle=True)
    # Re-read box once more under the screenshot observation to avoid stale crop.
    box_info2, box_err2, _ = _eval_with_one_resync(box_js)
    if isinstance(box_info2, dict) and box_info2.get("ok") and isinstance(
        box_info2.get("box"), dict
    ):
        live_box = box_info2["box"]
        dpr = float(box_info2.get("dpr") or dpr)
        info["box"] = live_box
        info["box_refresh_pre_screenshot"] = sanitize(box_info2)
        info["geometry"]["device_pixel_ratio"] = dpr
        info["geometry"]["box_css"] = live_box
    elif box_err2:
        info["status"] = "BOX_STALE_BEFORE_SCREENSHOT"
        info["box_error_pre_screenshot"] = box_err2
        info["note"] = "截图前 DOM box 未能在当前观察下刷新；拒绝用旧坐标裁切"
        return info

    vp_tmp = out_path.parent / f"_tmp_viewport_{meta_key}.png"
    shot_vp = screenshot_session(
        client,
        session_id,
        vp_tmp,
        ref=None,
        context_id=sync_vp.get("context_id"),
        observation_id=sync_vp.get("observation_id"),
        cooperate_policy=cooperate_policy,
    )
    info["viewport_shot"] = {
        "ok": bool(shot_vp.get("ok")),
        "path": shot_vp.get("path"),
        "file_saved": bool(vp_tmp.is_file()),
    }
    crop = _crop_viewport_to_dom_box(
        vp_tmp, out_path, live_box if isinstance(live_box, dict) else {},
        device_pixel_ratio=dpr,
    )
    info["crop"] = sanitize(crop)
    info["content_valid"] = bool(
        crop.get("ok")
        and out_path.is_file()
        and (crop.get("out_size_px") or [0, 0])[0] >= 8
        and (crop.get("out_size_px") or [0, 0])[1] >= 8
    )
    info["file_saved"] = bool(out_path.is_file())
    try:
        if vp_tmp.is_file():
            vp_tmp.unlink()
    except OSError:
        pass

    # Audit-only: resolve VOM leaf ref (often yen digit) — not used as primary crop.
    obs = observe_page(client, session_id, cooperate_policy=cooperate_policy, settle=False)
    info["observe_ok"] = bool(obs.get("ok"))
    vom_text = str(obs.get("vom_text") or "")
    if not vom_text and isinstance(obs.get("data"), dict):
        vom_text = str(obs["data"].get("text") or "")
    anchor = preview[:40] if preview else None
    if meta_key == "price" and selection_before:
        pv = selection_before.get("price_value_text")
        if pv:
            anchor = str(pv)
    ref, vom_reason = vom_find_ref(vom_text, region=meta_key, anchor=anchor)
    info["vom_ref_audit"] = ref
    info["vom_resolve_note"] = vom_reason
    info["vom_ref_not_used_for_primary_crop"] = True
    info["note"] = (
        "主截图=BSK viewport + DOM 容器 box 裁切；"
        "VOM --ref 仅审计（价区常落到数字子节点，不足以覆盖首单/参考金额）"
    )

    if crop.get("ok") and out_path.is_file():
        info["status"] = "CAPTURED"
        info["file_bytes"] = out_path.stat().st_size
        info["capture_method"] = CAPTURE_METHOD_DOM_BOX_CROP
        # Guard: price/title crop should not be tiny leaf-sized if discover was wide.
        disc_box = discover.get("box") if isinstance(discover.get("box"), dict) else {}
        try:
            disc_w = float(disc_box.get("width") or disc_box.get("w") or 0)
            out_w = float((crop.get("out_size_px") or [0, 0])[0])
            if disc_w >= 200 and out_w > 0 and out_w < disc_w * dpr * 0.35:
                info["narrow_guard_warning"] = {
                    "discover_width_css": disc_w,
                    "out_width_px": out_w,
                    "dpr": dpr,
                    "note": "裁切宽度远小于 discover 容器，需人工核对截图",
                }
        except (TypeError, ValueError, IndexError):
            pass
        return info

    # Fallback: element ref (may be leaf) — last resort, annotated.
    if not ref:
        info["status"] = "SCREENSHOT_FAILED"
        info["fallback"] = "vom_ref_missing_after_crop_fail"
        return info
    scroll_to_target(
        client,
        session_id,
        ref=ref,
        context_id=obs.get("context_id"),
        observation_id=(
            (obs.get("data") or {}).get("observation_id")
            if isinstance(obs.get("data"), dict)
            else obs.get("observation_id")
        ),
        cooperate_policy=cooperate_policy,
    )
    time.sleep(0.3)
    sync_shot = cooperate_sync(client, session_id, policy=cooperate_policy, settle=True)
    shot = screenshot_session(
        client,
        session_id,
        out_path,
        ref=ref,
        context_id=sync_shot.get("context_id"),
        observation_id=sync_shot.get("observation_id"),
        cooperate_policy=cooperate_policy,
    )
    info["screenshot_fallback_ref"] = sanitize(shot)
    info["capture_method"] = CAPTURE_METHOD_VOM_ELEMENT
    info["fallback_reason"] = "dom_box_crop_failed"
    if shot.get("ok") and out_path.is_file():
        info["status"] = "CAPTURED_REF_FALLBACK"
        info["file_bytes"] = out_path.stat().st_size
    else:
        info["status"] = "SCREENSHOT_FAILED"
    return info


def offline_self_checks() -> list[str]:
    """Meaningful offline gates before a live P5-J run."""
    failures: list[str] = []
    sample_vom = '@e53 button "¥ 163"\n@e65 button "1层 200抽*20包 整箱装"\n'
    ref, _ = vom_find_ref(sample_vom, region="price")
    if ref != "@e53":
        failures.append("vom_find_ref price sample")
    ref2, _ = vom_find_ref(sample_vom, region="selected_spec")
    if ref2 != "@e65":
        failures.append("vom_find_ref spec sample")
    err_info = eval_value_or_error({"ok": False, "raw": {"code": "permission_denied"}})[1]
    if not err_info or err_info.get("status") != "EVAL_ERROR":
        failures.append("eval_value_or_error must not return NOT_FOUND")
    if region_status_from_discover({"status": "EVAL_EXCEPTION"}) != "EVAL_EXCEPTION":
        failures.append("region_status EVAL_EXCEPTION")
    # DOM-box crop helper (synthetic image)
    try:
        from PIL import Image  # noqa: PLC0415

        tmp_dir = DETAIL_ROOT / "_offline_crop_check"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        vp = tmp_dir / "vp.png"
        out = tmp_dir / "crop.png"
        Image.new("RGB", (400, 200), (240, 240, 240)).save(vp)
        Image.new("RGB", (80, 40), (20, 20, 20)).save(tmp_dir / "leaf.png")
        # Paint a dark region at css box * dpr=2 → (20,10)-(220,90) in px if box 10,5,100,40
        im = Image.new("RGB", (400, 200), (240, 240, 240))
        for x in range(20, 220):
            for y in range(10, 90):
                im.putpixel((x, y), (10, 10, 10))
        im.save(vp)
        crop = _crop_viewport_to_dom_box(
            vp,
            out,
            {"x": 10, "y": 5, "w": 100, "h": 40},
            device_pixel_ratio=2.0,
            pad_px=0,
        )
        if not crop.get("ok") or not out.is_file():
            failures.append("dom box crop helper failed")
        elif (crop.get("out_size_px") or [0])[0] < 90:
            failures.append("dom box crop should cover container width")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"crop helper offline check: {exc}")
    return failures


def run_bsk_detail_capture(
    *,
    product_id: str,
    product_url: str | None = None,
    out_dir: Path | None = None,
    browser: str | None = None,
    keep_session_on_block: bool = True,
) -> dict[str, Any]:
    """
    Navigate desktop detail in Agent Window via BrowserSkill.
    Returns capture-compatible dict for p5a pause/identity gates.
    """
    pid = str(product_id).strip()
    url = (product_url or DESKTOP_ITEM_TMPL.format(pid=pid)).strip()
    # Force desktop host if caller passed m.jd — prefer desktop for this adapter.
    if "item.m.jd.com" in url:
        url = DESKTOP_ITEM_TMPL.format(pid=pid)

    started = utc_iso_now()
    if out_dir is None:
        run_id = f"{utc_stamp()}_{pid}_{secrets.token_hex(3)}"
        out_dir = DETAIL_ROOT / run_id
    else:
        out_dir = Path(out_dir).resolve()
        run_id = out_dir.name
    out_dir.mkdir(parents=True, exist_ok=True)
    shots = out_dir / "shots"
    shots.mkdir(exist_ok=True)

    client = BskClient()
    prereq = check_prereqs(client)
    write_json(out_dir / "bsk_prereq.json", prereq)
    cooperate_policy = cooperate_policy_from_prereqs(prereq)

    result: dict[str, Any] = {
        "run_id": run_id,
        "phase": "P5-BSK-DETAIL",
        "rules_version": RULES_VERSION,
        "backend": "bsk",
        "started_at_utc": started,
        "out_dir": str(out_dir),
        "target_product_id": pid,
        "requested_url": url,
        "browser_started": False,
        "navigation_status": "NOT_STARTED",
        "capture_status": "NOT_STARTED",
        "product_identity_ok": False,
        "actual_product_url": None,
        "session_id": None,
        "regions": {},
        "error": None,
        "notes": [
            "BrowserSkill desktop detail adapter",
            "不自动切规格/领券/加购/结算",
            "不导出 cookie/token/storage_state",
            "会话限制优先于普通ID不匹配",
            "BSK 0.4.x: cooperate sync --policy strict when extension protocol < 1.8",
        ],
        "cooperate_policy": cooperate_policy,
    }

    if not prereq.get("daemon_ready"):
        result.update(
            {
                "capture_status": "FAILED",
                "navigation_status": "DAEMON_NOT_READY",
                "error": "bsk daemon not ready",
                "ended_at_utc": utc_iso_now(),
            }
        )
        write_json(out_dir / "metadata.json", result)
        return result

    picked = pick_browser(prereq.get("browsers_list"), browser)
    write_json(out_dir / "bsk_browser_selection.json", picked)
    if not picked.get("selector"):
        result.update(
            {
                "capture_status": "FAILED",
                "navigation_status": "NO_BROWSER_CONNECTED",
                "error": picked.get("error") or "no browser",
                "ended_at_utc": utc_iso_now(),
                "user_action_required": (
                    "Chrome 已开但扩展未连接。请在日常 Chrome 打开 BrowserSkill 扩展，"
                    "确认连接到本机 daemon（当前 ws_port 见 bsk status）。"
                    "不要启动 p2d 独立 Profile。"
                ),
            }
        )
        write_json(out_dir / "metadata.json", result)
        return result

    sess = start_session(client, str(picked["selector"]))
    session_id = sess.get("session_id")
    result["session_id"] = session_id
    result["browser_started"] = bool(sess.get("ok"))
    write_json(out_dir / "bsk_session_start.json", sanitize(sess))
    if not sess.get("ok") or not session_id:
        result.update(
            {
                "capture_status": "FAILED",
                "navigation_status": "SESSION_START_FAILED",
                "error": "session start failed",
                "ended_at_utc": utc_iso_now(),
            }
        )
        write_json(out_dir / "metadata.json", result)
        return result

    try:
        skip_nav = False
        href_raw = eval_json(
            client,
            session_id,
            "(() => ({ href: location.href, id: (location.href.match(/item\\.jd\\.com\\/(\\d+)\\.html/i)||[])[1] || null }))()",
            cooperate_policy=cooperate_policy,
            cooperate_settle=True,
        )
        href_val, _ = eval_value_or_error(href_raw)
        if (
            isinstance(href_val, dict)
            and str(href_val.get("id") or "") == pid
            and "item.jd.com" in str(href_val.get("href") or "")
        ):
            skip_nav = True
            result["navigation_reused"] = True
            write_json(out_dir / "bsk_navigate_skipped.json", sanitize(href_val))
        if not skip_nav:
            nav = client.run(
                [
                    "navigate",
                    url,
                    "--session",
                    session_id,
                    "--wait-until",
                    "load",
                    "--timeout",
                    "60s",
                ],
                timeout=90,
            )
            write_json(out_dir / "bsk_navigate.json", sanitize(nav))
            result["navigation_status"] = (
                "NAVIGATION_COMMAND_DONE" if nav.get("ok") else "NAVIGATION_FAILED"
            )
            if not nav.get("ok"):
                result["error"] = str(
                    (nav.get("data") or {}).get("message") or nav.get("stderr") or "navigate failed"
                )
            time.sleep(2)
        else:
            result["navigation_status"] = "NAVIGATION_REUSED_TAB"
        page_info = wait_page_settle(
            client, session_id, cooperate_policy=cooperate_policy
        )
        write_json(out_dir / "page_classify.json", sanitize(page_info))
        result["actual_product_url"] = sanitize_final_url(str(page_info.get("href") or ""))
        result["page_classify"] = sanitize(page_info)

        ident = identity_from_page(page_info, candidate_product_id=pid)
        result["identity"] = ident
        block = classify_session_block(page_info)
        if block:
            result.update(
                {
                    "navigation_status": "WRONG_PAGE_OR_CHALLENGE"
                    if block != "LOGIN_REQUIRED"
                    else "LOGIN_REQUIRED",
                    "capture_status": "FAILED",
                    "product_identity_ok": False,
                    "error": block,
                    "session_block": block,
                    "ended_at_utc": utc_iso_now(),
                    "user_action_required": (
                        f"会话受限({block})。可在同一 Chrome Agent 标签页手动打开: {url}；"
                        "人工结果须单独记录，不能冒充自动通过。"
                        if keep_session_on_block
                        else None
                    ),
                }
            )
            write_json(out_dir / "metadata.json", result)
            # Keep tab for manual compare when blocked
            if not keep_session_on_block:
                stop_session(client, session_id)
                result["session_stopped"] = True
            else:
                result["session_kept_for_manual"] = True
            return result

        if not ident.get("ok"):
            result.update(
                {
                    "navigation_status": "IDENTITY_FAILED",
                    "capture_status": "FAILED",
                    "product_identity_ok": False,
                    "error": ident.get("reason"),
                    "ended_at_utc": utc_iso_now(),
                }
            )
            write_json(out_dir / "metadata.json", result)
            stop_session(client, session_id)
            result["session_stopped"] = True
            return result

        result["product_identity_ok"] = True
        result["navigation_status"] = "DETAIL_OK"
        result["identity_scope_note"] = (
            "URL_ID_MATCH 仅表示地址栏商品 ID 与候选一致；"
            "页面标题/规格/价格/店铺仍依赖分区证据。"
        )

        sel_before_raw = eval_json(
            client,
            session_id,
            SELECTION_STATE_JS,
            cooperate_policy=cooperate_policy,
        )
        selection_before, sel_err = eval_value_or_error(sel_before_raw)
        if sel_err:
            selection_before = {"eval_error": sel_err}
        write_json(out_dir / "selection_state_before.json", sanitize(selection_before))

        disc_raw = eval_json(
            client,
            session_id,
            DISCOVER_REGIONS_JS,
            cooperate_policy=cooperate_policy,
            cooperate_settle=True,
        )
        discover, disc_err = eval_value_or_error(disc_raw)
        if disc_err:
            discover = {"_discover_eval_error": disc_err}
        if not isinstance(discover, dict):
            discover = {"_discover_eval_error": {"status": "EVAL_ERROR", "detail": "non_dict"}}
        write_json(out_dir / "region_discover.json", sanitize(discover))

        diag_raw = eval_json(
            client,
            session_id,
            DOM_DISCOVER_DIAGNOSTIC_JS,
            cooperate_policy=cooperate_policy,
        )
        diag_val, diag_err = eval_value_or_error(diag_raw)
        diagnostic: dict[str, Any] = {
            "discover_eval_error": disc_err,
            "diagnostic_eval_error": diag_err,
            "diagnostic": sanitize(diag_val),
            "prior_run_reference": "20261102T003508Z_p5i_b4ef77",
            "prior_failure_summary": (
                "legacy .sku-name/.p-price selectors count=0 on 2026 react desktop; "
                "evaluate succeeded; DOM mismatch not eval failure"
            ),
        }
        write_json(out_dir / "region_discover_diagnostic.json", diagnostic)

        region_files = {
            "title": ("product_title", "product_title.png", "title"),
            "price": ("price", "price.png", "price"),
            "selected_spec": ("selected_spec", "selected_spec.png", "selected_spec"),
            "promo": ("promotions", "promotions.png", "promo"),
            "shop": ("shop", "shop.png", "shop"),
        }
        regions_out: dict[str, Any] = {}
        images_list: list[dict[str, Any]] = []
        for logical, (meta_key, filename, mark_key) in region_files.items():
            d = discover.get("selected_spec" if logical == "selected_spec" else logical)
            path = out_dir / filename
            if not isinstance(d, dict) or d.get("status") != "FOUND":
                st = region_status_from_discover(d if isinstance(d, dict) else None)
                if disc_err and st == "NOT_FOUND":
                    st = "DISCOVER_EVAL_ERROR"
                regions_out[meta_key] = {
                    "region": meta_key,
                    "filename": filename,
                    "status": st,
                    "discover": sanitize(d),
                    "path": str(path),
                }
                write_json(out_dir / f"region_{meta_key}.json", regions_out[meta_key])
                continue
            captured = capture_region_bsk(
                client,
                session_id,
                meta_key=meta_key,
                mark_key=mark_key,
                filename=filename,
                discover=d,
                out_path=path,
                cooperate_policy=cooperate_policy,
                selection_before=selection_before if isinstance(selection_before, dict) else None,
            )
            captured["region"] = meta_key
            captured["filename"] = filename
            captured["path"] = str(path)
            regions_out[meta_key] = captured
            write_json(out_dir / f"region_{meta_key}.json", captured)
            cap_st = str(captured.get("status") or "")
            if cap_st in {"CAPTURED", "CAPTURED_REF_FALLBACK"} and path.is_file():
                images_list.append(
                    {
                        "filename": filename,
                        "path": str(path),
                        "role": meta_key,
                        "status": cap_st,
                        "capture_method": captured.get("capture_method")
                        or CAPTURE_METHOD_DOM_BOX_CROP,
                    }
                )

        element_captured = len(images_list)
        if element_captured < len(region_files):
            vp = capture_viewport_diagnostic(
                client,
                session_id,
                out_dir,
                cooperate_policy=cooperate_policy,
                reason="partial_or_missing_element_regions",
            )
            regions_out["viewport_diagnostic"] = vp
            write_json(out_dir / "region_viewport_diagnostic.json", vp)

        sel_after_raw = eval_json(
            client,
            session_id,
            SELECTION_STATE_JS,
            cooperate_policy=cooperate_policy,
        )
        selection_after, sel_after_err = eval_value_or_error(sel_after_raw)
        if sel_after_err:
            selection_after = {"eval_error": sel_after_err}
        write_json(out_dir / "selection_state_after.json", sanitize(selection_after))

        consistency: dict[str, Any] = {"url_id_unchanged": None, "spec_unchanged": None}
        if isinstance(selection_before, dict) and isinstance(selection_after, dict):
            consistency["url_id_unchanged"] = (
                selection_before.get("url_product_id") == selection_after.get("url_product_id") == pid
            )
            consistency["spec_unchanged"] = (
                selection_before.get("selected_spec_text") == selection_after.get("selected_spec_text")
            )
            b_status = selection_before.get("selected_status")
            a_status = selection_after.get("selected_status")
            if (
                a_status == "SELECTED_SPEC_UNKNOWN"
                or b_status == "SELECTED_SPEC_UNKNOWN"
                or (
                    not selection_after.get("selected_spec_text")
                    and not selection_after.get("selected_options")
                )
            ):
                consistency["spec_evidence"] = "SELECTED_SPEC_UNKNOWN"
            elif selection_before.get("selected_spec_text") != selection_after.get(
                "selected_spec_text"
            ) or (
                selection_before.get("selected_options")
                != selection_after.get("selected_options")
            ):
                # Capture-time change of the selected option — refuse merge.
                consistency["spec_evidence"] = "SELECTED_OPTION_CHANGED"
            else:
                consistency["spec_evidence"] = "OBSERVED_CONSISTENT"
        result["selection_consistency"] = consistency

        write_json(
            out_dir / "capture_audit_notes.json",
            {
                "rules_version": RULES_VERSION,
                "supersedes_run": "20261102T003508Z_p5i_b4ef77",
                "viewport_diagnostic_filename": VIEWPORT_DIAGNOSTIC_FILENAME,
                "viewport_not_ingested_as_title": True,
                "price_ocr_policy": "元素价区截图优先；视口 OCR 不得作为主售价/到手价",
                "packaging": {
                    "missing_vs_mismatch": (
                        "无 selected_spec 截图为 MISSING_EVIDENCE；"
                        "OCR/选择与基准冲突为 OBSERVED_SPEC_MISMATCH"
                    ),
                },
            },
        )

        result["regions"] = regions_out
        result["images"] = images_list
        captured_n = len(images_list)
        required_roles = {"product_title", "price", "selected_spec", "shop"}
        captured_roles = {img["role"] for img in images_list}
        result["region_capture_roles_ok"] = required_roles.issubset(captured_roles)
        result["capture_status"] = (
            "SUCCESS"
            if result["region_capture_roles_ok"]
            else ("PARTIAL" if captured_n else "FAILED")
        )
        result["overall_capture_status"] = result["capture_status"]
        result["ended_at_utc"] = utc_iso_now()
        write_json(out_dir / "metadata.json", result)
        stop_session(client, session_id)
        result["session_stopped"] = True
        return result
    except Exception as exc:  # noqa: BLE001
        result.update(
            {
                "capture_status": "FAILED",
                "error": f"{type(exc).__name__}: {exc}",
                "ended_at_utc": utc_iso_now(),
            }
        )
        write_json(out_dir / "metadata.json", result)
        try:
            stop_session(client, session_id)
        except Exception:
            pass
        return result


def run_offline_bsk_detail_checks() -> list[str]:
    failures: list[str] = []
    # Session block priority over id mismatch
    limit_page = {
        "href": "https://trade.m.jd.com/common/limit.html",
        "page_kind": "SESSION_LIMIT",
        "session_block": "DETAIL_RATE_LIMITED",
        "url_product_id": None,
    }
    ident = identity_from_page(limit_page, candidate_product_id="1556260334")
    if ident.get("reason") != "DETAIL_RATE_LIMITED":
        failures.append("limit page must classify as DETAIL_RATE_LIMITED before id mismatch")
    if classify_session_block(limit_page) != "DETAIL_RATE_LIMITED":
        failures.append("classify_session_block limit failed")

    ok_page = {
        "href": "https://item.jd.com/1556260334.html",
        "page_kind": "PRODUCT_DETAIL",
        "url_product_id": "1556260334",
        "session_block": None,
    }
    if not identity_from_page(ok_page, candidate_product_id="1556260334").get("ok"):
        failures.append("matching desktop detail should pass identity")

    mismatch = {
        "href": "https://item.jd.com/999.html",
        "page_kind": "PRODUCT_DETAIL",
        "url_product_id": "999",
        "session_block": None,
    }
    mid = identity_from_page(mismatch, candidate_product_id="1556260334")
    if mid.get("ok") or mid.get("reason") != "PRODUCT_ID_MISMATCH":
        failures.append("id change must fail identity")

    # Desktop URL preference
    if "item.m.jd.com" in DESKTOP_ITEM_TMPL:
        failures.append("desktop template must not be m.jd")
    failures.extend(offline_self_checks())
    if VIEWPORT_DIAGNOSTIC_FILENAME == "product_title.png":
        failures.append("viewport diagnostic must not use product_title.png")
    return failures


if __name__ == "__main__":
    fails = run_offline_bsk_detail_checks()
    print("offline:", "OK" if not fails else fails)
