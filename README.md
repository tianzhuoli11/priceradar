# PriceRadar

电商商品破价监控（开发中）。

## WorkBuddy / 可迁移交付（v1）

本仓库可生成两套 ZIP（见 `dist/`，由 `scripts/fixtures/build_workbuddy_delivery_v1.py` 按允许清单打包）：

- `priceradar-workbuddy-skill-v1.zip` → WorkBuddy「上传技能」
- `priceradar-project-v1.zip` → 解压为项目（另建本机 `.venv` 与 Profile）

说明见 `dist/交付说明.md` 与 `docs/macos_first_setup.md`。  
安装 Skill **不会**安装 Python。第一版只承诺已登记商品单次检查（当前真实验证商品 B913A，Windows）；**不**宣称 macOS / WorkBuddy 实机已通过。禁止把历史验收价当作实时兜底。

当前阶段：

- **P1-A**：可见 Chrome 最小真实验证（已由用户真实验收通过）
- **P1-B**：京东手动登录与持久化登录态复用（已由用户真实验收通过）
- **P2-A**：单商品页面访问与人工核验（已由用户真实验收通过）
- **P2-B**：单商品页面展示价读取（inspect：自定义字体不可直接解析；DOM 无普通数字价）
- **P2-C**：商品页局部截图诊断基线（含人工框选；保留不覆盖）
- **P2-D**：全自动商品区域截图（价格区强制视口居中并避开固定底栏；已配合 P4-D 真实验收）
- **P3-A**：单商品关键词自动搜索与候选链接收集（**搜索受限，未通过**；本轮不宣称搜索可用）
- **P4-A**：本地 OCR 离线识图验证（无图片模型 API）
- **P4-B**：OCR 候选 → 主售价选择 / 单位分离 / 破价比较与输出表
- **P4-C**：单次截图任务 → OCR → 比价表一键流程（不启动浏览器）
- **P4-D**：已知商品链接 → 自动截图 → OCR → 比价（**单商品真实成功验收**：`run_id=20261019T052017Z_caa0fa`；`consistent=true`、`packaging_match=PASSED`、`box_comparable=true`、`normalized_price_cny_per_box=168.00`、`price_delta=41.00`、`comparison_status=EVALUATED`、`verdict=NOT_BREAK`；报告/历史 OK，退出码 0）
- **P4-E**：单商品中文 HTML 报告与运行历史（离线，不重跑 OCR）
- **P4-F**：单商品一键启动（`run_b913a.cmd`；启动器提示为 ASCII，中文业务结果由 Python 输出）

## 共同约定

- 使用本机已安装的 Google Chrome（Playwright `channel="chrome"`，`headless=False`）。
- 独立持久化 Profile：`browser_profiles/jd_persistent/`（两次运行必须使用同一路径）。
- 直接调用 `.venv\Scripts\python.exe`，无需 `Activate`，无需改 PowerShell 执行策略。
- 使用系统 Chrome，**不要**运行 `playwright install`，也不下载 Chromium。
- `.venv/` 与 `browser_profiles/` 不跨电脑复制。

## Windows（已验证环境路径）

项目根目录：

```powershell
cd C:\Users\tianz\Desktop\jasmineskill\priceradar
```

创建虚拟环境（仅首次；使用已确认的 Python 3.12，不要用默认 `py`）：

```powershell
& "C:\Users\tianz\AppData\Local\Programs\Python\Python312\python.exe" -m venv .venv
```

安装依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### P1-A：可见 Chrome

```powershell
.\.venv\Scripts\python.exe .\scripts\p1a_open_chrome.py
```

打开 `https://example.com`，确认可见窗口后，在终端按回车关闭。

### P1-B：京东手动登录与复用

**运行前**：结束任何仍占用同一 Profile 的会话（包括 P1-A），避免同时占用。

**第一次（首次登录验证）**：

```powershell
.\.venv\Scripts\python.exe .\scripts\p1b_jd_login.py
```

1. 选择 `1) 首次登录验证`。
2. 在可见 Chrome 中自行完成账号登录 / 验证（验证入口为「我的京东」页）。
3. 寻找**明确**账户昵称、账号信息或已登录账户入口。
4. 回到终端按提示回答非敏感核验问题（不要输入真实昵称、手机号或密码）。
5. 再按回车正常关闭本次浏览器。

**第二次（重启复用验证）**：确认第一次已正常关闭后，再次执行**同一命令**：

```powershell
.\.venv\Scripts\python.exe .\scripts\p1b_jd_login.py
```

1. 选择 `2) 重启复用验证`。
2. 不要重新输入密码、扫码、短信验证或其他登录动作。
3. 核验是否仍能看到明确登录证据。
4. 回答终端问题后按回车关闭。

**判定说明**：

- 两次必须使用相同 Profile：`browser_profiles/jd_persistent/`。
- 第二次若发生重新认证，**不能算复用通过**（即使最终已登录）。
- 本次通过只证明**这一次重启**可复用，不代表会话永不过期。
- 登录证据以人工确认为准；脚本不宣称自动登录检测。
- 脚本环境下 `https://www.jd.com/` 异常原因未确认；登录复用验证使用 `https://home.jd.com/index.html`。

### P2-A：单商品页面访问与人工核验

**运行前**：结束占用同一 Profile 的其他会话；确保该 Profile 已具备可用登录态。

```powershell
.\.venv\Scripts\python.exe .\scripts\p2a_jd_product_check.py
```

1. 脚本使用同一独立 Profile 启动可见 Chrome，并导航到内置短链接（允许跳转）。
2. 本轮**不**先访问京东首页，不做价格自动读取或破价判定。
3. 在窗口中人工核验：是否目标详情页、型号 B913A、规格「单层200抽×20包」、20包是否为一箱、店铺、价格金额与标签、优惠条件。
4. 可手动切换规格；脚本不自动点击。
5. 观察完成后回到终端按回车关闭。

若短链接失败、停在中转页、要求打开 App、出现登录/验证或异常页：保留窗口观察；可从日常正常浏览器复制商品详情页链接后再议，**不要猜测**京东数字商品 ID。

红线价 127 元／箱是业务基准，不是本轮采集结果。

### P2-B：展示价读取（先 inspect，再 read）

固定详情页：`https://item.m.jd.com/product/100005149674.html`

**当前状态**：本地尚无可核实的价格 DOM 证据，**仅提供 inspect 模式**。  
标记：**等待 DOM 证据，尚未实现可靠价格读取**。不会用猜测选择器宣称采价成功。

```powershell
.\.venv\Scripts\python.exe .\scripts\p2b_jd_price_read.py --mode inspect
```

1. 使用同一独立 Profile 打开上述详情页。
2. 在开发者工具中选中主展示价元素，复制该节点（及必要时最小父容器）的简短 `outerHTML`。
3. 把片段反馈给开发者；不要整页 HTML，不要 Cookie / 账户信息。
4. 回到终端按回车关闭。

拿到真实 DOM 证据后，才会实现 `--mode read`（定位主价格、Decimal 解析、人工核对）。  
上次人工看到的 168 元／箱仅作历史参考，不是抓取常量或成功标准。

### P2-C：局部截图（AI 识价输入准备）

固定详情页：`https://item.m.jd.com/product/100005149674.html`

本轮保存本机截图与元数据，**不调用 AI、不做 OCR、不输出自动采价成功**。

```powershell
.\.venv\Scripts\python.exe .\scripts\p2c_jd_capture.py
```

输出目录（每次唯一，不覆盖）：

`artifacts/price_captures/<UTC时间戳_商品ID_唯一后缀>/`

典型文件：

- `price.png`：自动截取 `#main_price` 内容并集（CSS 视口坐标 + 少量留白；完整性仍待你核验）
- `product_context.png`（或拆分的 `product_title.png` + `selected_spec.png`）：**人工辅助框选**（`manual_assisted`）
- `metadata.json`：导航状态、去参链接、各图边界/UTC 时间与尺寸、`ai_status=NOT_STARTED`、`recognized_price=null`、破价 `UNKNOWN`

人工框选提示：价格已自动截图后，请框选商品标题（含 B913A）到「已选」规格，下边界停在「送至」之前；不要再次只框价格。可先滚动再输入 `y` 启用覆盖层；可选拆成标题/规格两张图；可选取消重选。

运行后请打开本地图片核验：数字是否完整清晰、商品信息是否含标题与规格、是否含收货/账号等敏感信息。确认截图可用后，再另开任务接入 AI 识价。

### P2-D：全自动区域截图

固定开发样本：`https://item.m.jd.com/product/100005149674.html`

正常流程**无**手动框选、无复制 DOM、无预先滚动指令。截图在“按回车关闭”之前自动完成。

价格区：定位 `#main_price` 后**始终**滚到视口中部，检测固定顶/底栏遮挡并有界微调，再截取带余量的周边矩形；`save_status` 与 `content_quality_status` 分开记录。本地固定底栏夹具：`--self-check`。

```powershell
.\.venv\Scripts\python.exe .\scripts\p2d_jd_auto_capture.py
.\.venv\Scripts\python.exe .\scripts\p2d_jd_auto_capture.py --self-check
```

输出：`artifacts/price_captures/<UTC时间戳_商品ID_唯一后缀>/`（不覆盖历史目录）

可能文件：`price.png`、`product_title.png`、`selected_spec.png`、`promotions.png`、`shop.png`、`metadata.json`。

定位：`#main_price` 有 DOM 证据；标题/规格/优惠/店铺用页面语义文本线索自动发现（如「已选」「优惠」、型号线索），匹配不唯一或不安全（含「送至」等）则 `AMBIGUOUS` / `UNSAFE_REGION` / `NOT_FOUND`，不截图该区域。

仅有价格图成功时整体为 `PARTIAL`。截图完整性仍待验收；P3 起先做搜索候选，不强行宣称截图已全部通过。

### P3-A：关键词搜索与候选收集

```powershell
.\.venv\Scripts\python.exe .\scripts\p3a_jd_search.py --query "清风 B913A" --limit 5
```

- 使用独立 Profile 打开公开文档常见的 `search.jd.com/Search?keyword=...&enc=utf-8`（**入口与结果结构仍待本机真实验证**）。
- 从首个结果范围内自动发现商品详情锚点，最多 5 条去重候选；不翻页刷量。
- `B913A` 只作搜索关键词，不当作京东数字商品 ID；不用固定商品链接兜底。
- 输出：`artifacts/search_runs/<UTC时间戳_唯一后缀>/` 下的 `candidates.json`、`candidates.csv`、`run_metadata.json`。
- 本轮不读搜索页价格、不调 AI、不做比价表。
- 若页面可见「访问频繁导致无法搜索」等限制提示，状态为 `SEARCH_RESTRICTED`：立即停止滚动/提取，不刷新、不重试、不换入口；不把无锚点一律判为受限。

### P4-A：本地 OCR（RapidOCR + ONNX Runtime）

不启动浏览器。读取已有截图，本地 OCR 后输出结构化候选。

安装 OCR 依赖（项目 `.venv`，与浏览器依赖分离）：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-ocr.txt
```

单图：

```powershell
.\.venv\Scripts\python.exe .\scripts\p4a_local_ocr.py --image "artifacts\price_captures\<run>\product_context.png"
```

一次截图目录（只处理该目录内明确截图文件）：

```powershell
.\.venv\Scripts\python.exe .\scripts\p4a_local_ocr.py --run-dir "artifacts\price_captures\<run>"
```

输出：`artifacts/ocr_runs/<UTC时间戳_唯一后缀>/`（`ocr_raw.json`、`extraction.json`、`run_metadata.json`、预处理图）。

说明：首次推理可能加载/下载 rapidocr 自带模型；模型就绪后为本地推理。不做破价判定；历史观察价禁止用于纠偏。

### P4-B：主售价选择与破价表

从 P4-A 的 `extraction.json` 继续（不回头改截图/搜索状态）：

```powershell
.\.venv\Scripts\python.exe .\scripts\p4b_compare_from_ocr.py --extraction "artifacts\ocr_runs\<run>\extraction.json" --screenshot "artifacts\price_captures\<run>\product_context.png"
```

规则摘要：
- 主售价：排除已分类的优惠门槛；结合币种/标签/位置；歧义或裁切风险 → 待核验且不强行填金额。
- 规格：OCR「包数」与业务「20包／箱」分列；核验匹配前不做按箱比较。
- 比价：`Decimal`；低于 127 为破价，等于不算。
- 输出：`artifacts/compare_runs/<UTC_唯一后缀>/` 下的 `compare_table.csv` / `.json` / `compare_detail.json`。

### P4-C：截图任务一键到比价表

对**单次** `price_captures` 任务目录，重新 OCR 并产出统一比价表（不读历史 OCR 兜底，不启浏览器）：

```powershell
.\.venv\Scripts\python.exe .\scripts\p4c_process_capture_run.py --run-dir "artifacts\price_captures\<单次任务目录>"
```

输出：`artifacts/pipeline_runs/<UTC_唯一后缀>/`（`compare_table.csv` / `.json`、`pipeline_detail.json`、`ocr/`）。

说明：`expected_product` 仅为业务配置；裁切小图可排除后继续；主价格冲突或证据不足 → `REVIEW_REQUIRED` 且金额为空。不宣称「只输入 SKU 的全自动监控」。

### P4-F：双击一键启动（B913A）

项目根目录双击 `run_b913a.cmd` 即可（不依赖当前工作目录；不需 Activate）。

- 运行前关闭占用 `browser_profiles\jd_persistent` 的其他会话。
- 程序将自动：打开已知详情链接 → 截图 → 关闭浏览器 → OCR → 生成表格与中文报告。
- 结束后终端暂停，便于查看日志；暂停仅属于启动器，不改变 Python 无人值守流程。
- 启动器 `echo` 使用 **ASCII 英文**（避免 cmd 按系统 ANSI 误解析 UTF-8 中文，把提示碎片当成命令）。中文业务摘要仍由 Python 打印。
- **REVIEW_REQUIRED / 待核验**（见 Python 输出）表示本次证据不足，不等于未破价，也不等于价格采集成功。
- 请复制终端中的「HTML 报告」路径打开；启动器不自动开浏览器。

启动器自检（隔离 mock，不访问京东、不写真实历史）：

```powershell
$env:PRICERADAR_SELFTEST_NOPAUSE=1
cmd /c "scripts\fixtures\run_launcher_selftest.cmd 0"
cmd /c "scripts\fixtures\run_launcher_selftest.cmd 3"
```

原 PowerShell 命令仍可使用（见下节 P4-D）。

### P4-D：已知链接单商品自动流程

配置（无凭据）：`config/products/b913a.json`

```powershell
.\.venv\Scripts\python.exe .\scripts\p4d_run_product.py --product-config .\config\products\b913a.json
```

流程：读配置 → 打开已知详情 URL → P2-D 自动截图并关闭浏览器 → P4-C OCR/比价 → `artifacts/end_to_end_runs/<UTC_唯一后缀>/`。

**真实验收（单商品已知链接，2026-10-19）**：`artifacts/end_to_end_runs/20261019T052017Z_caa0fa/` — 核心规格一致、包装匹配、可按箱比较，结论 `NOT_BREAK`（页价换算每箱 168.00，相对红线差额 41.00）；报告与历史写入成功。这仍是「已知链接」闭环，**不是**搜索闭环。

离线格式检查（不启浏览器、不冒充新采集）：

```powershell
.\.venv\Scripts\python.exe .\scripts\p4d_run_product.py --product-config .\config\products\b913a.json --offline-capture-dir "artifacts\price_captures\<已有任务目录>"
```

说明：这是「已知链接单商品自动流程」，**不是**「输入 SKU 自动搜索」。**P3-A 搜索受限仍未通过**，不得用本轮比价成功反推搜索可用。

### P4-E：中文报告与单商品历史

对**已有**端到端结果目录离线生成 `report.html` 并更新历史（不启浏览器、不重新 OCR）：

```powershell
.\.venv\Scripts\python.exe .\scripts\p4e_report_history.py --run-dir "artifacts\end_to_end_runs\<明确的run目录>"
```

P4-D 一键运行结束后会自动调用本模块。报告写在该次 `end_to_end_runs/<run_id>/report.html`；历史在 `artifacts/product_history/<jd_product_id>/`（`history.json` / `history.csv`）。

如何理解：**流程完成**只表示流水线跑完；若 `REVIEW_REQUIRED` 且金额为空，是「待核验」，**不是**价格采集成功，也**不是**未破价。

## macOS（后续自行建立，尚未实测）

在 Mac 上另建环境与 Profile，**不要**从 Windows 复制 `.venv` 或 `browser_profiles/`。每台电脑分别手动登录。

```bash
cd /path/to/priceradar
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/p1a_open_chrome.py
.venv/bin/python scripts/p1b_jd_login.py
.venv/bin/python scripts/p2a_jd_product_check.py
.venv/bin/python scripts/p2b_jd_price_read.py --mode inspect
.venv/bin/python scripts/p2c_jd_capture.py
.venv/bin/python scripts/p2d_jd_auto_capture.py
.venv/bin/python scripts/p3a_jd_search.py --query "清风 B913A" --limit 5
```

前提：本机已安装 Google Chrome。上述命令在 macOS 上**尚未实测**。

## 跨电脑原则

- 只迁移代码与 `requirements.txt`（以及将来不含真实凭据的 `.env.example`）。
- 每台电脑单独创建 `.venv`，单独建立 Profile，并在该机手动完成登录。
- `.venv/` 与 `browser_profiles/` 已列入 `.gitignore`。

## 当前明确不做

只输入 SKU 的全自动监控、自动搜索闭环、AI API 调用、Excel、购物车/领券/结算、自动填写登录凭据、Cookie/Token 导出。
