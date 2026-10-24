"""P2-A: open one JD product short link for human product-page verification."""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = (PROJECT_ROOT / "browser_profiles" / "jd_persistent").resolve()

PRODUCT_SHORT_URL = "https://3.cn/37Ie-Wjh?jkl=@C2mg06oK7DA@"
PRODUCT_SHARE_TITLE = "清风抽纸擦手纸单层200抽*20包"
BUSINESS_SKU = "B913A"
TARGET_SPEC_HINT = "单层200抽×20包"
RED_LINE_PRICE_CNY = 127
NAVIGATION_TIMEOUT_MS = 60_000


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


def print_mission() -> None:
    print("=== PriceRadar P2-A ===")
    print(f"解释器: {sys.executable}")
    print(f"项目根: {PROJECT_ROOT}")
    print(f"独立 Profile（绝对路径）: {PROFILE_DIR}")
    print(f"Playwright: {playwright_version()}")
    print("本轮目标: 单商品短链接访问 + 人工核验（不自动采价、不做破价判定）")
    print()
    print("=== 本轮目标商品（业务输入，非采集结果）===")
    print(f"分享标题: {PRODUCT_SHARE_TITLE}")
    print(f"业务 SKU／型号: {BUSINESS_SKU}（不等于已确认的京东数字商品 ID）")
    print(f"待核实规格: {TARGET_SPEC_HINT}")
    print(f"业务红线价: {RED_LINE_PRICE_CNY} 元／箱（业务基准，不是页面采集结果）")
    print("待核实: 20包是否为一箱；型号与当前选中规格是否匹配。")
    print()
    print("重要提醒: 请先结束占用同一 Profile 的其他脚本会话，避免同时占用。")
    print("说明: https://www.jd.com/ 在脚本环境下异常原因未确认；本轮不访问京东首页。")
    print()


def print_observation_guide() -> None:
    print()
    print("=== 请人工观察（脚本不读取 DOM、不自动点击）===")
    print("1) 当前是否为目标商品详情页。")
    print(f"2) 型号是否为 {BUSINESS_SKU}；看不到则记为无法确认。")
    print(f"3) 当前选中规格是否为 {TARGET_SPEC_HINT}。")
    print("4) 20包是否明确对应一箱。")
    print("5) 店铺名称。")
    print("6) 页面显示的价格金额与原始标签（例如划线价、券后价等文案）。")
    print("7) 是否存在会员、领券、购买数量、新人、地区或其他优惠条件。")
    print()
    print("你可以在窗口内手动切换到目标规格；脚本不会自动点击。")
    print("HTTP 状态或页面标题不能代表商品页正常。")
    print("若短链接失败、停在中转页、要求打开 App、出现登录/验证或异常页：")
    print("  请保留窗口观察；不要自动重试。可改从日常正常浏览器复制商品详情页链接后再议。")
    print("请勿在本终端粘贴 Cookie、Token、账户详情或完整带参 URL。")
    print()


def wait_for_close() -> None:
    print("浏览器保持打开，请完成观察后回到本终端。")
    read_line("按回车结束本次自动化浏览器会话… ")


def main() -> int:
    print_mission()

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    browser_started = False
    navigation_ok = False
    close_ok = False
    exit_code = 0
    final_url_safe = "UNKNOWN"

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
                        PRODUCT_SHORT_URL,
                        wait_until="domcontentloaded",
                        timeout=NAVIGATION_TIMEOUT_MS,
                    )
                    status = response.status if response is not None else None
                    final_url_safe = url_without_query_or_fragment(page.url)
                    navigation_ok = True
                    print(
                        f"导航结果: 命令已完成（允许短链跳转）"
                        f" http_status={status} final={final_url_safe}"
                    )
                    print(
                        "说明: 导航命令完成不等于商品详情页正常；请以窗口内人工观察为准。"
                    )
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
                    print(
                        "若本机短链接持续失败，请从日常正常浏览器打开商品后，"
                        "复制详情页链接再提供；请勿猜测京东数字商品 ID。"
                    )

                print_observation_guide()
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
    print("=== P2-A 分项结果（非笼统 SUCCESS）===")
    print(f"浏览器启动结果: {'成功' if browser_started else '失败'}")
    print(f"导航结果: {'命令完成' if navigation_ok else '失败'}")
    print(f"最终页面（协议+主机+路径）: {final_url_safe}")
    print("人工观察结果: 请以你在窗口中的确认反馈为准（脚本未自动判定）")
    print(f"程序关闭结果: {'正常关闭' if close_ok else '未正常关闭'}")
    print(
        f"业务红线价提醒: {RED_LINE_PRICE_CNY} 元／箱仅为业务基准，不是本轮采集结果。"
    )

    if exit_code == 130:
        return 130
    if not browser_started:
        return 1
    if not navigation_ok:
        return 1

    print("脚本流程已正常结束。最终是否通过，以你对商品页的真实验收为准。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
