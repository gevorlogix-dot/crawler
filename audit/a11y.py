"""Accessibility auditing in the rendered page — the axe-core rules, reimplemented.

**Staged, not wired in.** The scored categories today are SEO and Performance
(`score.py`); Accessibility and Best practices are the next two. This module is
the collector for the first of them — complete and self-contained, but nothing
calls it yet, so it costs nothing at runtime. To turn it on: evaluate `AUDIT_JS`
in the runtime sweep, store the result as `rec["a11y"]`, and add a category in
`score.py` built from `summarise()`.

Lighthouse's accessibility score is a weighted average of axe-core rules, each
scored pass/fail on one page. axe itself cannot be used here: a published report
runs under a policy that blocks every external host, and the audit has to work
offline, so the rules that matter most are reimplemented against the live DOM in
`AUDIT_JS` and weighted the way axe weights them.

What that means for the number, stated plainly because it is the difference
between this score and Lighthouse's:

* **Nineteen rules, not fifty.** The set below covers every rule axe classes as
  *critical* or *serious* that can be decided from the DOM, plus the useful
  moderate ones. Rules needing human judgement (does this alt text describe the
  image?) are out, exactly as they are in Lighthouse.
* **Weights follow axe's impact levels** — critical 10, serious 7, moderate 3,
  minor 1 — which is the same basis Lighthouse's per-audit weights use.
* **A rule that finds nothing to test is dropped, not passed.** A page with no
  `<iframe>` cannot pass or fail `frame-title`, and counting it as a pass would
  inflate every score.
* **Site-wide, a rule scores the share of rendered pages that pass it.**
  Lighthouse scores one page, where every rule is binary. Averaging the binary
  result across the pages we render is the same rule applied to a site — and the
  report also prints the homepage's own strict binary score, which is the number
  directly comparable to a Lighthouse run.

Colour contrast is the one rule where a reimplementation can disagree with axe:
it resolves the background by walking ancestors for the first opaque
`background-color`, and **skips any element sitting on a background image or
gradient** rather than guessing. axe does the same, and both report the same
"could not determine" rather than a wrong ratio.
"""

from __future__ import annotations

# impact -> weight, the way axe grades severity.
IMPACT_WEIGHT = {"critical": 10, "serious": 7, "moderate": 3, "minor": 1}


class Rule:
    """One accessibility rule: what it tests, and what to do when it fails."""

    def __init__(self, key, label, impact, why, fix, wcag=""):
        self.key = key
        self.label = label
        self.impact = impact
        self.weight = IMPACT_WEIGHT[impact]
        self.why = why
        self.fix = fix
        self.wcag = wcag


RULES = [
    Rule("image-alt", "Images carry an alt attribute", "critical",
         "A screen reader announces an image with no <code>alt</code> by reading its "
         "file name, so a visitor hears “IMG_20240714-hero-final-2.jpg” where the "
         "page shows a photograph. Where the image is a link, the alt text is the "
         "only name that link has.",
         "Add <code>alt</code> to every <code>&lt;img&gt;</code>. Describe what the "
         "image conveys in this context, not what it depicts in general — for a "
         "product shot, the product name; for a linked logo, the destination "
         "(<code>alt=\"AMPM Auto Transport — home\"</code>). Purely decorative art "
         "takes an explicitly empty <code>alt=\"\"</code>, which tells the screen "
         "reader to skip it. An empty alt is correct; a missing one never is.",
         "WCAG 1.1.1"),
    Rule("input-image-alt", "Image buttons carry an alt attribute", "critical",
         "An <code>&lt;input type=\"image\"&gt;</code> is a submit button. Without "
         "alt text it is announced as an unlabelled button, so the visitor cannot "
         "know what submitting will do.",
         "Add <code>alt</code> describing the action, not the picture: "
         "<code>&lt;input type=\"image\" src=\"search.svg\" alt=\"Search\"&gt;</code>.",
         "WCAG 1.1.1"),
    Rule("button-name", "Buttons have an accessible name", "critical",
         "A button with no text, no <code>aria-label</code> and no titled icon is "
         "announced as just “button”. Icon-only controls — a hamburger menu, a "
         "close X, a play button — are where this happens, and they are usually the "
         "controls that matter most.",
         "Give the control a name in one of three ways: visible text inside the "
         "button, <code>aria-label=\"Close\"</code> on the button itself, or "
         "<code>&lt;title&gt;Close&lt;/title&gt;</code> inside an inline SVG icon. "
         "Do not label with <code>title=</code> alone — it is unreliable on touch "
         "devices — and do not put the name only in a CSS pseudo-element, which no "
         "assistive technology can read.",
         "WCAG 4.1.2"),
    Rule("link-name", "Links have an accessible name", "serious",
         "An unnamed link is announced as “link” with no destination. Screen-reader "
         "users navigate by pulling up a list of every link on the page, and an "
         "unnamed one is unusable in that list. Search engines read anchor text the "
         "same way — an unnamed link passes no signal about its target either.",
         "Put text in the link. Where the design is icon-only, add "
         "<code>aria-label</code> naming the destination, or wrap visible text in a "
         "visually-hidden span (clipped, not <code>display:none</code>, which "
         "removes it from the accessibility tree too). For a linked image, the "
         "image's <code>alt</code> becomes the link's name — use it.",
         "WCAG 2.4.4"),
    Rule("label", "Form fields have a label", "critical",
         "An unlabelled field is announced by its type alone: “edit text”. The "
         "visitor has to guess what to type, and any validation error that follows "
         "cannot be attached to anything they can identify.",
         "Pair every control with a <code>&lt;label for=\"id\"&gt;</code>, or wrap "
         "the control in its label. Where the design has no room for a visible "
         "label, <code>aria-label</code> on the control is acceptable. A "
         "<code>placeholder</code> is not a label: it disappears the moment typing "
         "starts, it fails contrast requirements in most themes, and some browsers "
         "do not expose it as a name at all.",
         "WCAG 1.3.1, 4.1.2"),
    Rule("label-placeholder-only", "Fields labelled by more than a placeholder",
         "moderate",
         "These fields have a placeholder and nothing else. The name vanishes as "
         "soon as the visitor types, so anyone interrupted mid-form loses track of "
         "which field is which — and on a long form, so does everybody else.",
         "Add a real <code>&lt;label&gt;</code> above the field and keep the "
         "placeholder for an example value (<code>placeholder=\"90001\"</code>), "
         "which is what it is for.",
         "WCAG 3.3.2"),
    Rule("color-contrast", "Text meets the WCAG AA contrast ratio", "serious",
         "Low-contrast text is unreadable in bright light, on a cheap screen, and "
         "for the very large number of people with even mild vision loss. It is the "
         "single most common accessibility failure on the web, and it is usually a "
         "brand grey applied to body copy.",
         "Normal text needs a contrast ratio of at least 4.5:1 against its "
         "background; text at 24px or larger (or 18.66px bold) needs 3:1. Darken the "
         "text rather than lightening the background — the ratios below list the "
         "exact colours involved, so each one can be fixed at its source in the "
         "theme. Grey-on-white body copy usually needs to reach about "
         "<code>#595959</code> to clear 4.5:1 on white.",
         "WCAG 1.4.3"),
    Rule("html-has-lang", "The page declares a language", "serious",
         "Without <code>&lt;html lang&gt;</code> a screen reader reads the page in "
         "whatever voice it defaults to, so English content can be pronounced with "
         "Spanish phonetics. It also drives hyphenation and the correct quotation "
         "marks.",
         "Set it on the root element: <code>&lt;html lang=\"en\"&gt;</code>. Mark "
         "any passage in another language inline with its own "
         "<code>lang</code> attribute.",
         "WCAG 3.1.1"),
    Rule("html-lang-valid", "The declared language is a valid code", "serious",
         "An invalid or misspelled language tag is ignored, which leaves the page in "
         "the same position as having none at all.",
         "Use a BCP 47 tag: <code>en</code>, <code>en-US</code>, <code>es-MX</code>. "
         "Not a name (<code>English</code>), not a locale with an underscore "
         "(<code>en_US</code>).",
         "WCAG 3.1.1"),
    Rule("document-title", "The page has a title", "serious",
         "The title is the first thing announced when a page loads and the only "
         "thing distinguishing one open tab from another. It is also the headline of "
         "the search result.",
         "Give every page a unique <code>&lt;title&gt;</code> naming that page "
         "first and the site second.",
         "WCAG 2.4.2"),
    Rule("meta-viewport", "Zooming is not disabled", "critical",
         "<code>user-scalable=no</code> or a maximum scale below 2 stops a visitor "
         "pinching to zoom. For anyone who enlarges text to read it, that turns the "
         "page into an image they cannot get closer to.",
         "Use <code>&lt;meta name=\"viewport\" "
         "content=\"width=device-width, initial-scale=1\"&gt;</code> and nothing "
         "else. Remove <code>user-scalable=no</code> and any "
         "<code>maximum-scale</code> below 5.",
         "WCAG 1.4.4"),
    Rule("heading-order", "Heading levels are not skipped", "moderate",
         "Headings are the table of contents a screen-reader user navigates by. "
         "Jumping from an H2 to an H4 implies a missing section, so the structure "
         "they are navigating does not match the page they are on.",
         "Use heading levels in order and choose them for structure, never for "
         "size — set the size in CSS. A page builder that renders section titles as "
         "H4 because H4 looks right is the usual cause.",
         "WCAG 1.3.1"),
    Rule("empty-heading", "Headings are not empty", "minor",
         "An empty heading is announced as a heading with no content, which breaks "
         "the outline of the page for anyone navigating heading to heading.",
         "Remove the empty element, or give it its text. Spacer headings left "
         "behind by a page builder are the usual source.",
         "WCAG 1.3.1"),
    Rule("duplicate-id-active", "Interactive elements have unique ids", "serious",
         "Two elements sharing an id break every reference that points at either "
         "one: <code>label[for]</code> resolves to the first match, so the second "
         "field silently loses its label, and <code>aria-labelledby</code> does the "
         "same.",
         "Make each id unique. Where a form or widget is repeated in a template "
         "(a mobile and a desktop copy of the same search box), suffix the ids per "
         "instance rather than shipping both with the same one.",
         "WCAG 4.1.1"),
    Rule("aria-hidden-focus", "Hidden content contains nothing focusable", "serious",
         "<code>aria-hidden=\"true\"</code> removes an element from the "
         "accessibility tree but not from the tab order. A focusable control inside "
         "one can be tabbed to and then announces nothing — the keyboard focus "
         "simply disappears.",
         "Either drop the <code>aria-hidden</code>, or take the contents out of the "
         "tab order with <code>tabindex=\"-1\"</code> — and use the "
         "<code>inert</code> attribute for a whole hidden region, which does both "
         "correctly.",
         "WCAG 4.1.2"),
    Rule("list", "Lists contain only list items", "serious",
         "A <code>&lt;ul&gt;</code> with other elements as direct children stops "
         "being announced as a list, so the visitor loses both the item count and "
         "the ability to jump through it.",
         "Only <code>&lt;li&gt;</code> (plus <code>&lt;script&gt;</code> and "
         "<code>&lt;template&gt;</code>) may be a direct child of "
         "<code>&lt;ul&gt;</code> or <code>&lt;ol&gt;</code>. Move wrappers and "
         "divs inside the <code>&lt;li&gt;</code>.",
         "WCAG 1.3.1"),
    Rule("frame-title", "Frames have a title", "serious",
         "An untitled <code>&lt;iframe&gt;</code> is announced as “frame”, so the "
         "visitor has to enter it to find out whether it is a map, a video or an "
         "advertisement.",
         "Add <code>title</code> naming the embedded content: "
         "<code>&lt;iframe title=\"Route map\"&gt;</code>. Embeds injected by a "
         "third-party script usually accept a title parameter.",
         "WCAG 4.1.2"),
    Rule("tabindex", "No positive tabindex values", "serious",
         "A positive <code>tabindex</code> pulls an element to the front of the tab "
         "order, ahead of everything in document order. One of them is enough to "
         "make the whole page's keyboard order unpredictable.",
         "Use <code>tabindex=\"0\"</code> to make something focusable and "
         "<code>tabindex=\"-1\"</code> to take it out of the order. If the tab "
         "sequence is wrong, fix the DOM order instead — visual order should follow "
         "it, not the other way round.",
         "WCAG 2.4.3"),
    Rule("target-size", "Touch targets are at least 24×24", "moderate",
         "A control smaller than 24×24 CSS pixels is hard to hit accurately with a "
         "finger, and much harder with a tremor or a stylus. Mis-taps on small "
         "controls are the commonest cause of accidental navigation on mobile.",
         "Give interactive controls a minimum 24×24 box — 44×44 is the comfortable "
         "target — using padding rather than a larger icon. Inline links inside a "
         "sentence are exempt and are not counted here.",
         "WCAG 2.5.8"),
    Rule("bypass", "There is a way to skip past the navigation", "serious",
         "Without a landmark, a heading or a skip link, a keyboard user tabs "
         "through the entire navigation on every single page before reaching the "
         "content.",
         "Add a <code>&lt;main&gt;</code> element around the page content — that "
         "alone satisfies it — and ideally a skip link as the first focusable "
         "element: <code>&lt;a href=\"#main\" class=\"skip\"&gt;Skip to "
         "content&lt;/a&gt;</code>, visible on focus.",
         "WCAG 2.4.1"),
]

BY_KEY = {r.key: r for r in RULES}

# Per page, per rule. Enough to name the fault and photograph it; not so many
# that the report becomes a dump.
MAX_ITEMS_PER_RULE = 8

AUDIT_JS = r"""
() => {
  const out = {};
  const MAX = %d;

  // ---------------------------------------------------------------- helpers
  const cs = el => { try { return getComputedStyle(el); } catch (e) { return null; } };

  const hidden = el => {
    if (!el || el.nodeType !== 1) return true;
    if (el.hasAttribute('hidden')) return true;
    if (el.closest('[aria-hidden="true"]')) return true;
    const s = cs(el);
    if (!s) return true;
    return s.display === 'none' || s.visibility === 'hidden';
  };

  const painted = el => {
    if (hidden(el)) return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };

  const text = el => (el.textContent || '').replace(/\s+/g, ' ').trim();

  const accName = el => {
    const aria = (el.getAttribute('aria-label') || '').trim();
    if (aria) return aria;
    const ref = el.getAttribute('aria-labelledby');
    if (ref) {
      const t = ref.split(/\s+/).map(id => {
        const e = document.getElementById(id);
        return e ? text(e) : '';
      }).join(' ').trim();
      if (t) return t;
    }
    const own = text(el);
    if (own) return own;
    const title = (el.getAttribute('title') || '').trim();
    if (title) return title;
    if (el.tagName === 'INPUT') {
      const v = (el.getAttribute('value') || '').trim();
      if (v) return v;
      const a = (el.getAttribute('alt') || '').trim();
      if (a) return a;
    }
    const img = el.querySelector && el.querySelector('img[alt]');
    if (img && (img.getAttribute('alt') || '').trim()) return img.getAttribute('alt').trim();
    const st = el.querySelector && el.querySelector('svg title, svg desc');
    if (st && text(st)) return text(st);
    return '';
  };

  // A short descendant selector, for pointing a camera at the element later.
  const sel = el => {
    const parts = [];
    let cur = el, depth = 0;
    while (cur && cur.nodeType === 1 && depth < 4) {
      if (cur.id) { parts.unshift('#' + CSS.escape(cur.id)); break; }
      let p = cur.tagName.toLowerCase();
      const cls = [...(cur.classList || [])]
        .filter(c => c && !/\d{2,}/.test(c)).slice(0, 2)
        .map(c => '.' + CSS.escape(c)).join('');
      if (cls) p += cls;
      else {
        const par = cur.parentElement;
        if (par) {
          const same = [...par.children].filter(c => c.tagName === cur.tagName);
          if (same.length > 1) p += ':nth-of-type(' + (same.indexOf(cur) + 1) + ')';
        }
      }
      parts.unshift(p);
      cur = cur.parentElement;
      depth++;
    }
    return parts.join(' ');
  };

  const rule = (key, applicable, failures) => {
    out[key] = {
      applicable: applicable,
      failed: failures.length,
      items: failures.slice(0, MAX),
    };
  };

  const item = (el, detail) => ({
    sel: sel(el), tag: el.tagName.toLowerCase(),
    text: text(el).slice(0, 90), detail: detail || '',
  });

  const safe = (key, fn) => { try { fn(); } catch (e) { out[key] = {error: String(e).slice(0, 120)}; } };

  // ---------------------------------------------------------------- rules
  safe('image-alt', () => {
    const imgs = [...document.querySelectorAll('img')].filter(painted);
    const bad = imgs.filter(i =>
      !i.hasAttribute('alt') &&
      !['presentation', 'none'].includes((i.getAttribute('role') || '').toLowerCase()));
    rule('image-alt', imgs.length, bad.map(i =>
      item(i, (i.currentSrc || i.src || '').split('/').pop().slice(0, 70))));
  });

  safe('input-image-alt', () => {
    const els = [...document.querySelectorAll('input[type=image]')];
    const bad = els.filter(e => !(e.getAttribute('alt') || '').trim());
    rule('input-image-alt', els.length, bad.map(e => item(e, 'no alt')));
  });

  safe('button-name', () => {
    const els = [...document.querySelectorAll(
      'button,[role=button],input[type=submit],input[type=button],input[type=reset]')]
      .filter(painted);
    const bad = els.filter(e => !accName(e));
    rule('button-name', els.length, bad.map(e => item(e, 'no accessible name')));
  });

  safe('link-name', () => {
    const els = [...document.querySelectorAll('a[href]')].filter(painted);
    const bad = els.filter(e => !accName(e));
    rule('link-name', els.length, bad.map(e =>
      item(e, 'href=' + (e.getAttribute('href') || '').slice(0, 60))));
  });

  safe('label', () => {
    const skip = ['hidden', 'submit', 'button', 'image', 'reset'];
    const els = [...document.querySelectorAll('input,select,textarea')]
      .filter(e => !skip.includes((e.type || '').toLowerCase()))
      .filter(painted);
    const named = e => {
      if ((e.getAttribute('aria-label') || '').trim()) return true;
      if (e.getAttribute('aria-labelledby')) return true;
      if ((e.getAttribute('title') || '').trim()) return true;
      if (e.closest('label')) return true;
      if (e.id) {
        try {
          const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]');
          if (l && text(l)) return true;
        } catch (err) {}
      }
      return false;
    };
    const bad = els.filter(e => !named(e) && !(e.getAttribute('placeholder') || '').trim());
    const weak = els.filter(e => !named(e) && (e.getAttribute('placeholder') || '').trim());
    rule('label', els.length, bad.map(e =>
      item(e, (e.name || e.id || e.type || 'field'))));
    rule('label-placeholder-only', els.length, weak.map(e =>
      item(e, 'placeholder: ' + (e.getAttribute('placeholder') || '').slice(0, 50))));
  });

  safe('color-contrast', () => {
    const parse = c => {
      const m = (c || '').match(/rgba?\(([^)]+)\)/);
      if (!m) return null;
      const p = m[1].split(',').map(x => parseFloat(x));
      return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1};
    };
    const lum = c => {
      const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
      return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
    };
    const ratio = (a, b) => {
      const la = lum(a), lb = lum(b);
      return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
    };
    const over = (fg, bg) => ({
      r: fg.r * fg.a + bg.r * (1 - fg.a),
      g: fg.g * fg.a + bg.g * (1 - fg.a),
      b: fg.b * fg.a + bg.b * (1 - fg.a), a: 1,
    });

    // The first ancestor with an opaque background colour. Anything sitting on a
    // background image or gradient is skipped rather than guessed at.
    const backdrop = el => {
      let cur = el;
      while (cur && cur.nodeType === 1) {
        const s = cs(cur);
        if (!s) return null;
        if (s.backgroundImage && s.backgroundImage !== 'none') return null;
        const c = parse(s.backgroundColor);
        if (c && c.a >= 0.95) return c;
        cur = cur.parentElement;
      }
      return {r: 255, g: 255, b: 255, a: 1};
    };

    let applicable = 0;
    const bad = [];
    const seen = new Set();
    const nodes = document.querySelectorAll('body *');
    for (const el of nodes) {
      if (bad.length >= MAX * 3) break;
      if (['SCRIPT', 'STYLE', 'NOSCRIPT', 'SVG', 'PATH'].includes(el.tagName)) continue;
      // Only elements with their own text, so a wrapper is not judged on its
      // children's copy.
      let own = '';
      for (const n of el.childNodes) {
        if (n.nodeType === 3) own += n.nodeValue;
      }
      own = own.replace(/\s+/g, ' ').trim();
      if (own.length < 2) continue;
      if (!painted(el)) continue;
      const s = cs(el);
      if (!s) continue;
      const fg = parse(s.color);
      if (!fg || fg.a < 0.05) continue;
      const bg = backdrop(el);
      if (!bg) continue;
      applicable++;
      const size = parseFloat(s.fontSize) || 16;
      const weight = parseInt(s.fontWeight, 10) || 400;
      const large = size >= 24 || (size >= 18.66 && weight >= 700);
      const need = large ? 3 : 4.5;
      const got = ratio(over(fg, bg), bg);
      if (got + 0.005 < need) {
        const hex = c => '#' + [c.r, c.g, c.b].map(v =>
          Math.round(v).toString(16).padStart(2, '0')).join('');
        const key = hex(fg) + hex(bg) + Math.round(size);
        if (seen.has(key)) continue;
        seen.add(key);
        bad.push(item(el, got.toFixed(2) + ':1 needs ' + need + ':1 — ' +
          hex(fg) + ' on ' + hex(bg) + ' at ' + Math.round(size) + 'px' +
          (weight >= 700 ? ' bold' : '')));
      }
    }
    rule('color-contrast', applicable, bad);
  });

  safe('html-has-lang', () => {
    const lang = (document.documentElement.getAttribute('lang') || '').trim();
    rule('html-has-lang', 1, lang ? [] : [{sel: 'html', tag: 'html', text: '', detail: 'no lang attribute'}]);
    if (lang) {
      const ok = /^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$/i.test(lang);
      rule('html-lang-valid', 1, ok ? [] : [{sel: 'html', tag: 'html', text: '', detail: 'lang="' + lang + '"'}]);
    } else {
      rule('html-lang-valid', 0, []);
    }
  });

  safe('document-title', () => {
    const t = (document.title || '').trim();
    rule('document-title', 1, t ? [] : [{sel: 'title', tag: 'title', text: '', detail: 'empty or missing'}]);
  });

  safe('meta-viewport', () => {
    const m = document.querySelector('meta[name=viewport]');
    if (!m) { rule('meta-viewport', 0, []); return; }
    const c = (m.getAttribute('content') || '').toLowerCase();
    const scale = /maximum-scale\s*=\s*([\d.]+)/.exec(c);
    const bad = /user-scalable\s*=\s*(no|0)/.test(c) || (scale && parseFloat(scale[1]) < 2);
    rule('meta-viewport', 1, bad
      ? [{sel: 'meta[name=viewport]', tag: 'meta', text: '', detail: c.slice(0, 90)}] : []);
  });

  safe('heading-order', () => {
    const hs = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].filter(painted);
    const bad = [];
    let prev = 0;
    for (const h of hs) {
      const lvl = parseInt(h.tagName[1], 10);
      if (prev && lvl > prev + 1) {
        bad.push(item(h, 'h' + prev + ' → h' + lvl));
      }
      prev = lvl;
    }
    rule('heading-order', hs.length, bad);
    const empty = hs.filter(h => !text(h) && !h.querySelector('img[alt]:not([alt=""])'));
    rule('empty-heading', hs.length, empty.map(h => item(h, 'empty ' + h.tagName.toLowerCase())));
  });

  safe('duplicate-id-active', () => {
    const withId = [...document.querySelectorAll('[id]')];
    const counts = {};
    withId.forEach(e => { counts[e.id] = (counts[e.id] || 0) + 1; });
    const interactive = e => e.matches(
      'a[href],button,input,select,textarea,[tabindex],[role=button],[contenteditable]');
    const referenced = id => {
      try {
        return !!document.querySelector('[for="' + CSS.escape(id) + '"],' +
          '[aria-labelledby~="' + CSS.escape(id) + '"],[aria-controls~="' + CSS.escape(id) + '"]');
      } catch (e) { return false; }
    };
    const bad = withId.filter(e => counts[e.id] > 1 && (interactive(e) || referenced(e.id)));
    const dedup = [];
    const seen = new Set();
    for (const e of bad) {
      if (seen.has(e.id)) continue;
      seen.add(e.id);
      dedup.push(item(e, 'id="' + e.id + '" used ' + counts[e.id] + ' times'));
    }
    rule('duplicate-id-active', withId.length, dedup);
  });

  safe('aria-hidden-focus', () => {
    const hosts = [...document.querySelectorAll('[aria-hidden="true"]')];
    const bad = [];
    for (const h of hosts) {
      const f = h.querySelector('a[href],button,input,select,textarea,[tabindex]:not([tabindex="-1"])');
      if (f && !f.hasAttribute('inert') && !h.hasAttribute('inert')) {
        bad.push(item(f, 'focusable inside aria-hidden'));
      }
    }
    rule('aria-hidden-focus', hosts.length, bad);
  });

  safe('list', () => {
    const lists = [...document.querySelectorAll('ul,ol')];
    const bad = lists.filter(l => [...l.children].some(c =>
      !['LI', 'SCRIPT', 'TEMPLATE'].includes(c.tagName)));
    rule('list', lists.length, bad.map(l => item(l,
      'child <' + ([...l.children].find(c => !['LI', 'SCRIPT', 'TEMPLATE'].includes(c.tagName))
        .tagName.toLowerCase()) + '>')));
  });

  safe('frame-title', () => {
    const fr = [...document.querySelectorAll('iframe,frame')];
    const bad = fr.filter(f => !(f.getAttribute('title') || '').trim() &&
                               !(f.getAttribute('aria-label') || '').trim());
    rule('frame-title', fr.length, bad.map(f =>
      item(f, (f.getAttribute('src') || '').slice(0, 70))));
  });

  safe('tabindex', () => {
    const els = [...document.querySelectorAll('[tabindex]')];
    const bad = els.filter(e => parseInt(e.getAttribute('tabindex'), 10) > 0);
    rule('tabindex', els.length, bad.map(e =>
      item(e, 'tabindex=' + e.getAttribute('tabindex'))));
  });

  safe('target-size', () => {
    const els = [...document.querySelectorAll(
      'a[href],button,input,select,textarea,[role=button]')].filter(painted);
    const inlineInText = el => {
      if (el.tagName !== 'A') return false;
      const p = el.parentElement;
      if (!p) return false;
      if (!['P', 'LI', 'SPAN', 'TD', 'DD', 'DT', 'BLOCKQUOTE'].includes(p.tagName)) return false;
      return text(p).length > text(el).length + 12;
    };
    const bad = [];
    for (const e of els) {
      if (inlineInText(e)) continue;
      const r = e.getBoundingClientRect();
      if (r.width < 24 || r.height < 24) {
        bad.push(item(e, Math.round(r.width) + '×' + Math.round(r.height) + ' px'));
      }
    }
    rule('target-size', els.length, bad);
  });

  safe('bypass', () => {
    const ok = !!document.querySelector('main,[role=main],h1') ||
      [...document.querySelectorAll('a[href^="#"]')].slice(0, 3).some(a =>
        /skip|jump|content|main/i.test(text(a) + (a.getAttribute('href') || '')));
    rule('bypass', 1, ok ? [] : [{sel: 'body', tag: 'body', text: '',
      detail: 'no <main>, no h1 and no skip link'}]);
  });

  return out;
}
""" % MAX_ITEMS_PER_RULE


# ---------------------------------------------------------------- aggregation

def page_score(result: dict) -> float | None:
    """The strict, binary, Lighthouse-shaped score for one page.

    Every applicable rule is a pass or a fail; the score is the passing weight
    over the applicable weight. This is the number to compare with a Lighthouse
    run against the same URL.
    """
    if not result:
        return None
    live = passed = 0.0
    for key, res in result.items():
        rule = BY_KEY.get(key)
        if not rule or not isinstance(res, dict) or res.get("error"):
            continue
        if not res.get("applicable"):
            continue
        live += rule.weight
        if not res.get("failed"):
            passed += rule.weight
    return (passed / live) if live else None


def summarise(runtime: list[dict]) -> dict:
    """Site-level accessibility: per rule, the share of rendered pages that pass."""
    pages = [p for p in runtime if p.get("a11y")]
    if not pages:
        return {}

    rules: list[dict] = []
    for rule in RULES:
        applicable = [p for p in pages
                      if isinstance(p["a11y"].get(rule.key), dict)
                      and p["a11y"][rule.key].get("applicable")]
        if not applicable:
            continue
        failing = [p for p in applicable if p["a11y"][rule.key].get("failed")]
        items = []
        for p in failing:
            for it in p["a11y"][rule.key].get("items", []):
                items.append({**it, "url": p["url"]})
        rules.append({
            "key": rule.key, "label": rule.label, "impact": rule.impact,
            "weight": rule.weight, "wcag": rule.wcag,
            "pages": len(applicable), "failing": len(failing),
            "score": (len(applicable) - len(failing)) / len(applicable),
            "elements": sum(p["a11y"][rule.key].get("failed", 0) for p in failing),
            "tested": sum(p["a11y"][rule.key].get("applicable", 0) for p in applicable),
            "items": items[:24],
        })

    live = sum(r["weight"] for r in rules)
    score = (sum(r["weight"] * r["score"] for r in rules) / live) if live else None
    strict = {p["url"]: page_score(p["a11y"]) for p in pages}
    return {
        "rules": rules,
        "score": score,
        "pages": len(pages),
        "strict": strict,
        "home": next(iter(strict.values()), None),
        "not_applicable": [r.key for r in RULES
                           if r.key not in {x["key"] for x in rules}],
    }
