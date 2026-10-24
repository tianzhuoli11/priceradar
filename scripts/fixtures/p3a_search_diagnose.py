"""
Minimal JD search diagnosis (one attempt). Not production P3-A candidate collection.

- Uses the same independent Profile and search.jd.com entry as p3a_jd_search.
- Saves viewport screenshot + sanitized diagnosis JSON.
- On clear restriction / captcha / login wall: save and stop (no refresh, no second entry).
- Does not export Cookie, Token, storage_state, full HTML, or account fields.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import sync_playwright

# Allow importing sibling production helpers without package install.
_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import p3a_jd_search as p3a  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = (PROJECT_ROOT / "browser_profiles" / "jd_persistent").resolve()
OUT_ROOT = (PROJECT_ROOT / "artifacts" / "search_probes").resolve()

SETTLE_BUDGET_MS = 12_000
SETTLE_POLL_MS = 500
DEFAULT_QUERY = "清风 B913A"

# Heuristic result-region selectors (presence only; not extraction success).
RESULT_REGION_SELECTORS = (
    "#J_goodsList",
    "#J_goodsList .gl-item",
    ".gl-warp .gl-item",
    "li.gl-item",
    "[class*='goods-list']",
    "[class*='goodsList']",
    "[class*='search-result']",
    ".search-results",
    "#searchResult",
    "[data-sku]",
    "div[class*='gl-i']",
)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sanitize_url_keep_keyword(url: str) -> str:
    if not url or url == "UNKNOWN":
        return url or "UNKNOWN"
    return p3a.sanitize_source_search_url(url)


def collect_diagnosis_dom(page: Page) -> dict[str, Any]:
    """Non-sensitive page signals only (no cookies / storage / full HTML)."""
    return page.evaluate(
        """(resultSelectors) => {
            const visibleText = (() => {
                const chunks = [];
                const walk = (node) => {
                    if (!node) return;
                    if (node.nodeType === Node.TEXT_NODE) {
                        const parent = node.parentElement;
                        if (!parent) return;
                        const tag = (parent.tagName || '').toUpperCase();
                        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') {
                            return;
                        }
                        const st = window.getComputedStyle(parent);
                        if (
                            st.display === 'none'
                            || st.visibility === 'hidden'
                            || st.opacity === '0'
                        ) {
                            return;
                        }
                        const t = (node.textContent || '').replace(/\\s+/g, ' ').trim();
                        if (t) chunks.push(t);
                        return;
                    }
                    if (node.nodeType !== Node.ELEMENT_NODE) return;
                    const tag = (node.tagName || '').toUpperCase();
                    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') {
                        return;
                    }
                    for (const child of node.childNodes) walk(child);
                };
                if (document.body) walk(document.body);
                return chunks.join('\\n');
            })();

            const abs = (href) => {
                try { return new URL(href, location.href).href; }
                catch (e) { return href || ''; }
            };
            const pathPattern = (href) => {
                try {
                    const u = new URL(abs(href));
                    // Keep host + path shape only; drop query (may carry tracking).
                    const path = u.pathname.replace(/\\d{5,}/g, '<id>');
                    return u.hostname + path;
                } catch (e) {
                    return 'unparseable';
                }
            };

            const allAnchors = Array.from(document.querySelectorAll('a[href]'));
            const productAnchors = Array.from(
                document.querySelectorAll(
                    'a[href*="item.jd.com"], a[href*="item.m.jd.com/product/"]'
                )
            );
            const itemLikeHrefs = productAnchors
                .map((a) => a.getAttribute('href') || '')
                .filter((h) =>
                    /item\\.jd\\.com\\/\\d+\\.html/i.test(h)
                    || /item\\.m\\.jd\\.com\\/product\\/\\d+/i.test(h)
                );

            // Broader JD item-ish patterns seen on modern SERP cards.
            const broadItem = allAnchors.map((a) => a.getAttribute('href') || '').filter((h) =>
                /item\\.jd\\.com/i.test(h)
                || /item\\.m\\.jd\\.com/i.test(h)
                || /skuId=\\d+/i.test(h)
                || /\\/product\\/\\d+/i.test(h)
                || /npc\\.jd\\.com/i.test(h)
            );

            const patternCounts = {};
            for (const a of allAnchors.slice(0, 400)) {
                const href = a.getAttribute('href') || '';
                if (!href || href.startsWith('javascript:')) continue;
                const pat = pathPattern(href);
                patternCounts[pat] = (patternCounts[pat] || 0) + 1;
            }
            const href_path_pattern_samples = Object.entries(patternCounts)
                .sort((x, y) => y[1] - x[1])
                .slice(0, 15)
                .map(([pattern, count]) => ({ pattern, count }));

            const dataSkuCount = document.querySelectorAll('[data-sku]').length;
            const iframeCount = document.querySelectorAll('iframe').length;

            const regionHits = [];
            for (const sel of resultSelectors) {
                try {
                    const nodes = document.querySelectorAll(sel);
                    if (nodes && nodes.length) {
                        const n = nodes[0];
                        const r = n.getBoundingClientRect();
                        regionHits.push({
                            selector: sel,
                            present: true,
                            match_count: nodes.length,
                            width: Math.round(r.width),
                            height: Math.round(r.height),
                        });
                    }
                } catch (e) {
                    /* ignore invalid selector */
                }
            }

            const textU = visibleText.toUpperCase();
            return {
                page_title: document.title || '',
                final_url: location.href,
                visible_text_len: visibleText.length,
                visible_contains_b913a: textU.includes('B913A'),
                visible_contains_qingfeng: visibleText.includes('清风'),
                visible_contains_restriction_words: (
                    visibleText.includes('访问频繁') && visibleText.includes('无法搜索')
                ),
                product_anchor_count: productAnchors.length,
                item_like_href_count: itemLikeHrefs.length,
                broad_item_href_count: broadItem.length,
                all_anchor_count: allAnchors.length,
                data_sku_count: dataSkuCount,
                iframe_count: iframeCount,
                href_path_pattern_samples: href_path_pattern_samples,
                result_region_hits: regionHits,
                result_region_present: regionHits.length > 0,
                ready_state: document.readyState || '',
            };
        }""",
        list(RESULT_REGION_SELECTORS),
    )


def settle_until_signal(page: Page, *, budget_ms: int = SETTLE_BUDGET_MS) -> dict[str, Any]:
    """
    Bounded wait: stop early on restriction / captcha / login, or when product
    anchors or a result-region selector appears. Does not refresh or retry.
    """
    started = datetime.now(timezone.utc)
    polls = 0
    last_dom: dict[str, Any] = {}
    stop_reason = "budget_exhausted"

    while True:
        polls += 1
        elapsed_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        try:
            restriction = p3a.detect_search_restriction(page)
            blocking = p3a.diagnose_page_state(page)
            last_dom = collect_diagnosis_dom(page)
        except PlaywrightError as exc:
            return {
                "settle_wait_ms": int(elapsed_ms),
                "settle_polls": polls,
                "stop_reason": "evaluate_error",
                "wait_error": f"{type(exc).__name__}: {exc}",
                "restriction": None,
                "blocking_status": p3a.STATUS_PAGE_ERROR,
                "dom": last_dom,
            }

        if restriction or blocking == p3a.STATUS_SEARCH_RESTRICTED:
            stop_reason = "search_restricted"
            return {
                "settle_wait_ms": int(elapsed_ms),
                "settle_polls": polls,
                "stop_reason": stop_reason,
                "wait_error": None,
                "restriction": restriction,
                "blocking_status": p3a.STATUS_SEARCH_RESTRICTED,
                "dom": last_dom,
            }

        if blocking in {
            p3a.STATUS_LOGIN_REQUIRED,
            p3a.STATUS_VERIFICATION_REQUIRED,
            p3a.STATUS_PAGE_ERROR,
            p3a.STATUS_NO_RESULTS,
        }:
            return {
                "settle_wait_ms": int(elapsed_ms),
                "settle_polls": polls,
                "stop_reason": f"blocking:{blocking}",
                "wait_error": None,
                "restriction": None,
                "blocking_status": blocking,
                "dom": last_dom,
            }

        anchors = int(last_dom.get("product_anchor_count") or 0)
        broad = int(last_dom.get("broad_item_href_count") or 0)
        data_sku = int(last_dom.get("data_sku_count") or 0)
        region = bool(last_dom.get("result_region_present"))
        text_hit = bool(
            last_dom.get("visible_contains_b913a")
            or last_dom.get("visible_contains_qingfeng")
        )
        if anchors > 0 or broad > 0 or data_sku > 0 or region:
            return {
                "settle_wait_ms": int(elapsed_ms),
                "settle_polls": polls,
                "stop_reason": "results_signal_seen",
                "wait_error": None,
                "restriction": None,
                "blocking_status": None,
                "dom": last_dom,
            }
        # Text-only hit is a weak signal (may appear in suggest chips); keep polling
        # until budget unless we already spent most of the wait.
        if text_hit and elapsed_ms >= max(3_000, budget_ms * 0.5):
            return {
                "settle_wait_ms": int(elapsed_ms),
                "settle_polls": polls,
                "stop_reason": "query_text_visible_weak_signal",
                "wait_error": None,
                "restriction": None,
                "blocking_status": None,
                "dom": last_dom,
            }

        if elapsed_ms >= budget_ms:
            break
        try:
            page.wait_for_timeout(SETTLE_POLL_MS)
        except PlaywrightError as exc:
            return {
                "settle_wait_ms": int(elapsed_ms),
                "settle_polls": polls,
                "stop_reason": "wait_timeout_error",
                "wait_error": f"{type(exc).__name__}: {exc}",
                "restriction": None,
                "blocking_status": None,
                "dom": last_dom,
            }

    return {
        "settle_wait_ms": int(
            (datetime.now(timezone.utc) - started).total_seconds() * 1000
        ),
        "settle_polls": polls,
        "stop_reason": stop_reason,
        "wait_error": None,
        "restriction": None,
        "blocking_status": None,
        "dom": last_dom,
    }


def classify_outcome(settle: dict[str, Any], *, http_status: int | None) -> dict[str, Any]:
    blocking = settle.get("blocking_status")
    restriction = settle.get("restriction")
    dom = settle.get("dom") or {}
    anchors = int(dom.get("product_anchor_count") or 0)
    item_like = int(dom.get("item_like_href_count") or 0)
    broad = int(dom.get("broad_item_href_count") or 0)
    data_sku = int(dom.get("data_sku_count") or 0)
    region = bool(dom.get("result_region_present"))
    text_products = bool(dom.get("visible_contains_b913a"))

    if blocking == p3a.STATUS_SEARCH_RESTRICTED or restriction:
        outcome = p3a.STATUS_SEARCH_RESTRICTED
        reason = "visible_search_frequency_block"
    elif blocking:
        outcome = str(blocking)
        reason = f"diagnose:{blocking}"
    elif anchors > 0 or item_like > 0 or broad > 0:
        outcome = "RESULTS_SIGNAL_PRESENT"
        reason = "product_anchors_or_item_like_hrefs_seen"
    elif data_sku > 0 or region:
        outcome = "RESULT_REGION_WITHOUT_ITEM_ANCHORS"
        reason = (
            "result_region_or_data_sku_present_but_zero_classic_item_anchors;"
            "selector_mismatch_likely"
        )
    elif text_products:
        outcome = "VISIBLE_TEXT_WITHOUT_ITEM_ANCHORS"
        reason = (
            "B913A_visible_in_text_but_zero_item_anchors;"
            "DOM_structure_or_href_shape_mismatch_likely;"
            "screenshot_required_for_human_confirm"
        )
    else:
        outcome = "NO_RESULTS_SIGNAL"
        reason = (
            "zero_product_anchors_and_no_result_region_after_settle;"
            "HTTP_ok_does_not_mean_search_ok;"
            "zero_anchors_not_asserted_as_NO_RESULTS"
        )

    return {
        "outcome": outcome,
        "reason": reason,
        "http_status": http_status,
        "http_ok_not_search_success": True,
        "zero_anchors_not_equal_no_products": True,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PriceRadar P3-A minimal search diagnosis (one attempt)"
    )
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument(
        "--settle-ms",
        type=int,
        default=SETTLE_BUDGET_MS,
        help="Bounded settle wait after domcontentloaded (ms)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    query = (args.query or "").strip()
    if not query:
        print("query 不能为空")
        return 2

    settle_ms = max(0, int(args.settle_ms))
    search_url = p3a.build_search_url(query)
    run_id = f"{utc_stamp()}_{secrets.token_hex(3)}"
    out_dir = OUT_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=False)

    print("=== PriceRadar P3-A search diagnose ===")
    print(f"interpreter: {sys.executable}")
    print(f"project: {PROJECT_ROOT}")
    print(f"profile: {PROFILE_DIR}")
    print(f"query: {query!r}")
    print(f"search_url: {search_url}")
    print(f"out: {out_dir}")
    print("policy: one attempt; stop on restriction/captcha; no cookie/HTML export")
    print()

    navigation_error: str | None = None
    http_status: int | None = None
    final_url = "UNKNOWN"
    settle: dict[str, Any] = {}
    screenshot_path: str | None = None
    screenshot_error: str | None = None
    browser_started = False
    close_ok = False

    try:
        with sync_playwright() as playwright:
            try:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(PROFILE_DIR),
                    channel="chrome",
                    headless=False,
                    chromium_sandbox=True,
                )
            except PlaywrightError as exc:
                navigation_error = f"browser_launch_failed: {type(exc).__name__}: {exc}"
                print(navigation_error)
                diagnosis = {
                    "run_id": run_id,
                    "phase": "P3-A-DIAGNOSE",
                    "query": query,
                    "profile_path": str(PROFILE_DIR),
                    "search_entry": p3a.SEARCH_ENTRY_STRATEGY,
                    "search_url_requested": search_url,
                    "final_url_sanitized": "UNKNOWN",
                    "page_title": None,
                    "http_status": None,
                    "navigation_error": navigation_error,
                    "wait_error": None,
                    "visible_restriction_tip": None,
                    "result_region_present": False,
                    "product_anchor_count": 0,
                    "item_like_href_count": 0,
                    "outcome": "LAUNCH_FAILED",
                    "reason": navigation_error,
                    "screenshot": None,
                    "recorded_at_utc": utc_iso(),
                    "notes": [
                        "诊断失败于浏览器启动；未访问搜索页",
                        "未导出 Cookie / Token / storage_state / 整页 HTML",
                    ],
                }
                (out_dir / "diagnosis.json").write_text(
                    json.dumps(diagnosis, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                print(f"diagnosis: {out_dir / 'diagnosis.json'}")
                return 1

            browser_started = True
            try:
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    response = page.goto(
                        search_url,
                        wait_until="domcontentloaded",
                        timeout=p3a.NAVIGATION_TIMEOUT_MS,
                    )
                    http_status = response.status if response is not None else None
                    final_url = page.url
                    print(
                        f"nav http_status={http_status} "
                        f"final={sanitize_url_keep_keyword(final_url)}"
                    )
                except PlaywrightError as exc:
                    navigation_error = f"{type(exc).__name__}: {exc}"
                    final_url = getattr(page, "url", "UNKNOWN") or "UNKNOWN"
                    print(f"navigation_error: {navigation_error}")
                    settle = {
                        "settle_wait_ms": 0,
                        "settle_polls": 0,
                        "stop_reason": "navigation_failed",
                        "wait_error": None,
                        "restriction": None,
                        "blocking_status": p3a.STATUS_NAVIGATION_FAILED,
                        "dom": {},
                    }
                else:
                    settle = settle_until_signal(page, budget_ms=settle_ms)
                    print(
                        f"settle stop={settle.get('stop_reason')} "
                        f"ms={settle.get('settle_wait_ms')} "
                        f"blocking={settle.get('blocking_status')}"
                    )
                    if settle.get("restriction"):
                        print(
                            "restriction tip: "
                            f"{settle['restriction'].get('observed_tip')!r}"
                        )
                        print("stop: no scroll / refresh / second entry")
                    else:
                        # Final DOM snapshot at screenshot time (may differ from early polls).
                        try:
                            settle["dom"] = collect_diagnosis_dom(page)
                            settle["restriction"] = p3a.detect_search_restriction(page)
                            settle["blocking_status"] = p3a.diagnose_page_state(page)
                        except PlaywrightError as exc:
                            settle["wait_error"] = (
                                f"final_dom: {type(exc).__name__}: {exc}"
                            )

                try:
                    shot = out_dir / "search_page.png"
                    page.screenshot(path=str(shot), full_page=False)
                    screenshot_path = str(shot.resolve())
                    print(f"screenshot: {screenshot_path}")
                except PlaywrightError as exc:
                    screenshot_error = f"{type(exc).__name__}: {exc}"
                    print(f"screenshot_error: {screenshot_error}")
            finally:
                try:
                    context.close()
                    close_ok = True
                except PlaywrightError as exc:
                    print(f"close_error: {type(exc).__name__}: {exc}")
    except PlaywrightError as exc:
        navigation_error = navigation_error or f"{type(exc).__name__}: {exc}"
        print(f"playwright_error: {navigation_error}")

    classification = classify_outcome(settle, http_status=http_status)
    dom = settle.get("dom") or {}
    restriction = settle.get("restriction") or {}

    diagnosis: dict[str, Any] = {
        "run_id": run_id,
        "phase": "P3-A-DIAGNOSE",
        "query": query,
        "profile_path": str(PROFILE_DIR),
        "search_entry": p3a.SEARCH_ENTRY_STRATEGY,
        "search_url_requested": search_url,
        "final_url_sanitized": sanitize_url_keep_keyword(final_url),
        "final_host": urlsplit(final_url).netloc if final_url not in {"UNKNOWN", ""} else None,
        "page_title": dom.get("page_title"),
        "http_status": http_status,
        "navigation_error": navigation_error,
        "wait_error": settle.get("wait_error"),
        "settle_wait_ms": settle.get("settle_wait_ms"),
        "settle_polls": settle.get("settle_polls"),
        "settle_stop_reason": settle.get("stop_reason"),
        "visible_restriction_tip": restriction.get("observed_tip"),
        "restriction_visible_excerpt": restriction.get("visible_excerpt"),
        "blocking_status": settle.get("blocking_status"),
        "result_region_present": bool(dom.get("result_region_present")),
        "result_region_hits": dom.get("result_region_hits") or [],
        "product_anchor_count": int(dom.get("product_anchor_count") or 0),
        "item_like_href_count": int(dom.get("item_like_href_count") or 0),
        "broad_item_href_count": int(dom.get("broad_item_href_count") or 0),
        "all_anchor_count": int(dom.get("all_anchor_count") or 0),
        "data_sku_count": int(dom.get("data_sku_count") or 0),
        "iframe_count": int(dom.get("iframe_count") or 0),
        "href_path_pattern_samples": dom.get("href_path_pattern_samples") or [],
        "visible_contains_b913a": bool(dom.get("visible_contains_b913a")),
        "visible_contains_qingfeng": bool(dom.get("visible_contains_qingfeng")),
        "visible_contains_restriction_words": bool(
            dom.get("visible_contains_restriction_words")
        ),
        "document_ready_state": dom.get("ready_state"),
        "visible_text_len": dom.get("visible_text_len"),
        "outcome": classification["outcome"],
        "reason": classification["reason"],
        "http_ok_not_search_success": True,
        "zero_anchors_not_equal_no_products": True,
        "screenshot": screenshot_path,
        "screenshot_error": screenshot_error,
        "browser_started": browser_started,
        "close_ok": close_ok,
        "recorded_at_utc": utc_iso(),
        "notes": [
            "HTTP 200 / 导航完成不等于搜索成功",
            "零商品锚点不直接等于没有商品",
            "可见频繁限制提示时立即停止，不刷新不重试",
            "未导出 Cookie / Token / storage_state / 整页 HTML / 账户信息",
            "本文件仅为诊断；候选收集仍走 p3a_jd_search.py",
        ],
    }

    diag_path = out_dir / "diagnosis.json"
    diag_path.write_text(
        json.dumps(diagnosis, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print()
    print(f"outcome: {diagnosis['outcome']}")
    print(f"reason: {diagnosis['reason']}")
    print(f"title: {diagnosis.get('page_title')!r}")
    print(f"restriction: {diagnosis.get('visible_restriction_tip')!r}")
    print(f"result_region_present: {diagnosis['result_region_present']}")
    print(f"product_anchor_count: {diagnosis['product_anchor_count']}")
    print(f"diagnosis.json: {diag_path.resolve()}")

    if diagnosis["outcome"] == p3a.STATUS_SEARCH_RESTRICTED:
        return 1
    if navigation_error:
        return 1
    if diagnosis["outcome"] == "RESULTS_SIGNAL_PRESENT":
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
