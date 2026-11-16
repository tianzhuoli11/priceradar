# PriceRadar（交付版 v2.0.0）

京东商品破价检查工具。默认只检查已登记的 **清风 B913A**（单层200抽×20包 = 1箱，红线 127.00 元/箱）。

- macOS 安装与首次使用：**`docs/macos_first_setup.md`**（先看这份）
- 配合 WorkBuddy 技能 `priceradar` 使用；技能只含说明，程序在本目录。

## 目录

```text
priceradar/
  README.md
  VERSION
  requirements.txt            # playwright, openpyxl（锁定版本）
  requirements-ocr.txt        # rapidocr, onnxruntime, Pillow（锁定版本）
  docs/macos_first_setup.md
  config/products/b913a.json                                   # 已登记商品 B913A
  config/policies/b913a_model_scope_include_other_20261027.json # 可选，仅 B913A，需显式传参
  templates/batch_sku_input_template.xlsx                      # 批量模板（只含 B913A）
  scripts/
    priceradar_env_check.py   # 只读环境自检
    p1b_jd_login.py           # 首次手动登录京东（项目内独立 Profile）
    p1a_open_chrome.py        # 可见 Chrome 最小验证
    p4d_run_product.py        # 默认：已登记 B913A 单次检查
    p5b_run_sku.py            # 进阶：单 SKU 实时搜索→核验→报告
    p5d_run_batch.py          # 进阶：Excel 批量 / 续跑
    ...                       # 其余为上述入口动态加载的模块
    fixtures/                 # 离线自检用 HTML / xlsx 夹具
```

运行时会在本目录生成 `.venv/`、`browser_profiles/`、`artifacts/`，这些不要复制到别的电脑。

## 最常用命令（macOS，在本目录执行）

```bash
.venv/bin/python scripts/priceradar_env_check.py --project-root .
.venv/bin/python scripts/p1b_jd_login.py
.venv/bin/python scripts/p4d_run_product.py --product-config config/products/b913a.json
```

## 验证状态（如实）

- 默认检查（p4d，已登记链接）：Windows 实测通过（最近一次 2026-10-24）；之后相关模块有更新，当前代码仅通过离线自检。
- 单 SKU 实时搜索（p5b + BrowserSkill）：Windows 小样本实测；翻页只测到第 2 页。
- 批量 / 续跑：仅离线验证；真实双 SKU 未验收。
- macOS、WorkBuddy 实际导入与运行：待接收方验证。

历史观察价（如 168.00 元/箱）不是预期价，不能当作实时结果。

## 明确不做

自动登录、领券、加购、结算、下单；导出 Cookie/Token；定时通知。
