"""P1-B: JD manual login and persistent-session reuse (human verification only)."""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = (PROJECT_ROOT / "browser_profiles" / "jd_persistent").resolve()
JD_MY_HOME_VERIFY_URL = "https://home.jd.com/index.html"
NAVIGATION_TIMEOUT_MS = 45_000

MODE_FIRST = "first"
MODE_REUSE = "reuse"

LOGIN_EVIDENCE_SEEN = "seen"
LOGIN_STILL_NEEDED = "need_login"
LOGIN_UNKNOWN = "unknown"

EVIDENCE_ACCOUNT_INFO = "account_info"
EVIDENCE_ACCOUNT_PAGE = "account_page"
EVIDENCE_OTHER = "other"


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


def choose_mode() -> str:
    print("请选择本次运行模式：")
    print("  1) 首次登录验证")
    print("  2) 重启复用验证")
    while True:
        choice = read_line("输入 1 或 2 后回车: ").strip()
        if choice == "1":
            return MODE_FIRST
        if choice == "2":
            return MODE_REUSE
        print("无效输入，请只输入 1 或 2。")


def print_verification_guide(mode: str) -> None:
    print()
    print("=== 人工核验指引 ===")
    print("请在可见 Chrome 窗口中自行操作（本脚本不会自动填写账号、点击登录或绕过验证）。")
    print("核验登录时，请寻找明确证据，例如：")
    print("  - 「我的京东登录验证页」上可见的账户昵称 / 账号信息；")
    print("  - 或你自行打开的账户页面中显示的账户信息。")
    print("不能仅凭“登录提示消失”、页面加载成功或 Profile 目录存在认定已登录。")
    print("请勿在本终端输入真实昵称、手机号或密码。")
    if mode == MODE_FIRST:
        print("本模式：请手动完成登录及可能出现的验证，再回来回答问题。")
    else:
        print("本模式：请不要重新输入密码、扫码或短信验证；观察是否仍保持登录。")
        print("若你在本轮又执行了登录动作，即使最终已登录，也不能算复用通过。")
    print()


def ask_login_status() -> str:
    print("你是否看到明确的登录证据？")
    print("  1) 已看到明确的登录证据")
    print("  2) 仍需登录")
    print("  3) 无法确认")
    while True:
        choice = read_line("输入 1 / 2 / 3 后回车: ").strip()
        if choice == "1":
            return LOGIN_EVIDENCE_SEEN
        if choice == "2":
            return LOGIN_STILL_NEEDED
        if choice == "3":
            return LOGIN_UNKNOWN
        print("无效输入，请只输入 1、2 或 3。")


def ask_reauth_happened() -> bool:
    print("本次是否重新输入密码、扫码、短信验证，或执行了其他登录动作？")
    print("  1) 是（发生了重新认证）")
    print("  2) 否（未进行新的登录动作）")
    while True:
        choice = read_line("输入 1 或 2 后回车: ").strip()
        if choice == "1":
            return True
        if choice == "2":
            return False
        print("无效输入，请只输入 1 或 2。")


def ask_evidence_type() -> str:
    print("证据类型（不记录具体个人信息）：")
    print("  1) 账户信息可见")
    print("  2) 账户页面可访问且显示账户信息")
    print("  3) 其他或不确定")
    while True:
        choice = read_line("输入 1 / 2 / 3 后回车: ").strip()
        if choice == "1":
            return EVIDENCE_ACCOUNT_INFO
        if choice == "2":
            return EVIDENCE_ACCOUNT_PAGE
        if choice == "3":
            return EVIDENCE_OTHER
        print("无效输入，请只输入 1、2 或 3。")


def login_status_label(status: str) -> str:
    return {
        LOGIN_EVIDENCE_SEEN: "已看到明确的登录证据",
        LOGIN_STILL_NEEDED: "仍需登录",
        LOGIN_UNKNOWN: "无法确认（UNKNOWN）",
    }[status]


def evidence_type_label(evidence: str) -> str:
    return {
        EVIDENCE_ACCOUNT_INFO: "账户信息可见",
        EVIDENCE_ACCOUNT_PAGE: "账户页面可访问且显示账户信息",
        EVIDENCE_OTHER: "其他或不确定",
    }[evidence]


def wait_for_close() -> None:
    print()
    print("问答已记录。浏览器仍保持打开，请确认无需继续观察后关闭本次会话。")
    read_line("按回车结束本次自动化浏览器会话… ")


def collect_human_verification(mode: str) -> dict[str, object]:
    print_verification_guide(mode)
    read_line("完成窗口内操作后，回到本终端按回车开始回答核验问题… ")

    login_status = ask_login_status()
    reauth = ask_reauth_happened()
    evidence_type = ask_evidence_type()

    reuse_confirmed = (
        mode == MODE_REUSE
        and login_status == LOGIN_EVIDENCE_SEEN
        and not reauth
    )
    reuse_failed_due_to_reauth = (
        mode == MODE_REUSE and reauth
    )

    return {
        "login_status": login_status,
        "reauth": reauth,
        "evidence_type": evidence_type,
        "reuse_confirmed": reuse_confirmed,
        "reuse_failed_due_to_reauth": reuse_failed_due_to_reauth,
    }


def print_human_summary(mode: str, result: dict[str, object]) -> None:
    print()
    print("=== 用户人工核验结果（非敏感摘要）===")
    print(f"运行模式: {'首次登录验证' if mode == MODE_FIRST else '重启复用验证'}")
    print(f"登录证据判断: {login_status_label(str(result['login_status']))}")
    print(f"证据类型: {evidence_type_label(str(result['evidence_type']))}")
    print(f"本次是否重新认证: {'是' if result['reauth'] else '否'}")

    if mode == MODE_REUSE:
        if result["reuse_confirmed"]:
            print("复用判定: 本次复用经人工确认")
        elif result["reuse_failed_due_to_reauth"]:
            print("复用判定: 本次发生重新认证，不能算复用通过")
        elif result["login_status"] == LOGIN_UNKNOWN:
            print("复用判定: UNKNOWN（无法确认，不推断登录成功）")
        elif result["login_status"] == LOGIN_STILL_NEEDED:
            print("复用判定: 未通过（仍需登录）")
        else:
            print("复用判定: 未通过")
    else:
        if result["login_status"] == LOGIN_EVIDENCE_SEEN:
            print("首次登录判定: 用户确认已看到明确登录证据（人工确认，非自动检测）")
        elif result["login_status"] == LOGIN_UNKNOWN:
            print("首次登录判定: UNKNOWN（无法确认，不推断登录成功）")
        else:
            print("首次登录判定: 用户确认仍需登录")


def main() -> int:
    print("=== PriceRadar P1-B ===")
    print(f"解释器: {sys.executable}")
    print(f"项目根: {PROJECT_ROOT}")
    print(f"独立 Profile（绝对路径）: {PROFILE_DIR}")
    print(f"Playwright: {playwright_version()}")
    print("本轮目标: 京东手动登录 + 同一 Profile 重启复用（人工核验；不采价、不 OCR）")
    print()
    print("重要提醒: 请先结束任何仍在使用该 Profile 的会话（包括 P1-A），避免同时占用。")
    print("核验依据仅为你的人工观察；本脚本不实现自动登录检测。")
    print()

    mode = choose_mode()
    mode_label = "首次登录验证" if mode == MODE_FIRST else "重启复用验证"
    print(f"已选择: {mode_label}")
    print()

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    browser_started = False
    navigation_ok = False
    close_ok = False
    human_result: dict[str, object] | None = None
    exit_code = 0

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
            print("浏览器启动结果: 成功（channel=chrome, headless=False, persistent context）")

            try:
                page = context.pages[0] if context.pages else context.new_page()

                try:
                    response = page.goto(
                        JD_MY_HOME_VERIFY_URL,
                        wait_until="domcontentloaded",
                        timeout=NAVIGATION_TIMEOUT_MS,
                    )
                    title = page.title()
                    status = response.status if response is not None else None
                    navigation_ok = True
                    print(
                        "我的京东登录验证页导航结果: "
                        f"成功 host=home.jd.com status={status} title={title!r}"
                    )
                except PlaywrightError as exc:
                    print(
                        "我的京东登录验证页导航结果: 失败（浏览器已启动，请保留窗口观察）"
                    )
                    print(f"异常类型: {type(exc).__name__}")
                    print(f"错误信息: {exc}")
                    try:
                        print(f"当前 title={page.title()!r}")
                    except PlaywrightError as title_exc:
                        print(
                            f"无法读取当前页面标题: {type(title_exc).__name__}: {title_exc}"
                        )

                human_result = collect_human_verification(mode)
                print_human_summary(mode, human_result)

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
                    print(f"程序关闭结果: 上下文关闭异常 ({type(exc).__name__}: {exc})")
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
    print("=== P1-B 分项结果（非笼统 SUCCESS）===")
    print(f"浏览器启动结果: {'成功' if browser_started else '失败'}")
    print(
        f"我的京东登录验证页导航结果: {'成功' if navigation_ok else '失败'}"
    )
    if human_result is None:
        print("用户人工核验结果: 未完成（UNKNOWN）")
        print("是否发生本次重新认证: UNKNOWN")
    else:
        print(f"用户人工核验结果: {login_status_label(str(human_result['login_status']))}")
        print(f"是否发生本次重新认证: {'是' if human_result['reauth'] else '否'}")
        if mode == MODE_REUSE:
            if human_result["reuse_confirmed"]:
                print("复用结论: 本次复用经人工确认")
            elif human_result["reuse_failed_due_to_reauth"]:
                print("复用结论: 本次发生重新认证，不能算复用通过")
            else:
                print("复用结论: 未通过或 UNKNOWN（不推断）")
    print(f"程序关闭结果: {'正常关闭' if close_ok else '未正常关闭'}")

    if exit_code == 130:
        return 130

    if not browser_started:
        return 1
    if human_result is None:
        return 1
    if mode == MODE_REUSE and not human_result["reuse_confirmed"]:
        return 1
    if mode == MODE_FIRST and human_result["login_status"] != LOGIN_EVIDENCE_SEEN:
        return 1
    if not navigation_ok:
        print(
            "说明: 浏览器曾启动，但我的京东登录验证页导航失败；请结合人工观察判断。"
        )
        return 1

    print("脚本流程已正常结束。最终是否通过，以你两次真实运行的人工反馈为准。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
