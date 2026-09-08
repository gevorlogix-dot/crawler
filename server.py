"""Web interface for the site auditor.

Paste a URL, pick what to run, watch it work, read the report.

    python server.py                 # binds every interface, so the office LAN
                                     # can reach it at http://<your-ip>:5000
    python server.py --host 127.0.0.1 # this machine only
    python server.py --port 8080

Each domain keeps exactly one report, at artifacts/audits/<domain>/. Re-auditing
a site replaces it, so the link to a site's report is stable and always current.
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import threading
import traceback
import uuid
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, Response, abort, jsonify, redirect, request, url_for

from audit import AuditConfig, run_audit
from audit.config import IMAGE_MAX_KB
from audit.theme import (SEVERITIES, css as theme_css, theme_script,
                         theme_toggle)

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "artifacts" / "audits"
RESULTS.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

# Hidden fields that turn every optional stage on. Used by both Re-run buttons,
# which carry no form of their own.
RERUN_STAGES = "".join(
    f'<input type="hidden" name="{name}" value="1">'
    for name in ("check_links", "check_external", "check_runtime", "check_security",
                 "check_images", "capture_shots"))


# ------------------------------------------------------------------ jobs

def start_job(cfg: AuditConfig) -> str:
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id, "base": cfg.base, "status": "running",
            "messages": [f"Queued {cfg.base}"], "frac": 0.0,
            "started": datetime.now().astimezone().isoformat(),
            "dir": None, "error": None, "counts": {}, "findings": 0, "elapsed": None,
            "score": None, "grade": "", "scores": {},
        }

    def progress(msg, frac=None):
        with JOBS_LOCK:
            job = JOBS[job_id]
            if not job["messages"] or job["messages"][-1] != msg:
                job["messages"].append(msg)
                del job["messages"][:-200]
            if frac is not None:
                job["frac"] = max(job["frac"], min(1.0, frac))

    def work():
        try:
            result = run_audit(cfg, progress)
            score = result.score
            with JOBS_LOCK:
                JOBS[job_id].update(
                    status="done", frac=1.0, dir=result.report_path.parent.name,
                    counts=result.counts, findings=len(result.findings),
                    elapsed=result.elapsed_s,
                    score=score.overall if score else None,
                    grade=score.grade if score else "",
                    scores=({c.key: c.total for c in score.categories}
                            if score else {}))
        except Exception as exc:
            with JOBS_LOCK:
                JOBS[job_id].update(status="error",
                                    error=str(exc) or exc.__class__.__name__)
            traceback.print_exc()

    threading.Thread(target=work, daemon=True, name=f"audit-{job_id}").start()
    return job_id


def history() -> list[dict]:
    """One entry per domain — the newest report each site has."""
    out = []
    for data_file in RESULTS.glob("*/data.json"):
        try:
            data = json.loads(data_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        score = data.get("score") or {}
        out.append({
            "dir": data_file.parent.name,
            "base": data.get("base", ""),
            "host": urlparse(data.get("base", "")).netloc,
            "finished": data.get("finished", ""),
            "counts": data.get("counts", {}),
            "findings": len(data.get("findings", [])),
            "pages": len(data.get("pages", [])),
            "elapsed": data.get("elapsed_s"),
            "score": score.get("overall"),
            "grade": score.get("grade", ""),
            "band": score.get("band", "low"),
            "scores": score.get("scores") or {},
        })
    out.sort(key=lambda h: h["finished"], reverse=True)
    return out


def ago(iso: str) -> str:
    if not iso:
        return ""
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return iso[:16]
    now = datetime.now(then.tzinfo or timezone.utc)
    secs = max(0, int((now - then).total_seconds()))
    for limit, div, unit in ((60, 1, "s"), (3600, 60, "min"), (86400, 3600, "h")):
        if secs < limit:
            n = secs // div
            return "just now" if unit == "s" and n < 30 else f"{n}{unit} ago"
    days = secs // 86400
    return f"{days}d ago" if days < 30 else then.strftime("%d %b %Y")


# ------------------------------------------------------------------ chrome

PAGE_CSS = r"""
.wrap{max-width:1000px;margin:0 auto;padding:0 22px 80px}
.mast{background:var(--surface);border:1px solid var(--rule);border-top:3px solid var(--ink);
  margin:26px 0 22px;padding:24px 28px}
.mast .eyebrow{display:flex;align-items:center;gap:10px;margin-bottom:11px}
.mast .eyebrow .dash{flex:1;height:1px;background:var(--rule)}
.mast h1{margin:0;font-size:29px;letter-spacing:-.7px;font-weight:700;line-height:1.15}
.mast p{margin:9px 0 0;color:var(--ink-soft);font-size:14.5px;max-width:70ch}

form.run{background:var(--surface);border:1px solid var(--rule);padding:22px 28px;
  margin-bottom:30px}
.field{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:end}
label.k{display:block;margin-bottom:7px}
input[type=url],input[type=number]{width:100%;padding:12px 14px;
  font:15px/1.4 var(--mono);background:var(--surface-2);color:var(--ink);
  border:1px solid var(--rule);border-radius:0}
input::placeholder{color:var(--ink-faint)}
button.go{background:var(--ink);color:var(--paper);border:1px solid var(--ink);
  padding:12px 26px;font:600 15px var(--sans);cursor:pointer;white-space:nowrap}
button.go:hover{background:var(--accent);border-color:var(--accent);color:#fff}
.opts{display:grid;grid-template-columns:repeat(auto-fit,minmax(206px,1fr));
  gap:16px 26px;margin-top:22px;padding-top:19px;border-top:1px solid var(--rule-soft)}
.opt{display:flex;gap:10px;align-items:flex-start}
.opt input{margin:3px 0 0;flex:none;accent-color:var(--accent);width:15px;height:15px}
.opt label{cursor:pointer}
.opt .t{display:block;font-size:13.5px;font-weight:600;color:var(--ink)}
.opt .d{display:block;font-size:12px;color:var(--ink-faint);line-height:1.5;margin-top:2px}
.opt input[type=number]{width:74px;padding:6px 8px;font-size:13px;margin:0}

h2.sec{font-size:15px;margin:0 0 12px;font-weight:700;border-bottom:2px solid var(--ink);
  padding-bottom:9px;display:flex;align-items:baseline;gap:10px}
h2.sec .n{margin-left:auto;font-family:var(--mono);font-size:12px;color:var(--ink-faint);
  font-weight:600}
.site{display:block;font-family:var(--mono);font-size:14px;font-weight:600;
  text-decoration:none;color:var(--ink);word-break:break-all}
.site:hover{color:var(--accent);text-decoration:underline}
.when{font-size:12px;color:var(--ink-faint);margin-top:2px}
.minibar{display:flex;height:8px;gap:2px;min-width:104px;background:var(--surface-2);
  border:1px solid var(--rule-soft);padding:1px}
.minibar span{display:block;min-width:2px}
.minibar .critical{background:var(--m-critical)}.minibar .high{background:var(--m-high)}
.minibar .medium{background:var(--m-medium)}.minibar .low{background:var(--m-low)}
.counts{display:flex;gap:10px;font-family:var(--mono);font-size:12px;margin-top:5px}
.counts b{font-weight:700}
.counts .critical{color:var(--t-critical)}.counts .high{color:var(--t-high)}
.counts .medium{color:var(--t-medium)}.counts .low{color:var(--t-low)}
.rowacts{display:flex;gap:8px;justify-content:flex-end}
.rowacts button,.rowacts a{font:600 11px var(--mono);text-transform:uppercase;
  letter-spacing:.07em;padding:6px 10px;border:1px solid var(--rule);
  background:var(--surface-2);color:var(--ink-soft);cursor:pointer;text-decoration:none}
.rowacts a.open{background:var(--ink);color:var(--paper);border-color:var(--ink)}
.rowacts button:hover,.rowacts a:hover{border-color:var(--accent);color:var(--accent)}
.rowacts a.open:hover{background:var(--accent);color:#fff}
.empty{color:var(--ink-faint);font-size:13.5px;background:var(--surface);
  border:1px solid var(--rule);padding:20px 22px}

/* score column: the number, its grade, then the two categories */
td.scorecell{white-space:nowrap;text-align:right}
td.scorecell .big{font-family:var(--mono);font-size:21px;font-weight:700;
  letter-spacing:-.7px;display:block}
td.scorecell .big.good{color:var(--t-good)}
td.scorecell .big.medium{color:var(--t-medium)}
td.scorecell .big.high{color:var(--t-high)}
td.scorecell .big.critical{color:var(--t-critical)}
td.scorecell .grade{display:block;font:600 10px var(--mono);text-transform:uppercase;
  letter-spacing:.08em;color:var(--ink-soft)}
td.scorecell .split{display:block;font-family:var(--mono);font-size:10.5px;
  color:var(--ink-faint);margin-top:2px}

.card{background:var(--surface);border:1px solid var(--rule);padding:22px 26px;
  margin-bottom:24px}
.statusrow{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.badge{display:inline-flex;align-items:center;gap:7px;font:700 10.5px var(--mono);
  text-transform:uppercase;letter-spacing:.08em;border:1px solid currentColor;padding:4px 9px}
.badge .dot{width:8px;height:8px;background:currentColor;flex:none}
.badge.running{color:var(--accent)}.badge.done{color:var(--t-good)}
.badge.error{color:var(--t-critical)}
.pct{font-family:var(--mono);font-size:12.5px;color:var(--ink-faint);margin-left:auto}
.bar{height:8px;background:var(--surface-2);border:1px solid var(--rule);margin:16px 0 14px}
.bar i{display:block;height:100%;background:var(--accent);width:0;transition:width .45s ease}
.log{background:var(--surface-2);border:1px solid var(--rule-soft);padding:13px 15px;
  max-height:330px;overflow:auto;font-family:var(--mono);font-size:12.5px;line-height:1.75;
  color:var(--ink-faint)}
.log div:last-child{color:var(--ink);font-weight:600}
.actions{display:flex;gap:12px;flex-wrap:wrap;margin-top:18px}
.actions a{display:inline-block;padding:11px 20px;border:1px solid var(--rule);
  text-decoration:none;font-weight:600;font-size:14px;background:var(--surface-2);
  color:var(--ink)}
.actions a.primary{background:var(--ink);color:var(--paper);border-color:var(--ink)}
.actions a:hover{border-color:var(--accent);color:var(--accent)}
.actions a.primary:hover{background:var(--accent);color:#fff}
.err{color:var(--t-critical)}
.foot{margin-top:38px;padding-top:15px;border-top:1px solid var(--rule);
  color:var(--ink-faint);font-size:12.5px}
@media (max-width:640px){
  .field{grid-template-columns:1fr}
  .wrap{padding:0 14px 60px}
  .mast,form.run,.card{padding-left:17px;padding-right:17px}
  td,th{padding:9px 10px}
}
"""


def page(title: str, body: str, tail: str = "") -> Response:
    """`tail` goes just before </body> — scripts must run after the DOM exists."""
    return Response(f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title><style>{theme_css()}{PAGE_CSS}</style>
<script>{theme_script()}</script></head>
<body><div class="wrap">{body}
<div class="foot">Reports live in <span class="mono">artifacts/audits/&lt;domain&gt;/</span> —
one per domain, replaced on each run.</div>
</div>{tail}</body></html>""", mimetype="text/html")


def minibar(counts: dict) -> str:
    total = sum(counts.get(s, 0) for s in SEVERITIES)
    if not total:
        return '<span class="mono" style="color:var(--t-good)">clean</span>'
    segs = "".join(f'<span class="{s}" style="width:{100 * counts.get(s, 0) / total:.3f}%"></span>'
                   for s in SEVERITIES if counts.get(s, 0))
    nums = "".join(f'<span class="{s}"><b>{counts.get(s, 0)}</b></span>'
                   for s in SEVERITIES if counts.get(s, 0))
    return (f'<div class="minibar" role="img" aria-label="'
            + ", ".join(f"{counts.get(s, 0)} {s}" for s in SEVERITIES if counts.get(s, 0))
            + f'">{segs}</div><div class="counts">{nums}</div>')


# ------------------------------------------------------------------ routes

def scorecell(h: dict) -> str:
    """The overall score, its grade, and the two category numbers beneath it."""
    if h.get("score") is None:
        # A withheld score still has a reason, and the reason is the whole point:
        # a bare dash reads as "the run failed" when what happened is that the
        # host refused it. See `score.compute` on why no number is published.
        why = h.get("grade") or "not scored"
        return ('<td class="scorecell">'
                '<span class="big" style="color:var(--ink-faint)">&mdash;</span>'
                f'<span class="grade">not scored</span>'
                f'<span class="split">{escape(why)}</span></td>')
    scores = h.get("scores") or {}
    parts = " · ".join(f"{k[:4].upper()} {v}" for k, v in scores.items()
                       if v is not None)
    return (f'<td class="scorecell"><span class="big {escape(h["band"])}">'
            f'{h["score"]}</span>'
            f'<span class="grade">{escape(h["grade"])}</span>'
            f'<span class="split">{escape(parts)}</span></td>')


@app.get("/")
def index():
    items = history()
    rows = ""
    for h in items:
        rows += (
            f'<tr><td><a class="site" href="{url_for("report", run=h["dir"])}">'
            f'{escape(h["host"])}</a>'
            f'<div class="when">{escape(ago(h["finished"]))} · {h["elapsed"]}s</div></td>'
            f'{scorecell(h)}'
            f'<td class="n">{h["pages"]}</td>'
            f'<td>{minibar(h["counts"])}</td>'
            f'<td class="n">{h["findings"]}</td>'
            f'<td><div class="rowacts">'
            f'<a class="open" href="{url_for("report", run=h["dir"])}">Open</a>'
            f'<form method="post" action="{url_for("run")}" style="display:inline">'
            f'<input type="hidden" name="url" value="{escape(h["base"])}">'
            # Every stage, explicitly: an unticked checkbox sends nothing, so a
            # re-run without these would quietly be a much shallower audit than
            # the one it is replacing.
            f'{RERUN_STAGES}'
            f'<button type="submit">Re-run</button></form>'
            f'</div></td></tr>')

    table = (f'<div class="scroll"><table><thead><tr><th>Site</th>'
             f'<th class="n">Score</th>'
             f'<th class="n">Pages</th><th>Severity</th><th class="n">Findings</th>'
             f'<th></th></tr></thead><tbody>{rows}</tbody></table></div>'
             if rows else
             '<div class="empty">No audits yet. Paste a URL above to run the first one.</div>')

    body = f"""
<header class="mast">
  <div class="eyebrow"><span class="k">Site auditor</span><span class="dash"></span>
    {theme_toggle()}</div>
  <h1>Audit any site</h1>
  <p>Crawls every page it can find, reads the copy, rebuilds the internal link
  graph to find orphans, probes for exposed files, and loads key templates in a
  real browser. You get one report per domain listing every issue — why it
  matters, how to fix it, and the exact pages it was found on.</p>
</header>

<form class="run" method="post" action="{url_for('run')}">
  <div class="field">
    <div>
      <label class="k" for="url">Site URL</label>
      <input id="url" name="url" type="url" required autofocus
             placeholder="https://example.com" autocomplete="url">
    </div>
    <button class="go" type="submit">Run audit</button>
  </div>

  <div class="opts">
    <div class="opt">
      <input type="number" id="max_pages" name="max_pages" value="700" min="1" max="5000">
      <label for="max_pages"><span class="t">Page limit</span>
        <span class="d">Orphan and reachability checks only run when the whole
        site fits inside this limit.</span></label>
    </div>
    <div class="opt">
      <input type="checkbox" id="check_links" name="check_links" checked>
      <label for="check_links"><span class="t">Verify every internal link</span>
        <span class="d">Requests each linked URL to find broken links.</span></label>
    </div>
    <div class="opt">
      <input type="checkbox" id="check_external" name="check_external" checked>
      <label for="check_external"><span class="t">Check outbound links</span>
        <span class="d">Finds dead links to other sites. By far the slowest
        stage — it waits on dozens of third-party servers.</span></label>
    </div>
    <div class="opt">
      <input type="checkbox" id="check_runtime" name="check_runtime" checked>
      <label for="check_runtime"><span class="t">Load pages in a browser</span>
        <span class="d">Catches JavaScript errors, broken images and unlabeled
        form fields. Slower.</span></label>
    </div>
    <div class="opt">
      <input type="checkbox" id="check_security" name="check_security" checked>
      <label for="check_security"><span class="t">Probe for exposed files</span>
        <span class="d">Read-only requests for logs, configs and CMS endpoints.</span></label>
    </div>
    <div class="opt">
      <input type="checkbox" id="check_images" name="check_images" checked>
      <label for="check_images"><span class="t">Measure image weight</span>
        <span class="d">Sizes every image on the site and lists the heavy ones,
        with a thumbnail of each. Weights are the <code>srcset</code> candidate a
        1440px desktop is served, with the 2&times; rendition beside it.</span></label>
    </div>
    <div class="opt">
      <input type="number" id="image_max_kb" name="image_max_kb"
             value="{IMAGE_MAX_KB}" min="1" max="100000">
      <label for="image_max_kb"><span class="t">Image size limit (KB)</span>
        <span class="d">Any single image above this is reported.</span></label>
    </div>
    <div class="opt">
      <input type="checkbox" id="capture_shots" name="capture_shots" checked>
      <label for="capture_shots"><span class="t">Screenshot the findings</span>
        <span class="d">Photographs each problem in place — the heavy image, the
        placeholder text, the unlabeled field — and embeds it in the report.</span></label>
    </div>
    <div class="opt">
      <input type="checkbox" id="staging" name="staging">
      <label for="staging"><span class="t">Treat as a staging site</span>
        <span class="d">Expect noindex everywhere. Auto-detected from the
        hostname when left unticked.</span></label>
    </div>
  </div>
</form>

<h2 class="sec">Audited sites <span class="n">{len(items)}</span></h2>
{table}
"""
    return page("Site auditor", body)


@app.post("/run")
def run():
    try:
        cfg = AuditConfig(
            base=request.form.get("url", ""),
            max_pages=max(1, min(5000, int(request.form.get("max_pages") or 600))),
            check_links=bool(request.form.get("check_links")),
            check_external_links=bool(request.form.get("check_external")),
            check_runtime=bool(request.form.get("check_runtime")),
            check_security=bool(request.form.get("check_security")),
            check_images=bool(request.form.get("check_images")),
            image_max_kb=max(1, min(100_000,
                                    int(request.form.get("image_max_kb") or IMAGE_MAX_KB))),
            capture_shots=bool(request.form.get("capture_shots")),
            expect_noindex=True if request.form.get("staging") else None,
        )
    except ValueError as exc:
        return page("Site auditor", f"""
<header class="mast"><h1>That URL did not work</h1>
<p class="err">{escape(str(exc))}</p></header>
<div class="actions"><a class="primary" href="{url_for('index')}">Try again</a></div>"""), 400

    return redirect(url_for("job_page", job_id=start_job(cfg)))


@app.get("/job/<job_id>")
def job_page(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        abort(404)
    host = urlparse(job["base"]).netloc
    body = f"""
<header class="mast">
  <div class="eyebrow"><span class="k">Running</span><span class="dash"></span>
    {theme_toggle()}</div>
  <h1 class="mono">{escape(host)}</h1>
  <p id="statusline">Crawling the site. This page updates itself — leave it open.</p>
</header>
<div class="card">
  <div class="statusrow">
    <span class="badge running" id="badge"><span class="dot"></span><span id="badgetext">running</span></span>
    <span class="pct" id="pct">0%</span>
  </div>
  <div class="bar"><i id="bar"></i></div>
  <div class="log" id="log"></div>
  <div class="actions" id="actions"></div>
</div>
<div class="actions"><a href="{url_for('index')}">← All audited sites</a></div>
"""
    tail = f"""<script>
(() => {{
  const JOB = {json.dumps(job_id)};
  const logEl = document.getElementById('log');
  const barEl = document.getElementById('bar');
  const pctEl = document.getElementById('pct');
  const badge = document.getElementById('badge');
  const badgeText = document.getElementById('badgetext');
  const actions = document.getElementById('actions');
  const statusline = document.getElementById('statusline');
  const esc = s => s.replace(/[&<>]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c]));

  async function poll() {{
    let res;
    try {{ res = await fetch('/api/job/' + JOB); }}
    catch (e) {{ setTimeout(poll, 2000); return; }}
    const j = await res.json();

    logEl.innerHTML = j.messages.map(m => '<div>' + esc(m) + '</div>').join('');
    logEl.scrollTop = logEl.scrollHeight;
    barEl.style.width = Math.round(j.frac * 100) + '%';
    pctEl.textContent = Math.round(j.frac * 100) + '%';

    if (j.status === 'done') {{
      badge.className = 'badge done'; badgeText.textContent = 'done';
      const c = j.counts || {{}};
      const s = j.scores || {{}};
      const head = j.score == null ? '' :
        'Score ' + j.score + '/100 (' + j.grade + ') — SEO ' + (s.seo ?? '—') +
        ', performance ' + (s.performance ?? '—') + '. ';
      statusline.textContent = head + j.findings + ' findings in ' + j.elapsed +
        's — ' + (c.critical||0) + ' critical, ' + (c.high||0) + ' high, ' +
        (c.medium||0) + ' medium, ' + (c.low||0) + ' low.';
      actions.innerHTML =
        '<a class="primary" href="/report/' + j.dir + '">Open the report</a>' +
        '<a href="/download/' + j.dir + '/report.html">Download HTML</a>' +
        '<a href="/download/' + j.dir + '/data.json">Download JSON</a>';
      return;
    }}
    if (j.status === 'error') {{
      badge.className = 'badge error'; badgeText.textContent = 'error';
      statusline.innerHTML = '<span class="err">' + esc(j.error || 'Unknown error') + '</span>';
      actions.innerHTML = '<a class="primary" href="/">Back</a>';
      return;
    }}
    setTimeout(poll, 1200);
  }}
  poll();
}})();
</script>"""
    return page(f"Auditing {host}", body, tail)


@app.get("/api/job/<job_id>")
def job_api(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        abort(404)
    return jsonify(job)


@app.get("/api/audits")
def audits_api():
    """Every audited domain with its current score. One request, no HTML."""
    return jsonify([
        {"host": h["host"], "base": h["base"], "run": h["dir"],
         "finished": h["finished"], "score": h["score"], "grade": h["grade"],
         "scores": h["scores"], "findings": h["findings"], "pages": h["pages"],
         "counts": h["counts"],
         "report": url_for("report", run=h["dir"], _external=True),
         "data": url_for("score_api", run=h["dir"], _external=True)}
        for h in history()])


@app.get("/api/score/<run>")
def score_api(run):
    """The full score breakdown for one domain: categories, groups, every metric
    with its measurement, its target, the curve that graded it, and its fix.

    `?summary=1` returns only the headline numbers, for a dashboard tile.
    """
    path = _safe(run, "data.json")
    if not path:
        abort(404)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        abort(500)
    score = data.get("score")
    if not score:
        return jsonify({"error": "This audit predates the scoring model. "
                                 "Re-run it to get a score."}), 409
    if request.args.get("summary"):
        return jsonify({
            "host": urlparse(data.get("base", "")).netloc,
            "finished": data.get("finished"),
            "overall": score.get("overall"), "grade": score.get("grade"),
            "scores": score.get("scores"), "confidence": score.get("confidence"),
            "model": score.get("model"),
        })
    return jsonify({"host": urlparse(data.get("base", "")).netloc,
                    "base": data.get("base"), "finished": data.get("finished"),
                    "score": score, "vitals": data.get("vitals")})


# The bar is one row at every width. Controls keep a fixed height and never
# wrap; below 700px the labels drop and the icons carry them, which is the only
# way a four-item bar stays on one line on a phone. The inner container matches
# the report's own 1240px measure so the first button lines up with the masthead
# edge rather than floating in from the window.
REPORT_NAV_CSS = r"""
.auditnav{position:sticky;top:0;z-index:50;background:var(--surface);
  border-bottom:1px solid var(--rule);box-shadow:0 1px 0 rgba(0,0,0,.03)}
.auditnav .navin{max-width:1240px;margin:0 auto;padding:10px 22px;
  display:flex;align-items:center;gap:10px;flex-wrap:nowrap;min-width:0}
.auditnav a,.auditnav button{flex:none;box-sizing:border-box;height:32px;
  display:inline-flex;align-items:center;justify-content:center;gap:7px;
  padding:0 12px;border:1px solid var(--rule);background:var(--surface-2);
  color:var(--ink-soft);text-decoration:none;cursor:pointer;
  font:600 11px/1 var(--mono);text-transform:uppercase;letter-spacing:.08em;
  transition:color .12s ease,border-color .12s ease,background-color .12s ease}
.auditnav form{display:flex;margin:0}
.auditnav .primary{background:var(--ink);color:var(--paper);border-color:var(--ink)}
.auditnav a:hover,.auditnav button:hover{border-color:var(--accent);color:var(--accent);
  background:var(--surface)}
.auditnav .primary:hover{background:var(--accent);color:#fff;border-color:var(--accent)}
/* segmented pair: one shared hairline between them */
.auditnav .group{display:flex;flex:none}
.auditnav .group>*+*,.auditnav .group>form+*{margin-left:-1px}
.auditnav .group>*:hover{position:relative;z-index:1}
.auditnav svg{width:13px;height:13px;flex:none;stroke:currentColor;fill:none;
  stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
/* the one control allowed to shrink — `flex:none` above would otherwise pin the
   host name at full width and push the bar past a narrow viewport */
.auditnav .site{flex:0 1 auto;margin-left:auto;min-width:0;display:flex;
  align-items:center;gap:7px;
  font:600 12px var(--mono);color:var(--ink-faint);text-decoration:none;
  padding-left:10px}
/* min-width:0 on both the flex item and the text inside it: without the inner
   one the host name refuses to shrink and pushes the bar past the viewport. */
.auditnav .site .host{min-width:0;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.auditnav .site:hover{color:var(--accent)}
.auditnav .site .dot{width:7px;height:7px;flex:none;background:var(--m-good);
  border-radius:50%}
@media (max-width:820px){
  .auditnav .navin{padding:9px 14px;gap:8px}
  .auditnav .lbl{display:none}
  .auditnav a,.auditnav button{padding:0 10px}
}
@media print{.auditnav{display:none}}
"""

# Inline, so the bar carries icons without reaching for an external asset.
ICONS = {
    "back": '<path d="M11 3.5 5.5 8l5.5 4.5"/><path d="M5.5 8H14"/>',
    "rerun": '<path d="M13 8a5 5 0 1 1-1.6-3.7"/><path d="M13.2 2v2.7h-2.7"/>',
    "download": '<path d="M8 2.5v7.5"/><path d="M5 7.5 8 10.5l3-3"/>'
                '<path d="M2.8 12.5h10.4"/>',
}


def _icon(name: str) -> str:
    return f'<svg viewBox="0 0 16 16" aria-hidden="true">{ICONS[name]}</svg>'


def _report_nav(run: str, base: str) -> str:
    """Injected when the report is served by the tool, not written into the file —
    the downloadable report has to stay standalone, with no links back to a
    localhost that will not be running."""
    host = urlparse(base).netloc or run
    return (
        f'<style>{REPORT_NAV_CSS}</style>'
        f'<nav class="auditnav"><div class="navin">'
        f'<a class="primary" href="{url_for("index")}" title="Audit another site">'
        f'{_icon("back")}<span class="lbl">Audit another site</span></a>'
        f'<span class="group">'
        f'<form method="post" action="{url_for("run")}">'
        f'<input type="hidden" name="url" value="{escape(base)}">'
        f'{RERUN_STAGES}'
        f'<button type="submit" title="Run this audit again">{_icon("rerun")}'
        f'<span class="lbl">Re-run</span></button></form>'
        f'<a href="{url_for("download", run=run, name="report.html")}"'
        f' title="Download this report as a standalone HTML file">'
        f'{_icon("download")}<span class="lbl">Download</span></a>'
        f'</span>'
        f'<a class="site" href="{escape(base)}" target="_blank" rel="noopener"'
        f' title="Open {escape(host)} in a new tab">'
        f'<span class="dot"></span><span class="host">{escape(host)}</span></a>'
        f'</div></nav>')


@app.get("/report/<run>")
def report(run):
    path = _safe(run, "report.html")
    if not path:
        abort(404)
    html = path.read_text(encoding="utf-8")

    base = ""
    data = _safe(run, "data.json")
    if data:
        try:
            base = json.loads(data.read_text(encoding="utf-8")).get("base", "")
        except Exception:
            base = ""

    nav = _report_nav(run, base or f"https://{run}")
    # Slot the bar in after the report's own <style> so it inherits the theme
    # tokens, and before the content it sits above.
    marker = '<div class="wrap">'
    html = html.replace(marker, nav + marker, 1) if marker in html else nav + html
    return Response(html, mimetype="text/html")


@app.get("/download/<run>/<name>")
def download(run, name):
    if name not in ("report.html", "data.json"):
        abort(404)
    path = _safe(run, name)
    if not path:
        abort(404)
    mime = "text/html" if name.endswith(".html") else "application/json"
    return Response(path.read_bytes(), mimetype=mime,
                    headers={"Content-Disposition": f'attachment; filename="{run}-{name}"'})


@app.post("/delete/<run>")
def delete(run):
    path = _safe(run, "data.json")
    if path:
        shutil.rmtree(path.parent, ignore_errors=True)
    return redirect(url_for("index"))


def _safe(run: str, name: str) -> Path | None:
    """Resolve <run>/<name> and refuse anything escaping the results directory."""
    path = (RESULTS / run / name).resolve()
    try:
        path.relative_to(RESULTS.resolve())
    except ValueError:
        return None
    return path if path.exists() else None


def lan_ip() -> str | None:
    """This machine's address on the LAN, as colleagues would type it.

    Asks the OS which interface it would route through — no packet is sent, and
    the socket is never connected — because enumerating interfaces returns
    loopback and 169.254.x link-local addresses that nobody can reach.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    return None if ip.startswith(("127.", "169.254.")) else ip


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0",
                    help="interface to bind (default: all, so the LAN can reach it; "
                         "pass 127.0.0.1 to keep it on this machine only)")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    print(f"\n  Site auditor -> http://127.0.0.1:{args.port}")
    if args.host == "0.0.0.0":
        ip = lan_ip()
        if ip:
            print(f"  on the network -> http://{ip}:{args.port}")
        print("  (if colleagues time out, Windows Firewall is blocking python.exe "
              "inbound — see scripts/allow_lan_access.ps1)")
    print()
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
