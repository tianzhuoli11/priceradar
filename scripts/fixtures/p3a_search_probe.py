"""
One-off JD search probe (diagnosis only). Not the production P3-A entry.

Tries:
1) direct search.jd.com URL
2) www.jd.com homepage search box submit

Saves screenshot + light DOM stats under artifacts/search_probes/.
Does not invent product IDs. Does not retry on restriction tips.
"""

from __future__ import annotations

import json
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = (PROJECT_ROOT / "browser_profiles" / "jd_persistent").resolve()
OUT_ROOT = (PROJECT_ROOT / "artifacts" / "search_probes").resolve()

QUERY = "清风 B913A"
SEARCH_URL = (
    "https://search.jd.com/Search?keyword="
    + quote(QUERY, safe="")
    + "&enc=utf-8"
)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def page_stats(page) -> dict:
    return page.evaluate(
        """() => {
            const text = (document.body && document.body.innerText || '')
                .replace(/\\s+/g, ' ').trim();
            const hrefs = Array.from(document.querySelectorAll('a[href]'))
                .map((a) => a.getAttribute('href') || '');
            const item = hrefs.filter((h) =>
                /item\\.jd\\.com\\/\\d+\\.html/i.test(h)
                || /item\\.m\\.jd\\.com\\/product\\/\\d+/i.test(h)
            );
            const tips = [];
            for (const p of [
                '访问频繁导致无法搜索',
                '无法搜索，请稍后再试',
                '抱歉由于访问频繁',
                '登录',
                '验证',
            ]) {
                if (text.includes(p)) tips.push(p);
            }
            return {
                title: document.title || '',
                url: location.href,
                text_len: text.length,
                text_head: text.slice(0, 400),
                anchor_total: hrefs.length,
                item_like_hrefs: item.slice(0, 20),
                item_like_count: item.length,
                tip_hits: tips,
            };
        }"""
    )


def main() -> int:
    run_id = f"{utc_stamp()}_{secrets.token_hex(3)}"
    out = OUT_ROOT / run_id
    out.mkdir(parents=True, exist_ok=False)
    report: dict = {
        "run_id": run_id,
        "query": QUERY,
        "profile": str(PROFILE_DIR),
        "steps": [],
    }
    print(f"probe out: {out}")
    print(f"profile: {PROFILE_DIR}")

    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel="chrome",
                headless=False,
                chromium_sandbox=True,
            )
        except PlaywrightError as exc:
            print(f"launch failed: {exc}")
            return 1

        try:
            page = context.pages[0] if context.pages else context.new_page()

            # Step A: direct search URL (same as p3a)
            print("=== A: direct search.jd.com ===")
            page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2500)
            stats_a = page_stats(page)
            shot_a = out / "A_direct_search.png"
            page.screenshot(path=str(shot_a), full_page=False)
            report["steps"].append(
                {
                    "name": "direct_search_url",
                    "stats": stats_a,
                    "screenshot": str(shot_a),
                }
            )
            print(
                f"  title={stats_a.get('title')!r} "
                f"item_like={stats_a.get('item_like_count')} "
                f"tips={stats_a.get('tip_hits')}"
            )

            # Step B: homepage search box
            print("=== B: www.jd.com search box ===")
            page.goto("https://www.jd.com/", wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2000)
            filled = page.evaluate(
                """(q) => {
                    const sels = [
                        '#key',
                        'input[aria-label*="搜索"]',
                        'input[name="keyword"]',
                        'input[type="text"]',
                    ];
                    let el = null;
                    for (const s of sels) {
                        const n = document.querySelector(s);
                        if (n && n.offsetParent !== null) { el = n; break; }
                    }
                    if (!el) return { ok: false, reason: 'no_visible_search_input' };
                    el.focus();
                    el.value = q;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return {
                        ok: true,
                        id: el.id || null,
                        name: el.getAttribute('name'),
                        value_len: (el.value || '').length,
                    };
                }""",
                QUERY,
            )
            report["homepage_fill"] = filled
            print(f"  fill={filled}")

            # Try Enter / click search button
            clicked = {"method": None}
            try:
                page.keyboard.press("Enter")
                clicked["method"] = "Enter"
            except PlaywrightError:
                pass
            page.wait_for_timeout(3000)
            # If still on homepage, try button
            if "search.jd.com" not in page.url and "so.m.jd.com" not in page.url:
                for sel in ("#search-btn", ".button", "button", 'a[clstag*="search"]'):
                    try:
                        loc = page.locator(sel).first
                        if loc.count() and loc.is_visible():
                            loc.click(timeout=2000)
                            clicked["method"] = f"click:{sel}"
                            page.wait_for_timeout(3000)
                            break
                    except PlaywrightError:
                        continue

            stats_b = page_stats(page)
            shot_b = out / "B_homepage_search.png"
            page.screenshot(path=str(shot_b), full_page=False)
            report["steps"].append(
                {
                    "name": "homepage_search_box",
                    "clicked": clicked,
                    "stats": stats_b,
                    "screenshot": str(shot_b),
                }
            )
            print(
                f"  url={stats_b.get('url')!r} "
                f"item_like={stats_b.get('item_like_count')} "
                f"tips={stats_b.get('tip_hits')}"
            )

            print("probe done; closing in 2s")
            page.wait_for_timeout(2000)
        finally:
            context.close()

    (out / "probe_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"report: {out / 'probe_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
