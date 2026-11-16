"""
P5-K: one-shot detail "product parameters" evidence probe (BSK, read-mostly).

v2 (heading-scoped):
- Parameters are parsed ONLY inside a block headed 规格参数 / 商品参数 /
  规格与包装 / 产品参数 / 参数信息. Unrelated key/value text elsewhere on the
  page (navigation, delivery, services) can never make the probe "succeed".
- Four separate states are recorded:
    summary_found          a headed parameter block exists
    expand_entry_found     an explicit 查看全部参数-style control INSIDE that block
    expand_confirmed       after at most ONE click, parameter content strictly
                           larger than the summary appeared (same product id)
    required_fields_found  explicit fields for layers / sheets-per-pack / packs
  If the expansion is not confirmed, the result is "全文未读到" - never
  "完整参数里没有".
- Viewing actions only: stepwise scroll; at most one tab click (only when no
  summary is found, unchanged from v1); at most one expand click. Never changes
  the selected spec, never searches, no retries beyond a single settled resync
  per read on a sync refusal.
- Runs inside the existing BSK detail capture via extra_capture (one detail
  navigation, same session) BEFORE the "after" selection / price snapshots.
- Raw evidence only; no binding or price judgement is made here.

Usage:
  python scripts/p5k_detail_params_probe.py --product-id <JD id> --browser <bsk id> [--out-dir DIR]
  python scripts/p5k_detail_params_probe.py --self-check
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import p5_bsk_detail as bd  # noqa: E402
import p5_bsk_runtime as rt  # noqa: E402

PROBE_ROOT = PROJECT_ROOT / "artifacts" / "param_probes"
RULES_VERSION = "p5k-params-probe-v2.3"
MAX_SCROLL_STEPS = 10
TAB_TEXTS = ("规格与包装", "商品参数", "规格参数", "参数")
HEADINGS = ("规格参数", "商品参数", "规格与包装", "产品参数", "参数信息")
EXPAND_TEXTS = ("查看全部参数", "查看更多参数", "全部参数", "更多参数", "展开全部参数")

# Pure text parser (unit-tested with node).
PARSE_LINES_JS = r"""
function parseLines(text) {
  const out = [];
  const lines = String(text || '').split(/\n+/).map(s => s.replace(/\s+/g, ' ').trim()).filter(Boolean);
  for (let i = 0; i < lines.length; i++) {
    const m = lines[i].match(/^([^：:]{1,14})[：:]\s*(.+)$/);
    if (m) { out.push({ k: m[1].trim(), v: m[2].trim(), src: 'colon' }); continue; }
  }
  return out;
}
"""

PARAMS_SCAN_JS = r"""(() => {
  """ + PARSE_LINES_JS + r"""
  const HEADINGS = %s;
  const EXPANDS = %s;
  const norm = s => String(s || '').replace(/[ \t\r\f\v]+/g, ' ').replace(/\n\s*/g, '\n').trim();
  const one = s => norm(s).replace(/\s+/g, ' ');
  const visible = e => { const r = e.getBoundingClientRect(); const st = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden'; };
  const short = s => s && s.length <= 14;
  const pairsOf = (el, headingText) => {
    const pairs = [];
    el.querySelectorAll('dl').forEach(dl => {
      const dts = dl.querySelectorAll('dt'), dds = dl.querySelectorAll('dd');
      for (let i = 0; i < Math.min(dts.length, dds.length); i++) pairs.push({ k: one(dts[i].innerText), v: one(dds[i].innerText), src: 'dl' });
    });
    el.querySelectorAll('tr').forEach(tr => {
      const c = tr.querySelectorAll('th,td');
      if (c.length >= 2) pairs.push({ k: one(c[0].innerText), v: one(c[1].innerText), src: 'tr' });
    });
    el.querySelectorAll('*').forEach(n => {
      const kids = Array.from(n.children);
      if (kids.length === 2 && kids.every(k => k.children.length <= 1)) {
        const a = one(kids[0].innerText), b = one(kids[1].innerText);
        if (short(a) && b && b.length <= 120 && !/[：:]/.test(a)) pairs.push({ k: a, v: b, src: 'pair' });
      }
    });
    parseLines(el.innerText).forEach(p => pairs.push(p));
    const seen = new Set();
    return pairs.filter(p => p.k && p.v && p.k !== headingText && !EXPANDS.includes(p.v) && !EXPANDS.includes(p.k)
      && !seen.has(p.k + '|' + p.v) && seen.add(p.k + '|' + p.v));
  };
  const chain = el => { const out = []; let n = el; for (let i = 0; i < 6 && n; i++) { out.push(String(n.className || n.tagName).slice(0, 60)); n = n.parentElement; } return out; };
  const pid = (location.pathname.match(/(\d+)\.html/) || [])[1] || null;
  document.querySelectorAll('[data-pr-expand]').forEach(x => x.removeAttribute('data-pr-expand'));
  document.querySelectorAll('[data-pr-region="params"]').forEach(x => x.removeAttribute('data-pr-region'));
  const heads = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,div,span,p,li,a,dt,th'))
    .filter(e => HEADINGS.includes(one(e.innerText)) && visible(e));
  const blocks = [];
  const used = new Set();
  for (const h of heads) {
    const headingText = one(h.innerText);
    // Smallest ancestor (<= 6 levels) that holds this heading plus >= 1 pair, and is not page-wide.
    let n = h.parentElement, block = null;
    for (let i = 0; i < 6 && n; i++, n = n.parentElement) {
      const t = norm(n.innerText);
      if (t.length > 4000) break;
      const kv = pairsOf(n, headingText);
      if (kv.length >= 1) { block = { el: n, kv, t }; break; }
    }
    if (!block || used.has(block.el)) continue;
    used.add(block.el);
    const expRaw = Array.from(block.el.querySelectorAll('a,span,div,button,em,i,p'))
      .filter(e => EXPANDS.includes(one(e.innerText)) && e.children.length <= 2 && visible(e));
    // Nested wrappers with identical text are ONE control: keep the innermost.
    const exp = expRaw.filter(e => !expRaw.some(o => o !== e && e.contains(o)));
    blocks.push({ el: block.el, heading: headingText, kv: block.kv, t: block.t, expand: exp, expand_raw: expRaw.length,
      in_dialog: !!block.el.closest('[role=dialog],[class*=modal],[class*=Modal],[class*=drawer],[class*=Drawer],[class*=popup],[class*=Popup],[class*=dialog],[class*=Dialog],[class*=layer]') });
  }
  // Keep innermost blocks only: a container (e.g. found via a tab-bar heading)
  // that holds another parameter block must not outrank the real block.
  const inner = blocks.filter(b => !blocks.some(o => o !== b && b.el.contains(o.el)));
  blocks.length = 0; inner.forEach(b => blocks.push(b));
  blocks.sort((a, b) => b.kv.length - a.kv.length);
  if (blocks.length) blocks[0].el.setAttribute('data-pr-region', 'params');
  let expandSel = null;
  const withExp = blocks.find(b => b.expand.length === 1);
  if (withExp) { withExp.expand[0].setAttribute('data-pr-expand', '1'); expandSel = '[data-pr-expand="1"]'; }
  const tabs = Array.from(document.querySelectorAll('li,a,div,span,button'))
    .filter(e => /^(商品介绍|规格与包装|规格参数|商品参数|参数|包装清单|售后保障)$/.test(one(e.innerText)) && e.children.length <= 2)
    .slice(0, 20).map(e => ({ text: one(e.innerText), active: /active|current|selected|curr|on\b/i.test(String(e.className || '')) }));
  const selected = Array.from(document.querySelectorAll('.specification-item-sku--selected')).map(n => one(n.innerText)).filter(Boolean);
  return {
    ok: true,
    url_product_id: pid,
    body_text_len: (document.body.innerText || '').length,
    tabs,
    selected_spec_nodes_now: selected,
    expand_selector: expandSel,
    blocks: blocks.slice(0, 6).map((b, i) => {
      const r = b.el.getBoundingClientRect();
      return { rank: i + 1, heading: b.heading, text: b.t.slice(0, 6000), text_len: b.t.length, kv: b.kv.slice(0, 120),
               kv_count: b.kv.length, contains_url_pid: !!(pid && b.t.includes(pid)), in_dialog: b.in_dialog,
               expand_entries: b.expand.map(e => one(e.innerText)), expand_raw_matches: b.expand_raw,
               ancestor_classes: chain(b.el),
               box: { x: r.x, y: r.y, w: r.width, h: r.height } };
    }),
  };
})()""" % (json.dumps(list(HEADINGS), ensure_ascii=False), json.dumps(list(EXPAND_TEXTS), ensure_ascii=False))

SCROLL_STEP_JS = r"""(() => { window.scrollBy(0, Math.floor(window.innerHeight * 0.9));
  const se = document.scrollingElement || document.documentElement;
  return { ok: true, y: window.scrollY, max: se.scrollHeight - window.innerHeight }; })()"""

MARK_TAB_JS = r"""(() => {
  const want = %s;
  const norm = s => String(s || '').replace(/\s+/g, ' ').trim();
  document.querySelectorAll('[data-pr-tab]').forEach(x => x.removeAttribute('data-pr-tab'));
  const hits = Array.from(document.querySelectorAll('li,a,div,span,button'))
    .filter(e => want.includes(norm(e.innerText)) && e.children.length <= 2)
    .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
  if (hits.length !== 1) return { ok: false, reason: hits.length ? 'ambiguous_tab' : 'no_tab', count: hits.length };
  hits[0].setAttribute('data-pr-tab', '1');
  hits[0].scrollIntoView({ block: 'center' });
  return { ok: true, text: norm(hits[0].innerText), selector: '[data-pr-tab="1"]' };
})()""" % json.dumps(list(TAB_TEXTS), ensure_ascii=False)

FIELD_RULES = {
    "layers": re.compile(r"层数|^层$"),
    "sheets_per_pack": re.compile(r"(每包|单包).*(张|抽|数)|张数|抽数|(张|抽)/包"),
    "packs_per_box": re.compile(r"包数|每箱|箱规|整箱|件数|装箱"),
}


def required_fields(kv: list[dict[str, Any]]) -> dict[str, Any]:
    """Explicit fields only (raw key/value kept); no inference of scope or unit."""
    out: dict[str, Any] = {}

    def scope(name: str, k: str, v: str) -> bool:
        # Explicit measurement scope only: never infer per-pack / per-box.
        if name == "sheets_per_pack":
            return "包" in k or "/包" in v or "每包" in v
        if name == "packs_per_box":
            return "箱" in k or "箱" in v
        return True

    for name, rx in FIELD_RULES.items():
        hits = [p for p in kv if rx.search(str(p.get("k") or ""))]
        out[name] = [
            {"key": p.get("k"), "value": p.get("v"), "src": p.get("src"),
             "scope_explicit": scope(name, str(p.get("k") or ""), str(p.get("v") or ""))}
            for p in hits
        ]
    # Values that merely contain spec tokens under a generic key (e.g. 规格) are listed separately.
    out["spec_like_values"] = [
        {"key": p.get("k"), "value": p.get("v")}
        for p in kv
        if re.search(r"\d+\s*(层|张|抽|包|箱)", str(p.get("v") or "")) and not any(
            FIELD_RULES[n].search(str(p.get("k") or "")) for n in FIELD_RULES
        )
    ]
    return out


def _top(scan: dict[str, Any] | None) -> dict[str, Any] | None:
    return ((scan or {}).get("blocks") or [None])[0]


def _eval(client: Any, session_id: str, expr: str, log: list[dict[str, Any]], stage: str) -> tuple[Any, dict[str, Any] | None]:
    """Read with at most one settled resync on a sync-required refusal."""
    res = rt.eval_json(client, session_id, expr)
    err = rt._bsk_error_payload(res)
    resynced = False
    if err and rt.is_sync_required_error(err):
        s = rt.cooperate_sync(client, session_id, settle=True)
        resynced = True
        res = rt.eval_json(client, session_id, expr, context_id=s.get("context_id"), observation_id=s.get("observation_id"))
        err = rt._bsk_error_payload(res)
    log.append({"stage": stage, "ok": not err, "resynced": resynced, "error": rt.sanitize(err)})
    val = res.get("value") if not err and isinstance(res.get("value"), dict) else None
    return val, err


def _viewport_shot(client: Any, session_id: str, out_path: Path) -> dict[str, Any]:
    """Plain viewport screenshot of the current (expanded) state; raw result kept."""
    shot = rt.screenshot_session(client, session_id, out_path)
    raw = shot.get("raw") if isinstance(shot.get("raw"), dict) else {}
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    return {
        "ok": bool(shot.get("ok")),
        "expected_path": str(out_path),
        "file_saved": out_path.is_file(),
        "exit_code": raw.get("exit_code"),
        "error_code": data.get("code"),
        "error_reason": (data.get("data") or {}).get("reason") if isinstance(data.get("data"), dict) else data.get("reason"),
        "error_message": str(data.get("message") or "")[:200] or None,
    }


def params_capture(*, client: Any, session_id: str, out_dir: Path, cooperate_policy: str, product_id: str) -> dict[str, Any]:
    log: list[dict[str, Any]] = []
    scans: list[dict[str, Any]] = []
    actions: dict[str, Any] = {"scroll_steps": 0, "tab_clicks": 0, "expand_clicks": 0}
    state = {"summary_found": False, "expand_entry_found": False, "expand_clicked": False,
             "expand_confirmed": False, "full_text_read": False, "required_fields_found": False,
             "login_required": False}
    summary = None
    stopped_on_error = None
    login_checks: list[dict[str, Any]] = []

    def login_stop(stage: str) -> dict[str, Any] | None:
        lw = bd.check_login_wall(client, session_id, cooperate_policy=cooperate_policy)
        login_checks.append({"stage": stage, "login_wall": lw.get("login_wall"), "checked": lw.get("checked"),
                             "covering": lw.get("covering")})
        if not lw.get("login_wall"):
            return None
        state["login_required"] = True
        conclusion = f"需要登录（{stage}检测到登录弹窗）：全文未读到"
        payload = {"rules_version": RULES_VERSION, "product_id": product_id,
                   "captured_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "state": state, "conclusion": conclusion, "actions": actions, "login_checks": login_checks,
                   "summary_block": _top(summary) if summary else None, "scans": scans, "eval_log": log,
                   "note": "登录拦截：不点击、不关闭弹窗、不自动登录；本次参数结果不进入比价验收"}
        bd.write_json(out_dir / "params_probe.json", payload)
        return {"conclusion": conclusion, "state": state, "actions": actions, "crop_status": None, "viewport_shot_ok": None}

    stop = login_stop("探针开始")
    if stop:
        return stop

    def scan(stage: str) -> dict[str, Any] | None:
        nonlocal stopped_on_error
        v, err = _eval(client, session_id, PARAMS_SCAN_JS, log, stage)
        if err and not rt.is_sync_required_error(err):
            stopped_on_error = rt.sanitize(err)
        if v:
            t = _top(v)
            scans.append({"stage": stage, "n_blocks": len(v.get("blocks") or []), "top_heading": (t or {}).get("heading"),
                          "top_kv_count": (t or {}).get("kv_count"), "expand_selector": v.get("expand_selector"),
                          "selected_spec_nodes_now": v.get("selected_spec_nodes_now"), "url_product_id": v.get("url_product_id")})
        return v

    # 1) Find a headed parameter summary (scroll stepwise; v1 stop conditions kept).
    for step in range(MAX_SCROLL_STEPS + 1):
        v = scan(f"scan_{step}")
        if stopped_on_error:
            break
        if v and _top(v):
            summary = v
            break
        if step == MAX_SCROLL_STEPS:
            break
        _eval(client, session_id, SCROLL_STEP_JS, log, f"scroll_{step}")
        actions["scroll_steps"] += 1
        client.run(["wait-ms", "700"], timeout=15)
    if not summary and not stopped_on_error:
        tab, _ = _eval(client, session_id, MARK_TAB_JS, log, "mark_tab")
        if tab and tab.get("ok"):
            rt.run_page_action(client, session_id, ["click", "--session", session_id, "--selector", tab["selector"]], timeout=30)
            actions["tab_clicks"] = 1
            actions["tab_clicked_text"] = tab.get("text")
            client.run(["wait-ms", "1200"], timeout=15)
            v = scan("scan_after_tab")
            if v and _top(v):
                summary = v
        else:
            actions["tab_click_skipped"] = (tab or {}).get("reason") or "tab_mark_failed"

    final = summary
    sblock = _top(summary)
    if sblock:
        state["summary_found"] = True
        state["expand_entry_found"] = bool(summary.get("expand_selector"))
        fields_in_summary = required_fields(sblock.get("kv") or [])
        need_more = not all(any(h.get("scope_explicit") for h in fields_in_summary[n]) for n in FIELD_RULES)
        # 2) At most ONE expand click, only from an explicit entry inside the block.
        if need_more and state["expand_entry_found"] and not stopped_on_error:
            rt.run_page_action(client, session_id, ["click", "--session", session_id, "--selector", summary["expand_selector"]], timeout=30)
            actions["expand_clicks"] = 1
            state["expand_clicked"] = True
            client.run(["wait-ms", "1500"], timeout=15)
            stop = login_stop("点击查看全部参数后")
            if stop:
                return stop
            for i in range(3):  # re-read only (no further clicks)
                v = scan(f"scan_after_expand_{i}")
                b = _top(v)
                if b and int(b.get("kv_count") or 0) > int(sblock.get("kv_count") or 0) and (
                    b.get("contains_url_pid") or not sblock.get("contains_url_pid")
                ):
                    state["expand_confirmed"] = True
                    state["full_text_read"] = True
                    final = v
                    break
                if stopped_on_error:
                    break
                client.run(["wait-ms", "800"], timeout=15)
        elif not need_more:
            state["full_text_read"] = False  # summary already has the fields; full list not needed
    fblock = _top(final)
    fields = required_fields((fblock or {}).get("kv") or []) if fblock else {n: [] for n in FIELD_RULES}
    state["required_fields_found"] = bool(fblock) and all(
        any(h.get("scope_explicit") for h in fields.get(n) or []) for n in FIELD_RULES
    )

    # 3) Evidence screenshots: element crop (existing path) + plain viewport of current state.
    crop = None
    if fblock:
        crop = bd.capture_region_bsk(
            client, session_id, meta_key="params", mark_key="params", filename="params.png",
            discover={"status": "FOUND", "selector": '[data-pr-region="params"]', "text": fblock["text"][:240], "box": fblock.get("box")},
            out_path=out_dir / "params.png", cooperate_policy=cooperate_policy,
        )
        bd.write_json(out_dir / "region_params.json", crop)
    vp = _viewport_shot(client, session_id, out_dir / "params_viewport.png")

    if not state["summary_found"]:
        conclusion = "参数区块未找到"
    elif state["required_fields_found"]:
        conclusion = "已找到所需字段（绑定另行判断）"
    elif state["expand_confirmed"]:
        conclusion = "已确认读到展开全文，但未见全部所需字段"
    elif state["expand_clicked"]:
        conclusion = "已点击展开但未确认展开内容：全文未读到"
    elif state["expand_entry_found"]:
        conclusion = "发现展开入口但未点击：全文未读到"
    else:
        conclusion = "仅摘要，无展开入口：全文未读到"
    payload = {
        "rules_version": RULES_VERSION,
        "product_id": product_id,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "state": state,
        "conclusion": conclusion,
        "actions": actions,
        "stopped_on_error": stopped_on_error,
        "summary_block": sblock,
        "final_block": fblock,
        "required_fields": fields,
        "scans": scans,
        "eval_log": log,
        "screenshots": {"crop_status": (crop or {}).get("status"), "crop_viewport_shot": (crop or {}).get("viewport_shot"),
                        "crop_evidence_valid": (crop or {}).get("evidence_valid"),
                        "crop_evidence_invalid_reason": (crop or {}).get("evidence_invalid_reason"),
                        "crop_visibility": (crop or {}).get("visibility"),
                        "viewport": vp, "viewport_note": "整页视口图仅作诊断，不作为参数证据"},
        "login_checks": login_checks,
        "note": "raw evidence only; binding to current SKU/option and price is judged separately",
    }
    bd.write_json(out_dir / "params_probe.json", payload)
    return {"conclusion": conclusion, "state": state, "actions": actions,
            "crop_status": (crop or {}).get("status"), "viewport_shot_ok": vp.get("ok")}


# ---------------------------------------------------------------------------
# Offline checks (fake client; no browser)
# ---------------------------------------------------------------------------

def _blk(heading: str, kv: list[tuple[str, str]], *, pid: bool = True, expand: bool = False) -> dict[str, Any]:
    return {"rank": 1, "heading": heading, "text": heading, "text_len": 10, "kv": [{"k": k, "v": v, "src": "pair"} for k, v in kv],
            "kv_count": len(kv), "contains_url_pid": pid, "in_dialog": False, "expand_entries": ["查看全部参数"] if expand else [],
            "ancestor_classes": [], "box": {"x": 0, "y": 0, "w": 100, "h": 50}}


SUMMARY_KV = [("品牌", "唯洁雅"), ("商品编号", "1"), ("店铺", "多金清洁店"), ("适用场景", "家用")]
FULL_KV = SUMMARY_KV + [("层数", "2层"), ("每包张数", "50张"), ("每箱包数", "72包")]


def run_self_check() -> list[str]:
    fails: list[str] = []

    class Fake:
        def __init__(self, scans: list[Any]):
            self.calls: list[list[str]] = []
            self.scans = list(scans)

        def run(self, args, *, timeout=120):
            self.calls.append(list(args))
            if args[:2] == ["cooperate", "sync"]:
                return {"ok": True, "data": {"context_id": "C", "observation_id": "O"}}
            if args and args[0] == "evaluate":
                expr = args[3]
                if expr == PARAMS_SCAN_JS:
                    v = self.scans.pop(0) if self.scans else {"ok": True, "blocks": []}
                    if isinstance(v, dict) and v.get("code"):
                        return {"ok": False, "exit_code": 1, "data": v}
                    return {"ok": True, "data": {"value": v}}
                if expr == bd.LOGIN_WALL_JS:
                    wall = bool(self.walls.pop(0)) if getattr(self, "walls", None) else False
                    return {"ok": True, "data": {"value": {"ok": True, "login_wall": wall, "covering": {"markers": ["扫码登录", "短信登录"]} if wall else None}}}
                if expr == MARK_TAB_JS:
                    return {"ok": True, "data": {"value": {"ok": False, "reason": "no_tab"}}}
                return {"ok": True, "data": {"value": {"ok": True}}}
            return {"ok": True, "data": {}}

        def clicks(self, sel: str | None = None) -> int:
            return sum(1 for c in self.calls if c and c[0] == "click" and (sel is None or sel in c))

    import tempfile

    orig_cap = bd.capture_region_bsk
    orig_vp = globals()["_viewport_shot"]
    bd.capture_region_bsk = lambda *a, **k: {"status": "CAPTURED_FAKE"}
    globals()["_viewport_shot"] = lambda *a, **k: {"ok": True}
    try:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            # A) nav/delivery pairs but no parameter heading -> never success
            f = Fake([{"ok": True, "blocks": []}] * (MAX_SCROLL_STEPS + 2))
            r = params_capture(client=f, session_id="S", out_dir=out, cooperate_policy="strict", product_id="1")
            if r["state"]["summary_found"] or r["state"]["required_fields_found"] or r["conclusion"] != "参数区块未找到":
                fails.append(f"A unrelated pairs must not succeed: {r}")
            # B) summary only + expand entry -> exactly one expand click, expansion confirmed, fields found
            summ = {"ok": True, "blocks": [_blk("规格参数", SUMMARY_KV, expand=True)], "expand_selector": '[data-pr-expand="1"]'}
            full = {"ok": True, "blocks": [_blk("规格参数", FULL_KV)], "expand_selector": None}
            f = Fake([summ, full])
            r = params_capture(client=f, session_id="S", out_dir=out, cooperate_policy="strict", product_id="1")
            if not (r["state"]["expand_confirmed"] and r["state"]["required_fields_found"]) or f.clicks('[data-pr-expand="1"]') != 1:
                fails.append(f"B summary->expand->full: {r} clicks={f.clicks()}")
            # C) clicked but no larger content -> 全文未读到, no second click
            f = Fake([summ, summ, summ, summ])
            r = params_capture(client=f, session_id="S", out_dir=out, cooperate_policy="strict", product_id="1")
            if r["state"]["expand_confirmed"] or r["state"]["full_text_read"] or "全文未读到" not in r["conclusion"] or f.clicks() != 1:
                fails.append(f"C unconfirmed expansion must stay 全文未读到: {r} clicks={f.clicks()}")
            # D) summary already has all fields -> no click
            f = Fake([{"ok": True, "blocks": [_blk("规格参数", FULL_KV)], "expand_selector": None}])
            r = params_capture(client=f, session_id="S", out_dir=out, cooperate_policy="strict", product_id="1")
            if f.clicks() or not r["state"]["required_fields_found"]:
                fails.append(f"D fields in summary must not click: {r}")
            # E) summary without expand entry -> no click, 全文未读到
            f = Fake([{"ok": True, "blocks": [_blk("规格参数", SUMMARY_KV)], "expand_selector": None}])
            r = params_capture(client=f, session_id="S", out_dir=out, cooperate_policy="strict", product_id="1")
            if f.clicks() or r["conclusion"] != "仅摘要，无展开入口：全文未读到":
                fails.append(f"E summary-only no entry: {r}")
            # F) sync refusal on first scan -> one settled resync, then continues
            refusal = {"code": "permission_denied", "data": {"reason": "cooperation_sync_required"}}
            f = Fake([refusal, {"ok": True, "blocks": [_blk("规格参数", FULL_KV)], "expand_selector": None}])
            r = params_capture(client=f, session_id="S", out_dir=out, cooperate_policy="strict", product_id="1")
            if not r["state"]["required_fields_found"] or sum(1 for c in f.calls if c[:2] == ["cooperate", "sync"] and "--settle" in c) != 1:
                fails.append(f"F refusal-resync: {r}")
            # G) non-sync error -> stop, no clicks
            f = Fake([{"code": "rate_limited", "message": "x"}])
            r = params_capture(client=f, session_id="S", out_dir=out, cooperate_policy="strict", product_id="1")
            if f.clicks() or r["state"]["summary_found"]:
                fails.append(f"G non-sync error must stop without clicks: {r}")
        with tempfile.TemporaryDirectory() as td2:
            # J) login wall appears only after the expand click -> stop, no rescans, no crop
            f = Fake([summ])
            f.walls = [False, True]
            r = params_capture(client=f, session_id="S", out_dir=Path(td2), cooperate_policy="strict", product_id="1")
            scans_after = sum(1 for c in f.calls if c and c[0] == "evaluate" and c[3] == PARAMS_SCAN_JS)
            if not r["state"]["login_required"] or r["state"]["full_text_read"] or f.clicks() != 1 or scans_after != 1 or "需要登录" not in r["conclusion"]:
                fails.append(f"J login after click must stop: {r} clicks={f.clicks()} scans={scans_after}")
            # K) login wall at probe start -> nothing scanned or clicked
            f = Fake([summ])
            f.walls = [True]
            r = params_capture(client=f, session_id="S", out_dir=Path(td2), cooperate_policy="strict", product_id="1")
            if not r["state"]["login_required"] or f.clicks() or any(c and c[0] == "evaluate" and c[3] == PARAMS_SCAN_JS for c in f.calls):
                fails.append(f"K login at start must stop before scanning: {r}")
        # H) field matcher: '50张' under a generic key is NOT a per-pack field; '72包' alone NOT per-box
        fr = required_fields([{"k": "规格", "v": "2层50张"}, {"k": "包装", "v": "72包"}])
        if fr["sheets_per_pack"] or fr["layers"] or fr["packs_per_box"] or len(fr["spec_like_values"]) != 2:
            fails.append(f"H generic values must not become explicit fields: {fr}")
        # I) '包数=72包' is a pack count, but NOT explicitly per box
        fr = required_fields([{"k": "包数", "v": "72包"}, {"k": "张数", "v": "50张"}])
        if not fr["packs_per_box"] or fr["packs_per_box"][0]["scope_explicit"] or fr["sheets_per_pack"][0]["scope_explicit"]:
            fails.append(f"I scope must not be inferred: {fr}")
    finally:
        bd.capture_region_bsk = orig_cap
        globals()["_viewport_shot"] = orig_vp
    return fails


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P5-K detail params evidence probe v2 (one navigation)")
    ap.add_argument("--product-id")
    ap.add_argument("--browser", help="BSK instance id from this session's `bsk browsers` (required for live)")
    ap.add_argument("--out-dir", type=Path)
    ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args(argv)
    if a.self_check:
        f = run_self_check()
        print("=== P5-K self-check ===")
        print("OK" if not f else "FAILED: " + "; ".join(f))
        return 0 if not f else 1
    if not a.product_id or not a.browser:
        ap.error("--product-id 和 --browser 必填")
    out = a.out_dir or PROBE_ROOT / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{a.product_id}"
    out = Path(out).resolve()
    if out.exists():
        print(f"拒绝覆盖已有目录: {out}")
        return 2
    res = bd.run_bsk_detail_capture(product_id=a.product_id, out_dir=out, browser=a.browser, extra_capture=params_capture)
    summary = {
        "product_id": a.product_id,
        "out_dir": str(out),
        "capture_status": res.get("capture_status"),
        "session_block": res.get("session_block") or (res.get("page_classify") or {}).get("session_block"),
        "navigation_records": sorted(p.name for p in out.glob("bsk_navigate*.json")),
        "extra_capture": res.get("extra_capture"),
        "selection_consistency": res.get("selection_consistency"),
        "error": res.get("error"),
    }
    bd.write_json(out / "probe_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0 if res.get("capture_status") in {"SUCCESS", "PARTIAL"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
