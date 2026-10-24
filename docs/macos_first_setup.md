# PriceRadar macOS 首次设置指南（接收方）

适用于：在 **Mac** 上用 **WorkBuddy** 运行本项目的第一版（已登记商品单次检查，当前真实验证商品为 **B913A**）。

**说明**

- 安装 WorkBuddy Skill **不会**自动安装 Python / Chrome / 项目依赖。
- **不要**从 Windows 拷贝 `.venv` 或 `browser_profiles`。
- 本指南中的命令需在 **项目根目录**执行（解压后的 `priceradar/`）。
- Windows 历史验收价（例如 168.00）**不是**固定预期，也禁止当作实时兜底。
- **macOS / WorkBuddy 实机运行尚未在本交付中验证**；以下为按项目现状整理的首次设置步骤。

---

## 1. 解压项目到固定位置

1. 解压 `priceradar-project-v1.zip`。
2. 建议放到固定路径，例如：`~/Projects/priceradar`（路径可含空格，但不建议过长）。
3. 确认根目录能看到：`README.md`、`requirements.txt`、`scripts/p4d_run_product.py`、`config/products/b913a.json`。

## 2. 在 WorkBuddy 中选定工作空间

1. 打开 WorkBuddy。
2. 将**项目根目录**设为当前工作空间 / 工作目录（以 WorkBuddy 界面为准）。
3. 不要把 Skill 安装目录当成项目数据目录；Profile 与 `artifacts/` 必须留在项目运行目录。

## 3. 导入并启用 PriceRadar Skill

1. 使用 `priceradar-workbuddy-skill-v1.zip`。
2. 在 WorkBuddy：**专家·技能·连接器** → **技能** → **添加技能** → **上传技能**。
3. 上传 ZIP；确认顶层为 `priceradar/SKILL.md` 结构。
4. 在「已安装」中**启用**该技能。

> 包结构已按开放平台文档准备；**WorkBuddy 导入待你在本机验收**（本交付未替你完成真实导入）。

可粘贴：

> 请使用PriceRadar技能检查当前项目的Mac运行环境。先完成只读检查，列出缺失依赖和具体设置步骤；环境就绪后引导我在独立Chrome窗口登录京东。

## 4. 检测系统、芯片架构与 Python

在终端进入项目根目录后执行（先用系统里可用的 `python3`）：

```bash
cd /path/to/priceradar
uname -m
python3 --version
which python3
```

解读：

- `arm64`：Apple Silicon
- `x86_64`：Intel

若没有 `python3`：从 [python.org](https://www.python.org/downloads/) 安装官方 macOS 安装包（**不要**默认假设已安装 Homebrew；也**不要**用 `sudo pip` 改系统 Python）。

建议使用 **Python 3.12.x**（与 Windows 验证环境接近）。若仅有 3.11/3.13，需自行确认 `onnxruntime` / `rapidocr` 轮子是否可用；**未在本交付中验证**。

## 5. 创建项目自己的 `.venv`

```bash
cd /path/to/priceradar
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
```

确认解释器：

```bash
.venv/bin/python -c "import sys; print(sys.executable); print(sys.version)"
```

## 6. 在该 `.venv` 内安装依赖

以仓库锁定文件为准（勿静默升级 Windows 已验证组合）：

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -r requirements-ocr.txt
```

当前锁定：

- `playwright==1.64.0`
- `rapidocr==3.10.0`
- `onnxruntime==1.31.0`
- `Pillow==13.0.0`

**兼容性说明（未在 Mac 真机验证）**

- `onnxruntime` / `rapidocr` 通常提供 macOS 轮子，但具体是否覆盖你的 Python 小版本 + `arm64`/`x86_64` 需安装时确认。
- 若 `pip install` 失败：记录完整报错；不要擅自换库版本除非你知情并授权。
- **不要**运行 `playwright install` 去下载捆绑 Chromium；本项目使用系统 Google Chrome（`channel="chrome"`）。

只读环境检查（不访问京东、不启动浏览器）：

```bash
.venv/bin/python scripts/priceradar_env_check.py --project-root .
```

若 `ready_for_live_check` 为 false，按输出的 `next_steps` 处理。

## 7. 安装 / 检查 Google Chrome

1. 若未安装：打开 [Google Chrome 官方下载页](https://www.google.com/chrome/) 安装。
2. 常见路径：`/Applications/Google Chrome.app`
3. 再次运行 `priceradar_env_check.py`，确认 `chrome_found: true`。

## 8. 首次登录（独立 Profile，可见窗口）

**必须在本机亲自登录**；不要在聊天里发送密码或 Cookie。

```bash
cd /path/to/priceradar
.venv/bin/python scripts/p1b_jd_login.py
```

实际 CLI（经项目核实）：

- 交互选择：`1` 首次登录验证，或 `2` 重启复用验证。
- 脚本会打开**可见** Chrome，使用项目内 `browser_profiles/jd_persistent/`。
- 按终端提示在浏览器中完成登录，再回到终端回答非敏感核验问题（不要输入真实昵称/手机号/密码）。
- **没有**已核实的「完全非交互、免人工确认」登录开关；请勿使用臆造参数。

建议顺序：先跑一次模式 `1`，关闭后再跑一次模式 `2` 确认复用。

同一 Profile **不要**并发跑多个自动化任务。

## 9. 运行一次 B913A 检查并打开报告

环境就绪且已登录后：

```bash
cd /path/to/priceradar
.venv/bin/python scripts/p4d_run_product.py --product-config config/products/b913a.json
```

或在 WorkBuddy 粘贴：

> 使用PriceRadar检查已登记的B913A，执行一次实时检查，输出比价表和本次报告路径。证据不足时标记待核验。

关注终端中的：

- `PRICERADAR_RUN_RESULT=.../run_result.json`（**本次**精确结果，勿猜「最新目录」）
- `HTML 报告` 绝对路径
- `verdict`：`BREAK` / `NOT_BREAK` / `REVIEW_REQUIRED`

用浏览器打开报告 HTML。退出码 `0` **不等于**已获得可靠可比价。

---

## 能力边界（第一版）

**承诺**：已登记商品的单次检查（当前仅 B913A 有 Windows 真实验收）。

**不承诺 / 未实现**：自动搜索、批量、多店最低价、新商品自动登记、定时通知；不宣称 macOS 或 WorkBuddy 已跑通。
