# 项目布局（交付范围摘要）

```text
priceradar/
  README.md
  requirements.txt
  requirements-ocr.txt
  run_b913a.cmd          # Windows 人工双击；WorkBuddy 不要自动调 pause
  docs/macos_first_setup.md
  config/products/b913a.json
  scripts/
    priceradar_env_check.py
    p1a_open_chrome.py
    p1b_jd_login.py
    p2d_jd_auto_capture.py
    p4a_local_ocr.py
    p4b_compare_from_ocr.py
    p4c_process_capture_run.py
    p4d_run_product.py      # 统一执行入口
    p4e_report_history.py
    fixtures/               # 本地自检 HTML 等
  browser_profiles/         # 运行时生成；勿跨机复制
  artifacts/                # 运行时生成；勿打包进交付
  .venv/                    # 本机创建；勿跨机复制
```

动态加载：`p4d` → `p2d`/`p4c`/`p4e`；`p4c` → `p4a`/`p4b`/`p2d`。打包时必须保留这些脚本，不能只留 `p4d`。
