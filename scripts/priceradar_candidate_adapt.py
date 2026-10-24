"""
Phase A helpers: business baseline vs candidate identity, URL adapt, model boundary.

Not a batch orchestrator. Does not invent product IDs or fallback fixed links.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

RULES_VERSION_ADAPT = "p5a-candidate-adapt-v1"

ALLOWED_DETAIL_HOSTS = frozenset({"item.jd.com", "item.m.jd.com"})

RE_ITEM_JD = re.compile(
    r"(?:https?:)?//item\.jd\.com/(?P<id>\d+)\.html",
    re.IGNORECASE,
)
RE_ITEM_M_JD = re.compile(
    r"(?:https?:)?//item\.m\.jd\.com/product/(?P<id>\d+)\.html",
    re.IGNORECASE,
)


def model_boundary_regex(model: str) -> re.Pattern[str]:
    """Whole-token model match: B913A must not match inside B913AE."""
    m = (model or "").strip()
    if not m:
        raise ValueError("model 不能为空")
    # Escape so business models with special chars stay literal.
    esc = re.escape(m)
    return re.compile(rf"(?<![A-Za-z0-9]){esc}(?![A-Za-z0-9])", re.IGNORECASE)


def title_contains_exact_model(title: str, model: str) -> bool:
    try:
        return bool(model_boundary_regex(model).search(title or ""))
    except ValueError:
        return False


def title_model_gate(
    title: str,
    *,
    expected_model: str,
) -> dict[str, Any]:
    """
    Coarse title screen before opening detail.

    If title clearly shows a longer conflicting model (expected is a proper prefix
    of another alphanumeric token, e.g. B913A vs B913AE), skip browse.
    """
    title_u = title or ""
    expected = (expected_model or "").strip()
    if not expected:
        return {
            "status": "REVIEW_REQUIRED",
            "reason": "业务 expected_model 为空",
            "skip_browse": False,
        }
    if title_contains_exact_model(title_u, expected):
        return {
            "status": "TITLE_MODEL_OK",
            "reason": "标题边界命中预期型号",
            "skip_browse": False,
        }
    # Conflicting longer token: expected + extra alnum (B913AE when expected B913A).
    conflict = re.search(
        rf"(?<![A-Za-z0-9]){re.escape(expected)}([A-Za-z0-9]+)(?![A-Za-z0-9])",
        title_u,
        re.IGNORECASE,
    )
    if conflict:
        return {
            "status": "MODEL_MISMATCH_SKIPPED",
            "reason": (
                f"标题含冲突型号扩展 {expected}{conflict.group(1)}；"
                f"不得当作 {expected}"
            ),
            "skip_browse": True,
            "conflict_token": f"{expected}{conflict.group(1)}",
        }
    return {
        "status": "TITLE_MODEL_UNCLEAR",
        "reason": "标题未边界命中预期型号；仍可进详情核验",
        "skip_browse": False,
    }


def extract_jd_product_id_from_url(url: str) -> str | None:
    raw = (url or "").strip()
    if not raw:
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    m = RE_ITEM_JD.search(raw) or RE_ITEM_M_JD.search(raw)
    if m:
        return m.group("id")
    parts = urlsplit(raw if "://" in raw else "https://" + raw.lstrip("/"))
    host = (parts.netloc or "").lower()
    if host not in ALLOWED_DETAIL_HOSTS:
        return None
    path = parts.path or ""
    if host == "item.m.jd.com":
        m2 = re.search(r"/product/(\d+)\.html", path, re.IGNORECASE)
        return m2.group(1) if m2 else None
    if host == "item.jd.com":
        m3 = re.search(r"/(\d+)\.html$", path)
        return m3.group(1) if m3 else None
    return None


def sanitize_detail_url(url: str) -> str:
    parts = urlsplit(url or "")
    if not parts.scheme and not parts.netloc:
        return url or ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))


def adapt_candidate_url(
    *,
    source_url: str,
    candidate_product_id: str,
    prefer_mobile: bool = True,
) -> dict[str, Any]:
    """
    Strict adapt to accepted detail shapes. Does not invent IDs.
    """
    pid = str(candidate_product_id or "").strip()
    if not pid.isdigit():
        return {
            "ok": False,
            "error": "candidate_product_id 必须为数字",
            "source_url": source_url,
            "adapted_url": None,
            "url_product_id": None,
        }
    src = (source_url or "").strip()
    if src:
        host = urlsplit(src if "://" in src else "https://" + src).netloc.lower()
        if host and host not in ALLOWED_DETAIL_HOSTS:
            return {
                "ok": False,
                "error": f"非允许详情主机: {host}",
                "source_url": sanitize_detail_url(src),
                "adapted_url": None,
                "url_product_id": None,
            }
    url_id = extract_jd_product_id_from_url(src) if src else None
    if src and url_id is None:
        return {
            "ok": False,
            "error": "无法从 URL 解析京东商品 ID",
            "source_url": sanitize_detail_url(src),
            "adapted_url": None,
            "url_product_id": None,
        }
    if url_id and url_id != pid:
        return {
            "ok": False,
            "error": f"URL 商品 ID({url_id}) 与候选 ID({pid}) 不一致",
            "source_url": sanitize_detail_url(src),
            "adapted_url": None,
            "url_product_id": url_id,
        }
    # Build adapted URL (prefer m.jd — currently verified capture entry).
    if prefer_mobile:
        adapted = f"https://item.m.jd.com/product/{pid}.html"
    else:
        adapted = f"https://item.jd.com/{pid}.html"
    host = urlsplit(adapted).netloc.lower()
    return {
        "ok": True,
        "error": None,
        "source_url": sanitize_detail_url(src) if src else None,
        "adapted_url": adapted,
        "adapted_host": host,
        "url_product_id": pid,
        "id_match": True,
    }


def final_url_identity_ok(final_url: str, candidate_product_id: str) -> dict[str, Any]:
    pid = str(candidate_product_id).strip()
    final_id = extract_jd_product_id_from_url(final_url)
    host = urlsplit(final_url or "").netloc.lower()
    allowed_host = host in ALLOWED_DETAIL_HOSTS
    ok = bool(final_id and final_id == pid and allowed_host)
    return {
        "ok": ok,
        "final_url_sanitized": sanitize_detail_url(final_url),
        "final_product_id": final_id,
        "final_host": host,
        "allowed_host": allowed_host,
        "id_match": final_id == pid if final_id else False,
    }


def load_business_baseline(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """
    Load business packaging / red line. Historical product_id/url/shop are
    recorded but must never override the current candidate identity.
    """
    errors: list[str] = []
    if not path.is_file():
        return None, [f"业务配置不存在: {path}"]
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        return None, [f"业务配置 JSON 无效: {exc}"]
    if not isinstance(raw, dict):
        return None, ["业务配置必须是 JSON 对象"]

    required = [
        "business_sku",
        "expected_model",
        "target_spec",
        "packs_per_box",
        "red_line",
        "currency",
        "comparison_unit",
    ]
    for key in required:
        if key not in raw or raw[key] in (None, ""):
            errors.append(f"缺少必要字段: {key}")
    if errors:
        return None, errors

    try:
        red = Decimal(str(raw["red_line"]))
        if red <= 0:
            errors.append("red_line 必须为正数金额")
    except (InvalidOperation, TypeError, ValueError):
        errors.append(f"red_line 不是有效金额: {raw.get('red_line')!r}")
        red = None

    try:
        packs = int(raw["packs_per_box"])
        if packs <= 0:
            errors.append("packs_per_box 必须为正整数")
    except (TypeError, ValueError):
        errors.append(f"packs_per_box 无效: {raw.get('packs_per_box')!r}")
        packs = None

    if errors:
        return None, errors

    historical = {
        "historical_product_id": raw.get("product_id"),
        "historical_url": raw.get("url"),
        "historical_expected_shop": raw.get("expected_shop"),
        "note": "历史登记锚点；不得覆盖本次候选 ID/URL/店名",
    }
    cfg = {
        "business_sku": str(raw["business_sku"]).strip(),
        "expected_model": str(raw["expected_model"]).strip(),
        "expected_brand": raw.get("expected_brand"),
        "expected_product_name": raw.get("expected_product_name"),
        "target_spec": str(raw["target_spec"]).strip(),
        "layers": raw.get("layers"),
        "sheets_or_pulls": raw.get("sheets_or_pulls"),
        "sheet_unit_aliases": raw.get("sheet_unit_aliases"),
        "packs_per_box": packs,
        "red_line": format(red, "f") if red is not None else None,
        "currency": str(raw["currency"]).strip(),
        "comparison_unit": str(raw["comparison_unit"]).strip(),
        "packaging_basis": raw.get("packaging_basis")
        if isinstance(raw.get("packaging_basis"), dict)
        else None,
        "_red_line_decimal": red,
        "_packs_per_box_int": packs,
        "_config_path": str(path.resolve()),
        "historical_anchors": historical,
    }
    return cfg, []


def business_override_from_baseline(cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": cfg["expected_model"],
        "spec_label": f"{cfg['target_spec']}／{cfg['comparison_unit']}",
        "target_spec": cfg["target_spec"],
        "packs_per_box": cfg["_packs_per_box_int"],
        "layers": cfg.get("layers"),
        "sheets_or_pulls": cfg.get("sheets_or_pulls"),
        "red_line_price_cny_per_box": cfg["_red_line_decimal"],
        "packaging_basis": cfg.get("packaging_basis"),
        "note": "来自业务 baseline；不是 OCR；不含候选 ID/店名覆盖",
    }


def expected_product_for_candidate_capture(
    cfg: dict[str, Any], *, candidate_product_id: str
) -> dict[str, Any]:
    """Capture hints: model/spec from business; identity from candidate only."""
    return {
        "jd_product_id": str(candidate_product_id),
        "model": cfg["expected_model"],
        "spec": f"{cfg['target_spec']}／{cfg['comparison_unit']}",
        # Do NOT pass historical shop — must not impersonate observed shop.
        "shop": None,
        "red_line_price_cny_per_box": float(cfg["_red_line_decimal"]),
        "note": "业务基准型号/规格；京东商品身份来自本次候选",
    }


def load_candidate_from_file(
    candidates_path: Path, product_id: str
) -> tuple[dict[str, Any] | None, str | None]:
    if not candidates_path.is_file():
        return None, f"candidates 文件不存在: {candidates_path}"
    try:
        data = json.loads(candidates_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        return None, f"candidates JSON 无效: {exc}"
    if not isinstance(data, list):
        return None, "candidates.json 必须是数组"
    pid = str(product_id).strip()
    for row in data:
        if not isinstance(row, dict):
            continue
        if str(row.get("product_id") or "").strip() == pid:
            return dict(row), None
    return None, f"candidates.json 中未找到 product_id={pid}"


def run_local_adapt_checks() -> list[str]:
    failures: list[str] = []

    a = adapt_candidate_url(
        source_url="https://item.jd.com/100005149674.html",
        candidate_product_id="100005149674",
    )
    if not a["ok"] or a["adapted_url"] != "https://item.m.jd.com/product/100005149674.html":
        failures.append("adapt item.jd.com failed")

    b = adapt_candidate_url(
        source_url="https://item.jd.com/111.html",
        candidate_product_id="222",
    )
    if b["ok"] or "不一致" not in str(b.get("error")):
        failures.append("id conflict should fail")

    c = adapt_candidate_url(
        source_url="https://evil.example/product/1.html",
        candidate_product_id="1",
    )
    if c["ok"]:
        failures.append("non-allowed host should fail")

    # Mobile source
    d = adapt_candidate_url(
        source_url="https://item.m.jd.com/product/1143036.html",
        candidate_product_id="1143036",
    )
    if not d["ok"]:
        failures.append("m.jd source should adapt ok")

    g = title_model_gate(
        "清风商用…B913AE …16包", expected_model="B913A"
    )
    if g["status"] != "MODEL_MISMATCH_SKIPPED" or not g["skip_browse"]:
        failures.append("B913AE must skip as mismatch for B913A")

    g2 = title_model_gate(
        "清风擦手纸B913A 1层200张20包", expected_model="B913A"
    )
    if g2["status"] != "TITLE_MODEL_OK":
        failures.append("exact B913A title should be OK")

    if title_contains_exact_model("批发B913AE", "B913A"):
        failures.append("boundary must not match B913A inside B913AE")

    id_ok = final_url_identity_ok(
        "https://item.m.jd.com/product/100005149674.html?x=1", "100005149674"
    )
    if not id_ok["ok"]:
        failures.append("final identity should pass")

    id_bad = final_url_identity_ok(
        "https://item.m.jd.com/product/999.html", "100005149674"
    )
    if id_bad["ok"]:
        failures.append("final id mismatch must fail")

    return failures
