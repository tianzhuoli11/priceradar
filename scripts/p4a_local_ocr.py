"""P4-A: local offline OCR on saved product screenshots (no browser / no JD)."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OCR_ROOT = (PROJECT_ROOT / "artifacts" / "ocr_runs").resolve()
MODELS_DIR = (PROJECT_ROOT / "models" / "ocr").resolve()

CAPTURE_IMAGE_NAMES = (
    "price.png",
    "product_context.png",
    "product_title.png",
    "selected_spec.png",
    "promotions.png",
    "shop.png",
)

# Engine is created only inside run entrypoints — never at import time.
_ENGINE: Any = None

PRICE_RE = re.compile(
    r"(?P<currency>¥|￥|\$)?\s*(?P<amount>\d{1,7}(?:\.\d{1,2})?)"
)
# Avoid Unicode \b (CJK letters are \w, so "批发B913A" would miss).
# Default pattern keeps CLI/selftest sample compatibility; callers should pass expected_model.
DEFAULT_EXPECTED_MODEL = "B913A"
MODEL_RE = re.compile(
    rf"(?<![A-Za-z0-9]){re.escape(DEFAULT_EXPECTED_MODEL)}(?![A-Za-z0-9])",
    re.IGNORECASE,
)
LAYER_RE = re.compile(r"(\d+)\s*层")
SHEET_RE = re.compile(r"(\d+)\s*(?:抽|张)")
PACK_RE = re.compile(r"(\d+)\s*包")
QTY_RE = re.compile(r"(\d+)\s*个")
PROMO_HINTS = (
    "满",
    "赠",
    "券",
    "优惠",
    "减",
    "会员",
    "满送",
    "满减",
    "到手",
    "券后",
    "新人",
    "预估",
)
SHOP_HINTS = ("旗舰店", "专营店", "自营店", "官方旗舰")
THRESHOLD_HINTS = ("满", "赠", "即赠", "满减", "满送")


def model_regex_for(expected_model: str | None) -> re.Pattern[str]:
    model = (expected_model or DEFAULT_EXPECTED_MODEL).strip() or DEFAULT_EXPECTED_MODEL
    return re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(model)}(?![A-Za-z0-9])",
        re.IGNORECASE,
    )


def pkg_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat(timespec="seconds")


def utc_run_stamp(dt: datetime | None = None) -> str:
    return (dt or utc_now()).strftime("%Y%m%dT%H%M%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def image_size(path: Path) -> dict[str, int] | None:
    try:
        from PIL import Image

        with Image.open(path) as im:
            w, h = im.size
            return {"width": int(w), "height": int(h)}
    except OSError:
        return None


def make_run_dir() -> tuple[str, Path]:
    run_id = f"{utc_run_stamp()}_{secrets.token_hex(3)}"
    out_dir = OCR_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=False)
    return run_id, out_dir


def get_engine() -> Any:
    """Lazy RapidOCR init (models load on first call per rapidocr>=3.10 docs)."""
    global _ENGINE
    if _ENGINE is None:
        from rapidocr import RapidOCR

        _ENGINE = RapidOCR()
    return _ENGINE


def preprocess_image(src: Path, dest: Path) -> dict[str, Any]:
    """Traditional preprocess only: white pad + upscale + mild contrast. No redraw."""
    from PIL import Image, ImageEnhance, ImageOps

    with Image.open(src) as im:
        rgb = im.convert("RGB")
        w, h = rgb.size
        # Equal-proportion upscale for small crops; keep large images moderate.
        scale = 3.0 if max(w, h) < 200 else (2.0 if max(w, h) < 500 else 1.5)
        new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
        up = rgb.resize(new_size, Image.Resampling.LANCZOS)
        pad = 24
        padded = ImageOps.expand(up, border=pad, fill=(255, 255, 255))
        enhanced = ImageEnhance.Contrast(padded).enhance(1.35)
        enhanced.save(dest, format="PNG")
        return {
            "method": "white_pad+lanczos_upscale+contrast_1.35",
            "scale": scale,
            "pad_px": pad,
            "source_size": {"width": w, "height": h},
            "output_size": {"width": enhanced.size[0], "height": enhanced.size[1]},
            "path": str(dest.resolve()),
            "filename": dest.name,
        }


class OcrBlock:
    __slots__ = ("text", "score", "box", "source_image", "variant")

    def __init__(
        self,
        text: str,
        score: float | None,
        box: list[list[float]] | None,
        source_image: str,
        variant: str,
    ) -> None:
        self.text = text
        self.score = score
        self.box = box
        self.source_image = source_image
        self.variant = variant  # original | preprocessed


def run_ocr_on_path(path: Path, *, variant: str, source_label: str) -> list[OcrBlock]:
    engine = get_engine()
    result = engine(str(path))
    blocks: list[OcrBlock] = []
    if result is None:
        return blocks
    txts = getattr(result, "txts", None) or ()
    scores = getattr(result, "scores", None) or ()
    boxes = getattr(result, "boxes", None)
    for i, text in enumerate(txts):
        score = float(scores[i]) if i < len(scores) else None
        box = None
        if boxes is not None and i < len(boxes):
            try:
                box = [[float(x), float(y)] for x, y in boxes[i]]
            except Exception:
                box = None
        blocks.append(
            OcrBlock(
                text=str(text),
                score=score,
                box=box,
                source_image=source_label,
                variant=variant,
            )
        )
    return blocks


def parse_amount(raw: str) -> Decimal | None:
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return None


def classify_price_role(block_text: str, amount: Decimal) -> str:
    t = block_text
    if any(h in t for h in THRESHOLD_HINTS) and (
        "满" in t or "赠" in t or "减" in t
    ):
        # Threshold / gift condition numbers are not selling price / take-home.
        return "promotion_threshold_or_gift"
    # Declared / estimated take-home labels (still not auto-confirmed deal price).
    if any(h in t for h in ("预估到手", "预计到手", "预估价")) or (
        "预估" in t and "到手" in t
    ):
        return "estimated_take_home_candidate"
    if any(
        h in t
        for h in ("到手价", "券后价", "券后", "会员价", "新人价", "学生价")
    ):
        return "declared_take_home_candidate"
    if "¥" in t or "￥" in t:
        # Currency near amount → candidate main/display price (still not unique pick).
        return "currency_tagged_price_candidate"
    if re.search(r"(售价|价格|单价)", t):
        return "labeled_price_candidate"
    return "unlabeled_number_candidate"


def extract_from_blocks(
    blocks: list[OcrBlock],
    *,
    expected_model: str | None = None,
) -> dict[str, Any]:
    price_candidates: list[dict[str, Any]] = []
    model_candidates: list[dict[str, Any]] = []
    specification_candidates: list[dict[str, Any]] = []
    promotion_text: list[dict[str, Any]] = []
    shop_candidates: list[dict[str, Any]] = []
    review_reasons: list[str] = []
    model_name = (expected_model or DEFAULT_EXPECTED_MODEL).strip() or DEFAULT_EXPECTED_MODEL
    model_re = model_regex_for(model_name)

    for b in blocks:
        text = b.text.strip()
        if not text:
            continue

        if model_re.search(text):
            model_candidates.append(
                {
                    "model": model_name,
                    "source_text": text,
                    "source_image": b.source_image,
                    "variant": b.variant,
                    "score": b.score,
                    "box": b.box,
                    "match_rule": "boundary_regex",
                }
            )

        if any(h in text for h in PROMO_HINTS):
            promotion_text.append(
                {
                    "text": text,
                    "source_image": b.source_image,
                    "variant": b.variant,
                    "score": b.score,
                    "box": b.box,
                }
            )

        if any(h in text for h in SHOP_HINTS) or ("店" in text and len(text) <= 40):
            noise = (
                "服务承诺",
                "正品保证",
                "正品行货",
                "开具发票",
                "全国联保",
                "质保证书",
            )
            if any(n in text for n in noise):
                pass
            elif re.search(r"旗舰店|专营店|自营店|官方旗舰", text):
                shop_candidates.append(
                    {
                        "shop_name": text,
                        "source_text": text,
                        "source_image": b.source_image,
                        "variant": b.variant,
                        "score": b.score,
                        "box": b.box,
                    }
                )

        # Spec pieces
        spec_hit = False
        spec_obj: dict[str, Any] = {
            "source_text": text,
            "source_image": b.source_image,
            "variant": b.variant,
            "score": b.score,
            "box": b.box,
            "layers": None,
            "sheets_or_pulls": None,
            "packs": None,
            "purchase_qty": None,
            "unit_box_evidence": None,
        }
        m = LAYER_RE.search(text)
        if m:
            spec_obj["layers"] = m.group(1)
            spec_hit = True
        m = SHEET_RE.search(text)
        if m:
            spec_obj["sheets_or_pulls"] = m.group(1)
            spec_hit = True
        m = PACK_RE.search(text)
        if m:
            spec_obj["packs"] = m.group(1)
            spec_hit = True
            if "箱" in text:
                spec_obj["unit_box_evidence"] = "text_contains_箱_near_packs"
            else:
                spec_obj["unit_box_evidence"] = (
                    "20包不自动等于一箱；无明确箱单位证据"
                )
        m = QTY_RE.search(text)
        if m:
            spec_obj["purchase_qty"] = m.group(1)
            spec_hit = True
        if spec_hit:
            specification_candidates.append(spec_obj)

        for m in PRICE_RE.finditer(text):
            amount_s = m.group("amount")
            currency = m.group("currency")
            amount = parse_amount(amount_s)
            if amount is None:
                continue
            role = classify_price_role(text, amount)
            nearby_label = None
            for h in (
                "预估到手",
                "到手价",
                "券后价",
                "会员价",
                "新人价",
                "满送",
                "满减",
                "优惠",
                "售价",
                "价格",
                "券",
            ):
                if h in text:
                    nearby_label = h
                    break
            price_candidates.append(
                {
                    "amount": format(amount, "f"),
                    "currency_evidence": currency,
                    "nearby_label": nearby_label,
                    "role": role,
                    "source_text": text,
                    "source_image": b.source_image,
                    "variant": b.variant,
                    "score": b.score,
                    "box": b.box,
                }
            )

    # Conflict / ambiguity flags (no picking by historical 168 / red-line 127).
    mainish = [
        p
        for p in price_candidates
        if p["role"] in {"currency_tagged_price_candidate", "labeled_price_candidate"}
    ]
    thresholds = [p for p in price_candidates if p["role"] == "promotion_threshold_or_gift"]
    amounts_main = {p["amount"] for p in mainish}
    if len(amounts_main) > 1:
        review_reasons.append("多个币种/标签价格金额冲突")
    if mainish and thresholds:
        review_reasons.append("同时存在售价候选与满赠门槛数字，需人工区分")
    if price_candidates and not mainish:
        review_reasons.append("仅有无标签数字，缺乏充分主价格依据")
    if not price_candidates:
        review_reasons.append("未识别到可解析金额")
    if not model_candidates:
        review_reasons.append(f"未识别到边界匹配型号 {model_name}")
    if not specification_candidates:
        review_reasons.append("未识别到规格相关文本")

    # Original vs preprocessed amount disagreement among currency-tagged
    orig_amt = {
        p["amount"]
        for p in mainish
        if p["variant"] == "original"
    }
    prep_amt = {
        p["amount"]
        for p in mainish
        if p["variant"] == "preprocessed"
    }
    if orig_amt and prep_amt and orig_amt != prep_amt:
        review_reasons.append("原图与预处理识别的币种价格不一致，待核验")

    status = "OK_CANDIDATES_ONLY"
    if review_reasons:
        status = "REVIEW_REQUIRED"

    return {
        "price_candidates": price_candidates,
        "model_candidates": model_candidates,
        "specification_candidates": specification_candidates,
        "promotion_text": promotion_text,
        "shop_candidates": shop_candidates,
        "selected_price": None,  # never auto-pick unique selling price this phase
        "comparison_status": "NOT_EVALUATED",
        "extraction_status": status,
        "expected_model_for_match": model_name,
        "review_reasons": review_reasons,
        "limits": [
            "不做破价判定",
            "不把主展示价改名为到手价",
            "到手价线索由后续模块分类；本层只提取 OCR 候选",
            "不从 expected_product 回填缺失字段",
            "历史观察价禁止用于纠偏或选候选",
            "20包不自动等于一箱",
        ],
    }


def blocks_to_jsonable(blocks: list[OcrBlock]) -> list[dict[str, Any]]:
    return [
        {
            "text": b.text,
            "score": b.score,
            "box": b.box,
            "source_image": b.source_image,
            "variant": b.variant,
            "score_note": "engine_score_not_correctness_probability",
        }
        for b in blocks
    ]


def process_single_image(
    image_path: Path,
    out_dir: Path,
    *,
    also_preprocess: bool = True,
    expected_model: str | None = None,
) -> dict[str, Any]:
    image_path = image_path.resolve()
    if not image_path.is_file():
        raise FileNotFoundError(str(image_path))

    size = image_size(image_path)
    digest = sha256_file(image_path)
    input_quality_risks: list[str] = []
    # Browser clip of a price region is intentionally small (often ~100–250px).
    # Do not treat "used a clip" or moderate region size as digit truncation.
    # Only flag bitmaps too small to plausibly contain ¥ + integer + decimals.
    if size and (size["height"] < 36 or size["width"] < 48):
        input_quality_risks.append(
            "输入像素过小，难以容纳完整价格数字；即使OCR读出金额也需待核验"
        )

    all_blocks: list[OcrBlock] = []
    ocr_ok = False
    ocr_error = None
    try:
        all_blocks.extend(
            run_ocr_on_path(
                image_path, variant="original", source_label=image_path.name
            )
        )
        ocr_ok = True
    except Exception as exc:  # noqa: BLE001 — surface engine errors in report
        ocr_error = f"{type(exc).__name__}: {exc}"

    preprocess_meta = None
    if also_preprocess and ocr_ok:
        prep_path = out_dir / f"preprocessed_{image_path.stem}.png"
        preprocess_meta = preprocess_image(image_path, prep_path)
        try:
            all_blocks.extend(
                run_ocr_on_path(
                    prep_path,
                    variant="preprocessed",
                    source_label=prep_path.name,
                )
            )
        except Exception as exc:  # noqa: BLE001
            ocr_error = (
                (ocr_error + "; " if ocr_error else "")
                + f"preprocess_ocr {type(exc).__name__}: {exc}"
            )

    extraction = extract_from_blocks(all_blocks, expected_model=expected_model)
    if input_quality_risks:
        extraction["review_reasons"] = list(
            dict.fromkeys(extraction.get("review_reasons", []) + input_quality_risks)
        )
        extraction["extraction_status"] = "REVIEW_REQUIRED"
        extraction["input_quality_risks"] = input_quality_risks

    return {
        "input": {
            "path": str(image_path),
            "filename": image_path.name,
            "sha256": digest,
            "pixel_size": size,
        },
        "ocr_ok": ocr_ok,
        "ocr_error": ocr_error,
        "preprocess": preprocess_meta,
        "blocks": blocks_to_jsonable(all_blocks),
        "extraction": extraction,
    }


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def collect_images_from_run_dir(run_dir: Path) -> list[Path]:
    run_dir = run_dir.resolve()
    found: list[Path] = []
    for name in CAPTURE_IMAGE_NAMES:
        p = run_dir / name
        if p.is_file():
            found.append(p)
    return found


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PriceRadar P4-A local OCR")
    g = p.add_mutually_exclusive_group(required=False)
    g.add_argument("--image", help="Single image path")
    g.add_argument("--run-dir", help="One price_captures run directory")
    p.add_argument(
        "--no-preprocess",
        action="store_true",
        help="Skip preprocess variant (default runs original + one preprocess)",
    )
    p.add_argument(
        "--self-test",
        action="store_true",
        help="Run synthetic extraction unit checks and exit (no OCR engine)",
    )
    args = p.parse_args(argv)
    if not args.self_test and not args.image and not args.run_dir:
        p.error("one of --image / --run-dir is required (unless --self-test)")
    if args.self_test and (args.image or args.run_dir):
        p.error("--self-test cannot be combined with --image / --run-dir")
    return args


def run_synthetic_extraction_tests() -> list[str]:
    """Synthetic parser tests — not real captures."""
    failures: list[str] = []

    blocks = [
        OcrBlock("¥168.00", 0.9, None, "synth.png", "original"),
        OcrBlock("满129元即赠热销商品，赠完即止", 0.9, None, "synth.png", "original"),
        OcrBlock("B913A 1层200抽*20包", 0.9, None, "synth.png", "original"),
    ]
    ext = extract_from_blocks(blocks)
    roles = {p["amount"]: p["role"] for p in ext["price_candidates"]}
    if roles.get("168.00") != "currency_tagged_price_candidate":
        failures.append("168 with yen should be currency-tagged candidate")
    if roles.get("129") != "promotion_threshold_or_gift" and roles.get("129.00") != "promotion_threshold_or_gift":
        # amount may be 129 without decimals
        if not any(
            p["amount"].startswith("129") and p["role"] == "promotion_threshold_or_gift"
            for p in ext["price_candidates"]
        ):
            failures.append("满129 should be promotion_threshold_or_gift")
    if not ext["model_candidates"]:
        failures.append("B913A should be found")
    if ext["selected_price"] is not None:
        failures.append("must not auto-select unique price")
    if ext["comparison_status"] != "NOT_EVALUATED":
        failures.append("comparison must be NOT_EVALUATED")

    # Multi-amount ambiguity
    multi = extract_from_blocks(
        [
            OcrBlock("¥168.00", 0.9, None, "a.png", "original"),
            OcrBlock("¥99.00", 0.9, None, "a.png", "original"),
        ]
    )
    if "冲突" not in "".join(multi["review_reasons"]) and multi["extraction_status"] != "REVIEW_REQUIRED":
        failures.append("multi currency prices should REVIEW_REQUIRED")

    empty = extract_from_blocks([])
    if empty["extraction_status"] != "REVIEW_REQUIRED":
        failures.append("empty OCR should REVIEW_REQUIRED")

    # Incomplete digit must not be filled with historical price
    bad = extract_from_blocks([OcrBlock("¥16", 0.5, None, "crop.png", "original")])
    if any(p["amount"] == "168.00" for p in bad["price_candidates"]):
        failures.append("must not invent 168 from incomplete OCR")

    return failures


def safe_print(msg: Any) -> None:
    """Avoid Windows console GBK crashes on currency symbols like ¥."""
    text = str(msg)
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        fails = run_synthetic_extraction_tests()
        safe_print(f"synthetic_failures={fails}")
        return 0 if not fails else 1

    started = utc_now()
    safe_print("=== PriceRadar P4-A ===")
    safe_print(f"解释器: {sys.executable}")
    safe_print(f"项目根: {PROJECT_ROOT}")
    safe_print(
        f"rapidocr={pkg_version('rapidocr')} onnxruntime={pkg_version('onnxruntime')}"
    )
    safe_print("说明: 首次推理可能下载/加载 bundled 模型；之后为本地推理。")
    safe_print("历史观察价168与红线价127不得用于纠偏或选候选。")
    safe_print("")

    images: list[Path]
    if args.image:
        images = [Path(args.image)]
    else:
        images = collect_images_from_run_dir(Path(args.run_dir))
        if not images:
            safe_print(f"run-dir 中未找到可识别截图: {args.run_dir}")
            return 2

    run_id, out_dir = make_run_dir()
    safe_print(f"run_id: {run_id}")
    safe_print(f"输出目录: {out_dir}")

    per_image: list[dict[str, Any]] = []
    all_blocks: list[dict[str, Any]] = []
    combined_extraction_inputs: list[OcrBlock] = []

    for img in images:
        safe_print("")
        safe_print(f"--- OCR: {img} ---")
        try:
            result = process_single_image(
                img,
                out_dir,
                also_preprocess=not args.no_preprocess,
            )
        except FileNotFoundError as exc:
            safe_print(f"文件不存在: {exc}")
            return 2
        per_image.append(result)
        all_blocks.extend(result["blocks"])
        safe_print(f"ocr_ok={result['ocr_ok']} error={result.get('ocr_error')}")
        ext = result["extraction"]
        safe_print(f"extraction_status={ext.get('extraction_status')}")
        safe_print(f"price_candidates={ext.get('price_candidates')}")
        safe_print(f"model_candidates={ext.get('model_candidates')}")
        safe_print(
            f"specification_candidates={ext.get('specification_candidates')}"
        )
        safe_print(
            f"promotion_text={[p.get('text') for p in ext.get('promotion_text', [])]}"
        )
        safe_print(f"shop_candidates={ext.get('shop_candidates')}")
        safe_print(f"review_reasons={ext.get('review_reasons')}")
        if result.get("preprocess"):
            safe_print(f"preprocess={result['preprocess'].get('path')}")

        # Rebuild blocks for optional per-image already done; keep separate records.
        for b in result["blocks"]:
            combined_extraction_inputs.append(
                OcrBlock(
                    text=b["text"],
                    score=b.get("score"),
                    box=b.get("box"),
                    source_image=b["source_image"],
                    variant=b["variant"],
                )
            )

    # When multiple images from one run-dir: keep per-image extractions separate;
    # also store a directory-level merge for convenience but flagged as multi-image.
    if len(per_image) == 1:
        extraction_payload = {
            "mode": "single_image",
            **per_image[0]["extraction"],
            "per_image": None,
        }
    else:
        extraction_payload = {
            "mode": "run_dir_separate_then_merged_candidates",
            "note": "不同图片候选合并仅便于查看；不得当作单一实时同步证据",
            "per_image": [
                {
                    "input": r["input"],
                    "extraction": r["extraction"],
                    "ocr_ok": r["ocr_ok"],
                }
                for r in per_image
            ],
            **extract_from_blocks(combined_extraction_inputs),
        }

    ocr_raw_path = out_dir / "ocr_raw.json"
    extraction_path = out_dir / "extraction.json"
    meta_path = out_dir / "run_metadata.json"

    write_json(
        ocr_raw_path,
        {
            "engine": "rapidocr",
            "engine_version": pkg_version("rapidocr"),
            "onnxruntime_version": pkg_version("onnxruntime"),
            "blocks": all_blocks,
            "per_image_block_counts": {
                r["input"]["filename"]: len(r["blocks"]) for r in per_image
            },
        },
    )
    write_json(extraction_path, extraction_payload)

    ended = utc_now()
    metadata = {
        "run_id": run_id,
        "phase": "P4-A",
        "started_at_utc": utc_iso(started),
        "ended_at_utc": utc_iso(ended),
        "engine": {
            "name": "rapidocr",
            "version": pkg_version("rapidocr"),
            "onnxruntime": pkg_version("onnxruntime"),
            "pillow": pkg_version("Pillow"),
            "model_note": (
                "Models bundled/loaded by rapidocr package defaults "
                "(Det/Cls/Rec PP-OCR family via ONNX Runtime CPU). "
                "First inference may fetch/load; subsequent runs are local."
            ),
            "models_dir_project": str(MODELS_DIR),
        },
        "inputs": [r["input"] for r in per_image],
        "preprocess": [r.get("preprocess") for r in per_image],
        "ocr_ok_flags": [r["ocr_ok"] for r in per_image],
        "comparison_status": "NOT_EVALUATED",
        "notes": [
            "本地 OCR 候选提取；引擎成功≠金额可靠",
            "禁止用历史168或红线127纠偏",
            "P3-A 搜索受限未通过；P2-D 截图状态不因本轮改写",
        ],
    }
    write_json(meta_path, metadata)

    safe_print("")
    safe_print("=== 输出 ===")
    safe_print(f"ocr_raw.json: {ocr_raw_path.resolve()}")
    safe_print(f"extraction.json: {extraction_path.resolve()}")
    safe_print(f"run_metadata.json: {meta_path.resolve()}")
    safe_print(
        "提醒: OCR 运行成功不等于金额提取可靠；comparison_status=NOT_EVALUATED。"
    )
    return 0 if all(r["ocr_ok"] for r in per_image) else 1


if __name__ == "__main__":
    raise SystemExit(main())
