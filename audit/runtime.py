"""Rendered-browser sweep.

An HTTP crawl sees the HTML; it does not see what the browser does with it.
This loads a handful of representative pages in Chromium and records JavaScript
exceptions, console errors, failed sub-resources, broken images, unlabeled form
controls, horizontal overflow, **the Core Web Vitals** and **the links that only
exist once JavaScript has run**.

Representative pages are chosen from the link graph — the homepage plus the
most-linked templates — rather than a hardcoded path list, so it adapts to
whatever site is being audited.

Order matters in `sweep()` and it is the one thing not to rearrange:

    goto → read vitals → scroll → measure images

The vitals are read **before** the scroll. Scrolling is what triggers lazy images
so their weight can be measured, and it also creates layout shifts and moves the
largest contentful element — measuring CLS or LCP afterwards would report numbers
no visitor experienced.
"""

from __future__ import annotations

from collections import defaultdict

from . import vitals as vitals_mod
from .config import USER_AGENT, AuditConfig

BROKEN_IMG_JS = """() =>
  [...document.images]
    .filter(i => i.complete && i.naturalWidth === 0)
    .map(i => i.currentSrc || i.src)
    .filter(Boolean)
    .slice(0, 40)"""

# Natural size against the box the image is actually painted into. A 3000px file
# rendered into a 400px slot is the single most common cause of a heavy page, and
# it cannot be seen from the HTML alone.
IMG_METRICS_JS = """() =>
  [...document.images]
    .filter(i => i.currentSrc && i.naturalWidth > 0)
    .map(i => ({
      src: i.currentSrc,
      nw: i.naturalWidth, nh: i.naturalHeight,
      dw: Math.round(i.getBoundingClientRect().width),
      dh: Math.round(i.getBoundingClientRect().height),
      alt: i.getAttribute('alt'),
      lazy: i.loading === 'lazy',
      srcset: !!(i.srcset || (i.parentElement && i.parentElement.tagName === 'PICTURE'
                              && i.parentElement.querySelector('source[srcset]'))),
    }))
    .slice(0, 120)"""

# The widest element sticking out past the document — what to point at when a
# page scrolls sideways.
OVERFLOW_JS = """() => {
  const limit = document.documentElement.clientWidth;
  let worst = null;
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const over = Math.round(r.right + window.scrollX - limit);
    if (over > 2 && (!worst || over > worst.over)) {
      worst = {over, tag: el.tagName.toLowerCase(),
               cls: (el.className || '').toString().slice(0, 80),
               id: el.id || '', width: Math.round(r.width)};
    }
  }
  return worst;
}"""

SCROLL_JS = """async () => {
  const step = Math.max(400, window.innerHeight * 0.9);
  for (let y = 0; y < document.body.scrollHeight; y += step) {
    window.scrollTo(0, y);
    await new Promise(r => setTimeout(r, 90));
  }
}"""

# Every internal link in the rendered DOM. An HTML-only crawl cannot see a
# listing built by script — a state map, a loop grid, a "load more" archive — so
# every page it links to looks orphaned. This is what tells those apart.
RENDERED_LINKS_JS = """() =>
  [...document.querySelectorAll('a[href]')]
    .map(a => a.href)
    .filter(h => /^https?:/.test(h))
    .map(h => h.split('#')[0])
    .filter((h, i, all) => all.indexOf(h) === i)
    .slice(0, 800)"""

# Form controls a screen reader can reach and would announce with nothing.
#
# Two exclusions matter more than the name lookup itself:
#
#   * `aria-hidden="true"` removes an element from the accessibility tree by
#     definition, so it needs no name and no screen reader ever reaches it. Every
#     component library that wraps a native control in a custom one ships a
#     hidden native twin to carry the form value — Radix renders
#     `<select aria-hidden="true" tabindex="-1" style="clip:rect(0,0,0,0)">` — and
#     counting those reported "2 form fields have no accessible label" on a page
#     whose five real controls each had a `<label for>`.
#   * A control that is not painted is not reachable either. Templates carry the
#     same form twice, once for mobile in a `display:none` branch.
#
# `title` counts as a name (it is the last resort in the HTML accessible-name
# computation), and `aria-labelledby` only counts when the ids it names actually
# resolve to text.
UNLABELED_JS = """() => {
  const named = e => {
    if ((e.getAttribute('aria-label') || '').trim()) return true;
    const lb = e.getAttribute('aria-labelledby');
    if (lb && lb.trim().split(' ').some(id => {
      const t = document.getElementById(id);
      return t && (t.textContent || '').trim();
    })) return true;
    if (e.closest('label')) return true;
    if (e.id && document.querySelector(`label[for="${CSS.escape(e.id)}"]`)) return true;
    if ((e.getAttribute('title') || '').trim()) return true;
    if (e.placeholder) return true;
    return false;
  };
  const reachable = e => {
    if (e.closest('[aria-hidden="true"]')) return false;
    if (e.disabled || e.hasAttribute('hidden')) return false;
    const role = (e.getAttribute('role') || '').toLowerCase();
    if (role === 'presentation' || role === 'none') return false;
    const cs = getComputedStyle(e);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    if (e.offsetParent === null && cs.position !== 'fixed') return false;
    return true;
  };
  return [...document.querySelectorAll('input,select,textarea')]
    .filter(e => !['hidden','submit','button','image','reset'].includes(e.type))
    .filter(reachable)
    .filter(e => !named(e))
    .map(e => e.name ? `${e.tagName.toLowerCase()}[name=${e.name}]`
                     : e.id ? `${e.tagName.toLowerCase()}#${e.id}`
                     : `unnamed ${e.tagName.toLowerCase()}`
                       + (e.type ? ` (type=${e.type})` : ''))
    .slice(0, 30);
}"""


def pick_pages(pages: list[dict], graph: dict, base: str, limit: int) -> list[str]:
    """Homepage first, then the most-linked distinct templates.

    The homepage is taken from the crawl rather than constructed, because a page
    is now crawled at the URL the site states — building `base + "/"` here would
    load a redirect of the page the rest of the run measured.
    """
    ranked = sorted(graph.get("rows", []), key=lambda r: -r["inbound"])
    root = base.rstrip("/")
    # Never a URL that turned out to be another spelling of a page already
    # crawled: loading it measures a redirect of the page the rest of the run
    # measured, which is the same fault as constructing `base + "/"`.
    home = next((p["url"] for p in pages
                 if p["url"].rstrip("/") == root and not p.get("duplicate_of")),
                None)
    if home is None:
        home = next((p["url"] for p in pages
                     if not p.get("duplicate_of")
                     and (p.get("final_url") or "").rstrip("/") == root), root + "/")
    picked = [home]
    for r in ranked:
        if len(picked) >= limit:
            break
        if r["url"] not in picked:
            picked.append(r["url"])
    return picked[:limit]


def vitals_sweep(urls: list[str], cfg: AuditConfig,
                 progress=lambda *_: None) -> list[dict]:
    """Core Web Vitals only, on the configured Lighthouse profile.

    A separate pass from `sweep()`, for one reason: the numbers only mean
    something under throttling, and throttling would make every other job in the
    sweep — image weight, overflow, broken images — both slower and measured
    against a device profile those checks were not written for. So this pass is
    small (`cfg.perf_pages` pages), throttled, and measures nothing else.

    Nothing is scrolled and nothing is clicked. LCP is revised upward as larger
    elements paint and CLS accumulates on every shift, so a pass that scrolls
    reports a load no visitor had.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []
    try:
        pw_ctx = sync_playwright().start()
    except Exception as exc:
        progress(f"could not start Playwright: {exc.__class__.__name__}")
        return []

    profile = getattr(cfg, "perf_profile", "mobile")
    mobile = profile == "mobile"
    out: list[dict] = []
    try:
        browser = pw_ctx.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport=vitals_mod.MOBILE_VIEWPORT if mobile
            else {"width": 1440, "height": 960},
            device_scale_factor=1.75 if mobile else 1,
            is_mobile=mobile, has_touch=mobile,
            user_agent=vitals_mod.MOBILE_UA if mobile else USER_AGENT,
            locale="en-US")
        # Before any navigation: LCP, CLS and long-task entries are dispatched
        # during load, and an observer attached afterwards has already missed them.
        ctx.add_init_script(vitals_mod.COLLECT_INIT_JS)
        page = ctx.new_page()
        vitals_mod.throttle(page, profile)

        for n, url in enumerate(urls, 1):
            rec = {"url": url}
            try:
                # `load` rather than `networkidle`: under 1.6 Mbps a page with a
                # chat widget may never go idle, and the vitals are settled long
                # before it would.
                resp = page.goto(url, wait_until="load", timeout=120_000)
                rec["status"] = resp.status if resp else None
                page.wait_for_timeout(2_000)
                rec["vitals"] = page.evaluate(vitals_mod.COLLECT_READ_JS)
            except Exception as exc:
                rec["nav_error"] = exc.__class__.__name__
            out.append(rec)
            progress(f"vitals {n}/{len(urls)} ({profile})")

        ctx.close()
        browser.close()
    except Exception as exc:
        progress(f"vitals sweep stopped: {exc.__class__.__name__}")
    finally:
        try:
            pw_ctx.stop()
        except Exception:
            pass
    return out


def sweep(urls: list[str], cfg: AuditConfig, progress=lambda *_: None,
          links_only: bool = False) -> list[dict]:
    """Load each URL and record what the browser saw.

    `links_only` is the cheap second pass used to explain orphans: it harvests the
    rendered link list and skips every other measurement, because the pages it
    opens were chosen to answer one question.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        progress("Playwright not installed — skipping the browser sweep")
        return []

    results: list[dict] = []
    try:
        pw_ctx = sync_playwright().start()
    except Exception as exc:
        progress(f"could not start Playwright: {exc.__class__.__name__}")
        return []

    # Deliberately desktop and unthrottled, whatever `perf_profile` says. This
    # sweep measures image weight, overflow and broken images at 1440px, and the
    # findings written against it read that way; the Core Web Vitals are measured
    # in `vitals_sweep`, which is where the profile applies.
    try:
        browser = pw_ctx.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 960},
                                  user_agent=USER_AGENT, locale="en-US")
        page = ctx.new_page()

        for n, url in enumerate(urls, 1):
            rec = {"url": url, "page_errors": [], "console_errors": [],
                   "failed_requests": [], "bad_responses": [],
                   "resource_bytes": 0, "resource_count": 0,
                   "image_bytes": {}}
            transfer: dict = defaultdict(int)

            handlers = [
                ("pageerror", lambda e: rec["page_errors"].append(str(e).split("\n")[0][:300])),
                ("console", lambda m: rec["console_errors"].append(m.text[:300])
                    if m.type == "error" else None),
                ("requestfailed", lambda r: rec["failed_requests"].append(
                    {"url": r.url[:250], "type": r.resource_type,
                     "reason": (r.failure or "unknown")[:120]})),
            ]

            def on_response(resp):
                try:
                    if resp.status >= 400:
                        rec["bad_responses"].append(
                            {"url": resp.url[:250], "status": resp.status,
                             "type": resp.request.resource_type})
                    cl = resp.headers.get("content-length")
                    if cl and cl.isdigit():
                        kind = resp.request.resource_type
                        transfer[kind] += int(cl)
                        rec["resource_count"] += 1
                        # Per-URL, not just per-type: the point is to name the
                        # image, and this is the size a visitor actually paid.
                        if kind == "image" and resp.status < 400:
                            rec["image_bytes"][resp.url[:400]] = int(cl)
                except Exception:
                    pass

            for ev, fn in handlers:
                page.on(ev, fn)
            page.on("response", on_response)

            try:
                resp = page.goto(url, wait_until="networkidle", timeout=90_000)
                rec["status"] = resp.status if resp else None
                # Lazy-loaded images never load in a browser that never scrolls,
                # so their weight would be invisible to this stage. Walk the page
                # once to trigger them, then return to the top.
                page.evaluate(SCROLL_JS)
                page.wait_for_timeout(400)
                try:
                    page.wait_for_load_state("networkidle", timeout=8_000)
                except Exception:
                    pass
                page.evaluate("() => window.scrollTo(0, 0)")
                # Links are harvested *after* the scroll, on purpose: an
                # infinite-scroll archive appends the links this pass exists to
                # find, and unlike the vitals they are not distorted by scrolling.
                try:
                    rec["rendered_links"] = page.evaluate(RENDERED_LINKS_JS)
                except Exception:
                    rec["rendered_links"] = []
            except Exception as exc:
                rec["nav_error"] = f"{exc.__class__.__name__}"

            if links_only:
                for ev, fn in handlers:
                    page.remove_listener(ev, fn)
                page.remove_listener("response", on_response)
                results.append(rec)
                progress(f"rendered {n}/{len(urls)} pages")
                continue

            try:
                rec["broken_images"] = page.evaluate(BROKEN_IMG_JS)
                rec["unlabeled_inputs"] = page.evaluate(UNLABELED_JS)
                rec["h_overflow"] = page.evaluate(
                    "() => document.documentElement.scrollWidth"
                    " - document.documentElement.clientWidth")
                rec["visible_words"] = page.evaluate(
                    "() => (document.body.innerText||'').trim().split(/\\s+/).length")
                rec["img_metrics"] = page.evaluate(IMG_METRICS_JS)
                if rec["h_overflow"]:
                    rec["overflow_element"] = page.evaluate(OVERFLOW_JS)
            except Exception:
                pass

            rec["transfer_by_type"] = dict(transfer)
            rec["resource_bytes"] = sum(transfer.values())

            for ev, fn in handlers:
                page.remove_listener(ev, fn)
            page.remove_listener("response", on_response)

            results.append(rec)
            progress(f"rendered {n}/{len(urls)} pages")

        ctx.close()
        browser.close()
    except Exception as exc:
        progress(f"browser sweep stopped: {exc.__class__.__name__}")
    finally:
        try:
            pw_ctx.stop()
        except Exception:
            pass
    return results
