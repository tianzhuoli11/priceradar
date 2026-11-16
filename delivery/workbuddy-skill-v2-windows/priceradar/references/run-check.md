# 默认检查：已登记 B913A 单次检查（Windows）

## 前提

- 环境自检 `ready_for_live_check: True`
- 用户已用 `scripts\p1b_jd_login.py` 在本机登录过京东
- 没有其他程序正在使用 `browser_profiles\jd_persistent\`

## 命令（PowerShell，在项目根执行）

```powershell
.\.venv\Scripts\python.exe .\scripts\p4d_run_product.py --product-config .\config\products\b913a.json
```

- 后端：Playwright + 系统 Google Chrome + 项目内独立 Profile。**不需要** BrowserSkill。
- 打开 `config\products\b913a.json` 里登记的详情链接 → 自动截图 → 关闭浏览器 → 本地 OCR → 按 20 包=1 箱换算 → 与 127.00 比较 → 写报告。
- 等待同一进程结束（通常一两分钟）。不并发、不自动重试。
- 不要用 `run_b913a.cmd`（含 `pause`，给用户双击用）。

可选参数（仅用户要求时）：

- `--emit-result-json <路径>`：额外复制一份 run_result.json
- `--offline-capture-dir <已有截图目录>`：离线重算，不开浏览器，**不是**新的采价；截图目录必须在原位置（metadata 里是绝对路径，搬动后会被拒绝）

## 本次结果定位

在本次 stdout 中找唯一一行：

```text
PRICERADAR_RUN_RESULT=<项目根>\artifacts\end_to_end_runs\<run_id>\run_result.json
```

读取该 JSON：`run_id`、`result_dir`、`report_html`、`capture_dir`、`compare_table_csv`、`verdict`、`comparison_status`、`box_comparable`、`normalized_price_cny_per_box`、`price_delta`、`exit_code`。

stdout 里还会打印 `HTML 报告: <路径>`、`结果表: <路径>\compare_table.csv`、`截图目录: <路径>`。

**禁止**按修改时间去 `artifacts\end_to_end_runs\` 挑“最新”目录冒充本次结果。

## 只看历史报告

用户给出明确 run_id 或路径时，只读 `artifacts\end_to_end_runs\<run_id>\run_result.json` / `report.html`，不重新访问京东。没给就询问，不要自己挑。

## 退出码

- `0` 只表示流程跑完，不等于价格可靠；仍要看 `verdict`。
- 非 0：报告失败阶段和终端最后几行错误，交给用户处理，不自动重跑。
