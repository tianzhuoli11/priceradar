"""
P5-G: BrowserSkill-connected Chrome — search 「清风 B913A」 and verify page 2.

Uses Tencent BrowserSkill (`bsk`) against the user's daily Chromium profile via
extension. Does NOT launch browser_profiles/jd_persistent, copy profiles, or
export cookies. Does not modify P3-A / collect-all entrypoints.

Prerequisites (official):
1. bsk CLI installed
2. `bsk daemon start --foreground` running in an independent terminal
   (Cursor agent Job Object cannot breakaway-start the daemon)
3. BrowserSkill extension connected in the target Chrome/Edge
4. User confirms that Chrome can still manually search JD normally
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = (PROJECT_ROOT / "artifacts" / "bsk_search_probes").resolve()
DEFAULT_BSK = Path(os.environ.get("BSK_EXE") or (Path.home() / ".local" / "bin" / "bsk.exe"))
QUERY_DEFAULT = "清风 B913A"
SEARCH_URL = "https://search.jd.com/Search?keyword=%E6%B8%85%E9%A3%8E%20B913A&enc=utf-8"
RULES_VERSION = "p5g-bsk-page2-v2"

# Evidence from 20261026T062211Z: page2 shot showed loading + still "1/12";
# extract used fixed 3.5s wait → 0 cards. Pager strip regex 上一页\s*(\d+)
# misreads strip "上一页 1 2 3…" as current=1 even after a real advance.

SENSITIVE_KEY_RE = re.compile(
    r"(cookie|token|authorization|password|passwd|secret|address|phone|mobile|"
    r"身份证|收货|innerHTML|outerHTML)",
    re.IGNORECASE,
)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


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


class BskClient:
    def __init__(self, exe: Path):
        self.exe = Path(exe)
        if not self.exe.is_file():
            raise FileNotFoundError(f"bsk not found: {self.exe}")

    def run(self, args: list[str], *, timeout: int = 120) -> dict[str, Any]:
        env = os.environ.copy()
        env["BSK_AUTO_START"] = "0"
        env["PATH"] = str(self.exe.parent) + os.pathsep + env.get("PATH", "")
        cmd = [str(self.exe), *args]
        if "--json" not in cmd:
            # insert after exe for machine-readable where supported
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
            return {
                "ok": False,
                "error": f"timeout: {exc}",
                "cmd": cmd,
            }
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
        "checked_at_utc": utc_iso(),
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
    """
    Official: with multiple connected browsers use --browser <id-or-label>.
    """
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
            return {"selector": sel, "source": "single_connected", "browser": sanitize(item)}
        return {"selector": str(item), "source": "single_connected"}
    # Multiple — require explicit selection
    summaries = []
    for it in items:
        if isinstance(it, dict):
            summaries.append(
                {
                    "id": it.get("instance_id") or it.get("id") or it.get("browser_id"),
                    "name": it.get("name") or it.get("label"),
                    "browser": it.get("browser_name")
                    or it.get("browser")
                    or it.get("product")
                    or it.get("type"),
                }
            )
        else:
            summaries.append({"raw": str(it)})
    return {
        "selector": None,
        "error": "multiple_browsers_need_explicit_choice",
        "options": summaries,
    }


# Page/state extract — active page MUST NOT use naive 「上一页 N」on digit strip.
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

  const cards = Array.from(document.querySelectorAll('[data-sku]'));
  const out = [];
  const seen = new Set();
  for (const el of cards) {
    const id = (el.getAttribute('data-sku') || '').trim();
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const titleEl = el.querySelector('a[title], .p-name a, .p-name em, a');
    let title = '';
    if (titleEl) {
      title = (titleEl.getAttribute('title') || titleEl.textContent || '')
        .replace(/\s+/g, ' ').trim();
    }
    out.push({
      jd_product_id: id,
      title: title.slice(0, 160),
      ad_hint: /广告|推广/.test(el.innerText || '') ? 'has_ad_text' : 'unknown_not_false',
    });
  }

  // --- Active page: prefer compact N/M widget; then curr/active digit ---
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
  // Active digit in bottom pager (class hints)
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
  // Bottom strip text for reference only (do NOT take first digit after 上一页 as current)
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

  // --- Next controls: list all exact 「下一页」 leaves ---
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
  const restriction = /访问频繁|无法搜索|验证码|安全验证/.test(body)
    ? (body.match(/.{0,40}(访问频繁|无法搜索|验证码|安全验证).{0,40}/) || ['restriction_hint'])[0]
    : null;

  return {
    url: location.href.split('#')[0],
    host: location.host,
    search_keyword_input: (document.querySelector('#key, input[name=keyword]') || {}).value || null,
    data_sku_count: cards.length,
    unique_ids: out.map(x => x.jd_product_id),
    cards: out.slice(0, 80),
    pager_text,
    current_page_hint,
    total_pages_hint,
    page_source,
    page_sources: pageSources.slice(0, 8),
    next_control: next,
    next_candidates: next_candidates.slice(0, 8),
    loading_hint: loading,
    restriction_tip: restriction,
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

CLICK_PREFERRED_NEXT_JS = r"""(() => {
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
  if (!cands.length) return { ok: false, reason: 'no_unique_enabled_next', count: 0 };
  // Prefer single best; if top two nearly equal y+class, flag ambiguity
  const best = cands[0];
  const ambiguous = cands.length > 1
    && Math.abs(cands[0].y - cands[1].y) < 5
    && cands[0].className === cands[1].className;
  best.el.scrollIntoView({ block: 'center', inline: 'nearest' });
  best.el.click();
  return {
    ok: true,
    className: best.className,
    candidate_count: cands.length,
    ambiguous: !!ambiguous,
  };
})()"""


def parse_active_page_offline(slash_text: str | None, strip_text: str | None, active_digit: int | None) -> dict[str, Any]:
    """Mirror extract rules for offline unit tests (no browser)."""
    current = None
    total = None
    source = "unknown"
    if slash_text:
        m = re.match(r"^<?\s*(\d+)\s*/\s*(\d+)\s*>?$", slash_text.strip())
        if m:
            current, total = int(m.group(1)), int(m.group(2))
            source = "slash_widget"
    if current is None and active_digit is not None:
        current = int(active_digit)
        source = "active_digit"
    # Demonstrate BUG of old heuristic — we deliberately do NOT use it for current
    old_buggy = None
    if strip_text:
        m2 = re.search(r"上一页\s*(\d+)", strip_text)
        if m2:
            old_buggy = int(m2.group(1))
        m3 = re.search(r"共\s*(\d+)\s*页", strip_text)
        if m3 and total is None:
            total = int(m3.group(1))
    return {
        "current_page_hint": current,
        "total_pages_hint": total,
        "page_source": source,
        "legacy_上一页_first_digit": old_buggy,
        "legacy_would_misread": (
            old_buggy is not None and current is not None and old_buggy != current
        ),
    }


def page2_stats_or_unknown(
    *,
    confirmed: bool,
    ids1: list[str],
    ids2: list[str] | None,
) -> dict[str, Any]:
    if not confirmed:
        return {
            "page2_confirmed": False,
            "page2_count": None,
            "new_count": None,
            "overlap_count": None,
            "new_ids_sample": None,
            "overlap_ids_sample": None,
            "note": "第二页未确认：计数为 unknown/null，不是确认空页",
        }
    s1, s2 = set(ids1), set(ids2 or [])
    new_ids = sorted(s2 - s1)
    overlap = sorted(s1 & s2)
    return {
        "page2_confirmed": True,
        "page2_count": len(s2),
        "new_count": len(new_ids),
        "overlap_count": len(overlap),
        "new_ids_sample": new_ids[:20],
        "overlap_ids_sample": overlap[:20],
    }


def eval_json(client: BskClient, session: str, expression: str) -> dict[str, Any]:
    # Pass expression as one argv; avoid shell interpolation.
    res = client.run(["evaluate", "--session", session, expression], timeout=60)
    data = res.get("data")
    # CLI may wrap as {value: ...} or return value directly
    if isinstance(data, dict) and "value" in data and len(data) <= 3:
        value = data.get("value")
    else:
        value = data
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    return {"ok": res.get("ok"), "value": value, "raw": sanitize(res)}


def sanitize_network_entry(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    url = str(entry.get("url") or entry.get("request_url") or "")
    if not url:
        return None
    path = url.split("?", 1)[0]
    # Keep only search/pagination-ish traffic
    if not re.search(r"search|page|list|s\.jd|Search", path, re.I):
        status = entry.get("status") or entry.get("status_code")
        if not entry.get("error") and not entry.get("failed"):
            return None
    return {
        "path": path[:180],
        "status": entry.get("status") or entry.get("status_code"),
        "error": sanitize(str(entry.get("error") or entry.get("errorText") or "")[:160]),
        "method": entry.get("method"),
    }


def wait_grid_and_page(
    client: BskClient,
    session_id: str,
    *,
    before_ids: list[str],
    before_page: int | None,
    timeout_s: int = 28,
) -> dict[str, Any]:
    """Poll until page advances and/or grid has new cards; not a fixed sleep."""
    import time

    started = time.time()
    transitions: list[dict[str, Any]] = []
    last: dict[str, Any] = {}
    before_fp = id_fingerprint(before_ids)
    # First: wait for either loading clear or cards present again
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
        page_ok = isinstance(cur, int) and isinstance(before_page, int) and cur > before_page
        page_ok = page_ok or (isinstance(cur, int) and cur == 2)
        grid_ok = len(ids) > 0 and (new_n > 0 or (page_ok and fp != before_fp))
        if page_ok and len(ids) > 0 and not loading:
            return {
                "ok": True,
                "reason": "page_and_grid_ready",
                "value": v,
                "transitions": transitions,
            }
        if grid_ok and page_ok:
            return {
                "ok": True,
                "reason": "page_advanced_with_new_or_changed_grid",
                "value": v,
                "transitions": transitions,
            }
        # If cards empty but loading, keep waiting; if empty and not loading after 8s, scroll
        if len(ids) == 0 and tr["t_ms"] > 8000:
            client.run(
                ["evaluate", "--session", session_id,
                 "(() => { window.scrollBy(0, Math.floor(innerHeight*0.8)); return true; })()"],
                timeout=30,
            )
        client.run(["wait-ms", "700"], timeout=15)
    return {
        "ok": False,
        "reason": "timeout_waiting_page_or_grid",
        "value": last,
        "transitions": transitions,
    }


def run_probe(
    *,
    query: str,
    browser: str | None,
    bsk_exe: Path,
    dry_check_only: bool = False,
    allow_manual_next: bool = True,
    reuse_search_tab: bool = True,
) -> dict[str, Any]:
    run_id = f"{utc_stamp()}_bsk_page2_{secrets.token_hex(3)}"
    out_dir = OUT_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=False)
    client = BskClient(bsk_exe)
    report: dict[str, Any] = {
        "run_id": run_id,
        "rules_version": RULES_VERSION,
        "connection": "BrowserSkill_extension_local",
        "query": query,
        "started_at_utc": utc_iso(),
        "not_using_jd_persistent": True,
        "final_status": "NOT_STARTED",
        "evidence_prior_failure": {
            "run_id": "20261026T062211Z_bsk_page2_89205d",
            "judgement": [
                "不是先认定京东阻止翻页",
                "page2截图为加载中且顶栏仍1/12；提取在固定3.5s后得到0卡",
                "页码解析用上一页后首数字，在「上一页 1 2 3…」条上会误锁为1",
                "下一页点击返回ok（class _pagination_next_*），但未等待网格更新",
            ],
        },
    }

    pre = check_prereqs(client)
    write_json(out_dir / "prereq.json", pre)
    report["prereq"] = {
        "daemon_ready": pre.get("daemon_ready"),
        "bsk_exe": pre.get("bsk_exe"),
    }
    if not pre.get("daemon_ready"):
        report["final_status"] = "BLOCKED_NO_DAEMON"
        report["user_action_required"] = [
            "在独立 PowerShell（非 Cursor Agent 终端）运行：",
            str((PROJECT_ROOT / "scripts" / "fixtures" / "start_bsk_daemon.ps1").resolve()),
            "或: & \"$env:USERPROFILE\\.local\\bin\\bsk.exe\" daemon start --foreground --daemon-idle 2h",
            "保持该窗口打开。",
            "Chrome 安装扩展: https://chromewebstore.google.com/detail/hhcmgoofomhgciiibhipgmgkgnoenaoi",
            "打开扩展弹窗 → 启用本地连接 → 确认 Connected。",
        ]
        write_json(out_dir / "report.json", sanitize(report))
        write_report_html(out_dir, report)
        print(f"BLOCKED_NO_DAEMON out_dir={out_dir}")
        return report

    pick = pick_browser(pre.get("browsers_list"), browser)
    report["browser_selection"] = pick
    if not pick.get("selector"):
        report["final_status"] = "BLOCKED_BROWSER_SELECTION"
        report["user_action_required"] = [
            "扩展已连上多个浏览器或尚未连接。",
            "运行: $env:BSK_AUTO_START='0'; bsk browsers --json",
            "然后用: --browser <id或名称> 重跑本脚本。",
            "可选浏览器见 report.json browser_selection.options",
        ]
        write_json(out_dir / "report.json", sanitize(report))
        write_report_html(out_dir, report)
        print(f"BLOCKED_BROWSER_SELECTION out_dir={out_dir}")
        return report

    if dry_check_only:
        report["final_status"] = "PREREQ_OK_DRY"
        write_json(out_dir / "report.json", sanitize(report))
        write_report_html(out_dir, report)
        print(f"PREREQ_OK_DRY browser={pick.get('selector')} out_dir={out_dir}")
        return report

    # Start session bound to selected browser
    start = client.run(
        ["session", "start", "--no-focus", "--browser", str(pick["selector"])]
    )
    write_json(out_dir / "session_start.json", sanitize(start))
    session_id = None
    data = start.get("data")
    if isinstance(data, dict):
        session_id = data.get("session_id") or data.get("id") or data.get("session")
    if not start.get("ok") or not session_id:
        report["final_status"] = "SESSION_START_FAILED"
        report["session_start"] = sanitize(start)
        write_json(out_dir / "report.json", sanitize(report))
        write_report_html(out_dir, report)
        print(f"SESSION_START_FAILED out_dir={out_dir}")
        return report

    report["session_id"] = session_id
    report["browser_instance"] = {
        "selector": pick.get("selector"),
        "source": pick.get("source"),
        "browser_meta": pick.get("browser"),
    }

    try:
        reused_tab = False
        if reuse_search_tab:
            tabs = client.run(
                ["tab", "list", "--scope", "user", "--session", session_id]
            )
            write_json(out_dir / "user_tabs.json", sanitize(tabs))
            tab_items = tabs.get("data")
            if isinstance(tab_items, dict):
                tab_items = tab_items.get("tabs") or tab_items.get("items") or []
            if isinstance(tab_items, list):
                for t in tab_items:
                    if not isinstance(t, dict):
                        continue
                    url = str(t.get("url") or "")
                    title = str(t.get("title") or "")
                    tid = t.get("id") or t.get("tab_id")
                    if tid and "search.jd.com" in url and (
                        "B913A" in url or "B913A" in title or "清风" in title
                    ):
                        br = client.run(
                            ["tab", "borrow", str(tid), "--session", session_id],
                            timeout=90,
                        )
                        write_json(out_dir / "tab_borrow.json", sanitize(br))
                        if br.get("ok"):
                            reused_tab = True
                            report["reused_user_search_tab"] = {
                                "tab_id": tid,
                                "url_host": "search.jd.com",
                            }
                            break
        report["reused_tab"] = reused_tab
        if not reused_tab:
            nav = client.run(
                ["navigate", SEARCH_URL, "--session", session_id],
                timeout=90,
            )
            write_json(out_dir / "navigate.json", sanitize(nav))
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

        # Ensure grid ready on page 1
        client.run(["wait-ms", "800"], timeout=15)
        page1 = eval_json(client, session_id, EXTRACT_STATE_JS)
        write_json(out_dir / "page1_extract.json", sanitize(page1))
        v1 = page1.get("value") if isinstance(page1.get("value"), dict) else {}
        ids1 = [str(x) for x in (v1.get("unique_ids") or [])]
        report["page1"] = {
            "page_hint": v1.get("current_page_hint"),
            "page_source": v1.get("page_source"),
            "pager_text": v1.get("pager_text"),
            "total_pages_hint": v1.get("total_pages_hint"),
            "data_sku_count": v1.get("data_sku_count"),
            "unique_count": len(ids1),
            "id_fingerprint": id_fingerprint(ids1) if ids1 else None,
            "ids": ids1[:60],
            "cards_sample": sanitize((v1.get("cards") or [])[:15]),
            "restriction_tip": v1.get("restriction_tip"),
            "url_host": v1.get("host"),
            "search_keyword_input": v1.get("search_keyword_input"),
            "next_candidates": sanitize(v1.get("next_candidates") or []),
        }
        shot1 = out_dir / "page1_viewport.png"
        client.run(
            ["screenshot", "--session", session_id, "--out", str(shot1)],
            timeout=60,
        )
        report["page1"]["screenshot"] = str(shot1) if shot1.exists() else None

        if v1.get("restriction_tip"):
            report["final_status"] = "SEARCH_RESTRICTED"
            report["visible_restriction"] = v1.get("restriction_tip")
            report["page_turn"] = page2_stats_or_unknown(
                confirmed=False, ids1=ids1, ids2=None
            )
            return _finish(client, session_id, out_dir, report)

        if not ids1:
            report["final_status"] = "NO_PRODUCT_GRID"
            report["page_turn"] = page2_stats_or_unknown(
                confirmed=False, ids1=ids1, ids2=None
            )
            return _finish(client, session_id, out_dir, report)

        # Scroll to pager, re-diagnose, screenshot
        scroll_info = eval_json(client, session_id, SCROLL_TO_NEXT_JS)
        write_json(out_dir / "scroll_to_next.json", sanitize(scroll_info))
        client.run(["wait-ms", "500"], timeout=15)
        pre_click = eval_json(client, session_id, EXTRACT_STATE_JS)
        write_json(out_dir / "pre_click_state.json", sanitize(pre_click))
        pv = pre_click.get("value") if isinstance(pre_click.get("value"), dict) else {}
        pager_shot = out_dir / "pager_region_viewport.png"
        client.run(
            ["screenshot", "--session", session_id, "--out", str(pager_shot)],
            timeout=60,
        )
        report["pre_click"] = {
            "url": pv.get("url"),
            "search_keyword_input": pv.get("search_keyword_input"),
            "current_page_hint": pv.get("current_page_hint"),
            "page_source": pv.get("page_source"),
            "page_sources": sanitize(pv.get("page_sources") or []),
            "id_fingerprint": id_fingerprint(
                [str(x) for x in (pv.get("unique_ids") or [])]
            ),
            "next_control": sanitize(pv.get("next_control")),
            "next_candidates": sanitize(pv.get("next_candidates") or []),
            "pager_screenshot": str(pager_shot) if pager_shot.exists() else None,
        }

        next_ctrl = pv.get("next_control") or {}
        cands = pv.get("next_candidates") or []
        enabled_vis = [
            c for c in cands if c.get("visible") and not c.get("disabled")
        ]
        if not next_ctrl or next_ctrl.get("disabled") or not enabled_vis:
            report["final_status"] = "NO_NEXT_CONTROL"
            report["page_turn"] = {
                **page2_stats_or_unknown(confirmed=False, ids1=ids1, ids2=None),
                "ok": False,
                "reason": "no_unique_enabled_visible_next",
            }
            return _finish(client, session_id, out_dir, report)

        before_hint = pv.get("current_page_hint")
        before_fp = id_fingerprint(ids1)
        net_before = client.run(
            ["network", "--session", session_id, "--limit", "20"], timeout=30
        )
        write_json(out_dir / "network_before.json", sanitize(net_before))

        # Prefer native bsk click (pointer events) over element.click().
        # Stamp a unique marker on the preferred enabled next control, then click it.
        mark_js = r"""(() => {
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
        mark_res = eval_json(client, session_id, mark_js)
        write_json(out_dir / "next_mark.json", sanitize(mark_res))
        mark_val = mark_res.get("value") if isinstance(mark_res.get("value"), dict) else {}
        click_path = "none"
        click_res: dict[str, Any] = {"ok": False}
        if mark_val.get("ok"):
            sel = str(mark_val.get("selector") or '[data-pr-next="1"]')
            client.run(
                [
                    "scroll-to",
                    "--session",
                    session_id,
                    "--selector",
                    sel,
                ],
                timeout=30,
            )
            native = client.run(
                [
                    "click",
                    "--session",
                    session_id,
                    "--selector",
                    sel,
                ],
                timeout=45,
            )
            write_json(out_dir / "next_click_native.json", sanitize(native))
            if native.get("ok"):
                click_ok = True
                click_path = "bsk_click_selector"
                click_res = {
                    "ok": True,
                    "value": {
                        "ok": True,
                        "path": click_path,
                        "className": mark_val.get("className"),
                        "selector": sel,
                        "candidate_count": mark_val.get("candidate_count"),
                    },
                }
            else:
                # Fallback: DOM click on same marked element
                js_click = eval_json(client, session_id, CLICK_PREFERRED_NEXT_JS)
                write_json(out_dir / "next_click_js_fallback.json", sanitize(js_click))
                click_ok = bool(
                    (js_click.get("value") or {}).get("ok")
                    if isinstance(js_click.get("value"), dict)
                    else False
                )
                click_path = "js_click_fallback"
                click_res = js_click
        else:
            js_click = eval_json(client, session_id, CLICK_PREFERRED_NEXT_JS)
            write_json(out_dir / "next_click.json", sanitize(js_click))
            click_ok = bool(
                (js_click.get("value") or {}).get("ok")
                if isinstance(js_click.get("value"), dict)
                else False
            )
            click_path = "js_click_only"
            click_res = js_click
        if isinstance(click_res.get("value"), dict):
            click_res["value"]["path"] = click_path
        write_json(out_dir / "next_click.json", sanitize(click_res))
        report["pre_click"]["click_path"] = click_path
        report["pre_click"]["enabled_visible_next_count"] = len(enabled_vis)

        wait_res = wait_grid_and_page(
            client,
            session_id,
            before_ids=ids1,
            before_page=before_hint if isinstance(before_hint, int) else 1,
            timeout_s=28,
        )
        write_json(out_dir / "wait_after_click.json", sanitize(wait_res))

        net_after = client.run(
            ["network", "--session", session_id, "--limit", "40"], timeout=30
        )
        cons = client.run(
            ["console", "--session", session_id, "--limit", "30"], timeout=30
        )
        net_rows = []
        nd = net_after.get("data")
        if isinstance(nd, dict):
            nd = nd.get("entries") or nd.get("items") or nd.get("responses") or []
        if isinstance(nd, list):
            for e in nd:
                s = sanitize_network_entry(e)
                if s:
                    net_rows.append(s)
        write_json(out_dir / "network_after_filtered.json", sanitize(net_rows[:25]))
        write_json(out_dir / "console_after.json", sanitize(cons))

        v2 = wait_res.get("value") if isinstance(wait_res.get("value"), dict) else {}
        ids2_raw = [str(x) for x in (v2.get("unique_ids") or [])]
        after_hint = v2.get("current_page_hint")
        page_advanced = (
            isinstance(after_hint, int)
            and isinstance(before_hint, int)
            and after_hint >= 2
            and after_hint > before_hint
        ) or (isinstance(after_hint, int) and after_hint == 2)
        new_ids = sorted(set(ids2_raw) - set(ids1))
        grid_updated = len(ids2_raw) > 0 and (
            len(new_ids) > 0 or id_fingerprint(ids2_raw) != before_fp
        )
        turn_ok = bool(page_advanced and grid_updated and not v2.get("restriction_tip"))
        # If page says 2 but grid empty → not confirmed empty page of results
        confirmed_page2 = turn_ok

        # Manual assist only if auto click path failed
        manual_used = False
        if not turn_ok and allow_manual_next and click_ok:
            report["manual_assist_prompt"] = (
                "请在 Agent 窗口中、仍打开的京东搜索结果页底部，"
                "手动点击同一个「下一页」按钮一次；完成后在扩展提示中确认。"
                "不要刷新、不要改搜索词、不要连点。"
            )
            print(report["manual_assist_prompt"])
            help_res = client.run(
                [
                    "request-help",
                    "--session",
                    session_id,
                    "--title",
                    "P5-G 请手动点下一页",
                    "--prompt",
                    "请在本搜索结果页点击底部「下一页」一次，等待商品网格出现后确认完成。不要刷新或重新搜索。",
                    "--timeout",
                    "3m",
                ],
                timeout=200,
            )
            write_json(out_dir / "manual_help.json", sanitize(help_res))
            manual_used = True
            if help_res.get("ok"):
                wait_res2 = wait_grid_and_page(
                    client,
                    session_id,
                    before_ids=ids1,
                    before_page=before_hint if isinstance(before_hint, int) else 1,
                    timeout_s=25,
                )
                write_json(out_dir / "wait_after_manual.json", sanitize(wait_res2))
                v2 = wait_res2.get("value") if isinstance(wait_res2.get("value"), dict) else v2
                ids2_raw = [str(x) for x in (v2.get("unique_ids") or [])]
                after_hint = v2.get("current_page_hint")
                page_advanced = (
                    isinstance(after_hint, int) and after_hint >= 2
                )
                new_ids = sorted(set(ids2_raw) - set(ids1))
                grid_updated = len(ids2_raw) > 0 and (
                    len(new_ids) > 0 or id_fingerprint(ids2_raw) != before_fp
                )
                turn_ok = bool(page_advanced and grid_updated)
                confirmed_page2 = turn_ok
                wait_res = wait_res2

        stats = page2_stats_or_unknown(
            confirmed=confirmed_page2, ids1=ids1, ids2=ids2_raw if confirmed_page2 else None
        )
        shot2 = out_dir / "page2_viewport.png"
        client.run(
            ["screenshot", "--session", session_id, "--out", str(shot2)],
            timeout=60,
        )

        report["page2"] = {
            "confirmed": confirmed_page2,
            "page_hint": after_hint,
            "page_source": v2.get("page_source"),
            "pager_text": v2.get("pager_text"),
            "data_sku_count": v2.get("data_sku_count") if confirmed_page2 else None,
            "unique_count": stats.get("page2_count"),
            "id_fingerprint": id_fingerprint(ids2_raw) if ids2_raw else None,
            "ids": ids2_raw[:60] if confirmed_page2 else None,
            "cards_sample": sanitize((v2.get("cards") or [])[:15])
            if confirmed_page2
            else None,
            "restriction_tip": v2.get("restriction_tip"),
            "loading_hint": v2.get("loading_hint"),
            "screenshot": str(shot2) if shot2.exists() else None,
            "raw_observed_card_count_unconfirmed": len(ids2_raw)
            if not confirmed_page2
            else None,
        }
        report["page_turn"] = {
            **stats,
            "ok": turn_ok,
            "auto_click_ok": click_ok,
            "manual_assist_used": manual_used,
            "before_page_hint": before_hint,
            "after_page_hint": after_hint,
            "page_advanced": page_advanced,
            "grid_updated": grid_updated,
            "before_id_fingerprint": before_fp,
            "after_id_fingerprint": id_fingerprint(ids2_raw) if ids2_raw else None,
            "wait_reason": wait_res.get("reason"),
            "transitions": sanitize(wait_res.get("transitions") or [])[-12:],
            "network_filtered": net_rows[:15],
            "success_criteria": "active_page>=2 AND grid updated with real IDs",
        }

        if v2.get("restriction_tip"):
            report["final_status"] = "SEARCH_RESTRICTED_AFTER_TURN"
            report["visible_restriction"] = v2.get("restriction_tip")
        elif turn_ok:
            report["final_status"] = "SEARCH_AND_PAGE2_OK"
            report["reuse_collect_all_min_plan"] = [
                "BrowserSkill 会话复用 P5-F 的逐页采集/翻页校验/checkpoint；",
                "翻页必须：滚动重定位→唯一启用下一页→等待页码与网格双变化；",
                "本轮只验证 1→2，不跑全部页。",
            ]
        elif not page_advanced and not grid_updated:
            report["final_status"] = "SEARCH_OK_PAGE2_FAIL"
        elif page_advanced and not grid_updated:
            report["final_status"] = "PAGE_LABEL_OK_GRID_PENDING"
        else:
            report["final_status"] = "SEARCH_OK_PAGE2_UNCONFIRMED"

        return _finish(client, session_id, out_dir, report)
    except Exception as exc:  # noqa: BLE001
        report["final_status"] = "ERROR"
        report["error"] = f"{type(exc).__name__}: {exc}"
        return _finish(client, session_id, out_dir, report)


def _finish(
    client: BskClient, session_id: str | None, out_dir: Path, report: dict[str, Any]
) -> dict[str, Any]:
    if session_id:
        stop = client.run(["session", "stop", session_id])
        report["session_stop"] = sanitize(stop)
    report["ended_at_utc"] = utc_iso()
    write_json(out_dir / "report.json", sanitize(report))
    write_report_html(out_dir, report)
    print(f"final_status={report.get('final_status')}")
    print(f"out_dir={out_dir}")
    print(f"report={out_dir / 'report.html'}")
    return report


def write_report_html(out_dir: Path, report: dict[str, Any]) -> None:
    esc = html.escape
    path = out_dir / "report.html"
    path.write_text(
        f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<title>BSK page2 probe {esc(str(report.get('run_id')))}</title>
<style>
body{{font-family:"Segoe UI","Microsoft YaHei",sans-serif;margin:24px}}
.warn{{color:#a40}} .ok{{color:#060}} img{{max-width:100%;border:1px solid #ccc}}
</style></head><body>
<h1>BrowserSkill 搜索+第2页验证</h1>
<p>status=<strong class="{'ok' if report.get('final_status')=='SEARCH_AND_PAGE2_OK' else 'warn'}">{esc(str(report.get('final_status')))}</strong></p>
<p>connection={esc(str(report.get('connection')))} · query={esc(str(report.get('query')))}</p>
<p>browser={esc(json.dumps(report.get('browser_instance'), ensure_ascii=False))}</p>
<p class="warn">未使用 jd_persistent；不保存 Cookie/Token/地址/整页HTML。</p>
<pre>{esc(json.dumps(sanitize(report), ensure_ascii=False, indent=2))}</pre>
<h2>截图</h2>
<p>page1: {(report.get('page1') or {}).get('screenshot') or '无'}</p>
<p>page2: {(report.get('page2') or {}).get('screenshot') or '无'}</p>
</body></html>
""",
        encoding="utf-8",
    )


def run_offline_self_checks() -> int:
    failures: list[str] = []
    # Active page: slash widget wins; strip first digit is NOT current
    r = parse_active_page_offline(
        "1/12",
        "上一页 1 2 3 4 5 6 7 下一页 共12页",
        None,
    )
    if r["current_page_hint"] != 1 or r["total_pages_hint"] != 12:
        failures.append(f"slash parse page1: {r}")
    r2 = parse_active_page_offline(
        "2/12",
        "上一页 1 2 3 4 5 6 7 下一页 共12页",
        None,
    )
    if r2["current_page_hint"] != 2:
        failures.append(f"slash parse page2: {r2}")
    if r2.get("legacy_上一页_first_digit") != 1:
        failures.append("legacy buggy digit should still be 1 on strip")
    if not r2.get("legacy_would_misread"):
        failures.append("legacy should misread page2 strip as 1")
    r3 = parse_active_page_offline(None, "上一页 1 2 3 下一页 共12页", 2)
    if r3["current_page_hint"] != 2 or r3["page_source"] != "active_digit":
        failures.append(f"active digit fallback: {r3}")

    # Unknown counts when unconfirmed
    u = page2_stats_or_unknown(confirmed=False, ids1=["a", "b"], ids2=[])
    if u["page2_count"] is not None or u["new_count"] is not None:
        failures.append("unconfirmed must use null counts")
    ok = page2_stats_or_unknown(
        confirmed=True, ids1=["a", "b"], ids2=["b", "c", "d"]
    )
    if ok["page2_count"] != 3 or ok["new_count"] != 2 or ok["overlap_count"] != 1:
        failures.append(f"confirmed stats wrong: {ok}")

    # Duplicate next / disabled selection scoring (pure data)
    cands = [
        {"visible": False, "disabled": False, "score": 0},
        {"visible": True, "disabled": True, "score": 1},
        {"visible": True, "disabled": False, "score": 10},
    ]
    enabled = [c for c in cands if c["visible"] and not c["disabled"]]
    if len(enabled) != 1:
        failures.append("should pick single enabled visible next")

    print("=== P5-G offline self-check ===")
    if failures:
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="P5-G BSK search + page2 probe")
    p.add_argument("--query", default=QUERY_DEFAULT)
    p.add_argument("--browser", default=None, help="bsk browser instance id/label")
    p.add_argument("--bsk-exe", type=Path, default=DEFAULT_BSK)
    p.add_argument("--check-only", action="store_true")
    p.add_argument("--self-check", action="store_true")
    p.add_argument(
        "--no-manual",
        action="store_true",
        help="Do not request human click if auto next fails",
    )
    p.add_argument(
        "--force-navigate",
        action="store_true",
        help="Do not try borrowing an existing user search tab",
    )
    args = p.parse_args(argv)
    if args.self_check:
        return run_offline_self_checks()
    report = run_probe(
        query=args.query,
        browser=args.browser,
        bsk_exe=Path(args.bsk_exe),
        dry_check_only=args.check_only,
        allow_manual_next=not args.no_manual,
        reuse_search_tab=not args.force_navigate,
    )
    status = report.get("final_status")
    if status in {"SEARCH_AND_PAGE2_OK", "PREREQ_OK_DRY"}:
        return 0
    if status and str(status).startswith("BLOCKED"):
        return 3
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
