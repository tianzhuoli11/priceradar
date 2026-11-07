"""P3-A: JD keyword search and candidate product-link collection (no price / AI)."""

from __future__ import annotations

import argparse
import csv
import json
import re
import secrets
import sys
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = (PROJECT_ROOT / "browser_profiles" / "jd_persistent").resolve()
SEARCH_ROOT = (PROJECT_ROOT / "artifacts" / "search_runs").resolve()

# Publicly documented desktop search entry (keyword + enc). Not project-verified yet.
SEARCH_ENTRY_STRATEGY = (
    "direct_url:https://search.jd.com/Search?keyword=<urlencoded>&enc=utf-8"
)
SEARCH_BASE = "https://search.jd.com/Search"

NAVIGATION_TIMEOUT_MS = 60_000
# Legacy CLI --limit default was 5; Phase C search default is 20.
MAX_LIMIT = 5  # kept for older validate_args callers; prefer MAX_CANDIDATES_CAP
DEFAULT_MAX_CANDIDATES = 20
MAX_CANDIDATES_CAP = 50
DEFAULT_MAX_PAGES = 2
MAX_PAGES_CAP = 10
DEFAULT_SEARCH_TIMEOUT_SECONDS = 60
MAX_SCROLLS = 3
SCROLL_PAUSE_MS = 800
SCROLL_BUDGET_MS = 8_000
EXTRACT_TIMEOUT_MS = 20_000
# After DOMContentLoaded, wait for either goods cards or a blocking tip.
RESULT_READY_TIMEOUT_MS = 12_000
# Wait after clicking observed 「下一页」 for real progress (new IDs / page label).
PAGE_PROGRESS_TIMEOUT_MS = 15_000
PAGE_PROGRESS_POLL_MS = 500
# New JD search UI uses div[data-sku] cards; classic item.jd.com anchors may be absent.
RESULT_CARD_SELECTOR = "[data-sku]"

# Coverage stop reasons (search stage)
STOP_HIT_CANDIDATE_LIMIT = "HIT_CANDIDATE_LIMIT"
STOP_HIT_PAGE_BUDGET = "HIT_PAGE_BUDGET"
STOP_SEARCH_TIMEOUT = "SEARCH_TIMEOUT"
STOP_NO_MORE_RESULTS = "NO_MORE_RESULTS"
STOP_NO_PROGRESS = "NO_PROGRESS"
STOP_LOGIN_REQUIRED = "LOGIN_REQUIRED"
STOP_VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"
STOP_SEARCH_RESTRICTED = "SEARCH_RESTRICTED"
STOP_EXTRACTION_FAILED = "EXTRACTION_FAILED"
STOP_NAVIGATION_FAILED = "NAVIGATION_FAILED"
STOP_NO_RESULTS = "NO_RESULTS"
STOP_LOAD_FAILED = "LOAD_FAILED"

# Accepted detail URL path patterns (digits = JD product id).
RE_ITEM_JD = re.compile(
    r"(?:https?:)?//item\.jd\.com/(?P<id>\d+)\.html",
    re.IGNORECASE,
)
RE_ITEM_M_JD = re.compile(
    r"(?:https?:)?//item\.m\.jd\.com/product/(?P<id>\d+)\.html",
    re.IGNORECASE,
)
RE_ITEM_JD_QUERY_SKU = re.compile(
    r"(?:https?:)?//item\.jd\.com/.*[?&](?:sku|skuId|productId)=(?P<id>\d+)",
    re.IGNORECASE,
)

STATUS_SUCCESS = "SUCCESS"
STATUS_PARTIAL = "PARTIAL"
STATUS_NO_RESULTS = "NO_RESULTS"
STATUS_EXTRACTION_FAILED = "EXTRACTION_FAILED"
STATUS_LOGIN_REQUIRED = "LOGIN_REQUIRED"
STATUS_VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"
STATUS_PAGE_ERROR = "PAGE_ERROR"
STATUS_NAVIGATION_FAILED = "NAVIGATION_FAILED"
STATUS_SEARCH_RESTRICTED = "SEARCH_RESTRICTED"

# Visible search-block tips observed in real runs. Require search-failure wording,
# not a bare "频繁" (which could appear in unrelated product titles).
SEARCH_RESTRICTED_PHRASES = (
    "抱歉由于访问频繁导致无法搜索，请稍后再试",
    "访问频繁导致无法搜索",
    "无法搜索，请稍后再试",
    "若长时间无法搜索可",
)


def playwright_version() -> str:
    try:
        return version("playwright")
    except PackageNotFoundError:
        return "unknown"


def read_line(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError as exc:
        raise RuntimeError(
            "当前 stdin 不可交互（EOF）。请在本机交互式终端中运行本脚本，"
            "不要通过无 stdin 的自动化工具启动。"
        ) from exc


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat(timespec="seconds")


def utc_run_stamp(dt: datetime | None = None) -> str:
    return (dt or utc_now()).strftime("%Y%m%dT%H%M%SZ")


def make_run_dir() -> tuple[str, Path]:
    run_id = f"{utc_run_stamp()}_{secrets.token_hex(3)}"
    out_dir = SEARCH_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=False)
    return run_id, out_dir


def build_search_url(query: str) -> str:
    # quote keeps spaces as %20; enc=utf-8 per public search URL docs.
    encoded = quote(query.strip(), safe="")
    return f"{SEARCH_BASE}?keyword={encoded}&enc=utf-8"


def url_without_query_or_fragment(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path if parts.path else "/"
    return f"{parts.scheme}://{parts.netloc}{path}"


def sanitize_source_search_url(url: str) -> str:
    """Keep host/path + keyword only; drop tracking params."""
    parts = urlsplit(url)
    qs = parse_qs(parts.query, keep_blank_values=False)
    keep: dict[str, list[str]] = {}
    if "keyword" in qs:
        keep["keyword"] = qs["keyword"][:1]
    if "enc" in qs:
        keep["enc"] = qs["enc"][:1]
    query = "&".join(f"{k}={quote(v[0], safe='')}" for k, v in keep.items())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def classify_detail_href(href: str, base_url: str = "https://search.jd.com/") -> dict[str, Any]:
    """
    Classify a candidate href. Does not invent product IDs.

    Returns keys: kind, product_id, observed_url, normalized_url, normalize_status
    kind: detail | pending_query_id | rejected
    """
    raw = (href or "").strip()
    if not raw or raw.startswith("javascript:"):
        return {
            "kind": "rejected",
            "product_id": None,
            "observed_url": raw,
            "normalized_url": None,
            "normalize_status": "empty_or_js",
        }

    absolute = urljoin(base_url, raw)
    # Protocol-relative
    if absolute.startswith("//"):
        absolute = "https:" + absolute

    m = RE_ITEM_JD.search(absolute) or RE_ITEM_M_JD.search(absolute)
    if m:
        pid = m.group("id")
        host = "item.jd.com" if "item.jd.com" in absolute else "item.m.jd.com"
        if host == "item.jd.com":
            normalized = f"https://item.jd.com/{pid}.html"
        else:
            normalized = f"https://item.m.jd.com/product/{pid}.html"
        return {
            "kind": "detail",
            "product_id": pid,
            "observed_url": absolute,
            "normalized_url": normalized,
            "normalize_status": "stripped_tracking_ok",
        }

    m_q = RE_ITEM_JD_QUERY_SKU.search(absolute)
    if m_q:
        return {
            "kind": "pending_query_id",
            "product_id": m_q.group("id"),
            "observed_url": absolute,
            "normalized_url": None,
            "normalize_status": "id_in_query_not_blindly_stripped",
        }

    host = urlsplit(absolute).netloc.lower()
    if "jd.com" in host and ("item" in host or "/product/" in absolute):
        return {
            "kind": "pending_query_id",
            "product_id": None,
            "observed_url": absolute,
            "normalized_url": None,
            "normalize_status": "unrecognized_jd_item_shape",
        }

    return {
        "kind": "rejected",
        "product_id": None,
        "observed_url": absolute,
        "normalized_url": None,
        "normalize_status": "not_jd_product_detail",
    }


def match_status_for_title(title: str, query: str) -> str:
    title_u = title or ""
    query_u = query or ""
    # Treat B913A specially when present in query or title.
    if "B913A" in title_u.upper() or "B913A" in query_u.upper():
        if "B913A" in title_u.upper():
            return "型号关键词命中，规格待核验"
        return "查询含型号词，标题未命中B913A，规格待核验"
    # Generic: all query tokens appear?
    tokens = [t for t in re.split(r"\s+", query_u.strip()) if t]
    if tokens and all(t.lower() in title_u.lower() for t in tokens):
        return "标题含全部查询词，规格待核验"
    if tokens and any(t.lower() in title_u.lower() for t in tokens):
        return "标题部分命中查询词，规格待核验"
    return "标题未明显命中查询词，仅作候选"


def csv_guard(value: Any) -> str:
    """CSV formula-injection guard for Excel."""
    text = "" if value is None else str(value)
    if text[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def dedupe_candidates(raw: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in raw:
        if len(out) >= limit:
            break
        pid = item.get("product_id")
        norm = item.get("normalized_url") or item.get("observed_url")
        if pid:
            if pid in seen_ids:
                continue
            seen_ids.add(pid)
        elif norm:
            key = norm.split("?", 1)[0]
            if key in seen_urls:
                continue
            seen_urls.add(key)
        else:
            continue
        out.append(item)
    return out


def collect_visible_text(page: Page, *, max_chars: int = 8000) -> str:
    """Visible rendered text only (skips hidden / script / style nodes)."""
    return str(
        page.evaluate(
            """(maxChars) => {
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
                return chunks.join('\\n').slice(0, maxChars);
            }""",
            max_chars,
        )
        or ""
    )


def match_search_restricted_tip(visible_text: str) -> str | None:
    """
    Return the matched non-sensitive tip fragment, or None.

    Requires explicit search-block wording. A product title that merely contains
    「频繁」 must not trigger SEARCH_RESTRICTED.
    """
    text = visible_text or ""
    for phrase in SEARCH_RESTRICTED_PHRASES:
        if phrase in text:
            return phrase
    # Combined visible cues seen together on the block page.
    if "访问频繁" in text and "无法搜索" in text:
        return "访问频繁+无法搜索"
    if "无法搜索" in text and "请稍后再试" in text and "抱歉" in text:
        return "抱歉+无法搜索+请稍后再试"
    return None


def detect_search_restriction(page: Page) -> dict[str, Any] | None:
    """If the visible page shows a search-frequency block, return evidence dict."""
    try:
        visible = collect_visible_text(page)
    except PlaywrightError:
        return None  # leave other diagnostics to handle hard page errors
    tip = match_search_restricted_tip(visible)
    if not tip:
        return None
    return {
        "status": STATUS_SEARCH_RESTRICTED,
        "observed_tip": tip,
        "detected_at_utc": utc_iso(),
        # Short visible excerpt around the tip for metadata (non-sensitive).
        "visible_excerpt": _excerpt_around(visible, tip, radius=40),
    }


def _excerpt_around(text: str, needle: str, *, radius: int = 40) -> str:
    idx = text.find(needle)
    if idx < 0:
        return text[:120]
    start = max(0, idx - radius)
    end = min(len(text), idx + len(needle) + radius)
    return text[start:end].replace("\n", " ")


def diagnose_page_state(page: Page) -> str | None:
    """Return a blocking status if obvious; None if search may proceed."""
    try:
        url = page.url.lower()
    except PlaywrightError:
        return STATUS_PAGE_ERROR

    if any(x in url for x in ("passport.jd.com", "login", "plogin")):
        return STATUS_LOGIN_REQUIRED

    restricted = detect_search_restriction(page)
    if restricted:
        return STATUS_SEARCH_RESTRICTED

    try:
        visible = collect_visible_text(page)
    except PlaywrightError:
        return STATUS_PAGE_ERROR

    if any(k in visible for k in ("安全验证", "验证码", "滑动验证", "请完成验证")):
        return STATUS_VERIFICATION_REQUIRED
    if "当前页面异常" in visible or "访问异常" in visible:
        return STATUS_PAGE_ERROR
    if any(
        k in visible
        for k in (
            "没有找到相关商品",
            "抱歉，没有找到",
            "未找到相关商品",
            "没有找到符合条件的商品",
        )
    ):
        return STATUS_NO_RESULTS
    return None


def wait_for_search_content(page: Page) -> dict[str, Any]:
    """
    Wait briefly for goods cards or an explicit block tip.

    Does not refresh or change URL. Returns wait outcome for metadata.
    """
    started = utc_now()
    outcome = "timeout_no_cards_no_tip"
    try:
        page.wait_for_function(
            """({ cardSel, phrases }) => {
                if (document.querySelector(cardSel)) return 'cards';
                const text = (document.body && document.body.innerText) || '';
                for (const p of phrases) {
                    if (text.includes(p)) return 'restricted_tip';
                }
                if (
                    text.includes('安全验证')
                    || text.includes('验证码')
                    || text.includes('滑动验证')
                    || text.includes('请完成验证')
                ) {
                    return 'verification';
                }
                return false;
            }""",
            arg={
                "cardSel": RESULT_CARD_SELECTOR,
                "phrases": list(SEARCH_RESTRICTED_PHRASES),
            },
            timeout=RESULT_READY_TIMEOUT_MS,
        )
        # Re-check which signal won (wait_for_function only tells us something matched).
        if detect_search_restriction(page):
            outcome = "restricted_tip"
        else:
            try:
                visible = collect_visible_text(page, max_chars=4000)
            except PlaywrightError:
                visible = ""
            if any(
                k in visible
                for k in ("安全验证", "验证码", "滑动验证", "请完成验证")
            ):
                outcome = "verification"
            elif page.locator(RESULT_CARD_SELECTOR).count() > 0:
                outcome = "cards_present"
            else:
                outcome = "signal_unclear"
    except PlaywrightError as exc:
        # Timeout or page closed — still useful as metadata.
        if detect_search_restriction(page):
            outcome = "restricted_tip_after_wait_error"
        else:
            outcome = f"wait_error:{type(exc).__name__}"

    elapsed_ms = int((utc_now() - started).total_seconds() * 1000)
    card_count = 0
    try:
        card_count = page.locator(RESULT_CARD_SELECTOR).count()
    except PlaywrightError:
        pass
    return {
        "result_ready_timeout_ms": RESULT_READY_TIMEOUT_MS,
        "wait_elapsed_ms": elapsed_ms,
        "wait_outcome": outcome,
        "data_sku_count_after_wait": card_count,
    }


def limited_scroll_for_lazy_load(page: Page) -> dict[str, Any]:
    """Scroll a few times, but stop immediately if search becomes restricted."""
    scrolls = 0
    started = utc_now()
    stopped_reason = "completed_or_budget"
    restriction: dict[str, Any] | None = None

    # Check before any scroll (normal wait / pre-extract window).
    restriction = detect_search_restriction(page)
    if restriction:
        return {
            "scroll_count": 0,
            "scroll_budget_ms": SCROLL_BUDGET_MS,
            "max_scrolls": MAX_SCROLLS,
            "stopped_reason": "search_restricted_before_scroll",
            "restriction": restriction,
        }

    while scrolls < MAX_SCROLLS:
        elapsed_ms = (utc_now() - started).total_seconds() * 1000
        if elapsed_ms >= SCROLL_BUDGET_MS:
            stopped_reason = "budget_exhausted"
            break
        restriction = detect_search_restriction(page)
        if restriction:
            stopped_reason = "search_restricted_before_scroll_iteration"
            break
        try:
            page.evaluate(
                "() => window.scrollBy(0, Math.floor(window.innerHeight * 0.85))"
            )
            page.wait_for_timeout(SCROLL_PAUSE_MS)
            scrolls += 1
        except PlaywrightError:
            stopped_reason = "scroll_error"
            break
        restriction = detect_search_restriction(page)
        if restriction:
            stopped_reason = "search_restricted_after_scroll"
            break

    return {
        "scroll_count": scrolls,
        "scroll_budget_ms": SCROLL_BUDGET_MS,
        "max_scrolls": MAX_SCROLLS,
        "stopped_reason": stopped_reason,
        "restriction": restriction,
    }


def page_scroll_metrics(page: Page) -> dict[str, Any]:
    """Viewport / document scroll metrics for end-of-page checks."""
    try:
        return dict(
            page.evaluate(
                """() => ({
                    scroll_y: window.scrollY || 0,
                    inner_height: window.innerHeight || 0,
                    scroll_height: Math.max(
                        document.documentElement.scrollHeight || 0,
                        document.body ? document.body.scrollHeight : 0
                    ),
                    at_bottom: (window.scrollY + window.innerHeight)
                        >= (Math.max(
                            document.documentElement.scrollHeight || 0,
                            document.body ? document.body.scrollHeight : 0
                        ) - 40)
                })"""
            )
            or {}
        )
    except PlaywrightError as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "at_bottom": False}


def resolve_search_budget(
    *,
    limit: int | None = None,
    max_candidates: int | None = None,
    max_pages: int | None = None,
    search_timeout_seconds: int | None = None,
    limit_explicit: bool = False,
    max_candidates_explicit: bool = False,
    max_pages_explicit: bool = False,
    search_timeout_explicit: bool = False,
) -> dict[str, Any]:
    """
    Resolve search coverage budget. --limit is legacy alias for max_candidates.
    Conflict when both --limit and --max-candidates are set to different values.
    """
    if limit_explicit and max_candidates_explicit and limit != max_candidates:
        raise ValueError(
            f"--limit={limit} 与 --max-candidates={max_candidates} 冲突；"
            "请只保留其一，或使两者相等"
        )
    if limit_explicit and not max_candidates_explicit:
        mc = int(limit)  # type: ignore[arg-type]
    elif max_candidates_explicit:
        mc = int(max_candidates)  # type: ignore[arg-type]
    else:
        mc = DEFAULT_MAX_CANDIDATES

    mp = (
        int(max_pages)
        if max_pages_explicit and max_pages is not None
        else DEFAULT_MAX_PAGES
    )
    st = (
        int(search_timeout_seconds)
        if search_timeout_explicit and search_timeout_seconds is not None
        else DEFAULT_SEARCH_TIMEOUT_SECONDS
    )

    if mc < 1 or mc > MAX_CANDIDATES_CAP:
        raise ValueError(f"max-candidates 必须是 1..{MAX_CANDIDATES_CAP}")
    if mp < 1 or mp > MAX_PAGES_CAP:
        raise ValueError(f"max-pages 必须是 1..{MAX_PAGES_CAP}")
    if st < 5 or st > 600:
        raise ValueError("search-timeout-seconds 必须是 5..600")

    return {
        "max_candidates": mc,
        "max_pages": mp,
        "search_timeout_seconds": st,
        "limit_compat": mc,
        "legacy_limit_mode": bool(limit_explicit and not max_candidates_explicit),
    }


def collect_visible_product_ids(page: Page) -> list[str]:
    return list(
        page.evaluate(
            """() => {
                const ids = [];
                const seen = new Set();
                for (const el of document.querySelectorAll('[data-sku]')) {
                    const id = String(el.getAttribute('data-sku') || '').trim();
                    if (!id || !/^\\d{5,}$/.test(id) || seen.has(id)) continue;
                    seen.add(id);
                    ids.push(id);
                }
                return ids;
            }"""
        )
        or []
    )


def discover_next_page_control(page: Page) -> dict[str, Any]:
    """
    Observe real pagination controls on the rendered SERP.
    Prefer a leaf whose visible text is exactly 「下一页」 (not ancestor wrappers).
    Does not invent URL page parameters.
    """
    return page.evaluate(
        """() => {
            const out = {
                status: 'NOT_FOUND',
                control: null,
                pager_text: null,
                total_pages_hint: null,
                current_page_hint: null,
                disabled: false,
                notes: [],
            };
            const nodes = Array.from(
                document.querySelectorAll('a, button, span, div, li, em, i')
            );
            const exact = [];
            for (const el of nodes) {
                const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                if (t !== '下一页') continue;
                const r = el.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) continue;
                // Reject huge wrappers that merely contain the label deep inside.
                if (r.width > 220 || r.height > 80) continue;
                const cls = typeof el.className === 'string' ? el.className : '';
                const ariaDisabled = (el.getAttribute('aria-disabled') || '') === 'true';
                const disabledAttr = el.hasAttribute('disabled');
                const disabledCls = /disabled|is-disabled|pagination_disable/i.test(cls);
                exact.push({
                    tag: el.tagName,
                    className: cls.slice(0, 120),
                    id: el.id || '',
                    text: t,
                    href: el.href || el.getAttribute('href') || null,
                    x: r.left, y: r.top, width: r.width, height: r.height,
                    area: r.width * r.height,
                    disabled: ariaDisabled || disabledAttr || disabledCls,
                });
            }
            exact.sort((a, b) => a.area - b.area);
            if (exact.length) {
                out.control = exact[0];
                out.disabled = !!exact[0].disabled;
                out.status = out.disabled ? 'DISABLED' : 'FOUND';
            }

            // Record pager strip text if a compact bar contains 上一页/下一页.
            for (const el of nodes) {
                const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                if (!t || t.length > 80) continue;
                if (!(t.includes('下一页') && (t.includes('上一页') || /共\\s*\\d+\\s*页/.test(t)))) {
                    continue;
                }
                const r = el.getBoundingClientRect();
                if (r.height > 100 || r.width < 80) continue;
                out.pager_text = t;
                const mTotal = t.match(/共\\s*(\\d+)\\s*页/);
                if (mTotal) out.total_pages_hint = parseInt(mTotal[1], 10);
                // Heuristic: digit immediately after 上一页 is often current on JD strip.
                const mCur = t.match(/上一页\\s*(\\d+)/);
                if (mCur) out.current_page_hint = parseInt(mCur[1], 10);
                break;
            }
            if (!out.control) out.notes.push('未观察到可点的叶子「下一页」控件');
            return out;
        }"""
    )


def click_observed_next_page(page: Page, control: dict[str, Any]) -> dict[str, Any]:
    """Click the observed next-page control after scrolling it into view."""
    info: dict[str, Any] = {"ok": False, "error": None, "method": None}
    try:
        page.evaluate(
            """(c) => {
                const nodes = Array.from(
                    document.querySelectorAll('a, button, span, div, li, em, i')
                );
                let target = null;
                for (const el of nodes) {
                    const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                    if (t !== '下一页') continue;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.width > 220 || r.height > 80) {
                        continue;
                    }
                    const cls = typeof el.className === 'string' ? el.className : '';
                    if (c && c.className && cls === c.className) {
                        target = el;
                        break;
                    }
                    if (!target) target = el;
                }
                if (target) target.scrollIntoView({block: 'center', inline: 'nearest'});
                return !!target;
            }""",
            {
                "className": (control or {}).get("className"),
            },
        )
        page.wait_for_timeout(400)
        # Re-measure after scroll
        disc = discover_next_page_control(page)
        ctrl = disc.get("control") or control
        if not ctrl:
            info["error"] = "next_control_missing_after_scroll"
            return info
        if disc.get("disabled") or ctrl.get("disabled"):
            info["error"] = "next_control_disabled"
            info["disabled"] = True
            return info
        cx = float(ctrl["x"]) + float(ctrl["width"]) / 2
        cy = float(ctrl["y"]) + float(ctrl["height"]) / 2
        page.mouse.click(cx, cy)
        info["ok"] = True
        info["method"] = "mouse_click_observed_next"
        info["clicked"] = {
            "x": cx,
            "y": cy,
            "className": ctrl.get("className"),
            "text": ctrl.get("text"),
        }
        return info
    except PlaywrightError as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info


def wait_for_search_batch_progress(
    page: Page,
    *,
    before_ids: set[str],
    before_url: str,
    before_page_hint: int | None,
    timeout_ms: int = PAGE_PROGRESS_TIMEOUT_MS,
) -> dict[str, Any]:
    """
    Wait until an explicit progress signal after a load action.
    Click-complete / HTTP 200 / scroll-alone are NOT success.
    """
    started = utc_now()
    deadline = started.timestamp() + (timeout_ms / 1000.0)
    last_ids: list[str] = []
    while utc_now().timestamp() < deadline:
        restriction = detect_search_restriction(page)
        if restriction:
            return {
                "progressed": False,
                "reason": "SEARCH_RESTRICTED",
                "restriction": restriction,
                "ids": collect_visible_product_ids(page),
                "elapsed_ms": int((utc_now() - started).total_seconds() * 1000),
            }
        diag = diagnose_page_state(page)
        if diag in {
            STATUS_LOGIN_REQUIRED,
            STATUS_VERIFICATION_REQUIRED,
            STATUS_SEARCH_RESTRICTED,
        }:
            return {
                "progressed": False,
                "reason": diag,
                "ids": collect_visible_product_ids(page),
                "elapsed_ms": int((utc_now() - started).total_seconds() * 1000),
            }
        last_ids = collect_visible_product_ids(page)
        new_ids = [i for i in last_ids if i not in before_ids]
        pager = discover_next_page_control(page)
        page_hint = pager.get("current_page_hint")
        url_changed = page.url != before_url
        page_changed = (
            before_page_hint is not None
            and page_hint is not None
            and int(page_hint) != int(before_page_hint)
        )
        if new_ids:
            return {
                "progressed": True,
                "reason": "NEW_PRODUCT_IDS",
                "new_ids": new_ids,
                "ids": last_ids,
                "url_changed": url_changed,
                "page_hint_before": before_page_hint,
                "page_hint_after": page_hint,
                "pager": pager,
                "elapsed_ms": int((utc_now() - started).total_seconds() * 1000),
            }
        if page_changed:
            return {
                "progressed": True,
                "reason": "PAGE_LABEL_CHANGED",
                "new_ids": new_ids,
                "ids": last_ids,
                "url_changed": url_changed,
                "page_hint_before": before_page_hint,
                "page_hint_after": page_hint,
                "pager": pager,
                "elapsed_ms": int((utc_now() - started).total_seconds() * 1000),
            }
        # Explicit end-of-results copy
        try:
            tail = collect_visible_text(page, max_chars=3000)[-600:]
        except PlaywrightError:
            tail = ""
        if any(x in tail for x in ("没有更多", "已到底", "没有找到")):
            return {
                "progressed": False,
                "reason": "NO_MORE_RESULTS",
                "ids": last_ids,
                "elapsed_ms": int((utc_now() - started).total_seconds() * 1000),
            }
        page.wait_for_timeout(PAGE_PROGRESS_POLL_MS)

    return {
        "progressed": False,
        "reason": "TIMEOUT_NO_PROGRESS",
        "new_ids": [i for i in last_ids if i not in before_ids],
        "ids": last_ids,
        "url_changed": page.url != before_url,
        "elapsed_ms": int((utc_now() - started).total_seconds() * 1000),
    }


def extract_raw_candidates(page: Page) -> dict[str, Any]:
    """
    Auto-discover product candidates on the rendered search page.

    Supports:
    - Classic item.jd.com / item.m.jd.com anchors (legacy list UI)
    - New search cards: div[data-sku] (observed 2026-10; may lack item hrefs)
    """
    return page.evaluate(
        """() => {
            const abs = (href) => {
                try { return new URL(href, location.href).href; }
                catch (e) { return href || ''; }
            };
            const isForbiddenZone = (el) => {
                let n = el;
                for (let i = 0; i < 10 && n; i++) {
                    const id = (n.id || '').toLowerCase();
                    const cls = (typeof n.className === 'string' ? n.className : '').toLowerCase();
                    const tag = (n.tagName || '').toLowerCase();
                    const roles = ((n.getAttribute && n.getAttribute('role')) || '').toLowerCase();
                    if (
                        tag === 'header' || tag === 'nav' || tag === 'footer'
                        || roles === 'navigation' || roles === 'banner'
                        || id.includes('search-bar') || id.includes('header')
                        || cls.includes('nav') && cls.includes('top')
                        || cls.includes('m-cart') || id.includes('cart')
                    ) {
                        return true;
                    }
                    n = n.parentElement;
                }
                return false;
            };

            const anchors = Array.from(
                document.querySelectorAll(
                    'a[href*="item.jd.com"], a[href*="item.m.jd.com/product/"]'
                )
            );

            const rows = [];
            const seenSku = new Set();

            for (const a of anchors) {
                if (isForbiddenZone(a)) continue;
                const href = abs(a.getAttribute('href') || '');
                if (!href) continue;
                // Skip obvious non-product paths
                if (/passport|cart|order|login|mall\\.jd\\.com\\/index/i.test(href)) continue;

                let card = a.closest('li')
                    || a.closest('[class*="gl-item"]')
                    || a.closest('[data-sku]')
                    || a.parentElement;
                let hops = 0;
                while (card && hops < 6) {
                    const r = card.getBoundingClientRect();
                    if (r.height >= 40 && r.width >= 80) break;
                    card = card.parentElement;
                    hops += 1;
                }
                const cardText = ((card && card.innerText) || a.innerText || '')
                    .replace(/\\s+/g, ' ').trim();
                const titleAttr = (a.getAttribute('title') || '').trim();
                const titleText = (a.innerText || '').replace(/\\s+/g, ' ').trim();
                let title = titleAttr || titleText;
                if (!title || title.length < 4) {
                    const lines = cardText.split(' ').filter((x) => x.length >= 6);
                    title = lines[0] || titleText || titleAttr || '';
                }

                let shop = null;
                if (card) {
                    const shopEl = card.querySelector(
                        '[class*="shop"], [class*="store"], a[href*="mall.jd.com"]'
                    );
                    if (shopEl) {
                        const s = (shopEl.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (s && s.length <= 40) shop = s;
                    }
                }

                const ad_marked = /广告|推广|ad\\b/i.test(cardText.slice(0, 80))
                    || !!(card && card.querySelector('[class*="ad"], [class*="AD"]'));

                const rect = a.getBoundingClientRect();
                const skuFromCard = card && card.getAttribute
                    ? (card.getAttribute('data-sku') || '')
                    : '';
                if (skuFromCard) seenSku.add(skuFromCard);
                rows.push({
                    href,
                    product_id_hint: skuFromCard || null,
                    source: 'item_anchor',
                    title: title.slice(0, 200),
                    shop,
                    ad_marked,
                    in_forbidden_zone: false,
                    y: rect.top + window.scrollY,
                    visible: !!(rect.width || rect.height),
                });
            }

            // New JD search UI: goods cards expose data-sku without item.jd.com anchors.
            const cards = Array.from(document.querySelectorAll('[data-sku]'));
            for (const card of cards) {
                if (isForbiddenZone(card)) continue;
                const sku = (card.getAttribute('data-sku') || '').trim();
                if (!sku || !/^\\d{5,}$/.test(sku)) continue;
                if (seenSku.has(sku)) continue;
                seenSku.add(sku);

                const rect = card.getBoundingClientRect();
                if (!(rect.width || rect.height)) continue;

                const cardText = (card.innerText || '').replace(/\\s+/g, ' ').trim();
                let title = '';
                const titled = card.querySelector('[title]');
                if (titled) {
                    title = (titled.getAttribute('title') || '').trim();
                }
                if (!title || title.length < 4) {
                    // Drop leading 广告 marker; take a long-enough phrase as title.
                    const cleaned = cardText.replace(/^广告\\s*/, '');
                    const parts = cleaned.split(/\\s+/).filter((x) => x.length >= 4);
                    // Prefer a part that looks like a product name (has CJK or model).
                    title = parts.find((p) => /[\\u4e00-\\u9fff]|B\\d/i.test(p))
                        || parts[0]
                        || cleaned.slice(0, 80);
                }

                let shop = null;
                const shopEl = card.querySelector(
                    '[class*="shop"], [class*="store"], a[href*="mall.jd.com"]'
                );
                if (shopEl) {
                    const s = (shopEl.innerText || '').replace(/\\s+/g, ' ').trim();
                    if (s && s.length <= 40) shop = s;
                }
                if (!shop) {
                    // Shop name is often the last short token on the card.
                    const tokens = cardText.split(' ').filter(Boolean);
                    const last = tokens[tokens.length - 1] || '';
                    if (
                        last.length >= 2
                        && last.length <= 30
                        && /店|旗舰|专营|自营/.test(last)
                    ) {
                        shop = last;
                    }
                }

                const ad_marked = /^广告/.test(cardText)
                    || /广告|推广/.test(cardText.slice(0, 12));

                rows.push({
                    // Construct desktop detail URL from observed numeric sku on the card.
                    href: 'https://item.jd.com/' + sku + '.html',
                    product_id_hint: sku,
                    source: 'data_sku_card',
                    title: String(title || '').slice(0, 200),
                    shop,
                    ad_marked,
                    in_forbidden_zone: false,
                    y: rect.top + window.scrollY,
                    visible: true,
                });
            }

            rows.sort((a, b) => a.y - b.y);
            return {
                anchor_count_raw: anchors.length,
                data_sku_card_count: cards.length,
                candidate_rows: rows,
                page_title: document.title || '',
                final_url: location.href,
            };
        }"""
    )


def build_candidate_records(
    rows: list[dict[str, Any]],
    *,
    query: str,
    limit: int,
    source_url: str,
    load_batch: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    classified: list[dict[str, Any]] = []
    rejected = 0
    pending = 0
    for row in rows:
        info = classify_detail_href(str(row.get("href") or ""))
        if info["kind"] == "rejected":
            rejected += 1
            continue
        if info["kind"] == "pending_query_id":
            pending += 1
            # Keep only if we have a product_id from query; still not normalized.
            if not info.get("product_id"):
                continue
        title = str(row.get("title") or "")
        # ad_marked from same card only; False means「未发现标识」not「确认非广告」
        if "ad_marked" not in row or row.get("ad_marked") is None:
            ad_marked = None
            ad_status = "广告标识未知，不能写 false"
        else:
            ad_marked = bool(row.get("ad_marked"))
            ad_status = (
                "页面有广告标识"
                if ad_marked
                else "未发现广告标识，不能断言非广告"
            )
        source = str(row.get("source") or "item_anchor")
        normalize_status = info.get("normalize_status")
        if source == "data_sku_card" and info.get("product_id"):
            # Page had numeric data-sku; detail href was constructed (no item anchor).
            normalize_status = "constructed_from_data_sku"
        # Prefer explicit data-sku when present and consistent.
        product_id = info.get("product_id")
        hint = row.get("product_id_hint")
        if hint and str(hint).isdigit():
            if not product_id:
                product_id = str(hint)
            elif str(product_id) != str(hint):
                # Keep classified id from href; record mismatch only via status.
                normalize_status = f"{normalize_status};data_sku_mismatch"
        record = {
            "query": query,
            "rank": None,  # filled after dedupe
            "title": title,
            "product_id": product_id,
            "shop_name": row.get("shop"),
            "ad_marked": ad_marked,
            "ad_status": ad_status,
            "observed_url": info.get("observed_url"),
            "normalized_url": info.get("normalized_url")
            or (
                f"https://item.jd.com/{product_id}.html"
                if source == "data_sku_card" and product_id
                else None
            ),
            "normalize_status": normalize_status,
            "discovery_source": source,
            "source_search_url": sanitize_source_search_url(source_url),
            "discovered_at_utc": utc_iso(),
            "match_status": match_status_for_title(title, query),
            "price": None,
            "first_seen_load_batch": load_batch,
        }
        classified.append(record)

    deduped = dedupe_candidates(classified, limit=limit)
    for i, item in enumerate(deduped, start=1):
        item["rank"] = i

    stats = {
        "raw_row_count": len(rows),
        "accepted_before_dedupe": len(classified),
        "rejected_link_count": rejected,
        "pending_unnormalized_count": pending,
        "deduped_count": len(deduped),
    }
    return deduped, stats


def merge_first_seen_candidates(
    accumulated: list[dict[str, Any]],
    batch_records: list[dict[str, Any]],
    *,
    max_candidates: int,
    load_batch: int,
) -> tuple[list[dict[str, Any]], int]:
    """Keep first rank/appearance by JD product id; re-number global ranks."""
    seen = {
        str(c.get("product_id"))
        for c in accumulated
        if c.get("product_id")
    }
    added = 0
    for rec in batch_records:
        if len(accumulated) >= max_candidates:
            break
        pid = str(rec.get("product_id") or "")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        item = dict(rec)
        item["first_seen_load_batch"] = load_batch
        accumulated.append(item)
        added += 1
    for i, item in enumerate(accumulated, start=1):
        item["rank"] = i
    return accumulated, added


def collect_candidates_across_pages(
    page: Page,
    *,
    query: str,
    max_candidates: int,
    max_pages: int,
    search_deadline: float,
    log: Any = print,
) -> dict[str, Any]:
    """
    Extract + optional next-page loads within budgets.
    Mechanism (observed 2026-10): desktop SERP has real 「下一页」 pager
    (e.g. 「上一页 1 2 … 下一页 共12页」), not load-more.
    Progress requires new product IDs or page-label change.
    """
    candidates: list[dict[str, Any]] = []
    load_batches: list[dict[str, Any]] = []
    scroll_total = 0
    raw_cards_observed = 0
    pages_confirmed: int | None = None
    stop_reason = STOP_HIT_PAGE_BUDGET
    related_reasons: list[str] = []
    run_status = STATUS_PARTIAL
    status_reason = "in_progress"
    restriction_info: dict[str, Any] | None = None
    incomplete_due_to_restriction = False
    pagination_mechanism = "observed_next_page_control"
    extract_stats: dict[str, Any] = {}

    def timed_out() -> bool:
        return utc_now().timestamp() >= search_deadline

    def extract_batch(batch_index: int) -> dict[str, Any]:
        nonlocal raw_cards_observed, extract_stats, restriction_info
        nonlocal run_status, status_reason, incomplete_due_to_restriction
        batch_meta: dict[str, Any] = {
            "load_batch": batch_index,
            "url": sanitize_source_search_url(page.url),
            "new_id_count": 0,
            "unique_after": len(candidates),
        }
        if timed_out():
            batch_meta["stopped"] = STOP_SEARCH_TIMEOUT
            return {"stop": STOP_SEARCH_TIMEOUT, "batch": batch_meta}

        restriction_info = detect_search_restriction(page)
        if restriction_info:
            batch_meta["stopped"] = STOP_SEARCH_RESTRICTED
            return {"stop": STOP_SEARCH_RESTRICTED, "batch": batch_meta}

        try:
            raw = extract_raw_candidates(page)
        except PlaywrightError as exc:
            batch_meta["error"] = f"{type(exc).__name__}: {exc}"
            return {"stop": STOP_EXTRACTION_FAILED, "batch": batch_meta}

        cards = int(raw.get("data_sku_card_count") or 0)
        anchors = int(raw.get("anchor_count_raw") or 0)
        raw_cards_observed += max(cards, anchors)
        rows = list(raw.get("candidate_rows") or [])
        batch_records, stats = build_candidate_records(
            rows,
            query=query,
            limit=max_candidates,
            source_url=page.url,
            load_batch=batch_index,
        )
        extract_stats = {
            **extract_stats,
            **{f"batch{batch_index}_{k}": v for k, v in stats.items()},
            "anchor_count_raw_last": anchors,
            "data_sku_card_count_last": cards,
            "page_title": raw.get("page_title"),
        }
        before = len(candidates)
        merge_first_seen_candidates(
            candidates,
            batch_records,
            max_candidates=max_candidates,
            load_batch=batch_index,
        )
        added = len(candidates) - before
        batch_meta["new_id_count"] = added
        batch_meta["unique_after"] = len(candidates)
        batch_meta["raw_rows"] = len(rows)
        pager = discover_next_page_control(page)
        batch_meta["pager"] = {
            "status": pager.get("status"),
            "pager_text": pager.get("pager_text"),
            "current_page_hint": pager.get("current_page_hint"),
            "total_pages_hint": pager.get("total_pages_hint"),
            "disabled": pager.get("disabled"),
        }
        return {"stop": None, "batch": batch_meta, "pager": pager, "raw": raw}

    # --- Batch 0: wait + in-page lazy scroll + extract ---
    wait_info = wait_for_search_content(page)
    log(
        f"结果等待: outcome={wait_info.get('wait_outcome')} "
        f"data_sku={wait_info.get('data_sku_count_after_wait')}"
    )
    if detect_search_restriction(page):
        restriction_info = detect_search_restriction(page)
        stop_reason = STOP_SEARCH_RESTRICTED
        run_status = STATUS_SEARCH_RESTRICTED
        status_reason = "visible_search_frequency_block_after_wait"
        return _coverage_result(
            candidates,
            load_batches,
            scroll_total,
            raw_cards_observed,
            pages_confirmed,
            stop_reason,
            related_reasons,
            run_status,
            status_reason,
            restriction_info,
            incomplete_due_to_restriction,
            pagination_mechanism,
            extract_stats,
            wait_info,
            max_candidates,
            max_pages,
        )
    if wait_info.get("wait_outcome") == "verification":
        stop_reason = STOP_VERIFICATION_REQUIRED
        run_status = STATUS_VERIFICATION_REQUIRED
        status_reason = "verification_after_wait"
        return _coverage_result(
            candidates,
            load_batches,
            scroll_total,
            raw_cards_observed,
            pages_confirmed,
            stop_reason,
            related_reasons,
            run_status,
            status_reason,
            restriction_info,
            incomplete_due_to_restriction,
            pagination_mechanism,
            extract_stats,
            wait_info,
            max_candidates,
            max_pages,
        )

    scroll_info = limited_scroll_for_lazy_load(page)
    scroll_info["result_wait"] = wait_info
    scroll_total += int(scroll_info.get("scroll_count") or 0)
    if scroll_info.get("restriction"):
        restriction_info = scroll_info["restriction"]
        stop_reason = STOP_SEARCH_RESTRICTED
        run_status = STATUS_SEARCH_RESTRICTED
        status_reason = str(scroll_info.get("stopped_reason") or stop_reason)
        incomplete_due_to_restriction = False
        return _coverage_result(
            candidates,
            load_batches,
            scroll_total,
            raw_cards_observed,
            pages_confirmed,
            stop_reason,
            related_reasons,
            run_status,
            status_reason,
            restriction_info,
            incomplete_due_to_restriction,
            pagination_mechanism,
            extract_stats,
            scroll_info,
            max_candidates,
            max_pages,
        )

    r0 = extract_batch(0)
    load_batches.append(r0["batch"])
    if r0.get("pager") and r0["pager"].get("current_page_hint") is not None:
        pages_confirmed = 1
    elif r0.get("batch", {}).get("new_id_count", 0) > 0 or candidates:
        # First scope loaded; page index not confirmed from UI → null unless hint.
        pages_confirmed = 1 if (r0.get("pager") or {}).get("pager_text") else None

    if r0.get("stop"):
        stop_reason = r0["stop"]
        if stop_reason == STOP_SEARCH_RESTRICTED:
            run_status = STATUS_SEARCH_RESTRICTED
            restriction_info = detect_search_restriction(page) or restriction_info
        elif stop_reason == STOP_EXTRACTION_FAILED:
            run_status = STATUS_EXTRACTION_FAILED
        status_reason = stop_reason
        return _coverage_result(
            candidates,
            load_batches,
            scroll_total,
            raw_cards_observed,
            pages_confirmed,
            stop_reason,
            related_reasons,
            run_status,
            status_reason,
            restriction_info,
            incomplete_due_to_restriction,
            pagination_mechanism,
            extract_stats,
            scroll_info,
            max_candidates,
            max_pages,
        )

    if not candidates:
        again = diagnose_page_state(page)
        raw = r0.get("raw") or {}
        if again == STATUS_NO_RESULTS:
            stop_reason = STOP_NO_RESULTS
            run_status = STATUS_NO_RESULTS
            status_reason = "explicit_no_results_copy"
        elif int(raw.get("anchor_count_raw") or 0) > 0 or int(
            raw.get("data_sku_card_count") or 0
        ) > 0:
            stop_reason = STOP_EXTRACTION_FAILED
            run_status = STATUS_EXTRACTION_FAILED
            status_reason = "cards_present_but_no_reliable_candidates"
        else:
            stop_reason = STOP_EXTRACTION_FAILED
            run_status = STATUS_EXTRACTION_FAILED
            status_reason = "zero_product_signals"
        return _coverage_result(
            candidates,
            load_batches,
            scroll_total,
            raw_cards_observed,
            pages_confirmed,
            stop_reason,
            related_reasons,
            run_status,
            status_reason,
            restriction_info,
            incomplete_due_to_restriction,
            pagination_mechanism,
            extract_stats,
            scroll_info,
            max_candidates,
            max_pages,
        )

    # --- Further pages via observed 「下一页」 ---
    pages_loaded = 1  # initial scope counts as first page/batch
    while len(candidates) < max_candidates and pages_loaded < max_pages:
        if timed_out():
            stop_reason = STOP_SEARCH_TIMEOUT
            related_reasons.append(STOP_HIT_PAGE_BUDGET if pages_loaded >= max_pages else "")
            related_reasons = [r for r in related_reasons if r]
            status_reason = "search_budget_timeout"
            run_status = STATUS_PARTIAL if candidates else STATUS_EXTRACTION_FAILED
            break

        pager = discover_next_page_control(page)
        if pager.get("status") == "DISABLED" or pager.get("disabled"):
            stop_reason = STOP_NO_MORE_RESULTS
            status_reason = "next_page_disabled"
            run_status = (
                STATUS_SUCCESS
                if len(candidates) >= max_candidates
                else STATUS_PARTIAL
            )
            break
        if pager.get("status") != "FOUND" or not pager.get("control"):
            # No clear next control — do not invent URL pagination.
            stop_reason = STOP_NO_MORE_RESULTS
            status_reason = "no_observed_next_page_control"
            pagination_mechanism = "no_next_control_after_batch0"
            run_status = STATUS_PARTIAL
            break

        before_ids = set(collect_visible_product_ids(page))
        before_url = page.url
        before_hint = pager.get("current_page_hint")
        log(
            f"翻页: 点击观察到的「下一页」 "
            f"(batch={pages_loaded} → {pages_loaded + 1}, "
            f"pager={pager.get('pager_text')!r})"
        )
        click_info = click_observed_next_page(page, pager.get("control") or {})
        if click_info.get("disabled"):
            stop_reason = STOP_NO_MORE_RESULTS
            status_reason = "next_disabled_at_click"
            run_status = STATUS_PARTIAL
            break
        if not click_info.get("ok"):
            stop_reason = STOP_LOAD_FAILED
            status_reason = f"next_click_failed:{click_info.get('error')}"
            related_reasons.append(STOP_NO_PROGRESS)
            run_status = STATUS_PARTIAL
            break

        progress = wait_for_search_batch_progress(
            page,
            before_ids=before_ids,
            before_url=before_url,
            before_page_hint=before_hint
            if isinstance(before_hint, int)
            else None,
        )
        log(
            f"翻页进展: progressed={progress.get('progressed')} "
            f"reason={progress.get('reason')} "
            f"new_ids={len(progress.get('new_ids') or [])}"
        )
        if progress.get("reason") in {
            STOP_SEARCH_RESTRICTED,
            STATUS_SEARCH_RESTRICTED,
        }:
            restriction_info = progress.get("restriction") or detect_search_restriction(
                page
            )
            stop_reason = STOP_SEARCH_RESTRICTED
            run_status = STATUS_SEARCH_RESTRICTED
            status_reason = "restricted_after_next_click"
            incomplete_due_to_restriction = bool(candidates)
            break
        if progress.get("reason") in {
            STOP_LOGIN_REQUIRED,
            STATUS_LOGIN_REQUIRED,
            STOP_VERIFICATION_REQUIRED,
            STATUS_VERIFICATION_REQUIRED,
        }:
            stop_reason = str(progress.get("reason"))
            run_status = stop_reason
            status_reason = "blocked_after_next_click"
            incomplete_due_to_restriction = bool(candidates)
            break
        if progress.get("reason") == "NO_MORE_RESULTS":
            stop_reason = STOP_NO_MORE_RESULTS
            status_reason = "explicit_no_more_after_next"
            run_status = STATUS_PARTIAL
            break
        if not progress.get("progressed"):
            stop_reason = STOP_NO_PROGRESS
            status_reason = f"next_click_no_progress:{progress.get('reason')}"
            run_status = STATUS_PARTIAL
            related_reasons.append("avoid_dead_loop_repeat_page")
            break

        pages_loaded += 1
        if progress.get("page_hint_after") is not None:
            pages_confirmed = int(progress["page_hint_after"])
        elif pages_confirmed is not None:
            pages_confirmed = pages_loaded
        else:
            # Label unknown but batch advanced via new IDs — record loads, not fake pages.
            pages_confirmed = pages_loaded if progress.get("reason") == "PAGE_LABEL_CHANGED" else pages_confirmed

        rN = extract_batch(pages_loaded - 1)
        load_batches.append(rN["batch"])
        if rN.get("stop") == STOP_SEARCH_RESTRICTED:
            restriction_info = detect_search_restriction(page)
            stop_reason = STOP_SEARCH_RESTRICTED
            run_status = STATUS_SEARCH_RESTRICTED
            incomplete_due_to_restriction = bool(candidates)
            status_reason = "restricted_during_batch_extract"
            break
        if rN.get("stop") == STOP_EXTRACTION_FAILED:
            stop_reason = STOP_EXTRACTION_FAILED
            run_status = STATUS_EXTRACTION_FAILED
            status_reason = "extract_failed_on_later_batch"
            incomplete_due_to_restriction = bool(candidates)
            break
        if rN["batch"].get("new_id_count", 0) == 0 and progress.get("reason") != "PAGE_LABEL_CHANGED":
            # Progress claimed but no new unique IDs after merge → stop bounded.
            stop_reason = STOP_NO_PROGRESS
            status_reason = "repeat_candidates_no_new_unique"
            related_reasons.append("duplicate_page_or_ids")
            run_status = STATUS_PARTIAL
            break

    else:
        # while conditions ended without break
        if len(candidates) >= max_candidates:
            stop_reason = STOP_HIT_CANDIDATE_LIMIT
            status_reason = "collected_max_candidates"
            run_status = STATUS_SUCCESS
        elif pages_loaded >= max_pages:
            stop_reason = STOP_HIT_PAGE_BUDGET
            status_reason = "hit_max_pages_or_load_batches"
            run_status = STATUS_PARTIAL if candidates else STATUS_EXTRACTION_FAILED
            related_reasons.append(f"pages_loaded={pages_loaded}")

    if len(candidates) >= max_candidates and stop_reason not in {
        STOP_SEARCH_RESTRICTED,
        STOP_LOGIN_REQUIRED,
        STOP_VERIFICATION_REQUIRED,
    }:
        stop_reason = STOP_HIT_CANDIDATE_LIMIT
        run_status = STATUS_SUCCESS
        status_reason = status_reason or "collected_max_candidates"
    elif (
        stop_reason == STOP_HIT_PAGE_BUDGET
        and len(candidates) < max_candidates
        and candidates
    ):
        run_status = STATUS_PARTIAL

    return _coverage_result(
        candidates,
        load_batches,
        scroll_total,
        raw_cards_observed,
        pages_confirmed,
        stop_reason,
        related_reasons,
        run_status,
        status_reason,
        restriction_info,
        incomplete_due_to_restriction,
        pagination_mechanism,
        extract_stats,
        scroll_info,
        max_candidates,
        max_pages,
        pages_loaded=pages_loaded,
    )


def _coverage_result(
    candidates: list[dict[str, Any]],
    load_batches: list[dict[str, Any]],
    scroll_total: int,
    raw_cards_observed: int,
    pages_confirmed: int | None,
    stop_reason: str,
    related_reasons: list[str],
    run_status: str,
    status_reason: str,
    restriction_info: dict[str, Any] | None,
    incomplete_due_to_restriction: bool,
    pagination_mechanism: str,
    extract_stats: dict[str, Any],
    scroll_info: dict[str, Any],
    max_candidates: int,
    max_pages: int,
    pages_loaded: int | None = None,
) -> dict[str, Any]:
    return {
        "candidates": candidates,
        "load_batches": load_batches,
        "scroll_count": scroll_total,
        "raw_cards_observed": raw_cards_observed,
        "unique_candidates_collected": len(candidates),
        "pages_confirmed": pages_confirmed,
        "pages_loaded": pages_loaded
        if pages_loaded is not None
        else len(load_batches),
        "stop_reason": stop_reason,
        "related_stop_reasons": [r for r in related_reasons if r],
        "run_status": run_status,
        "status_reason": status_reason,
        "restriction_info": restriction_info,
        "incomplete_due_to_restriction": incomplete_due_to_restriction,
        "pagination_mechanism": pagination_mechanism,
        "extract_stats": extract_stats,
        "scroll": scroll_info,
        "budget": {
            "max_candidates": max_candidates,
            "max_pages": max_pages,
        },
        "coverage_note": (
            "本次搜索范围内有限页/加载批次收集；"
            "不是全京东链接、不是全平台最低价。"
            f"分页机制观察为：{pagination_mechanism}。"
        ),
    }


def write_candidates_json(path: Path, candidates: list[dict[str, Any]]) -> None:
    path.write_text(
        json.dumps(candidates, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_candidates_csv(path: Path, candidates: list[dict[str, Any]]) -> None:
    fields = [
        "query",
        "rank",
        "title",
        "product_id",
        "shop_name",
        "ad_status",
        "observed_url",
        "normalized_url",
        "normalize_status",
        "discovery_source",
        "source_search_url",
        "discovered_at_utc",
        "match_status",
        "price",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in candidates:
            safe = {k: csv_guard(row.get(k)) for k in fields}
            writer.writerow(safe)


def write_metadata(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PriceRadar P3-A: JD keyword search candidate collection"
    )
    parser.add_argument("--query", required=True, help="Search keyword (not a JD numeric SKU id)")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Legacy alias of --max-candidates (compat)",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=None,
        help=f"Max unique candidates (default {DEFAULT_MAX_CANDIDATES}, cap {MAX_CANDIDATES_CAP})",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help=f"Max search pages / load batches (default {DEFAULT_MAX_PAGES})",
    )
    parser.add_argument(
        "--search-timeout-seconds",
        type=int,
        default=None,
        help=f"Whole-search timeout seconds (default {DEFAULT_SEARCH_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--auto-close",
        action="store_true",
        help="Close browser without waiting for Enter (for non-interactive diagnose runs)",
    )
    return parser.parse_args(argv)


def validate_args(query: str, limit: int) -> None:
    """Legacy validator (limit-only). Prefer resolve_search_budget for Phase C."""
    if not query or not query.strip():
        raise ValueError("query 不能为空")
    if limit < 1 or limit > MAX_CANDIDATES_CAP:
        raise ValueError(f"limit 必须是 1..{MAX_CANDIDATES_CAP} 的正整数")


def run_local_logic_checks() -> list[str]:
    """Local unit checks (not live JD)."""
    failures: list[str] = []

    ok = classify_detail_href("//item.jd.com/100005149674.html")
    if ok["kind"] != "detail" or ok["product_id"] != "100005149674":
        failures.append("valid item.jd.com link failed")

    ok_m = classify_detail_href("https://item.m.jd.com/product/100005149674.html?a=1")
    if ok_m["normalized_url"] != "https://item.m.jd.com/product/100005149674.html":
        failures.append("m.jd product normalize failed")

    bad = classify_detail_href("https://cart.jd.com/cart.action")
    if bad["kind"] != "rejected":
        failures.append("cart link should be rejected")

    pending = classify_detail_href("https://item.jd.com/ware?skuId=12345")
    if pending["kind"] != "pending_query_id" or pending["normalized_url"] is not None:
        failures.append("query-id link must not be blindly normalized")

    rows = [
        {
            "title": "清风擦手纸 B913A 200抽",
            "href": "https://item.jd.com/111.html",
            "shop": "店A",
            "ad_marked": False,
        },
        {
            "title": "清风擦手纸 B913A 200抽",
            "href": "https://item.jd.com/111.html?spm=1",
            "shop": "店A",
            "ad_marked": False,
        },
        {
            "title": "其他抽纸",
            "href": "https://item.jd.com/222.html",
            "shop": None,
            "ad_marked": True,
        },
        {
            "title": "无效",
            "href": "https://example.com/x",
            "shop": None,
            "ad_marked": False,
        },
    ]
    # classify via build
    classified, stats = build_candidate_records(
        rows, query="清风 B913A", limit=5, source_url=build_search_url("清风 B913A")
    )
    if len(classified) != 2:
        failures.append(f"dedupe expected 2, got {len(classified)}")
    if classified[0]["product_id"] != "111":
        failures.append("rank1 id wrong")
    if "型号关键词命中" not in classified[0]["match_status"]:
        failures.append("B913A match status wrong")
    if classified[1]["price"] is not None:
        failures.append("price must be null")

    if csv_guard("=cmd") != "'=cmd":
        failures.append("csv guard failed for =")
    if csv_guard("正常标题") != "正常标题":
        failures.append("csv guard false positive")

    # Ensure known sample id is not injected as fallback when empty
    empty, _ = build_candidate_records(
        [], query="清风 B913A", limit=5, source_url=build_search_url("清风 B913A")
    )
    if empty:
        failures.append("empty extraction must not invent candidates")

    # data-sku card path (constructed detail URL; not a hardcoded fallback list)
    sku_rows = [
        {
            "title": "清风擦手纸B913A 1层200张20包",
            "href": "https://item.jd.com/10124562600570.html",
            "shop": "清风清洁旗舰店",
            "ad_marked": False,
            "source": "data_sku_card",
            "product_id_hint": "10124562600570",
        }
    ]
    sku_classified, _ = build_candidate_records(
        sku_rows, query="清风 B913A", limit=5, source_url=build_search_url("清风 B913A")
    )
    if len(sku_classified) != 1:
        failures.append("data_sku_card row should yield one candidate")
    elif sku_classified[0].get("normalize_status") != "constructed_from_data_sku":
        failures.append("data_sku_card normalize_status wrong")
    elif sku_classified[0].get("product_id") != "10124562600570":
        failures.append("data_sku_card product_id wrong")
    elif sku_classified[0].get("discovery_source") != "data_sku_card":
        failures.append("discovery_source should be data_sku_card")
    elif sku_classified[0].get("price") is not None:
        failures.append("data_sku candidate price must be null")

    # Restriction tip matching (local text only; not live JD).
    tip = match_search_restricted_tip(
        "抱歉由于访问频繁导致无法搜索，请稍后再试！\n若长时间无法搜索可点此反馈"
    )
    if tip != "抱歉由于访问频繁导致无法搜索，请稍后再试":
        failures.append(f"expected primary restriction tip, got {tip!r}")

    tip2 = match_search_restricted_tip("清风抽纸 高频 频繁替换装 超值装")
    if tip2 is not None:
        failures.append("product title with 频繁 must not be SEARCH_RESTRICTED")

    tip3 = match_search_restricted_tip("访问频繁导致无法搜索，请稍后再试")
    if tip3 is None:
        failures.append("short restriction phrase should match")

    tip4 = match_search_restricted_tip("")
    if tip4 is not None:
        failures.append("empty text must not be restricted")

    # Phase C budget resolution + merge / dedupe across batches
    try:
        resolve_search_budget(
            limit=5,
            max_candidates=10,
            limit_explicit=True,
            max_candidates_explicit=True,
        )
        failures.append("limit vs max-candidates conflict should raise")
    except ValueError:
        pass

    b = resolve_search_budget(limit_explicit=False, max_candidates_explicit=False)
    if b["max_candidates"] != DEFAULT_MAX_CANDIDATES or b["max_pages"] != DEFAULT_MAX_PAGES:
        failures.append("defaults for max_candidates/pages wrong")

    b2 = resolve_search_budget(
        limit=5, limit_explicit=True, max_candidates_explicit=False
    )
    if b2["max_candidates"] != 5 or not b2["legacy_limit_mode"]:
        failures.append("legacy --limit alone should set max_candidates=5")

    acc: list[dict[str, Any]] = []
    batch1 = [
        {
            "product_id": "111",
            "title": "A",
            "query": "q",
            "ad_marked": False,
            "ad_status": "x",
            "price": None,
        },
        {
            "product_id": "222",
            "title": "B",
            "query": "q",
            "ad_marked": True,
            "ad_status": "x",
            "price": None,
        },
    ]
    batch2 = [
        {
            "product_id": "111",
            "title": "A-dup",
            "query": "q",
            "ad_marked": False,
            "ad_status": "x",
            "price": None,
        },
        {
            "product_id": "333",
            "title": "C",
            "query": "q",
            "ad_marked": False,
            "ad_status": "x",
            "price": None,
        },
    ]
    merge_first_seen_candidates(acc, batch1, max_candidates=10, load_batch=0)
    merge_first_seen_candidates(acc, batch2, max_candidates=10, load_batch=1)
    if [c["product_id"] for c in acc] != ["111", "222", "333"]:
        failures.append(f"cross-page dedupe order wrong: {acc}")
    if acc[0].get("title") != "A":
        failures.append("first-seen title must be kept on duplicate id")
    if acc[2].get("first_seen_load_batch") != 1:
        failures.append("new id should record later load batch")

    # Cap stops further merges
    acc2: list[dict[str, Any]] = []
    merge_first_seen_candidates(acc2, batch1, max_candidates=1, load_batch=0)
    _, added = merge_first_seen_candidates(
        acc2, batch2, max_candidates=1, load_batch=1
    )
    if len(acc2) != 1 or added != 0:
        failures.append("max_candidates cap should block further merges")

    return failures


def run_search_collect(
    query: str,
    limit: int | None = None,
    *,
    max_candidates: int | None = None,
    max_pages: int | None = None,
    search_timeout_seconds: int | None = None,
    auto_close: bool = True,
    out_dir: Path | None = None,
    profile_dir: Path | None = None,
    quiet: bool = False,
    limit_explicit: bool | None = None,
    max_candidates_explicit: bool | None = None,
) -> dict[str, Any]:
    """
    Library entry for keyword search → candidates (closes browser when done).

    Returns dict with candidates, run_status, metadata, out_dir, paths.
    Does not invent candidates or fall back to fixed product links.
    Search coverage budget is separate from detail verify budget (P5-B).
    """
    # Compat: positional/kw limit without Phase-C flags → treat as explicit max_candidates
    if limit_explicit is None:
        limit_explicit = limit is not None
    if max_candidates_explicit is None:
        max_candidates_explicit = max_candidates is not None
    budget = resolve_search_budget(
        limit=limit,
        max_candidates=max_candidates,
        max_pages=max_pages,
        search_timeout_seconds=search_timeout_seconds,
        limit_explicit=bool(limit_explicit),
        max_candidates_explicit=bool(max_candidates_explicit),
        max_pages_explicit=max_pages is not None,
        search_timeout_explicit=search_timeout_seconds is not None,
    )
    if not query or not str(query).strip():
        raise ValueError("query 不能为空")
    query = query.strip()
    max_cand = int(budget["max_candidates"])
    max_pg = int(budget["max_pages"])
    timeout_s = int(budget["search_timeout_seconds"])
    limit = max_cand  # compat field in metadata
    search_url = build_search_url(query)
    started = utc_now()
    search_deadline = started.timestamp() + timeout_s
    profile = (profile_dir or PROFILE_DIR).resolve()

    def _log(msg: str) -> None:
        if not quiet:
            print(msg)

    _log("=== PriceRadar P3-A ===")
    _log(f"解释器: {sys.executable}")
    _log(f"项目根: {PROJECT_ROOT}")
    _log(f"独立 Profile: {profile}")
    _log(f"Playwright: {playwright_version()}")
    _log(f"query: {query!r}（作搜索关键词；B913A 不当作京东数字商品ID）")
    _log(
        f"budget: max_candidates={max_cand} max_pages={max_pg} "
        f"search_timeout_seconds={timeout_s} (legacy limit字段={limit})"
    )
    _log(f"搜索方式: {SEARCH_ENTRY_STRATEGY}")
    _log("说明: 不使用固定商品链接兜底；HTTP 200≠搜索成功；点击翻页≠翻页成功。")
    _log("")

    if out_dir is None:
        run_id, out_dir = make_run_dir()
    else:
        out_dir = Path(out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        run_id = out_dir.name
    _log(f"run_id: {run_id}")
    _log(f"输出目录: {out_dir}")

    browser_started = False
    close_ok = False
    exit_code = 0
    run_status = STATUS_NAVIGATION_FAILED
    status_reason = "not_started"
    final_url = "UNKNOWN"
    scroll_info: dict[str, Any] = {}
    extract_stats: dict[str, Any] = {}
    candidates: list[dict[str, Any]] = []
    page_diagnosis: str | None = None
    restriction_info: dict[str, Any] | None = None
    incomplete_due_to_restriction = False
    http_status: int | None = None
    coverage_bundle: dict[str, Any] = {}
    coverage_stop_early: str | None = None

    try:
        with sync_playwright() as playwright:
            try:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile),
                    channel="chrome",
                    headless=False,
                    chromium_sandbox=True,
                )
            except PlaywrightError as exc:
                status_reason = f"browser_launch_failed: {type(exc).__name__}: {exc}"
                _log(f"浏览器启动失败: {status_reason}")
                raise

            browser_started = True
            _log("浏览器启动结果: 成功")

            try:
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    response = page.goto(
                        search_url,
                        wait_until="domcontentloaded",
                        timeout=NAVIGATION_TIMEOUT_MS,
                    )
                    http_status = response.status if response is not None else None
                    final_url = page.url
                    _log(
                        f"导航命令完成 http_status={http_status} "
                        f"final={sanitize_source_search_url(final_url)}"
                    )
                    _log("说明: HTTP 状态不等于搜索成功，需检查最终地址与页面内容。")
                except PlaywrightError as exc:
                    run_status = STATUS_NAVIGATION_FAILED
                    status_reason = f"{type(exc).__name__}: {exc}"
                    _log(f"导航失败: {status_reason}")
                    final_url = getattr(page, "url", "UNKNOWN")
                else:
                    restriction_info = detect_search_restriction(page)
                    if restriction_info:
                        run_status = STATUS_SEARCH_RESTRICTED
                        page_diagnosis = STATUS_SEARCH_RESTRICTED
                        status_reason = "visible_search_frequency_block"
                        _log(f"页面诊断阻塞状态: {run_status}")
                        _log(
                            f"观察到的限制提示: {restriction_info.get('observed_tip')!r} "
                            f"at {restriction_info.get('detected_at_utc')}"
                        )
                        _log(
                            "已停止候选提取与滚动；不刷新、不重试、不换入口。"
                            "不推断具体触发机制。"
                        )
                    else:
                        page_diagnosis = diagnose_page_state(page)
                        if page_diagnosis == STATUS_SEARCH_RESTRICTED:
                            restriction_info = detect_search_restriction(page)
                            run_status = STATUS_SEARCH_RESTRICTED
                            status_reason = "visible_search_frequency_block"
                            _log(f"页面诊断阻塞状态: {run_status}")
                        elif page_diagnosis:
                            run_status = page_diagnosis
                            status_reason = f"diagnose:{page_diagnosis}"
                            _log(f"页面诊断阻塞状态: {run_status}")
                        else:
                            host = urlsplit(final_url).netloc.lower()
                            if (
                                "search.jd.com" not in host
                                and "so.m.jd.com" not in host
                            ):
                                _log(
                                    f"警告: 最终主机 {host!r} 非预期搜索域；"
                                    "提取策略待真实验证。"
                                )

                            # Whole-search deadline (not per-page reset).
                            remaining = search_deadline - utc_now().timestamp()
                            if remaining <= 0:
                                run_status = STATUS_PARTIAL
                                status_reason = "search_timeout_before_extract"
                                coverage_stop_early = STOP_SEARCH_TIMEOUT
                                _log("搜索总超时：进入提取前已到期。")
                            else:
                                cov = collect_candidates_across_pages(
                                    page,
                                    query=query,
                                    max_candidates=max_cand,
                                    max_pages=max_pg,
                                    search_deadline=search_deadline,
                                    log=_log,
                                )
                                candidates = list(cov.get("candidates") or [])
                                extract_stats = dict(cov.get("extract_stats") or {})
                                scroll_info = dict(cov.get("scroll") or {})
                                scroll_info["scroll_count"] = cov.get("scroll_count")
                                run_status = str(
                                    cov.get("run_status") or STATUS_PARTIAL
                                )
                                status_reason = str(
                                    cov.get("status_reason") or ""
                                )
                                page_diagnosis = run_status if run_status in {
                                    STATUS_SEARCH_RESTRICTED,
                                    STATUS_LOGIN_REQUIRED,
                                    STATUS_VERIFICATION_REQUIRED,
                                    STATUS_NO_RESULTS,
                                } else page_diagnosis
                                restriction_info = cov.get("restriction_info")
                                incomplete_due_to_restriction = bool(
                                    cov.get("incomplete_due_to_restriction")
                                )
                                coverage_bundle = cov
                                _log(
                                    f"覆盖: unique={cov.get('unique_candidates_collected')} "
                                    f"pages_confirmed={cov.get('pages_confirmed')} "
                                    f"load_batches={len(cov.get('load_batches') or [])} "
                                    f"stop={cov.get('stop_reason')} "
                                    f"mechanism={cov.get('pagination_mechanism')}"
                                )

                _log("")
                _log(f"运行状态: {run_status}")
                _log(f"原因: {status_reason}")
                if restriction_info:
                    _log(
                        f"限制提示: {restriction_info.get('observed_tip')!r} "
                        f"检测时间(UTC): {restriction_info.get('detected_at_utc')}"
                    )
                if incomplete_due_to_restriction and candidates:
                    _log("说明: 限制出现前已有部分候选，本轮标记为不完整。")
                _log(f"候选数: {len(candidates)}")
                for c in candidates:
                    _log(
                        f"  #{c['rank']} id={c.get('product_id')} "
                        f"ad={c.get('ad_marked')} "
                        f"src={c.get('discovery_source')} "
                        f"title={str(c.get('title') or '')[:60]!r}"
                    )

                _log("")
                if auto_close:
                    _log("auto-close: 跳过回车，即将关闭浏览器。")
                else:
                    _log("开发验收: 可核对窗口中的页面与终端状态；按回车关闭。")
                    read_line("按回车结束本次自动化浏览器会话… ")
            except KeyboardInterrupt:
                _log("")
                _log("收到 Ctrl+C，正在关闭…")
                exit_code = 130
            finally:
                try:
                    context.close()
                    close_ok = True
                    _log("程序关闭结果: 上下文关闭完成")
                except PlaywrightError as exc:
                    _log(f"程序关闭结果: 异常 {type(exc).__name__}: {exc}")
                    raise
    except RuntimeError as exc:
        _log(f"运行中止: {type(exc).__name__}: {exc}")
        if run_status == STATUS_NAVIGATION_FAILED and status_reason == "not_started":
            status_reason = str(exc)
        exit_code = 2
    except PlaywrightError as exc:
        _log(f"Playwright 错误: {type(exc).__name__}: {exc}")
        status_reason = f"{type(exc).__name__}: {exc}"
        exit_code = 1

    ended = utc_now()
    cand_json = out_dir / "candidates.json"
    cand_csv = out_dir / "candidates.csv"
    meta_path = out_dir / "run_metadata.json"

    try:
        write_candidates_json(cand_json, candidates)
        write_candidates_csv(cand_csv, candidates)
    except OSError as exc:
        _log(f"候选文件写入失败: {type(exc).__name__}: {exc}")

    elapsed_seconds = (ended - started).total_seconds()
    # Prefer bundle stop reason; fall back to status mapping.
    coverage_stop_reason = (
        coverage_stop_early
        or coverage_bundle.get("stop_reason")
        or (
            STOP_HIT_CANDIDATE_LIMIT
            if run_status == STATUS_SUCCESS and len(candidates) >= max_cand
            else None
        )
        or (
            STOP_SEARCH_RESTRICTED
            if run_status == STATUS_SEARCH_RESTRICTED
            else None
        )
        or (
            STOP_LOGIN_REQUIRED
            if run_status == STATUS_LOGIN_REQUIRED
            else None
        )
        or (
            STOP_VERIFICATION_REQUIRED
            if run_status == STATUS_VERIFICATION_REQUIRED
            else None
        )
        or (STOP_NO_RESULTS if run_status == STATUS_NO_RESULTS else None)
        or (
            STOP_EXTRACTION_FAILED
            if run_status == STATUS_EXTRACTION_FAILED
            else None
        )
        or (
            STOP_NAVIGATION_FAILED
            if run_status == STATUS_NAVIGATION_FAILED
            else None
        )
        or f"STATUS:{run_status}"
    )

    pages_confirmed = coverage_bundle.get("pages_confirmed")
    metadata = {
        "run_id": run_id,
        "phase": "P3-A",
        "query": query,
        "limit": limit,
        "max_candidates": max_cand,
        "max_pages": max_pg,
        "search_timeout_seconds": timeout_s,
        "budget": {
            "max_candidates": max_cand,
            "max_pages": max_pg,
            "search_timeout_seconds": timeout_s,
        },
        "search_entry_strategy": SEARCH_ENTRY_STRATEGY,
        "search_entry_verification": "REAL_RUN_DATA_SKU_CARDS",
        "search_url_requested": search_url,
        "final_url_sanitized": (
            sanitize_source_search_url(final_url)
            if final_url not in {"UNKNOWN", ""}
            else final_url
        ),
        "http_status": http_status,
        "started_at_utc": utc_iso(started),
        "ended_at_utc": utc_iso(ended),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "browser_started": browser_started,
        "scroll": scroll_info,
        "scroll_count": coverage_bundle.get("scroll_count")
        or (scroll_info or {}).get("scroll_count"),
        "extract_stats": extract_stats,
        "run_status": run_status,
        "status_reason": status_reason,
        "search_status": run_status,
        "coverage_stop_reason": coverage_stop_reason,
        "stop_reason": coverage_stop_reason,
        "related_stop_reasons": coverage_bundle.get("related_stop_reasons") or [],
        "raw_cards_observed": coverage_bundle.get("raw_cards_observed"),
        "unique_candidates_collected": len(candidates),
        "pages_confirmed": pages_confirmed,
        "pages_visited": pages_confirmed
        if pages_confirmed is not None
        else None,
        "load_batches": coverage_bundle.get("load_batches") or [],
        "pages_loaded": coverage_bundle.get("pages_loaded"),
        "pagination_mechanism": coverage_bundle.get("pagination_mechanism"),
        "coverage_note": coverage_bundle.get("coverage_note")
        or (
            "本次搜索范围内有限页/加载批次收集；不是全京东、不是全平台最低价"
        ),
        "page_diagnosis": page_diagnosis,
        "search_restriction": restriction_info,
        "incomplete_due_to_restriction": incomplete_due_to_restriction,
        "candidate_count": len(candidates),
        "user_acceptance": "PENDING",
        "close_ok": close_ok,
        "notes": [
            "本轮仅候选收集；不做价格读取、AI、比价表",
            "不使用固定商品ID/链接作为搜索成功兜底",
            "B913A 仅作关键词；标题命中≠规格已核验",
            "SEARCH_RESTRICTED 表示页面可见搜索频繁限制提示；不推断触发机制",
            "新版搜索页可能无 item.jd.com 锚点，改为 data-sku 卡片发现并构造详情链接",
            "候选仅搜索结果，不代表已确认同款或最低价",
            "达到 max-candidates 属于覆盖限制，不等于全部商品",
            "pages_confirmed=null 表示未从页码控件确认页数；load_batches 仍如实记录",
            "翻页成功以新商品ID或页码变化为准；点击完成/HTTP200/滚动完成不算成功",
            "搜索超时为整次搜索预算，不是每页重置",
        ],
    }
    try:
        write_metadata(meta_path, metadata)
    except OSError as exc:
        _log(f"metadata 写入失败: {type(exc).__name__}: {exc}")

    _log("")
    _log("=== 输出文件 ===")
    _log(f"candidates.json: {cand_json.resolve()}")
    _log(f"candidates.csv: {cand_csv.resolve()}")
    _log(f"run_metadata.json: {meta_path.resolve()}")
    _log(f"run_status={run_status} candidates={len(candidates)}")
    if run_status == STATUS_SUCCESS:
        _log(
            "本轮候选收集在限定范围内完成；"
            "不代表全平台搜索、同款核验或最低价监控完成。"
        )
    elif run_status == STATUS_PARTIAL:
        _log("本轮仅部分候选；不代表搜索验收通过。")
    elif run_status == STATUS_SEARCH_RESTRICTED:
        _log(
            "搜索受限（可见频繁限制提示）。已停止提取/滚动；"
            "真实自动搜索仍受阻，P3-A 未通过。"
        )
        if candidates:
            _log("已保留受限前收集到的部分候选（本轮不完整）。")
        else:
            _log("无候选；已保存空清单。")
    elif run_status == STATUS_NO_RESULTS:
        _log("页面明确无搜索结果；非搜索成功。")
    else:
        _log(f"本轮未成功完成候选收集（状态={run_status}）。")

    if exit_code == 0:
        if run_status not in {
            STATUS_SUCCESS,
            STATUS_PARTIAL,
            STATUS_NO_RESULTS,
        }:
            exit_code = 1

    return {
        "ok": exit_code == 0
        and run_status
        in {STATUS_SUCCESS, STATUS_PARTIAL, STATUS_NO_RESULTS},
        "exit_code": exit_code,
        "run_id": run_id,
        "out_dir": out_dir,
        "candidates_path": cand_json,
        "candidates_csv_path": cand_csv,
        "metadata_path": meta_path,
        "candidates": candidates,
        "run_status": run_status,
        "status_reason": status_reason,
        "coverage_stop_reason": coverage_stop_reason,
        "metadata": metadata,
        "browser_started": browser_started,
        "close_ok": close_ok,
        "page_diagnosis": page_diagnosis,
        "search_restriction": restriction_info,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if not args.query or not str(args.query).strip():
            raise ValueError("query 不能为空")
        resolve_search_budget(
            limit=args.limit,
            max_candidates=args.max_candidates,
            max_pages=args.max_pages,
            search_timeout_seconds=args.search_timeout_seconds,
            limit_explicit=args.limit is not None,
            max_candidates_explicit=args.max_candidates is not None,
            max_pages_explicit=args.max_pages is not None,
            search_timeout_explicit=args.search_timeout_seconds is not None,
        )
    except ValueError as exc:
        print(f"参数错误: {exc}")
        return 2
    result = run_search_collect(
        args.query.strip(),
        limit=args.limit,
        max_candidates=args.max_candidates,
        max_pages=args.max_pages,
        search_timeout_seconds=args.search_timeout_seconds,
        auto_close=bool(args.auto_close),
        limit_explicit=args.limit is not None,
        max_candidates_explicit=args.max_candidates is not None,
    )
    return int(result.get("exit_code") or 0)


if __name__ == "__main__":
    raise SystemExit(main())
