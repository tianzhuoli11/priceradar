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
MAX_LIMIT = 5
MAX_SCROLLS = 3
SCROLL_PAUSE_MS = 800
SCROLL_BUDGET_MS = 8_000
EXTRACT_TIMEOUT_MS = 20_000

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


def extract_raw_candidates(page: Page) -> dict[str, Any]:
    """
    Auto-discover product anchors on the rendered search page.
    Strategy pending real JD structure verification; based on common detail URL shapes.
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
            for (const a of anchors) {
                if (isForbiddenZone(a)) continue;
                const href = abs(a.getAttribute('href') || '');
                if (!href) continue;
                // Skip obvious non-product paths
                if (/passport|cart|order|login|mall\\.jd\\.com\\/index/i.test(href)) continue;

                let card = a.closest('li') || a.closest('[class*="gl-item"]') || a.parentElement;
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
                    // fallback: first long line in card
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
                rows.push({
                    href,
                    title: title.slice(0, 200),
                    shop,
                    ad_marked,
                    in_forbidden_zone: false,
                    y: rect.top + window.scrollY,
                    visible: !!(rect.width || rect.height),
                });
            }

            // Sort by document order (y then appearance)
            rows.sort((a, b) => a.y - b.y);
            return {
                anchor_count_raw: anchors.length,
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
        ad_marked = bool(row.get("ad_marked"))
        record = {
            "query": query,
            "rank": None,  # filled after dedupe
            "title": title,
            "product_id": info.get("product_id"),
            "shop_name": row.get("shop"),
            "ad_marked": ad_marked,
            "ad_status": "页面有广告标识" if ad_marked else "未发现广告标识，不能断言非广告",
            "observed_url": info.get("observed_url"),
            "normalized_url": info.get("normalized_url"),
            "normalize_status": info.get("normalize_status"),
            "source_search_url": sanitize_source_search_url(source_url),
            "discovered_at_utc": utc_iso(),
            "match_status": match_status_for_title(title, query),
            "price": None,
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
        default=5,
        help=f"Max unique candidates (1..{MAX_LIMIT})",
    )
    return parser.parse_args(argv)


def validate_args(query: str, limit: int) -> None:
    if not query or not query.strip():
        raise ValueError("query 不能为空")
    if limit < 1 or limit > MAX_LIMIT:
        raise ValueError(f"limit 必须是 1..{MAX_LIMIT} 的正整数")


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

    return failures


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        validate_args(args.query, args.limit)
    except ValueError as exc:
        print(f"参数错误: {exc}")
        return 2

    query = args.query.strip()
    limit = int(args.limit)
    search_url = build_search_url(query)
    started = utc_now()

    print("=== PriceRadar P3-A ===")
    print(f"解释器: {sys.executable}")
    print(f"项目根: {PROJECT_ROOT}")
    print(f"独立 Profile: {PROFILE_DIR}")
    print(f"Playwright: {playwright_version()}")
    print(f"query: {query!r}（作搜索关键词；B913A 不当作京东数字商品ID）")
    print(f"limit: {limit}")
    print(f"搜索方式: {SEARCH_ENTRY_STRATEGY}")
    print("说明: 搜索入口与结果卡片结构仍待本机真实验证；不使用固定商品链接兜底。")
    print()

    run_id, out_dir = make_run_dir()
    print(f"run_id: {run_id}")
    print(f"输出目录: {out_dir}")

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
                status_reason = f"browser_launch_failed: {type(exc).__name__}: {exc}"
                print(f"浏览器启动失败: {status_reason}")
                raise

            browser_started = True
            print("浏览器启动结果: 成功")

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
                    print(
                        f"导航命令完成 http_status={http_status} "
                        f"final={sanitize_source_search_url(final_url)}"
                    )
                    print("说明: HTTP 状态不等于搜索成功，需检查最终地址与页面内容。")
                except PlaywrightError as exc:
                    run_status = STATUS_NAVIGATION_FAILED
                    status_reason = f"{type(exc).__name__}: {exc}"
                    print(f"导航失败: {status_reason}")
                    final_url = getattr(page, "url", "UNKNOWN")
                else:
                    # Immediate post-navigation restriction check (before wait/scroll/extract).
                    restriction_info = detect_search_restriction(page)
                    if restriction_info:
                        run_status = STATUS_SEARCH_RESTRICTED
                        page_diagnosis = STATUS_SEARCH_RESTRICTED
                        status_reason = "visible_search_frequency_block"
                        print(f"页面诊断阻塞状态: {run_status}")
                        print(
                            f"观察到的限制提示: {restriction_info.get('observed_tip')!r} "
                            f"at {restriction_info.get('detected_at_utc')}"
                        )
                        print(
                            "已停止候选提取与滚动；不刷新、不重试、不换入口。"
                            "不推断具体触发机制。"
                        )
                    else:
                        page_diagnosis = diagnose_page_state(page)
                        if page_diagnosis == STATUS_SEARCH_RESTRICTED:
                            # diagnose also checks restriction; keep consistent.
                            restriction_info = detect_search_restriction(page)
                            run_status = STATUS_SEARCH_RESTRICTED
                            status_reason = "visible_search_frequency_block"
                            print(f"页面诊断阻塞状态: {run_status}")
                        elif page_diagnosis:
                            run_status = page_diagnosis
                            status_reason = f"diagnose:{page_diagnosis}"
                            print(f"页面诊断阻塞状态: {run_status}")
                        else:
                            host = urlsplit(final_url).netloc.lower()
                            if (
                                "search.jd.com" not in host
                                and "so.m.jd.com" not in host
                            ):
                                print(
                                    f"警告: 最终主机 {host!r} 非预期搜索域；"
                                    "提取策略待真实验证。"
                                )

                            scroll_info = limited_scroll_for_lazy_load(page)
                            print(
                                f"有限滚动: count={scroll_info.get('scroll_count')} "
                                f"stopped={scroll_info.get('stopped_reason')} "
                                f"budget_ms={SCROLL_BUDGET_MS}"
                            )
                            if scroll_info.get("restriction"):
                                restriction_info = scroll_info["restriction"]
                                run_status = STATUS_SEARCH_RESTRICTED
                                page_diagnosis = STATUS_SEARCH_RESTRICTED
                                status_reason = str(
                                    scroll_info.get("stopped_reason")
                                    or "search_restricted_during_scroll"
                                )
                                incomplete_due_to_restriction = bool(candidates)
                                print(
                                    f"滚动期间检测到搜索限制: "
                                    f"{restriction_info.get('observed_tip')!r}"
                                )
                                print("已停止继续滚动与候选提取；不重试。")
                            else:
                                try:
                                    raw = extract_raw_candidates(page)
                                except PlaywrightError as exc:
                                    run_status = STATUS_EXTRACTION_FAILED
                                    status_reason = (
                                        f"extract_error: {type(exc).__name__}: {exc}"
                                    )
                                    raw = {
                                        "anchor_count_raw": 0,
                                        "candidate_rows": [],
                                        "page_title": "",
                                        "final_url": final_url,
                                    }
                                else:
                                    # Restriction may appear during extract window.
                                    restriction_info = detect_search_restriction(page)
                                    if restriction_info:
                                        run_status = STATUS_SEARCH_RESTRICTED
                                        page_diagnosis = STATUS_SEARCH_RESTRICTED
                                        status_reason = (
                                            "visible_search_frequency_block_before_parse"
                                        )
                                        print(
                                            "提取前检测到搜索限制；停止解析候选。"
                                        )
                                    else:
                                        again = diagnose_page_state(page)
                                        if (
                                            again == STATUS_NO_RESULTS
                                            and not raw.get("candidate_rows")
                                        ):
                                            run_status = STATUS_NO_RESULTS
                                            status_reason = "explicit_no_results_copy"
                                        else:
                                            rows = list(
                                                raw.get("candidate_rows") or []
                                            )
                                            candidates, extract_stats = (
                                                build_candidate_records(
                                                    rows,
                                                    query=query,
                                                    limit=limit,
                                                    source_url=final_url,
                                                )
                                            )
                                            extract_stats["anchor_count_raw"] = (
                                                raw.get("anchor_count_raw")
                                            )
                                            extract_stats["page_title"] = raw.get(
                                                "page_title"
                                            )

                                            if candidates:
                                                if len(candidates) >= limit:
                                                    run_status = STATUS_SUCCESS
                                                    status_reason = (
                                                        "collected_limit_unique_candidates_from_first_scope"
                                                    )
                                                else:
                                                    run_status = STATUS_PARTIAL
                                                    status_reason = (
                                                        "fewer_than_limit_after_dedupe_in_first_scope"
                                                    )
                                            else:
                                                if again == STATUS_NO_RESULTS:
                                                    run_status = STATUS_NO_RESULTS
                                                    status_reason = (
                                                        "no_results_message_and_zero_candidates"
                                                    )
                                                elif (
                                                    int(
                                                        raw.get("anchor_count_raw")
                                                        or 0
                                                    )
                                                    > 0
                                                ):
                                                    run_status = (
                                                        STATUS_EXTRACTION_FAILED
                                                    )
                                                    status_reason = (
                                                        "anchors_present_but_no_reliable_detail_candidates"
                                                    )
                                                else:
                                                    run_status = (
                                                        STATUS_EXTRACTION_FAILED
                                                    )
                                                    status_reason = (
                                                        "zero_product_anchors_without_explicit_no_results;"
                                                        "not_asserting_NO_RESULTS"
                                                    )

                print()
                print(f"运行状态: {run_status}")
                print(f"原因: {status_reason}")
                if restriction_info:
                    print(
                        f"限制提示: {restriction_info.get('observed_tip')!r} "
                        f"检测时间(UTC): {restriction_info.get('detected_at_utc')}"
                    )
                if incomplete_due_to_restriction and candidates:
                    print("说明: 限制出现前已有部分候选，本轮标记为不完整。")
                print(f"候选数: {len(candidates)}")
                for c in candidates:
                    print(
                        f"  #{c['rank']} id={c.get('product_id')} "
                        f"ad={c.get('ad_marked')} "
                        f"title={str(c.get('title') or '')[:60]!r}"
                    )

                print()
                print("开发验收: 可核对窗口中的页面与终端状态；按回车关闭。")
                read_line("按回车结束本次自动化浏览器会话… ")
            except KeyboardInterrupt:
                print()
                print("收到 Ctrl+C，正在关闭…")
                exit_code = 130
            finally:
                try:
                    context.close()
                    close_ok = True
                    print("程序关闭结果: 上下文关闭完成")
                except PlaywrightError as exc:
                    print(f"程序关闭结果: 异常 {type(exc).__name__}: {exc}")
                    raise
    except RuntimeError as exc:
        print(f"运行中止: {type(exc).__name__}: {exc}")
        if run_status == STATUS_NAVIGATION_FAILED and status_reason == "not_started":
            status_reason = str(exc)
        exit_code = 2
    except PlaywrightError as exc:
        print(f"Playwright 错误: {type(exc).__name__}: {exc}")
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
        print(f"候选文件写入失败: {type(exc).__name__}: {exc}")

    metadata = {
        "run_id": run_id,
        "phase": "P3-A",
        "query": query,
        "limit": limit,
        "search_entry_strategy": SEARCH_ENTRY_STRATEGY,
        "search_entry_verification": "PENDING_REAL_RUN",
        "search_url_requested": search_url,
        "final_url_sanitized": (
            sanitize_source_search_url(final_url)
            if final_url not in {"UNKNOWN", ""}
            else final_url
        ),
        "started_at_utc": utc_iso(started),
        "ended_at_utc": utc_iso(ended),
        "browser_started": browser_started,
        "scroll": scroll_info,
        "extract_stats": extract_stats,
        "run_status": run_status,
        "status_reason": status_reason,
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
        ],
    }
    try:
        write_metadata(meta_path, metadata)
    except OSError as exc:
        print(f"metadata 写入失败: {type(exc).__name__}: {exc}")

    print()
    print("=== 输出文件 ===")
    print(f"candidates.json: {cand_json.resolve()}")
    print(f"candidates.csv: {cand_csv.resolve()}")
    print(f"run_metadata.json: {meta_path.resolve()}")
    print(f"run_status={run_status} candidates={len(candidates)}")
    if run_status == STATUS_SUCCESS:
        print(
            "本轮候选收集在限定范围内完成；"
            "不代表全平台搜索、同款核验或最低价监控完成。"
        )
    elif run_status == STATUS_PARTIAL:
        print("本轮仅部分候选；不代表搜索验收通过。")
    elif run_status == STATUS_SEARCH_RESTRICTED:
        print(
            "搜索受限（可见频繁限制提示）。已停止提取/滚动；"
            "真实自动搜索仍受阻，P3-A 未通过。"
        )
        if candidates:
            print("已保留受限前收集到的部分候选（本轮不完整）。")
        else:
            print("无候选；已保存空清单。")
    elif run_status == STATUS_NO_RESULTS:
        print("页面明确无搜索结果；非搜索成功。")
    else:
        print(f"本轮未成功完成候选收集（状态={run_status}）。")

    if exit_code:
        return exit_code
    if run_status in {
        STATUS_SUCCESS,
        STATUS_PARTIAL,
        STATUS_NO_RESULTS,
    }:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
