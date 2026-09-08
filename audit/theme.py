"""One design system for the tool and the reports it produces.

Direction: a survey instrument. Monospace is the identity face — it carries every
number, identifier and URL, because that is what this tool traffics in — set
against a plain sans for prose. Square corners, hairline rules, no decoration
that does not encode something.

Colour discipline:
  * The accent is deliberately quiet. Severity is the only loud thing on the
    page, so chrome must not compete with it.
  * Severity uses a fixed status palette. Status colour never carries meaning
    alone: every severity is a swatch *plus* a written label.
  * Marks and text use different steps of the same status hue — the mark steps
    are tuned for fills, the text steps clear 4.5:1 on their own surface.

All values below were contrast-checked against the surfaces they sit on.
"""

TOKENS = r"""
:root{
  color-scheme:light;
  --paper:#eef0f4; --surface:#ffffff; --surface-2:#f7f8fa; --surface-3:#eef1f5;
  --ink:#101418; --ink-soft:#4e5866; --ink-faint:#6d7684;
  --rule:#dbe0e7; --rule-soft:#eaeef3;
  --accent:#1a4f8f; --accent-soft:#e8eff8;

  /* status marks — fills and swatches */
  --m-critical:#d03b3b; --m-high:#ec835a; --m-medium:#fab219;
  --m-low:#8b95a3; --m-good:#0ca30c;
  /* status text — same hues, stepped to clear 4.5:1 */
  --t-critical:#a92e2e; --t-high:#8f4419; --t-medium:#755200;
  --t-low:#525b68; --t-good:#0a6d0a;

  --mono:ui-monospace,"Cascadia Mono","SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    color-scheme:dark;
    --paper:#0d1015; --surface:#161b22; --surface-2:#1b212a; --surface-3:#212936;
    --ink:#e6eaf0; --ink-soft:#9aa4b2; --ink-faint:#868f9c;
    --rule:#2a323d; --rule-soft:#222933;
    --accent:#7fb2ea; --accent-soft:#182739;
    --m-critical:#d03b3b; --m-high:#ec835a; --m-medium:#fab219;
    --m-low:#8b95a3; --m-good:#0ca30c;
    --t-critical:#f5938a; --t-high:#f2ac86; --t-medium:#f7c65e;
    --t-low:#9aa4b2; --t-good:#5fd05f;
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --paper:#0d1015; --surface:#161b22; --surface-2:#1b212a; --surface-3:#212936;
  --ink:#e6eaf0; --ink-soft:#9aa4b2; --ink-faint:#868f9c;
  --rule:#2a323d; --rule-soft:#222933;
  --accent:#7fb2ea; --accent-soft:#182739;
  --m-critical:#d03b3b; --m-high:#ec835a; --m-medium:#fab219;
  --m-low:#8b95a3; --m-good:#0ca30c;
  --t-critical:#f5938a; --t-high:#f2ac86; --t-medium:#f7c65e;
  --t-low:#9aa4b2; --t-good:#5fd05f;
}
"""

BASE = r"""
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--paper); color:var(--ink);
  font:15px/1.62 var(--sans);
  font-variant-numeric:tabular-nums;
  -webkit-font-smoothing:antialiased;
}
.mono{font-family:var(--mono)}
h1,h2,h3,h4{text-wrap:balance}
a{color:var(--accent);text-underline-offset:2px}
a:focus-visible,summary:focus-visible,button:focus-visible,
input:focus-visible,[tabindex]:focus-visible{
  outline:2px solid var(--accent);outline-offset:2px}
::selection{background:var(--accent-soft);color:var(--ink)}

/* micro-label: the uppercase key used for every field name */
.k{
  font-family:var(--mono); font-size:10.5px; font-weight:600;
  text-transform:uppercase; letter-spacing:.09em; color:var(--ink-faint);
}

/* severity: a swatch plus a word — colour never carries it alone */
.sev{display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.sev .sw{width:9px;height:9px;flex:none;border:1px solid rgba(0,0,0,.16)}
.sev .lb{font-family:var(--mono);font-size:10.5px;font-weight:700;
  text-transform:uppercase;letter-spacing:.08em}
.sev.critical .sw{background:var(--m-critical)} .sev.critical .lb{color:var(--t-critical)}
.sev.high     .sw{background:var(--m-high)}     .sev.high     .lb{color:var(--t-high)}
.sev.medium   .sw{background:var(--m-medium)}   .sev.medium   .lb{color:var(--t-medium)}
.sev.low      .sw{background:var(--m-low)}      .sev.low      .lb{color:var(--t-low)}
.sev.good     .sw{background:var(--m-good)}     .sev.good     .lb{color:var(--t-good)}

/* proportion bar: composition of one whole, 2px surface gaps between segments */
.propbar{display:flex;height:12px;background:var(--surface-2);
  border:1px solid var(--rule);gap:2px;padding:2px}
.propbar span{display:block;min-width:3px}
.propbar .critical{background:var(--m-critical)}
.propbar .high{background:var(--m-high)}
.propbar .medium{background:var(--m-medium)}
.propbar .low{background:var(--m-low)}
/* validator states reuse the status palette rather than introducing hues:
   error/warning/notice map onto critical/medium/low. */
.propbar .error{background:var(--m-critical)}
.propbar .warning{background:var(--m-medium)}
.propbar .notice{background:var(--m-low)}

/* horizontal magnitude bars: one hue, 4px rounded data-end at the baseline */
.hbars{display:grid;gap:7px}
.hbar{display:grid;grid-template-columns:minmax(0,1fr) 46px;gap:12px;align-items:center}
.hbar .lab{font-size:13px;color:var(--ink-soft);min-width:0}
.hbar .track{position:relative;height:16px;background:var(--surface-2);
  border:1px solid var(--rule-soft)}
.hbar .fill{position:absolute;inset:0 auto 0 0;background:var(--accent);
  border-radius:0 4px 4px 0}
.hbar .fill.muted{background:var(--m-low)}
.hbar .val{font-family:var(--mono);font-size:12.5px;font-weight:700;text-align:right}
.hbar .row{display:grid;grid-template-columns:1fr;gap:4px}

/* theme control: three states, because that is how theming actually works —
   "system" stamps nothing and lets prefers-color-scheme decide, while an
   explicit choice stamps data-theme and wins over the OS in both directions. */
.themeset{display:inline-flex;flex:none;border:1px solid var(--rule);
  background:var(--surface-2);height:26px;align-self:center}
.themeset button{width:31px;height:100%;display:grid;place-items:center;padding:0;
  border:0;background:none;cursor:pointer;color:var(--ink-faint);
  transition:color .12s ease,background-color .12s ease}
.themeset button+button{border-left:1px solid var(--rule)}
.themeset button:hover{color:var(--accent)}
.themeset button[aria-pressed="true"]{background:var(--ink);color:var(--paper)}
.themeset svg{width:13px;height:13px;fill:none;stroke:currentColor;stroke-width:1.6;
  stroke-linecap:round;stroke-linejoin:round}

code{font-family:var(--mono);font-size:.86em;background:var(--surface-2);
  border:1px solid var(--rule-soft);padding:1px 5px;overflow-wrap:break-word}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13.5px;background:var(--surface);
  border:1px solid var(--rule)}
th,td{text-align:left;padding:10px 13px;border-bottom:1px solid var(--rule-soft);
  vertical-align:top}
th{background:var(--surface-2);font-family:var(--mono);font-size:10.5px;
  font-weight:600;text-transform:uppercase;letter-spacing:.09em;color:var(--ink-faint)}
tr:last-child td{border-bottom:0}
td.n,th.n{text-align:right;font-family:var(--mono);font-weight:600;white-space:nowrap}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

SEVERITIES = ("critical", "high", "medium", "low")

# Validator states borrow the status palette instead of adding hues, so a red
# swatch means the same thing everywhere in the report.
STATES = {"error": "critical", "warning": "medium", "notice": "low"}


def css() -> str:
    return TOKENS + BASE


def sev_chip(severity: str, label: str | None = None) -> str:
    """A swatch plus its word — the only sanctioned way to show severity.

    `label` overrides the printed word for states that share a status hue
    (`sev_chip("critical", "error")`); the swatch and the word still travel
    together, which is the point of the component.
    """
    return (f'<span class="sev {severity}"><span class="sw"></span>'
            f'<span class="lb">{label or severity}</span></span>')


def state_chip(state: str) -> str:
    """`error` / `warning` / `notice`, drawn with the matching status swatch."""
    return sev_chip(STATES.get(state, "low"), state)


# --------------------------------------------------------------------------
# Theme control
#
# One implementation for the tool and for the reports it writes, so a report
# behaves the same whether it is served or opened from disk. Icons are inline
# SVG: a published report may run under a policy that blocks every external
# host, and an icon font would fail silently there.
# --------------------------------------------------------------------------

THEME_ICONS = {
    "system": '<rect x="2.2" y="3" width="11.6" height="8" rx="1"/>'
              '<path d="M6 13.4h4"/>',
    "light": '<circle cx="8" cy="8" r="3.1"/><path d="M8 1.4v1.5M8 13.1v1.5'
             'M1.4 8h1.5M13.1 8h1.5M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1'
             'M12.6 3.4l-1.1 1.1M4.5 11.5l-1.1 1.1"/>',
    "dark": '<path d="M13 9.8A5.6 5.6 0 0 1 6.2 3a5.6 5.6 0 1 0 6.8 6.8z"/>',
}
THEME_LABELS = {"system": "Match the system setting",
                "light": "Light theme", "dark": "Dark theme"}


def theme_toggle() -> str:
    """A three-state theme control. Pair it with `theme_script()`."""
    buttons = "".join(
        f'<button type="button" data-theme-set="{key}" aria-pressed="false" '
        f'title="{THEME_LABELS[key]}" aria-label="{THEME_LABELS[key]}">'
        f'<svg viewBox="0 0 16 16" aria-hidden="true">{THEME_ICONS[key]}</svg>'
        f'</button>'
        for key in ("system", "light", "dark"))
    return (f'<div class="themeset" role="group" aria-label="Colour theme">'
            f'{buttons}</div>')


def theme_script() -> str:
    """Applies the stored choice, then keeps every control on the page in sync.

    Written to run as early as it is parsed — the stored theme is stamped on
    `<html>` before the body renders, so there is no flash of the wrong theme.
    The click handler is delegated from `document`, so it works regardless of
    whether the buttons exist yet.
    """
    return """
(() => {
  const KEY = 'auditTheme', root = document.documentElement;
  const sync = v => {
    if (v === 'light' || v === 'dark') { root.setAttribute('data-theme', v); }
    else { root.removeAttribute('data-theme'); v = 'system'; }
    document.querySelectorAll('[data-theme-set]').forEach(b =>
      b.setAttribute('aria-pressed', String(b.dataset.themeSet === v)));
  };
  let current = 'system';
  try { current = localStorage.getItem(KEY) || 'system'; } catch (e) {}
  sync(current);
  document.addEventListener('click', e => {
    const b = e.target.closest && e.target.closest('[data-theme-set]');
    if (!b) return;
    current = b.dataset.themeSet;
    try { localStorage.setItem(KEY, current); } catch (e) {}
    sync(current);
  });
  document.addEventListener('DOMContentLoaded', () => sync(current));
})();
"""
