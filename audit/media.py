"""Image weight measurement, and thumbnails of the offenders.

Two independent measurements, merged:

  * every `<img>` the crawl saw, sized with a HEAD request (site-wide, cheap)
  * every image response the browser sweep actually transferred (a handful of
    pages, but it catches CSS backgrounds and the real `srcset` candidate)

**Which rendition is being weighed is decided before any of this**, by
`srcset.select`: the URL measured site-wide is the candidate a 1440px DPR-1
desktop selects, not the `src` fallback. The two used to disagree badly — a
framework points `src` at the widest rendition in its own srcset, so the HEAD
measured a file no device is served, and the browser then measured the real one
under a *different* URL and both landed in the total. A second, smaller pass
sizes the DPR-2 candidate so a finding can print a retina worst case beside the
typical weight instead of conflating them.

The browser number wins where both exist — **not** the larger of the two. It is
smaller precisely when it matters: the browser negotiated a modern format, or
picked a narrower `srcset` candidate than the widest one in the markup. Taking
the maximum kept the inflated HEAD number every time, which is how an image
costing 2.03 MB was reported at 5.44 MB.

Both measurements are sent with the `Accept` header a browser sends
(`config.IMAGE_ACCEPT`). An image CDN decides what to serve from that header, so
measuring with `*/*` measures a variant nobody is served.

A finding about a heavy image is much easier to act on when you can see which
image it is, so the offenders are also downloaded once and reduced to a small
embedded thumbnail — no external asset, no second request at read time.
"""

from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

from .config import IMAGE_ACCEPT, AuditConfig
from .fetch import session
from .probe import HostGate

# A single image bigger than this is a bug, not a photograph, and reading the
# whole body to size it is not worth the bandwidth.
STREAM_CAP = 8 * 1024 * 1024
THUMB_PX = 320
THUMB_QUALITY = 62


def _measure_one(sess, url: str, timeout: int, gate: HostGate) -> dict:
    """Transferred size of one image, without downloading it if avoidable."""
    rec = {"url": url, "status": None, "bytes": 0, "content_type": "",
           "error": None, "how": "head"}
    host = urlparse(url).netloc.lower()
    if gate.is_down(host):
        rec["error"] = "host unreachable earlier in this run"
        return rec

    # The header that decides which variant we are quoting the size of.
    headers = {"Accept": IMAGE_ACCEPT}
    with gate.sem(host):
        try:
            r = sess.head(url, timeout=timeout, allow_redirects=True,
                          headers=headers)
        except Exception as exc:
            gate.record_failure(host)
            rec["error"] = exc.__class__.__name__
            return rec

        rec["status"] = r.status_code
        rec["content_type"] = r.headers.get("Content-Type", "")
        length = r.headers.get("Content-Length")
        if r.status_code < 400 and length and length.isdigit():
            rec["bytes"] = int(length)
            return rec

        # No Content-Length (chunked, or a server that mishandles HEAD): stream
        # the body and count it, which is the only honest way to get the number.
        try:
            with sess.get(url, timeout=timeout, allow_redirects=True, stream=True,
                          headers=headers) as g:
                rec["status"] = g.status_code
                rec["content_type"] = g.headers.get("Content-Type", rec["content_type"])
                if g.status_code >= 400:
                    return rec
                total = 0
                for chunk in g.iter_content(64 * 1024):
                    total += len(chunk)
                    if total > STREAM_CAP:
                        break
                rec["bytes"] = total
                rec["how"] = "stream"
        except Exception as exc:
            gate.record_failure(host)
            rec["error"] = exc.__class__.__name__
    return rec


def measure(urls, cfg: AuditConfig, progress=lambda *_: None,
            limit: int | None = None) -> dict:
    """Size a set of image URLs concurrently. Returns {url: record}.

    `limit` overrides `cfg.image_limit` so the variant pass gets its own smaller
    bound rather than eating into the site-wide one.
    """
    urls = list(dict.fromkeys(urls))[: cfg.image_limit if limit is None else limit]
    out: dict = {}
    if not urls:
        return out

    sess = session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=cfg.link_workers, pool_maxsize=cfg.link_workers * 2)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)
    gate = HostGate(cfg.per_host, own_host=cfg.host, own_allowance=cfg.workers)

    with ThreadPoolExecutor(max_workers=cfg.link_workers) as pool:
        futs = [pool.submit(_measure_one, sess, u, cfg.link_timeout, gate) for u in urls]
        for n, fut in enumerate(as_completed(futs), 1):
            rec = fut.result()
            out[rec["url"]] = rec
            if n % 40 == 0 or n == len(urls):
                progress(f"{n}/{len(urls)} images measured")
    return out


def image_urls(records: list[dict]) -> dict:
    """{image url: [pages carrying it]}, most-used first."""
    pages: dict[str, list[str]] = {}
    for r in records:
        if r.get("duplicate_of"):
            continue        # the same page under another spelling, not a second page
        for src in (r.get("images") or {}):
            pages.setdefault(src, []).append(r["url"])
    return dict(sorted(pages.items(), key=lambda kv: -len(kv[1])))


def image_markup(records: list[dict]) -> dict:
    """{image url: what the markup said about it}, first page wins.

    The measurement records carry bytes and nothing else, so the srcset facts
    resolved at extraction time — the slot width, the other renditions, how many
    candidates were on offer — have to be reachable separately for a finding to
    explain its own number.
    """
    out: dict[str, dict] = {}
    for r in records:
        for src, meta in (r.get("images") or {}).items():
            if src not in out:
                out[src] = meta
    return out


def variant_urls(markup: dict) -> dict:
    """{chosen url: {"retina": url, "widest": url}} for the srcset images.

    Only renditions that differ from the chosen one appear, so a site without a
    single `srcset` produces an empty mapping and pays for no extra requests.
    """
    out: dict[str, dict] = {}
    for url, meta in markup.items():
        other = {k: meta.get(k) or "" for k in ("retina", "widest")}
        other = {k: v for k, v in other.items() if v and v != url}
        if other:
            out[url] = other
    return out


def variant_targets(variants: dict, exclude: set[str], limit: int) -> list[str]:
    """The distinct extra URLs worth a request, retina before widest.

    The retina pick is a rendition a real device selects, so it is measured
    first and the cap falls on the widest candidate — which is often a file
    nothing selects and is recorded as metadata either way.
    """
    seen: list[str] = []
    for key in ("retina", "widest"):
        for other in variants.values():
            url = other.get(key)
            if url and url not in exclude and url not in seen:
                seen.append(url)
    return seen[:limit]


def merge_variant_sizes(measured: dict, variants: dict, extra: dict) -> dict:
    """Fold the retina/widest measurements onto the record they belong to.

    They are attributed to the chosen URL rather than kept as entries of their
    own: they are the same image, and counting them separately would double a
    site's reported image weight for every `srcset` on it.
    """
    for url, other in variants.items():
        rec = measured.get(url)
        if rec is None:
            continue
        for key in ("retina", "widest"):
            alt = other.get(key)
            if not alt:
                continue
            rec[f"{key}_url"] = alt
            got = (extra.get(alt) or measured.get(alt) or {})
            if got.get("bytes"):
                rec[f"{key}_bytes"] = got["bytes"]
    return measured


def variant_alias(variants: dict) -> dict:
    """{any rendition url: the chosen url it belongs to}.

    The browser may transfer a candidate we did not predict — a `sizes` we could
    not read, a lazy-loading script rewriting `src`. Attributing that response to
    the image it is a rendition of keeps one image as one row; without the alias
    it arrives as a second image with no page and no markup behind it.
    """
    out: dict[str, str] = {}
    for url, other in variants.items():
        for alt in other.values():
            if alt and alt != url:
                out[alt] = url
    return out


def merge_browser_sizes(measured: dict, runtime: list[dict],
                        pages: dict | None = None,
                        alias: dict | None = None) -> dict:
    """Fold the browser's transferred sizes in. The browser is authoritative.

    Authoritative means it replaces the HEAD number, not that it competes with
    it. This used to keep `max(head, browser)`, which sounds safe and is
    backwards: the browser's number is *lower* exactly in the two cases the
    browser exists to catch — a CDN that negotiated WebP or AVIF off the `Accept`
    header, and a `srcset` whose chosen candidate is narrower than the widest URL
    in the markup. So the inflated measurement won every time, and the docstring
    promising otherwise was the only thing standing.

    The HEAD figure is kept as `head_bytes` when the two disagree by more than a
    tenth, because that gap is a measurement fact worth having in `data.json` —
    it is usually the size of the win a modern format is already delivering.

    Anything only the browser saw is also attributed to the page it was seen on,
    otherwise a CSS background image would be reported with no page to fix it on.

    `alias` maps a known rendition back to the image it is a rendition of. The
    browser sometimes transfers a candidate other than the predicted one — a
    `sizes` we could not read, or a script rewriting `src` after load — and
    without the alias that response was filed as a *second* image: the predicted
    URL kept its HEAD weight, the transferred URL was added beside it, and one
    image counted twice in the site total. It also meant the "browser wins" rule
    silently did not apply to the very images it was written for.
    """
    alias = alias or {}
    for page in runtime or []:
        for url, size in (page.get("image_bytes") or {}).items():
            if not size:
                continue      # a cache hit or a 304 says nothing about weight
            key = url if url in measured else alias.get(url, url)
            rec = measured.get(key) or {}
            head_bytes = rec.get("bytes") or 0
            merged = {k: v for k, v in rec.items()
                      if k in ("retina_url", "widest_url",
                               "retina_bytes", "widest_bytes")}
            merged.update({"url": key, "status": rec.get("status", 200),
                           "bytes": int(size), "how": "browser",
                           "content_type": rec.get("content_type", ""),
                           "error": None})
            if key != url:
                merged["browser_url"] = url
            if head_bytes and abs(head_bytes - int(size)) > 0.1 * int(size):
                merged["head_bytes"] = head_bytes
            measured[key] = merged
            if pages is not None and page.get("url") not in pages.setdefault(key, []):
                pages[key].append(page["url"])
    return measured


def kind(url: str, content_type: str = "") -> str:
    """A short format label — `WebP`, `JPEG` — for the report."""
    ct = (content_type or "").lower()
    for token, label in (("webp", "WebP"), ("avif", "AVIF"), ("svg", "SVG"),
                         ("png", "PNG"), ("gif", "GIF"), ("jpeg", "JPEG"),
                         ("jpg", "JPEG")):
        if token in ct:
            return label
    path = urlparse(url).path.lower()
    for ext, label in ((".webp", "WebP"), (".avif", "AVIF"), (".svg", "SVG"),
                       (".png", "PNG"), (".gif", "GIF"), (".jpeg", "JPEG"),
                       (".jpg", "JPEG"), (".ico", "ICO"), (".bmp", "BMP")):
        if path.endswith(ext):
            return label
    return "image"


def modern(fmt: str) -> bool:
    return fmt in ("WebP", "AVIF", "SVG")


def vector(fmt: str) -> bool:
    """Vector art has no meaningful pixel dimensions — an SVG drawn into a 30px
    box is not 'oversized', it is just heavy. Comparing its `naturalWidth` to its
    display width produces a ratio that means nothing."""
    return fmt == "SVG"


def thumbnails(urls: list[str], cfg: AuditConfig, max_px: int = THUMB_PX) -> dict:
    """{url: {b64, mime, natural_w, natural_h}} previews of specific images.

    Downloading a 900 KB image to show it in a report would be absurd, so each
    one is reduced to a ~10 KB JPEG here and embedded. Returns an empty mapping
    if Pillow is not installed — the finding still stands, it just has no picture.
    """
    try:
        from PIL import Image
    except Exception:
        return {}
    import base64

    sess = session()
    out: dict = {}

    def one(url: str):
        try:
            r = sess.get(url, timeout=cfg.timeout, stream=True)
            if r.status_code >= 400:
                return None
            raw = r.raw.read(STREAM_CAP, decode_content=True)
            im = Image.open(io.BytesIO(raw))
            im.load()
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            w, h = im.size
            im.thumbnail((max_px, max_px))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=THUMB_QUALITY, optimize=True)
            return {"b64": base64.b64encode(buf.getvalue()).decode("ascii"),
                    "mime": "image/jpeg", "natural_w": w, "natural_h": h}
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(urls)))) as pool:
        futs = {pool.submit(one, u): u for u in urls}
        for fut in as_completed(futs):
            got = fut.result()
            if got:
                out[futs[fut]] = got
    return out
