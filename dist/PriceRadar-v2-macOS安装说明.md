# PriceRadar v2 · macOS 安装与首次使用说明

照着从上往下做即可。终端命令可以整段复制粘贴。

你会收到两个 ZIP（文件名以实际收到的为准）：

| 文件 | 怎么用 |
| --- | --- |
| `priceradar-workbuddy-skill-v2.zip` | **不要解压**，直接在 WorkBuddy 里“上传技能” |
| `priceradar-project-v2.zip` | **解压**，得到 `priceradar` 文件夹，这是真正运行的程序 |

> 说明：本版本在 Windows 上做过实测和离线检查，**在 Mac 和 WorkBuddy 里还没有实际跑过**。遇到和本说明不一致的地方，请截图发回给我，不要自己改程序。

---

## 0. 先确认电脑条件

打开「终端」（启动台 → 其他 → 终端），粘贴：

```bash
uname -m; sw_vers -productVersion
```

- 第一行要是 `arm64`（Apple 芯片 M1/M2/M3/M4…）。如果是 `x86_64`（Intel Mac），OCR 组件装不上，先停下告诉我。
- 第二行要是 `14` 或更高（macOS Sonoma 及以后）。低于 14 也先停下告诉我。

## 1. 解压项目 ZIP

1. 在「访达」里新建文件夹：你的用户目录下的 `PriceRadar`（即 `~/PriceRadar`，路径里不要有空格更省事）。
2. 把 `priceradar-project-v2.zip` 放进去，双击解压，得到 `~/PriceRadar/priceradar/`。
3. 在终端确认：

```bash
cd ~/PriceRadar/priceradar
ls README.md requirements.txt scripts/p4d_run_product.py config/products/b913a.json
```

四个文件名都打印出来、没有 “No such file” 就对了。

## 2. 安装 Python 3.12

Mac 自带的 `python3` 往往版本太旧（3.9），**不能用**。

1. 打开 <https://www.python.org/downloads/macos/>，下载 **Python 3.12.x 的 macOS 64-bit universal2 installer**，双击安装。
2. 终端确认：

```bash
python3.12 --version
```

显示 `Python 3.12.x` 即可。

## 3. 安装 Google Chrome

如果还没有：从 <https://www.google.com/chrome/> 下载安装，拖进「应用程序」。路径应为 `/Applications/Google Chrome.app`。

## 4. 创建虚拟环境并安装依赖

```bash
cd ~/PriceRadar/priceradar
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -r requirements-ocr.txt
```

- 需要联网下载（PyPI），几分钟。
- 不要加 `sudo`，不要运行 `playwright install`（程序直接用你装好的 Chrome）。
- 如果报错，把**最后 20 行**截图发给我，不要自己改版本号。

## 5. 环境自检（不会打开浏览器，不访问京东）

```bash
cd ~/PriceRadar/priceradar
.venv/bin/python scripts/priceradar_env_check.py --project-root .
```

看最后几行：

- `ready_for_live_check: True` → 可以继续。
- `False` → 按下面 `next_steps:` 列出的缺项处理，处理完再跑一次。

## 6. 在 WorkBuddy 里导入技能、选工作空间

> 这一节的菜单名称**没有在真实 WorkBuddy 里核对过**，以你看到的界面为准。

1. **上传技能**：在 WorkBuddy 的技能管理处（上一版说明写的是「专家·技能·连接器 → 技能 → 添加技能 → 上传技能」）选择上传，选中 `priceradar-workbuddy-skill-v2.zip`（不用解压）。
   - 这个 ZIP 打开后第一层就是 `SKILL.md` 和 `references/` 文件夹。
   - 如果提示“找不到 SKILL.md / 结构不对 / 格式不支持”，截图发给我，我换一种打包结构。
2. 在「已安装」里**启用** `priceradar` 技能。
3. **选工作空间**：把 `~/PriceRadar/priceradar` 这个文件夹设为 WorkBuddy 的工作空间 / 工作目录（通常是“打开文件夹”或“选择工作空间”）。注意选的是**解压出来的 priceradar 文件夹**，不是 ZIP，也不是技能安装目录。

可以先发这句话测试技能能不能找到项目：

```text
请用 PriceRadar 技能检查当前项目的 Mac 运行环境，只做只读检查，告诉我缺什么。
```

## 7. 首次登录京东（必须你自己操作）

这一步要在终端里交互，建议直接在「终端」运行：

```bash
cd ~/PriceRadar/priceradar
.venv/bin/python scripts/p1b_jd_login.py
```

1. 输入 `1` 回车（首次登录）。
2. 会弹出一个新的 Chrome 窗口（独立的，不影响你平时的 Chrome）。在里面**自己登录京东**（扫码或账号都可以）。
3. 登录后回到终端，按提示回答几个问题（不要输入真实手机号、密码），回车关闭浏览器。
4. 再运行一次同一命令，这次输入 `2`，确认不用重新登录也能看到已登录状态。

账号、密码、验证码、Cookie **不要**发到 WorkBuddy 聊天里，也不要发给我。

## 8. 第一次检查 B913A

在 WorkBuddy 里输入：

```text
使用 PriceRadar 检查已登记的清风 B913A，执行一次检查，输出中文比价表和本次报告路径。证据不足或需要登录时直接告诉我，不要下确定结论。
```

会发生什么：

- 弹出一个 Chrome 窗口，自动打开 B913A 商品页、截图，然后自己关闭（期间不要操作这个窗口）。
- 一两分钟后（第一次识别文字可能更久），WorkBuddy 给出结果表和报告路径。

不用 WorkBuddy，也可以在终端直接跑同一检查：

```bash
cd ~/PriceRadar/priceradar
.venv/bin/python scripts/p4d_run_product.py --product-config config/products/b913a.json
```

### 怎么看报告

- 输出最后有一行 `PRICERADAR_RUN_RESULT=…/run_result.json`，还有 `HTML 报告: …/report.html`。
- 打开报告：`open "<把 HTML 报告后面的路径粘贴到这里>"`，或在访达里进入 `priceradar/artifacts/end_to_end_runs/<本次的 run_id>/` 双击 `report.html`。
- 结论含义：
  - `NOT_BREAK`：这次检查的链接没有低于 127 元/箱。
  - `BREAK`：**疑似**破价，请自己打开商品页确认后再处理。
  - `REVIEW_REQUIRED`：证据不足，需要人工看截图，**不代表**没破价。
- 以前看到过的价格（如 168 元/箱）只是历史，不代表这次应该是这个价。

---

## 常见问题

### A. WorkBuddy 说找不到项目

- 确认工作空间选的是 `~/PriceRadar/priceradar`（里面直接能看到 `README.md` 和 `scripts` 文件夹），不是外层 `PriceRadar`、不是 ZIP。
- 解压后如果出现 `priceradar/priceradar/` 两层，选**里面那层**。
- 也可以直接告诉 WorkBuddy：“项目在 ~/PriceRadar/priceradar”。

### B. 缺依赖 / 自检不是 True

| 现象 | 处理 |
| --- | --- |
| `python3.12: command not found` | 第 2 步没装好，重新安装 python.org 的 3.12 |
| `venv: exists=False` | 重新做第 4 步 |
| `dep playwright: {'ok': False…}` | `.venv/bin/python -m pip install -r requirements.txt` |
| `dep rapidocr / onnxruntime: {'ok': False…}` | `.venv/bin/python -m pip install -r requirements-ocr.txt`；若提示没有匹配版本，多半是 Intel 芯片或 macOS 低于 14，截图发我 |
| `chrome_found: False` | 第 3 步，把 Chrome 装到「应用程序」 |
| 第一次检查卡在识别文字 | OCR 首次可能要加载模型，等几分钟；仍失败把终端最后 20 行发我 |

### C. 登录失效 / 验证码 / 访问受限

- 结果里出现“需要登录 / LOGIN_REQUIRED / 验证 / 访问频繁 / REVIEW_REQUIRED（原因含登录）”：
  1. 关掉所有 PriceRadar 打开的 Chrome 窗口；
  2. 重新执行第 7 步（选 `1`）自己登录；
  3. 过一会儿再检查一次。不要连续反复跑。
- 提示 Profile 被占用 / Chrome 打不开：确认没有另一次检查正在跑，关掉 PriceRadar 打开的 Chrome 窗口后再试。
- 程序不会自动登录、领券、加购或下单。

---

## 进阶（非首次使用，需要时再看）

实时搜索京东上所有 B913A 商品、Excel 批量、续跑，需要另装 **BrowserSkill**（腾讯开源，用你日常 Chrome + 扩展）。这部分在 Windows 小样本测过，**Mac 上未测**，第一次使用不需要。

1. 安装命令行工具（官方脚本，装到 `~/.local/bin`）：

   ```bash
   curl -fsSL https://raw.githubusercontent.com/Tencent/BrowserSkill/main/install.sh | sh
   export PATH="$HOME/.local/bin:$PATH"
   bsk --version
   ```

2. 在日常 Chrome 里安装 BrowserSkill 扩展（Chrome 应用商店，或按 GitHub 说明加载），并按扩展提示完成配对。在这个日常 Chrome 里自己登录京东。
3. 另开一个终端窗口启动 daemon，并保持这个窗口不关：

   ```bash
   ~/.local/bin/bsk daemon start --foreground --daemon-idle 2h
   ```

   默认端口在 Mac 上一般可用；若提示端口被占用，可加 `--port 54100`（Windows 上就是这么处理的）。以上参数来自 Windows 用法，Mac 上待验证。
4. 回到原终端做连接检查（四条都要成功）：

   ```bash
   export BSK_AUTO_START=0
   ~/.local/bin/bsk --json status
   ~/.local/bin/bsk --json doctor
   ~/.local/bin/bsk --json browsers
   ```

   `browsers` 里应能看到你的 Chrome。多个实例时记下要用的 instance id。
5. 运行单 SKU 实时搜索（**必须**先设置 `BSK_EXE`，因为程序默认找的是 Windows 的 `bsk.exe`）：

   ```bash
   cd ~/PriceRadar/priceradar
   export BSK_EXE="$HOME/.local/bin/bsk"
   .venv/bin/python scripts/p5b_run_sku.py --business-config config/products/b913a.json --backend bsk --max-candidates 20 --max-pages 2 --max-verify 3
   ```

   输出里的 `job_dir=` 和 `report=` 就是本次结果。报告只代表“本次搜索范围内”。若提示运行 `start_bsk_daemon.ps1`，那是 Windows 脚本，Mac 上按第 3 步处理。

更多命令（续跑、批量、可选的 B913A OTHER 型号策略）见技能里的 `references/advanced.md`。

## 离线自检（可选，不开浏览器、不访问京东）

```bash
cd ~/PriceRadar/priceradar
.venv/bin/python scripts/p4d_run_product.py --self-test
.venv/bin/python scripts/p5b_run_sku.py --self-check
.venv/bin/python scripts/p5d_run_batch.py --self-check
```

最后分别看到 `self_test_failures=[]`、`OK: all offline checks passed` 类字样即通过。
