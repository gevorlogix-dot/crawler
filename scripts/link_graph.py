"""Internal link graph — orphan pages, click depth, and inbound link counts.

A page can be in the sitemap, return 200, and still be invisible to both users
and crawlers if nothing links to it. This builds the actual internal link graph
and reports:

  * orphans          — 0 inbound internal links from any other page
  * nav-only pages   — linked only from the header/footer/mega-menu chrome, with
                       no contextual (in-body) link anywhere. Weak, but not orphaned.
  * click depth      — shortest path from the homepage; >3 is hard to crawl and rank
  * unreachable      — cannot be reached from the homepage at all
  * not-in-sitemap   — linked internally but missing from the sitemap

    python scripts/link_graph.py
    python scripts/link_graph.py --limit 50

Writes artifacts/content/link_graph.json
"""

from __future__ import annotations

import argparse
import json
import os
import time
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urldefrag, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("AMPM_BASE_URL", "https://ampm.testingforproduction.com")
OUT = ROOT / "artifacts" / "content"
SM_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")}

CHROME_TAGS = ("nav", "header", "footer")
CHROME_HINTS = ("menu", "nav", "header", "footer", "breadcrumb", "sidebar", "widget-area")


def norm(u: str) -> str:
    """Canonical form for graph keys: no fragment, no query, trailing slash kept."""
    u = urldefrag(u)[0]
    p = urlparse(u)
    path = p.path or "/"
    if not path.endswith("/") and "." not in path.rsplit("/", 1)[-1]:
        path += "/"
    return f"{p.scheme}://{p.netloc}{path}"


def discover(base: str) -> list[str]:
    r = requests.get(f"{base}/sitemap.xml", headers=UA, timeout=45)
    root = ET.fromstring(r.content)
    children = [e.text for e in root.findall(".//s:sitemap/s:loc", SM_NS)] or [f"{base}/sitemap.xml"]
    urls, seen = [], set()
    for child in children:
        try:
            croot = ET.fromstring(requests.get(child, headers=UA, timeout=45).content)
        except Exception:
            continue
        for loc in croot.findall(".//s:url/s:loc", SM_NS):
            u = norm((loc.text or "").strip())
            if u and u not in seen:
                seen.add(u)
                urls.append(u)
    return urls


def in_chrome(a) -> bool:
    for parent in a.parents:
        if parent.name in CHROME_TAGS:
            return True
        ident = " ".join(parent.get("class") or []) + " " + (parent.get("id") or "")
        if any(h in ident.lower() for h in CHROME_HINTS):
            return True
    return False


def fetch_links(url: str) -> tuple[str, list[tuple[str, str, str]]]:
    """Return (url, [(target, region, anchor_text)])."""
    try:
        r = requests.get(url, headers=UA, timeout=45)
        if r.status_code != 200 or "html" not in r.headers.get("Content-Type", ""):
            return url, []
    except Exception:
        return url, []

    soup = BeautifulSoup(r.text, "html.parser")
    host = urlparse(url).netloc
    out: list[tuple[str, str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        target = norm(urljoin(url, href))
        if urlparse(target).netloc != host or target == url:
            continue
        region = "chrome" if in_chrome(a) else "body"
        out.append((target, region, a.get_text(" ", strip=True)[:80]))
    return url, out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    home = norm(args.base + "/")
    pages = discover(args.base)
    if args.limit:
        pages = pages[: args.limit]
    if home not in pages:
        pages.insert(0, home)
    page_set = set(pages)
    print(f"building link graph over {len(pages)} sitemap URLs ...\n")

    t0 = time.time()
    outbound: dict[str, list] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(fetch_links, u) for u in pages]
        for n, fut in enumerate(as_completed(futures), 1):
            u, links = fut.result()
            outbound[u] = links
            if n % 25 == 0 or n == len(pages):
                print(f"  {n}/{len(pages)} pages linked")

    # ---- inbound maps
    inbound_all: dict[str, set] = defaultdict(set)
    inbound_body: dict[str, set] = defaultdict(set)
    anchors: dict[str, list] = defaultdict(list)
    for src, links in outbound.items():
        for target, region, text in links:
            inbound_all[target].add(src)
            if region == "body":
                inbound_body[target].add(src)
            if text:
                anchors[target].append(text)

    # ---- click depth from the homepage
    depth = {home: 0}
    q = deque([home])
    while q:
        cur = q.popleft()
        for target, _r, _t in outbound.get(cur, []):
            if target in page_set and target not in depth:
                depth[target] = depth[cur] + 1
                q.append(target)

    # A target linked from more than half of all pages is site-wide chrome
    # (header / footer / mega-menu). Everything else is earned linking.
    # This frequency test is far more reliable than guessing from ancestor
    # class names, which Elementor markup makes meaningless.
    boilerplate_cut = len(pages) * 0.5

    orphans, single_inbound, global_nav, deep, unreachable = [], [], [], [], []
    for u in pages:
        n_all = len(inbound_all.get(u, ()))
        if n_all == 0:
            orphans.append(u)
        elif n_all >= boilerplate_cut:
            global_nav.append(u)
        elif n_all == 1:
            single_inbound.append(
                {"url": u, "source": sorted(inbound_all[u])[0]})
        d = depth.get(u)
        if d is None:
            unreachable.append(u)
        elif d > 3:
            deep.append({"url": u, "depth": d})

    linked_not_in_sitemap = sorted(
        {t for links in outbound.values() for t, _r, _x in links
         if t not in page_set and "/wp-content/" not in t and "/cdn-cgi/" not in t}
    )

    rows = sorted(
        ({"url": u,
          "inbound_total": len(inbound_all.get(u, ())),
          "inbound_body": len(inbound_body.get(u, ())),
          "outbound": len(outbound.get(u, [])),
          "depth": depth.get(u),
          "sample_sources": sorted(inbound_all.get(u, ()))[:5],
          "sample_anchors": sorted(set(anchors.get(u, [])))[:5]}
         for u in pages),
        key=lambda r: (r["inbound_total"], r["inbound_body"]),
    )

    payload = {
        "base": args.base,
        "pages": len(pages),
        "elapsed_s": round(time.time() - t0, 1),
        "orphans": orphans,
        "single_inbound": single_inbound,
        "global_nav": global_nav,
        "deep_pages": sorted(deep, key=lambda d: -d["depth"]),
        "unreachable": unreachable,
        "linked_not_in_sitemap": linked_not_in_sitemap,
        "rows": rows,
        "depth_histogram": {
            str(k): sum(1 for u in pages if depth.get(u) == k)
            for k in sorted({d for d in depth.values()})
        },
    }
    (OUT / "link_graph.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"\norphans (0 inbound links)      : {len(orphans)}")
    for u in orphans[:25]:
        print("   ", u)
    print(f"exactly 1 inbound link         : {len(single_inbound)}")
    print(f"site-wide nav/footer links     : {len(global_nav)}")
    print(f"unreachable from homepage      : {len(unreachable)}")
    print(f"click depth > 3                : {len(deep)}")
    print(f"linked but not in sitemap      : {len(linked_not_in_sitemap)}")
    for u in linked_not_in_sitemap[:15]:
        print("   ", u)
    print(f"depth histogram                : {payload['depth_histogram']}")
    print(f"\njson : {OUT / 'link_graph.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
