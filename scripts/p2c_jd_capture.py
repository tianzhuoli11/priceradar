"""P2-C: local JD product screenshots for later AI image price recognition."""

from __future__ import annotations

import json
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
    "note": "预先登记的业务信息，不是本次识别结果；历史观察价不得写入成功判据",
}

NAVIGATION_TIMEOUT_MS = 60_000
MAIN_PRICE_SELECTOR = "#main_price"
MAIN_PRICE_WAIT_MS = 15_000
FONT_WAIT_MS = 8_000
LAYOUT_STABLE_MS = 500
# Padding around the measured *content* union (CSS px, viewport space).
# Prior run used parent box + uniform 6px pad and pulled adjacent red UI into the crop.
PRICE_PAD_X_PX = 8
PRICE_PAD_Y_PX = 3
MIN_PRICE_CONTENT_WIDTH = 24
MIN_PRICE_CONTENT_HEIGHT = 12
MANUAL_SELECT_TIMEOUT_MS = 300_000


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
    value = dt or utc_now()
    return value.isoformat(timespec="seconds")


def utc_run_stamp(dt: datetime | None = None) -> str:
    """Filesystem-safe UTC stamp without colons: 20261018T090000Z."""
    value = dt or utc_now()
    return value.strftime("%Y%m%dT%H%M%SZ")


def url_without_query_or_fragment(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path if parts.path else "/"
    return f"{parts.scheme}://{parts.netloc}{path}"


def is_target_product_url(url: str) -> bool:
    """Strict host + product path check (not a fuzzy product-id substring test)."""
    parts = urlsplit(url)
    if parts.netloc.lower() != EXPECTED_HOST:
        return False
    path = parts.path
    allowed = set(EXPECTED_PATHS)
    allowed.update(p.rstrip("/") for p in EXPECTED_PATHS)
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


def make_run_dir() -> tuple[str, Path]:
    stamp = utc_run_stamp()
    suffix = secrets.token_hex(3)
    run_id = f"{stamp}_{EXPECTED_PRODUCT_ID}_{suffix}"
    out_dir = CAPTURE_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=False)
    return run_id, out_dir


def wait_for_close() -> None:
    print()
    print("请打开本地截图文件，人工检查数字完整性、规格与是否含敏感信息。")
    print("本轮不上传、不调用 AI。浏览器保持打开。")
    read_line("按回车结束本次自动化浏览器会话… ")


def install_manual_rect_overlay(page: Page, overlay_hint: str) -> None:
    page.evaluate(
        """(hintText) => {
            const old = document.getElementById('priceradar-capture-overlay');
            if (old) old.remove();
            window.__priceradar_sel = { status: 'pending', rect: null };

            const overlay = document.createElement('div');
            overlay.id = 'priceradar-capture-overlay';
            Object.assign(overlay.style, {
                position: 'fixed',
                left: '0',
                top: '0',
                right: '0',
                bottom: '0',
                zIndex: '2147483647',
                cursor: 'crosshair',
                background: 'rgba(0, 0, 0, 0.18)',
                userSelect: 'none',
            });

            const box = document.createElement('div');
            Object.assign(box.style, {
                position: 'fixed',
                border: '2px solid #1e90ff',
                background: 'rgba(30, 144, 255, 0.18)',
                display: 'none',
                pointerEvents: 'none',
            });
            overlay.appendChild(box);

            const hint = document.createElement('div');
            hint.textContent = hintText + ' 松开完成；Esc 取消重选。';
            Object.assign(hint.style, {
                position: 'fixed',
                top: '10px',
                left: '50%',
                transform: 'translateX(-50%)',
                background: '#111',
                color: '#fff',
                padding: '8px 12px',
                fontSize: '13px',
                borderRadius: '4px',
                zIndex: '2147483647',
                pointerEvents: 'none',
                maxWidth: '92vw',
                lineHeight: '1.4',
            });
            overlay.appendChild(hint);

            let start = null;
            const finish = (status, rect) => {
                window.__priceradar_sel = { status, rect };
                window.removeEventListener('keydown', onKey, true);
                overlay.remove();
            };
            const onDown = (e) => {
                e.preventDefault();
                e.stopPropagation();
                start = { x: e.clientX, y: e.clientY };
                box.style.display = 'block';
                box.style.left = start.x + 'px';
                box.style.top = start.y + 'px';
                box.style.width = '0px';
                box.style.height = '0px';
            };
            const onMove = (e) => {
                if (!start) return;
                e.preventDefault();
                e.stopPropagation();
                const x1 = Math.min(start.x, e.clientX);
                const y1 = Math.min(start.y, e.clientY);
                const x2 = Math.max(start.x, e.clientX);
                const y2 = Math.max(start.y, e.clientY);
                box.style.left = x1 + 'px';
                box.style.top = y1 + 'px';
                box.style.width = (x2 - x1) + 'px';
                box.style.height = (y2 - y1) + 'px';
            };
            const onUp = (e) => {
                if (!start) return;
                e.preventDefault();
                e.stopPropagation();
                const x1 = Math.min(start.x, e.clientX);
                const y1 = Math.min(start.y, e.clientY);
                const x2 = Math.max(start.x, e.clientX);
                const y2 = Math.max(start.y, e.clientY);
                const width = x2 - x1;
                const height = y2 - y1;
                start = null;
                if (width < 8 || height < 8) {
                    finish('too_small', null);
                    return;
                }
                finish('done', { x: x1, y: y1, width, height });
            };
            const onKey = (e) => {
                if (e.key === 'Escape') {
                    e.preventDefault();
                    e.stopPropagation();
                    finish('cancelled', null);
                }
            };

            // Capture-phase handlers keep underlying buy/cart clicks from firing.
            overlay.addEventListener('mousedown', onDown, true);
            overlay.addEventListener('mousemove', onMove, true);
            overlay.addEventListener('mouseup', onUp, true);
            overlay.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopPropagation();
            }, true);
            window.addEventListener('keydown', onKey, true);
            document.documentElement.appendChild(overlay);
        }""",
        overlay_hint,
    )


def wait_manual_rect(page: Page, *, overlay_hint: str) -> dict[str, Any]:
    install_manual_rect_overlay(page, overlay_hint)
    print("覆盖层已启用：请拖拽框选。完成后覆盖层会移除再截图。")
    try:
        page.wait_for_function(
            "() => window.__priceradar_sel && window.__priceradar_sel.status !== 'pending'",
            timeout=MANUAL_SELECT_TIMEOUT_MS,
        )
    except PlaywrightError as exc:
        page.evaluate(
            """() => {
                const old = document.getElementById('priceradar-capture-overlay');
                if (old) old.remove();
                if (!window.__priceradar_sel || window.__priceradar_sel.status === 'pending') {
                    window.__priceradar_sel = { status: 'timeout', rect: null };
                }
            }"""
        )
        print(f"人工框选等待失败: {type(exc).__name__}: {exc}")
        return {"status": "timeout", "rect": None}

    return page.evaluate("() => window.__priceradar_sel")


def viewport_css_size(page: Page) -> dict[str, float]:
    """CSS-pixel viewport size (same space as Playwright clip / getBoundingClientRect)."""
    metrics = page.evaluate(
        "() => ({ width: window.innerWidth, height: window.innerHeight })"
    )
    return {
        "width": float(metrics["width"]),
        "height": float(metrics["height"]),
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
    """Pad a content box in CSS viewport space, then clamp to the visible viewport."""
    clip = {
        "x": float(content["x"]) - pad_x,
        "y": float(content["y"]) - pad_y,
        "width": float(content["width"]) + (pad_x * 2),
        "height": float(content["height"]) + (pad_y * 2),
    }
    return clamp_clip_to_viewport(clip, viewport)


def measure_main_price_content_box(page: Page) -> dict[str, Any] | None:
    """
    Measure #main_price content in CSS viewport coordinates.

    Uses the union of #main_price and its visible price descendants so custom-font
    glyphs that overflow the parent layout box are included, without expanding to
    sibling nodes outside #main_price (which previously contributed adjacent red UI).
    """
    return page.evaluate(
        """() => {
            const el = document.querySelector('#main_price');
            if (!el) return null;

            const rects = [];
            const pushVisible = (node) => {
                if (!node || node.nodeType !== 1) return;
                const r = node.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) {
                    rects.push({
                        left: r.left,
                        top: r.top,
                        right: r.right,
                        bottom: r.bottom,
                    });
                }
            };

            pushVisible(el);
            // Prefer known price content nodes; also include other element children.
            el.querySelectorAll('em, .price_decimals, span').forEach(pushVisible);

            if (!rects.length) return null;

            const left = Math.min(...rects.map((r) => r.left));
            const top = Math.min(...rects.map((r) => r.top));
            const right = Math.max(...rects.map((r) => r.right));
            const bottom = Math.max(...rects.map((r) => r.bottom));
            const parent = el.getBoundingClientRect();

            return {
                x: left,
                y: top,
                width: right - left,
                height: bottom - top,
                parent_box: {
                    x: parent.left,
                    y: parent.top,
                    width: parent.width,
                    height: parent.height,
                },
                descendant_box_count: rects.length,
                device_pixel_ratio: window.devicePixelRatio || 1,
                viewport: {
                    width: window.innerWidth,
                    height: window.innerHeight,
                },
                coordinate_space: 'css_viewport_px',
            };
        }"""
    )


def wait_fonts_ready(page: Page) -> bool:
    return bool(
        page.evaluate(
            """async (timeoutMs) => {
                if (!(document.fonts && document.fonts.ready)) {
                    return false;
                }
                const ready = document.fonts.ready.then(() => true);
                const timeout = new Promise((resolve) =>
                    setTimeout(() => resolve(false), timeoutMs)
                );
                return await Promise.race([ready, timeout]);
            }""",
            FONT_WAIT_MS,
        )
    )


def capture_price_png(page: Page, out_path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {
        "filename": out_path.name,
        "path": str(out_path.resolve()),
        "capture_method": "automatic",
        "selector": MAIN_PRICE_SELECTOR,
        "match_count": 0,
        "visible": False,
        "status": "FAILED",
        "captured_at_utc": None,
        "pixel_size": None,
        "price_label_coverage": "PENDING_USER_VERIFY",
        "integrity": "PENDING_USER_VERIFY",
        "content_box_css_viewport": None,
        "clip_box_css_viewport": None,
        "coordinate_space": "css_viewport_px",
        "error": None,
    }

    try:
        page.wait_for_selector(
            MAIN_PRICE_SELECTOR,
            timeout=MAIN_PRICE_WAIT_MS,
            state="attached",
        )
    except PlaywrightError as exc:
        info["match_count"] = page.locator(MAIN_PRICE_SELECTOR).count()
        info["error"] = f"{type(exc).__name__}: {exc}"
        info["status"] = "SELECTOR_WAIT_FAILED"
        return info

    locator = page.locator(MAIN_PRICE_SELECTOR)
    match_count = locator.count()
    info["match_count"] = match_count
    if match_count != 1:
        info["status"] = "MATCH_COUNT_NOT_ONE"
        info["error"] = f"expected 1 match, got {match_count}"
        return info

    target = locator.first
    try:
        target.scroll_into_view_if_needed(timeout=MAIN_PRICE_WAIT_MS)
        visible = target.is_visible()
    except PlaywrightError as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        info["status"] = "VISIBILITY_CHECK_FAILED"
        return info

    info["visible"] = bool(visible)
    if not visible:
        info["status"] = "NOT_VISIBLE"
        info["error"] = "main_price not visible"
        return info

    try:
        font_ok = wait_fonts_ready(page)
    except PlaywrightError as exc:
        info["error"] = f"font_wait {type(exc).__name__}: {exc}"
        info["status"] = "FONT_WAIT_FAILED"
        info["font_ready"] = False
        return info

    info["font_ready"] = font_ok
    if not font_ok:
        print("字体加载有界等待未确认 ready；若仍截图将标记待核验。")

    # Stabilize after scroll/font, then re-measure (do not reuse pre-scroll boxes).
    page.wait_for_timeout(LAYOUT_STABLE_MS)

    try:
        measured = measure_main_price_content_box(page)
    except PlaywrightError as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        info["status"] = "MEASURE_FAILED"
        return info

    if not measured:
        info["status"] = "CONTENT_BOX_EMPTY"
        info["error"] = "unable to measure visible #main_price content box"
        return info

    content = {
        "x": float(measured["x"]),
        "y": float(measured["y"]),
        "width": float(measured["width"]),
        "height": float(measured["height"]),
    }
    info["content_box_css_viewport"] = {
        **content,
        "parent_box": measured.get("parent_box"),
        "descendant_box_count": measured.get("descendant_box_count"),
        "device_pixel_ratio": measured.get("device_pixel_ratio"),
    }

    if (
        content["width"] < MIN_PRICE_CONTENT_WIDTH
        or content["height"] < MIN_PRICE_CONTENT_HEIGHT
    ):
        info["status"] = "CONTENT_BOX_TOO_SMALL"
        info["error"] = (
            f"content box too small for reliable crop: "
            f"{content['width']:.1f}x{content['height']:.1f}"
        )
        info["integrity"] = "NOT_ACCEPTED"
        return info

    viewport = {
        "width": float(measured["viewport"]["width"]),
        "height": float(measured["viewport"]["height"]),
    }
    # If content sits on a viewport edge after scroll, try one more scroll + remeasure.
    if content["y"] < 1 or content["y"] + content["height"] > viewport["height"] - 1:
        try:
            target.scroll_into_view_if_needed(timeout=MAIN_PRICE_WAIT_MS)
            page.wait_for_timeout(LAYOUT_STABLE_MS)
            measured = measure_main_price_content_box(page)
            if measured:
                content = {
                    "x": float(measured["x"]),
                    "y": float(measured["y"]),
                    "width": float(measured["width"]),
                    "height": float(measured["height"]),
                }
                info["content_box_css_viewport"] = {
                    **content,
                    "parent_box": measured.get("parent_box"),
                    "descendant_box_count": measured.get("descendant_box_count"),
                    "device_pixel_ratio": measured.get("device_pixel_ratio"),
                }
                viewport = {
                    "width": float(measured["viewport"]["width"]),
                    "height": float(measured["viewport"]["height"]),
                }
        except PlaywrightError:
            pass

    clipped = build_padded_clip(
        content,
        pad_x=PRICE_PAD_X_PX,
        pad_y=PRICE_PAD_Y_PX,
        viewport=viewport,
    )
    if clipped is None:
        info["status"] = "CLIP_OUT_OF_VIEWPORT"
        info["error"] = "padded content clip invalid after viewport clamp"
        info["integrity"] = "NOT_ACCEPTED"
        return info

    # Ensure clip still covers the full content box after clamp.
    content_right = content["x"] + content["width"]
    content_bottom = content["y"] + content["height"]
    clip_right = clipped["x"] + clipped["width"]
    clip_bottom = clipped["y"] + clipped["height"]
    if (
        clipped["x"] > content["x"] + 0.5
        or clipped["y"] > content["y"] + 0.5
        or clip_right < content_right - 0.5
        or clip_bottom < content_bottom - 0.5
    ):
        info["status"] = "CONTENT_CLIPPED_BY_VIEWPORT"
        info["error"] = "viewport clamp would crop price content; refusing incomplete shot"
        info["clip_box_css_viewport"] = clipped
        info["integrity"] = "NOT_ACCEPTED"
        return info

    info["clip_box_css_viewport"] = clipped
    info["price_label_coverage"] = (
        "价格标签未覆盖，价格口径待核验"
        "（仅截取 #main_price 内容并集及少量留白，不含相邻控件）"
    )

    captured_at = utc_now()
    try:
        # clip uses the same CSS viewport px space as getBoundingClientRect / Playwright.
        page.screenshot(path=str(out_path), clip=clipped, type="png")
    except PlaywrightError as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        info["status"] = "SCREENSHOT_FAILED"
        info["integrity"] = "NOT_ACCEPTED"
        return info

    info["captured_at_utc"] = utc_iso(captured_at)
    size = png_pixel_size(out_path)
    info["pixel_size"] = (
        {"width": size[0], "height": size[1]} if size else None
    )
    if size and (
        size[0] < MIN_PRICE_CONTENT_WIDTH or size[1] < MIN_PRICE_CONTENT_HEIGHT
    ):
        info["status"] = "CAPTURED_TOO_SMALL"
        info["integrity"] = "NOT_ACCEPTED"
        info["error"] = (
            f"output pixels too small: {size[0]}x{size[1]}; "
            "not marking as accepted capture"
        )
        print(info["error"])
        return info

    info["status"] = "CAPTURED_PENDING_USER_VERIFY"
    if not font_ok:
        info["status"] = "CAPTURED_FONT_UNCONFIRMED"
    info["integrity"] = "PENDING_USER_VERIFY"
    return info


def remove_capture_overlay(page: Page) -> None:
    page.evaluate(
        """() => {
            const old = document.getElementById('priceradar-capture-overlay');
            if (old) old.remove();
        }"""
    )


def capture_manual_clip(
    page: Page,
    out_path: Path,
    *,
    role: str,
    hint: str,
) -> dict[str, Any]:
    info: dict[str, Any] = {
        "filename": out_path.name,
        "path": str(out_path.resolve()),
        "capture_method": "manual_assisted",
        "manual_assisted": True,
        "role": role,
        "status": "FAILED",
        "captured_at_utc": None,
        "pixel_size": None,
        "clip_box_css_viewport": None,
        "integrity": "PENDING_USER_VERIFY",
        "url_at_capture": None,
        "error": None,
    }

    while True:
        print()
        print(f"--- 人工框选：{role} ---")
        print(hint)
        print("可先在页面中正常滚动，使目标区域进入视口（此时尚无覆盖层）。")
        ready = read_line(
            "准备好后输入 y 开始框选，或 s 跳过该项，然后回车: "
        ).strip().lower()
        if ready in {"s", "skip"}:
            info["status"] = "SKIPPED_BY_USER"
            info["error"] = "user skipped this capture"
            return info
        if ready not in {"y", "yes"}:
            print("请输入 y（开始）或 s（跳过）。")
            continue

        selection = wait_manual_rect(page, overlay_hint=hint)
        remove_capture_overlay(page)
        status = selection.get("status")
        rect = selection.get("rect")

        if status in {"cancelled", "too_small", "timeout"}:
            print(f"框选未完成（{status}）。可重试，不会退出整个流程。")
            again = read_line("输入 r 重试，或 s 跳过该项: ").strip().lower()
            if again in {"s", "skip"}:
                info["status"] = f"MANUAL_SELECT_{str(status).upper()}"
                info["error"] = f"manual selection not completed: {status}"
                return info
            continue

        if status != "done" or not isinstance(rect, dict):
            info["status"] = f"MANUAL_SELECT_{str(status).upper()}"
            info["error"] = f"manual selection not completed: {status}"
            return info

        viewport = viewport_css_size(page)
        clipped = clamp_clip_to_viewport(
            {
                "x": float(rect["x"]),
                "y": float(rect["y"]),
                "width": float(rect["width"]),
                "height": float(rect["height"]),
            },
            viewport,
        )
        if clipped is None:
            print("选区无效（视口钳制后为空）。请重试。")
            continue

        try:
            info["url_at_capture"] = url_without_query_or_fragment(page.url)
        except PlaywrightError:
            info["url_at_capture"] = "UNKNOWN"

        info["clip_box_css_viewport"] = clipped
        captured_at = utc_now()
        try:
            page.screenshot(path=str(out_path), clip=clipped, type="png")
        except PlaywrightError as exc:
            info["error"] = f"{type(exc).__name__}: {exc}"
            info["status"] = "SCREENSHOT_FAILED"
            retry = read_line("截图失败。输入 r 重试，或 s 跳过: ").strip().lower()
            if retry in {"s", "skip"}:
                return info
            continue

        info["captured_at_utc"] = utc_iso(captured_at)
        size = png_pixel_size(out_path)
        info["pixel_size"] = (
            {"width": size[0], "height": size[1]} if size else None
        )
        info["status"] = "CAPTURED_MANUAL_ASSISTED"
        print(f"已保存: {out_path.resolve()}")
        accept = read_line(
            "若选区不对可输入 r 重新框选；输入 Enter 接受当前图: "
        ).strip().lower()
        if accept in {"r", "retry"}:
            try:
                out_path.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        return info


def capture_product_context_set(page: Page, out_dir: Path) -> list[dict[str, Any]]:
    """
    Manual-assisted product context capture. Never claims automatic title/spec detection.
    """
    print()
    print("=== product_context 人工辅助框选 ===")
    print(
        "价格已自动截图。接下来请框选商品标题（包含B913A）到“已选”规格，"
        "选区下边界停在“送至”之前。不要再次只框价格。"
    )
    print("标记: manual_assisted（不宣称已实现自动上下文截图；内容由你核验）。")
    print("请勿框入账号、地址、手机号或“送至”收货信息。")
    print()
    print("请选择框选方式：")
    print("  1) 一张 product_context.png（标题到已选规格）")
    print("  2) 拆成 product_title.png + selected_spec.png")
    print("  3) 跳过商品信息截图")

    while True:
        choice = read_line("输入 1 / 2 / 3 后回车: ").strip()
        if choice in {"1", "2", "3"}:
            break
        print("无效输入，请只输入 1、2 或 3。")

    if choice == "3":
        return [
            {
                "filename": None,
                "capture_method": "manual_assisted",
                "manual_assisted": True,
                "status": "SKIPPED_BY_USER",
                "integrity": "NOT_CAPTURED",
                "error": "user skipped product context capture",
            }
        ]

    if choice == "1":
        path = out_dir / "product_context.png"
        return [
            capture_manual_clip(
                page,
                path,
                role="product_context",
                hint=(
                    "框选：商品标题（含 B913A）→「已选」规格；"
                    "下边界停在「送至」之前；不要只框价格。"
                ),
            )
        ]

    title_info = capture_manual_clip(
        page,
        out_dir / "product_title.png",
        role="product_title",
        hint="框选商品标题区域（应能看到 B913A）；不要包含「送至」或价格。",
    )
    spec_info = capture_manual_clip(
        page,
        out_dir / "selected_spec.png",
        role="selected_spec",
        hint="框选「已选」规格区域；下边界停在「送至」之前；不要只框价格。",
    )
    return [title_info, spec_info]


def write_metadata(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    print("=== PriceRadar P2-C ===")
    print(f"解释器: {sys.executable}")
    print(f"项目根: {PROJECT_ROOT}")
    print(f"独立 Profile（绝对路径）: {PROFILE_DIR}")
    print(f"Playwright: {playwright_version()}")
    print(f"目标商品链接: {PRODUCT_DETAIL_URL}")
    print("本轮目标: 局部截图保存（不接 AI、不做 OCR、不输出自动采价成功）")
    print()
    print("=== expected_product（预先登记，非本次识别）===")
    for key, value in EXPECTED_PRODUCT.items():
        print(f"  {key}: {value}")
    print()
    print("重要提醒: 请先结束占用同一 Profile 的其他脚本会话。")
    print()

    run_id, out_dir = make_run_dir()
    print(f"运行 ID: {run_id}")
    print(f"本轮输出目录: {out_dir}")
    print()

    browser_started = False
    navigation_ok = False
    identity_ok = False
    close_ok = False
    exit_code = 0
    final_url_safe = "UNKNOWN"
    navigation_status = "NOT_STARTED"
    price_info: dict[str, Any] | None = None
    context_infos: list[dict[str, Any]] = []
    url_before_price: str | None = None
    url_consistency = "NOT_CHECKED"

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
                print("浏览器启动结果: 失败")
                print(f"异常类型: {type(exc).__name__}")
                print(f"错误信息: {exc}")
                raise

            browser_started = True
            print(
                "浏览器启动结果: 成功"
                "（channel=chrome, headless=False, chromium_sandbox=True）"
            )

            try:
                page = context.pages[0] if context.pages else context.new_page()

                try:
                    response = page.goto(
                        PRODUCT_DETAIL_URL,
                        wait_until="domcontentloaded",
                        timeout=NAVIGATION_TIMEOUT_MS,
                    )
                    status = response.status if response is not None else None
                    final_url_safe = url_without_query_or_fragment(page.url)
                    navigation_ok = True
                    navigation_status = "NAVIGATION_COMMAND_DONE"
                    print(
                        f"导航结果: 命令完成 http_status={status} "
                        f"final={final_url_safe}"
                    )
                except PlaywrightError as exc:
                    navigation_status = "NAVIGATION_FAILED"
                    print("导航结果: 失败（浏览器已启动，请保留窗口观察）")
                    print(f"异常类型: {type(exc).__name__}")
                    print(f"错误信息: {exc}")
                    try:
                        final_url_safe = url_without_query_or_fragment(page.url)
                        print(f"当前页面（无查询参数）: {final_url_safe}")
                    except PlaywrightError:
                        pass

                if navigation_ok:
                    try:
                        identity_ok = is_target_product_url(page.url)
                        final_url_safe = url_without_query_or_fragment(page.url)
                    except PlaywrightError:
                        identity_ok = False

                    if identity_ok:
                        print(
                            "商品身份检查: 通过"
                            f"（host={EXPECTED_HOST}, path 对应 "
                            f"/product/{EXPECTED_PRODUCT_ID}.html）"
                        )
                    else:
                        navigation_status = "WRONG_PAGE_OR_CHALLENGE"
                        print(
                            "商品身份检查: 未通过"
                            "（最终主机/路径不是目标商品详情页；"
                            "可能是登录、验证、异常页或其他商品）"
                        )
                        print("已停止正常商品截图流程（不自动登录、不刷新重试）。")

                if navigation_ok and identity_ok:
                    url_before_price = final_url_safe
                    price_path = out_dir / "price.png"
                    print()
                    print("=== 自动截图 price.png（#main_price）===")
                    price_info = capture_price_png(page, price_path)
                    print(f"price.png 状态: {price_info['status']}")
                    if price_info.get("path") and price_path.exists():
                        print(f"price.png 绝对路径: {price_info['path']}")
                        print(f"像素尺寸: {price_info.get('pixel_size')}")
                        print(
                            f"内容边界(CSS viewport): "
                            f"{price_info.get('content_box_css_viewport')}"
                        )
                        print(
                            f"截图裁切(CSS viewport): "
                            f"{price_info.get('clip_box_css_viewport')}"
                        )
                        print(
                            f"价格标签覆盖: {price_info.get('price_label_coverage')}"
                        )
                    if price_info.get("error"):
                        print(f"price.png 错误: {price_info['error']}")

                    context_infos = capture_product_context_set(page, out_dir)
                    for ctx in context_infos:
                        label = ctx.get("filename") or ctx.get("role") or "context"
                        print(f"{label} 状态: {ctx.get('status')}")
                        if ctx.get("path") and ctx.get("filename"):
                            p = Path(str(ctx["path"]))
                            if p.exists():
                                print(f"  绝对路径: {ctx['path']}")
                                print(f"  像素尺寸: {ctx.get('pixel_size')}")
                        if ctx.get("error"):
                            print(f"  错误: {ctx['error']}")

                    try:
                        url_after = url_without_query_or_fragment(page.url)
                    except PlaywrightError:
                        url_after = "UNKNOWN"
                    if url_before_price and url_after == url_before_price:
                        url_consistency = "SAME_URL"
                    else:
                        url_consistency = "URL_CHANGED"
                        print(
                            "警告: 分次截图期间商品 URL 发生变化；"
                            "多张图不能当作同一状态的证据。"
                        )
                        print(f"  价图前: {url_before_price}")
                        print(f"  上下文后: {url_after}")

                wait_for_close()
                print("收到结束信号，正在关闭本次浏览器上下文…")
            except KeyboardInterrupt:
                print()
                print("收到 Ctrl+C，正在关闭本次浏览器上下文…")
                exit_code = 130
            finally:
                try:
                    context.close()
                    close_ok = True
                    print("程序关闭结果: 上下文关闭完成")
                except PlaywrightError as exc:
                    print(
                        f"程序关闭结果: 上下文关闭异常 ({type(exc).__name__}: {exc})"
                    )
                    raise
    except RuntimeError as exc:
        print(f"运行中止: {type(exc).__name__}: {exc}")
        print("程序关闭结果: 中止（见上方原因）")
        exit_code = 2
    except PlaywrightError as exc:
        if browser_started:
            print(f"浏览器已启动后的 Playwright 错误: {type(exc).__name__}: {exc}")
        else:
            print(f"Playwright 错误: {type(exc).__name__}: {exc}")
        if not close_ok:
            print("程序关闭结果: 未正常完成关闭或启动失败")
        exit_code = 1

    images: list[dict[str, Any]] = []
    if price_info is not None:
        images.append(price_info)
    images.extend(context_infos)

    metadata = {
        "run_id": run_id,
        "phase": "P2-C",
        "navigation_status": navigation_status,
        "browser_started": browser_started,
        "product_identity_ok": identity_ok,
        "actual_product_url": final_url_safe,
        "expected_product": EXPECTED_PRODUCT,
        "selector": {
            "main_price": MAIN_PRICE_SELECTOR,
            "match_count": (price_info or {}).get("match_count"),
            "visible": (price_info or {}).get("visible"),
        },
        "images": images,
        "url_consistency_across_captures": url_consistency,
        "ai_status": "NOT_STARTED",
        "recognized_price": None,
        "break_price_verdict": "UNKNOWN",
        "notes": [
            "截图仅用于后续 AI 图片识价输入验证",
            "不代表绕过登录、验证码或网站风控",
            "历史观察价禁止作为成功判据",
        ],
        "close_ok": close_ok,
        "written_at_utc": utc_iso(),
    }
    meta_path = out_dir / "metadata.json"
    try:
        write_metadata(meta_path, metadata)
        print(f"metadata.json 绝对路径: {meta_path.resolve()}")
    except OSError as exc:
        print(f"metadata.json 写入失败: {type(exc).__name__}: {exc}")

    print()
    print("=== P2-C 分项结果（非笼统 SUCCESS）===")
    print(f"浏览器启动结果: {'成功' if browser_started else '失败'}")
    print(f"导航状态: {navigation_status}")
    print(f"商品身份检查: {'通过' if identity_ok else '未通过或未确认'}")
    print(f"去参数商品链接: {final_url_safe}")
    if price_info is None:
        print("price.png: 未执行")
    else:
        print(f"price.png: {price_info.get('status')}")
    if not context_infos:
        print("商品信息截图: 未执行")
    else:
        for ctx in context_infos:
            label = ctx.get("filename") or ctx.get("role") or "context"
            print(f"{label}: {ctx.get('status')} (manual_assisted)")
    print(f"分次截图 URL 一致性: {url_consistency}")
    print("ai_status: NOT_STARTED")
    print("recognized_price: null")
    print("破价判定: UNKNOWN")
    print(f"程序关闭结果: {'正常关闭' if close_ok else '未正常关闭'}")
    print(f"输出目录: {out_dir}")

    successes = []
    failures = []
    for item in images:
        label = item.get("filename") or item.get("role") or "?"
        status = str(item.get("status", ""))
        if status.startswith("CAPTURED"):
            successes.append(f"{label}={status}")
        else:
            failures.append(f"{label}={status}")
    print(f"已生成待核验项: {successes if successes else '无'}")
    print(f"失败/跳过项: {failures if failures else '无'}")
    print("说明: 生成待核验 ≠ 截图验收通过；AI 识价尚未接入。")

    if exit_code == 130:
        return 130
    if exit_code != 0:
        return exit_code
    if not browser_started or not navigation_ok or not identity_ok:
        return 1
    price_status = str((price_info or {}).get("status", ""))
    if price_info is None or not price_status.startswith("CAPTURED"):
        return 1
    if price_info.get("integrity") == "NOT_ACCEPTED":
        return 1
    print("截图流程已结束。最终图片是否可用，以你的人工核验为准；AI 识价尚未接入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
