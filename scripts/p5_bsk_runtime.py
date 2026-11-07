"""
Shared BrowserSkill runtime for PriceRadar list collection (P5-G / P5-F).

Connection, page-state extract, next-page click, and wait-for-grid helpers.
Does not implement full collect-all orchestration (that stays in p5f).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

DEFAULT_BSK = Path(os.environ.get("BSK_EXE") or (Path.home() / ".local" / "bin" / "bsk.exe"))
SEARCH_URL_TMPL = "https://search.jd.com/Search?keyword={q}&enc=utf-8"

SENSITIVE_KEY_RE = re.compile(
    r"(cookie|token|authorization|password|passwd|secret|address|phone|mobile|"
    r"身份证|收货|innerHTML|outerHTML)",
    re.IGNORECASE,
)

# Prefer slash N/M; never use 「上一页」后首数字 as current page.
# Title evidence (20261027 DOM diag): product name lives on DIV[title] inside
# plugin_goodsCardWrapper — NOT a[title]. Old selector a[title],…,a hit chat
# anchors with empty text → all titles blank.
EXTRACT_STATE_JS = r"""(() => {
  const visible = (el) => {
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0
      && st.visibility !== 'hidden' && st.display !== 'none'
      && Number(st.opacity || '1') > 0.05;
  };
  const inViewport = (r) => r.bottom > 0 && r.top < window.innerHeight
    && r.right > 0 && r.left < window.innerWidth;

  const regionOf = (el) => {
    let n = el;
    for (let i = 0; i < 10 && n; i++) {
      const id = (n.id || '').toLowerCase();
      const cls = (typeof n.className === 'string' ? n.className : '').toLowerCase();
      const blob = id + ' ' + cls;
      if (/recommend|guess|similar|related|猜你喜欢|为你推荐|lookalike/i.test(blob)) {
        return 'recommend_or_related';
      }
      if (/plugin_goodscontainer|goodscontainer-box|_searchcontent_|searchcontent/i.test(blob)) {
        return 'main_results';
      }
      n = n.parentElement;
    }
    return 'unknown';
  };

  const pickTitle = (el) => {
    // 1) Same-card [title] attrs (observed: DIV[title]=product name)
    let best = '';
    let source = 'none';
    const titled = Array.from(el.querySelectorAll('[title]'));
    for (const t of titled) {
      const v = (t.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
      if (!v || v.length < 4) continue;
      if (/^(广告|推广|收藏|加购|购物车)$/.test(v)) continue;
      if (!/[\u4e00-\u9fff]|B\d/i.test(v)) continue;
      if (v.length > best.length) {
        best = v;
        source = 'attr_title';
      }
    }
    // 2) aria-label
    if (!best) {
      const ariaNodes = Array.from(el.querySelectorAll('[aria-label]'));
      for (const a of ariaNodes) {
        const v = (a.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
        if (v.length >= 4 && /[\u4e00-\u9fff]|B\d/i.test(v) && !/^(广告|推广)$/.test(v)) {
          best = v;
          source = 'aria_label';
          break;
        }
      }
    }
    // 3) img[alt]
    if (!best) {
      for (const img of Array.from(el.querySelectorAll('img[alt]'))) {
        const v = (img.getAttribute('alt') || '').replace(/\s+/g, ' ').trim();
        if (v.length >= 4 && /[\u4e00-\u9fff]|B\d/i.test(v)) {
          best = v;
          source = 'img_alt';
          break;
        }
      }
    }
    // 4) visible text: drop leading 广告 markers; do NOT use search keyword
    if (!best) {
      const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
      const parts = text.split(' ').filter(Boolean);
      const cand = parts.find(p =>
        p.length >= 8
        && /[\u4e00-\u9fff]/.test(p)
        && !/^(广告|推广|已售|到手价|加购|收藏)/.test(p)
        && !/^¥/.test(p)
        && !/^\d+$/.test(p)
      );
      if (cand) {
        best = cand;
        source = 'inner_text_phrase';
      }
    }
    return { title: best.slice(0, 200), title_source: best ? source : 'empty' };
  };

  const nodes = Array.from(document.querySelectorAll('[data-sku]'));
  const out = [];
  const seen = new Set();
  let nested_skipped_as_variant = 0;
  let recommend_count = 0;
  let main_count = 0;
  let hidden_skipped = 0;
  for (const el of nodes) {
    const id = (el.getAttribute('data-sku') || '').trim();
    if (!id || !/^\d{5,}$/.test(id)) continue;
    if (!visible(el)) { hidden_skipped += 1; continue; }
    const parentSkuEl = el.parentElement
      ? el.parentElement.closest('[data-sku]')
      : null;
    const parent_id = parentSkuEl && parentSkuEl !== el
      ? (parentSkuEl.getAttribute('data-sku') || '').trim()
      : null;
    // Nested data-sku: keep relation, do not copy parent title blindly
    const is_variant = !!(parent_id && parent_id !== id);
    if (is_variant) nested_skipped_as_variant += 1;
    if (seen.has(id)) continue;
    seen.add(id);

    const region = regionOf(el);
    if (region === 'recommend_or_related') recommend_count += 1;
    if (region === 'main_results') main_count += 1;

    const { title, title_source } = pickTitle(el);
    const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
    let ad_marked = null;
    if (/广告|推广/.test(text.slice(0, 40)) || /广告|推广/.test(text)) ad_marked = true;

    const hrefEl = el.querySelector(
      'a[href*="item.jd.com"], a[href*="item.m.jd.com"], a[href*="/product/"]'
    );
    let href = hrefEl ? (hrefEl.getAttribute('href') || '') : '';
    if (!href) {
      // constructed later from id; keep observed empty rather than chat links
      href = '';
    }
    const nested_ids = Array.from(el.querySelectorAll('[data-sku]'))
      .map(x => (x.getAttribute('data-sku') || '').trim())
      .filter(x => x && x !== id)
      .slice(0, 12);

    out.push({
      jd_product_id: id,
      product_id: id,
      title,
      title_source,
      ad_marked,
      href: href.slice(0, 300),
      discovery_source: is_variant ? 'data_sku_variant' : 'data_sku_card',
      result_region: region,
      parent_product_id: is_variant ? parent_id : null,
      nested_product_ids: nested_ids,
    });
  }

  let current_page_hint = null;
  let total_pages_hint = null;
  let page_source = null;
  let pager_text = null;
  const pageSources = [];
  const all = Array.from(document.querySelectorAll('a, button, span, div, li, em, i, b, strong'));
  for (const el of all) {
    if (!visible(el)) continue;
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length > 24) continue;
    const m = t.match(/^<?\s*(\d+)\s*\/\s*(\d+)\s*>?$/);
    if (m) {
      const cur = parseInt(m[1], 10);
      const tot = parseInt(m[2], 10);
      if (cur >= 1 && tot >= cur && tot <= 500) {
        pageSources.push({ kind: 'slash_widget', cur, tot, text: t });
      }
    }
  }
  for (const el of all) {
    if (!visible(el)) continue;
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (!/^\d{1,3}$/.test(t)) continue;
    const cls = typeof el.className === 'string' ? el.className : '';
    if (/curr|active|selected|current|page_on|is-active/i.test(cls)
        || el.getAttribute('aria-current') === 'page') {
      const n = parseInt(t, 10);
      if (n >= 1 && n <= 500) {
        pageSources.push({ kind: 'active_digit', cur: n, tot: null, text: t, className: cls.slice(0, 80) });
      }
    }
  }
  for (const el of all) {
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length > 80) continue;
    if (t.includes('下一页') && (t.includes('上一页') || /共\s*\d+\s*页/.test(t))) {
      const r = el.getBoundingClientRect();
      if (r.height <= 120 && r.width >= 80) {
        pager_text = t;
        const mTotal = t.match(/共\s*(\d+)\s*页/);
        if (mTotal) {
          total_pages_hint = parseInt(mTotal[1], 10);
          pageSources.push({
            kind: 'strip_total_only',
            cur: null,
            tot: total_pages_hint,
            text: t,
            note: 'strip digits after 上一页 are page links, not current',
          });
        }
        break;
      }
    }
  }
  const slash = pageSources.find(p => p.kind === 'slash_widget');
  const digit = pageSources.find(p => p.kind === 'active_digit');
  if (slash) {
    current_page_hint = slash.cur;
    total_pages_hint = slash.tot;
    page_source = 'slash_widget';
  } else if (digit) {
    current_page_hint = digit.cur;
    page_source = 'active_digit';
  } else {
    page_source = 'unknown';
  }

  const next_candidates = [];
  for (const el of all) {
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (t !== '下一页') continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0 || r.width > 220 || r.height > 80) continue;
    const cls = typeof el.className === 'string' ? el.className : '';
    const disabled = el.hasAttribute('disabled')
      || (el.getAttribute('aria-disabled') || '') === 'true'
      || /disabled|is-disabled|pagination_disable/i.test(cls);
    const vis = visible(el);
    next_candidates.push({
      tag: el.tagName,
      className: cls.slice(0, 140),
      text: t,
      disabled,
      visible: vis,
      in_viewport: inViewport(r),
      area: Math.round(r.width * r.height),
      x: Math.round(r.left),
      y: Math.round(r.top),
      width: Math.round(r.width),
      height: Math.round(r.height),
      score: (vis ? 10 : 0) + (disabled ? -20 : 5) + (inViewport(r) ? 2 : 0)
        + (/pagination|page/i.test(cls) ? 5 : 0) + Math.min(r.top, 5000) / 1000,
    });
  }
  next_candidates.sort((a, b) => b.score - a.score);
  const next = next_candidates.find(c => c.visible && !c.disabled) || null;

  const body = (document.body && document.body.innerText || '').slice(0, 3000);
  const loading = /正在奋力加载|加载中|请稍候/.test(body);
  // Do NOT match bare「登录」(header link is always present).
  const restriction = /访问频繁|无法搜索|验证码|安全验证|请登录后|登录后继续/.test(body)
    ? (body.match(/.{0,40}(访问频繁|无法搜索|验证码|安全验证|请登录后|登录后继续).{0,40}/) || ['restriction_hint'])[0]
    : null;

  // Prefer goods container metrics when present (virtual list host).
  let scroll_host = 'document';
  let scroll_y = window.scrollY || document.documentElement.scrollTop || 0;
  let scroll_max = Math.max(
    0,
    (document.documentElement.scrollHeight || 0) - window.innerHeight
  );
  const goodsBox = document.querySelector(
    '.plugin_goodsContainer, [class*="goodsContainer"], [class*="_goodsContainer"]'
  );
  if (goodsBox) {
    const cs = getComputedStyle(goodsBox);
    const canScroll = goodsBox.scrollHeight > goodsBox.clientHeight + 20
      && /(auto|scroll)/.test(cs.overflowY || '');
    if (canScroll) {
      scroll_host = 'goods_container';
      scroll_y = goodsBox.scrollTop;
      scroll_max = Math.max(0, goodsBox.scrollHeight - goodsBox.clientHeight);
    }
  }
  const pagerNearBottom = (() => {
    if (!next) return false;
    // next control y relative to viewport; near bottom of page content
    return next.y > window.innerHeight * 0.4 || next.in_viewport;
  })();

  return {
    url: location.href.split('#')[0],
    host: location.host,
    search_keyword_input: (document.querySelector('#key, input[name=keyword]') || {}).value || null,
    data_sku_count: nodes.length,
    unique_ids: out.map(x => x.jd_product_id),
    cards: out.slice(0, 250),
    card_stats: {
      extracted: out.length,
      nested_variant_nodes: nested_skipped_as_variant,
      recommend_count: recommend_count,
      main_count: main_count,
      hidden_skipped: hidden_skipped,
      with_title: out.filter(x => x.title).length,
    },
    pager_text,
    current_page_hint,
    total_pages_hint,
    page_source,
    page_sources: pageSources.slice(0, 8),
    next_control: next,
    next_candidates: next_candidates.slice(0, 8),
    loading_hint: loading,
    restriction_tip: restriction,
    scroll_host,
    scroll_y,
    scroll_max,
    pager_near_bottom: pagerNearBottom,
  };
})()"""

SCROLL_TO_NEXT_JS = r"""(() => {
  const nodes = Array.from(document.querySelectorAll('a, button, span, div, li, em, i'));
  let best = null;
  for (const el of nodes) {
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (t !== '下一页') continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0 || r.width > 220 || r.height > 80) continue;
    const cls = typeof el.className === 'string' ? el.className : '';
    const disabled = el.hasAttribute('disabled')
      || (el.getAttribute('aria-disabled') || '') === 'true'
      || /disabled|is-disabled/i.test(cls);
    if (disabled) continue;
    const score = r.top + (/pagination/i.test(cls) ? 1000 : 0);
    if (!best || score > best.score) best = { el, score, className: cls.slice(0, 140) };
  }
  if (!best) return { ok: false, reason: 'no_enabled_next' };
  best.el.scrollIntoView({ block: 'center', inline: 'nearest' });
  return { ok: true, className: best.className };
})()"""

MARK_NEXT_JS = r"""(() => {
  document.querySelectorAll('[data-pr-next]').forEach(el => el.removeAttribute('data-pr-next'));
  const nodes = Array.from(document.querySelectorAll('a, button, span, div, li, em, i'));
  const cands = [];
  for (const el of nodes) {
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (t !== '下一页') continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0 || r.width > 220 || r.height > 80) continue;
    const st = window.getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity||'1') < 0.05) continue;
    const cls = typeof el.className === 'string' ? el.className : '';
    const disabled = el.hasAttribute('disabled')
      || (el.getAttribute('aria-disabled') || '') === 'true'
      || /disabled|is-disabled/i.test(cls);
    if (disabled) continue;
    cands.push({
      el,
      className: cls.slice(0, 140),
      y: r.top,
      score: r.top + (/pagination_next|pagination/i.test(cls) ? 2000 : 0),
    });
  }
  cands.sort((a, b) => b.score - a.score);
  if (!cands.length) return { ok: false, reason: 'no_markable_next' };
  const best = cands[0];
  best.el.setAttribute('data-pr-next', '1');
  best.el.scrollIntoView({ block: 'center', inline: 'nearest' });
  return {
    ok: true,
    className: best.className,
    candidate_count: cands.length,
    selector: '[data-pr-next="1"]',
  };
})()"""


def utc_iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[truncated_depth]"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if SENSITIVE_KEY_RE.search(str(k)):
                out[str(k)] = "[redacted]"
            else:
                out[str(k)] = sanitize(v, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [sanitize(v, depth=depth + 1) for v in value[:80]]
    if isinstance(value, str):
        if "<html" in value.lower() or "<!doctype" in value.lower():
            return "[redacted_html]"
        if len(value) > 500:
            return value[:500] + "…[truncated]"
        return value
    return value


def id_fingerprint(ids: list[str]) -> str:
    joined = ",".join(sorted(ids))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def search_url_for(query: str) -> str:
    from urllib.parse import quote

    return SEARCH_URL_TMPL.format(q=quote(query))


class BskClient:
    def __init__(self, exe: Path | None = None):
        self.exe = Path(exe or DEFAULT_BSK)
        if not self.exe.is_file():
            raise FileNotFoundError(f"bsk not found: {self.exe}")

    def run(self, args: list[str], *, timeout: int = 120) -> dict[str, Any]:
        env = os.environ.copy()
        env["BSK_AUTO_START"] = "0"
        env["PATH"] = str(self.exe.parent) + os.pathsep + env.get("PATH", "")
        cmd = [str(self.exe), *args]
        if "--json" not in cmd:
            cmd = [str(self.exe), "--json", *args]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return {"ok": False, "error": f"timeout: {exc}", "cmd": cmd}
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        parsed: Any = None
        if stdout:
            try:
                parsed = json.loads(stdout)
            except json.JSONDecodeError:
                parsed = {"raw_stdout": stdout[:2000]}
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "cmd": cmd,
            "data": parsed,
            "stderr": stderr[:2000] if stderr else None,
        }


def check_prereqs(client: BskClient) -> dict[str, Any]:
    status = client.run(["status"])
    doctor = client.run(["doctor"])
    browsers = client.run(["browsers"])
    return {
        "checked_at_utc": utc_iso_now(),
        "bsk_exe": str(client.exe),
        "status": sanitize(status),
        "doctor": sanitize(doctor),
        "browsers": sanitize(browsers),
        "daemon_ready": bool(status.get("ok")),
        "browsers_list": (browsers.get("data") or {})
        if isinstance(browsers.get("data"), (dict, list))
        else browsers.get("data"),
    }


def pick_browser(browsers_payload: Any, explicit: str | None) -> dict[str, Any]:
    if explicit:
        return {"selector": explicit, "source": "cli --browser"}
    data = browsers_payload
    items: list[Any] = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("browsers", "items", "connected"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        if not items and data.get("id"):
            items = [data]
    if not items:
        return {"selector": None, "error": "no_connected_browsers", "raw": data}
    if len(items) == 1:
        item = items[0]
        if isinstance(item, dict):
            sel = (
                item.get("instance_id")
                or item.get("id")
                or item.get("browser_id")
                or item.get("name")
                or item.get("label")
            )
            return {
                "selector": sel,
                "source": "single_connected",
                "browser": sanitize(item),
            }
        return {"selector": str(item), "source": "single_connected"}
    summaries = []
    for it in items:
        if isinstance(it, dict):
            summaries.append(
                {
                    "id": it.get("instance_id") or it.get("id") or it.get("browser_id"),
                    "name": it.get("name") or it.get("label"),
                    "browser": it.get("browser_name")
                    or it.get("browser")
                    or it.get("product"),
                }
            )
        else:
            summaries.append({"raw": str(it)})
    return {
        "selector": None,
        "error": "multiple_browsers_need_explicit_choice",
        "options": summaries,
    }


def cooperate_policy_from_prereqs(prereqs: dict[str, Any] | None) -> str:
    """Extension protocol <1.8 requires strict on every sync (BSK 0.4.x)."""
    if not prereqs:
        return "strict"
    status_wrap = prereqs.get("status")
    if isinstance(status_wrap, dict):
        data = status_wrap.get("data")
    else:
        data = None
    if not isinstance(data, dict):
        data = {}
    skew = data.get("version_skew_browsers") or []
    browsers = data.get("browsers") or []
    for b in list(skew) + list(browsers):
        if not isinstance(b, dict):
            continue
        ext_proto = str(
            b.get("client_protocol_version")
            or b.get("extension_protocol_version")
            or ""
        )
        if ext_proto and ext_proto < "1.8":
            return "strict"
        if b.get("version_skew"):
            return "strict"
    daemon_proto = str(data.get("protocol_version") or "")
    if daemon_proto >= "1.8":
        # Default target policy when peers are current; skew list handled above.
        return "auto"
    return "strict"


def cooperate_sync(
    client: BskClient,
    session: str,
    *,
    policy: str = "strict",
    settle: bool = False,
) -> dict[str, Any]:
    """Return sync payload including context_id for subsequent page actions."""
    args = ["cooperate", "sync", "--session", session, "--policy", policy]
    if settle:
        args.append("--settle")
    res = client.run(args, timeout=120)
    data = res.get("data")
    if not isinstance(data, dict):
        data = {}
    ctx = data.get("context_id") or data.get("context")
    obs = data.get("observation_id")
    if not obs and isinstance(data.get("observation"), dict):
        obs = data["observation"].get("observation_id")
    return {
        "ok": bool(res.get("ok") and ctx),
        "context_id": ctx,
        "observation_id": obs,
        "data": sanitize(data),
        "raw": sanitize(res),
    }


def _append_cooperate_receipt(
    args: list[str],
    sync: dict[str, Any],
    *,
    include_observation: bool,
) -> None:
    ctx = sync.get("context_id")
    if ctx:
        args.extend(["--context", str(ctx)])
    if include_observation:
        obs = sync.get("observation_id")
        if obs:
            args.extend(["--observation", str(obs)])


def eval_json(
    client: BskClient,
    session: str,
    expression: str,
    *,
    context_id: str | None = None,
    observation_id: str | None = None,
    cooperate_policy: str = "strict",
    cooperate_settle: bool = False,
) -> dict[str, Any]:
    ctx = context_id
    obs = observation_id
    sync_meta = None
    if not ctx or not obs:
        sync_meta = cooperate_sync(
            client,
            session,
            policy=cooperate_policy,
            settle=cooperate_settle,
        )
        if not ctx:
            ctx = sync_meta.get("context_id")
        if not obs:
            obs = sync_meta.get("observation_id")
        if not ctx:
            return {
                "ok": False,
                "value": None,
                "sync": sync_meta,
                "raw": sync_meta.get("raw") if sync_meta else None,
            }
    args = ["evaluate", "--session", session, expression]
    sync_for_receipt = sync_meta or {
        "context_id": ctx,
        "observation_id": obs,
    }
    _append_cooperate_receipt(
        args, sync_for_receipt, include_observation=True
    )
    res = client.run(args, timeout=60)
    data = res.get("data")
    if isinstance(data, dict) and "value" in data:
        value = data.get("value")
    else:
        value = data
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    out = {
        "ok": res.get("ok"),
        "value": value,
        "context_id": ctx,
        "observation_id": obs,
        "raw": sanitize(res),
    }
    if sync_meta:
        out["sync"] = sync_meta
    return out


def observe_page(
    client: BskClient,
    session: str,
    *,
    cooperate_policy: str = "strict",
    settle: bool = False,
) -> dict[str, Any]:
    sync = cooperate_sync(client, session, policy=cooperate_policy, settle=settle)
    ctx = sync.get("context_id")
    if not ctx:
        return {"ok": False, "sync": sync, "data": None}
    args = ["observe", "--session", session]
    _append_cooperate_receipt(args, sync, include_observation=False)
    res = client.run(args, timeout=90)
    raw_data = res.get("data")
    vom_text = ""
    if isinstance(raw_data, dict):
        vom_text = str(raw_data.get("text") or "")
    return {
        "ok": res.get("ok"),
        "context_id": ctx,
        "observation_id": sync.get("observation_id"),
        "sync": sync,
        "vom_text": vom_text,
        "data": sanitize(raw_data),
        "raw": sanitize(res),
    }


def screenshot_session(
    client: BskClient,
    session: str,
    out_path: Path,
    *,
    ref: str | None = None,
    context_id: str | None = None,
    observation_id: str | None = None,
    cooperate_policy: str = "strict",
    cooperate_settle: bool = False,
) -> dict[str, Any]:
    sync = None
    ctx = context_id
    obs = observation_id
    if not ctx or (ref and not obs):
        sync = cooperate_sync(
            client, session, policy=cooperate_policy, settle=cooperate_settle
        )
        if not ctx:
            ctx = sync.get("context_id")
        if not obs:
            obs = sync.get("observation_id")
    if not ctx:
        return {"ok": False, "sync": sync}
    args = ["screenshot", "--session", session, "--out", str(out_path)]
    receipt = sync or {"context_id": ctx, "observation_id": obs}
    _append_cooperate_receipt(
        args, receipt, include_observation=bool(obs)
    )
    if ref:
        args.extend(["--ref", ref])
    res = client.run(args, timeout=120)
    return {
        "ok": res.get("ok"),
        "context_id": ctx,
        "sync": sync,
        "data": sanitize(res.get("data")),
        "path": str(out_path) if out_path.is_file() else None,
        "raw": sanitize(res),
    }


def scroll_to_target(
    client: BskClient,
    session: str,
    *,
    ref: str | None = None,
    selector: str | None = None,
    context_id: str | None = None,
    observation_id: str | None = None,
    cooperate_policy: str = "strict",
) -> dict[str, Any]:
    sync = None
    ctx = context_id
    obs = observation_id
    if not ctx:
        sync = cooperate_sync(client, session, policy=cooperate_policy, settle=False)
        ctx = sync.get("context_id")
        obs = obs or sync.get("observation_id")
    if not ctx:
        return {"ok": False, "sync": sync}
    args = ["scroll-to", "--session", session]
    if ref:
        args.append(ref)
    elif selector:
        args.extend(["--selector", selector])
    else:
        return {"ok": False, "error": "ref_or_selector_required"}
    receipt = sync or {"context_id": ctx, "observation_id": obs}
    _append_cooperate_receipt(args, receipt, include_observation=bool(obs))
    res = client.run(args, timeout=60)
    return {"ok": res.get("ok"), "raw": sanitize(res), "sync": sync}


def pager_from_state(v: dict[str, Any]) -> dict[str, Any]:
    next_ctrl = v.get("next_control")
    disabled = bool(next_ctrl and next_ctrl.get("disabled"))
    if next_ctrl and next_ctrl.get("visible") and not disabled:
        status = "FOUND"
    elif next_ctrl and disabled:
        status = "DISABLED"
        disabled = True
    elif not next_ctrl:
        # Check if any candidate exists but disabled
        cands = v.get("next_candidates") or []
        if cands and all(c.get("disabled") for c in cands):
            status = "DISABLED"
            disabled = True
        else:
            status = "NOT_FOUND"
    else:
        status = "NOT_FOUND"
    return {
        "status": status,
        "disabled": disabled,
        "current_page_hint": v.get("current_page_hint"),
        "total_pages_hint": v.get("total_pages_hint"),
        "page_source": v.get("page_source"),
        "pager_text": v.get("pager_text"),
        "control": next_ctrl,
        "next_candidates": v.get("next_candidates") or [],
    }


def cards_to_rows(
    cards: list[dict[str, Any]],
    *,
    query: str,
    page_index: int,
    source_url: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i, c in enumerate(cards, start=1):
        pid = str(c.get("jd_product_id") or c.get("product_id") or "")
        if not pid:
            continue
        # Never fill title from the search query.
        title = str(c.get("title") or "").strip()
        title_source = str(c.get("title_source") or ("empty" if not title else "unknown"))
        if title and title.replace(" ", "") == query.replace(" ", ""):
            title = ""
            title_source = "rejected_query_echo"
        ad_marked = c.get("ad_marked")
        if ad_marked is None:
            ad_status = "广告标识未知，不能写 false"
        elif ad_marked:
            ad_status = "页面有广告标识"
        else:
            ad_status = "未发现广告标识，不能断言非广告"
            ad_marked = False
        href = str(c.get("href") or "")
        if href and ("chat.jd.com" in href or "passport" in href):
            href = ""
        rows.append(
            {
                "query": query,
                "product_id": pid,
                "jd_product_id": pid,
                "title": title,
                "title_source": title_source,
                "result_region": c.get("result_region") or "unknown",
                "parent_product_id": c.get("parent_product_id"),
                "nested_product_ids": c.get("nested_product_ids") or [],
                "ad_marked": ad_marked,
                "ad_status": ad_status,
                "observed_url": href or None,
                "normalized_url": f"https://item.jd.com/{pid}.html",
                "normalize_status": "constructed_from_data_sku",
                "discovery_source": c.get("discovery_source") or "data_sku_card",
                "source_search_url": source_url.split("?")[0] if source_url else None,
                "discovered_at_utc": utc_iso_now(),
                "position_on_page": i,
                "page": page_index,
            }
        )
    return rows


def wait_grid_and_page(
    client: BskClient,
    session_id: str,
    *,
    before_ids: list[str],
    before_page: int | None,
    expect_page: int | None = None,
    timeout_s: int = 28,
) -> dict[str, Any]:
    started = time.time()
    transitions: list[dict[str, Any]] = []
    last: dict[str, Any] = {}
    before_fp = id_fingerprint(before_ids)
    client.run(
        [
            "wait-for-element",
            "--session",
            session_id,
            "--selector",
            "[data-sku]",
            "--state",
            "visible",
            "--timeout",
            f"{timeout_s}s",
        ],
        timeout=timeout_s + 15,
    )
    while time.time() - started < timeout_s:
        snap = eval_json(client, session_id, EXTRACT_STATE_JS)
        v = snap.get("value") if isinstance(snap.get("value"), dict) else {}
        ids = [str(x) for x in (v.get("unique_ids") or [])]
        cur = v.get("current_page_hint")
        loading = bool(v.get("loading_hint"))
        fp = id_fingerprint(ids)
        new_n = len(set(ids) - set(before_ids))
        tr = {
            "t_ms": int((time.time() - started) * 1000),
            "page": cur,
            "page_source": v.get("page_source"),
            "cards": len(ids),
            "new_vs_before": new_n,
            "loading": loading,
            "fp": fp,
        }
        transitions.append(tr)
        last = v
        page_ok = False
        if isinstance(cur, int):
            if expect_page is not None:
                page_ok = cur == expect_page
            elif isinstance(before_page, int):
                page_ok = cur > before_page
            else:
                page_ok = cur >= 2
        grid_ok = len(ids) > 0 and (new_n > 0 or (page_ok and fp != before_fp))
        if page_ok and len(ids) > 0 and not loading and grid_ok:
            return {
                "ok": True,
                "reason": "page_and_grid_ready",
                "value": v,
                "transitions": transitions,
            }
        if len(ids) == 0 and tr["t_ms"] > 8000:
            client.run(
                [
                    "evaluate",
                    "--session",
                    session_id,
                    "(() => { window.scrollBy(0, Math.floor(innerHeight*0.8)); return true; })()",
                ],
                timeout=30,
            )
        client.run(["wait-ms", "700"], timeout=15)
    return {
        "ok": False,
        "reason": "timeout_waiting_page_or_grid",
        "value": last,
        "transitions": transitions,
    }


def click_next_page(client: BskClient, session_id: str) -> dict[str, Any]:
    """Scroll, re-locate unique enabled next, native bsk click (P5-G verified)."""
    eval_json(client, session_id, SCROLL_TO_NEXT_JS)
    client.run(["wait-ms", "400"], timeout=10)
    mark = eval_json(client, session_id, MARK_NEXT_JS)
    mv = mark.get("value") if isinstance(mark.get("value"), dict) else {}
    if not mv.get("ok"):
        return {"ok": False, "reason": "no_unique_enabled_next", "mark": sanitize(mark)}
    sel = str(mv.get("selector") or '[data-pr-next="1"]')
    client.run(
        ["scroll-to", "--session", session_id, "--selector", sel],
        timeout=30,
    )
    native = client.run(
        ["click", "--session", session_id, "--selector", sel],
        timeout=45,
    )
    if native.get("ok"):
        return {
            "ok": True,
            "path": "bsk_click_selector",
            "className": mv.get("className"),
            "selector": sel,
            "candidate_count": mv.get("candidate_count"),
        }
    # Fallback DOM click
    click_js = r"""(() => {
      const el = document.querySelector('[data-pr-next="1"]');
      if (!el) return { ok: false, reason: 'marker_gone' };
      el.scrollIntoView({ block: 'center' });
      el.click();
      return { ok: true, path: 'js_click_fallback' };
    })()"""
    fb = eval_json(client, session_id, click_js)
    fv = fb.get("value") if isinstance(fb.get("value"), dict) else {}
    return {
        "ok": bool(fv.get("ok")),
        "path": fv.get("path") or "js_click_fallback",
        "className": mv.get("className"),
        "selector": sel,
        "native_error": sanitize(native),
    }


def turn_to_next_page(
    client: BskClient,
    session_id: str,
    *,
    from_page: int,
    before_ids: list[str],
    timeout_s: int = 28,
) -> dict[str, Any]:
    before = eval_json(client, session_id, EXTRACT_STATE_JS)
    bv = before.get("value") if isinstance(before.get("value"), dict) else {}
    hint = bv.get("current_page_hint")
    if isinstance(hint, int) and hint != from_page:
        # Soft: trust observed hint if we are locating; still require advance
        pass
    ids = before_ids or [str(x) for x in (bv.get("unique_ids") or [])]
    click = click_next_page(client, session_id)
    if not click.get("ok"):
        return {"ok": False, "reason": "click_failed", "click": click}
    expect = from_page + 1
    wait = wait_grid_and_page(
        client,
        session_id,
        before_ids=ids,
        before_page=from_page,
        expect_page=expect,
        timeout_s=timeout_s,
    )
    v = wait.get("value") if isinstance(wait.get("value"), dict) else {}
    after_hint = v.get("current_page_hint")
    after_ids = [str(x) for x in (v.get("unique_ids") or [])]
    page_advanced = isinstance(after_hint, int) and after_hint == expect
    new_n = len(set(after_ids) - set(ids))
    grid_updated = len(after_ids) > 0 and (
        new_n > 0 or id_fingerprint(after_ids) != id_fingerprint(ids)
    )
    if page_advanced and not grid_updated:
        return {
            "ok": False,
            "reason": "page_label_ok_grid_stale",
            "page_after": after_hint,
            "ids_changed": False,
            "click": click,
            "wait": sanitize(wait),
        }
    if not page_advanced:
        return {
            "ok": False,
            "reason": "page_not_advanced",
            "page_after": after_hint,
            "ids_changed": grid_updated,
            "click": click,
            "wait": sanitize(wait),
        }
    if v.get("restriction_tip"):
        return {
            "ok": False,
            "reason": "SEARCH_RESTRICTED",
            "page_after": after_hint,
            "restriction_tip": v.get("restriction_tip"),
            "click": click,
            "wait": sanitize(wait),
        }
    return {
        "ok": True,
        "page_after": after_hint,
        "ids_changed": True,
        "new_count": new_n,
        "click": click,
        "wait_reason": wait.get("reason"),
        "after_ids_sample": after_ids[:10],
    }


PAGE_MAX_SCROLLS = 18
PAGE_SCROLL_PAUSE_MS = 750
PAGE_STABLE_ROUNDS = 3  # idle rounds alone are NOT enough without bottom+pager

SCROLL_STEP_JS = r"""(() => {
  const box = document.querySelector(
    '.plugin_goodsContainer, [class*="goodsContainer"], [class*="_goodsContainer"]'
  );
  const step = Math.floor(window.innerHeight * 0.85);
  if (box) {
    const cs = getComputedStyle(box);
    if (box.scrollHeight > box.clientHeight + 20 && /(auto|scroll)/.test(cs.overflowY || '')) {
      box.scrollBy(0, step);
      return { host: 'goods_container', y: box.scrollTop, max: box.scrollHeight - box.clientHeight };
    }
  }
  window.scrollBy(0, step);
  const y = window.scrollY || document.documentElement.scrollTop || 0;
  const max = Math.max(0, (document.documentElement.scrollHeight || 0) - window.innerHeight);
  return { host: 'document', y, max };
})()"""


def collect_one_page_bsk(
    client: BskClient,
    session_id: str,
    *,
    query: str,
    page_index: int,
    expected_page: int | None = None,
    log: Any = print,
) -> dict[str, Any]:
    """Scroll-while-collect all observed [data-sku] on the current result page."""
    by_id: dict[str, dict[str, Any]] = {}
    scroll_meta: list[dict[str, Any]] = []
    load_status = "IN_PROGRESS"
    page_load_coverage = "UNCONFIRMED"
    stop_reason_detail = "in_progress"
    stable = 0
    scrolls = 0
    wait_info: dict[str, Any] = {}

    client.run(
        [
            "wait-for-element",
            "--session",
            session_id,
            "--selector",
            "[data-sku]",
            "--state",
            "visible",
            "--timeout",
            "20s",
        ],
        timeout=35,
    )
    client.run(["wait-ms", "500"], timeout=10)

    for scroll_pass in range(PAGE_MAX_SCROLLS + 1):
        snap = eval_json(client, session_id, EXTRACT_STATE_JS)
        v = snap.get("value") if isinstance(snap.get("value"), dict) else {}
        host = str(v.get("host") or "")
        url = str(v.get("url") or "")
        if "passport.jd.com" in host or "/login" in url.lower():
            return {
                "page": page_index,
                "load_status": "SEARCH_RESTRICTED",
                "page_load_coverage": "FAILED_RESTRICTED",
                "restriction": {
                    "observed_tip": "LOGIN_REQUIRED:passport.jd.com",
                    "kind": "LOGIN_REQUIRED",
                },
                "rows": [],
                "scroll_meta": scroll_meta,
                "raw_card_peaks": 0,
                "pager": pager_from_state(v),
                "id_fingerprint": "",
                "collected_at_utc": utc_iso_now(),
                "url_safe": url.split("?")[0],
            }
        if v.get("restriction_tip"):
            return {
                "page": page_index,
                "load_status": "SEARCH_RESTRICTED",
                "page_load_coverage": "FAILED_RESTRICTED",
                "restriction": {"observed_tip": v.get("restriction_tip")},
                "rows": [],
                "scroll_meta": scroll_meta,
                "raw_card_peaks": 0,
                "pager": pager_from_state(v),
                "id_fingerprint": "",
                "collected_at_utc": utc_iso_now(),
                "url_safe": url.split("?")[0],
            }
        if expected_page is not None and isinstance(v.get("current_page_hint"), int):
            if v["current_page_hint"] != expected_page and scroll_pass == 0:
                wait_info["page_hint_mismatch"] = {
                    "expected": expected_page,
                    "observed": v["current_page_hint"],
                    "source": v.get("page_source"),
                }
        cards = list(v.get("cards") or [])
        source_url = str(v.get("url") or "")
        rows = cards_to_rows(
            cards, query=query, page_index=page_index, source_url=source_url
        )
        added = 0
        for rec in rows:
            pid = str(rec.get("product_id") or "")
            if not pid:
                continue
            if pid in by_id:
                prev = by_id[pid]
                sources = prev.setdefault("scroll_passes_seen", [])
                if scroll_pass not in sources:
                    sources.append(scroll_pass)
                if not (prev.get("title") or "").strip() and (rec.get("title") or "").strip():
                    prev["title"] = rec["title"]
                    prev["title_source"] = rec.get("title_source")
                continue
            item = dict(rec)
            item["position_on_page"] = len(by_id) + 1
            item["scroll_pass_first"] = scroll_pass
            item["scroll_passes_seen"] = [scroll_pass]
            by_id[pid] = item
            added += 1
        scroll_y = float(v.get("scroll_y") or 0)
        scroll_max = float(v.get("scroll_max") or 0)
        at_bottom = scroll_max <= 0 or scroll_y >= scroll_max - 48
        loading = bool(v.get("loading_hint"))
        pager_near = bool(v.get("pager_near_bottom"))
        card_stats = v.get("card_stats") or {}
        scroll_meta.append(
            {
                "scroll_pass": scroll_pass,
                "raw_rows": len(cards),
                "new_on_page": added,
                "unique_on_page": len(by_id),
                "data_sku_card_count": v.get("data_sku_count"),
                "with_title": card_stats.get("with_title"),
                "scroll_host": v.get("scroll_host"),
                "scroll_y": scroll_y,
                "scroll_max": scroll_max,
                "at_bottom": at_bottom,
                "loading": loading,
                "pager_near_bottom": pager_near,
                "page_hint": v.get("current_page_hint"),
            }
        )
        if added == 0 and not loading:
            stable += 1
        else:
            stable = 0
        if at_bottom and not loading and stable >= PAGE_STABLE_ROUNDS:
            pager_ok = pager_near or bool(v.get("next_control")) or bool(
                v.get("pager_text")
            )
            if pager_ok:
                load_status = "PAGE_LOAD_STABLE"
                page_load_coverage = "CONFIRMED"
                stop_reason_detail = "bottom_idle_with_pager"
                break
            if stable >= PAGE_STABLE_ROUNDS + 2:
                load_status = "PAGE_LOAD_STABLE_SOFT"
                page_load_coverage = "UNCONFIRMED_NO_PAGER_EVIDENCE"
                stop_reason_detail = "bottom_idle_without_pager"
                break
        if scroll_pass >= PAGE_MAX_SCROLLS:
            if at_bottom and not loading:
                load_status = "PAGE_LOAD_STABLE_SOFT"
                page_load_coverage = "UNCONFIRMED_SCROLL_BUDGET_AT_BOTTOM"
                stop_reason_detail = "scroll_budget_at_bottom"
            else:
                load_status = "PAGE_SCROLL_BUDGET"
                page_load_coverage = "UNCONFIRMED_SCROLL_BUDGET"
                stop_reason_detail = "scroll_budget_not_at_bottom"
            break
        client.run(
            ["evaluate", "--session", session_id, SCROLL_STEP_JS],
            timeout=30,
        )
        client.run(["wait-ms", str(PAGE_SCROLL_PAUSE_MS)], timeout=15)
        scrolls += 1

    eval_json(client, session_id, SCROLL_TO_NEXT_JS)
    client.run(["wait-ms", "400"], timeout=10)
    final = eval_json(client, session_id, EXTRACT_STATE_JS)
    fv = final.get("value") if isinstance(final.get("value"), dict) else {}
    pager = pager_from_state(fv)
    rows_out = sorted(by_id.values(), key=lambda r: int(r.get("position_on_page") or 0))
    peak = (
        max((m.get("data_sku_card_count") or 0) for m in scroll_meta)
        if scroll_meta
        else 0
    )
    titled_n = sum(1 for r in rows_out if (r.get("title") or "").strip())
    if expected_page is not None and isinstance(fv.get("current_page_hint"), int):
        if fv["current_page_hint"] != expected_page:
            load_status = "PAGE_HINT_MISMATCH"
            page_load_coverage = "FAILED_PAGE_HINT"
            stop_reason_detail = "page_hint_mismatch"
    log(
        f"BSK页{page_index}: unique={len(rows_out)} titled={titled_n} scrolls={scrolls} "
        f"status={load_status} coverage={page_load_coverage} "
        f"pager={pager.get('status')} "
        f"hint={pager.get('current_page_hint')}/{pager.get('total_pages_hint')}"
    )
    return {
        "page": page_index,
        "load_status": load_status,
        "page_load_coverage": page_load_coverage,
        "stop_reason_detail": stop_reason_detail,
        "restriction": None,
        "wait_info": wait_info,
        "rows": rows_out,
        "scroll_meta": scroll_meta,
        "scroll_count": scrolls,
        "raw_card_peaks": peak,
        "title_coverage_on_page": {
            "with_title": titled_n,
            "total": len(rows_out),
            "ratio": (titled_n / len(rows_out)) if rows_out else None,
        },
        "id_fingerprint": id_fingerprint([r["jd_product_id"] for r in rows_out]),
        "pager": pager,
        "collected_at_utc": utc_iso_now(),
        "url_safe": (fv.get("url") or "").split("?")[0],
        "page_source": fv.get("page_source"),
        "card_stats_last": fv.get("card_stats"),
    }



def start_session(client: BskClient, browser: str) -> dict[str, Any]:
    start = client.run(
        ["session", "start", "--no-focus", "--browser", str(browser), "--name", "priceradar-collect-all"]
    )
    data = start.get("data")
    session_id = None
    if isinstance(data, dict):
        session_id = data.get("session_id") or data.get("id") or data.get("session")
    return {"ok": bool(start.get("ok") and session_id), "session_id": session_id, "raw": sanitize(start)}


def stop_session(client: BskClient, session_id: str | None) -> dict[str, Any]:
    if not session_id:
        return {"ok": True, "skipped": True}
    return sanitize(client.run(["session", "stop", session_id]))


def open_search(
    client: BskClient,
    session_id: str,
    query: str,
    *,
    reuse_search_tab: bool = True,
) -> dict[str, Any]:
    reused = False
    if reuse_search_tab:
        tabs = client.run(["tab", "list", "--scope", "user", "--session", session_id])
        tab_items = tabs.get("data")
        if isinstance(tab_items, dict):
            tab_items = tab_items.get("tabs") or tab_items.get("items") or []
        if isinstance(tab_items, list):
            q_compact = query.replace(" ", "")
            for t in tab_items:
                if not isinstance(t, dict):
                    continue
                url = str(t.get("url") or "")
                title = str(t.get("title") or "")
                tid = t.get("id") or t.get("tab_id")
                if tid and "search.jd.com" in url and (
                    "B913A" in url or "B913A" in title or "清风" in title or q_compact[:4] in url
                ):
                    br = client.run(
                        ["tab", "borrow", str(tid), "--session", session_id],
                        timeout=90,
                    )
                    if br.get("ok"):
                        reused = True
                        break
    if not reused:
        nav = client.run(
            ["navigate", search_url_for(query), "--session", session_id],
            timeout=90,
        )
        if not nav.get("ok"):
            return {"ok": False, "reason": "navigate_failed", "raw": sanitize(nav)}
    client.run(
        [
            "wait-for-element",
            "--session",
            session_id,
            "--selector",
            "[data-sku]",
            "--state",
            "visible",
            "--timeout",
            "25s",
        ],
        timeout=40,
    )
    client.run(["wait-ms", "800"], timeout=10)
    state = eval_json(client, session_id, EXTRACT_STATE_JS)
    v = state.get("value") if isinstance(state.get("value"), dict) else {}
    host = str(v.get("host") or "")
    url = str(v.get("url") or "")
    login_required = "passport.jd.com" in host or "/login" in url.lower()
    tip = v.get("restriction_tip")
    if login_required and not tip:
        tip = "LOGIN_REQUIRED:passport.jd.com"
    return {
        "ok": True,
        "reused_tab": reused,
        "state": v,
        "restriction_tip": tip,
        "login_required": login_required,
        "page_hint": v.get("current_page_hint"),
    }


def screenshot(client: BskClient, session_id: str, out: Path) -> str | None:
    out.parent.mkdir(parents=True, exist_ok=True)
    res = client.run(
        ["screenshot", "--session", session_id, "--out", str(out)],
        timeout=60,
    )
    return str(out) if res.get("ok") and out.exists() else None
