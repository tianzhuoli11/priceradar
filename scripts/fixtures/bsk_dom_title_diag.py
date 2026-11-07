"""Minimal live DOM diagnosis for JD search card titles (no full HTML dump)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from p5_bsk_runtime import (  # noqa: E402
    BskClient,
    check_prereqs,
    eval_json,
    open_search,
    pick_browser,
    start_session,
    stop_session,
)

DIAG_JS = r"""(() => {
  const cards = Array.from(document.querySelectorAll('[data-sku]'));
  const sample = [];
  const regionHints = {};
  for (let i = 0; i < cards.length && sample.length < 8; i++) {
    const el = cards[i];
    const id = (el.getAttribute('data-sku') || '').trim();
    if (!id) continue;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    const hidden = r.width < 2 || r.height < 2
      || st.display === 'none' || st.visibility === 'hidden';
    // ancestry: class/id crumbs only (no full HTML)
    const crumbs = [];
    let n = el;
    for (let h = 0; h < 8 && n; h++) {
      const cls = typeof n.className === 'string' ? n.className.split(/\s+/).slice(0, 3).join('.') : '';
      crumbs.push({
        tag: n.tagName,
        id: (n.id || '').slice(0, 40),
        cls: cls.slice(0, 80),
      });
      n = n.parentElement;
    }
    const parentSku = el.parentElement && el.parentElement.closest
      ? (el.parentElement.closest('[data-sku]') || {})
      : {};
    const parentSkuId = parentSku.getAttribute
      ? (parentSku.getAttribute('data-sku') || null)
      : null;
    const nested = Array.from(el.querySelectorAll('[data-sku]'))
      .map(x => (x.getAttribute('data-sku') || '').trim())
      .filter(x => x && x !== id)
      .slice(0, 8);
    const attrs = {};
    for (const a of Array.from(el.attributes || [])) {
      if (/^(data-|aria-|title|alt|class|id)/i.test(a.name)) {
        attrs[a.name] = String(a.value || '').slice(0, 120);
      }
    }
    const titled = Array.from(el.querySelectorAll('[title]')).slice(0, 4).map(x => ({
      tag: x.tagName,
      title: (x.getAttribute('title') || '').slice(0, 120),
      text: (x.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 80),
    }));
    const alts = Array.from(el.querySelectorAll('img[alt]')).slice(0, 3).map(x => ({
      alt: (x.getAttribute('alt') || '').slice(0, 120),
    }));
    const aria = Array.from(el.querySelectorAll('[aria-label]')).slice(0, 3).map(x => ({
      tag: x.tagName,
      aria: (x.getAttribute('aria-label') || '').slice(0, 120),
    }));
    const links = Array.from(el.querySelectorAll('a[href]')).slice(0, 4).map(x => ({
      href: (x.getAttribute('href') || '').slice(0, 160),
      title: (x.getAttribute('title') || '').slice(0, 80),
      text: (x.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 60),
    }));
    const textSample = (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 180);
    // guess region from crumbs
    const crumbStr = crumbs.map(c => (c.id + ' ' + c.cls).toLowerCase()).join('|');
    let region = 'unknown';
    if (/recommend|猜你|similar|related|feed/i.test(crumbStr + textSample)) region = 'recommend_or_related';
    else if (/goods|search|result|j-sku|gl-|wrapper|main/i.test(crumbStr)) region = 'likely_main_results';
    regionHints[region] = (regionHints[region] || 0) + 1;
    sample.push({
      index: i,
      id,
      hidden,
      w: Math.round(r.width),
      h: Math.round(r.height),
      y: Math.round(r.top + window.scrollY),
      parent_data_sku: parentSkuId && parentSkuId !== id ? parentSkuId : null,
      nested_data_skus: nested,
      attrs_sample: attrs,
      titled_nodes: titled,
      img_alts: alts,
      aria_labels: aria,
      links,
      text_sample: textSample,
      crumbs: crumbs.slice(0, 6),
      region_guess: region,
    });
  }
  // structure summary: how many data-sku, how many nested
  let nestedCount = 0;
  let hiddenCount = 0;
  const idCounts = {};
  for (const el of cards) {
    const id = (el.getAttribute('data-sku') || '').trim();
    if (!id) continue;
    idCounts[id] = (idCounts[id] || 0) + 1;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || st.display === 'none') hiddenCount += 1;
    const p = el.parentElement && el.parentElement.closest
      ? el.parentElement.closest('[data-sku]') : null;
    if (p && p !== el) nestedCount += 1;
  }
  return {
    host: location.host,
    url: location.href.split('?')[0],
    query_input: (document.querySelector('#key, input[name=keyword]') || {}).value || null,
    page_hint_text: (() => {
      const all = Array.from(document.querySelectorAll('span,div,em,b'));
      for (const el of all) {
        const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
        if (/^\d+\s*\/\s*\d+$/.test(t)) return t;
      }
      return null;
    })(),
    data_sku_node_count: cards.length,
    unique_ids: Object.keys(idCounts).length,
    duplicate_id_nodes: Object.entries(idCounts).filter(([,n]) => n > 1).slice(0, 10),
    nested_data_sku_nodes: nestedCount,
    hidden_data_sku_nodes: hiddenCount,
    region_hints: regionHints,
    samples: sample,
  };
})()"""


def main() -> int:
    os.environ["BSK_AUTO_START"] = "0"
    out_dir = ROOT / "artifacts" / "bsk_dom_diags"
    out_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    import secrets

    run = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_title_diag_{secrets.token_hex(2)}"
    dest = out_dir / run
    dest.mkdir()

    client = BskClient()
    pre = check_prereqs(client)
    if not pre.get("daemon_ready"):
        print("NO_DAEMON")
        return 2
    browser = sys.argv[1] if len(sys.argv) > 1 else None
    pick = pick_browser(pre.get("browsers_list"), browser)
    if not pick.get("selector"):
        print("NO_BROWSER", pick)
        return 2
    sess = start_session(client, str(pick["selector"]))
    sid = sess.get("session_id")
    if not sid:
        print("SESSION_FAIL", sess)
        return 2
    try:
        opened = open_search(client, sid, "清风 B913A", reuse_search_tab=True)
        (dest / "open.json").write_text(
            json.dumps(opened, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        if opened.get("login_required") or opened.get("restriction_tip"):
            print("RESTRICTED", opened.get("restriction_tip"))
            return 3
        d1 = eval_json(client, sid, DIAG_JS)
        (dest / "page1_dom.json").write_text(
            json.dumps(d1.get("value"), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print("page1", json.dumps({
            k: (d1.get("value") or {}).get(k)
            for k in (
                "page_hint_text",
                "data_sku_node_count",
                "unique_ids",
                "nested_data_sku_nodes",
                "hidden_data_sku_nodes",
                "region_hints",
                "query_input",
            )
        }, ensure_ascii=False))
        samples = (d1.get("value") or {}).get("samples") or []
        for s in samples[:3]:
            print(
                "sample",
                s.get("id"),
                "region",
                s.get("region_guess"),
                "title_nodes",
                s.get("titled_nodes"),
                "alts",
                s.get("img_alts"),
                "text",
                (s.get("text_sample") or "")[:80],
            )
        print("out", dest)
        return 0
    finally:
        stop_session(client, sid)


if __name__ == "__main__":
    raise SystemExit(main())
