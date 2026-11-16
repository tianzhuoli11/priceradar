# PriceRadar v2.1 · Windows 最短上手步骤

需要的文件（放在同一个文件夹，例如「下载」）：

| 文件 | SHA256 |
| --- | --- |
| `priceradar-project-v2.1-windows.zip` | `ea33ade4aa1af89077a855d64413c0b09ce98ad5f5bbfb0b59def7cd10a2a69f` |
| `priceradar-workbuddy-skill-v2-windows.zip`（技能，内容未变） | `44a399b8fb571daa9a295e0bafa2dcb1a328eb710ad00939f909f34644b95db0` |

以下命令都在 **PowerShell** 里执行。详细说明见项目内 `docs\windows_setup.md`。

## 1. 校验并解压

```powershell
Set-Location "$env:USERPROFILE\Downloads"
Get-FileHash .\priceradar-project-v2.1-windows.zip -Algorithm SHA256
Expand-Archive .\priceradar-project-v2.1-windows.zip -DestinationPath C:\PriceRadar21
Set-Location C:\PriceRadar21\priceradar
```

显示的哈希应与上表一致（大小写无所谓），不一致就停下，重新获取文件。
已有旧版项目文件夹的，**不要覆盖**，按上面解压到新文件夹即可。

## 2. 安装依赖并自检（不开浏览器、不访问京东）

需要已安装 Python 3.12（64 位）和 Google Chrome。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-ocr.txt
.\.venv\Scripts\python.exe .\scripts\priceradar_env_check.py --project-root .
```

- 最后一条显示 `ready_for_live_check: True` 才继续。
- 如果 `py -3.12` 报 `No suitable Python runtime found`，把第一条换成：
  `& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m venv .venv`

## 3. 手动登录京东，然后检查 B913A

```powershell
.\.venv\Scripts\python.exe .\scripts\p1b_jd_login.py
```

输入 `1` 回车，在弹出的独立 Chrome 窗口里**自己**登录京东，回到 PowerShell 按提示结束。账号、密码、验证码不要发给任何人或聊天窗口。

然后检查已登记的清风 B913A（商品 100005149674，红线 127 元/箱）：

```powershell
.\.venv\Scripts\python.exe .\scripts\p4d_run_product.py --product-config .\config\products\b913a.json
```

会打开一次 Chrome 截图后自动关闭。等它自己结束，不要同时再开第二次。

## 4. 找到第一份报告

在输出的最后几行找：

- `PRICERADAR_RUN_RESULT=…\run_result.json`
- `HTML 报告: …\report.html`

打开报告：`Start-Process "<把 report.html 的完整路径粘贴到这里>"`

| 结论 | 含义 |
| --- | --- |
| `NOT_BREAK` | 本次检查的这个链接不低于 127 元/箱 |
| `BREAK` | **疑似**破价，请自己打开商品页确认 |
| `REVIEW_REQUIRED` | 证据不足，需要人工看截图，**不代表**没有破价 |

## 5. WorkBuddy 技能（可选）

- **已经装过** `priceradar` 技能（v2 Windows 版）：不用重新安装，这次技能内容没有变化。
  只需把工作空间改成 `C:\PriceRadar21\priceradar`。
- 没装过：技能管理里「上传技能」，选 `priceradar-workbuddy-skill-v2-windows.zip`（**不要解压**），启用后把工作空间设为 `C:\PriceRadar21\priceradar`。

## 什么算「通过」

- 发包方电脑上的**离线预检**已经通过：在干净目录里解压、安装依赖、环境自检和离线自测都正常。
  这只说明安装包本身完整可用。
- **你的电脑首跑是否通过，要等你亲自做完第 1–4 步才能确认。**
  请回传三样东西：第 2 步最后一条的输出、第 4 步的结论那一行、`report.html` 的路径。
  不要发送账号、密码、Cookie 或登录截图。
