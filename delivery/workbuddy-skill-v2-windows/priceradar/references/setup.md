# 环境与首次设置（Windows / PowerShell）

## 定位项目根目录

同时存在以下文件即为 PriceRadar 项目根：

- `README.md`
- `requirements.txt`
- `scripts\p4d_run_product.py`
- `config\products\b913a.json`

顺序：当前 WorkBuddy 工作空间 → 用户给出的路径 → 询问用户。不要全盘扫描，不要猜用户名。
已有开发目录（含 `.git`、`artifacts\`）和解压的交付包都可以，只要上述四个文件在。

```powershell
Set-Location "<项目根>"
```

## 解释器

始终用项目内 `.\.venv\Scripts\python.exe`，直接调用，**不需要** `Activate.ps1`，不需要改执行策略。
不要用系统全局 Python 跑检查；不要用默认 `py`（可能不是 3.12），新建环境用 `py -3.12`。

## 只读环境自检

```powershell
.\.venv\Scripts\python.exe .\scripts\priceradar_env_check.py --project-root .
```

- 可加 `--json-out .\artifacts\env_check.json`，stdout 会出现 `PRICERADAR_ENV_CHECK=<路径>`。
- 检查：系统、Python、`.venv`、playwright / rapidocr / onnxruntime / Pillow 能否导入、Chrome（Program Files 或 LocalAppData 下的 `chrome.exe`）、`artifacts\` 可写。
- 不检查 openpyxl（只有批量需要），也不检查 BrowserSkill（只有进阶实时搜索需要）。
- 不访问京东、不启动浏览器。

关键判断：`ready_for_live_check: True` 才能跑默认检查。

## 依赖安装（需用户同意）

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-ocr.txt
```

- `py -3.12` 报 `No suitable Python runtime found` 时（3.12 已装但未登记到 py 启动器），改用 `& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m venv .venv`；仍找不到就请用户安装 Python 3.12，不要改用其他版本。
- 版本已锁定，不要擅自升级或换库。
- 不运行 `playwright install`；本项目用系统 Google Chrome（`channel="chrome"`）。
- 已有 `.venv` 且自检通过时，不要重建。

## 首次登录（用户亲自操作）

```powershell
.\.venv\Scripts\python.exe .\scripts\p1b_jd_login.py
```

- 交互选择 `1`（首次登录验证）或 `2`（重启复用验证）。
- 打开可见 Chrome，使用项目内 `browser_profiles\jd_persistent\`。用户在窗口里自己登录京东，再回终端回答非敏感问题、按回车关闭。
- 需要终端交互输入；如果 WorkBuddy 内无法交互，请用户在普通 PowerShell 窗口里自己运行。
- 运行前确认没有其他 PriceRadar 会话占用同一 Profile。
- 不要在聊天里索要账号、密码、验证码、Cookie。
