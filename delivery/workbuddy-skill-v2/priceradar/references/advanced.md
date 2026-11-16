# 进阶：实时搜索 / 批量 / 续跑（非默认）

只有用户**明确**要求“搜索京东上的 B913A 商品”“批量检查 Excel”“续跑某个任务”时才使用。执行前先向用户确认：会打开浏览器、访问京东搜索页多次，可能触发限流。

当前验证状态（如实告诉用户）：

- 单 SKU 搜索→详情核验→报告：仅在 Windows + BrowserSkill 小样本实测（20 个候选里核验 3 个）。
- BrowserSkill 翻页：仅实测第 1→2 页，不代表全量覆盖。
- 批量、暂停续跑、`--focus-window`：只做过离线验证；真实双 SKU 未验收。
- macOS 上以上均未实测。

## 单 SKU 实时搜索（p5b）

必须显式选择后端，程序不会自动切换：

```bash
# BrowserSkill（用用户日常 Chrome + 扩展；Windows 实测用的这个）
export BSK_EXE="$HOME/.local/bin/bsk"
.venv/bin/python scripts/p5b_run_sku.py --business-config config/products/b913a.json --backend bsk --max-candidates 20 --max-pages 2 --max-verify 3
```

- macOS 必须设置 `BSK_EXE`：程序默认找 `~/.local/bin/bsk.exe`（Windows 文件名）。
- 需要 BrowserSkill daemon 已在另一个终端窗口运行、扩展已连接，见安装说明“进阶”一节。若程序报 daemon 未运行，提示里出现的 `start_bsk_daemon.ps1` 是 Windows 脚本，Mac 不适用，按安装说明的 Mac 命令处理。
- 多个浏览器实例时加 `--bsk-browser <instance_id>`（由用户运行 `bsk --json browsers` 查到，不要用别人电脑上的 ID）。
- `--backend p2d` 走项目 Profile + Playwright，该组合的搜索近期曾 `SEARCH_RESTRICTED`，未在本版本实测。

结果定位：stdout 中 `=== PriceRadar P5-B job <job_id> ===`、`job_dir=<路径>`、`report=<路径>/report.html`。只用本次打印的 job_id，不挑最新目录。
退出码 3 = 会话暂停（登录/验证/限流）→ 停止，交用户处理。

报告措辞：“本次搜索范围内”。达到候选或核验上限 ≠ 全部商品；`NO_BREAK_IN_SCOPE` 只代表已核验的候选。

## OTHER 型号授权策略（可选，仅 B913A）

`config/policies/b913a_model_scope_include_other_20261027.json`：用户曾授权把 B913A 搜索结果中“其他型号但包数/层数/抽数一致”的商品也按 B913A 红线比较，最后人工判断。

- 只有显式加 `--model-scope-policy config/policies/b913a_model_scope_include_other_20261027.json` 才生效；默认不使用。
- 仅适用于 B913A；不得用于其他型号。

## 续跑

```bash
.venv/bin/python scripts/p5b_run_sku.py --resume-job-id <本次打印的 job_id> --max-verify 3
```

已完成项不重抓；普通失败需显式 `--retry-failed`。

## 批量（p5d，需 openpyxl）

```bash
.venv/bin/python scripts/p5d_run_batch.py --validate-only --excel templates/batch_sku_input_template.xlsx
.venv/bin/python scripts/p5d_run_batch.py --excel templates/batch_sku_input_template.xlsx --backend bsk --max-candidates 10 --max-pages 2 --max-verify 3 --max-verify-total 10
.venv/bin/python scripts/p5d_run_batch.py --excel templates/batch_sku_input_template.xlsx --resume <batch_id>
```

模板里只有已确认的 B913A。新增型号必须由用户填全规格与红线，不得默认继承 B913A。结果目录看 stdout 的 `batch_dir=`。

## 离线自检（不开浏览器、不访问京东）

```bash
.venv/bin/python scripts/p4d_run_product.py --self-test
.venv/bin/python scripts/p5b_run_sku.py --self-check
.venv/bin/python scripts/p5d_run_batch.py --self-check
```
