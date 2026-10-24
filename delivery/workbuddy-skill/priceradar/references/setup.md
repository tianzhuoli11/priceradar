# 环境与首次设置

## 项目根目录判定

同时存在即可视为 PriceRadar 根目录：

- `README.md`
- `requirements.txt`
- `scripts/p4d_run_product.py`
- `config/products/b913a.json`

## 解释器

| 系统 | 项目 venv Python |
| --- | --- |
| Windows | `.venv/Scripts/python.exe` |
| macOS / Linux | `.venv/bin/python` |

始终用**项目 venv** 的解释器调用脚本，不要用系统全局 Python 跑检查（除非用户明确且依赖已装在该解释器中）。

## 只读环境检查

```text
<venv-python> scripts/priceradar_env_check.py --project-root <项目根>
```

可选：`--json-out <path>` 写出 JSON；stdout 可能含 `PRICERADAR_ENV_CHECK=...`。

检查内容：OS/CPU 架构、Python 路径与版本、venv 是否存在、playwright/rapidocr/onnxruntime/Pillow 能否导入、Chrome 是否存在、`artifacts/` 是否可写。  
**不**访问京东，**不**启动浏览器。

## 依赖安装（需用户授权）

```text
<venv-python> -m pip install -r requirements.txt
<venv-python> -m pip install -r requirements-ocr.txt
```

锁定版本见项目内上述文件。不要擅自升级。不要 `sudo pip`。不要 `playwright install`（本项目使用系统 Chrome `channel=chrome`）。

## 首次登录

```text
<venv-python> scripts/p1b_jd_login.py
```

交互输入 `1`（首次）或 `2`（复用）。用户在可见 Chrome 完成登录。Profile：`browser_profiles/jd_persistent/`（位于项目内）。

macOS 逐步说明见项目包内 `docs/macos_first_setup.md`。
