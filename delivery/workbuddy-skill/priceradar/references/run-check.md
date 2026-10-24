# 执行一次实时检查

## 命令（与项目 CLI 一致）

```text
<venv-python> scripts/p4d_run_product.py --product-config config/products/b913a.json
```

可选：

- `--emit-result-json <path>`：额外复制一份 `run_result.json`
- `--offline-capture-dir <已有截图目录>`：离线重处理（不启浏览器；不冒充新采集）

**不要**通过 `run_b913a.cmd` 在 WorkBuddy 中自动执行（该文件含 `pause`，面向人工双击）。

## 等待与并发

- 等待同一 Python 进程结束。
- 同一 Profile 目录不要并行第二次检查。
- 失败后不要自动重试打开京东。

## 本次结果定位

stdout 中查找：

```text
PRICERADAR_RUN_RESULT=<绝对路径>/run_result.json
```

该文件字段包括：`run_id`、`result_dir`、`report_html`、`capture_dir`、`verdict`、`box_comparable`、`normalized_price_cny_per_box`、`price_delta`、`exit_code` 等。

**禁止**：按修改时间挑选 `artifacts/end_to_end_runs` 下“最新”目录当作本次结果。

## 仅看报告

用户提供明确路径时读取 `run_summary.json` / `report.html` / `run_result.json`；不调用 `p4d_run_product.py` 实时检查。
