"""
DIAGNOSTIC ONLY — strict-policy ``evaluate --depends-on`` reads (BrowserSkill >= 0.5.0).

Not imported by the default pipeline; nothing here changes default behaviour.
Runs only with ``--enable-depends-on-diagnostic`` and an explicit BSK_EXE whose
connected daemon is >= 0.5.0 / protocol 1.9 with no version skew.

Per page (one navigation, no search, no spec switch, no retry):
  PREFLIGHT -> NAVIGATE -> INITIAL_SYNC -> DEPENDENCY_DISCOVERY (``bsk read`` confirms
  each selector exists exactly once on the observed page; nothing guessed) ->
  IDENTITY (classify page: login / verification / rate-limit / product id) ->
  LOGIN_CHECK_START -> READ_A (selection state + price-role probe) ->
  SCREENSHOT (per region: dep-read box + hit-test, screenshot under the same
  receipt, a second dep-read box under a new receipt must match; occlusion /
  login overlay invalidates) -> READ_B -> LOGIN_CHECK_END -> COMPARE (A vs B:
  product id, structured spec selection, price-role evidence) -> JUDGE.

Every evaluate is: ``cooperate sync --policy strict --settle`` then
``evaluate --context <ctx> --depends-on <sel> ...`` WITHOUT ``--observation``
(passing an observation makes BSK check the whole page again). No --unverified.
A refusal stops the run at that stage (no extra retries).

BSK 0.5.0 limits (verified from the cli-v0.5.0 source and the local fixtures):
- a dependency is compared over its enclosing scope (nearest form/list/table/
  dialog, modal, main/section/article/region, or absolute/fixed ancestor);
  without one the scope is the whole page, so unrelated timers still refuse;
- class-only selection markers such as ``specification-item-sku--selected`` are
  not compared by BSK; the application-level A/B comparison catches them.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

SCRIPTS = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import p5_bsk_detail as bd  # noqa: E402
import p5_bsk_runtime as rt  # noqa: E402
import priceradar_selected_spec as ss  # noqa: E402
import priceradar_take_home_prices as th  # noqa: E402

DIAG_VERSION = "depends-on-diag-v3"  # v2: no scroll; full-viewport containment; OCR must show amount AND label
DIAG_ROOT = PROJECT_ROOT / "artifacts" / "depends_on_diag"
MIN_DAEMON = (0, 5, 0)
REQUIRED_PROTOCOL = "1.9"

# Candidate selectors (same families as DISCOVER_REGIONS_JS). Only a candidate that
# `bsk read` finds exactly once on the observed page is declared.
DEP_REGION_CANDIDATES: dict[str, list[str]] = {
    "title": [".sku-title-text", ".sku-title-name", ".sku-title", ".sku-name", "#name h1"],
    "spec": [".page-right-spec", ".specifications-panel-content"],
    "price": [".product-price-panel", ".page-right-price", ".summary-price-wrap .p-price", "#jd-price"],
}
REQUIRED_DEPS = ("title", "spec", "price")
SHOT_REGIONS = ("price", "spec", "title")


def _ver(s: str | None) -> tuple[int, ...]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", str(s or ""))
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def preflight(client: Any) -> dict[str, Any]:
    """The CLI version alone is not enough: the connected daemon/extension must be aligned."""
    out: dict[str, Any] = {"ok": False, "bsk_exe": str(getattr(client, "exe", ""))}
    try:
        v = subprocess.run([str(client.exe), "--version"], capture_output=True, text=True, timeout=30)
        out["cli_version"] = (v.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"cli_version_failed: {exc}"
        return out
    st = client.run(["status"])
    d = st.get("data") if isinstance(st.get("data"), dict) else {}
    browsers = [b for b in (d.get("browsers") or []) if isinstance(b, dict)]
    out.update({
        "daemon_version": d.get("daemon_version"), "protocol_version": d.get("protocol_version"),
        "ws_port": d.get("ws_port"), "pid": d.get("pid"),
        "browsers": [{k: b.get(k) for k in ("instance_id", "extension_version", "extension_protocol_version",
                                             "version_skew", "unresponsive")} for b in browsers],
        "version_skew_browsers": d.get("version_skew_browsers"),
    })
    problems = []
    if _ver(out["cli_version"]) < MIN_DAEMON:
        problems.append("cli_below_0.5.0")
    if _ver(d.get("daemon_version")) < MIN_DAEMON:
        problems.append("daemon_below_0.5.0")
    if str(d.get("protocol_version")) != REQUIRED_PROTOCOL:
        problems.append("daemon_protocol_not_1.9")
    ok_b = [b for b in browsers if not b.get("version_skew") and not b.get("unresponsive")
            and str(b.get("extension_protocol_version")) == REQUIRED_PROTOCOL]
    if not ok_b:
        problems.append("no_connected_extension_on_1.9_without_skew")
    if d.get("version_skew_browsers"):
        problems.append("version_skew_reported")
    out["problems"] = problems
    out["ok"] = not problems
    out["browser_instance"] = ok_b[0]["instance_id"] if ok_b else None
    return out


def _err_summary(res: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(res, dict):
        return None
    d = res.get("data") if isinstance(res.get("data"), dict) else {}
    inner = d.get("data") if isinstance(d.get("data"), dict) else {}
    oc = inner.get("observed_change") if isinstance(inner.get("observed_change"), dict) else {}
    return {"code": d.get("code"), "message": d.get("message"), "reason": inner.get("reason"),
            "observed_change_scope": oc.get("scope"), "observed_change_part": oc.get("part"),
            "observed_change_role": oc.get("role"), "observed_changes": rt.sanitize(inner.get("observed_changes")),
            "exit_code": res.get("exit_code"), "stderr": (res.get("stderr") or None)}


def strict_sync(client: Any, sid: str) -> dict[str, Any]:
    r = client.run(["cooperate", "sync", "--session", sid, "--policy", "strict", "--settle"], timeout=120)
    d = r.get("data") if isinstance(r.get("data"), dict) else {}
    ctx = d.get("context_id") or d.get("context")
    return {"ok": bool(r.get("ok") and ctx), "context_id": ctx, "control_policy": d.get("control_policy"),
            "kind": d.get("kind"), "baseline_reason": d.get("baseline_reason"),
            "error": None if ctx else _err_summary(r),
            "raw_without_context": None if ctx else {"keys": sorted(d.keys()), "data": rt.sanitize({k: v for k, v in d.items() if k != "observation"}),
                                                     "raw_stdout": rt.sanitize(r.get("data")) if not d else None}}


def dep_eval(client: Any, sid: str, expr: str, deps: list[str], *, ctx: str | None = None) -> dict[str, Any]:
    """One strict sync (unless a receipt is given) + one dependency-scoped evaluate. No retry."""
    sync = None
    if ctx is None:
        sync = strict_sync(client, sid)
        if not sync["ok"]:
            return {"ok": False, "failure": "SYNC_FAILED", "sync": sync, "value": None}
        ctx = sync["context_id"]
    args = ["evaluate", "--session", sid, expr, "--context", str(ctx)]
    for d in deps:
        args += ["--depends-on", d]
    r = client.run(args, timeout=60)
    data = r.get("data")
    val = data.get("value") if isinstance(data, dict) and "value" in data else data
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except json.JSONDecodeError:
            pass
    if not r.get("ok"):
        return {"ok": False, "failure": "EVAL_REFUSED", "sync": sync, "context_id": ctx, "error": _err_summary(r), "value": None}
    return {"ok": True, "sync": sync, "context_id": ctx, "value": val, "depends_on": list(deps)}


def discover_dependencies(client: Any, sid: str, ctx: str) -> dict[str, Any]:
    """Pick, per region, the first candidate selector that the observed page holds exactly once."""
    regions: dict[str, Any] = {}
    for key, cands in DEP_REGION_CANDIDATES.items():
        tried = []
        for sel in cands:
            r = client.run(["read", "--session", sid, "--selector", sel, "--context", ctx, "--max-nodes", "5"], timeout=60)
            d = r.get("data") if isinstance(r.get("data"), dict) else {}
            items = d.get("items") if isinstance(d.get("items"), list) else []
            row = {"selector": sel, "ok": bool(r.get("ok")), "count": len(items), "truncated": d.get("truncated"),
                   "error": None if r.get("ok") else _err_summary(r)}
            tried.append(row)
            if r.get("ok") and len(items) == 1 and not d.get("truncated"):
                vals = items[0].get("values") if isinstance(items[0].get("values"), dict) else {}
                regions[key] = {"selector": sel, "observed_ref": items[0].get("ref"),
                                "observed_text": str(vals.get("text") or "").strip()[:240],
                                "page_url": d.get("page_url"), "tried": tried}
                break
        else:
            regions[key] = {"selector": None, "tried": tried}
    missing = [k for k in REQUIRED_DEPS if not (regions.get(k) or {}).get("selector")]
    return {"ok": not missing, "missing": missing, "regions": regions,
            "depends_on": [regions[k]["selector"] for k in REQUIRED_DEPS if (regions.get(k) or {}).get("selector")]}


def _ocr_amounts(png: Path) -> tuple[set[str], list[str]]:
    import p4a_local_ocr as p4a  # noqa: PLC0415

    blocks = p4a.run_ocr_on_path(png, variant="original", source_label=png.name)
    texts = [b.text for b in blocks]
    amounts: set[str] = set()
    for t in texts:
        for m in re.finditer(r"[¥￥]\s*(\d+(?:\.\d+)?)", t):
            amounts.add(m.group(1))
        if re.fullmatch(r"\s*\d+(?:\.\d+)?\s*", t):
            amounts.add(t.strip())
    return amounts, texts


def viewport_box_js(selector: str) -> str:
    """Region box + hit-test + viewport (CSS px, scrollbars excluded) and full-containment flag.
    Also each amount / take-home label leaf inside the region (same rules as PRICE_ROLE_EVIDENCE_JS)
    with its own containment + hit-test, so a fixed sidebar over the amount or label is caught."""
    return f"""(() => {{
{bd.PR_DOM_HELPERS_JS}
      const el = document.querySelector({json.dumps(selector)});
      if (!el) return {{ ok: false, reason: 'element_missing' }};
      const r = el.getBoundingClientRect();
      const de = document.documentElement;
      const vw = de.clientWidth || window.innerWidth, vh = de.clientHeight || window.innerHeight;
      const norm = (t) => String(t || '').replace(/\\s+/g, ' ').trim();
      const AMT = /^[¥￥]\\s*\\d+(?:\\.\\d+)?$/;
      const LABEL = /(到手价|首单|新人|会员|PLUS|plus|券后|预估|优惠后|折后)/;
      const inVp = (b) => b.width > 0 && b.height > 0 && b.left >= 0 && b.top >= 0 && b.right <= vw && b.bottom <= vh;
      const all = Array.from(el.querySelectorAll('*')).filter((x) => __prVis(x));
      const isAmt = (x) => AMT.test(norm(x.textContent));
      const node = (x, kind) => {{ const b = x.getBoundingClientRect(); const h = __prHitTest(x);
        return {{ kind, text: norm(x.textContent), box: {{ x: b.x, y: b.y, w: b.width, h: b.height }}, fully_in_viewport: inVp(b), hit: h.status }}; }};
      const priceNodes = all.filter((x) => isAmt(x) && !Array.from(x.children).some(isAmt)).map((x) => node(x, 'amount'))
        .concat(all.filter((x) => norm(x.textContent).length <= 12 && LABEL.test(norm(x.textContent))
          && !Array.from(x.children).some((c) => LABEL.test(norm(c.textContent)) && norm(c.textContent).length <= 12)).map((x) => node(x, 'label')));
      return {{
        price_nodes: priceNodes,
        ok: true,
        text: String(el.innerText||'').replace(/\\s+/g,' ').trim().slice(0,240),
        box: {{ x:r.x, y:r.y, w:r.width, h:r.height, width:r.width, height:r.height }},
        viewport: {{ client_w: vw, client_h: vh, inner_w: window.innerWidth, inner_h: window.innerHeight,
                     outer_w: window.outerWidth, outer_h: window.outerHeight, scroll_x: window.scrollX, scroll_y: window.scrollY }},
        fully_in_viewport: r.width > 8 && r.height > 8 && r.left >= 0 && r.top >= 0 && r.right <= vw && r.bottom <= vh,
        dpr: window.devicePixelRatio || 1,
        hit: __prHitTest(el),
      }};
    }})()"""


WINDOW_STATE_JS = r"""(() => ({ inner_w: innerWidth, inner_h: innerHeight,
  client_w: document.documentElement.clientWidth, client_h: document.documentElement.clientHeight,
  outer_w: outerWidth, outer_h: outerHeight, screen_x: screenX, screen_y: screenY,
  avail_w: screen.availWidth, avail_h: screen.availHeight, screen_w: screen.width, screen_h: screen.height,
  dpr: devicePixelRatio, visibility: document.visibilityState }))()"""


# Usable viewport needed before the single detail navigation (CSS px, scrollbars excluded).
# JD PC detail content is ~1210 px wide and a fixed ~56 px sidebar sits at the right edge;
# the right-hand price column must clear it with margin. Height: title/spec/price sit in the
# first ~650 px of the right column.
MIN_VIEWPORT_W = 1280
MIN_VIEWPORT_H = 700


def window_gate(win: dict[str, Any] | None) -> dict[str, Any]:
    """Gate on the MEASURED usable viewport (not on launch parameters, not on outer-vs-screen equality)."""
    if not isinstance(win, dict) or "client_w" not in win:
        return {"ok": False, "reason": "WINDOW_STATE_UNREADABLE"}
    w_ok = float(win["client_w"]) >= MIN_VIEWPORT_W
    h_ok = float(win["client_h"]) >= MIN_VIEWPORT_H
    vis = win.get("visibility") == "visible"
    ok = w_ok and h_ok and vis
    return {"ok": ok, "min_viewport": [MIN_VIEWPORT_W, MIN_VIEWPORT_H],
            "measured_viewport": [win.get("client_w"), win.get("client_h")],
            "width_ok": w_ok, "height_ok": h_ok, "visible": vis,
            "reason": None if ok else ("PAGE_NOT_VISIBLE" if not vis else "VIEWPORT_TOO_SMALL_FOR_RIGHT_PRICE_COLUMN")}


def capture_region(client: Any, sid: str, key: str, selector: str, deps: list[str], out_dir: Path) -> dict[str, Any]:
    """Box/hit read and screenshot under ONE receipt; a second box read under a new receipt must match."""
    info: dict[str, Any] = {"region": key, "selector": selector, "evidence_valid": False}
    box_js = viewport_box_js(selector)
    sync = strict_sync(client, sid)
    info["receipt_sync"] = {k: sync.get(k) for k in ("ok", "context_id", "error")}
    if not sync["ok"]:
        info["failure"] = "SYNC_FAILED"
        return info
    b1 = dep_eval(client, sid, box_js, deps, ctx=sync["context_id"])
    info["box_read_1"] = {k: b1.get(k) for k in ("ok", "failure", "error")}
    if not b1["ok"] or not isinstance(b1["value"], dict) or not b1["value"].get("ok"):
        info["failure"] = "BOX_READ_REFUSED" if not b1["ok"] else "BOX_ELEMENT_MISSING"
        return info
    v1 = b1["value"]
    hit = (v1.get("hit") or {}).get("status")
    info["visibility"] = hit
    info["viewport"] = v1.get("viewport")
    info["box"] = v1["box"]
    info["fully_in_viewport"] = v1.get("fully_in_viewport")
    info["price_nodes_at_shot"] = v1.get("price_nodes")
    if not v1.get("fully_in_viewport"):
        # No scrolling and no retry in this diagnostic: record coordinates and stop.
        info["failure"] = "REGION_NOT_FULLY_IN_VIEWPORT"
        info["evidence_invalid_reason"] = "区域未完整进入视口（未滚动、未重试）"
        return info
    vp = out_dir / f"viewport_{key}.png"
    shot = client.run(["screenshot", "--session", sid, "--out", str(vp), "--context", str(sync["context_id"])], timeout=120)
    info["screenshot"] = {"ok": bool(shot.get("ok")), "receipt_context": sync["context_id"], "path": str(vp) if vp.is_file() else None,
                          "error": None if shot.get("ok") else _err_summary(shot)}
    if not shot.get("ok") or not vp.is_file():
        info["failure"] = "SCREENSHOT_FAILED"
        return info
    b2 = dep_eval(client, sid, box_js, deps)
    info["box_read_2"] = {k: b2.get(k) for k in ("ok", "failure", "error")}
    if not b2["ok"] or not isinstance(b2["value"], dict) or not b2["value"].get("ok"):
        info["failure"] = "BOX_RECHECK_REFUSED"
        return info
    v2 = b2["value"]
    same_box = all(abs(float(v1["box"][k]) - float(v2["box"][k])) <= 2 for k in ("x", "y", "w", "h"))
    info["box"] = v1["box"]
    info["box_recheck_same"] = same_box
    info["visibility_recheck"] = (v2.get("hit") or {}).get("status")
    info["text_at_shot"] = v1.get("text")
    if hit != "VISIBLE" or info["visibility_recheck"] != "VISIBLE":
        info["failure"] = {"LOGIN_OVERLAY": "LOGIN_OVERLAY", "OCCLUDED": "OCCLUDED"}.get(hit or "", "NOT_VISIBLE")
        info["evidence_invalid_reason"] = bd.EVIDENCE_REASON_ZH.get(hit or "UNCONFIRMED") or "可见性未确认"
        return info
    if not same_box or v1.get("text") != v2.get("text"):
        info["failure"] = "REGION_MOVED_OR_CHANGED_AROUND_SHOT"
        return info
    if not v2.get("fully_in_viewport"):
        info["failure"] = "REGION_LEFT_VIEWPORT_AROUND_SHOT"
        return info
    crop = out_dir / f"region_{key}.png"
    dpr = float(v1.get("dpr") or 1)
    from PIL import Image  # noqa: PLC0415

    with Image.open(vp) as im:
        info["screenshot_size_px"] = [im.width, im.height]
    b = v1["box"]
    need = [int((float(b["x"]) + float(b["w"])) * dpr), int((float(b["y"]) + float(b["h"])) * dpr)]
    info["crop_needs_px"] = need
    if need[0] > info["screenshot_size_px"][0] or need[1] > info["screenshot_size_px"][1]:
        info["failure"] = "REGION_CLIPPED_IN_SCREENSHOT"
        return info
    c = bd._crop_viewport_to_dom_box(vp, crop, b, device_pixel_ratio=dpr, pad_px=0)
    info["crop"] = c
    if not c.get("ok"):
        info["failure"] = "CROP_FAILED"
        return info
    exp = [int(float(b["w"]) * dpr), int(float(b["h"]) * dpr)]
    if abs(c["out_size_px"][0] - exp[0]) > 2 or abs(c["out_size_px"][1] - exp[1]) > 2:
        info["failure"] = "CROP_CLAMPED"
        return info
    info["image"] = str(crop)
    info["evidence_valid"] = True
    return info


MODEL_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _model_re(model: str) -> re.Pattern[str]:
    if model not in MODEL_RE_CACHE:
        MODEL_RE_CACHE[model] = re.compile(r"(?<![0-9A-Z])" + re.escape(model.upper()) + r"(?![0-9A-Z])")
    return MODEL_RE_CACHE[model]


def judge(business: dict[str, Any], *, title: str | None, sel_state: dict[str, Any], price_status: dict[str, Any],
          shots: dict[str, Any]) -> dict[str, Any]:
    """Only page evidence of THIS run; never titles of other pages, old screenshots or history prices."""
    model = str(business["expected_model"]).upper()
    spec_text = str(sel_state.get("selected_spec_text") or "")
    tok_spec = ss.parse_spec_tokens(spec_text)
    tok_title = ss.parse_spec_tokens(title)
    facts: dict[str, Any] = {
        "model_in_selected_spec": bool(_model_re(model).search(spec_text.upper())),
        "model_in_title": bool(_model_re(model).search(str(title or "").upper())),
        "selected_spec_text": spec_text or None, "title": title,
        "spec_tokens": tok_spec, "title_tokens": tok_title,
    }

    def pick(field: str) -> tuple[Any, str | None]:
        if tok_spec.get(field) is not None:
            return tok_spec[field], "selected_spec"
        if tok_title.get(field) is not None:
            return tok_title[field], "title"
        return None, None

    layers, layers_src = pick("layers")
    sheets, sheets_src = pick("sheets_or_pulls")
    unit = tok_spec.get("sheet_unit") if sheets_src == "selected_spec" else tok_title.get("sheet_unit")
    packs = tok_spec.get("packs")  # pack count must come from the SELECTED option (72包 etc. elsewhere ≠ a box)
    allowed_units = {str(business.get("packaging_basis", {}).get("sheet_unit_business") or "")} | set(business.get("sheet_unit_aliases") or [])
    checks = {
        "model": facts["model_in_selected_spec"] or facts["model_in_title"],
        "layers": layers is not None and int(layers) == int(business["layers"]),
        "sheets": sheets is not None and int(sheets) == int(business["sheets_or_pulls"]),
        "sheet_unit": unit in allowed_units,
        "packs_selected": packs is not None and int(packs) == int(business["packs_per_box"]),
        "price_role_resolved": price_status.get("status") == "RESOLVED" and not price_status.get("spec_binding"),
        "screenshots_valid": all((shots.get(k) or {}).get("evidence_valid") is True for k in SHOT_REGIONS),
    }
    facts.update({"layers": layers, "layers_source": layers_src, "sheets": sheets, "sheets_source": sheets_src,
                  "sheet_unit": unit, "allowed_units": sorted(u for u in allowed_units if u), "packs_selected": packs})
    missing = [k for k, v in checks.items() if not v]
    out = {"facts": facts, "checks": checks, "missing": missing, "red_line": str(business["red_line"])}
    if missing:
        out.update({"verdict": "REVIEW_REQUIRED", "verdict_zh": "待复核（本次证据不齐）", "compared": False})
        return out
    cmp_ = __import__("p4b_compare_from_ocr").compare_to_red_line(
        price_status.get("resolved_amount"), box_comparable=True, selection_status="SELECTED",
        red_line=Decimal(str(business["red_line"])))
    out.update({"compared": True, "comparison": cmp_, "price": price_status.get("resolved_amount"),
                "price_label": price_status.get("resolved_label"), "verdict": cmp_["verdict"],
                "verdict_zh": {"BREAK_PRICE": "破价", "NOT_BREAK": "未破价"}.get(cmp_["verdict"], "待复核")})
    return out


def run_diag(client: Any, *, url: str, product_id: str, business: dict[str, Any], out_dir: Path,
             browser: str | None = None, test_hook: Callable[[str, Any, str], None] | None = None,
             precheck_url: str | None = None, precheck_dep: str = "#x",
             window_size: tuple[int, int] | None = None) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=False)  # never overwrite an earlier run
    res: dict[str, Any] = {"diag_version": DIAG_VERSION, "started_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           "url": url, "product_id": product_id, "business_sku": business.get("business_sku"),
                           "business_rules": {k: business.get(k) for k in ("expected_model", "layers", "sheets_or_pulls", "sheet_unit_aliases",
                                                                            "packs_per_box", "red_line", "comparison_unit")},
                           "policy": "strict", "navigations": 0, "stages": {}, "failed_stage": None,
                           "uses_unverified": False, "uses_observation_on_eval": False}

    def stage(name: str, passed: bool, **kw: Any) -> bool:
        ok = bool(passed)
        kw.pop("ok", None)
        res["stages"][name] = {"ok": ok, **rt.sanitize(kw)}
        if not ok and not res["failed_stage"]:
            res["failed_stage"] = name
        return ok

    def hook(name: str) -> None:
        if test_hook:
            test_hook(name, client, sid)

    def finish() -> dict[str, Any]:
        if res.get("judgement") is None:
            res["judgement"] = {"verdict": "REVIEW_REQUIRED", "verdict_zh": "待复核", "compared": False,
                                "reason": f"stopped at {res['failed_stage']}"}
        res["finished_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        bd.write_json(out_dir / "result.json", res)
        return res

    pf = preflight(client)
    if not stage("PREFLIGHT", pf["ok"], **pf):
        return finish()
    if window_size:
        # Diagnostic-only explicit launch size for the NEW Agent Window; measured separately below.
        st_args = ["session", "start", "--no-focus", "--browser", str(browser or pf["browser_instance"]),
                   "--name", "priceradar-depends-on-diag", "--width", str(int(window_size[0])), "--height", str(int(window_size[1]))]
        st_raw = client.run(st_args)
        st_d = st_raw.get("data") if isinstance(st_raw.get("data"), dict) else {}
        sess = {"session_id": st_d.get("session_id") or st_d.get("id") or st_d.get("session"), "raw": rt.sanitize(st_raw)}
        res["launch_window_size_requested"] = list(window_size)
    else:
        sess = rt.start_session(client, browser or pf["browser_instance"], focus=False)
    sid = sess.get("session_id")
    res["session_id"] = sid
    try:
        if not stage("SESSION", bool(sid), raw=None if sid else sess.get("raw")):
            return finish()
        pre_tab = None
        if precheck_url:
            # Same session / same Agent Window as the detail navigation; a LOCAL page, not counted as a detail navigation.
            pn = client.run(["navigate", precheck_url, "--session", sid, "--wait-until", "load", "--timeout", "30s"], timeout=60)
            res["precheck_navigations"] = 1
            pre_tab = (pn.get("data") or {}).get("tab_id") if isinstance(pn.get("data"), dict) else None
            time.sleep(1.0)
            ps = strict_sync(client, sid)
            win = None
            if pn.get("ok") and ps["ok"]:
                pr = dep_eval(client, sid, WINDOW_STATE_JS, [precheck_dep], ctx=ps["context_id"])
                win = pr["value"] if pr["ok"] else {"error": pr.get("error")}
            gate = window_gate(win)
            if not stage("WINDOW_PRECHECK", bool(pn.get("ok")) and pre_tab is not None and ps["ok"] and gate["ok"], tab_id=pre_tab, window=win, gate=gate,
                         error=None if pn.get("ok") else _err_summary(pn)):
                return finish()
        nav_args = ["navigate", url, "--session", sid, "--wait-until", "load", "--timeout", "60s"]
        if precheck_url:
            # The tab was synchronized during the precheck: BSK requires the latest context for navigate.
            ns = strict_sync(client, sid)
            res["pre_navigate_sync"] = {k: ns.get(k) for k in ("ok", "context_id", "error")}
            if not stage("PRE_NAVIGATE_SYNC", ns["ok"], **ns):
                return finish()
            nav_args += ["--context", str(ns["context_id"])]
        nav = client.run(nav_args, timeout=90)
        res["navigations"] += 1
        nav_tab = (nav.get("data") or {}).get("tab_id") if isinstance(nav.get("data"), dict) else None
        if not stage("NAVIGATE", bool(nav.get("ok")) and (not precheck_url or (pre_tab is not None and nav_tab == pre_tab)),
                     final_url=(nav.get("data") or {}).get("final_url") if isinstance(nav.get("data"), dict) else None,
                     tab_id=nav_tab, same_tab_as_precheck=None if pre_tab is None else nav_tab == pre_tab,
                     error=None if nav.get("ok") else _err_summary(nav)):
            return finish()
        time.sleep(2.0)
        s0 = strict_sync(client, sid)
        if not stage("INITIAL_SYNC", s0["ok"], **s0):
            return finish()
        deps = discover_dependencies(client, sid, s0["context_id"])
        res["depends_on"] = deps["depends_on"]
        if not stage("DEPENDENCY_DISCOVERY", deps["ok"], **{k: v for k, v in deps.items() if k != "ok"}):
            return finish()
        dl = deps["depends_on"]
        hook("after_dependency_discovery")
        r = dep_eval(client, sid, bd.CLASSIFY_PAGE_JS, dl)
        page_info = r["value"] if r["ok"] and isinstance(r["value"], dict) else {}
        ident = bd.identity_from_page(page_info, candidate_product_id=product_id) if r["ok"] else None
        if not stage("IDENTITY", bool(ident and ident.get("ok")), read=r, identity=ident,
                     session_block=bd.classify_session_block(page_info) if page_info else None):
            return finish()
        r = dep_eval(client, sid, bd.LOGIN_WALL_JS, dl)
        if not stage("LOGIN_CHECK_START", r["ok"] and not (r["value"] or {}).get("login_wall"), read=r,
                     login_wall=(r["value"] or {}).get("login_wall") if r["ok"] else None):
            return finish()
        hook("after_login_check_start")
        sa = dep_eval(client, sid, bd.SELECTION_STATE_JS, dl)
        pa = dep_eval(client, sid, bd.PRICE_ROLE_EVIDENCE_JS, dl)
        if not stage("READ_A", sa["ok"] and pa["ok"] and isinstance(sa["value"], dict) and isinstance(pa["value"], dict),
                     selection=sa, price_probe=pa):
            return finish()
        hook("after_read_a")
        shots: dict[str, Any] = {}
        for key in SHOT_REGIONS:
            shots[key] = capture_region(client, sid, key, deps["regions"][key]["selector"], dl, out_dir)
            if not shots[key].get("evidence_valid"):
                break
        res["shots"] = shots
        if not stage("SCREENSHOT", all((shots.get(k) or {}).get("evidence_valid") for k in SHOT_REGIONS),
                     failures={k: v.get("failure") for k, v in shots.items() if v.get("failure")}):
            return finish()
        hook("after_screenshot")
        sb = dep_eval(client, sid, bd.SELECTION_STATE_JS, dl)
        pb = dep_eval(client, sid, bd.PRICE_ROLE_EVIDENCE_JS, dl)
        if not stage("READ_B", sb["ok"] and pb["ok"] and isinstance(sb["value"], dict) and isinstance(pb["value"], dict),
                     selection=sb, price_probe=pb):
            return finish()
        r = dep_eval(client, sid, bd.LOGIN_WALL_JS, dl)
        if not stage("LOGIN_CHECK_END", r["ok"] and not (r["value"] or {}).get("login_wall"), read=r,
                     login_wall=(r["value"] or {}).get("login_wall") if r["ok"] else None):
            return finish()
        # ---- application-level A/B comparison (BSK does not see class-only selection markers) ----
        sel_a, sel_b = sa["value"], sb["value"]
        spec_cmp = ss.compare_spec_selection(sel_a.get("spec_selection"), sel_b.get("spec_selection"), expected_product_id=product_id)
        try:
            ocr_amts, ocr_texts = _ocr_amounts(Path(shots["price"]["image"]))
        except Exception as exc:  # noqa: BLE001
            ocr_amts, ocr_texts = set(), [f"OCR_FAILED: {exc}"]
        _, price_st = th.resolve_price_roles_from_dom_evidence(
            pa["value"], pb["value"], target_product_id=product_id,
            selected_spec_text=sel_a.get("selected_spec_text"), spec_selection=sel_a.get("spec_selection"),
            ocr_amounts=ocr_amts or None, evidence_source="dom_depends_on_diag",
            evidence_path=shots["price"]["image"])
        title_a = (deps["regions"]["title"].get("observed_text") or "").strip()
        title_shot = str(shots["title"].get("text_at_shot") or "").strip()
        title_same = re.sub(r"\s+", " ", title_a) == re.sub(r"\s+", " ", title_shot)
        same_pid = str(sel_a.get("url_product_id") or "") == str(sel_b.get("url_product_id") or "") == str(product_id)
        ocr_joined = re.sub(r"\s+", "", "".join(ocr_texts))
        amt = price_st.get("resolved_amount")
        label = re.sub(r"\s+", "", str(price_st.get("resolved_label") or ""))
        amount_in_shot = bool(amt) and any(Decimal(a) == Decimal(amt) for a in ocr_amts if re.fullmatch(r"\d+(?:\.\d+)?", a))
        label_in_shot = bool(label) and label in ocr_joined
        pn = shots["price"].get("price_nodes_at_shot") or []

        def _node_ok(kind: str, match: Callable[[str], bool]) -> dict[str, Any]:
            hits = [n for n in pn if n.get("kind") == kind and match(str(n.get("text") or ""))]
            return {"found": len(hits), "ok": len(hits) == 1 and hits[0].get("fully_in_viewport") is True and hits[0].get("hit") == "VISIBLE",
                    "nodes": hits}

        amt_node = _node_ok("amount", lambda t: bool(amt) and bool(re.search(r"\d+(?:\.\d+)?", t))
                            and Decimal(re.search(r"\d+(?:\.\d+)?", t).group(0)) == Decimal(amt))
        lbl_node = _node_ok("label", lambda t: bool(label) and re.sub(r"\s+", "", t) == label)
        price_ocr_ok = (price_st.get("status") == "RESOLVED" and amount_in_shot and label_in_shot
                        and amt_node["ok"] and lbl_node["ok"])
        compare_ok = spec_cmp["status"] == "MATCH" and same_pid and title_same and price_ocr_ok
        if not stage("COMPARE", compare_ok, spec_compare=spec_cmp, same_product_id=same_pid, title_same=title_same,
                     amount_in_screenshot=amount_in_shot, label_in_screenshot=label_in_shot,
                     amount_node_at_shot=amt_node, label_node_at_shot=lbl_node,
                     price_role=price_st, ocr_amounts=sorted(ocr_amts), ocr_texts=ocr_texts[:40]):
            res["judgement"] = {"verdict": "REVIEW_REQUIRED", "verdict_zh": "待复核", "compared": False,
                                "reason": "A/B 证据不一致或价格角色未唯一确定",
                                "spec_compare": spec_cmp["status"], "price_role": price_st.get("status")}
            return finish()
        res["judgement"] = judge(business, title=title_shot or title_a, sel_state=sel_a, price_status=price_st, shots=shots)
        stage("JUDGE", res["judgement"].get("compared") is True, missing=res["judgement"].get("missing"))
        return finish()
    finally:
        if sid:
            rt.stop_session(client, sid)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--enable-depends-on-diagnostic", action="store_true", required=True)
    ap.add_argument("--product-id", required=True)
    ap.add_argument("--url", default=None)
    ap.add_argument("--business-json", required=True, type=Path)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--window-precheck-url", default=None,
                    help="LOCAL static page opened first in the SAME session/tab to measure the usable viewport")
    ap.add_argument("--window-width", type=int, default=None, help="diagnostic-only launch width of the new Agent Window")
    ap.add_argument("--window-height", type=int, default=None, help="diagnostic-only launch height of the new Agent Window")
    a = ap.parse_args(argv)
    business = json.loads(a.business_json.read_text(encoding="utf-8"))
    url = a.url or f"https://item.jd.com/{a.product_id}.html"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = a.out_dir or (DIAG_ROOT / f"{stamp}_{a.product_id}")
    res = run_diag(rt.BskClient(), url=url, product_id=a.product_id, business=business, out_dir=out,
                   precheck_url=a.window_precheck_url,
                   window_size=(a.window_width, a.window_height) if a.window_width and a.window_height else None)
    print(json.dumps({"out_dir": str(out), "failed_stage": res["failed_stage"], "navigations": res["navigations"],
                      "judgement": {k: res["judgement"].get(k) for k in ("verdict", "verdict_zh", "compared", "missing", "price", "price_label", "reason")}},
                     ensure_ascii=False, indent=1))
    return 0 if res["failed_stage"] is None else 2


if __name__ == "__main__":
    raise SystemExit(main())
