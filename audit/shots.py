"""Visual evidence: screenshots of the things the checks found.

A finding that says "this image is 640 KB" is a work item. A finding that says
that *and shows you the image, outlined, in place on the page* is a work item
somebody can hand to a designer without opening a browser. This stage runs after
the checks, reads the targets each check attached to its finding, opens the
handful of pages involved once each, and captures a tight, labelled region around
every target.

Design constraints:
  * The report must stay a single standalone file, so every capture is reduced
    and embedded as a data URI. Nothing is written next to the report and nothing
    is fetched when it is read.
  * Captures are bounded — `cfg.shot_limit` in total, `MAX_PER_FINDING` each, and
    `MAX_PAGES` page loads — because this is the one stage whose cost scales with
    how broken the site is.
  * A target that cannot be located is skipped silently. A screenshot of the
    wrong element is worse than none.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

from .config import USER_AGENT, AuditConfig

MAX_PER_FINDING = 3
MAX_PAGES = 10
PAD = 26                    # context around the target, in CSS pixels
MAX_CLIP_H = 760            # a hero image must not produce a 4000px tall figure
OUT_MAX_W = 1000            # embedded width ceiling
JPEG_QUALITY = 62
VIEWPORT = {"width": 1440, "height": 940}

# Injected before every capture. The outline colour is fixed rather than themed:
# it sits on top of the audited site's own pixels, not on the report's surface.
MARK_CSS = """
[data-audit-mark]{outline:3px solid #e02424 !important;
  outline-offset:2px !important;box-shadow:0 0 0 9999px rgba(0,0,0,.02) !important}
#auditBadge{position:absolute;z-index:2147483647;background:#e02424;color:#fff;
  font:700 12px/1.45 ui-monospace,Consolas,monospace;padding:3px 7px;
  border-radius:2px;white-space:nowrap;letter-spacing:.02em;
  box-shadow:0 1px 4px rgba(0,0,0,.35);pointer-events:none}
"""

# Mark an element and return its rect in *document* coordinates.
MARK_JS = r"""
(payload) => {
  const {kind, arg} = payload;
  const clean = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();

  const byImage = () => {
    const want = arg.split('?')[0];
    const tail = want.split('/').pop();
    let best = null;
    for (const i of document.images) {
      const src = (i.currentSrc || i.src || '').split('?')[0];
      if (!src) continue;
      if (src === want || src.endsWith(want) || (tail && src.endsWith(tail))) {
        const r = i.getBoundingClientRect();
        if (!best || r.width * r.height > best.area) best = {el: i, area: r.width * r.height};
      }
    }
    return best && best.el;
  };

  // The element that *renders* the text, found through the text node itself.
  // Matching on textContent instead would return an ancestor, which is how you
  // end up outlining a heading and captioning it with a phrase further down the
  // page. If the text node's own element is not painted, the copy is in the
  // markup but invisible — there is nothing to photograph, and pointing at the
  // nearest visible ancestor would be a lie.
  const rendered = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0';
  };

  const byText = () => {
    const needle = clean(arg).slice(0, 90);
    if (!needle) return null;
    const short = needle.slice(0, 45);
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node, buried = null;
    while ((node = walker.nextNode())) {
      if (!clean(node.nodeValue).includes(short)) continue;
      const host = node.parentElement;
      if (!host || host.closest('script,style,template,noscript')) continue;
      // Templates routinely carry the same copy several times — a hidden mobile
      // variant, then the one on screen. Keep walking for a painted occurrence
      // and only fall back to reporting it unpaintable if there is none.
      if (rendered(host)) return host;
      buried = buried || {hidden: host.tagName.toLowerCase()};
    }
    // The phrase may be split across inline children; take the smallest
    // rendered block that holds all of it.
    let best = null;
    for (const el of document.body.querySelectorAll('*')) {
      if (el.children.length > 3) continue;
      const t = clean(el.textContent);
      if (t.includes(needle) && t.length < needle.length + 400 && rendered(el)
          && (!best || t.length < best.len)) {
        best = {el, len: t.length};
      }
    }
    return best ? best.el : buried;
  };

  const byField = () => {
    const esc = window.CSS && CSS.escape ? CSS.escape(arg) : arg;
    return document.querySelector(`[name="${esc}"]`) ||
           document.getElementById(arg) ||
           document.querySelector(`[type="${esc}"]`);
  };

  const byOverflow = () => {
    const limit = document.documentElement.clientWidth;
    let worst = null, worstOver = 2;
    for (const el of document.querySelectorAll('body *')) {
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      const over = r.right + window.scrollX - limit;
      if (over > worstOver) { worst = el; worstOver = over; }
    }
    return worst;
  };

  const finders = {image: byImage, text: byText, field: byField, overflow: byOverflow,
                   css: () => document.querySelector(arg)};
  let el = null;
  try { el = (finders[kind] || (() => null))(); } catch (e) { el = null; }
  if (!el) return null;
  // Found in the markup, never painted: report it rather than photograph an
  // ancestor that does not contain what the caption claims.
  if (!el.tagName) return {hidden: el.hidden || true};
  if (!rendered(el)) return {hidden: el.tagName.toLowerCase()};

  document.querySelectorAll('[data-audit-mark]').forEach(e =>
    e.removeAttribute('data-audit-mark'));
  el.setAttribute('data-audit-mark', '1');
  el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});

  // Overlays that cover the page would hide the very thing being shown.
  for (const o of document.querySelectorAll('body *')) {
    if (o === el || o.contains(el)) continue;
    const cs = getComputedStyle(o);
    if (cs.position !== 'fixed' && cs.position !== 'sticky') continue;
    const r = o.getBoundingClientRect();
    const cover = (r.width * r.height) / (window.innerWidth * window.innerHeight);
    if (cover > 0.22) o.style.setProperty('visibility', 'hidden', 'important');
  }

  // Coordinates are read *after* scrolling, and reported relative to the
  // viewport: page.screenshot() clips in viewport space unless the whole page is
  // being captured, and rendering a 10,000px document to photograph one image is
  // not a trade worth making.
  const r = el.getBoundingClientRect();
  const out = {
    x: r.left, y: r.top, w: r.width, h: r.height,
    tag: el.tagName.toLowerCase(),
    vw: window.innerWidth, vh: window.innerHeight,
  };

  if (payload.label) {
    const existing = document.getElementById('auditBadge');
    if (existing) existing.remove();
    const badge = document.createElement('div');
    badge.id = 'auditBadge';
    badge.textContent = payload.label;
    document.body.appendChild(badge);
    const top = Math.max(2, r.top + window.scrollY - badge.offsetHeight - 5);
    badge.style.left = Math.max(2, r.left + window.scrollX) + 'px';
    badge.style.top = top + 'px';
    out.badgeY = top - window.scrollY;
  }
  return out;
}
"""


@dataclass
class Target:
    """One thing worth showing, attached to a finding by the check that found it."""
    url: str
    kind: str = "text"        # image | text | field | overflow | css
    arg: str = ""
    label: str = ""           # short badge drawn on the capture
    caption: str = ""         # sentence printed under the figure

    def as_dict(self) -> dict:
        return {"url": self.url, "kind": self.kind, "arg": self.arg,
                "label": self.label, "caption": self.caption}


@dataclass
class Shot:
    finding_id: str
    url: str
    label: str
    caption: str
    b64: str
    mime: str = "image/jpeg"
    w: int = 0
    h: int = 0
    kind: str = ""

    def as_dict(self) -> dict:
        return {"finding_id": self.finding_id, "url": self.url, "label": self.label,
                "caption": self.caption, "mime": self.mime, "w": self.w, "h": self.h,
                "kind": self.kind, "bytes": len(self.b64) * 3 // 4}


@dataclass
class Plan:
    """Targets grouped by page, so each page is loaded once."""
    by_page: dict = field(default_factory=dict)
    total: int = 0


def plan(findings, limit: int) -> Plan:
    """Round-robin across findings so one noisy finding cannot use every slot."""
    queues = [(f.id, list(getattr(f, "targets", []) or [])[:MAX_PER_FINDING])
              for f in findings]
    queues = [(fid, q) for fid, q in queues if q]
    out = Plan()
    while queues and out.total < limit:
        for fid, q in list(queues):
            if not q:
                queues.remove((fid, q))
                continue
            target = q.pop(0)
            if len(out.by_page) >= MAX_PAGES and target.url not in out.by_page:
                continue
            out.by_page.setdefault(target.url, []).append((fid, target))
            out.total += 1
            if out.total >= limit:
                break
    return out


def capture(findings, cfg: AuditConfig, progress=lambda *_: None,
            preview_urls=()) -> tuple[list[Shot], dict]:
    """Screenshot every planned target, and preview any images asked for.

    Both jobs need a browser, so they share one session. Returns
    `(shots, {image url: preview})`.
    """
    todo = plan(findings, cfg.shot_limit)
    preview_urls = list(dict.fromkeys(preview_urls))
    if not todo.total and not preview_urls:
        return [], {}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        progress("Playwright not installed — no screenshots")
        return [], {}

    shots: list[Shot] = []
    previews: dict = {}
    try:
        pw = sync_playwright().start()
    except Exception as exc:
        progress(f"could not start Playwright: {exc.__class__.__name__}")
        return [], {}

    try:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport=VIEWPORT, user_agent=USER_AGENT,
                                  locale="en-US", device_scale_factor=1)
        ctx.add_init_script("window.__audit = true")
        page = ctx.new_page()
        page.set_default_timeout(20_000)

        done = 0
        for url, targets in todo.by_page.items():
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                page.add_style_tag(content=MARK_CSS)
                page.evaluate(
                    "async () => { const s = document.body.scrollHeight;"
                    " for (let y = 0; y < s; y += 700) { window.scrollTo(0, y);"
                    " await new Promise(r => setTimeout(r, 60)); }"
                    " window.scrollTo(0, 0); }")
                page.wait_for_timeout(500)
            except Exception as exc:
                progress(f"{url}: {exc.__class__.__name__}")
                continue

            for fid, target in targets:
                shot = _one(page, fid, target)
                done += 1
                if shot:
                    shots.append(shot)
                progress(f"captured {len(shots)}/{done} targets")

        if preview_urls:
            previews = _previews(page, cfg.base, preview_urls)
            progress(f"{len(previews)} image previews rendered")

        ctx.close()
        browser.close()
    except Exception as exc:
        progress(f"screenshots stopped: {exc.__class__.__name__}")
    finally:
        try:
            pw.stop()
        except Exception:
            pass
    return shots, previews


PREVIEW_PX = 300

# One page holding every image that needs a preview: the browser can rasterise
# formats Pillow cannot (SVG above all), and doing it in a single document means
# one navigation rather than one per image.
PREVIEW_PAGE = """<body style="margin:0;background:#fff">
<div id="strip" style="display:flex;flex-wrap:wrap;gap:4px">%s</div></body>"""
PREVIEW_CELL = ('<div style="width:{px}px;height:{h}px;display:grid;'
                'place-items:center;background:#fff" data-i="{i}">'
                '<img src="{src}" style="max-width:{px}px;max-height:{h}px" '
                'referrerpolicy="no-referrer"></div>')


def _previews(page, base: str, urls: list[str]) -> dict:
    """Render each image at preview size and cut a thumbnail out of the page."""
    out: dict = {}
    cells = "".join(
        PREVIEW_CELL.format(px=PREVIEW_PX, h=int(PREVIEW_PX * 0.62), i=n,
                            src=url.replace('"', "&quot;"))
        for n, url in enumerate(urls))
    try:
        # Same origin as the images, so nothing is blocked as cross-origin.
        page.goto(base.rstrip("/") + "/", wait_until="domcontentloaded", timeout=45_000)
        page.set_content(PREVIEW_PAGE % cells, wait_until="load", timeout=45_000)
        page.wait_for_timeout(700)
    except Exception:
        return out

    for n, url in enumerate(urls):
        try:
            cell = page.query_selector(f'[data-i="{n}"] img')
            if not cell:
                continue
            size = cell.evaluate("i => [i.naturalWidth, i.naturalHeight]")
            if not size or not size[0]:
                continue          # never loaded; a preview of nothing is worse
            raw = cell.screenshot(type="png")
            b64, w, h, mime = _encode(raw)
            if b64:
                out[url] = {"b64": b64, "mime": mime,
                            "natural_w": size[0], "natural_h": size[1]}
        except Exception:
            continue
    return out


def _one(page, finding_id: str, target: Target) -> Shot | None:
    try:
        box = page.evaluate(MARK_JS, {"kind": target.kind, "arg": target.arg,
                                      "label": target.label})
    except Exception:
        return None
    if not box or box.get("hidden"):
        # In the markup but not painted at this viewport. Real, and reported by
        # the finding — just not something a photograph can show.
        return None

    try:
        page.wait_for_timeout(160)
        clip = _clip(box)
        raw = page.screenshot(clip=clip, type="png", animations="disabled")
    except Exception:
        try:
            el = page.query_selector("[data-audit-mark]")
            raw = el.screenshot(type="png") if el else None
        except Exception:
            raw = None
    if not raw:
        return None

    encoded, w, h, mime = _encode(raw)
    if not encoded:
        return None
    return Shot(finding_id=finding_id, url=target.url, label=target.label,
                caption=target.caption, b64=encoded, mime=mime, w=w, h=h,
                kind=target.kind)


# A capture smaller than this shows the element and nothing else, which is not
# evidence — a 30px state icon needs the row it sits in to be recognisable.
MIN_CLIP_W, MIN_CLIP_H = 460, 230


def _clip(box: dict) -> dict:
    """A padded window around the target, in viewport coordinates.

    The target has already been scrolled to the middle of the viewport, so the
    window is grown outward from it and then clamped to what is on screen.
    """
    vw = float(box.get("vw") or VIEWPORT["width"])
    vh = float(box.get("vh") or VIEWPORT["height"])
    top = min(box["y"], box.get("badgeY", box["y"]))
    x, y = box["x"] - PAD, top - PAD
    w = box["w"] + PAD * 2
    h = box["h"] + (box["y"] - top) + PAD * 2

    # Grow a small target outward from its centre until it carries context.
    for want, pos, size in ((MIN_CLIP_W, "x", "w"), (MIN_CLIP_H, "y", "h")):
        have = w if size == "w" else h
        if have < want:
            grow = (want - have) / 2
            if pos == "x":
                x, w = x - grow, want
            else:
                y, h = y - grow, want

    w, h = min(w, vw), min(h, min(vh, float(MAX_CLIP_H)))
    x = min(max(0.0, x), max(0.0, vw - w))
    y = min(max(0.0, y), max(0.0, vh - h))
    return {"x": round(x), "y": round(y),
            "width": round(max(24.0, min(w, vw - x))),
            "height": round(max(24.0, min(h, vh - y)))}


def _encode(raw: bytes) -> tuple[str, int, int, str]:
    """PNG bytes -> a small embeddable JPEG. Falls back to the raw PNG."""
    try:
        import io

        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        im.load()
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        if im.width > OUT_MAX_W:
            im = im.resize((OUT_MAX_W, max(1, round(im.height * OUT_MAX_W / im.width))),
                           Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True,
                progressive=True)
        return (base64.b64encode(buf.getvalue()).decode("ascii"),
                im.width, im.height, "image/jpeg")
    except Exception:
        # Without Pillow the capture still ships, just bigger.
        return base64.b64encode(raw).decode("ascii"), 0, 0, "image/png"
