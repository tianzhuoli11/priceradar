# PriceRadar v2 · Windows 安装与首次使用说明

两种情况，选一种看：

- **A. 已有项目的电脑**：电脑上已经有能跑的 PriceRadar 项目文件夹（有 `.venv`、登录过京东）。只需导入技能。
- **B. 全新电脑**：没有源码。需要解压项目包、装环境、登录。

本说明用到的文件：

| 文件 | 用途 | A 已有项目 | B 全新电脑 |
| --- | --- | --- | --- |
| `priceradar-workbuddy-skill-v2-windows.zip` | WorkBuddy “上传技能”，**不要解压** | 需要 | 需要 |
| `priceradar-project-v2-windows.zip` | 解压成项目文件夹 | 不需要 | 需要 |

> 说明：WorkBuddy 的实际导入**尚未验证**；技能 ZIP 第一层就是 `SKILL.md` 和 `references\`。如果导入提示结构不对，请截图反馈，不要自己改包。

下面所有命令都在 **PowerShell** 里执行（开始菜单搜 “PowerShell”）。直接调用 `.\.venv\Scripts\python.exe`，**不需要** Activate，也**不需要**改执行策略。

---

## A. 已有项目的电脑

### A1. 确认项目文件夹

找到你现有的项目根目录（下文写作 `<项目根>`），里面能直接看到这四个文件：

```powershell
Set-Location "<项目根>"
Test-Path README.md, requirements.txt, scripts\p4d_run_product.py, config\products\b913a.json
```

四行都是 `True` 即可。不需要解压项目 ZIP，也不要用项目 ZIP 覆盖现有目录（会丢掉你的改动；`.venv`、登录 Profile、历史结果保留在原目录）。

### A2. 环境自检（不开浏览器、不访问京东）

```powershell
Set-Location "<项目根>"
.\.venv\Scripts\python.exe .\scripts\priceradar_env_check.py --project-root .
```

看到 `ready_for_live_check: True` 即可。若为 `False`，按 `next_steps` 处理（多半是 `.\.venv\Scripts\python.exe -m pip install -r requirements-ocr.txt`）。

### A3. 导入技能、选工作空间

1. 在 WorkBuddy 的技能管理处选择“上传技能”（菜单名以界面为准），选 `priceradar-workbuddy-skill-v2-windows.zip`。
2. 若已装过旧版 `priceradar`（v1），先停用或删除旧版，或在导入时选择更新，避免两个同名技能。
3. 在「已安装」里启用 `priceradar`。
4. 把 `<项目根>` 设为 WorkBuddy 工作空间（选的是项目文件夹本身，里面直接有 `README.md` 和 `scripts`）。

### A4. 试跑

先在 WorkBuddy 里输入（只读）：

```text
请用 PriceRadar 技能检查当前项目的 Windows 运行环境，只做只读检查，告诉我缺什么。
```

再输入（会打开一次 Chrome）：

```text
使用 PriceRadar 检查已登记的清风 B913A，执行一次检查，输出中文比价表和本次报告路径。证据不足或需要登录时直接告诉我，不要下确定结论。
```

如果登录过期，按下面 B5 重新登录一次。看结果见 B7。

---

## B. 全新电脑

### B1. 解压项目

1. 建议放到**短路径、纯英文**的位置，例如 `C:\PriceRadar\`（含空格可以，但不要含中文，路径越短越好；Windows 路径太长会导致批量续跑写文件失败）。
2. 把 `priceradar-project-v2-windows.zip` 解压到 `C:\PriceRadar\`，得到 `C:\PriceRadar\priceradar\`。
3. 确认：

```powershell
Set-Location C:\PriceRadar\priceradar
Test-Path README.md, requirements.txt, scripts\p4d_run_product.py, config\products\b913a.json
```

四行都是 `True`。如果出现 `priceradar\priceradar\` 两层，用里面那层。

### B2. 安装 Python 3.12

1. 打开 <https://www.python.org/downloads/windows/>，下载 **Python 3.12.x Windows installer (64-bit)**。
2. 安装时勾选 “Add python.exe to PATH”，并保留 “py launcher”。
3. 确认：

```powershell
py -3.12 --version
```

显示 `Python 3.12.x`。（不要只用 `py` 或 `python`，它们可能指向别的版本。）

如果提示 `No suitable Python runtime found`（3.12 已装但 py 启动器没登记，实测会出现），改用安装目录里的解释器，并在下面 B4 中把 `py -3.12` 换成它：

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" --version
```

（这是 python.org“仅为当前用户安装”的默认位置；若装给所有用户，一般在 `C:\Program Files\Python312\python.exe`。）

### B3. 安装 Google Chrome

从 <https://www.google.com/chrome/> 安装。程序会在 `C:\Program Files\Google\Chrome\Application\chrome.exe` 或当前用户的 `AppData\Local\Google\Chrome\Application\chrome.exe` 找它。

### B4. 创建虚拟环境并安装依赖

```powershell
Set-Location C:\PriceRadar\priceradar
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-ocr.txt
.\.venv\Scripts\python.exe .\scripts\priceradar_env_check.py --project-root .
```

- 若 B2 里 `py -3.12` 不可用，第二行改为：`& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m venv .venv`，其余不变。
- 需要联网下载（PyPI），几分钟；OCR 模型随安装包自带。
- 不要运行 `playwright install`（程序用你装好的 Chrome）。
- 最后一条应显示 `ready_for_live_check: True`。

### B5. 首次登录京东（必须你自己操作）

在普通 PowerShell 窗口运行：

```powershell
Set-Location C:\PriceRadar\priceradar
.\.venv\Scripts\python.exe .\scripts\p1b_jd_login.py
```

1. 输入 `1` 回车（首次登录验证）。
2. 弹出一个独立的 Chrome 窗口（不影响你平时的 Chrome），在里面**自己登录京东**。
3. 回到 PowerShell 按提示回答（不要输入真实手机号、密码），回车关闭浏览器。
4. 再运行一次同一命令，输入 `2`（重启复用验证），确认不用重新登录。

账号、密码、验证码、Cookie 不要发到 WorkBuddy 聊天里。

### B6. 导入技能、选工作空间、试跑

同 A3、A4，工作空间选 `C:\PriceRadar\priceradar`。

### B7. 不用 WorkBuddy 的两种跑法 & 怎么看报告

PowerShell：

```powershell
Set-Location C:\PriceRadar\priceradar
.\.venv\Scripts\python.exe .\scripts\p4d_run_product.py --product-config .\config\products\b913a.json
```

或在资源管理器里双击项目根的 `run_b913a.cmd`（结束后窗口会暂停，按任意键关闭）。

结果：

- 输出最后有 `PRICERADAR_RUN_RESULT=…\run_result.json` 和 `HTML 报告: …\report.html`，把报告路径复制到浏览器地址栏打开，或：`Start-Process "<HTML 报告路径>"`。
- `NOT_BREAK`：本次检查的链接未低于 127 元/箱。
- `BREAK`：**疑似**破价，请自己打开商品页确认。
- `REVIEW_REQUIRED`：证据不足，需要人工看截图，**不代表**没破价。
- 以前看到过的价格（如 168 元/箱）只是历史，不是本次结果。

---

## 常见问题

### 找不到项目

- 工作空间要选项目根本身（直接能看到 `README.md`、`scripts`），不是上一层，也不是 ZIP。
- 也可以直接告诉 WorkBuddy 项目路径。

### 缺依赖 / 自检不是 True

| 现象 | 处理 |
| --- | --- |
| `py -3.12` 报 `No suitable Python runtime found` | 先试 B2 里的安装目录解释器；仍没有再重新安装 Python 3.12 |
| `venv: exists=False` | 做 B4 |
| `dep playwright: {'ok': False…}` | `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` |
| `dep rapidocr / onnxruntime: {'ok': False…}` | `.\.venv\Scripts\python.exe -m pip install -r requirements-ocr.txt` |
| `chrome_found: False` | 安装 Chrome（B3） |

### 登录失效 / 验证码 / 访问受限 / Profile 占用

1. 关掉 PriceRadar 打开的 Chrome 窗口，确认没有另一次检查在跑；
2. 重新做 B5（选 `1`）自己登录；
3. 过一会儿再检查一次，不要连续反复跑。

程序不会自动登录、领券、加购或下单。

---

## 进阶（非首次使用）

实时搜索京东上所有 B913A 商品、Excel 批量、续跑需要 **BrowserSkill**（用你日常 Chrome + 扩展）。首次使用不需要。

1. 安装 `bsk`（官方 PowerShell 安装脚本，装到 `%USERPROFILE%\.local\bin\bsk.exe`）：

   ```powershell
   irm https://raw.githubusercontent.com/Tencent/BrowserSkill/main/install.ps1 | iex
   ```

2. 日常 Chrome 安装 BrowserSkill 扩展并按提示配对，在日常 Chrome 里自己登录京东。
3. 另开一个 PowerShell 窗口启动 daemon 并保持不关（端口 54100，避开 Windows Hyper-V 保留的 52733–52832）：

   ```powershell
   Set-Location C:\PriceRadar\priceradar
   powershell -ExecutionPolicy Bypass -File .\scripts\fixtures\start_bsk_daemon.ps1
   ```

4. 回到原窗口检查连接：

   ```powershell
   $env:BSK_AUTO_START = '0'
   & "$env:USERPROFILE\.local\bin\bsk.exe" --json status
   & "$env:USERPROFILE\.local\bin\bsk.exe" --json browsers
   ```

5. 单 SKU 实时搜索：

   ```powershell
   .\.venv\Scripts\python.exe .\scripts\p5b_run_sku.py --business-config .\config\products\b913a.json --backend bsk --max-candidates 20 --max-pages 2 --max-verify 3
   ```

   输出的 `job_dir=`、`report=` 是本次结果；报告只代表“本次搜索范围内”。多个浏览器实例时加 `--bsk-browser <instance_id>`。

续跑、批量、可选的 B913A OTHER 型号策略见技能里的 `references\advanced.md`。

## 离线自检（可选，不开浏览器、不访问京东）

```powershell
.\.venv\Scripts\python.exe .\scripts\p4d_run_product.py --self-test
.\.venv\Scripts\python.exe .\scripts\p5b_run_sku.py --self-check
.\.venv\Scripts\python.exe .\scripts\p5d_run_batch.py --self-check
$env:PRICERADAR_SELFTEST_NOPAUSE = '1'; cmd /c "scripts\fixtures\run_launcher_selftest.cmd 0"
```

分别看到 `self_test_failures=[]`、`OK: all offline checks passed`、`[OK] exit code matched expected 0` 即通过。
