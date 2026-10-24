"""P2-D: fully automatic JD product region screenshots (no manual box selection)."""

from __future__ import annotations

import json
import re
import secrets
import struct
import sys
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = (PROJECT_ROOT / "browser_profiles" / "jd_persistent").resolve()
CAPTURE_ROOT = (PROJECT_ROOT / "artifacts" / "price_captures").resolve()
FIXTURE_PATH = (
    PROJECT_ROOT / "scripts" / "fixtures" / "p2d_geometry_fixture.html"
).resolve()
BOTTOM_BAR_FIXTURE_PATH = (
    PROJECT_ROOT / "scripts" / "fixtures" / "p2d_price_bottom_bar_fixture.html"
).resolve()

PRODUCT_DETAIL_URL = "https://item.m.jd.com/product/100005149674.html"
EXPECTED_HOST = "item.m.jd.com"
EXPECTED_PRODUCT_ID = "100005149674"
EXPECTED_PATHS = (
    f"/product/{EXPECTED_PRODUCT_ID}.html",
    f"/product/{EXPECTED_PRODUCT_ID}",
)

EXPECTED_PRODUCT = {
    "jd_product_id": EXPECTED_PRODUCT_ID,
    "model": "B913A",
    "spec": "单层200抽×20包／箱",
    "shop": "清风京东自营旗舰店",
    "red_line_price_cny_per_box": 127,
    "note": "预先登记的业务基准，不是本次自动识别结果",
}

# Semantic discovery clues only (not claimed real-time scraped values).
CLUE_MODEL = "B913A"
CLUE_SELECTED = "已选"
CLUE_PROMO = "优惠"
CLUE_SHIP_TO = "送至"
# Prefer explicit shop-name patterns; bare「自营」易命中服务承诺文案。
SHOP_HINTS = ("旗舰店", "专营店", "自营店", "官方旗舰")
SHOP_EXCLUDE_PHRASES = (
    "服务承诺",
    "正品保证",
    "正品行货",
    "开具发票",
    "开具机打发票",
    "电子发票",
    "全国联保",
    "质保证书",
    "京东商城向您保证",
)

NAVIGATION_TIMEOUT_MS = 60_000
MAIN_PRICE_SELECTOR = "#main_price"
MAIN_PRICE_WAIT_MS = 15_000
FONT_WAIT_MS = 8_000
LAYOUT_STABLE_MS = 500
SELECTION_STABLE_POLLS_MS = 250
SELECTION_STABLE_TIMEOUT_MS = 3_000
SELECTION_STABLE_REQUIRED_POLLS = 2
# Layout boxes under-measure custom-font ink (P2-C evidence: bottom digits clipped
# with height≈28 + pad_y=3). Use generous whitespace around the price region —
# not hard-coded page absolute coordinates.
PRICE_PAD_X_PX = 40
PRICE_PAD_Y_PX = 36
REGION_PAD_X_PX = 12
REGION_PAD_Y_PX = 10
MAX_REGION_HEIGHT_PX = 420
MAX_REGION_WIDTH_RATIO = 0.98
MIN_PRICE_CONTENT_WIDTH = 20
MIN_PRICE_CONTENT_HEIGHT = 10
# Keep price clear of fixed chrome; bounded occlusion retries only.
PRICE_SAFE_MARGIN_PX = 16
MAX_PRICE_OCCLUSION_ADJUSTMENTS = 3
PRICE_CENTER_BAND_RATIO = 0.22  # accept center ±22% of viewport height
# Non-price regions: independent safe band + occlusion (do not reuse prior scroll).
REGION_SAFE_MARGIN_PX = 16
MAX_REGION_POSITION_ADJUSTMENTS = 4
# Side-branch copy that must not enter core selection comparison.
SELECTION_NOISE_MARKERS = (
    "本商品支持",
    "保障服务",
    "点击可选服务",
    "点击可选",
    "送至",
    "现货",
    "活动",
    "免基础运费",
    "无理由退货",
    "价保",
    "可配送",
    "供应商售后",
    "复购补贴",
    "限时达",
    "预约送货",
    "收货送货上门",
)
# Stop expanding selected-spec container when these appear (unless core already matched).
SELECTED_SPEC_STOP_PHRASES = (
    "保障服务",
    "本商品支持",
    "点击可选服务",
    "送至",
    "现货",
    "活动",
)
# Core: 已选 <option>，<N>个  — keep size/style tokens inside option.
CORE_SELECTED_SPEC_RE = re.compile(
    r"^已选\s*(.+?)[，,]\s*(\d+)\s*个\s*$",
    re.UNICODE,
)
# Browser-side: same semantics as CORE_SELECTED_SPEC_RE after noise strip.
CORE_SELECTED_SPEC_JS_RE = r"已选\s*.+?[，,]\s*\d+\s*个"

# Shared locator used by discover_regions + snapshot_selection_state (fresh DOM each call).
LOCATE_SELECTED_SPEC_JS = """({ selectedClue, noiseMarkers, stopPhrases, maxH, maxWRatio, coreReSource }) => {
    const main = document.querySelector('#main_price');
    const mainRect = main ? main.getBoundingClientRect() : { top: 0, bottom: 0, left: 0, right: 0 };
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const coreRe = new RegExp(coreReSource);
    const textOf = (el) => (el.innerText || '').replace(/\\s+/g, ' ').trim();
    const visible = (el) => {
        const r = el.getBoundingClientRect();
        const st = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0
            && st.visibility !== 'hidden'
            && st.display !== 'none'
            && st.opacity !== '0';
    };
    const stripNoise = (t) => {
        let s = (t || '').replace(/\\s+/g, ' ').trim();
        let cut = s.length;
        for (const marker of noiseMarkers) {
            const i = s.indexOf(marker);
            if (i >= 0) cut = Math.min(cut, i);
        }
        return s.slice(0, cut).replace(/[，,;；\\s]+$/g, '').trim();
    };
    const coreBlobOf = (t) => {
        const s = stripNoise(t);
        const i = s.indexOf(selectedClue);
        if (i < 0) return '';
        return s.slice(i).replace(/\\s+/g, ' ').trim();
    };
    const hasCorePattern = (t) => coreRe.test(coreBlobOf(t));
    const containsShip = (el) => textOf(el).includes('送至');
    const inMainColumn = (el) => {
        const r = el.getBoundingClientRect();
        if (r.left > vw * 0.98 || r.right < 0) return false;
        if (r.left > vw * 0.60 && r.left > mainRect.right + 100) return false;
        if (r.left > Math.max(mainRect.right + 80, vw * 0.72)) return false;
        return true;
    };
    const scoreNearMain = (el) => {
        const r = el.getBoundingClientRect();
        const cy = (r.top + r.bottom) / 2;
        const my = (mainRect.top + mainRect.bottom) / 2;
        return Math.abs(cy - my) + Math.abs(r.left - mainRect.left) * 0.25;
    };
    const boxOf = (el) => {
        const r = el.getBoundingClientRect();
        return {
            x: r.left, y: r.top, width: r.width, height: r.height,
            sample_text: textOf(el).slice(0, 80),
        };
    };
    // Climb from clue until stripped text matches 已选+选项+数量；勿停在仅「已选」标签。
    const expandToCoreContainer = (start) => {
        let node = start;
        let bestWithCore = null;
        for (let depth = 0; depth < 10 && node && node !== document.body; depth++) {
            if (!visible(node)) {
                node = node.parentElement;
                continue;
            }
            const t = textOf(node);
            const r = node.getBoundingClientRect();
            if (!t.includes(selectedClue)) {
                node = node.parentElement;
                continue;
            }
            if (hasCorePattern(t)) {
                const dirty = stopPhrases.some((p) => t.includes(p)) || t.includes('送至');
                if (!dirty) {
                    // Minimal clean ancestor: 已选标签＋选项＋数量, no side-branch.
                    bestWithCore = node;
                    break;
                }
                // Same node mixes core + 保障服务/送至 — keep as fallback, stop climb.
                if (!bestWithCore) bestWithCore = node;
                break;
            }
            // Label-only / incomplete: keep climbing unless side-branch / size blows up.
            if (stopPhrases.some((p) => t.includes(p))) {
                break;
            }
            if (containsShip(node)) {
                break;
            }
            if (r.height > maxH || r.width > vw * maxWRatio) {
                break;
            }
            if (t.length > 320) {
                break;
            }
            node = node.parentElement;
        }
        return bestWithCore;
    };

    const hits = [];
    for (const el of Array.from(document.querySelectorAll('body *'))) {
        if (!visible(el) || !inMainColumn(el)) continue;
        const own = Array.from(el.childNodes)
            .filter((n) => n.nodeType === 3)
            .map((n) => n.textContent || '')
            .join('');
        const t = (own || el.innerText || '').replace(/\\s+/g, ' ');
        if (!t.includes(selectedClue)) continue;
        if (containsShip(el) && !hasCorePattern(textOf(el))) continue;
        if (textOf(el).length > 400) continue;
        hits.push(el);
    }
    // Prefer compact clue nodes as expansion starts (label leaf is OK as START only).
    hits.sort((a, b) => {
        const ra = a.getBoundingClientRect();
        const rb = b.getBoundingClientRect();
        return (ra.width * ra.height) - (rb.width * rb.height);
    });

    const located = [];
    const seen = new Set();
    for (const hit of hits.slice(0, 24)) {
        const container = expandToCoreContainer(hit);
        if (!container || seen.has(container)) continue;
        seen.add(container);
        const sample = textOf(container);
        if (!hasCorePattern(sample)) continue;
        located.push({
            el: container,
            score: scoreNearMain(container),
            sample_text: sample.slice(0, 120),
            core_blob: coreBlobOf(sample).slice(0, 120),
            box: boxOf(container),
            dirty: stopPhrases.some((p) => sample.includes(p)) || sample.includes('送至'),
        });
    }
    // Prefer clean containers (no 保障服务/送至) when scores are comparable.
    located.sort((a, b) => (a.dirty - b.dirty) || (a.score - b.score));
    if (!located.length) {
        // Diagnostics: whether a bare 已选 label exists without option/qty.
        let labelOnly = false;
        for (const hit of hits.slice(0, 12)) {
            const t = textOf(hit).replace(/\\s+/g, ' ').trim();
            if (t === selectedClue || t === selectedClue + '：' || t === selectedClue + ':') {
                labelOnly = true;
                break;
            }
        }
        return {
            status: 'NOT_FOUND',
            match_count: hits.length,
            reason: labelOnly
                ? 'found_已选_label_without_option_qty'
                : 'no_selected_spec_core_container',
            discovery: 'text_clue:' + selectedClue,
            sample_text: null,
            core_blob: null,
            box: null,
            label_only_observed: labelOnly,
        };
    }
    const near = located.filter((s) => s.score < Math.max(vh, 700));
    const pool = near.length ? near : located;
    if (pool.length > 1 && Math.abs(pool[0].score - pool[1].score) < 40) {
        const a = pool[0].el;
        const b = pool[1].el;
        if (!(a.contains(b) || b.contains(a) || pool[0].core_blob === pool[1].core_blob)) {
            return {
                status: 'AMBIGUOUS',
                match_count: pool.length,
                reason: 'multiple selected-spec core regions near main_price',
                discovery: 'text_clue:' + selectedClue,
                sample_texts: pool.slice(0, 3).map((s) => s.sample_text),
                sample_text: null,
                core_blob: null,
                box: null,
            };
        }
    }
    const win = pool[0];
    return {
        status: 'LOCATED',
        match_count: located.length,
        visible: true,
        discovery: 'text_clue:' + selectedClue,
        sample_text: win.sample_text,
        core_blob: win.core_blob,
        box: win.box,
    };
}"""


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


def url_without_query_or_fragment(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path if parts.path else "/"
    return f"{parts.scheme}://{parts.netloc}{path}"


def product_paths_for_id(product_id: str) -> tuple[str, ...]:
    return (f"/product/{product_id}.html", f"/product/{product_id}")


def extract_product_id_from_detail_url(url: str) -> str | None:
    """Parse digits from item.jd.com/{id}.html or item.m.jd.com/product/{id}.html."""
    parts = urlsplit(url or "")
    host = parts.netloc.lower()
    path = parts.path or ""
    if host == "item.m.jd.com":
        m = re.search(r"/product/(\d+)\.html", path, re.IGNORECASE)
        return m.group(1) if m else None
    if host == "item.jd.com":
        m = re.search(r"/(\d+)\.html$", path)
        return m.group(1) if m else None
    return None


def is_target_product_url(
    url: str,
    *,
    product_id: str | None = None,
    host: str | None = None,
) -> bool:
    """
    Identity: allowed detail hosts + URL product id must equal expected id.
    host=None → accept item.m.jd.com or item.jd.com (Phase A candidate adapt).
    host set → legacy strict single-host check (CLI sample default).
    """
    parts = urlsplit(url)
    netloc = parts.netloc.lower()
    pid = product_id or EXPECTED_PRODUCT_ID
    extracted = extract_product_id_from_detail_url(url)
    if extracted is not None and extracted == pid:
        if host is None:
            return netloc in {"item.m.jd.com", "item.jd.com"}
        return netloc == host.lower()
    # Legacy path equality for m.jd when extract failed oddly
    if host is not None and netloc != host.lower():
        return False
    if host is None and netloc not in {"item.m.jd.com", "item.jd.com"}:
        return False
    check_host = host or EXPECTED_HOST
    if netloc != check_host.lower():
        return False
    allowed = set(product_paths_for_id(pid))
    allowed.update(p.rstrip("/") for p in tuple(allowed))
    path = parts.path
    return path in allowed or path.rstrip("/") in allowed


def png_pixel_size(path: Path) -> tuple[int, int] | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def make_run_dir(product_id: str | None = None) -> tuple[str, Path]:
    pid = product_id or EXPECTED_PRODUCT_ID
    run_id = f"{utc_run_stamp()}_{pid}_{secrets.token_hex(3)}"
    out_dir = CAPTURE_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=False)
    return run_id, out_dir


def strip_selection_noise(text: str) -> str:
    """Cut side-branch copy (保障服务/送至/活动/…) from an 已选 blob."""
    s = re.sub(r"\s+", " ", (text or "")).strip()
    if not s:
        return ""
    cut = len(s)
    for marker in SELECTION_NOISE_MARKERS:
        idx = s.find(marker)
        if idx >= 0:
            cut = min(cut, idx)
    return s[:cut].strip(" ，,;；")


def parse_core_selected_spec(raw_text: str | None) -> dict[str, Any]:
    """
    Extract only selected option + purchase qty.
    Preserves option semantics (incl. 【超大尺寸】); never treats 个 as 包/箱.
    """
    raw = re.sub(r"\s+", " ", (raw_text or "")).strip()
    if not raw:
        return {
            "core_status": "UNKNOWN",
            "core_selected_spec": None,
            "selected_option_text": None,
            "purchase_qty": None,
            "raw_selected_blob": raw_text,
            "reason": "empty_selected_text",
        }
    stripped = strip_selection_noise(raw)
    # Ensure leading 已选 for the regex; some blobs already have it.
    if stripped.startswith("已选"):
        candidate = stripped
    elif "已选" in stripped:
        candidate = stripped[stripped.index("已选") :]
    else:
        candidate = f"已选 {stripped}" if stripped else ""
    candidate = re.sub(r"\s+", " ", candidate).strip()
    match = CORE_SELECTED_SPEC_RE.match(candidate)
    if not match:
        return {
            "core_status": "UNKNOWN",
            "core_selected_spec": None,
            "selected_option_text": None,
            "purchase_qty": None,
            "raw_selected_blob": raw_text,
            "stripped_text": stripped,
            "reason": "core_pattern_not_matched",
        }
    option = match.group(1).strip()
    qty = match.group(2).strip()
    if not option or not qty:
        return {
            "core_status": "UNKNOWN",
            "core_selected_spec": None,
            "selected_option_text": None,
            "purchase_qty": None,
            "raw_selected_blob": raw_text,
            "reason": "empty_option_or_qty",
        }
    core = f"已选 {option}，{qty}个"
    return {
        "core_status": "EXTRACTED",
        "core_selected_spec": core,
        "selected_option_text": option,
        "purchase_qty": qty,
        "raw_selected_blob": raw_text,
        "stripped_text": stripped,
        "reason": None,
    }


def compare_selection_states(
    before: dict[str, Any] | None, after: dict[str, Any] | None
) -> dict[str, Any]:
    """
    Compare product_id / variant token / option / qty only.
    UNKNOWN if either core extract failed — never default consistent=True.
    Capture-time gaps are not treated as spec changes.
    """
    before = before or {}
    after = after or {}
    reasons: list[str] = []
    b_core = before.get("core_status")
    a_core = after.get("core_status")
    if before.get("error") or after.get("error"):
        return {
            "consistent": None,
            "consistency_status": "UNKNOWN",
            "inconsistency_reasons": [
                "selection_state 采集异常，无法核验核心规格一致性"
            ],
            "compared_fields": {},
        }
    if b_core != "EXTRACTED" or a_core != "EXTRACTED":
        detail = []
        if b_core != "EXTRACTED":
            detail.append(f"before={b_core or 'missing'}")
        if a_core != "EXTRACTED":
            detail.append(f"after={a_core or 'missing'}")
        return {
            "consistent": None,
            "consistency_status": "UNKNOWN",
            "inconsistency_reasons": [
                "核心已选规格无法提取，不做一致性默认: " + ", ".join(detail)
            ],
            "compared_fields": {
                "before_core": before.get("core_selected_spec"),
                "after_core": after.get("core_selected_spec"),
            },
        }

    fields = {
        "product_id": (before.get("product_id"), after.get("product_id")),
        "variant_token": (before.get("variant_token"), after.get("variant_token")),
        "selected_option_text": (
            before.get("selected_option_text"),
            after.get("selected_option_text"),
        ),
        "purchase_qty": (before.get("purchase_qty"), after.get("purchase_qty")),
    }
    labels = {
        "product_id": "product_id",
        "variant_token": "选中变体标识",
        "selected_option_text": "已选规格选项",
        "purchase_qty": "购买数量",
    }
    for key, (bv, av) in fields.items():
        # Both absent: nothing to compare on this field (local fixtures / no sku token).
        if key in {"variant_token", "product_id"} and not bv and not av:
            continue
        if bv is None or av is None or bv == "":
            reasons.append(f"{labels[key]} 缺失，无法比较")
            continue
        if av is None or av == "":
            reasons.append(f"{labels[key]} 缺失，无法比较")
            continue
        if bv != av:
            reasons.append(f"{labels[key]} 在截图前后变化（{bv!r} → {av!r}）")

    if reasons:
        return {
            "consistent": False,
            "consistency_status": "INCONSISTENT",
            "inconsistency_reasons": reasons,
            "compared_fields": {k: {"before": v[0], "after": v[1]} for k, v in fields.items()},
        }
    return {
        "consistent": True,
        "consistency_status": "CONSISTENT",
        "inconsistency_reasons": [],
        "compared_fields": {k: {"before": v[0], "after": v[1]} for k, v in fields.items()},
    }


def clamp_clip_to_viewport(
    clip: dict[str, float],
    viewport: dict[str, float],
) -> dict[str, float] | None:
    x = max(0.0, float(clip["x"]))
    y = max(0.0, float(clip["y"]))
    right = min(float(viewport["width"]), float(clip["x"]) + float(clip["width"]))
    bottom = min(float(viewport["height"]), float(clip["y"]) + float(clip["height"]))
    width = right - x
    height = bottom - y
    if width < 1 or height < 1:
        return None
    return {"x": x, "y": y, "width": width, "height": height}


def build_padded_clip(
    content: dict[str, float],
    *,
    pad_x: float,
    pad_y: float,
    viewport: dict[str, float],
) -> dict[str, float] | None:
    clip = {
        "x": float(content["x"]) - pad_x,
        "y": float(content["y"]) - pad_y,
        "width": float(content["width"]) + (pad_x * 2),
        "height": float(content["height"]) + (pad_y * 2),
    }
    return clamp_clip_to_viewport(clip, viewport)


def clip_fully_covers(content: dict[str, float], clip: dict[str, float]) -> bool:
    return (
        clip["x"] <= content["x"] + 0.5
        and clip["y"] <= content["y"] + 0.5
        and clip["x"] + clip["width"] >= content["x"] + content["width"] - 0.5
        and clip["y"] + clip["height"] >= content["y"] + content["height"] - 0.5
    )


def wait_fonts_ready(page: Page) -> bool:
    return bool(
        page.evaluate(
            """async (timeoutMs) => {
                if (!(document.fonts && document.fonts.ready)) return false;
                const ready = document.fonts.ready.then(() => true);
                const timeout = new Promise((resolve) =>
                    setTimeout(() => resolve(false), timeoutMs)
                );
                return await Promise.race([ready, timeout]);
            }""",
            FONT_WAIT_MS,
        )
    )


def measure_price_ink_box(page: Page) -> dict[str, Any] | None:
    """
    Measure #main_price in CSS viewport pixels.

    Combines element boxes with Range.getClientRects() because custom-font glyphs
    can ink-overflow layout boxes (observed: bottom of digits clipped when pad≈3).
    """
    return page.evaluate(
        """() => {
            const el = document.querySelector('#main_price');
            if (!el) return null;

            const rects = [];
            const pushRect = (r) => {
                if (!r || r.width <= 0 || r.height <= 0) return;
                rects.push({
                    left: r.left, top: r.top, right: r.right, bottom: r.bottom
                });
            };
            const pushNode = (node) => {
                if (!node || node.nodeType !== 1) return;
                pushRect(node.getBoundingClientRect());
                try {
                    const range = document.createRange();
                    range.selectNodeContents(node);
                    Array.from(range.getClientRects()).forEach(pushRect);
                } catch (e) {}
            };

            pushNode(el);
            el.querySelectorAll('em, .price_decimals, span').forEach(pushNode);

            // Optional nearby direct price label inside the same parent, if tiny.
            const parent = el.parentElement;
            let labelIncluded = false;
            if (parent) {
                for (const child of Array.from(parent.children)) {
                    if (child === el) continue;
                    const t = (child.innerText || '').trim();
                    if (!t || t.length > 8) continue;
                    if (!/价|券|促销/.test(t)) continue;
                    const pr = child.getBoundingClientRect();
                    const er = el.getBoundingClientRect();
                    if (Math.abs(pr.bottom - er.top) > 36 && Math.abs(pr.top - er.bottom) > 36) {
                        continue;
                    }
                    if (pr.width > 120 || pr.height > 40) continue;
                    pushRect(pr);
                    labelIncluded = true;
                }
            }

            if (!rects.length) return null;
            const left = Math.min(...rects.map((r) => r.left));
            const top = Math.min(...rects.map((r) => r.top));
            const right = Math.max(...rects.map((r) => r.right));
            const bottom = Math.max(...rects.map((r) => r.bottom));
            const layout = el.getBoundingClientRect();
            return {
                x: left,
                y: top,
                width: right - left,
                height: bottom - top,
                layout_box: {
                    x: layout.left, y: layout.top,
                    width: layout.width, height: layout.height
                },
                rect_count: rects.length,
                label_included: labelIncluded,
                device_pixel_ratio: window.devicePixelRatio || 1,
                scroll_x: window.scrollX,
                scroll_y: window.scrollY,
                viewport: { width: window.innerWidth, height: window.innerHeight },
                coordinate_space: 'css_viewport_px',
            };
        }"""
    )


def locate_selected_spec_region(page: Page) -> dict[str, Any]:
    """
    Shared selected-spec locator (fresh DOM read every call).

    Finds the minimal region that contains 已选标签 + 实际选项 + 购买数量.
    Must not stop on the bare「已选」label leaf when option/qty live in siblings.
    Must not expand into 送至/活动/保障服务 wrappers.
    Used by discover_regions (screenshot) and snapshot_selection_state (before/after).
    """
    return page.evaluate(
        LOCATE_SELECTED_SPEC_JS,
        {
            "selectedClue": CLUE_SELECTED,
            "noiseMarkers": list(SELECTION_NOISE_MARKERS),
            "stopPhrases": list(SELECTED_SPEC_STOP_PHRASES),
            "maxH": min(MAX_REGION_HEIGHT_PX, 120),
            "maxWRatio": MAX_REGION_WIDTH_RATIO,
            "coreReSource": CORE_SELECTED_SPEC_JS_RE,
        },
    )


def discover_regions(
    page: Page, *, model_clue: str | None = None
) -> dict[str, Any]:
    """
    Semantic region discovery near #main_price (no invented CSS).
    model_clue: business expected model for title locate; default CLI sample.
    """
    model_clue = (model_clue or CLUE_MODEL).strip() or CLUE_MODEL
    result = page.evaluate(
        """({ model, selectedClue, promoClue, shipTo, shopHints, shopExclude, maxH, maxWRatio }) => {
            const main = document.querySelector('#main_price');
            if (!main) {
                return { ok: false, reason: 'main_price_missing' };
            }
            const mainRect = main.getBoundingClientRect();
            const vw = window.innerWidth;
            const vh = window.innerHeight;

            const visible = (el) => {
                const r = el.getBoundingClientRect();
                const st = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0
                    && st.visibility !== 'hidden'
                    && st.display !== 'none'
                    && st.opacity !== '0';
            };
            const textOf = (el) => (el.innerText || '').replace(/\\s+/g, ' ').trim();
            const containsShip = (el) => textOf(el).includes(shipTo);
            const isShopNoise = (el) => {
                const t = textOf(el);
                return (shopExclude || []).some((p) => t.includes(p));
            };
            // Whole-model boundary: avoid B913A matching inside B913AB.
            const modelRe = new RegExp(
                '(^|[^A-Za-z0-9])' + model.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&') + '([^A-Za-z0-9]|$)',
                'i'
            );
            const hasModelToken = (el) => modelRe.test(textOf(el));
            const inMainColumn = (el) => {
                const r = el.getBoundingClientRect();
                // Drop off-screen / far-right recommendation rails.
                if (r.left > vw * 0.98 || r.right < 0) return false;
                // Side rail: clearly right of main price column (desktop dual-pane).
                if (r.left > vw * 0.60 && r.left > mainRect.right + 100) return false;
                if (r.left > Math.max(mainRect.right + 80, vw * 0.72)) return false;
                return true;
            };

            const scoreNearMain = (el) => {
                const r = el.getBoundingClientRect();
                const cy = (r.top + r.bottom) / 2;
                const my = (mainRect.top + mainRect.bottom) / 2;
                return Math.abs(cy - my) + Math.abs(r.left - mainRect.left) * 0.25;
            };

            const minimalSafeContainer = (start, { requireText, maxHeight, stopPhrases }) => {
                let best = null;
                let node = start;
                const stops = stopPhrases || [];
                for (let depth = 0; depth < 8 && node && node !== document.body; depth++) {
                    if (!visible(node)) {
                        node = node.parentElement;
                        continue;
                    }
                    const t = textOf(node);
                    const r = node.getBoundingClientRect();
                    if (requireText && !t.includes(requireText)) {
                        node = node.parentElement;
                        continue;
                    }
                    if (containsShip(node)) {
                        // too wide / unsafe if 送至 included
                        break;
                    }
                    if (stops.some((p) => t.includes(p))) {
                        // Do not expand into 保障服务 / promo side-branch wrappers.
                        break;
                    }
                    if (r.height > maxHeight || r.width > vw * maxWRatio) {
                        break;
                    }
                    if (r.height >= 12 && r.width >= 40) {
                        best = node;
                    }
                    node = node.parentElement;
                }
                return best;
            };

            const boxOf = (el) => {
                const r = el.getBoundingClientRect();
                return {
                    x: r.left, y: r.top, width: r.width, height: r.height,
                    sample_text: textOf(el).slice(0, 80),
                };
            };

            const findByExactText = (needle) => {
                const all = Array.from(document.querySelectorAll('body *')).filter((el) => {
                    if (!visible(el)) return false;
                    // Prefer leaf-ish nodes: own text contains needle, avoid huge wrappers early.
                    const own = Array.from(el.childNodes)
                        .filter((n) => n.nodeType === 3)
                        .map((n) => n.textContent || '')
                        .join('');
                    const t = (own || el.innerText || '').replace(/\\s+/g, ' ');
                    return t.includes(needle);
                });
                return all;
            };

            const isLikelyNonProductTitle = (el) => {
                const t = textOf(el);
                // Huge wrappers that also contain review/recommend/boilerplate.
                if (t.length > 220 && /(推荐|看了又看|全部评价|商品评价|服务承诺|正品保证)/.test(t)) {
                    return true;
                }
                return false;
            };

            // Collapse parent/child + same-text re-render duplicates of the title/model hit.
            const dedupeNestedSameText = (candidates) => {
                const list = Array.from(new Set(candidates))
                    .filter(inMainColumn)
                    .filter((el) => !isLikelyNonProductTitle(el));
                const kept = [];
                for (const el of list) {
                    const dominated = list.some((other) => (
                        other !== el
                        && other.contains(el)
                        && textOf(other).includes(textOf(el).slice(0, 40))
                    ));
                    // Drop pure nested copies when a parent already carries the same model.
                    if (dominated) {
                        const parent = list.find((other) => other !== el && other.contains(el));
                        if (parent && hasModelToken(parent) && hasModelToken(el)) {
                            continue;
                        }
                    }
                    kept.push(el);
                }
                // Second pass: if A contains B and scores nearly equal, keep smaller leaf.
                const final = [];
                for (const el of kept) {
                    const childDup = kept.find((other) => (
                        other !== el
                        && el.contains(other)
                        && hasModelToken(other)
                        && Math.abs(scoreNearMain(el) - scoreNearMain(other)) < 40
                    ));
                    if (childDup) {
                        continue;
                    }
                    final.push(el);
                }
                const afterNest = final.length ? final : kept;
                // Third pass: sibling / duplicate-render with same normalized text.
                const byKey = new Map();
                for (const el of afterNest) {
                    const key = textOf(el).replace(/\\s+/g, ' ').slice(0, 100);
                    const prev = byKey.get(key);
                    if (!prev) {
                        byKey.set(key, el);
                        continue;
                    }
                    const sa = scoreNearMain(el);
                    const sb = scoreNearMain(prev);
                    const ha = el.getBoundingClientRect().height;
                    const hb = prev.getBoundingClientRect().height;
                    // Prefer closer to main_price; tie-break smaller box.
                    const preferEl = (sa < sb - 1) || (Math.abs(sa - sb) <= 1 && ha <= hb);
                    byKey.set(key, preferEl ? el : prev);
                }
                return Array.from(byKey.values());
            };

            const candidateDiag = (pool) => pool.slice(0, 5).map((s) => ({
                score: s.score,
                sample_text: textOf(s.el).slice(0, 80),
                box: boxOf(s.el),
            }));

            const pickUniqueNear = (candidates, label) => {
                const scored = candidates
                    .map((el) => ({ el, score: scoreNearMain(el) }))
                    .sort((a, b) => a.score - b.score);
                if (!scored.length) {
                    return { status: 'NOT_FOUND', match_count: 0, label };
                }
                // Keep only those reasonably near the main price vertically.
                const near = scored.filter((s) => s.score < Math.max(vh, 700));
                const pool = near.length ? near : scored;
                if (pool.length > 1 && Math.abs(pool[0].score - pool[1].score) < 40) {
                    const a = pool[0].el;
                    const b = pool[1].el;
                    if (a.contains(b) || b.contains(a)) {
                        const leaf = a.contains(b) ? b : a;
                        return {
                            status: 'CANDIDATE',
                            match_count: candidates.length,
                            label,
                            el: leaf,
                            score: scoreNearMain(leaf),
                            deduped_nested: true,
                        };
                    }
                    // Same-content re-render (not nested): still one title.
                    const ta = textOf(a).replace(/\\s+/g, ' ').slice(0, 80);
                    const tb = textOf(b).replace(/\\s+/g, ' ').slice(0, 80);
                    if (ta && tb && (ta === tb || ta.includes(tb) || tb.includes(ta))) {
                        const prefer = pool[0].score <= pool[1].score ? a : b;
                        return {
                            status: 'CANDIDATE',
                            match_count: candidates.length,
                            label,
                            el: prefer,
                            score: scoreNearMain(prefer),
                            deduped_same_text: true,
                        };
                    }
                    // Truly distinct candidates — keep ambiguity + auto diagnostics.
                    return {
                        status: 'AMBIGUOUS',
                        match_count: pool.length,
                        label,
                        reason: 'multiple similar-score matches near main_price',
                        sample_texts: pool.slice(0, 3).map((s) => textOf(s.el).slice(0, 60)),
                        candidate_diagnostics: candidateDiag(pool),
                    };
                }
                return {
                    status: 'CANDIDATE',
                    match_count: candidates.length,
                    label,
                    el: pool[0].el,
                    score: pool[0].score,
                };
            };

            const result = {
                ok: true,
                main_price: {
                    status: 'FOUND',
                    match_count: 1,
                    visible: visible(main),
                    box: boxOf(main),
                },
                product_title: null,
                selected_spec: null,
                promotions: null,
                shop: null,
            };

            // Title / model clue — whole-token + nested dedupe + main-column filter.
            const modelHitsRaw = findByExactText(model).filter((el) => (
                !containsShip(el)
                && hasModelToken(el)
                && inMainColumn(el)
                && !isLikelyNonProductTitle(el)
            ));
            const modelHits = dedupeNestedSameText(modelHitsRaw);
            const modelPick = pickUniqueNear(modelHits, 'product_title');
            if (modelPick.status === 'CANDIDATE') {
                const container = minimalSafeContainer(modelPick.el, {
                    requireText: model,
                    maxHeight: maxH,
                });
                if (!container) {
                    result.product_title = {
                        status: 'UNSAFE_REGION',
                        match_count: modelPick.match_count,
                        reason: 'no safe container without 送至 / size limits',
                        discovery: 'text_clue:' + model,
                        raw_hit_count: modelHitsRaw.length,
                        deduped_hit_count: modelHits.length,
                    };
                } else {
                    result.product_title = {
                        status: 'LOCATED',
                        match_count: modelPick.match_count,
                        visible: true,
                        discovery: 'text_clue:' + model,
                        sample_text: textOf(container).slice(0, 80),
                        box: boxOf(container),
                        raw_hit_count: modelHitsRaw.length,
                        deduped_hit_count: modelHits.length,
                        deduped_nested: !!modelPick.deduped_nested,
                        deduped_same_text: !!modelPick.deduped_same_text,
                    };
                }
            } else {
                result.product_title = {
                    status: modelPick.status,
                    match_count: modelPick.match_count || 0,
                    reason: modelPick.reason || null,
                    discovery: 'text_clue:' + model,
                    raw_hit_count: modelHitsRaw.length,
                    deduped_hit_count: modelHits.length,
                    sample_texts: modelPick.sample_texts || null,
                    candidate_diagnostics: modelPick.candidate_diagnostics || null,
                };
            }

            // selected_spec: filled by locate_selected_spec_region() after this evaluate
            // (shared locator with snapshot_selection_state — fresh DOM each call).
            result.selected_spec = { status: 'PENDING_SHARED_LOCATOR' };

            // Promotions
            const promoHits = findByExactText(promoClue).filter((el) => !containsShip(el));
            const promoPick = pickUniqueNear(promoHits, 'promotions');
            if (promoPick.status === 'CANDIDATE') {
                const container = minimalSafeContainer(promoPick.el, {
                    requireText: promoClue,
                    maxHeight: Math.min(maxH, 260),
                });
                if (!container) {
                    result.promotions = {
                        status: 'UNSAFE_REGION',
                        match_count: promoPick.match_count,
                        reason: 'no safe container without 送至 / size limits',
                        discovery: 'text_clue:' + promoClue,
                    };
                } else {
                    result.promotions = {
                        status: 'LOCATED',
                        match_count: promoPick.match_count,
                        visible: true,
                        discovery: 'text_clue:' + promoClue,
                        sample_text: textOf(container).slice(0, 80),
                        box: boxOf(container),
                    };
                }
            } else {
                result.promotions = {
                    status: promoPick.status === 'AMBIGUOUS' ? 'AMBIGUOUS' : 'NOT_FOUND',
                    match_count: promoPick.match_count || 0,
                    reason: promoPick.reason || null,
                    discovery: 'text_clue:' + promoClue,
                };
            }

            // Shop hints — exclude service-promise boilerplate; stay in main column.
            let shopCandidates = [];
            for (const hint of shopHints) {
                shopCandidates = shopCandidates.concat(
                    findByExactText(hint).filter((el) => (
                        !containsShip(el)
                        && !isShopNoise(el)
                        && inMainColumn(el)
                    ))
                );
            }
            shopCandidates = Array.from(new Set(shopCandidates));
            const shopPick = pickUniqueNear(shopCandidates, 'shop');
            if (shopPick.status === 'CANDIDATE') {
                if (isShopNoise(shopPick.el)) {
                    result.shop = {
                        status: 'NOT_FOUND',
                        match_count: shopPick.match_count,
                        reason: 'candidate matched service-promise boilerplate, not shop name',
                        discovery: 'text_clue:shop_hints',
                        sample_text: textOf(shopPick.el).slice(0, 80),
                    };
                } else {
                    const container = minimalSafeContainer(shopPick.el, {
                        requireText: '',
                        maxHeight: Math.min(maxH, 160),
                    });
                    let shopEl = container;
                    if (!shopEl) {
                        const r = shopPick.el.getBoundingClientRect();
                        if (
                            visible(shopPick.el)
                            && !containsShip(shopPick.el)
                            && !isShopNoise(shopPick.el)
                            && r.height <= 160
                            && r.width <= vw * maxWRatio
                        ) {
                            shopEl = shopPick.el;
                        }
                    }
                    if (!shopEl || containsShip(shopEl) || isShopNoise(shopEl)) {
                        result.shop = {
                            status: 'UNSAFE_REGION',
                            match_count: shopPick.match_count,
                            reason: 'shop container unsafe, includes 送至, or service boilerplate',
                            discovery: 'text_clue:shop_hints',
                        };
                    } else {
                        result.shop = {
                            status: 'LOCATED',
                            match_count: shopPick.match_count,
                            visible: true,
                            discovery: 'text_clue:shop_hints',
                            sample_text: textOf(shopEl).slice(0, 80),
                            box: boxOf(shopEl),
                        };
                    }
                }
            } else {
                result.shop = {
                    status: shopPick.status === 'AMBIGUOUS' ? 'AMBIGUOUS' : 'NOT_FOUND',
                    match_count: shopPick.match_count || 0,
                    reason: shopPick.reason || null,
                    discovery: 'text_clue:shop_hints',
                };
            }

            return result;
        }""",
        {
            "model": model_clue,
            "selectedClue": CLUE_SELECTED,
            "promoClue": CLUE_PROMO,
            "shipTo": CLUE_SHIP_TO,
            "shopHints": list(SHOP_HINTS),
            "shopExclude": list(SHOP_EXCLUDE_PHRASES),
            "maxH": MAX_REGION_HEIGHT_PX,
            "maxWRatio": MAX_REGION_WIDTH_RATIO,
        },
    )
    if not isinstance(result, dict):
        result = {"ok": False, "reason": "discover_regions_non_dict"}
    # Always re-read selected_spec via shared locator (same as before/after state).
    result["selected_spec"] = locate_selected_spec_region(page)
    return result


def snapshot_selection_state(page: Page) -> dict[str, Any]:
    """
    Capture product id + minimal 已选 core (option + qty) via shared locator.
    Fresh DOM every call — never reuse discovery sample_text or business config.
    """
    meta = page.evaluate(
        """() => {
            const url = location.href;
            let productId = null;
            const m = url.match(/\\/product\\/(\\d+)/);
            if (m) productId = m[1];
            let variantToken = null;
            const skuEl = document.querySelector(
                '[data-sku].selected, [data-sku].cur, .sku.selected [data-sku], .selected[data-sku]'
            );
            if (skuEl && skuEl.getAttribute) {
                variantToken = skuEl.getAttribute('data-sku');
            }
            if (!variantToken) {
                const skuM = url.match(/[?&#]skuId=(\\d+)/i);
                if (skuM) variantToken = skuM[1];
            }
            return {
                url,
                product_id: productId,
                variant_token: variantToken,
                main_price_present: !!document.querySelector('#main_price'),
                scroll_y: window.scrollY,
            };
        }"""
    )
    located = locate_selected_spec_region(page)
    # Prefer core_blob (noise-stripped); fall back to sample_text then UNKNOWN.
    blob = None
    if isinstance(located, dict):
        blob = located.get("core_blob") or located.get("sample_text")
    parsed = parse_core_selected_spec(str(blob) if blob else None)
    if parsed.get("core_status") != "EXTRACTED" and blob:
        parsed = parse_core_selected_spec(strip_selection_noise(str(blob)))
    out = {
        "url": (meta or {}).get("url"),
        "product_id": (meta or {}).get("product_id"),
        "variant_token": (meta or {}).get("variant_token"),
        "main_price_present": (meta or {}).get("main_price_present"),
        "scroll_y": (meta or {}).get("scroll_y"),
        "locator_status": (located or {}).get("status"),
        "locator_reason": (located or {}).get("reason"),
        "selected_spec_text_raw_preview": (
            None
            if not (located or {}).get("sample_text")
            else str(located.get("sample_text"))[:80]
        ),
        **parsed,
    }
    out["selected_spec_text"] = parsed.get("core_selected_spec")
    return out


def wait_for_core_selection_stable(page: Page) -> dict[str, Any]:
    """
    Bounded wait until core selection (option+qty+product_id) repeats stably.
    Does not treat async 保障服务 appearance as a change.
    """
    deadline = utc_now().timestamp() + (SELECTION_STABLE_TIMEOUT_MS / 1000.0)
    last_key: tuple[Any, ...] | None = None
    stable_hits = 0
    last_snap: dict[str, Any] = {}
    polls = 0
    while utc_now().timestamp() < deadline:
        polls += 1
        snap = snapshot_selection_state(page)
        last_snap = snap
        if snap.get("core_status") != "EXTRACTED":
            last_key = None
            stable_hits = 0
            page.wait_for_timeout(SELECTION_STABLE_POLLS_MS)
            continue
        key = (
            snap.get("product_id"),
            snap.get("variant_token"),
            snap.get("selected_option_text"),
            snap.get("purchase_qty"),
        )
        if key == last_key:
            stable_hits += 1
            if stable_hits >= SELECTION_STABLE_REQUIRED_POLLS:
                snap["stability"] = {
                    "status": "STABLE",
                    "polls": polls,
                    "stable_hits": stable_hits,
                }
                return snap
        else:
            last_key = key
            stable_hits = 1
        page.wait_for_timeout(SELECTION_STABLE_POLLS_MS)
    last_snap["stability"] = {
        "status": "TIMEOUT",
        "polls": polls,
        "stable_hits": stable_hits,
        "note": "有界等待结束；若 core 已提取仍可用于后续核对，但不能默认一致",
    }
    return last_snap


def scroll_box_into_view(page: Page, box: dict[str, float]) -> None:
    """Center the given viewport-relative box in the window (always scrolls)."""
    page.evaluate(
        """(b) => {
            const cy = b.y + b.height / 2 + window.scrollY;
            const target = Math.max(0, cy - window.innerHeight / 2);
            window.scrollTo({ top: target, left: 0, behavior: 'instant' });
        }""",
        {"y": box["y"], "height": box["height"]},
    )
    page.wait_for_timeout(LAYOUT_STABLE_MS)


def center_selector_in_viewport(page: Page, selector: str) -> dict[str, Any]:
    """
    Always scroll the selector to near vertical center of the viewport.
    Does not rely on scrollIntoViewIfNeeded (which may leave elements at the
    bottom edge behind fixed checkout bars).
    """
    return page.evaluate(
        """(sel) => {
            const el = document.querySelector(sel);
            if (!el) return { ok: false, error: 'selector_missing' };
            const before = el.getBoundingClientRect();
            const scrollBefore = window.scrollY;
            const cyDoc = before.top + before.height / 2 + window.scrollY;
            const target = Math.max(0, cyDoc - window.innerHeight / 2);
            window.scrollTo({ top: target, left: 0, behavior: 'instant' });
            const after = el.getBoundingClientRect();
            return {
                ok: true,
                scroll_before: scrollBefore,
                scroll_after: window.scrollY,
                target_scroll_y: target,
                box_before: {
                    x: before.left, y: before.top,
                    width: before.width, height: before.height
                },
                box_after_immediate: {
                    x: after.left, y: after.top,
                    width: after.width, height: after.height
                },
                viewport: { width: window.innerWidth, height: window.innerHeight },
            };
        }""",
        selector,
    )


def detect_fixed_chrome_occlusion(
    page: Page,
    content: dict[str, float],
    *,
    margin_px: float = PRICE_SAFE_MARGIN_PX,
) -> dict[str, Any]:
    """
    Detect overlap between the price box and fixed/sticky top/bottom chrome.
    Does not hide or rewrite page UI — diagnostics + suggested scroll delta only.
    """
    return page.evaluate(
        """({ content, marginPx }) => {
            const vpW = window.innerWidth;
            const vpH = window.innerHeight;
            const price = {
                left: content.x - marginPx,
                top: content.y - marginPx,
                right: content.x + content.width + marginPx,
                bottom: content.y + content.height + marginPx,
            };
            const intersects = (a, b) => !(
                a.right <= b.left || a.left >= b.right ||
                a.bottom <= b.top || a.top >= b.bottom
            );
            const chrome = [];
            const nodes = document.body ? document.body.querySelectorAll('*') : [];
            for (const node of nodes) {
                const st = getComputedStyle(node);
                if (st.position !== 'fixed' && st.position !== 'sticky') continue;
                if (st.visibility === 'hidden' || st.display === 'none') continue;
                const op = parseFloat(st.opacity || '1');
                if (op < 0.05) continue;
                const r = node.getBoundingClientRect();
                if (r.width < 40 || r.height < 16) continue;
                if (r.height > vpH * 0.55) continue; // skip near-fullscreen layers
                if (r.width < vpW * 0.35 && r.height < 80) continue; // tiny chips
                let band = 'other';
                if (r.top <= 8 && r.bottom < vpH * 0.45) band = 'top';
                else if (r.bottom >= vpH - 8 && r.top > vpH * 0.45) band = 'bottom';
                else if (r.top < vpH * 0.25 && r.height < vpH * 0.35) band = 'top';
                else if (r.bottom > vpH * 0.75 && r.height < vpH * 0.35) band = 'bottom';
                chrome.push({
                    band,
                    position: st.position,
                    x: r.left, y: r.top, width: r.width, height: r.height,
                    tag: node.tagName.toLowerCase(),
                    id: node.id || null,
                    className: typeof node.className === 'string'
                        ? node.className.slice(0, 80) : null,
                });
            }
            const topBars = chrome.filter((c) => c.band === 'top');
            const bottomBars = chrome.filter((c) => c.band === 'bottom');
            const overlapping = [];
            // Positive delta → increase scrollY → element moves up in viewport (clear bottom bar).
            // Negative delta → decrease scrollY → element moves down (clear top bar).
            let scroll_delta_y = 0;
            for (const bar of [...topBars, ...bottomBars]) {
                const b = {
                    left: bar.x, top: bar.y,
                    right: bar.x + bar.width, bottom: bar.y + bar.height,
                };
                if (!intersects(price, b)) continue;
                overlapping.push(bar);
                if (bar.band === 'bottom') {
                    const delta = Math.ceil(price.bottom - (b.top - marginPx));
                    if (delta > 0) scroll_delta_y = Math.max(scroll_delta_y, delta);
                } else if (bar.band === 'top') {
                    const delta = Math.ceil((b.bottom + marginPx) - price.top);
                    if (delta > 0) {
                        scroll_delta_y = scroll_delta_y === 0
                            ? -delta
                            : Math.min(scroll_delta_y, -delta);
                    }
                }
            }
            return {
                method: 'fixed_sticky_getBoundingClientRect_vs_price_box_with_margin',
                viewport: { width: vpW, height: vpH },
                scroll_y: window.scrollY,
                price_box_checked: price,
                margin_px: marginPx,
                fixed_or_sticky_count: chrome.length,
                top_bar_count: topBars.length,
                bottom_bar_count: bottomBars.length,
                overlapping,
                occluded: overlapping.length > 0,
                suggested_scroll_delta_y: scroll_delta_y,
                notes: overlapping.length
                    ? 'price overlaps fixed/sticky chrome'
                    : 'no overlap with classified top/bottom fixed chrome',
            };
        }""",
        {"content": content, "marginPx": margin_px},
    )


def apply_scroll_delta(page: Page, delta_y: float) -> float:
    """Adjust window.scrollY by delta_y (positive = scroll down / content moves up)."""
    if not delta_y:
        return float(page.evaluate("() => window.scrollY"))
    return float(
        page.evaluate(
            """(d) => {
                const next = Math.max(0, window.scrollY + d);
                window.scrollTo({ top: next, left: 0, behavior: 'instant' });
                return window.scrollY;
            }""",
            float(delta_y),
        )
    )


def remesaure_selector_box(page: Page, selector: str) -> dict[str, float] | None:
    data = page.evaluate(
        """(sel) => {
            const el = document.querySelector(sel);
            if (!el) return null;
            const r = el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) return null;
            return {
                x: r.left, y: r.top, width: r.width, height: r.height,
                viewport: { width: window.innerWidth, height: window.innerHeight },
                scroll_y: window.scrollY,
            };
        }""",
        selector,
    )
    return data


def screenshot_clip(
    page: Page,
    out_path: Path,
    content: dict[str, float],
    *,
    pad_x: float,
    pad_y: float,
    viewport: dict[str, float],
) -> dict[str, Any]:
    clipped = build_padded_clip(content, pad_x=pad_x, pad_y=pad_y, viewport=viewport)
    result: dict[str, Any] = {
        "clip_box_css_viewport": clipped,
        "status": "FAILED",
        "error": None,
        "captured_at_utc": None,
        "pixel_size": None,
    }
    if clipped is None:
        result["status"] = "CLIP_OUT_OF_VIEWPORT"
        result["error"] = "padded clip empty after viewport clamp"
        return result
    if not clip_fully_covers(content, clipped):
        result["status"] = "CONTENT_CLIPPED_BY_VIEWPORT"
        result["error"] = "viewport clamp would crop content; refusing incomplete shot"
        return result

    captured_at = utc_now()
    try:
        page.screenshot(
            path=str(out_path),
            clip=clipped,
            type="png",
            scale="css",
        )
    except PlaywrightError as exc:
        result["status"] = "SCREENSHOT_FAILED"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    result["captured_at_utc"] = utc_iso(captured_at)
    size = png_pixel_size(out_path)
    result["pixel_size"] = (
        {"width": size[0], "height": size[1]} if size else None
    )
    result["status"] = "CAPTURED"
    return result


def capture_price(page: Page, out_path: Path) -> dict[str, Any]:
    """
    Locate #main_price, always center it in the viewport, clear fixed chrome
    occlusion with bounded scroll tweaks, then clip-screenshot the price region.
    Does not hide bars, rewrite text, or treat PNG save as content-complete.
    """
    info: dict[str, Any] = {
        "region": "price",
        "filename": out_path.name,
        "path": str(out_path.resolve()),
        "capture_method": "automatic",
        "selector": MAIN_PRICE_SELECTOR,
        "status": "FAILED",
        "save_status": "NOT_SAVED",
        "content_quality_status": "UNCONFIRMED",
        "match_count": 0,
        "visible": False,
        "integrity": "PENDING_USER_VERIFY",
        "coordinate_space": "css_viewport_px",
        "error": None,
        "diagnostics": {
            "centering_forced": True,
            "occlusion_adjustments": 0,
            "occlusion_checks": [],
            "scroll_before": None,
            "scroll_after_center": None,
            "scroll_final": None,
            "price_box_before_scroll": None,
            "price_box_after_scroll": None,
            "viewport": None,
            "pad_x": PRICE_PAD_X_PX,
            "pad_y": PRICE_PAD_Y_PX,
        },
    }
    diag = info["diagnostics"]

    try:
        page.wait_for_selector(
            MAIN_PRICE_SELECTOR, timeout=MAIN_PRICE_WAIT_MS, state="attached"
        )
    except PlaywrightError as exc:
        info["match_count"] = page.locator(MAIN_PRICE_SELECTOR).count()
        info["status"] = "SELECTOR_WAIT_FAILED"
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info

    loc = page.locator(MAIN_PRICE_SELECTOR)
    info["match_count"] = loc.count()
    if info["match_count"] != 1:
        info["status"] = "MATCH_COUNT_NOT_ONE"
        info["error"] = f"expected 1, got {info['match_count']}"
        return info

    target = loc.first
    info["visible"] = bool(target.is_visible())
    if not info["visible"]:
        # Still attempt centering — may become visible after scroll.
        pass

    # Snapshot before any centering scroll (do not reuse these coords later).
    pre = remesaure_selector_box(page, MAIN_PRICE_SELECTOR)
    if pre:
        diag["price_box_before_scroll"] = {
            "x": float(pre["x"]),
            "y": float(pre["y"]),
            "width": float(pre["width"]),
            "height": float(pre["height"]),
        }
        diag["scroll_before"] = float(pre.get("scroll_y") or 0)
        diag["viewport"] = {
            "width": float(pre["viewport"]["width"]),
            "height": float(pre["viewport"]["height"]),
        }

    # Always center — even when already "in view" near the bottom chrome.
    center = center_selector_in_viewport(page, MAIN_PRICE_SELECTOR)
    diag["center_scroll"] = center
    page.wait_for_timeout(LAYOUT_STABLE_MS)
    info["font_ready"] = wait_fonts_ready(page)
    page.wait_for_timeout(LAYOUT_STABLE_MS)

    measured = measure_price_ink_box(page)
    if not measured:
        info["status"] = "MEASURE_FAILED"
        info["error"] = "price ink box empty after center scroll"
        info["content_quality_status"] = "MEASURE_FAILED"
        return info

    content = {
        "x": float(measured["x"]),
        "y": float(measured["y"]),
        "width": float(measured["width"]),
        "height": float(measured["height"]),
    }
    vp = {
        "width": float(measured["viewport"]["width"]),
        "height": float(measured["viewport"]["height"]),
    }
    diag["price_box_after_scroll"] = dict(content)
    diag["scroll_after_center"] = float(measured.get("scroll_y") or 0)
    diag["viewport"] = dict(vp)

    info["visible"] = bool(target.is_visible())
    if not info["visible"]:
        info["status"] = "NOT_VISIBLE"
        info["content_quality_status"] = "NOT_VISIBLE"
        return info

    # Bounded occlusion adjustments against fixed top/bottom chrome.
    occluded_final = False
    last_occ: dict[str, Any] | None = None
    for _ in range(MAX_PRICE_OCCLUSION_ADJUSTMENTS):
        last_occ = detect_fixed_chrome_occlusion(
            page, content, margin_px=PRICE_SAFE_MARGIN_PX
        )
        diag["occlusion_checks"].append(last_occ)
        if not last_occ.get("occluded"):
            occluded_final = False
            break
        occluded_final = True
        delta = float(last_occ.get("suggested_scroll_delta_y") or 0)
        if abs(delta) < 1:
            break
        apply_scroll_delta(page, delta)
        diag["occlusion_adjustments"] = int(diag["occlusion_adjustments"]) + 1
        page.wait_for_timeout(LAYOUT_STABLE_MS)
        measured = measure_price_ink_box(page)
        if not measured:
            info["status"] = "MEASURE_FAILED"
            info["error"] = "price ink box empty after occlusion adjust"
            info["content_quality_status"] = "MEASURE_FAILED"
            return info
        content = {
            "x": float(measured["x"]),
            "y": float(measured["y"]),
            "width": float(measured["width"]),
            "height": float(measured["height"]),
        }
        vp = {
            "width": float(measured["viewport"]["width"]),
            "height": float(measured["viewport"]["height"]),
        }

    # Final occlusion check after adjustments (or if loop never ran clear).
    last_occ = detect_fixed_chrome_occlusion(
        page, content, margin_px=PRICE_SAFE_MARGIN_PX
    )
    diag["occlusion_checks"].append({**last_occ, "phase": "final"})
    occluded_final = bool(last_occ.get("occluded"))
    diag["scroll_final"] = float(last_occ.get("scroll_y") or page.evaluate("() => window.scrollY"))
    diag["price_box_final"] = dict(content)
    diag["occlusion_final"] = {
        "occluded": occluded_final,
        "method": last_occ.get("method"),
        "overlapping": last_occ.get("overlapping"),
        "notes": last_occ.get("notes"),
    }

    # Soft check: is price near vertical center band?
    cy = content["y"] + content["height"] / 2
    band = PRICE_CENTER_BAND_RATIO * vp["height"]
    mid = vp["height"] / 2
    diag["center_band_ok"] = abs(cy - mid) <= band + PRICE_PAD_Y_PX

    info["content_box_css_viewport"] = {
        **content,
        "layout_box": measured.get("layout_box"),
        "rect_count": measured.get("rect_count"),
        "label_included": measured.get("label_included"),
        "device_pixel_ratio": measured.get("device_pixel_ratio"),
        "scroll_y": measured.get("scroll_y"),
    }

    if (
        content["width"] < MIN_PRICE_CONTENT_WIDTH
        or content["height"] < MIN_PRICE_CONTENT_HEIGHT
    ):
        info["status"] = "CONTENT_BOX_TOO_SMALL"
        info["integrity"] = "NOT_ACCEPTED"
        info["save_status"] = "NOT_SAVED"
        info["content_quality_status"] = "CONTENT_BOX_TOO_SMALL"
        info["error"] = f"content too small: {content['width']}x{content['height']}"
        return info

    if occluded_final:
        info["content_quality_status"] = "OCCLUDED_OR_UNCLEAR"
        info["error"] = (
            "价格区域仍可能被固定顶/底栏遮挡；已做有界滚动调整仍未清除。"
            "PNG 若保存成功也不代表价格完整可识别。"
        )
    else:
        info["content_quality_status"] = "GEOMETRY_OK_CONTENT_UNCONFIRMED"

    # Ensure padded clip can fully cover content inside viewport; if not, refuse.
    probe_clip = build_padded_clip(
        content, pad_x=PRICE_PAD_X_PX, pad_y=PRICE_PAD_Y_PX, viewport=vp
    )
    if probe_clip is None or not clip_fully_covers(content, probe_clip):
        info["status"] = "CONTENT_CLIPPED_BY_VIEWPORT"
        info["save_status"] = "NOT_SAVED"
        info["content_quality_status"] = "VIEWPORT_CANNOT_FIT_PADDED_REGION"
        info["error"] = (
            info.get("error") or ""
        ) + "；留白后的价格截图矩形无法完整落在可视区域内"
        info["error"] = info["error"].lstrip("；")
        info["clip_box_css_viewport"] = probe_clip
        info["integrity"] = "NOT_ACCEPTED"
        return info

    shot = screenshot_clip(
        page,
        out_path,
        content,
        pad_x=PRICE_PAD_X_PX,
        pad_y=PRICE_PAD_Y_PX,
        viewport=vp,
    )
    info["clip_box_css_viewport"] = shot.get("clip_box_css_viewport")
    info["captured_at_utc"] = shot.get("captured_at_utc")
    info["pixel_size"] = shot.get("pixel_size")
    if shot.get("error"):
        info["error"] = (
            (str(info.get("error") or "") + "; " if info.get("error") else "")
            + str(shot["error"])
        )

    if shot["status"] != "CAPTURED":
        info["status"] = shot["status"]
        info["save_status"] = "NOT_SAVED"
        info["integrity"] = "NOT_ACCEPTED"
        if info["content_quality_status"] == "GEOMETRY_OK_CONTENT_UNCONFIRMED":
            info["content_quality_status"] = "SAVE_FAILED"
        return info

    info["save_status"] = "SAVED"
    info["status"] = "CAPTURED"
    # Geometry completeness ≠ pixel OCR completeness.
    if occluded_final:
        info["integrity"] = "NOT_ACCEPTED"
        info["status"] = "CAPTURED_BUT_OCCLUDED"
    else:
        info["integrity"] = "PENDING_USER_VERIFY"
    info["price_label_coverage"] = (
        "PENDING_USER_VERIFY"
        if measured.get("label_included")
        else "价格标签可能未覆盖，价格口径待核验"
    )
    info["notes"] = [
        "save_status=SAVED 仅表示 PNG 写入成功",
        "content_quality_status 为几何/遮挡诊断；像素数字是否完整需 OCR/人工确认",
        "未隐藏下单栏、未改写价格文本、未使用历史观察价",
    ]
    return info


def _region_safe_scroll_delta(
    content: dict[str, float],
    viewport: dict[str, float],
    *,
    pad_y: float = REGION_PAD_Y_PX,
    margin: float = REGION_SAFE_MARGIN_PX,
) -> float:
    """
    Scroll delta so content (+pad) sits inside the viewport safe band.
    Positive delta = scroll down (content moves up).
    """
    top_limit = pad_y + margin
    bot_limit = float(viewport["height"]) - pad_y - margin
    y0 = float(content["y"])
    y1 = y0 + float(content["height"])
    if y0 < top_limit:
        return y0 - top_limit  # negative → scroll up → content moves down
    if y1 > bot_limit:
        return y1 - bot_limit
    return 0.0


def capture_discovered_region(
    page: Page,
    out_path: Path,
    *,
    region: str,
    discovery: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Per-region capture: independent scroll → stabilize → rediscover →
    safe-band / occlusion adjust → remeasure → clip.
    Does not reuse prior region coordinates or scroll state as final geometry.
    """
    info: dict[str, Any] = {
        "region": region,
        "filename": out_path.name,
        "path": str(out_path.resolve()),
        "capture_method": "automatic",
        "status": "FAILED",
        "integrity": "PENDING_USER_VERIFY",
        "coordinate_space": "css_viewport_px",
        "discovery": None,
        "error": None,
        "diagnostics": {
            "independent_positioning": True,
            "position_adjustments": 0,
            "occlusion_checks": [],
            "scroll_trace": [],
        },
    }
    if not discovery:
        info["status"] = "NOT_FOUND"
        info["error"] = "no discovery payload"
        return info

    info["discovery"] = {
        k: discovery.get(k)
        for k in (
            "status",
            "match_count",
            "visible",
            "discovery",
            "sample_text",
            "reason",
            "sample_texts",
            "candidate_diagnostics",
            "raw_hit_count",
            "deduped_hit_count",
            "deduped_nested",
            "deduped_same_text",
        )
        if k in discovery
    }
    status = discovery.get("status")
    if status in {"NOT_FOUND", "AMBIGUOUS", "UNSAFE_REGION"}:
        info["status"] = str(status)
        info["error"] = discovery.get("reason")
        if status == "AMBIGUOUS":
            info["candidate_diagnostics"] = discovery.get("candidate_diagnostics")
            info["sample_texts"] = discovery.get("sample_texts")
        return info
    if status != "LOCATED" or not isinstance(discovery.get("box"), dict):
        info["status"] = "NOT_LOCATED"
        info["error"] = f"unexpected discovery status: {status}"
        return info

    hint_box = {
        "x": float(discovery["box"]["x"]),
        "y": float(discovery["box"]["y"]),
        "width": float(discovery["box"]["width"]),
        "height": float(discovery["box"]["height"]),
    }
    if hint_box["height"] > MAX_REGION_HEIGHT_PX:
        info["status"] = "UNSAFE_REGION"
        info["error"] = f"region height {hint_box['height']} exceeds limit"
        return info

    # Fresh positioning for THIS region only (hint scroll, then always rediscover).
    scroll_before = float(page.evaluate("() => window.scrollY"))
    scroll_box_into_view(page, hint_box)
    page.wait_for_timeout(LAYOUT_STABLE_MS)
    info["diagnostics"]["scroll_trace"].append(
        {
            "phase": "initial_center",
            "scroll_before": scroll_before,
            "scroll_after": float(page.evaluate("() => window.scrollY")),
            "hint_box": hint_box,
        }
    )

    content: dict[str, float] | None = None
    again: dict[str, Any] | None = None
    viewport = {"width": 0.0, "height": 0.0}

    for attempt in range(MAX_REGION_POSITION_ADJUSTMENTS + 1):
        rediscovered = discover_regions(page)
        again = (rediscovered or {}).get(region)
        if not again or again.get("status") != "LOCATED" or not again.get("box"):
            info["status"] = "LOST_AFTER_SCROLL"
            info["error"] = "region not stably locatable after scroll"
            info["discovery_after_scroll"] = again
            return info

        content = {
            "x": float(again["box"]["x"]),
            "y": float(again["box"]["y"]),
            "width": float(again["box"]["width"]),
            "height": float(again["box"]["height"]),
        }
        vp = page.evaluate(
            "() => ({ width: window.innerWidth, height: window.innerHeight })"
        )
        viewport = {"width": float(vp["width"]), "height": float(vp["height"])}

        if CLUE_SHIP_TO in str(again.get("sample_text") or ""):
            info["status"] = "UNSAFE_REGION"
            info["error"] = "sample text contains 送至 after scroll"
            info["content_box_css_viewport"] = content
            info["sample_text"] = again.get("sample_text")
            return info

        safe_delta = _region_safe_scroll_delta(content, viewport)
        occ = detect_fixed_chrome_occlusion(
            page, content, margin_px=REGION_SAFE_MARGIN_PX
        )
        info["diagnostics"]["occlusion_checks"].append(
            {**occ, "attempt": attempt, "content_box": content}
        )
        occ_delta = float(occ.get("suggested_scroll_delta_y") or 0)
        # Prefer occlusion clearance when chrome overlaps; else safe-band delta.
        delta = occ_delta if occ.get("occluded") and abs(occ_delta) >= 1 else safe_delta

        probe = build_padded_clip(
            content,
            pad_x=REGION_PAD_X_PX,
            pad_y=REGION_PAD_Y_PX,
            viewport=viewport,
        )
        covers = probe is not None and clip_fully_covers(content, probe)
        if abs(delta) < 1 and covers and not occ.get("occluded"):
            break
        if attempt >= MAX_REGION_POSITION_ADJUSTMENTS:
            break
        if abs(delta) < 1 and not covers:
            # Content still clipped at viewport edge — nudge toward center.
            cy = content["y"] + content["height"] / 2
            delta = cy - float(viewport["height"]) / 2
        if abs(delta) < 1:
            break
        apply_scroll_delta(page, delta)
        page.wait_for_timeout(LAYOUT_STABLE_MS)
        info["diagnostics"]["position_adjustments"] += 1
        info["diagnostics"]["scroll_trace"].append(
            {
                "phase": "adjust",
                "attempt": attempt,
                "delta_y": delta,
                "scroll_y": float(page.evaluate("() => window.scrollY")),
                "occluded": bool(occ.get("occluded")),
            }
        )

    assert content is not None and again is not None
    # Final rediscover after last adjust (geometry must be post-scroll).
    rediscovered = discover_regions(page)
    again = (rediscovered or {}).get(region)
    if not again or again.get("status") != "LOCATED" or not again.get("box"):
        info["status"] = "LOST_AFTER_SCROLL"
        info["error"] = "region lost after final position adjust"
        info["discovery_after_scroll"] = again
        return info
    content = {
        "x": float(again["box"]["x"]),
        "y": float(again["box"]["y"]),
        "width": float(again["box"]["width"]),
        "height": float(again["box"]["height"]),
    }
    vp = page.evaluate(
        "() => ({ width: window.innerWidth, height: window.innerHeight })"
    )
    viewport = {"width": float(vp["width"]), "height": float(vp["height"])}
    final_occ = detect_fixed_chrome_occlusion(
        page, content, margin_px=REGION_SAFE_MARGIN_PX
    )
    info["diagnostics"]["occlusion_final"] = final_occ
    info["diagnostics"]["content_box_final"] = content
    info["diagnostics"]["scroll_final"] = float(page.evaluate("() => window.scrollY"))

    info["content_box_css_viewport"] = content
    info["sample_text"] = again.get("sample_text")
    info["match_count"] = again.get("match_count")
    info["visible"] = again.get("visible")

    if CLUE_SHIP_TO in str(again.get("sample_text") or ""):
        info["status"] = "UNSAFE_REGION"
        info["error"] = "sample text contains 送至 after scroll"
        return info

    shot = screenshot_clip(
        page,
        out_path,
        content,
        pad_x=REGION_PAD_X_PX,
        pad_y=REGION_PAD_Y_PX,
        viewport=viewport,
    )
    info["clip_box_css_viewport"] = shot.get("clip_box_css_viewport")
    info["captured_at_utc"] = shot.get("captured_at_utc")
    info["pixel_size"] = shot.get("pixel_size")
    info["error"] = shot.get("error")
    if shot["status"] != "CAPTURED":
        info["status"] = shot["status"]
        info["integrity"] = "NOT_ACCEPTED"
        if final_occ.get("occluded"):
            info["error"] = (
                (info.get("error") or shot.get("status"))
                + "; fixed chrome still occluding after bounded adjusts"
            )
        return info
    if final_occ.get("occluded"):
        info["status"] = "CAPTURED_BUT_OCCLUDED"
        info["integrity"] = "PENDING_USER_VERIFY"
        info["error"] = "captured but fixed chrome still overlaps safe margin"
        return info
    info["status"] = "CAPTURED"
    return info


def write_metadata(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def capture_ambiguous_diagnostics(
    page: Page,
    out_dir: Path,
    *,
    region: str,
    discovery: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """
    When discovery is AMBIGUOUS, auto-clip each candidate box for local diagnosis.
    Does not require the user to copy DOM nodes.
    """
    if not discovery or discovery.get("status") != "AMBIGUOUS":
        return None
    diags = discovery.get("candidate_diagnostics") or []
    if not isinstance(diags, list) or not diags:
        return {
            "status": "NO_CANDIDATE_BOXES",
            "note": "AMBIGUOUS without candidate_diagnostics payload",
        }

    diag_dir = out_dir / "diagnostics" / region
    diag_dir.mkdir(parents=True, exist_ok=True)
    vp_raw = page.evaluate(
        "() => ({ width: window.innerWidth, height: window.innerHeight })"
    )
    viewport = {"width": float(vp_raw["width"]), "height": float(vp_raw["height"])}
    saved: list[dict[str, Any]] = []
    for idx, item in enumerate(diags[:5]):
        box = item.get("box") if isinstance(item, dict) else None
        if not isinstance(box, dict):
            continue
        content = {
            "x": float(box.get("x") or 0),
            "y": float(box.get("y") or 0),
            "width": float(box.get("width") or 0),
            "height": float(box.get("height") or 0),
        }
        if content["width"] < 8 or content["height"] < 8:
            continue
        out_path = diag_dir / f"candidate_{idx}.png"
        shot = screenshot_clip(
            page,
            out_path,
            content,
            pad_x=REGION_PAD_X_PX,
            pad_y=REGION_PAD_Y_PX,
            viewport=viewport,
        )
        saved.append(
            {
                "index": idx,
                "sample_text": (item.get("sample_text") if isinstance(item, dict) else None),
                "score": (item.get("score") if isinstance(item, dict) else None),
                "box": content,
                "path": str(out_path.resolve()) if out_path.is_file() else None,
                "shot_status": shot.get("status"),
                "error": shot.get("error"),
            }
        )
    meta = {
        "status": "CAPTURED" if any(s.get("path") for s in saved) else "FAILED",
        "reason": discovery.get("reason"),
        "candidates": saved,
    }
    (diag_dir / "candidates.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return meta


def required_evidence_ready(regions: dict[str, dict[str, Any]]) -> bool:
    """Price + title + selected_spec cleanly captured (shop/promo optional)."""
    price_ok = str(regions.get("price", {}).get("status", "")) == "CAPTURED"
    title_ok = str(regions.get("product_title", {}).get("status", "")) == "CAPTURED"
    spec_ok = str(regions.get("selected_spec", {}).get("status", "")) == "CAPTURED"
    return price_ok and title_ok and spec_ok


def all_regions_captured(regions: dict[str, dict[str, Any]]) -> bool:
    for key in ("price", "product_title", "selected_spec", "promotions", "shop"):
        if str(regions.get(key, {}).get("status", "")) != "CAPTURED":
            return False
    return True


def overall_status(regions: dict[str, dict[str, Any]]) -> str:
    """
    Distinguish required-evidence readiness from full multi-region completion.
    Shop/promo failure must not be reported as全部完整.
    """
    price_status = str(regions.get("price", {}).get("status", ""))
    price_ok = price_status == "CAPTURED"
    price_partial = price_status.startswith("CAPTURED")
    if all_regions_captured(regions):
        return "COMPLETE_PENDING_USER_VERIFY"
    if required_evidence_ready(regions):
        return "REQUIRED_EVIDENCE_READY"
    if price_ok or price_partial:
        return "PARTIAL"
    return "FAILED"


def run_geometry_fixture_check() -> dict[str, Any]:
    """Local page only: verify scrolled clip covers oversized ink box. Not JD acceptance."""
    if not FIXTURE_PATH.is_file():
        return {"ok": False, "error": f"fixture missing: {FIXTURE_PATH}"}

    report: dict[str, Any] = {"ok": False, "fixture": str(FIXTURE_PATH)}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            context = browser.new_context(viewport={"width": 390, "height": 700})
            page = context.new_page()
            page.goto(FIXTURE_PATH.as_uri(), wait_until="domcontentloaded")
            page.locator("#main_price").scroll_into_view_if_needed()
            page.wait_for_timeout(200)
            measured = measure_price_ink_box(page)
            if not measured:
                report["error"] = "fixture measure failed"
                return report
            content = {
                "x": float(measured["x"]),
                "y": float(measured["y"]),
                "width": float(measured["width"]),
                "height": float(measured["height"]),
            }
            vp = {
                "width": float(measured["viewport"]["width"]),
                "height": float(measured["viewport"]["height"]),
            }
            clipped = build_padded_clip(
                content,
                pad_x=PRICE_PAD_X_PX,
                pad_y=PRICE_PAD_Y_PX,
                viewport=vp,
            )
            report["measured"] = content
            report["layout_box"] = measured.get("layout_box")
            report["clip"] = clipped
            report["scroll_y"] = measured.get("scroll_y")
            report["dpr"] = measured.get("device_pixel_ratio")
            if clipped is None or not clip_fully_covers(content, clipped):
                report["error"] = "clip does not fully cover content"
                return report
            # ink taller than layout height in fixture by design
            layout_h = float((measured.get("layout_box") or {}).get("height") or 0)
            report["ink_taller_than_layout"] = content["height"] >= layout_h
            out = CAPTURE_ROOT.parent / "_geometry_fixture_probe.png"
            # Do not use CAPTURE_ROOT run dirs; write a throwaway under artifacts root.
            out.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out), clip=clipped, type="png", scale="css")
            size = png_pixel_size(out)
            report["probe_png_size"] = (
                {"width": size[0], "height": size[1]} if size else None
            )
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass
            report["ok"] = True
        finally:
            browser.close()
    return report


def run_region_logic_unit_checks() -> list[str]:
    """Pure logic checks for unsafe/ambiguous handling (not live JD)."""
    failures: list[str] = []
    # clamp edge
    vp = {"width": 390.0, "height": 700.0}
    c = {"x": 10.0, "y": 680.0, "width": 60.0, "height": 30.0}
    clip = build_padded_clip(c, pad_x=18, pad_y=16, viewport=vp)
    if clip is None:
        failures.append("expected clamp overlap near bottom")
    elif clip_fully_covers(c, clip):
        # content bottom 710 > viewport 700 => cannot fully cover
        failures.append("bottom-overflow content should not fully cover after clamp")
    # mid page should cover
    c2 = {"x": 20.0, "y": 200.0, "width": 70.0, "height": 30.0}
    clip2 = build_padded_clip(c2, pad_x=PRICE_PAD_X_PX, pad_y=PRICE_PAD_Y_PX, viewport=vp)
    if clip2 is None or not clip_fully_covers(c2, clip2):
        failures.append("mid-page padded clip should fully cover content")
    return failures


def run_selection_core_unit_checks() -> list[str]:
    """Core 已选 parse/compare — no browser / no JD."""
    failures: list[str] = []
    core = "已选 1层200抽*20包【超大尺寸】，1个"
    with_service = (
        core + " 本商品支持京东保障服务，点击可选服务 送至 北京朝阳区 "
        "现货今晚达 活动 充多送多"
    )
    p1 = parse_core_selected_spec(core)
    p2 = parse_core_selected_spec(with_service)
    if p1.get("core_status") != "EXTRACTED" or p2.get("core_status") != "EXTRACTED":
        failures.append("core extract should succeed with/without 保障服务")
    if p1.get("core_selected_spec") != p2.get("core_selected_spec"):
        failures.append("保障服务 async text must not change core spec")
    if p1.get("selected_option_text") != "1层200抽*20包【超大尺寸】":
        failures.append("option must keep 【超大尺寸】 semantics")
    if p1.get("purchase_qty") != "1":
        failures.append("purchase qty should be 1")

    before = {
        "product_id": "100005149674",
        "variant_token": None,
        **p1,
    }
    after_service = {
        "product_id": "100005149674",
        "variant_token": None,
        **p2,
    }
    same = compare_selection_states(before, after_service)
    if same.get("consistent") is not True:
        failures.append("only 保障服务 added → must be consistent")

    after_packs = {
        "product_id": "100005149674",
        "variant_token": None,
        **parse_core_selected_spec("已选 1层200抽*10包【超大尺寸】，1个"),
    }
    diff_packs = compare_selection_states(before, after_packs)
    if diff_packs.get("consistent") is not False:
        failures.append("20包→10包 must be inconsistent")

    after_qty = {
        "product_id": "100005149674",
        "variant_token": None,
        **parse_core_selected_spec("已选 1层200抽*20包【超大尺寸】，2个"),
    }
    diff_qty = compare_selection_states(before, after_qty)
    if diff_qty.get("consistent") is not False:
        failures.append("数量1→2 must be inconsistent")

    after_size = {
        "product_id": "100005149674",
        "variant_token": None,
        **parse_core_selected_spec("已选 1层200抽*20包【小尺寸】，1个"),
    }
    diff_size = compare_selection_states(before, after_size)
    if diff_size.get("consistent") is not False:
        failures.append("尺寸选项变化 must be inconsistent")

    after_pid = {
        "product_id": "999999999999",
        "variant_token": None,
        **p1,
    }
    diff_pid = compare_selection_states(before, after_pid)
    if diff_pid.get("consistent") is not False:
        failures.append("product_id change must be inconsistent")

    unknown = compare_selection_states(
        before,
        {"product_id": "100005149674", "core_status": "UNKNOWN"},
    )
    if unknown.get("consistent") is not None or unknown.get("consistency_status") != "UNKNOWN":
        failures.append("extract failure must be UNKNOWN, not default consistent")

    # Negative y needs upward scroll (negative delta).
    delta = _region_safe_scroll_delta(
        {"x": 15.0, "y": -3.33, "width": 200.0, "height": 64.0},
        {"width": 1280.0, "height": 720.0},
    )
    if delta >= 0:
        failures.append("y≈-3 content should request negative scroll delta")

    # overall status must not claim complete when shop failed
    st = overall_status(
        {
            "price": {"status": "CAPTURED"},
            "product_title": {"status": "CAPTURED"},
            "selected_spec": {"status": "CAPTURED"},
            "promotions": {"status": "CAPTURED"},
            "shop": {"status": "CONTENT_CLIPPED_BY_VIEWPORT"},
        }
    )
    if st != "REQUIRED_EVIDENCE_READY":
        failures.append(f"shop fail should be REQUIRED_EVIDENCE_READY, got {st}")

    return failures


DISCOVERY_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "p2d_discovery_fixture.html"
)
REGION_SCROLL_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "p2d_region_scroll_fixture.html"
)
SPEC_LABEL_SIBLING_FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "p2d_selected_spec_label_sibling_fixture.html"
)
SPEC_LABEL_ONLY_FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "p2d_selected_spec_label_only_fixture.html"
)


def run_selected_spec_locator_fixture_check() -> dict[str, Any]:
    """
    Local DOM: 已选 label + sibling option/qty; warranty async; label-only → UNKNOWN.
    Not live JD.
    """
    report: dict[str, Any] = {"ok": False, "checks": {}}
    if not SPEC_LABEL_SIBLING_FIXTURE_PATH.is_file():
        report["error"] = f"fixture missing: {SPEC_LABEL_SIBLING_FIXTURE_PATH}"
        return report
    if not SPEC_LABEL_ONLY_FIXTURE_PATH.is_file():
        report["error"] = f"fixture missing: {SPEC_LABEL_ONLY_FIXTURE_PATH}"
        return report

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            context = browser.new_context(viewport={"width": 780, "height": 700})
            page = context.new_page()

            page.goto(
                SPEC_LABEL_SIBLING_FIXTURE_PATH.as_uri(),
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(50)
            located_before = locate_selected_spec_region(page)
            snap_before = snapshot_selection_state(page)
            # Let async warranty appear; core must stay the same.
            page.wait_for_timeout(150)
            located_after_w = locate_selected_spec_region(page)
            snap_after_w = snapshot_selection_state(page)
            same = compare_selection_states(snap_before, snap_after_w)

            # Mutate option packs 20→10 → inconsistent.
            page.evaluate(
                """() => {
                    const el = document.getElementById('spec_option');
                    if (el) el.textContent = '1层200抽*10包【超大尺寸】，1个';
                }"""
            )
            snap_packs = snapshot_selection_state(page)
            diff_packs = compare_selection_states(snap_before, snap_packs)

            # Restore then mutate qty 1→2.
            page.evaluate(
                """() => {
                    const el = document.getElementById('spec_option');
                    if (el) el.textContent = '1层200抽*20包【超大尺寸】，2个';
                }"""
            )
            snap_qty = snapshot_selection_state(page)
            diff_qty = compare_selection_states(snap_before, snap_qty)

            # Discover must use the same locator (not label-only).
            discovery = discover_regions(page)
            disc_spec = (discovery or {}).get("selected_spec") or {}

            checks = {
                "locate_before_ok": located_before.get("status") == "LOCATED",
                "snap_before_extracted": snap_before.get("core_status") == "EXTRACTED",
                "core_has_option": "20包" in str(snap_before.get("core_selected_spec") or ""),
                "core_has_qty": str(snap_before.get("purchase_qty")) == "1",
                "core_keeps_size": "超大尺寸"
                in str(snap_before.get("selected_option_text") or ""),
                "not_label_only": str(snap_before.get("raw_selected_blob") or "")
                != "已选",
                "warranty_still_consistent": same.get("consistent") is True,
                "packs_change_inconsistent": diff_packs.get("consistent") is False,
                "qty_change_inconsistent": diff_qty.get("consistent") is False,
                "locate_after_warranty_ok": located_after_w.get("status") == "LOCATED",
                # discover_regions must call the same locator on current DOM (qty=2).
                "discover_shared_fresh": disc_spec.get("status") == "LOCATED"
                and (
                    "2个" in str(disc_spec.get("sample_text") or "")
                    or "2个" in str(disc_spec.get("core_blob") or "")
                ),
            }

            # Label-only page → UNKNOWN, never invent option.
            page.goto(
                SPEC_LABEL_ONLY_FIXTURE_PATH.as_uri(),
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(50)
            loc_only = locate_selected_spec_region(page)
            snap_only = snapshot_selection_state(page)
            checks["label_only_not_located_core"] = loc_only.get("status") != "LOCATED"
            checks["label_only_unknown"] = snap_only.get("core_status") == "UNKNOWN"
            checks["label_only_consistent_null"] = (
                compare_selection_states(snap_only, snap_only).get("consistent") is None
            )

            report["checks"] = checks
            report["snap_before_core"] = snap_before.get("core_selected_spec")
            report["locate_before"] = {
                "status": located_before.get("status"),
                "sample_text": located_before.get("sample_text"),
                "core_blob": located_before.get("core_blob"),
            }
            report["ok"] = all(checks.values())
            if not report["ok"]:
                report["error"] = "selected-spec locator fixture checks failed"
        finally:
            browser.close()
    return report


def run_discovery_fixture_check() -> dict[str, Any]:
    """
    Local HTML: nested B913A title dedupe + shop must not hit 服务承诺.
    Not a live JD acceptance.
    """
    report: dict[str, Any] = {
        "ok": False,
        "fixture": str(DISCOVERY_FIXTURE_PATH),
    }
    if not DISCOVERY_FIXTURE_PATH.is_file():
        report["error"] = f"fixture missing: {DISCOVERY_FIXTURE_PATH}"
        return report

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 720})
            page = context.new_page()
            page.goto(DISCOVERY_FIXTURE_PATH.as_uri(), wait_until="domcontentloaded")
            page.wait_for_timeout(100)
            discovery = discover_regions(page)
            title = (discovery or {}).get("product_title") or {}
            shop = (discovery or {}).get("shop") or {}
            spec = (discovery or {}).get("selected_spec") or {}
            report["discovery"] = {
                "product_title": title,
                "shop": shop,
                "selected_spec": {
                    "status": spec.get("status"),
                    "sample_text": spec.get("sample_text"),
                },
            }
            spec_sample = str(spec.get("sample_text") or "")
            checks = {
                "title_located": title.get("status") == "LOCATED",
                "title_has_model": "B913A" in str(title.get("sample_text") or ""),
                "title_not_recommend": "推荐" not in str(title.get("sample_text") or ""),
                "shop_not_boilerplate": "服务承诺" not in str(shop.get("sample_text") or ""),
                "shop_is_name_or_missing": (
                    shop.get("status") == "NOT_FOUND"
                    or (
                        shop.get("status") == "LOCATED"
                        and "旗舰店" in str(shop.get("sample_text") or "")
                    )
                ),
                "spec_located": spec.get("status") == "LOCATED",
                "spec_excludes_warranty": "保障服务" not in spec_sample,
                "spec_keeps_size_option": "超大尺寸" in spec_sample,
            }
            # Core snapshot must ignore async 保障服务 / 送至.
            snap = snapshot_selection_state(page)
            checks["core_extracted"] = snap.get("core_status") == "EXTRACTED"
            checks["core_no_ship_address"] = "朝阳" not in str(
                snap.get("core_selected_spec") or ""
            )
            checks["core_no_warranty"] = "保障" not in str(
                snap.get("core_selected_spec") or ""
            )
            report["selection_snapshot"] = {
                "core_status": snap.get("core_status"),
                "core_selected_spec": snap.get("core_selected_spec"),
            }
            report["checks"] = checks
            report["ok"] = all(checks.values())
            if not report["ok"]:
                report["error"] = "discovery fixture checks failed"
        finally:
            browser.close()
    return report


def run_region_scroll_fixture_check() -> dict[str, Any]:
    """
    Local HTML: after scrolling near price, shop capture must independently
    re-position so content is not clipped at y<0. Not live JD.
    """
    report: dict[str, Any] = {
        "ok": False,
        "fixture": str(REGION_SCROLL_FIXTURE_PATH),
    }
    if not REGION_SCROLL_FIXTURE_PATH.is_file():
        report["error"] = f"fixture missing: {REGION_SCROLL_FIXTURE_PATH}"
        return report

    out_dir = CAPTURE_ROOT.parent / "_region_scroll_probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    shop_png = out_dir / "shop.png"
    promo_png = out_dir / "promotions.png"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            context = browser.new_context(viewport={"width": 390, "height": 700})
            page = context.new_page()
            page.goto(
                REGION_SCROLL_FIXTURE_PATH.as_uri(), wait_until="domcontentloaded"
            )
            page.wait_for_timeout(200)
            # Simulate leftover scroll near top content (as after price capture).
            page.evaluate("() => window.scrollTo(0, 850)")
            page.wait_for_timeout(100)
            discovery = discover_regions(page)
            shop_disc = (discovery or {}).get("shop")
            promo_disc = (discovery or {}).get("promotions")
            shop_info = capture_discovered_region(
                page, shop_png, region="shop", discovery=shop_disc
            )
            # Re-scroll elsewhere then capture promo independently.
            page.evaluate("() => window.scrollTo(0, 0)")
            page.wait_for_timeout(100)
            discovery2 = discover_regions(page)
            promo_info = capture_discovered_region(
                page,
                promo_png,
                region="promotions",
                discovery=(discovery2 or {}).get("promotions") or promo_disc,
            )
            shop_box = shop_info.get("content_box_css_viewport") or {}
            promo_box = promo_info.get("content_box_css_viewport") or {}
            checks = {
                "shop_captured": shop_info.get("status") == "CAPTURED",
                "shop_y_non_negative": float(shop_box.get("y") or -999) >= 0,
                "shop_fully_in_view": float(shop_box.get("y") or -999) >= REGION_SAFE_MARGIN_PX * 0.5,
                "promo_captured": promo_info.get("status")
                in {"CAPTURED", "CAPTURED_BUT_OCCLUDED"},
                "promo_y_safe": float(promo_box.get("y") or -999) >= 0,
                "shop_file": shop_png.is_file(),
            }
            report["checks"] = checks
            report["shop"] = {
                "status": shop_info.get("status"),
                "box": shop_box,
                "error": shop_info.get("error"),
                "diagnostics": shop_info.get("diagnostics"),
            }
            report["promotions"] = {
                "status": promo_info.get("status"),
                "box": promo_box,
                "error": promo_info.get("error"),
            }
            report["ok"] = all(checks.values())
            if not report["ok"]:
                report["error"] = "region scroll fixture checks failed"
        finally:
            browser.close()
            for p in (shop_png, promo_png):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                out_dir.rmdir()
            except OSError:
                pass
    return report


def run_price_bottom_bar_fixture_check() -> dict[str, Any]:
    """
    Local page with fixed bottom bar: price starts near bottom (occluded),
    capture_price must center + clear occlusion before screenshot. Not JD.
    """
    report: dict[str, Any] = {
        "ok": False,
        "fixture": str(BOTTOM_BAR_FIXTURE_PATH),
    }
    if not BOTTOM_BAR_FIXTURE_PATH.is_file():
        report["error"] = f"fixture missing: {BOTTOM_BAR_FIXTURE_PATH}"
        return report

    out_png = CAPTURE_ROOT.parent / "_price_bottom_bar_probe.png"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            context = browser.new_context(viewport={"width": 390, "height": 700})
            page = context.new_page()
            page.goto(BOTTOM_BAR_FIXTURE_PATH.as_uri(), wait_until="domcontentloaded")
            page.wait_for_timeout(300)
            pre = remesaure_selector_box(page, MAIN_PRICE_SELECTOR)
            report["pre_capture"] = pre
            # Expect price near bottom before capture (fixture script positions it).
            if pre and float(pre["y"]) < 400:
                report["warning"] = (
                    f"fixture price y={pre['y']} not near bottom; continuing anyway"
                )

            info = capture_price(page, out_png)
            report["capture"] = {
                "status": info.get("status"),
                "save_status": info.get("save_status"),
                "content_quality_status": info.get("content_quality_status"),
                "pixel_size": info.get("pixel_size"),
                "diagnostics": info.get("diagnostics"),
                "error": info.get("error"),
                "content_box": info.get("content_box_css_viewport"),
                "clip_box": info.get("clip_box_css_viewport"),
            }
            box = info.get("content_box_css_viewport") or {}
            vp_h = float((info.get("diagnostics") or {}).get("viewport", {}).get("height") or 700)
            cy = float(box.get("y") or 0) + float(box.get("height") or 0) / 2
            occluded = bool(
                ((info.get("diagnostics") or {}).get("occlusion_final") or {}).get(
                    "occluded"
                )
            )
            size = info.get("pixel_size") or {}
            ok_center = abs(cy - vp_h / 2) <= vp_h * 0.35
            ok_size = (size.get("height") or 0) >= 70 and (size.get("width") or 0) >= 90
            ok_status = info.get("status") == "CAPTURED" and not occluded
            report["checks"] = {
                "centered_enough": ok_center,
                "center_y": cy,
                "viewport_height": vp_h,
                "not_occluded": not occluded,
                "png_generous": ok_size,
                "status_captured": ok_status,
            }
            report["ok"] = bool(ok_center and ok_size and ok_status)
            if not report["ok"]:
                report["error"] = "bottom-bar fixture capture checks failed"
        finally:
            try:
                if out_png.exists():
                    out_png.unlink()
            except OSError:
                pass
            browser.close()
    return report


def run_auto_capture(
    *,
    product_url: str | None = None,
    product_id: str | None = None,
    expected_product: dict[str, Any] | None = None,
    interactive: bool = True,
    profile_dir: Path | None = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """
    Automatic region screenshots for one known product URL.

    interactive=True (CLI default): wait for Enter before closing.
    interactive=False (P4-D): close context immediately after captures.
    Returns exact out_dir / status; never guesses “latest” capture folder.
    """

    def _log(msg: str) -> None:
        if not quiet:
            print(msg)

    started = utc_now()
    target_url = product_url or PRODUCT_DETAIL_URL
    target_id = product_id or EXPECTED_PRODUCT_ID
    expected = dict(expected_product or EXPECTED_PRODUCT)
    expected.setdefault("jd_product_id", target_id)
    profile = (profile_dir or PROFILE_DIR).resolve()

    _log("=== PriceRadar P2-D ===")
    _log(f"解释器: {sys.executable}")
    _log(f"项目根: {PROJECT_ROOT}")
    _log(f"独立 Profile: {profile}")
    _log(f"Playwright: {playwright_version()}")
    _log(f"目标链接: {target_url}")
    _log(
        "本轮: 全自动区域截图（无手动框选；不接 AI；不做搜索）"
        + ("；非交互自动关闭" if not interactive else "")
    )
    _log("")
    _log("=== expected_product（预先登记，非本次识别）===")
    for key, value in expected.items():
        _log(f"  {key}: {value}")
    _log("")
    if interactive:
        _log("请先结束占用同一 Profile 的其他会话。")
        _log("")

    run_id, out_dir = make_run_dir(target_id)
    _log(f"run_id: {run_id}")
    _log(f"输出目录: {out_dir}")

    browser_started = False
    navigation_ok = False
    identity_ok = False
    close_ok = False
    exit_code = 0
    error: str | None = None
    final_url_safe = "UNKNOWN"
    navigation_status = "NOT_STARTED"
    url_consistency = "NOT_CHECKED"
    regions: dict[str, dict[str, Any]] = {}
    discovery_raw: dict[str, Any] | None = None
    url_snapshots: list[dict[str, str]] = []
    selection_state: dict[str, Any] = {
        "before": None,
        "after": None,
        "consistent": None,
        "inconsistency_reasons": [],
    }
    interrupted = False

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
                error = f"浏览器启动失败 {type(exc).__name__}: {exc}"
                _log(f"浏览器启动结果: 失败 {type(exc).__name__}: {exc}")
                raise

            browser_started = True
            _log("浏览器启动结果: 成功")

            try:
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    response = page.goto(
                        target_url,
                        wait_until="domcontentloaded",
                        timeout=NAVIGATION_TIMEOUT_MS,
                    )
                    status_code = response.status if response is not None else None
                    final_url_safe = url_without_query_or_fragment(page.url)
                    navigation_ok = True
                    navigation_status = "NAVIGATION_COMMAND_DONE"
                    _log(
                        f"导航结果: 命令完成 http_status={status_code} "
                        f"final={final_url_safe}"
                    )
                except PlaywrightError as exc:
                    navigation_status = "NAVIGATION_FAILED"
                    error = f"导航失败 {type(exc).__name__}: {exc}"
                    _log(f"导航结果: 失败 {type(exc).__name__}: {exc}")
                    try:
                        final_url_safe = url_without_query_or_fragment(page.url)
                    except PlaywrightError:
                        pass

                if navigation_ok:
                    try:
                        # Accept item.m.jd.com or item.jd.com; id must match candidate.
                        identity_ok = is_target_product_url(
                            page.url, product_id=target_id, host=None
                        )
                        final_url_safe = url_without_query_or_fragment(page.url)
                    except PlaywrightError:
                        identity_ok = False
                    if identity_ok:
                        _log(
                            "商品身份检查: 通过（允许详情主机 + 商品ID一致）"
                        )
                    else:
                        navigation_status = "WRONG_PAGE_OR_CHALLENGE"
                        error = (
                            "商品身份检查未通过（登录/验证/异常/其他商品？）；"
                            "停止正常商品截图。"
                        )
                        _log(error)

                if navigation_ok and identity_ok:
                    url_snapshots.append(
                        {"phase": "before_captures", "url": final_url_safe}
                    )
                    _log("")
                    _log("=== 等待核心已选规格稳定 ===")
                    try:
                        selection_state["before"] = wait_for_core_selection_stable(page)
                    except PlaywrightError as exc:
                        selection_state["before"] = {
                            "error": f"{type(exc).__name__}: {exc}",
                            "core_status": "UNKNOWN",
                        }
                    before_core = selection_state["before"]
                    _log(
                        f"  core_status={before_core.get('core_status')} "
                        f"core={before_core.get('core_selected_spec')!r} "
                        f"stability={(before_core.get('stability') or {}).get('status')}"
                    )

                    _log("")
                    _log("=== 自动截图 price.png ===")
                    regions["price"] = capture_price(page, out_dir / "price.png")
                    _log(
                        f"price: {regions['price']['status']} "
                        f"size={regions['price'].get('pixel_size')}"
                    )
                    if regions["price"].get("path") and (out_dir / "price.png").exists():
                        _log(f"  路径: {regions['price']['path']}")

                    _log("")
                    _log("=== 语义发现标题/规格/优惠/店铺 ===")
                    model_for_clue = str(
                        (expected or {}).get("model") or CLUE_MODEL
                    ).strip() or CLUE_MODEL
                    _log(f"  标题型号线索: {model_for_clue!r}")
                    discovery_raw = discover_regions(
                        page, model_clue=model_for_clue
                    )
                    for key in (
                        "product_title",
                        "selected_spec",
                        "promotions",
                        "shop",
                    ):
                        d = (discovery_raw or {}).get(key) or {}
                        _log(
                            f"  {key}: status={d.get('status')} "
                            f"matches={d.get('match_count')} "
                            f"sample={d.get('sample_text')!r}"
                        )

                    for key, filename in (
                        ("product_title", "product_title.png"),
                        ("selected_spec", "selected_spec.png"),
                        ("promotions", "promotions.png"),
                        ("shop", "shop.png"),
                    ):
                        _log("")
                        _log(f"=== 自动截图 {filename} ===")
                        disc = (discovery_raw or {}).get(key)
                        regions[key] = capture_discovered_region(
                            page,
                            out_dir / filename,
                            region=key,
                            discovery=disc,
                        )
                        if (
                            isinstance(disc, dict)
                            and disc.get("status") == "AMBIGUOUS"
                            and key == "product_title"
                        ):
                            diag = capture_ambiguous_diagnostics(
                                page,
                                out_dir,
                                region=key,
                                discovery=disc,
                            )
                            regions[key]["auto_diagnostics"] = diag
                            if diag and diag.get("status") == "CAPTURED":
                                _log(
                                    f"  歧义诊断已写入 diagnostics/{key}/ "
                                    f"({len(diag.get('candidates') or [])} 候选)"
                                )
                        _log(f"{key}: {regions[key]['status']}")
                        path = out_dir / filename
                        if path.exists():
                            _log(f"  路径: {path.resolve()}")
                        elif regions[key]["status"] in {
                            "NOT_FOUND",
                            "AMBIGUOUS",
                            "UNSAFE_REGION",
                        }:
                            _log(
                                f"  未截图原因: "
                                f"{regions[key].get('error') or regions[key]['status']}"
                            )

                    try:
                        selection_state["after"] = snapshot_selection_state(page)
                    except PlaywrightError as exc:
                        selection_state["after"] = {
                            "error": f"{type(exc).__name__}: {exc}",
                            "core_status": "UNKNOWN",
                        }
                    cmp = compare_selection_states(
                        selection_state.get("before"),
                        selection_state.get("after"),
                    )
                    selection_state["consistency_status"] = cmp.get(
                        "consistency_status"
                    )
                    selection_state["inconsistency_reasons"] = cmp.get(
                        "inconsistency_reasons"
                    ) or []
                    selection_state["compared_fields"] = cmp.get("compared_fields")
                    # Never invent True; UNKNOWN → consistent=None.
                    selection_state["consistent"] = cmp.get("consistent")
                    _log(
                        f"选中状态核验: {selection_state.get('consistency_status')} "
                        f"consistent={selection_state.get('consistent')}"
                    )
                    if selection_state.get("consistency_status") == "INCONSISTENT":
                        _log(
                            "警告: 核心规格不一致 — "
                            + "; ".join(selection_state["inconsistency_reasons"])
                        )
                    elif selection_state.get("consistency_status") == "UNKNOWN":
                        _log(
                            "警告: 核心规格状态未知 — "
                            + "; ".join(selection_state["inconsistency_reasons"])
                        )

                    try:
                        url_after = url_without_query_or_fragment(page.url)
                    except PlaywrightError:
                        url_after = "UNKNOWN"
                    url_snapshots.append({"phase": "after_captures", "url": url_after})
                    if url_after == final_url_safe:
                        url_consistency = "SAME_URL"
                    else:
                        url_consistency = "INCONSISTENT"
                        _log("警告: 截图期间 URL 变化，标记 INCONSISTENT。")

                _log("")
                _log("自动截图流程已结束（不依赖人工框选）。")
                if interactive:
                    _log("开发验收: 可检查输出目录中的图片；按回车关闭浏览器。")
                    read_line("按回车关闭本次自动化浏览器会话… ")
                else:
                    _log("非交互模式: 即将自动关闭浏览器上下文。")
            except KeyboardInterrupt:
                interrupted = True
                exit_code = 130
                error = "KeyboardInterrupt"
                _log("")
                _log("收到 Ctrl+C，正在关闭…")
            finally:
                try:
                    context.close()
                    close_ok = True
                    _log("程序关闭结果: 上下文关闭完成")
                except PlaywrightError as exc:
                    _log(f"程序关闭结果: 异常 {type(exc).__name__}: {exc}")
                    if error is None:
                        error = f"context.close 失败 {type(exc).__name__}: {exc}"
                    raise
    except RuntimeError as exc:
        error = f"{type(exc).__name__}: {exc}"
        _log(f"运行中止: {error}")
        exit_code = 2
    except PlaywrightError as exc:
        if error is None:
            error = f"Playwright 错误 {type(exc).__name__}: {exc}"
        _log(f"Playwright 错误: {type(exc).__name__}: {exc}")
        exit_code = 1

    ended = utc_now()
    status = overall_status(regions)
    required_ready = required_evidence_ready(regions)
    selection_ok = selection_state.get("consistent") is True
    # Auto compare needs required captures + core selection consistency — not shop.
    auto_compare_ready = required_ready and selection_ok
    gaps = []
    for key in ("promotions", "shop"):
        st = str(regions.get(key, {}).get("status", "NOT_RUN"))
        if st != "CAPTURED":
            gaps.append(f"{key}={st}（证据缺口，不推断无优惠/店铺身份）")
    if not str(regions.get("product_title", {}).get("status", "")).startswith(
        "CAPTURED"
    ):
        gaps.append("product_title 缺失 → 暂不具备自动规格比价条件")
    if not str(regions.get("selected_spec", {}).get("status", "")).startswith(
        "CAPTURED"
    ):
        gaps.append("selected_spec 缺失 → 暂不具备自动规格比价条件")
    if selection_state.get("consistent") is False:
        gaps.append(
            "核心已选规格/商品状态不一致 → 不得跨状态拼接: "
            + "; ".join(selection_state.get("inconsistency_reasons") or [])
        )
    elif selection_state.get("consistent") is None and regions:
        gaps.append(
            "核心已选规格状态未知 → 不默认一致: "
            + "; ".join(selection_state.get("inconsistency_reasons") or [])
        )
    if status == "REQUIRED_EVIDENCE_READY":
        gaps.append(
            "必需证据（价格/标题/已选规格）已就绪；店铺或优惠等非必需区域未全部截取成功"
        )

    has_any_image = any(
        (out_dir / name).is_file()
        for name in (
            "price.png",
            "product_title.png",
            "selected_spec.png",
            "promotions.png",
            "shop.png",
            "product_context.png",
        )
    )

    metadata = {
        "run_id": run_id,
        "phase": "P2-D",
        "started_at_utc": utc_iso(started),
        "ended_at_utc": utc_iso(ended),
        "navigation_status": navigation_status,
        "browser_started": browser_started,
        "product_identity_ok": identity_ok,
        "actual_product_url": final_url_safe,
        "target_product_id": target_id,
        "expected_product": expected,
        "discovery": discovery_raw,
        "regions": regions,
        "url_snapshots": url_snapshots,
        "url_consistency": url_consistency,
        "selection_state": selection_state,
        "overall_capture_status": status,
        "required_evidence_ready": required_ready,
        "all_regions_captured": all_regions_captured(regions),
        "auto_spec_compare_ready": auto_compare_ready,
        "evidence_gaps": gaps,
        "manual_image_qa_status": "PENDING",
        "ai_status": "NOT_STARTED",
        "recognized_price": None,
        "break_price_verdict": "UNKNOWN",
        "close_ok": close_ok,
        "interactive": interactive,
        "notes": [
            "全自动截图；无手动框选",
            "不代表自动搜索/AI识价/批量监控已完成",
            "expected_product 非实时采集结果",
        ],
    }
    meta_path = out_dir / "metadata.json"
    try:
        write_metadata(meta_path, metadata)
        _log(f"metadata.json: {meta_path.resolve()}")
    except OSError as exc:
        _log(f"metadata 写入失败: {type(exc).__name__}: {exc}")
        if error is None:
            error = f"metadata 写入失败: {exc}"

    _log("")
    _log("=== P2-D 分项结果 ===")
    _log(f"整体截图状态: {status}")
    for name, info in regions.items():
        _log(f"  {name}: {info.get('status')}")
        if info.get("path") and Path(str(info.get("path"))).exists():
            _log(f"    file={info['path']}")
    _log(f"证据缺口: {gaps if gaps else '无（仍待人工图片核验）'}")
    _log("ai_status=NOT_STARTED recognized_price=null 破价=UNKNOWN")
    _log(f"输出目录: {out_dir}")

    if exit_code == 0 and status == "FAILED":
        exit_code = 1

    return {
        "ok": exit_code == 0 and status != "FAILED",
        "exit_code": exit_code,
        "run_id": run_id,
        "out_dir": out_dir,
        "capture_status": status,
        "has_any_image": has_any_image,
        "browser_started": browser_started,
        "close_ok": close_ok,
        "navigation_status": navigation_status,
        "product_identity_ok": identity_ok,
        "actual_product_url": final_url_safe,
        "regions": regions,
        "evidence_gaps": gaps,
        "metadata_path": meta_path,
        "error": error,
        "interrupted": interrupted,
        "started_at_utc": utc_iso(started),
        "ended_at_utc": utc_iso(ended),
        "interactive": interactive,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="PriceRadar P2-D auto capture")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Close browser automatically (no Enter). Default waits for Enter.",
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="Run local unit/fixture checks only (no JD navigation).",
    )
    args = parser.parse_args(argv)

    if args.self_check:
        logic_fails = run_region_logic_unit_checks()
        print(f"region_logic_failures={logic_fails}")
        sel_fails = run_selection_core_unit_checks()
        print(f"selection_core_failures={sel_fails}")
        spec_loc = run_selected_spec_locator_fixture_check()
        print(f"selected_spec_locator_ok={spec_loc.get('ok')}")
        print(f"selected_spec_locator_checks={spec_loc.get('checks')}")
        if spec_loc.get("error"):
            print(f"selected_spec_locator_error={spec_loc.get('error')}")
        discovery = run_discovery_fixture_check()
        print(f"discovery_fixture_ok={discovery.get('ok')}")
        print(f"discovery_fixture_checks={discovery.get('checks')}")
        if discovery.get("error"):
            print(f"discovery_fixture_error={discovery.get('error')}")
        region_scroll = run_region_scroll_fixture_check()
        print(f"region_scroll_fixture_ok={region_scroll.get('ok')}")
        print(f"region_scroll_fixture_checks={region_scroll.get('checks')}")
        if region_scroll.get("error"):
            print(f"region_scroll_fixture_error={region_scroll.get('error')}")
        bottom = run_price_bottom_bar_fixture_check()
        print(f"bottom_bar_fixture_ok={bottom.get('ok')}")
        print(f"bottom_bar_fixture_checks={bottom.get('checks')}")
        if bottom.get("error"):
            print(f"bottom_bar_fixture_error={bottom.get('error')}")
        if (
            logic_fails
            or sel_fails
            or not spec_loc.get("ok")
            or not discovery.get("ok")
            or not region_scroll.get("ok")
            or not bottom.get("ok")
        ):
            return 1
        return 0

    result = run_auto_capture(interactive=not args.non_interactive)
    return int(result.get("exit_code") or 0)


if __name__ == "__main__":
    raise SystemExit(main())
