"""P2-B: JD single-product display-price read (inspect-first; no guessed selectors)."""

from __future__ import annotations

import argparse
import re
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

PRODUCT_DETAIL_URL = "https://item.m.jd.com/product/100005149674.html"
EXPECTED_PRODUCT_ID = "100005149674"

# Business baselines only — not scrape results, not fallbacks, not success criteria.
BUSINESS_SKU = "B913A"
BUSINESS_SPEC = "单层200抽×20包／箱"
BUSINESS_SHOP = "清风京东自营旗舰店"
RED_LINE_PRICE_CNY = 127

NAVIGATION_TIMEOUT_MS = 60_000
MAIN_PRICE_SELECTOR = "#main_price"
MAIN_PRICE_WAIT_MS = 15_000
ATTR_VALUE_MAX_LEN = 120
# Plain ASCII digit price fragment (not a permanent glyph map).
PLAIN_DIGIT_PRICE_RE = re.compile(r"(?:¥|￥)?\s*\d{1,7}(?:\.\d{1,2})?")

MODE_INSPECT = "inspect"
MODE_READ = "read"


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


def url_without_query_or_fragment(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path if parts.path else "/"
    return f"{parts.scheme}://{parts.netloc}{path}"


def product_id_in_url(url: str) -> bool:
    path = urlsplit(url).path
    return EXPECTED_PRODUCT_ID in path


def now_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def print_common_header(mode: str) -> None:
    print("=== PriceRadar P2-B ===")
    print(f"解释器: {sys.executable}")
    print(f"项目根: {PROJECT_ROOT}")
    print(f"独立 Profile（绝对路径）: {PROFILE_DIR}")
    print(f"Playwright: {playwright_version()}")
    print(f"运行模式: {mode}")
    print(f"采集时间戳（脚本启动，UTC）: {now_stamp()}")
    print()
    print("=== 预先登记的业务基准（不是本次页面采集结果）===")
    print(f"商品详情链接: {PRODUCT_DETAIL_URL}")
    print(f"京东商品 ID: {EXPECTED_PRODUCT_ID}")
    print(f"业务型号: {BUSINESS_SKU}")
    print(f"业务规格: {BUSINESS_SPEC}")
    print(f"业务店铺: {BUSINESS_SHOP}")
    print(
        f"红线价: {RED_LINE_PRICE_CNY} 元／箱（低于才算破价，等于不算；本轮不做破价判定）"
    )
    print("说明: 上次人工观察的 168 元／箱仅作历史参考，禁止作为抓取常量或成功标准。")
    print()
    print("重要提醒: 请先结束占用同一 Profile 的其他脚本会话。")
    print()


def print_inspect_guide() -> None:
    print("=== inspect 模式：#main_price 局部只读检查（尚无可靠价格读取）===")
    print("状态标记: 已有 #main_price DOM 证据，但自定义字体文本不可直接 Decimal 解析；")
    print("          本轮只确认局部是否存在可核验的普通数字价格来源，不宣称采价成功。")
    print("依据用户提供的价格节点证据使用选择器 #main_price；不做整页扫描。")
    print()
    print("请人工对照窗口中的主展示价；若局部检查无普通数字价，可补充属性线索反馈。")
    print("请勿导出整页 HTML；请勿反馈 Cookie / Token / 账户信息。")
    print()


def wait_for_close() -> None:
    print("浏览器保持打开，完成对照观察后回到本终端。")
    read_line("按回车结束本次自动化浏览器会话… ")


def unicode_escape(text: str) -> str:
    return text.encode("unicode_escape").decode("ascii")


def truncate_attr(value: str) -> str:
    if len(value) <= ATTR_VALUE_MAX_LEN:
        return value
    return value[:ATTR_VALUE_MAX_LEN] + "…(truncated)"


def has_ascii_digit(value: str) -> bool:
    return any(("0" <= ch <= "9") for ch in value)


def looks_like_plain_digit_price(value: str) -> bool:
    if not value or not has_ascii_digit(value):
        return False
    return PLAIN_DIGIT_PRICE_RE.search(value) is not None


def is_price_related_attr_name(name: str) -> bool:
    lower = name.lower()
    if lower in {"aria-label", "title", "content"}:
        return True
    if lower.startswith("data-"):
        return True
    return False


def collect_plain_digit_candidates(
    node_desc: dict[str, Any],
) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    attrs = node_desc.get("attrs") or {}
    role = str(node_desc.get("role", "?"))
    tag = str(node_desc.get("tag", "?"))
    for name, raw in attrs.items():
        if not isinstance(name, str) or not isinstance(raw, str):
            continue
        if not is_price_related_attr_name(name):
            continue
        if not looks_like_plain_digit_price(raw):
            continue
        found.append(
            {
                "node": f"{role} <{tag}>",
                "attr": name,
                "value": truncate_attr(raw),
            }
        )
    return found


def run_main_price_local_inspect(page: Page) -> dict[str, Any]:
    """Bounded local check around #main_price only. Never scans the whole page."""
    result: dict[str, Any] = {
        "status": "NOT_CHECKED",
        "match_count": 0,
        "visible": False,
        "plain_digit_candidates": [],
    }
    print()
    print("=== #main_price 局部 DOM 检查 ===")
    try:
        page.wait_for_selector(
            MAIN_PRICE_SELECTOR,
            timeout=MAIN_PRICE_WAIT_MS,
            state="attached",
        )
    except PlaywrightError as exc:
        count = page.locator(MAIN_PRICE_SELECTOR).count()
        result["status"] = "SELECTOR_WAIT_FAILED"
        result["match_count"] = count
        print(f"选择器: {MAIN_PRICE_SELECTOR}")
        print(f"有界等待结果: 失败（timeout={MAIN_PRICE_WAIT_MS}ms）")
        print(f"匹配数量: {count}")
        print(f"异常类型: {type(exc).__name__}")
        print(f"错误信息: {exc}")
        print("本轮局部 DOM 检查未发现普通数字价格（节点未就绪）。")
        return result

    snapshot = page.evaluate(
        """() => {
            const sel = '#main_price';
            const nodes = Array.from(document.querySelectorAll(sel));
            const count = nodes.length;
            if (count === 0) {
                return { count, visible: false };
            }
            if (count !== 1) {
                return { count, visible: false };
            }
            const el = nodes[0];
            const cs = window.getComputedStyle(el);
            const visible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                && cs.visibility !== 'hidden'
                && cs.display !== 'none'
                && cs.opacity !== '0';

            function pickAttrs(node) {
                const out = {};
                if (!node || !node.attributes) return out;
                for (const a of Array.from(node.attributes)) {
                    const n = a.name;
                    if (
                        n === 'aria-label'
                        || n === 'title'
                        || n === 'content'
                        || n.startsWith('data-')
                    ) {
                        out[n] = a.value;
                    }
                }
                return out;
            }

            function describe(node, role) {
                if (!node || node.nodeType !== 1) return null;
                let className = '';
                if (typeof node.className === 'string') {
                    className = node.className.slice(0, 80);
                }
                return {
                    role,
                    tag: node.tagName.toLowerCase(),
                    id: node.id || '',
                    className,
                    attrs: pickAttrs(node),
                };
            }

            const self = describe(el, 'self');
            const descendants = Array.from(el.querySelectorAll('*')).map(
                (node, i) => describe(node, 'descendant[' + i + ']')
            );
            const parents = [];
            let parent = el.parentElement;
            for (let depth = 1; depth <= 2 && parent; depth += 1) {
                parents.push(describe(parent, 'parent[' + depth + ']'));
                parent = parent.parentElement;
            }

            const contentNode =
                el.querySelector('em')
                || el.querySelector('.price_decimals')
                || el.querySelector('span')
                || el;
            const fontFamily = window.getComputedStyle(contentNode).fontFamily;

            return {
                count,
                visible,
                textContent: el.textContent || '',
                self,
                descendants,
                parents,
                contentNodeTag: contentNode.tagName.toLowerCase(),
                contentNodeClass:
                    typeof contentNode.className === 'string'
                        ? contentNode.className.slice(0, 80)
                        : '',
                fontFamily,
            };
        }"""
    )

    match_count = int(snapshot.get("count") or 0)
    visible = bool(snapshot.get("visible"))
    result["match_count"] = match_count
    result["visible"] = visible
    print(f"选择器: {MAIN_PRICE_SELECTOR}")
    print(f"匹配数量: {match_count}")
    print(f"可见性: {'可见' if visible else '不可见或无法确认'}")

    if match_count != 1:
        result["status"] = "MATCH_COUNT_NOT_ONE"
        print("继续条件未满足: 需要唯一匹配。已输出实际状态，不做扩大扫描。")
        print("本轮局部 DOM 检查未发现普通数字价格。")
        return result
    if not visible:
        result["status"] = "NOT_VISIBLE"
        print("继续条件未满足: 唯一节点但不可见。已输出实际状态，不做扩大扫描。")
        print("本轮局部 DOM 检查未发现普通数字价格。")
        return result

    text_content = str(snapshot.get("textContent") or "")
    print(f"价格节点 textContent 原始: {text_content!r}")
    print(f"价格节点 textContent Unicode 转义: {unicode_escape(text_content)}")
    print(
        "价格内容节点 computed font-family: "
        f"tag={snapshot.get('contentNodeTag')} "
        f"class={snapshot.get('contentNodeClass')!r} "
        f"font-family={snapshot.get('fontFamily')!r}"
    )

    nodes_to_scan: list[dict[str, Any]] = []
    if isinstance(snapshot.get("self"), dict):
        nodes_to_scan.append(snapshot["self"])
    for item in snapshot.get("descendants") or []:
        if isinstance(item, dict):
            nodes_to_scan.append(item)
    for item in snapshot.get("parents") or []:
        if isinstance(item, dict):
            nodes_to_scan.append(item)

    candidates: list[dict[str, str]] = []
    for node_desc in nodes_to_scan:
        candidates.extend(collect_plain_digit_candidates(node_desc))

    # Deduplicate while preserving order.
    unique: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in candidates:
        key = (item["node"], item["attr"], item["value"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    result["plain_digit_candidates"] = unique

    if unique:
        result["status"] = "PLAIN_DIGIT_CANDIDATES_FOUND"
        print("发现包含普通数字价格的相关属性（候选，待核验；不得直接认定为当前展示价）：")
        for item in unique:
            print(
                f"  - 节点={item['node']} 属性={item['attr']} "
                f"原始值={item['value']!r}"
            )
    else:
        result["status"] = "NO_PLAIN_DIGIT_PRICE"
        print("本轮局部 DOM 检查未发现普通数字价格。")
        print("（仅检查了 #main_price 自身、其子节点及最多两层父节点的相关属性。）")

    return result


def launch_and_open_product(mode: str) -> int:
    print_common_header(mode)

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    browser_started = False
    navigation_ok = False
    identity_ok = False
    close_ok = False
    exit_code = 0
    final_url_safe = "UNKNOWN"
    inspect_local: dict[str, Any] = {"status": "NOT_RUN"}

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
                    print(
                        f"导航结果: 命令完成 http_status={status} "
                        f"final={final_url_safe}"
                    )
                    print("说明: 导航完成不等于商品页正常，也不等于可以采价。")
                except PlaywrightError as exc:
                    print("导航结果: 失败（浏览器已启动，请保留窗口观察）")
                    print(f"异常类型: {type(exc).__name__}")
                    print(f"错误信息: {exc}")
                    try:
                        final_url_safe = url_without_query_or_fragment(page.url)
                        print(f"当前页面（无查询参数）: {final_url_safe}")
                    except PlaywrightError as url_exc:
                        print(
                            f"无法读取当前 URL: {type(url_exc).__name__}: {url_exc}"
                        )

                if navigation_ok:
                    try:
                        identity_ok = product_id_in_url(page.url)
                    except PlaywrightError:
                        identity_ok = False
                    if identity_ok:
                        print(
                            f"商品身份检查: 最终路径含预期商品 ID {EXPECTED_PRODUCT_ID}"
                        )
                    else:
                        print(
                            "商品身份检查: 未在最终路径中确认预期商品 ID；"
                            "可能是登录/验证/异常页或其他商品。请人工观察窗口。"
                        )
                        print("本轮不得输出成功采价。")

                if mode == MODE_INSPECT:
                    print_inspect_guide()
                    if navigation_ok and identity_ok:
                        inspect_local = run_main_price_local_inspect(page)
                    elif navigation_ok:
                        inspect_local = {
                            "status": "SKIPPED_IDENTITY_NOT_OK",
                            "match_count": 0,
                            "visible": False,
                            "plain_digit_candidates": [],
                        }
                        print()
                        print("=== #main_price 局部 DOM 检查 ===")
                        print(
                            "已跳过：商品身份未确认，避免在错误页面上检查价格节点。"
                        )
                    else:
                        inspect_local = {
                            "status": "SKIPPED_NAVIGATION_FAILED",
                            "match_count": 0,
                            "visible": False,
                            "plain_digit_candidates": [],
                        }
                        print()
                        print("=== #main_price 局部 DOM 检查 ===")
                        print("已跳过：导航失败。")
                else:
                    print("=== read 模式 ===")
                    print(
                        "读取状态: NOT_IMPLEMENTED"
                        "（自定义字体文本不可直接 Decimal 解析；"
                        "且不得依据单样本建立永久字符映射）"
                    )
                    print("解析金额: （空）")
                    print("破价判定: UNKNOWN")
                    print("人工核验状态: 未开始（read 尚未实现）")

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
        return 2
    except PlaywrightError as exc:
        if browser_started:
            print(f"浏览器已启动后的 Playwright 错误: {type(exc).__name__}: {exc}")
        else:
            print(f"Playwright 错误: {type(exc).__name__}: {exc}")
        if not close_ok:
            print("程序关闭结果: 未正常完成关闭或启动失败")
        return 1

    print()
    print("=== P2-B 分项结果（非笼统 SUCCESS）===")
    print(f"浏览器启动结果: {'成功' if browser_started else '失败'}")
    print(f"导航结果: {'命令完成' if navigation_ok else '失败'}")
    print(f"去参数商品链接: {final_url_safe}")
    print(f"商品身份检查: {'通过' if identity_ok else '未通过或未确认'}")
    if mode == MODE_INSPECT:
        print(
            "模式结果: inspect 局部检查已执行；尚未实现可靠价格读取；不宣称采价成功"
        )
        print(f"局部检查状态: {inspect_local.get('status')}")
        print(f"#main_price 匹配数量: {inspect_local.get('match_count')}")
        print(f"#main_price 可见性: {inspect_local.get('visible')}")
        cand_n = len(inspect_local.get("plain_digit_candidates") or [])
        print(f"普通数字价格候选数: {cand_n}（均为待核验，非认定展示价）")
        print("价格读取状态: NOT_IMPLEMENTED")
        print("解析金额: （空）")
        print("破价判定: UNKNOWN")
    else:
        print("模式结果: read 未实现")
        print("价格读取状态: NOT_IMPLEMENTED")
        print("解析金额: （空）")
        print("破价判定: UNKNOWN")
    print(f"程序关闭结果: {'正常关闭' if close_ok else '未正常关闭'}")

    if exit_code == 130:
        return 130
    if not browser_started or not navigation_ok:
        return 1
    # inspect 成功打开页面并关闭即流程完成；read 因未实现返回非 0
    if mode == MODE_READ:
        return 1
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PriceRadar P2-B: inspect DOM first; read only after real evidence."
    )
    parser.add_argument(
        "--mode",
        choices=(MODE_INSPECT, MODE_READ),
        default=MODE_INSPECT,
        help="inspect=收集主价格 DOM 证据（默认）；read=需已有证据后才实现",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return launch_and_open_product(args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
