"""P1-A: open a visible system Chrome with an isolated persistent profile."""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = PROJECT_ROOT / "browser_profiles" / "jd_persistent"
TEST_URL = "https://example.com"
NAVIGATION_TIMEOUT_MS = 30_000


def playwright_version() -> str:
    try:
        return version("playwright")
    except PackageNotFoundError:
        return "unknown"


def wait_for_user_exit() -> None:
    print()
    print("浏览器应保持打开。请在本窗口自行操作，确认完成后回到此终端按回车关闭。")
    try:
        input("按回车结束本次自动化浏览器会话… ")
    except EOFError as exc:
        raise RuntimeError(
            "当前 stdin 不可交互（EOF）。请在本机交互式终端中运行本脚本，"
            "不要通过无 stdin 的自动化工具启动。"
        ) from exc


def main() -> int:
    print("=== PriceRadar P1-A ===")
    print(f"解释器: {sys.executable}")
    print(f"项目根: {PROJECT_ROOT}")
    print(f"独立 Profile: {PROFILE_DIR}")
    print(f"Playwright: {playwright_version()}")
    print("本轮目标: 可见 Chrome + example.com + 等待回车后正常关闭（不登录京东、不采价）")
    print()

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    browser_started = False
    navigation_ok = False
    exit_code = 0

    try:
        with sync_playwright() as playwright:
            try:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(PROFILE_DIR),
                    channel="chrome",
                    headless=False,
                )
            except PlaywrightError as exc:
                print("浏览器启动: 失败")
                print(f"异常类型: {type(exc).__name__}")
                print(f"错误信息: {exc}")
                raise

            browser_started = True
            print("浏览器启动: 成功（channel=chrome, headless=False, persistent context）")

            try:
                page = context.pages[0] if context.pages else context.new_page()

                try:
                    response = page.goto(
                        TEST_URL,
                        wait_until="domcontentloaded",
                        timeout=NAVIGATION_TIMEOUT_MS,
                    )
                    title = page.title()
                    status = response.status if response is not None else None
                    navigation_ok = True
                    print(
                        f"导航结果: 成功 url={page.url!r} status={status} title={title!r}"
                    )
                except PlaywrightError as exc:
                    print("导航结果: 失败（浏览器已启动，请保留窗口观察）")
                    print(f"异常类型: {type(exc).__name__}")
                    print(f"错误信息: {exc}")
                    try:
                        print(f"当前 url={page.url!r} title={page.title()!r}")
                    except PlaywrightError as title_exc:
                        print(
                            f"无法读取当前页面信息: {type(title_exc).__name__}: {title_exc}"
                        )

                wait_for_user_exit()
                print("收到结束信号，正在关闭本次浏览器上下文…")
            except KeyboardInterrupt:
                print()
                print("收到 Ctrl+C，正在关闭本次浏览器上下文…")
                exit_code = 130
            finally:
                try:
                    context.close()
                    print("上下文关闭: 完成")
                except PlaywrightError as exc:
                    print(f"上下文关闭异常: {type(exc).__name__}: {exc}")
                    raise
    except RuntimeError as exc:
        print(f"运行中止: {type(exc).__name__}: {exc}")
        return 2
    except PlaywrightError as exc:
        if browser_started:
            print(f"浏览器已启动后的 Playwright 错误: {type(exc).__name__}: {exc}")
        else:
            print(f"Playwright 错误: {type(exc).__name__}: {exc}")
        return 1

    if exit_code == 130:
        return 130

    if browser_started and not navigation_ok:
        print("P1-A 小结: 浏览器启动成功，但导航失败；请结合窗口观察与终端错误判断。")
        return 1

    print("P1-A 小结: 脚本流程已正常结束。最终是否通过，以你对可见窗口的真实验收为准。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
