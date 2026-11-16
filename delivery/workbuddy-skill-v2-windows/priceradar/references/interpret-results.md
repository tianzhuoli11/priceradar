# 结果解释

## 结论（verdict）

| verdict | 怎么对用户说 |
| --- | --- |
| `NOT_BREAK` | 本次检查的这个链接、这个规格未低于红线（等于红线也算未破价） |
| `BREAK` | **疑似破价**：每箱价严格低于 127.00。请用户打开链接人工确认后再下结论 |
| `REVIEW_REQUIRED` | 证据不足，需人工核验。**不是**未破价，也**不是**采价成功 |
| 其他 / 空 | 未完成有效比价，说明失败阶段 |

只有同时满足才可以报每箱价和差额：

- `comparison_status = EVALUATED`
- `box_comparable = true`（型号、规格、包数核验通过）

否则每箱价、差额留空，写“待核验”。

## 中文结果表（建议列）

| 列 | 来源 |
| --- | --- |
| 型号 | `observed_model`（本次页面识别），不要用配置冒充 |
| 店铺 | `shop`；缺失写“未确认” |
| 规格 | 本次页面已选规格（见 `run_summary_path` / 报告）；`packaging_match` 表示包装核验结果 |
| 每箱价格 | `normalized_price_cny_per_box`（不可比时留空） |
| 红线 | `red_line_price`（127.00 元/箱，来自配置） |
| 差额 | `price_delta`（不可比时留空） |
| 结论 | `verdict` 按上表翻译；附 `review_reasons` |
| 采集时间 | `processed_at_utc` |

表后附：`report_html`、`compare_table_csv`、`capture_dir` 三个路径（均来自本次 run_result.json）。

## 必须人工处理

- 登录失效 / 验证码 / 访问受限 / Profile 被占用：停止，请用户本机处理（见 setup.md 首次登录）。
- 规格缺失、价格证据不清、疑似破价：给出报告路径和截图目录，请用户人工确认。
- 不自动换后端（例如改用 BrowserSkill），不自动重试。

## 历史数据

Windows 曾于 2026-10-19 / 10-24 观察到该链接页价换算 168.00 元/箱、`NOT_BREAK`。这只是历史记录，**不是**预期价格，禁止当作本次结果或兜底。
