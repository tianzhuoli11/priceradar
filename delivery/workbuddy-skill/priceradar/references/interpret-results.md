# 结果解释

## 结论字段

| verdict | 含义 |
| --- | --- |
| BREAK | 在可比价且证据充分时，每箱价**严格低于**红线 |
| NOT_BREAK | 可比价且未破价（含等于红线） |
| REVIEW_REQUIRED | 证据不足或核验未通过；**不是**未破价 |
| NOT_EVALUATED / 其他 | 未完成有效比价 |

补充：

- `comparison_status=EVALUATED` 才表示完成破价判定。
- `box_comparable=true` 且包装/型号核验通过后，才应采信每箱价与差额。
- **退出码 0 ≠ 可靠价格**。流程跑完仍可能 `REVIEW_REQUIRED`。

## 中文表（建议列）

- 型号（observed_model）
- 店铺（shop；缺失则标明未确认）
- 规格（来自 compare / run_summary，勿用业务配置冒充 observed）
- 每箱价格（normalized_price_cny_per_box；不可比时留空）
- 红线（red_line_price）
- 差额（price_delta；不可比时留空）
- 结论（verdict）
- 采集时间（processed_at_utc）

并给出：

- HTML 报告路径
- CSV 路径
- 截图目录路径

## 历史验收数据

Windows 曾验收：`run_id=20261019T052017Z_caa0fa`，页价换算 168.00／箱，红线 127.00，`NOT_BREAK`。  
这是**历史**记录，禁止回填为当前检查结果。

## 失败与受限

登录失效、验证码、访问限制、Profile 占用：停止任务，说明需用户本机处理；不杀 Chrome、不换号、不自动刷新重试。
