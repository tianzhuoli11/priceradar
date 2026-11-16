# 环境与首次设置

## 定位项目根目录

同时存在以下文件即为 PriceRadar 项目根（解压后的 `priceradar/` 文件夹）：

- `README.md`
- `requirements.txt`
- `scripts/p4d_run_product.py`
- `config/products/b913a.json`

顺序：当前 WorkBuddy 工作空间 → 用户给出的路径 → 询问用户。不要全盘扫描，不要猜用户名。
路径可能含空格，命令里要加引号，或先 `cd` 进项目根再用相对路径。

## 解释器

| 系统 | 项目 venv Python |
| --- | --- |
| macOS | `.venv/bin/python` |
| Windows | `.venv\Scripts\python.exe` |

始终用项目 `.venv` 的解释器。不要 `sudo pip`，不要改系统 Python。

## 只读环境自检

```bash
.venv/bin/python scripts/priceradar_env_check.py --project-root .
```

- `.venv` 还没建时可先用 `python3 scripts/priceradar_env_check.py --project-root .`（脚本只用标准库）。
- 可加 `--json-out artifacts/env_check.json`，stdout 会出现 `PRICERADAR_ENV_CHECK=<路径>`。
- 检查：系统/芯片、Python、`.venv`、playwright / rapidocr / onnxruntime / Pillow 能否导入、Chrome 是否在 `/Applications/Google Chrome.app`、`artifacts/` 可写。
- 不检查 openpyxl（只有批量功能需要），也不检查 BrowserSkill（只有进阶实时搜索需要）。
- 不访问京东、不启动浏览器。

关键判断：`ready_for_live_check: True` 才能跑默认检查。

## 依赖安装（需用户同意）

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -r requirements-ocr.txt
```

- 版本已锁定；不要擅自升级或换库。
- `onnxruntime==1.31.0` 在 macOS 只有 **Apple 芯片（arm64）+ macOS 14 及以上** 的安装包，Python 需 3.11–3.14（建议 3.12）。Intel Mac 或更旧系统会安装失败 → 如实告诉用户，不要自行换版本。
- 不运行 `playwright install`；本项目用系统 Google Chrome（`channel="chrome"`）。

## 首次登录（用户亲自操作）

```bash
.venv/bin/python scripts/p1b_jd_login.py
```

- 交互选择 `1`（首次登录）或 `2`（重启后复用验证）。
- 打开可见 Chrome，使用项目内 `browser_profiles/jd_persistent/`。用户在窗口里自己登录京东，再回终端回答非敏感问题、按回车关闭。
- 这一步需要终端交互输入；如果 WorkBuddy 内无法交互，请用户在 macOS「终端」里自己运行。
- 不要在聊天里索要账号、密码、验证码、Cookie。
