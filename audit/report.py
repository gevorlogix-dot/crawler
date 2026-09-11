"""HTML report renderer.

Laid out as a survey instrument rather than a marketing dashboard: a readout
masthead, a severity composition bar, a sticky index you can filter, and a ruled
ledger of findings — each with why it matters, how to fix it, and every affected
URL.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from html import escape
from urllib.parse import urlparse

from . import score as score_mod
from . import vitals as vitals_mod
from .theme import (SEVERITIES, css as theme_css, sev_chip, state_chip,
                    theme_script, theme_toggle)

PAGE_CSS = r"""
.wrap{max-width:1240px;margin:0 auto;padding:0 22px 90px}

/* ---------------------------------------------------------- masthead */
.mast{background:var(--surface);border:1px solid var(--rule);border-top:3px solid var(--ink);
  margin:26px 0 22px}
.mast .top{padding:26px 28px 22px}
.mast .eyebrow{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.mast .eyebrow .k{color:var(--ink-faint)}
.mast .eyebrow .dash{flex:1;height:1px;background:var(--rule)}
.mast h1{margin:0;font-size:32px;line-height:1.14;letter-spacing:-.9px;font-weight:700}
.mast h1 .host{font-family:var(--mono);letter-spacing:-.4px}
.mast .lede{margin:11px 0 0;color:var(--ink-soft);font-size:14.5px;max-width:72ch}
.readout{display:grid;grid-template-columns:repeat(auto-fit,minmax(178px,1fr));
  border-top:1px solid var(--rule)}
.readout .cell{padding:13px 28px;border-right:1px solid var(--rule-soft)}
.readout .cell:last-child{border-right:0}
.readout .v{font-family:var(--mono);font-size:15px;font-weight:600;margin-top:4px;
  overflow-wrap:anywhere}
.readout .v a{text-decoration:none}

/* ---------------------------------------------------------- scores
   One hero figure (the overall), then a stat tile per category. The meter fill
   carries the band and the written grade travels with it, so the colour is never
   doing the work on its own. */
.scores{display:grid;gap:1px;background:var(--rule);border:1px solid var(--rule);
  grid-template-columns:repeat(auto-fit,minmax(238px,1fr));margin-bottom:22px}
.scores>div{background:var(--surface);padding:17px 21px 19px;min-width:0}
.scores .big{font-family:var(--mono);font-size:54px;font-weight:700;
  letter-spacing:-3px;line-height:1;margin-top:5px}
.scores .big .of{font-size:19px;color:var(--ink-faint);letter-spacing:-.5px}
.scores .num{font-family:var(--mono);font-size:34px;font-weight:700;
  letter-spacing:-1.6px;line-height:1.1;margin-top:5px}
.scores .num .of{font-size:14px;color:var(--ink-faint);letter-spacing:0}
.scores .chip{margin-top:7px}
.scores .basis{font-size:12.5px;color:var(--ink-soft);margin:9px 0 0}
.scores .na{font-family:var(--mono);font-size:30px;color:var(--ink-faint);margin-top:6px}

.meter{margin-top:12px}
.meter .track{position:relative;height:10px;background:var(--surface-2);
  border:1px solid var(--rule-soft)}
.meter .fill{position:absolute;inset:0 auto 0 0;border-radius:0 4px 4px 0;
  background:var(--m-low)}
.meter .fill.good{background:var(--m-good)}
.meter .fill.medium{background:var(--m-medium)}
.meter .fill.high{background:var(--m-high)}
.meter .fill.critical{background:var(--m-critical)}
.meter .ticks{position:relative;height:13px;margin-top:2px}
.meter .ticks i{position:absolute;top:0;width:1px;height:4px;background:var(--rule)}
.meter .ticks b{position:absolute;top:5px;transform:translateX(-50%);
  font:600 9.5px/1 var(--mono);color:var(--ink-faint)}

/* per-group and per-metric readout */
.mgroup{border:1px solid var(--rule);background:var(--surface)}
.mgroup+.mgroup{border-top:0}
.mgroup>summary{cursor:pointer;list-style:none;display:grid;
  grid-template-columns:minmax(0,1fr) 92px 54px;gap:14px;align-items:center;
  padding:12px 16px}
.mgroup>summary::-webkit-details-marker{display:none}
.mgroup>summary:hover{background:var(--surface-2)}
.mgroup>summary .gname{min-width:0;font-size:14px;font-weight:650}
.mgroup>summary .gname .k{display:block;margin-top:2px;letter-spacing:.07em}
.mgroup>summary .gscore{font-family:var(--mono);font-size:19px;font-weight:700;
  text-align:right}
.mgroup>summary .glost{font-family:var(--mono);font-size:12px;
  color:var(--ink-faint);text-align:right;white-space:nowrap}
.mgroup .inner{border-top:1px solid var(--rule-soft);padding:4px 16px 14px}
.mrow{padding:12px 0;border-bottom:1px dashed var(--rule)}
.mrow:last-child{border-bottom:0}
.mtop{display:grid;grid-template-columns:minmax(0,1fr) 116px 46px;gap:8px 14px;
  align-items:baseline}
.mtop .lab{font-size:13.5px;min-width:0}
.mtop .bar{grid-column:2;position:relative;height:8px;background:var(--surface-2);
  border:1px solid var(--rule-soft);align-self:center}
.mtop .bar i{position:absolute;inset:0 auto 0 0;background:var(--accent);
  border-radius:0 3px 3px 0}
.mtop .pc{font-family:var(--mono);font-size:13px;font-weight:700;text-align:right}
.mmeta{display:flex;flex-wrap:wrap;gap:3px 18px;margin-top:5px;
  font-family:var(--mono);font-size:11.5px;color:var(--ink-faint)}
.mmeta .measured{color:var(--ink-soft)}
.mnote{margin:6px 0 0;font-size:12.5px;color:var(--ink-faint);max-width:80ch}
details.fix{margin-top:8px}
details.fix summary{cursor:pointer;list-style:none;display:inline-flex;
  align-items:center;gap:6px;font-family:var(--mono);font-size:10.5px;
  font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--accent)}
details.fix summary::-webkit-details-marker{display:none}
details.fix summary::before{content:"+";display:inline-block;width:13px;height:13px;
  border:1px solid currentColor;text-align:center;line-height:11px;font-size:11px}
details.fix[open] summary::before{content:"\2212"}
details.fix .body{margin:8px 0 0;padding:11px 14px;background:var(--surface-2);
  border-left:2px solid var(--rule);font-size:13.5px;color:var(--ink-soft);
  max-width:84ch}
details.fix .body p{margin:0}
details.fix .body .also{margin-top:7px;font-family:var(--mono);font-size:11px;
  color:var(--ink-faint)}

/* Core Web Vitals: value, curve, score */
.cwv{width:100%}
.cwv td .sub{display:block;font-size:11.5px;color:var(--ink-faint);
  font-family:var(--mono)}
.opp{border:1px solid var(--rule);background:var(--surface)}
.opp+.opp{border-top:0}
.opp .head{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;
  align-items:baseline;padding:13px 16px 0}
.opp .head h4{margin:0;font-size:14px;font-weight:650}
.opp .head .save{font-family:var(--mono);font-size:13px;font-weight:700;
  color:var(--t-medium);white-space:nowrap}
.opp p{margin:6px 0 0;padding:0 16px;font-size:13px;color:var(--ink-soft);
  max-width:84ch}
.opp .why{padding:0 16px 14px}
@media (max-width:680px){
  .mgroup>summary{grid-template-columns:minmax(0,1fr) auto;gap:6px 12px}
  .mgroup>summary .glost{grid-column:2}
  .mtop{grid-template-columns:minmax(0,1fr) 46px}
  .mtop .bar{grid-column:1/-1;order:3}
}

/* ---------------------------------------------------------- summary */
.summary{background:var(--surface);border:1px solid var(--rule);padding:22px 28px;
  margin-bottom:22px}
.summary h2{margin:0 0 14px;font-size:13px;font-weight:700;letter-spacing:.02em}
.legend{display:flex;flex-wrap:wrap;gap:8px 26px;margin-top:13px}
.legend .item{display:flex;align-items:baseline;gap:8px}
.legend .n{font-family:var(--mono);font-size:19px;font-weight:700}
.legend .n.critical{color:var(--t-critical)} .legend .n.high{color:var(--t-high)}
.legend .n.medium{color:var(--t-medium)} .legend .n.low{color:var(--t-low)}
.legend .cap{font-size:12.5px;color:var(--ink-soft)}

.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:1px;
  background:var(--rule);border:1px solid var(--rule);margin-bottom:26px}
.tile{background:var(--surface);padding:16px 18px}
.tile .n{font-family:var(--mono);font-size:27px;font-weight:700;letter-spacing:-1px;
  line-height:1.1}
.tile .n.crit{color:var(--t-critical)} .tile .n.ok{color:var(--t-good)}
.tile .cap{font-size:12.5px;color:var(--ink-soft);margin-top:3px}
.tile .k{display:block;margin-top:6px}

.callout{background:var(--surface);border:1px solid var(--rule);
  border-left:3px solid var(--m-good);padding:15px 19px;margin-bottom:26px;
  font-size:13.5px;color:var(--ink-soft)}
.callout.warn{border-left-color:var(--m-medium)}
.callout strong{color:var(--ink)}
.callout p{margin:0 0 7px}.callout p:last-child{margin:0}

/* ---------------------------------------------------------- layout */
.cols{display:grid;grid-template-columns:1fr;gap:26px;align-items:start}
/* grid children default to min-width:auto, which lets a wide <pre> push the
   whole column past the viewport — pin them to 0 so they can shrink. */
.cols>*{min-width:0}
@media (min-width:1080px){.cols{grid-template-columns:236px minmax(0,1fr)}}
.rail{position:sticky;top:20px;display:none}
@media (min-width:1080px){.rail{display:block}}
.rail .box{background:var(--surface);border:1px solid var(--rule)}
.rail h2{margin:0;padding:13px 15px;font-size:11px;border-bottom:1px solid var(--rule);
  font-family:var(--mono);text-transform:uppercase;letter-spacing:.09em;
  color:var(--ink-faint);font-weight:600}
.filters{display:flex;flex-wrap:wrap;gap:5px;padding:12px 15px;
  border-bottom:1px solid var(--rule-soft)}
.filters button{display:inline-flex;align-items:center;gap:5px;background:var(--surface-2);
  border:1px solid var(--rule);color:var(--ink-soft);font:600 10.5px/1 var(--mono);
  text-transform:uppercase;letter-spacing:.07em;padding:5px 7px;cursor:pointer}
.filters button .sw{width:8px;height:8px;border:1px solid rgba(0,0,0,.16)}
.filters button[aria-pressed="true"]{background:var(--ink);color:var(--paper);
  border-color:var(--ink)}
.filters button.critical .sw{background:var(--m-critical)}
.filters button.high .sw{background:var(--m-high)}
.filters button.medium .sw{background:var(--m-medium)}
.filters button.low .sw{background:var(--m-low)}
.jump{display:grid;padding:8px 0;border-bottom:1px solid var(--rule-soft)}
.jump a{padding:5px 15px;font-size:12.5px;color:var(--ink-soft);text-decoration:none}
.jump a:hover{background:var(--surface-2);color:var(--accent)}
.rail ol{list-style:none;margin:0;padding:8px 0;max-height:48vh;overflow:auto}
.rail li a{display:grid;grid-template-columns:9px 1fr auto;gap:8px;align-items:center;
  padding:5px 15px;text-decoration:none;color:var(--ink-soft);font-size:12.5px;line-height:1.35}
.rail li a:hover{background:var(--surface-2);color:var(--ink)}
.rail li a .sw{width:9px;height:9px;border:1px solid rgba(0,0,0,.16)}
.rail li a .id{font-family:var(--mono);font-size:11px;font-weight:600}
.rail li a .c{font-family:var(--mono);font-size:11px;color:var(--ink-faint)}
.rail li.critical .sw{background:var(--m-critical)}
.rail li.high .sw{background:var(--m-high)}
.rail li.medium .sw{background:var(--m-medium)}
.rail li.low .sw{background:var(--m-low)}

/* ---------------------------------------------------------- findings */
.sec{margin:0 0 30px;scroll-margin-top:20px}
.sechead{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 12px;
  border-bottom:2px solid var(--ink);padding-bottom:9px;margin-bottom:0}
.sechead h2{margin:0;font-size:17px;font-weight:700;letter-spacing:-.25px}
.sechead .count{font-family:var(--mono);font-size:12px;color:var(--ink-faint);
  margin-left:auto;flex:none}
.sechead p{margin:5px 0 0;color:var(--ink-soft);font-size:13.5px;max-width:76ch;
  flex-basis:100%}

.f{background:var(--surface);border:1px solid var(--rule);border-top:0;
  scroll-margin-top:20px}
.f.hidden{display:none}
.fhead{display:grid;grid-template-columns:104px 66px 1fr auto;gap:14px;align-items:baseline;
  padding:14px 18px;border-bottom:1px solid var(--rule-soft)}
.fhead .fid{font-family:var(--mono);font-size:11.5px;font-weight:600;color:var(--ink-faint)}
.fhead h3{margin:0;font-size:15.5px;font-weight:650;letter-spacing:-.15px}
.fhead .pages{font-family:var(--mono);font-size:12px;color:var(--ink-faint);
  text-align:right;white-space:nowrap}
.fhead .pages b{display:block;font-size:16px;color:var(--ink-soft);font-weight:700}
.fbody{padding:16px 18px 18px}
.what{margin:0 0 14px;max-width:84ch}
.qa{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:9px 16px;
  align-items:baseline;border-top:1px solid var(--rule-soft);padding-top:13px}
.qa dt{padding-top:2px}
.qa dd{margin:0;color:var(--ink-soft);font-size:13.5px;max-width:82ch;
  overflow-wrap:anywhere}
.repsrc td:first-child{font-weight:520;white-space:nowrap}
.repsrc .verdict{white-space:nowrap}
.repprov td{border-top:0;padding-top:0;font-size:12.5px;color:var(--ink-faint);
  overflow-wrap:anywhere}
.repprov code{font-size:.92em;overflow-wrap:anywhere}
.repprov .lbl{color:var(--ink-soft);letter-spacing:.02em}
.repunread{margin:4px 0 22px;padding-left:18px;font-size:13px;color:var(--ink-soft)}
.repunread li{margin:6px 0;overflow-wrap:anywhere}
.ev{margin:14px 0 0}
.ev pre{margin:6px 0 0;padding:12px 14px;background:var(--surface-2);
  border:1px solid var(--rule-soft);border-left:2px solid var(--rule);overflow-x:auto;
  font-family:var(--mono);font-size:12.5px;line-height:1.6;color:var(--ink-soft);
  white-space:pre}
details.urls{margin:14px 0 0;border-top:1px solid var(--rule-soft);padding-top:12px}
details.urls summary{cursor:pointer;list-style:none;display:inline-flex;align-items:center;
  gap:7px;font-family:var(--mono);font-size:11px;font-weight:600;color:var(--accent);
  text-transform:uppercase;letter-spacing:.08em}
details.urls summary::-webkit-details-marker{display:none}
details.urls summary::before{content:"+";display:inline-block;width:13px;height:13px;
  border:1px solid currentColor;text-align:center;line-height:11px;font-size:11px}
details.urls[open] summary::before{content:"\2212"}
.urllist{margin:11px 0 0;padding:0;list-style:none;max-height:320px;overflow:auto;
  border:1px solid var(--rule-soft);background:var(--surface-2)}
.urllist li{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:14px;
  font-family:var(--mono);font-size:12px;padding:6px 11px;
  border-bottom:1px solid var(--rule-soft)}
.urllist li:last-child{border-bottom:0}
.urllist li:nth-child(even){background:var(--surface-3)}
.urllist a{color:var(--ink-soft);text-decoration:none;word-break:break-all}
.urllist a:hover,.urllist a:focus{color:var(--accent);text-decoration:underline}
.urllist .d{color:var(--ink-faint);text-align:right;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;max-width:270px}
/* A quoted fragment is prose: it wraps rather than being cut off at 270px.
   Named `quote`, not `wrap`: `.wrap` is this page's own layout container and
   carries a 90px bottom padding, which stretched every row that borrowed it. */
.urllist .d.quote{white-space:normal;overflow:visible;max-width:none;text-align:left}
/* Further hits on the page above, indented under it. */
.urllist li.more{padding-left:22px}
.urllist li.more .d{border-left:2px solid var(--rule);padding-left:10px;
  color:var(--ink-soft)}

/* ------------------------------------------------- visual evidence */
/* Screenshots of the finding, embedded as data URIs. Each frame is a label for
   a hidden checkbox, so clicking it lifts the height cap — zoom with no script. */
.shots{margin:16px 0 0;border-top:1px solid var(--rule-soft);padding-top:14px}
.shots .grid{display:grid;gap:14px;margin-top:11px}
@media (min-width:820px){.shots .grid.two{grid-template-columns:1fr 1fr}}
.shot{margin:0;min-width:0;background:var(--surface-2);border:1px solid var(--rule)}
.shot .zoom{position:absolute;opacity:0;pointer-events:none}
.shot .frame{display:block;max-height:340px;overflow:hidden;cursor:zoom-in;
  background:var(--surface-3);border-bottom:1px solid var(--rule-soft)}
.shot .frame img{display:block;width:100%;height:auto}
/* Zoomed means 1:1 pixels, not merely uncapped height — a capture scaled into a
   430px column is unreadable at the sizes that matter. The frame becomes its own
   scroll container so the page itself never scrolls sideways. */
.shot .zoom:checked+.frame{max-height:none;overflow:auto;cursor:zoom-out}
.shot .zoom:checked+.frame img{width:auto;max-width:none}
.shot .zoom:focus-visible+.frame{outline:2px solid var(--accent);outline-offset:-2px}
.shot figcaption{padding:9px 12px;font-size:12.5px;color:var(--ink-soft);
  line-height:1.5}
.shot figcaption .where{display:block;font-family:var(--mono);font-size:11px;
  color:var(--ink-faint);margin-top:4px;overflow-wrap:anywhere}
.shot figcaption .hint{color:var(--ink-faint)}

/* ------------------------------------------------- image evidence */
.imgcards{display:grid;gap:1px;background:var(--rule);border:1px solid var(--rule);
  grid-template-columns:repeat(auto-fill,minmax(206px,1fr));margin:12px 0 0}
.imgcard{background:var(--surface);padding:0;min-width:0;display:flex;
  flex-direction:column}
.imgcard .pic{aspect-ratio:16/10;background:var(--surface-3);display:grid;
  place-items:center;overflow:hidden;border-bottom:1px solid var(--rule-soft)}
.imgcard .pic img{max-width:100%;max-height:100%;display:block}
.imgcard .pic .none{font:600 10.5px var(--mono);color:var(--ink-faint);
  text-transform:uppercase;letter-spacing:.08em}
.imgcard .meta{padding:11px 13px 13px;display:grid;gap:3px}
.imgcard .size{font-family:var(--mono);font-size:19px;font-weight:700;
  letter-spacing:-.5px;line-height:1.15;color:var(--t-medium)}
.imgcard .size.hot{color:var(--t-critical)}
.imgcard .dims{font-family:var(--mono);font-size:11.5px;color:var(--ink-soft)}
/* The retina rendition sits under the headline weight, deliberately quieter:
   it is a second measurement of the same image, not a competing total. */
.imgcard .alt{font-family:var(--mono);font-size:11.5px;color:var(--ink-faint)}
.imgcard .name{font-family:var(--mono);font-size:11px;color:var(--ink-faint);
  overflow-wrap:anywhere}
.imgcard .name a{color:inherit;text-decoration:none}
.imgcard .name a:hover{color:var(--accent);text-decoration:underline}
.imgcard .tags{display:flex;flex-wrap:wrap;gap:5px;margin-top:5px}
.tag{font:600 10px/1.5 var(--mono);text-transform:uppercase;letter-spacing:.07em;
  color:var(--ink-faint);border:1px solid var(--rule);background:var(--surface-2);
  padding:1px 5px}
.tablenote{margin:10px 0 0;font-size:12px;color:var(--ink-faint)}

/* ------------------------------------------------- structured data */
/* A grid child that cannot shrink sets the column, and the column then sets
   every sibling: one unbreakable URL in an issue message widened the whole
   section to 459px inside a 360px viewport, taking the stat tiles and the
   proportion bar with it. Same rule, same reason as .cols>*. */
.sd{display:grid;gap:22px}
.sd>*{min-width:0}
.sdgrid{display:grid;gap:1px;background:var(--rule);border:1px solid var(--rule);
  grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.sdgrid .cell{background:var(--surface);padding:13px 16px}
.sdgrid .v{font-family:var(--mono);font-size:22px;font-weight:700;
  letter-spacing:-.8px;margin-top:3px}
.sdgrid .v.err{color:var(--t-critical)} .sdgrid .v.warn{color:var(--t-medium)}
.sdgrid .v.ok{color:var(--t-good)}
.sdpage{border:1px solid var(--rule);background:var(--surface)}
.sdpage+.sdpage{border-top:0}
.sdpage>summary{cursor:pointer;list-style:none;display:grid;
  grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:center;
  padding:11px 15px;font-family:var(--mono);font-size:12px}
.sdpage>summary::-webkit-details-marker{display:none}
.sdpage>summary:hover{background:var(--surface-2)}
.sdpage>summary .p{overflow-wrap:anywhere;color:var(--ink)}
.sdpage>summary .tally{display:flex;gap:10px;white-space:nowrap}
.sdpage .inner{border-top:1px solid var(--rule-soft);padding:13px 15px}
.sditem{margin:0 0 13px}
.sditem:last-child{margin-bottom:0}
.sditem .head{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:baseline;
  padding-bottom:6px;border-bottom:1px dashed var(--rule)}
.sditem .type{font-family:var(--mono);font-size:12.5px;font-weight:700}
.sditem .path{font-family:var(--mono);font-size:11px;color:var(--ink-faint);
  margin-left:auto;overflow-wrap:anywhere}
.sdissues{list-style:none;margin:8px 0 0;padding:0;display:grid;gap:7px}
.sdissues li{display:grid;grid-template-columns:74px minmax(0,1fr);gap:11px;
  font-size:13px;color:var(--ink-soft);align-items:baseline}
/* Messages quote @ids, URLs and context blobs inside <code>; without this a
   single long token is unbreakable and pushes the column past the viewport. */
.sdissues li .msg code{font-size:.85em;overflow-wrap:anywhere}
.sdissues li .msg{min-width:0}
.sdvalue{display:block;font-family:var(--mono);font-size:11.5px;
  color:var(--ink-faint);margin-top:2px;overflow-wrap:anywhere}
/* Which authority an issue speaks for. Text, never colour: it borrows the
   neutral .tag tokens rather than adding a hue, because the hues are reserved
   for severity and a second meaning on the same swatch is unreadable. */
.sdlayer{font:600 9.5px/1.5 var(--mono);text-transform:uppercase;
  letter-spacing:.07em;color:var(--ink-faint);border:1px solid var(--rule);
  background:var(--surface-2);padding:0 4px;margin-right:6px;
  white-space:nowrap;vertical-align:1px}
.sdcount{font:600 9.5px/1.5 var(--mono);color:var(--ink-faint);margin-left:6px}
@media (max-width:680px){
  .sdissues li{grid-template-columns:1fr;gap:3px}
  .sdpage>summary{grid-template-columns:1fr}
}

.foot{margin-top:42px;padding-top:16px;border-top:1px solid var(--rule);
  color:var(--ink-faint);font-size:12.5px;max-width:88ch}
.nores{background:var(--surface);border:1px solid var(--rule);border-top:0;
  padding:22px 18px;color:var(--ink-soft);font-size:13.5px}
@media (max-width:680px){
  .wrap{padding:0 14px 70px}
  .mast h1{font-size:23px;overflow-wrap:anywhere}
  /* eyebrow: label, timestamp and theme control, in that order of importance */
  .mast .eyebrow{flex-wrap:wrap;gap:8px}
  .mast .eyebrow .dash{display:none}
  .mast .eyebrow .themeset{margin-left:auto}
  .mast .top,.readout .cell,.summary,.tile{padding-left:16px;padding-right:16px}
  .readout{margin:0}
  .fhead{grid-template-columns:1fr auto;gap:7px 12px;padding:13px 16px}
  .fhead .fid{grid-column:1}.fhead h3{grid-column:1/-1}
  .fbody{padding:14px 16px 16px}
  .qa{grid-template-columns:1fr;gap:3px}
  .qa dd{margin-bottom:8px}
  /* the detail column earns its space only when there is space */
  .urllist li{grid-template-columns:minmax(0,1fr)}
  .urllist .d{max-width:none;text-align:left;white-space:normal}
}
"""

CATEGORY_BLURB = {
    "Security and exposure": "Files and endpoints this server hands to anyone who asks.",
    "Email authentication": "SPF, DKIM and DMARC as the domain publishes them "
                            "\u2014 records it controls, so every fix here is one "
                            "DNS change.",
    "Reputation and blocklists": "What third-party blocklists already say about this "
                                 "host, ranked by what a listing does to a visitor "
                                 "rather than by how many vendors voted.",
    "Content and copy": "What a visitor actually reads, checked in the rendered page.",
    "Orphans and internal linking": "From the real internal link graph — inbound link "
                                    "counts and shortest path from the homepage.",
    "Errors and availability": "Failures seen while fetching, and while rendering in a "
                               "real browser.",
    "Indexing": "Whether search engines can, and should, index this site.",
    "Metadata": "Titles, descriptions and headings.",
    "Social sharing": "How pages present when shared to social platforms and "
                      "messaging apps.",
    "Structured data": "Every structured-data item found — JSON-LD, Microdata or "
                       "RDFa — validated against the schema.org vocabulary, and the "
                       "page's subjects against the required-property rules that "
                       "decide rich-result eligibility.",
    "Media and performance": "Images, page weight and response time — image weight "
                             "measured per file, with the offenders pictured.",
}
CATEGORY_ORDER = list(CATEGORY_BLURB)


def _short(url: str, base: str) -> str:
    s = url.replace(base, "")
    return s if s.startswith("/") else ("/" + s if s else "/")


def _plain(html: str) -> str:
    """Markup out, for places that need text — an `alt`, a `title`."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html or "")).strip()


def _url_list(hits, base, cap=400, per_page=6) -> str:
    """Affected pages, and **every** hit on each of them.

    This used to keep one detail per URL, which silently threw away the rest: a
    long-form page with five "space before a punctuation mark" slips showed one
    row, so the reader knew the page and could not find the sentence. Each extra
    hit now gets its own line under its page.
    """
    seen: dict[str, list] = {}
    rows = []
    for h in hits:
        details = seen.setdefault(h.url, [])
        if h.detail and h.detail not in details:
            details.append(h.detail)
    for url, details in list(seen.items())[:cap]:
        link = (f'<a href="{escape(url)}" target="_blank" rel="noopener">'
                f'{escape(_short(url, base))}</a>')
        # A short detail ("266 KB · JPEG") sits on the same line, right-aligned.
        # A quoted sentence cannot: sharing the row squeezed the URL column to
        # nothing and broke the URL down the page one character at a time. Long
        # details go underneath instead, indented, in the full width of the row.
        inline = details[0] if details and len(details[0]) <= 58 else ""
        below = details if not inline else details[1:]
        rows.append(f'<li>{link}'
                    + (f'<span class="d">{escape(inline)}</span>' if inline
                       else "<span></span>") + '</li>')
        for extra in below[:per_page]:
            rows.append(f'<li class="more"><span class="d quote">{escape(extra)}</span>'
                        f'<span></span></li>')
        if len(below) > per_page:
            rows.append(f'<li class="more"><span class="d quote">+ '
                        f'{len(below) - per_page} more on this page</span>'
                        f'<span></span></li>')
    if len(seen) > cap:
        rows.append(f'<li><span class="d">+ {len(seen) - cap} more</span><span></span></li>')
    if not rows:
        return ""
    return ('<details class="urls"><summary>Show the '
            f'{len(seen)} affected page{"s" if len(seen) != 1 else ""}</summary>'
            f'<ul class="urllist">{"".join(rows)}</ul></details>')


def _kb(n: int) -> str:
    return f"{n / 1024:,.0f} KB" if n < 1024 * 1024 else f"{n / 1048576:.1f} MB"


def _shots(shots: list, base: str, fid: str) -> str:
    """Screenshots of the finding, embedded. Each frame zooms on click."""
    if not shots:
        return ""
    figs = []
    for n, s in enumerate(shots):
        zid = f"z-{fid}-{n}"
        figs.append(
            f'<figure class="shot">'
            f'<input class="zoom" type="checkbox" id="{escape(zid)}">'
            f'<label class="frame" for="{escape(zid)}">'
            f'<img src="data:{s.mime};base64,{s.b64}" loading="lazy" '
            f'alt="Screenshot: {escape(_plain(s.caption))}"'
            + (f' width="{s.w}" height="{s.h}"' if s.w and s.h else "") + '>'
            f'</label>'
            f'<figcaption>{s.caption} '
            f'<span class="hint">Click for full size.</span>'
            f'<span class="where">{escape(_short(s.url, base))}</span>'
            f'</figcaption></figure>')
    two = " two" if len(figs) > 1 else ""
    return (f'<div class="shots"><span class="k">Seen on the page</span>'
            f'<div class="grid{two}">{"".join(figs)}</div></div>')


def _image_cards(table: dict, thumbs: dict, base: str) -> str:
    """The offending images themselves, largest first, with their weight."""
    rows = table.get("rows") or []
    if not rows:
        return ""
    cards = []
    for r in rows:
        thumb = thumbs.get(r["url"])
        pic = (f'<img src="data:{thumb["mime"]};base64,{thumb["b64"]}" loading="lazy" '
               f'alt="">' if thumb else '<span class="none">no preview</span>')
        # An SVG's intrinsic size says nothing about how it should be drawn, so
        # its "natural" dimensions are omitted rather than presented as a fault.
        vector = r["fmt"] == "SVG"
        natural = (f'{r["nw"]}&times;{r["nh"]}' if r.get("nw") and not vector else "")
        shown = (f' shown at {r["dw"]}&times;{r["dh"]}'
                 if r.get("dw") and not vector else "")
        if vector and r.get("dw"):
            natural, shown = f'drawn at {r["dw"]}&times;{r["dh"]}', ""
        tags = [r["fmt"]]
        if not vector and r.get("dw") and r.get("nw") and r["nw"] >= r["dw"] * 2:
            tags.append(f'{r["nw"] / r["dw"]:.1f}&times; too wide')
        if not r.get("srcset") and not vector:
            tags.append("no srcset")
        elif r.get("candidates"):
            tags.append(f'{r["candidates"]} srcset candidates')
        if r.get("slot_px"):
            tags.append(f'{r["slot_px"]}px slot')
        if r.get("pages", 0) > 1:
            tags.append(f'{r["pages"]} pages')
        size = _kb(r["bytes"]) if r.get("bytes") else "not measured"
        # The retina rendition is a second number, never folded into the first.
        # Printing one figure for both is what let a 133 KB image be reported as
        # 211 KB — the weight of a candidate no 1x device selects.
        retina = (f'<div class="alt">{_kb(r["retina_bytes"])} at 2&times; DPR</div>'
                  if r.get("retina_bytes") else "")
        hot = " hot" if r.get("bytes", 0) >= 512 * 1024 else ""
        name = r["url"].rsplit("/", 1)[-1][:44] or r["url"]
        cards.append(
            f'<div class="imgcard"><div class="pic">{pic}</div>'
            f'<div class="meta"><div class="size{hot}">{size}</div>'
            + retina
            + (f'<div class="dims">{natural}{shown}</div>' if natural else "")
            + f'<div class="name"><a href="{escape(r["url"])}" target="_blank" '
              f'rel="noopener">{escape(name)}</a></div>'
              f'<div class="tags">'
            + "".join(f'<span class="tag">{t}</span>' for t in tags)
            + '</div></div></div>')
    note = (f'<p class="tablenote">{escape(table["note"])}</p>'
            if table.get("note") else "")
    return f'<div class="imgcards">{"".join(cards)}</div>{note}'


def _finding(f, base, shots_by_id=None, thumbs=None) -> str:
    ev = (f'<div class="ev"><span class="k">Evidence</span>'
          f'<pre>{escape(f.evidence)}</pre></div>') if f.evidence else ""
    pages = (f'<div class="pages"><b>{f.count}</b>'
             f'{"pages" if f.count != 1 else "page"}</div>') if f.count else ""
    cards = (_image_cards(f.table, thumbs or {}, base)
             if f.table.get("kind") == "images" else "")
    shots = _shots((shots_by_id or {}).get(f.id, []), base, f.id)
    return (
        f'<article class="f" id="{escape(f.id)}" data-sev="{f.severity}">'
        f'<div class="fhead">{sev_chip(f.severity)}'
        f'<span class="fid">{escape(f.id)}</span>'
        f'<h3>{escape(f.title)}</h3>{pages}</div>'
        f'<div class="fbody"><p class="what">{f.what}</p>'
        f'<dl class="qa">'
        f'<dt class="k">Why it matters</dt><dd>{f.why}</dd>'
        f'<dt class="k">How to fix it</dt><dd>{f.fix}</dd>'
        f'</dl>{cards}{ev}{shots}{_url_list(f.hits, base)}</div></article>')


def _propbar(counts: dict) -> str:
    total = sum(counts.get(s, 0) for s in SEVERITIES) or 1
    segs = "".join(
        f'<span class="{s}" style="width:{100 * counts.get(s, 0) / total:.4f}%" '
        f'title="{counts.get(s, 0)} {s}"></span>'
        for s in SEVERITIES if counts.get(s, 0))
    items = "".join(
        f'<span class="item"><span class="n {s}">{counts.get(s, 0)}</span>'
        f'<span class="cap">{s}</span></span>'
        for s in SEVERITIES)
    return (f'<div class="propbar" role="img" aria-label="'
            + ", ".join(f"{counts.get(s, 0)} {s}" for s in SEVERITIES)
            + f'">{segs}</div><div class="legend">{items}</div>')


def _depth_chart(hist: dict) -> str:
    if not hist:
        return ""

    def sort_key(k):
        return (99, 0) if k == "unreachable" else (0, int(k))

    def label(k):
        if k == "unreachable":
            return "Not reachable from the homepage"
        if k == "0":
            return "The homepage itself"
        return f"{k} click{'' if k == '1' else 's'} from the homepage"

    items = sorted(hist.items(), key=lambda kv: sort_key(kv[0]))
    top = max(v for _, v in items) or 1
    rows = "".join(
        f'<div class="hbar"><div class="row"><span class="lab">{escape(label(k))}</span>'
        f'<span class="track"><span class="fill{" muted" if k == "unreachable" else ""}" '
        f'style="width:{max(2, 100 * v / top):.3f}%"></span></span></div>'
        f'<span class="val">{v}</span></div>'
        for k, v in items)
    return f'<div class="hbars">{rows}</div>'


def _schema_tile(errors: int, items: int) -> str:
    if not items:
        return ('<div class="tile"><div class="n">&mdash;</div>'
                '<span class="k">Structured data</span>'
                '<div class="cap">None found to validate</div></div>')
    return (f'<div class="tile"><div class="n {"crit" if errors else "ok"}">{errors}'
            '</div><span class="k">Structured-data errors</span>'
            f'<div class="cap">in {items} validated item'
            f'{"s" if items != 1 else ""}</div></div>')


def _image_tile(over: list, all_sizes: list, limit_kb: int, measured: bool) -> str:
    if not measured:
        return ('<div class="tile"><div class="n">&mdash;</div>'
                '<span class="k">Heavy images</span>'
                '<div class="cap">Image weight not measured</div></div>')
    cap = (f"largest {_kb(all_sizes[0])}" if all_sizes and over
           else f"nothing over {limit_kb} KB")
    return (f'<div class="tile"><div class="n {"crit" if over else "ok"}">{len(over)}'
            f'</div><span class="k">Images over {limit_kb} KB</span>'
            f'<div class="cap">{cap}</div></div>')


LEVEL_STATE = {"error": "error", "warning": "warning", "info": "notice"}
# How many pages get their full item-by-item readout. The findings above already
# carry every affected URL; this is the worked example, not the index.
SD_PAGES = 12


def _schema_section(result, ctx) -> str:
    """The validator's own view: what was found, and what is wrong with it.

    Deliberately not a rewrite of the findings above — those group one fault
    across the site. This groups the other way, per page and per item, which is
    what you need open beside the template you are editing.
    """
    pages = [r for r in ctx.pages if r.get("schema_items") or r.get("schema_issues")]
    if not pages and not ctx.pages:
        return ""

    items = sum(len(r.get("schema_items") or []) for r in ctx.pages)
    by_source = Counter(it.get("source", "json-ld")
                        for r in ctx.pages for it in (r.get("schema_items") or []))
    all_issues = [i for r in ctx.pages for i in (r.get("schema_issues") or [])]
    levels = Counter(i["level"] for i in all_issues)
    # The two layers, counted apart. A schema.org error is invalid markup; a
    # Google one is a rich result the page cannot earn. Pooling them is what let
    # a service catalogue's offers appear in a list headed by JSON syntax errors.
    sch_err = sum(1 for i in all_issues
                  if i["level"] == "error" and i.get("layer", "schema") == "schema")
    sch_warn = sum(1 for i in all_issues
                   if i["level"] == "warning" and i.get("layer", "schema") == "schema")
    goog_req = sum(1 for i in all_issues if i["code"] == "missing-required")
    goog_rec = sum(1 for i in all_issues if i["code"] == "missing-recommended")
    types = Counter(t for r in ctx.pages for t in (r.get("schema_types") or []))
    without = sum(1 for r in ctx.pages if not r.get("schema_items"))
    blocks = sum(r.get("schema_blocks") or 0 for r in ctx.pages)

    if not items:
        body = ('<div class="f" style="border-top:1px solid var(--rule)">'
                '<div class="fbody">No JSON-LD, Microdata or RDFa was found on any '
                'page, so there was nothing to validate.</div></div>')
        return _sd_wrap(body, 0)

    total = sum(levels.values()) or 1
    segs = "".join(
        f'<span class="{LEVEL_STATE[lv]}" '
        f'style="width:{100 * levels.get(lv, 0) / total:.4f}%" '
        f'title="{levels.get(lv, 0)} {lv}"></span>'
        for lv in ("error", "warning", "info") if levels.get(lv))
    legend = "".join(
        f'<span class="item">{state_chip(LEVEL_STATE[lv])}'
        f'<span class="cap">{levels.get(lv, 0)}</span></span>'
        for lv in ("error", "warning", "info"))

    tiles = (
        '<div class="sdgrid">'
        f'<div class="cell"><span class="k">Items validated</span>'
        f'<div class="v">{items}</div></div>'
        f'<div class="cell"><span class="k">schema.org errors</span>'
        f'<div class="v {"err" if sch_err else "ok"}">{sch_err}</div></div>'
        f'<div class="cell"><span class="k">schema.org warnings</span>'
        f'<div class="v {"warn" if sch_warn else "ok"}">{sch_warn}</div></div>'
        f'<div class="cell"><span class="k">Rich-result blockers</span>'
        f'<div class="v {"warn" if goog_req else "ok"}">{goog_req}</div></div>'
        f'<div class="cell"><span class="k">Distinct types</span>'
        f'<div class="v">{len(types)}</div></div>'
        f'<div class="cell"><span class="k">Pages with none</span>'
        f'<div class="v">{without}</div></div>'
        '</div>')

    bars = _depth_chart_generic(
        [(t, n) for t, n in types.most_common(10)]) if types else ""

    ranked = sorted(
        pages,
        key=lambda r: (-sum(1 for i in r.get("schema_issues") or []
                            if i["level"] == "error"),
                       -len(r.get("schema_issues") or [])))
    ledger = "".join(_sd_page(r, ctx) for r in ranked[:SD_PAGES]
                     if r.get("schema_issues"))
    more = len([r for r in ranked if r.get("schema_issues")]) - SD_PAGES
    tail = (f'<p class="tablenote">{more} more pages carry the same classes of '
            'issue; every affected URL is listed under the findings above.</p>'
            if more > 0 else "")

    body = (
        '<div class="f" style="border-top:1px solid var(--rule)"><div class="fbody">'
        f'<div class="sd">'
        f'<div>{tiles}</div>'
        f'<div><span class="k">Issues by level</span>'
        f'<div class="propbar" role="img" aria-label="'
        + ", ".join(f"{levels.get(lv, 0)} {lv}" for lv in ("error", "warning", "info"))
        + f'" style="margin-top:9px">{segs}</div>'
          f'<div class="legend">{legend}</div>'
          f'<p class="tablenote">Of these, {sch_err + sch_warn} are schema.org '
          f'validity \u2014 the vocabulary, the ranges, the syntax \u2014 and '
          f'{goog_req + goog_rec} are Google rich-result eligibility '
          f'({goog_req} required, {goog_rec} recommended), which schema.org has no '
          'opinion about.</p></div>'
        + (f'<div><span class="k">Types found, by item count</span>'
           f'<div style="margin-top:11px">{bars}</div></div>' if bars else "")
        + (f'<div><span class="k">Item-by-item readout</span>'
           f'<div style="margin-top:11px">{ledger}</div>{tail}</div>'
           if ledger else
           '<div><p class="tablenote">Every item on every page validated with no '
           'errors or warnings.</p></div>')
        + '</div></div></div>')
    return _sd_wrap(body, items, blocks, by_source)


SD_FORMATS = (("json-ld", "JSON-LD"), ("microdata", "Microdata"), ("rdfa", "RDFa"))


def _sd_inventory(items: int, blocks: int, by_source: dict) -> str:
    """What was actually found, per format.

    "Every JSON-LD, Microdata and RDFa item" reads as though three formats were
    present. On a site with 27 JSON-LD blocks and no `itemscope` or `typeof`
    anywhere, two thirds of that sentence describes the tool rather than the site.
    """
    found = [(label, by_source.get(key, 0)) for key, label in SD_FORMATS
             if by_source.get(key)]
    absent = [label for key, label in SD_FORMATS if not by_source.get(key)]
    parts = []
    if blocks:
        parts.append(f"{blocks} JSON-LD block{'s' if blocks != 1 else ''} parsed")
    if found:
        parts.append(", ".join(
            f"{n} {label} item{'s' if n != 1 else ''}" for label, n in found))
    elif not blocks:
        parts.append("no structured data found")
    if absent and found:
        parts.append("no " + " or ".join(absent) + " anywhere on the site")
    return ". ".join(x[0].upper() + x[1:] for x in parts if x) + "."


def _sd_wrap(body: str, items: int, blocks: int = 0, by_source: dict | None = None) -> str:
    from .schema_validate import vocab_stamp
    inventory = _sd_inventory(items, blocks, by_source or {})
    return (
        '<section class="sec" id="structured-data">'
        '<div class="sechead"><h2>Structured data</h2>'
        f'<span class="count">{items}</span>'
        f'<p>{escape(inventory)} Each item is resolved against the schema.org '
        f'vocabulary ({escape(vocab_stamp())}), in '
        '<strong>two layers, kept apart</strong>. '
        '<em>schema.org validity</em>: types and properties that exist, '
        'properties in their item&rsquo;s domain, values inside the <em>union</em> '
        'of each property\'s declared range \u2014 what validator.schema.org '
        'checks. <em>Google rich-result eligibility</em>: the fields a specific '
        'search feature needs, which schema.org marks required on no type, so '
        'nothing there is a validity error and every row names the authority it '
        'speaks for. A floor applies only inside the feature it belongs to; a '
        'page subject is held to its own type&rsquo;s floor, a node filling a '
        'property slot to what that slot expects, so a <code>provider</code> '
        'reference is not read as a business listing missing its address. Objects '
        'sharing an <code>@id</code> are the one node they are, and answer once '
        'between them.</p></div>'
        f'{body}</section>')


SD_LAYER_LABEL = {"schema": "schema.org", "google": "google"}


def _sd_layer(issue: dict) -> str:
    """Name the authority beside the message.

    A red chip on "Offer is missing price" and a red chip on "startDate is not a
    date" mean very different things — one is invalid markup, the other is a
    feature the page will not qualify for — and severity alone cannot say which.
    """
    layer = issue.get("layer") or "schema"
    label = SD_LAYER_LABEL.get(layer, layer)
    if layer == "google" and issue.get("feature"):
        label = f"google · {issue['feature']}"
    return f'<span class="sdlayer">{escape(label)}</span>'


def _sd_page(rec: dict, ctx) -> str:
    """One page's items and their issues, grouped the way a validator shows them."""
    issues = rec.get("schema_issues") or []
    by_path: dict[str, list] = {}
    for i in issues:
        by_path.setdefault(i["path"] or "—", []).append(i)
    item_type = {it["path"]: it for it in (rec.get("schema_items") or [])}

    errs = sum(1 for i in issues if i["level"] == "error")
    warns = sum(1 for i in issues if i["level"] == "warning")
    tally = []
    if errs:
        tally.append(f'{state_chip("error")}<span class="cap">{errs}</span>')
    if warns:
        tally.append(f'{state_chip("warning")}<span class="cap">{warns}</span>')
    notices = len(issues) - errs - warns
    if notices:
        tally.append(f'{state_chip("notice")}<span class="cap">{notices}</span>')

    blocks = []
    for path, group in by_path.items():
        it = item_type.get(path)
        kind = it["type"] if it else ""
        source = f'<span class="tag">{it["source"]}</span>' if it else ""
        rows = "".join(
            f'<li>{state_chip(LEVEL_STATE[i["level"]])}'
            f'<span class="msg">{_sd_layer(i)}{i["message"]}'
            + (f'<span class="sdcount">×{i["count"]} nodes</span>'
               if i.get("count", 1) > 1 else "")
            + (f'<span class="sdvalue">value: {escape(i["value"])}</span>'
               if i.get("value") else "")
            + '</span></li>'
            for i in group)
        blocks.append(
            f'<div class="sditem"><div class="head">'
            f'<span class="type">{escape(kind) if kind else "—"}</span>{source}'
            f'<span class="path">{escape(path)}</span></div>'
            f'<ul class="sdissues">{rows}</ul></div>')

    snippets = rec.get("schema_snippets") or {}
    for path, raw in list(snippets.items())[:1]:
        blocks.append(f'<div class="ev"><span class="k">{escape(path)} as published'
                      f'</span><pre>{escape(raw)}</pre></div>')

    return (f'<details class="sdpage"><summary>'
            f'<span class="p">{escape(_short(rec["url"], ctx.cfg.base))}</span>'
            f'<span class="tally">{"".join(tally)}</span></summary>'
            f'<div class="inner">{"".join(blocks)}</div></details>')


# --------------------------------------------------------------------------
# The scores
#
# One hero figure — the overall — then a stat tile per category, then the
# arithmetic in full: every group, every metric, what was measured, what the
# target was, and what to do about the gap. A score nobody can take apart is
# a number; a score with its own working shown is a work list.
# --------------------------------------------------------------------------

def _meter(value: float | None, sev: str, ticks: bool = False) -> str:
    """A 0–100 meter. The fill carries the band; the grade word travels beside it."""
    if value is None:
        return ""
    tick_html = ""
    if ticks:
        marks = "".join(
            f'<i style="left:{b}%"></i><b style="left:{b}%">{b}</b>'
            for b, _w, _s in reversed(score_mod.BANDS) if b)
        tick_html = f'<div class="ticks">{marks}</div>'
    return (f'<div class="meter"><div class="track" role="img" '
            f'aria-label="{round(value)} out of 100">'
            f'<span class="fill {sev}" style="width:{max(1.5, value):.2f}%"></span>'
            f'</div>{tick_html}</div>')


def _score_tiles(score) -> str:
    """The headline: hero overall, then one tile per category."""
    if not score:
        return ""
    word, sev = score.grade, score.severity
    if score.overall is None:
        # No number, and no meter either: a bar at 0 reads as "scored zero",
        # which is the opposite of what happened. The reason takes its place.
        cells = [
            '<div>'
            '<span class="k">Overall score</span>'
            '<div class="na">&mdash;</div>'
            f'<div class="chip">{sev_chip(sev, "not scored")}</div>'
            f'<p class="basis">{escape(word)}. Nothing about this site was '
            'measured, so no score is published — the findings below say why. '
            f'{round(100 * score.coverage)}% of the model\'s weight was '
            'measurable.</p>'
            '</div>']
    else:
        cells = [
            '<div>'
            '<span class="k">Overall score</span>'
            f'<div class="big">{score.overall}<span class="of">/100</span></div>'
            f'<div class="chip">{sev_chip(sev, word)}</div>'
            f'{_meter(score.overall, sev, ticks=True)}'
            f'<p class="basis">SEO weighted {score.seo.weight if score.seo else 0}, '
            f'performance {score.performance.weight if score.performance else 0}. '
            f'Confidence {score.confidence} — {round(100 * score.coverage)}% of the '
            f'model\'s weight measured.</p>'
            '</div>']
    for posture in (getattr(score, "spam", None),
                    getattr(score, "phishing", None)):
        if posture is None:
            continue
        if posture.score is None:
            cells.append(
                f'<div><span class="k">{escape(posture.name)} · not in overall'
                '</span><div class="na">&mdash;</div>'
                f'<p class="basis">{escape(posture.note or posture.basis)}</p>'
                '</div>')
            continue
        cells.append(
            f'<div><span class="k">{escape(posture.name)} · not in overall</span>'
            f'<div class="num">{posture.score}<span class="of">/100</span></div>'
            f'<div class="chip">{sev_chip(posture.severity, posture.grade)}</div>'
            f'{_meter(posture.score, posture.severity)}'
            f'<p class="basis">{posture.basis} '
            f'<a href="#mail-auth" style="text-decoration:none">'
            f'{len(posture.measured)} checks</a> — confidence '
            f'{posture.confidence}.</p></div>')  # noqa: E501 - see _mail_section

    rep = getattr(score, "reputation", None)
    if rep is not None:
        if rep.score is None:
            cells.append(
                '<div><span class="k">Reputation · not in overall</span>'
                '<div class="na">&mdash;</div>'
                f'<p class="basis">{escape(rep.basis)}</p></div>')
        else:
            cells.append(
                '<div><span class="k">Reputation · not in overall</span>'
                f'<div class="num">{rep.score}<span class="of">/100</span></div>'
                f'<div class="chip">{sev_chip(rep.severity, rep.grade)}</div>'
                f'{_meter(rep.score, rep.severity)}'
                f'<p class="basis">{escape(rep.basis)} '
                f'<a href="#reputation" style="text-decoration:none">'
                f'{rep.sources_read} source'
                f'{"s" if rep.sources_read != 1 else ""} read</a>'
                + (f", {rep.sources_unread} unreadable" if rep.sources_unread
                   else "")
                + f" — confidence {rep.confidence}."
                '</p></div>')

    for cat in score.categories:
        if cat.total is None:
            cells.append(
                f'<div><span class="k">{escape(cat.name)}</span>'
                f'<div class="na">&mdash;</div>'
                f'<p class="basis">{cat.note or "Not measured."}</p></div>')
            continue
        # The homepage's own score gets its own line rather than a clause at the
        # end of a paragraph: it is the number a reader will compare against a
        # PageSpeed Insights run, so it has to be findable at a glance.
        extra = ""
        home = cat.extra.get("home_score")
        if home is not None:
            extra = (f'<p class="basis" style="margin-top:7px">'
                     f'<a href="#performance-score" style="text-decoration:none">'
                     f'<strong>Homepage {home}/100</strong></a> — the single-page '
                     f'number, directly comparable with a Lighthouse run.</p>')
        cells.append(
            f'<div><span class="k">{escape(cat.name)} · weight {cat.weight}</span>'
            f'<div class="num">{cat.total}<span class="of">/100</span></div>'
            f'<div class="chip">{sev_chip(cat.severity, cat.grade)}</div>'
            f'{_meter(cat.total, cat.severity)}'
            f'<p class="basis">{cat.basis}</p>{extra}</div>')
    return f'<section class="scores">{"".join(cells)}</section>'


def _fix_block(fix: str, findings: tuple = ()) -> str:
    if not fix:
        return ""
    also = ""
    if findings:
        links = " · ".join(f'<a href="#{escape(f)}">{escape(f)}</a>' for f in findings)
        also = (f'<div class="also">Affected pages are listed under {links}</div>')
    return ('<details class="fix"><summary>How to fix</summary>'
            f'<div class="body"><p>{fix}</p>{also}</div></details>')


def _metric_row(m) -> str:
    if not m.measured:
        return (f'<div class="mrow"><div class="mtop">'
                f'<span class="lab">{escape(m.label)}</span>'
                f'<span></span><span class="pc" style="color:var(--ink-faint)">&mdash;</span>'
                f'</div><div class="mmeta"><span>not measured</span></div>'
                + (f'<p class="mnote">{m.note}</p>' if m.note else "")
                + '</div>')
    pct = m.pct
    perfect = pct >= 100
    return (
        '<div class="mrow"><div class="mtop">'
        f'<span class="lab">{escape(m.label)}</span>'
        f'<span class="bar"><i style="width:{max(1.5, pct):.1f}%"></i></span>'
        f'<span class="pc">{pct}</span>'
        '</div>'
        '<div class="mmeta">'
        f'<span class="measured">{m.detail}</span>'
        f'<span>target: {escape(m.target)}</span>'
        + (f'<span>curve: {escape(score_mod.window_text(m.key))}</span>'
           if m.key in score_mod.WINDOWS else "")
        + f'<span>weight {m.weight:g}</span>'
        + (f'<span>&minus;{m.lost:.1f} within the group</span>' if not perfect else
           f'<span>{sev_chip("good", "at target")}</span>')
        + '</div>'
        + (f'<p class="mnote">{m.note}</p>' if m.note else "")
        + ("" if perfect else _fix_block(m.fix, m.findings))
        + '</div>')


def _group_block(g, open_worst: bool = False) -> str:
    score = g.score
    if score is None:
        return (f'<details class="mgroup"><summary>'
                f'<span class="gname">{escape(g.name)}'
                f'<span class="k">not scored</span></span>'
                f'<span class="gscore" style="color:var(--ink-faint)">&mdash;</span>'
                f'<span class="glost"></span></summary>'
                f'<div class="inner"><p class="mnote">{g.note}</p></div></details>')
    rows = "".join(_metric_row(m) for m in
                   sorted(g.metrics, key=lambda m: (m.measured is False, -m.lost)))
    return (
        f'<details class="mgroup"{" open" if open_worst else ""}><summary>'
        f'<span class="gname">{escape(g.name)}'
        f'<span class="k">weight {g.effective_weight:.0f} of 100</span></span>'
        f'<span class="gscore">{round(score)}</span>'
        f'<span class="glost">&minus;{g.lost:.1f}</span></summary>'
        f'<div class="inner"><p class="mnote" style="margin:10px 0 0">{g.what}</p>'
        f'{rows}</div></details>')


def _seo_section(score) -> str:
    cat = score.seo if score else None
    if not cat:
        return ""
    bars = _depth_chart_generic(
        [(g.name, round(g.score)) for g in cat.scored_groups]) if cat.scored_groups else ""
    worst = cat.deficits[0].key if cat.deficits else None
    groups = "".join(_group_block(g, open_worst=(g.key == worst))
                     for g in cat.groups)
    lost = 100 - (cat.score or 0)
    return (
        '<section class="sec" id="seo-score">'
        f'<div class="sechead"><h2>SEO score</h2>'
        f'<span class="count">{cat.total}/100</span>'
        f'<p>{cat.basis} Every row states what was measured, what the target was, '
        f'and what to do about the gap; the groups add up to the '
        f'{cat.total} above, and the {lost:.0f} points lost are attributed below.'
        '</p></div>'
        '<div class="f" style="border-top:1px solid var(--rule)"><div class="fbody">'
        + (f'<span class="k">Score by group</span>'
           f'<div style="margin:11px 0 22px">{bars}</div>' if bars else "")
        + '<span class="k">Where the points went</span>'
          f'<div style="margin-top:11px">{groups}</div>'
        '</div></div></section>')


def _vitals_table(cat, base: str) -> str:
    group = next((g for g in cat.groups if g.key == "vitals"), None)
    if not group or group.score is None:
        return ""
    rows = "".join(
        f'<tr><td>{escape(m.label)}<span class="sub">{escape(m.key[3:].upper())}</span></td>'
        f'<td class="n">{m.detail}</td>'
        f'<td>{escape(m.target)}</td>'
        f'<td class="n">{m.weight:g}%</td>'
        f'<td class="n">{m.pct}</td></tr>'
        for m in group.metrics)
    pages = cat.extra.get("pages") or []
    per_page = ""
    if len(pages) > 1:
        prows = "".join(
            f'<tr><td class="mono">{escape(_short(p["url"], base))}</td>'
            f'<td class="n">{vitals_mod.fmt("lcp", p.get("lcp"))}</td>'
            f'<td class="n">{vitals_mod.fmt("cls", p.get("cls"))}</td>'
            f'<td class="n">{vitals_mod.fmt("tbt", p.get("tbt"))}</td>'
            f'<td class="n">{vitals_mod.fmt("fcp", p.get("fcp"))}</td>'
            f'<td class="n">{"" if p.get("score") is None else round(100 * p["score"])}</td>'
            '</tr>'
            for p in pages)
        per_page = (
            '<span class="k">Every rendered page</span>'
            '<div class="scroll" style="margin:10px 0 0">'
            '<table><thead><tr><th>Page</th><th class="n">LCP</th><th class="n">CLS</th>'
            '<th class="n">TBT</th><th class="n">FCP</th><th class="n">Score</th>'
            '</tr></thead><tbody>' + prows + '</tbody></table></div>')

    fixes = "".join(
        f'<div class="mrow"><div class="mtop">'
        f'<span class="lab"><strong>{escape(m.label)}</strong> — '
        f'{m.detail}</span>'
        f'<span class="bar"><i style="width:{max(1.5, m.pct):.1f}%"></i></span>'
        f'<span class="pc">{m.pct}</span></div>'
        + _fix_block(m.fix) + '</div>'
        for m in sorted(group.metrics, key=lambda m: -m.lost) if m.measured and m.pct < 90)

    page_scores = cat.extra.get("page_scores") or []
    composite = cat.extra.get("composite_score")
    aggregation = ""
    if len(page_scores) > 1:
        aggregation = (
            f'<p class="mnote" style="margin:8px 0 22px">The category score '
            f'({cat.total}) is the <strong>median of the per-page scores</strong> '
            f'({", ".join(str(s) for s in sorted(page_scores))}) — not the score of '
            f'the medians in this table'
            + (f', which would be {composite}' if composite is not None else '')
            + '. Each metric\'s median picks whichever page happens to be best at '
              'it, so combining them describes a page with none of the site\'s '
              'faults. The rows below are diagnostics: what a typical value looks '
              'like per metric, and what it is worth.</p>')

    return (
        '<span class="k">Metrics, weights and curves</span>'
        '<div class="scroll" style="margin:10px 0 0">'
        '<table class="cwv"><thead><tr><th>Metric</th><th class="n">Measured</th>'
        '<th>Lighthouse curve</th><th class="n">Weight</th><th class="n">Score</th>'
        '</tr></thead><tbody>' + rows + '</tbody></table></div>' + aggregation
        + (f'<span class="k">Worth fixing first</span>'
           f'<div style="margin:6px 0 22px">{fixes}</div>' if fixes else "")
        + per_page)


def _opportunities(cat) -> str:
    if not cat.opportunities:
        return ""
    blocks = []
    for o in cat.opportunities:
        save = ""
        if o.savings_bytes:
            save = f"~{_kb(o.savings_bytes)}"
        elif o.savings_ms:
            save = f"~{o.savings_ms:,} ms"
        links = ""
        if o.findings:
            links = (' · '.join(f'<a href="#{escape(f)}">{escape(f)}</a>'
                                for f in o.findings))
            links = f'<div class="also">Detail and affected pages: {links}</div>'
        blocks.append(
            f'<div class="opp"><div class="head"><h4>{escape(o.label)}</h4>'
            f'<span class="save">{save}</span></div>'
            f'<p>{o.detail}</p>'
            f'<div class="why"><details class="fix" open><summary>How to fix</summary>'
            f'<div class="body"><p>{o.fix}</p>{links}</div></details></div></div>')
    return ('<span class="k">Opportunities — unscored, in Lighthouse\'s sense</span>'
            '<p class="tablenote" style="margin:6px 0 11px">These do not move the '
            'score above; Lighthouse\'s opportunities do not move its score either. '
            'They are the work that would — ranked by the bytes or milliseconds each '
            'one is worth.</p>'
            f'<div>{"".join(blocks)}</div>')


def _performance_section(score, base: str) -> str:
    cat = score.performance if score else None
    if not cat:
        return ""
    profile = cat.extra.get("profile", "desktop")
    missing = cat.extra.get("missing") or {}
    ttfb = cat.extra.get("median_ttfb")
    home = cat.extra.get("home_score")
    pages = cat.extra.get("pages") or []
    compare = ""
    if home is not None and cat.total is not None:
        word, sev = score_mod.band(home)
        gap = ""
        if len(pages) > 1 and abs(home - cat.total) >= 8:
            gap = (f" It differs from the {cat.total} above because that is the "
                   f"median across {len(pages)} templates and this is one page: "
                   f"{'the homepage is the slower' if home < cat.total else 'the homepage is the faster'}"
                   f" of them.")
        compare = (
            f'<p class="mnote" style="margin:0 0 16px"><strong>Homepage: '
            f'{home}/100</strong> {sev_chip(sev, word)} — the number a PageSpeed '
            f'Insights run of the homepage on this profile should land near, since '
            f'Lighthouse scores one URL and this section scores a site.{gap}</p>')
    crawl_ms = cat.extra.get("crawl_response_median")
    sample_note = ""
    if crawl_ms and ttfb and crawl_ms > max(1.6 * ttfb, ttfb + 400):
        sample_note = (
            f'<p class="mnote" style="margin:0 0 16px">The median page across the '
            f'whole crawl answered in {crawl_ms:,} ms, against '
            f'{vitals_mod.fmt("ttfb", ttfb)} on the '
            f'{len(pages)} pages measured here — so these pages are faster than the '
            f'site typically is, and the score above is optimistic. MED-03 lists '
            f'the slow ones.</p>')
    caveat = (
        f'Measured in Chromium on the <strong>{escape(profile)}</strong> profile, '
        'before any scrolling. '
        + ("Applied CDP throttling (150 ms RTT, 1.6 Mbps, 4× CPU) rather than "
           "Lighthouse's simulated throttling, which can differ by 10–20%. "
           if profile == "mobile" else
           "No throttling, scored against Lighthouse's desktop control points — "
           "a mobile Lighthouse run will be harsher. ")
        + (f"Speed Index ({missing.get('si')}% in Lighthouse) needs frame-by-frame "
           "video analysis and is not collected, so the remaining weights "
           "renormalise. " if missing else "")
        + (f"Median time to first byte {vitals_mod.fmt('ttfb', ttfb)}."
           if ttfb else ""))
    body = compare + sample_note + _vitals_table(cat, base) + _opportunities(cat)
    if cat.total is None:
        body = (f'<p class="mnote">{cat.note}</p>' + _opportunities(cat))
    return (
        '<section class="sec" id="performance-score">'
        '<div class="sechead"><h2>Performance</h2>'
        f'<span class="count">{"—" if cat.total is None else str(cat.total) + "/100"}'
        '</span>'
        f'<p>Core Web Vitals scored on Lighthouse\'s own log-normal curves, so the '
        f'number is comparable with a Lighthouse run rather than with itself. '
        f'{caveat}</p></div>'
        '<div class="f" style="border-top:1px solid var(--rule)">'
        f'<div class="fbody">{body}</div></div></section>')


def _gate_callout(score) -> str:
    if not score or not score.gates:
        return ""
    rows = "".join(
        f'<p><strong>Capped at {g.cap}.</strong> {g.reason} {g.fix}</p>'
        for g in score.gates)
    n = len(score.gates)
    return (f'<div class="callout warn" style="border-left-color:var(--m-critical)">'
            f'<p><strong>The overall score is capped.</strong> '
            f'{"One fault" if n == 1 else f"{n} faults"} below cannot be averaged '
            f'away — a site in this state does not rank whatever else is right, so '
            f'each one puts a ceiling on the total. Raw weighted score before the '
            f'cap: {score.raw:.0f}.</p>{rows}</div>')


# Tier -> the chip a listing at that level earns. Status hue only, never a new
# one: `theme.sev_chip` is the single sanctioned way to draw a status, and a
# listing's colour has to mean the same thing here as it does on a finding.
REP_TIER_CHIP = {"browser": "critical", "gateway": "high", "mail": "high",
                 "endpoint": "medium", "feed": "low", "unknown": "low"}

# Read in the report's own words, next to the verdicts, because a reader handed
# "4 of 89 vendors" by someone else needs to see why this report does not print
# that number.
REP_METHOD = (
    "Every source below was asked about this host directly. None of them is a "
    "vote, and the count of how many flagged it is not used: an aggregate like "
    "&ldquo;4 of 89 vendors&rdquo; has no usable denominator &mdash; it is how "
    "many feeds the aggregator polls this month, and it moves without the site "
    "changing. What is used is <strong>what a listing at each source actually "
    "does to a visitor</strong>, which is the column on the right.")
REP_CONTROL_NOTE = (
    "Each source is also asked about a domain it publishes as a known-bad test "
    "point, on the same run and through the same request. If that control does "
    "not come back listed, the source is reported as <em>not read</em> rather "
    "than as a pass &mdash; two of these lists answer a refused query in a way "
    "that reads exactly like a clean answer, so without the control this "
    "section would be publishing verdicts nobody measured.")


def _rep_verdict_chip(rec: dict) -> str:
    if rec.get("error") or "listed" not in rec and not rec.get("hits"):
        if rec.get("hits") is None and rec.get("error"):
            return sev_chip("low", "not read")
    if rec.get("hits"):
        return sev_chip(REP_TIER_CHIP.get(rec.get("tier", ""), "low"), "listed")
    if rec.get("listed"):
        return sev_chip(REP_TIER_CHIP.get(rec.get("tier", ""), "low"), "listed")
    if rec.get("error"):
        return sev_chip("low", "not read")
    if rec.get("no_record"):
        return sev_chip("low", "no record")
    return sev_chip("good", "not listed")


def _rep_rows(sources: list[dict]) -> str:
    """One row per source, with its provenance on the line beneath it.

    Two lines rather than eight columns: the endpoint is a URL and the method is
    a sentence, and neither survives a 360px column. This is the same shape
    `_url_list` uses for a page's extra hits.
    """
    order = {"safe_browsing": 0, "web_risk": 1, "resolver": 2, "dnsbl": 3,
             "feed": 4, "virustotal": 5}
    rows = []
    for rec in sorted(sources, key=lambda r: (order.get(r.get("source"), 9),
                                              (r.get("vendor") or "").lower())):
        vendor = rec.get("vendor") or rec.get("source") or "unknown source"
        tier = rec.get("tier") or ""
        # A real em dash, not an entity: this string is escaped below, and an
        # escaped entity renders as its own source text.
        consequence = (theme_tiers().get(tier, (0, ""))[1] if tier else
                       "not a blocking list \u2014 breadth only")
        rows.append(
            f'<tr class="repsrc"><td>{escape(vendor)}</td>'
            f'<td class="verdict">{_rep_verdict_chip(rec)}</td>'
            f'<td>{escape(consequence)}</td></tr>')

        bits = []
        if rec.get("endpoint"):
            bits.append(f'<span class="lbl">asked</span> '
                        f'<code>{escape(rec["endpoint"])}</code>')
        ok = rec.get("control_ok")
        if ok is False:
            # The target was never queried: the control failed first and the
            # source stood down. An "answered" line here would describe a
            # measurement that did not happen.
            bits.append('<span class="lbl">answered</span> not asked &mdash; '
                        'the control failed first, so this source stood down')
        else:
            answer = rec.get("answer") or rec.get("error") or ""
            if answer:
                bits.append(f'<span class="lbl">answered</span> '
                            f'<code>{escape(str(answer))}</code>')
        if rec.get("control"):
            said = rec.get("control_answer")
            bits.append(
                f'<span class="lbl">control</span> '
                f'<code>{escape(str(rec["control"]))}</code> &rarr; '
                + escape(str(said if said else
                             ("verified once for this run" if ok
                              else "not verified")))
                + ("" if ok is not False else " &mdash; source not read"))
        if rec.get("method"):
            bits.append(escape(rec["method"]))
        if rec.get("terms"):
            bits.append(f'<span class="lbl">terms</span> '
                        f'{escape(rec["terms"])}')
        rows.append('<tr class="repprov"><td colspan="3">'
                    + " &middot; ".join(bits) + "</td></tr>")
    return "".join(rows)


def theme_tiers():
    """`reputation.TIERS`, imported lazily so `report` keeps no import of a
    stage it only renders."""
    from . import reputation as rep_mod
    return rep_mod.TIERS


def _posture_rows(posture) -> str:
    """One row per check, with its weight, what was found, and the fix.

    The weight is printed because these are a weighted checklist rather than a
    ratio through a calibrated window, and a reader who cannot see the weights
    cannot argue with the number.
    """
    rows = []
    for m in posture.metrics:
        if not m.measured:
            rows.append(
                f'<tr class="repsrc"><td>{escape(m.label)}</td>'
                f'<td class="verdict">{sev_chip("low", "not measured")}</td>'
                f'<td class="n">{m.weight:.0f}</td><td>{m.note or ""}</td></tr>')
            continue
        state = ("good" if m.score >= 0.999 else
                 "medium" if m.score >= 0.5 else "critical")
        word = ("pass" if m.score >= 0.999 else
                "partial" if m.score > 0 else "fail")
        rows.append(
            f'<tr class="repsrc"><td>{escape(m.label)}</td>'
            f'<td class="verdict">{sev_chip(state, word)}</td>'
            f'<td class="n">{m.weight:.0f}</td><td>{m.detail}</td></tr>')
        if m.score < 0.999 and m.fix:
            rows.append('<tr class="repprov"><td colspan="4">'
                        f'<span class="lbl">target</span> {m.target} '
                        f'&middot; {m.fix}</td></tr>')
    return "".join(rows)


def _posture_block(posture) -> str:
    if posture is None:
        return ""
    if posture.score is None:
        return (
            '<div class="f" style="border-top:1px solid var(--rule)">'
            f'<div class="fbody"><span class="k">{escape(posture.name)}</span>'
            '<div class="na" style="margin:2px 0 0">&mdash;</div>'
            f'<p class="mnote">{escape(posture.note or posture.basis)}</p>'
            '</div></div>')
    return (
        '<div class="f" style="border-top:1px solid var(--rule)">'
        f'<div class="fbody"><span class="k">{escape(posture.name)}</span>'
        f'<div class="big" style="margin:2px 0 6px">{posture.score}'
        '<span class="of">/100</span></div>'
        f'<div class="chip">{sev_chip(posture.severity, posture.grade)}</div>'
        f'{_meter(posture.score, posture.severity, ticks=True)}'
        f'<p class="mnote" style="margin-top:9px">{escape(posture.what)} '
        f'{posture.basis} Confidence {posture.confidence} — '
        f'{round(100 * posture.coverage)}% of the checklist measured. '
        '<strong>Not part of the overall score.</strong></p>'
        '<div class="scroll" style="margin:9px 0 0"><table><thead><tr>'
        '<th>Check</th><th>Result</th><th class="n">Weight</th>'
        '<th>What was found, and what to do</th></tr></thead>'
        f'<tbody>{_posture_rows(posture)}</tbody></table></div>'
        '</div></div>')


def _mail_records(mail: dict) -> str:
    """The records as published, and the exact lookup that read each one."""
    rows = []
    for key, label in (("spf", "SPF"), ("dmarc", "DMARC"), ("dkim", "DKIM"),
                       ("mx", "MX"), ("mta_sts", "MTA-STS"), ("bimi", "BIMI")):
        rec = mail.get(key) or {}
        if not rec:
            continue
        if key == "dkim":
            live = [f for f in (rec.get("found") or []) if not f.get("revoked")]
            value = (", ".join(
                f"{f['selector']} ({f.get('key_type', 'rsa')}, ~{f['bits']}-bit)"
                for f in live) if live else
                f"no key on {len(rec.get('probed') or [])} conventional selectors")
        elif rec.get("error"):
            value = rec["error"]
        else:
            got = rec.get("records") or ([rec.get("record")] if rec.get("record")
                                         else [])
            value = " · ".join(g for g in got if g) or "no record published"
        rows.append(
            f'<tr class="repsrc"><td>{label}</td><td>{escape(value[:400])}</td>'
            f'</tr><tr class="repprov"><td colspan="2">'
            f'<span class="lbl">read by</span> '
            f'<code>{escape(rec.get("endpoint", ""))}</code> &middot; '
            f'{escape(rec.get("what", ""))}</td></tr>')
    if not rows:
        return ""
    return ('<div class="f" style="border-top:1px solid var(--rule)">'
            '<div class="fbody"><span class="k">The records, as published</span>'
            '<div class="scroll" style="margin:9px 0 0"><table><thead><tr>'
            '<th>Record</th><th>Value</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></div></div>')


def _mail_section(result, ctx) -> str:
    """Spam and phishing posture, and the DNS records both are built from.

    Rendered whenever the lookups ran, pass or fail, for the same reason the
    reputation section is: "no findings" is not visibly different from "nobody
    looked", and these two numbers are the ones a reader came for.
    """
    mail = getattr(result, "mailauth", None) or {}
    score = getattr(result, "score", None)
    spam = getattr(score, "spam", None)
    phishing = getattr(score, "phishing", None)
    if not mail and spam is None and phishing is None:
        return ""

    unread = mail.get("unavailable") or []
    unread_html = ""
    if unread:
        unread_html = (
            '<div class="f" style="border-top:1px solid var(--rule)">'
            '<div class="fbody">'
            '<span class="k">Not readable on this run</span>'
            '<ul class="repunread">'
            + "".join(f'<li>{escape(u)}</li>' for u in unread)
            + '</ul></div></div>')

    host = escape(mail.get("host", "") or urlparse(result.config.base).netloc)
    head = (
        '<div class="sechead"><h2>Spam and phishing posture</h2>'
        f'<span class="count">{mail.get("lookups", 0)}</span>'
        f'<p>Read from DNS for <code>{host}</code> &mdash; records this domain '
        'publishes about itself, so every fix is one DNS change and nothing '
        'here is a third party&rsquo;s opinion. Both numbers are weighted '
        'checklists against the published standards, and neither is part of '
        'the overall score.</p></div>')

    return ('<section class="sec" id="mail-auth">' + head
            + _posture_block(spam) + _posture_block(phishing)
            + _mail_records(mail) + unread_html + '</section>')


def _reputation_section(result, ctx) -> str:
    """What the blocklists said, where each answer came from, and how it is read.

    Rendered on every run, including a completely clean one. A section that only
    appears when something is wrong cannot be used to show that nothing is: the
    reader's actual question is "is my site flagged", and "no findings" is not
    visibly different from "we never looked".
    """
    rep = getattr(result, "reputation", None) or {}
    cfg = result.config
    if not cfg.check_reputation and not rep:
        return ""

    sources = [r for r in (rep.get("sources") or []) if r.get("source")]

    read = len(rep.get("clean") or {}) + len({
        r.get("vendor") for r in rep.get("listings") or []})
    unread = rep.get("unavailable") or []

    head = (f'<div class="sechead"><h2>Reputation and blocklists</h2>'
            f'<span class="count">{read}</span>'
            f'<p>What third-party blocklists already say about this host &mdash; '
            f'every source named, with the exact request that produced the '
            f'answer.</p></div>')

    # The score, at the head of the evidence it was derived from, so the number
    # and its basis are never read apart.
    rep_score = getattr(getattr(result, "score", None), "reputation", None)
    score_html = ""
    if rep_score is not None:
        if rep_score.score is None:
            score_html = (
                '<div class="f" style="border-top:1px solid var(--rule)">'
                '<div class="fbody">'
                '<span class="k">Reputation score</span>'
                '<div class="na" style="margin:2px 0 0">&mdash;</div>'
                f'<p class="mnote">{escape(rep_score.basis)}</p>'
                '</div></div>')
        else:
            score_html = (
                '<div class="f" style="border-top:1px solid var(--rule)">'
                '<div class="fbody">'
                '<span class="k">Reputation score</span>'
                f'<div class="big" style="margin:2px 0 6px">{rep_score.score}'
                '<span class="of">/100</span></div>'
                f'<div class="chip">{sev_chip(rep_score.severity, rep_score.grade)}'
                '</div>'
                f'{_meter(rep_score.score, rep_score.severity, ticks=True)}'
                f'<p class="mnote" style="margin-top:9px">'
                f'{escape(rep_score.basis)} Built from '
                f'{rep_score.sources_read} source'
                f'{"s" if rep_score.sources_read != 1 else ""} that answered'
                + (f' and {rep_score.sources_unread} that could not be read'
                   if rep_score.sources_unread else '')
                + f' — confidence {rep_score.confidence}. '
                '<strong>This number is not part of the overall score</strong> '
                'and never moves it: every other metric in the model is a ratio '
                'over this site&rsquo;s own pages, and a third party&rsquo;s '
                'opinion is neither a ratio nor a property of the pages. The one '
                'case that must move the total already does, as a cap &mdash; a '
                'browser-level listing limits the overall to 20.</p>'
                '<p class="mnote">An unreadable source costs no points here; it '
                'is disclosed in the confidence instead, which is the same rule '
                'the rest of the model follows for a stage that did not run.</p>'
                '</div></div>')

    verdict = ""
    if rep.get("worst_tier"):
        verdict = (
            f'<p class="what"><strong>This host is listed.</strong> The most '
            f'consequential listing is {escape(rep["worst_tier"])}-level &mdash; '
            f'{escape(theme_tiers().get(rep["worst_tier"], (0, ""))[1])}. '
            f'See REP-01&hellip;REP-03 above for what to do about it.</p>')
    elif read:
        verdict = (
            f'<p class="what"><strong>No listing found.</strong> {read} '
            f'source{"s" if read != 1 else ""} answered and none of them lists '
            f'this host'
            + (f'; {len(unread)} could not be read and '
               f'{"is" if len(unread) == 1 else "are"} shown below as unread '
               'rather than counted as a pass' if unread else '')
            + '.</p>')
    else:
        verdict = ('<p class="what"><strong>Not measured.</strong> No source '
                   'returned a usable answer on this run, so this section says '
                   'nothing about the host either way.</p>')

    unread_html = ""
    if unread:
        unread_html = (
            '<span class="k">Not readable on this run</span>'
            '<ul class="repunread">'
            + "".join(f'<li>{escape(u)}</li>' for u in unread)
            + '</ul>')

    table = ""
    if sources:
        table = (
            '<span class="k">Sources, and where each answer came from</span>'
            '<div class="scroll" style="margin:9px 0 22px">'
            '<table><thead><tr><th>Source</th><th>Verdict</th>'
            '<th>What a listing here would do to a visitor</th></tr></thead>'
            f'<tbody>{_rep_rows(sources)}</tbody></table></div>')

    grading = (
        '<span class="k">How a listing would be graded</span>'
        '<div class="scroll" style="margin:9px 0 0"><table><thead><tr>'
        '<th>Level</th><th>Consequence</th><th>Severity</th></tr></thead><tbody>'
        + "".join(
            f'<tr><td>{escape(name)}</td><td>{escape(text)}</td>'
            f'<td class="verdict">{sev_chip(REP_TIER_CHIP.get(name, "low"))}</td></tr>'
            for name, (_, text) in sorted(theme_tiers().items(),
                                          key=lambda kv: kv[1][0]))
        + '</tbody></table></div>'
        '<p class="mnote">A browser-level listing is critical on its own &mdash; '
        'it is not a vote, it is already happening to visitors, and it caps the '
        'overall score at 20. At the gateway and mail levels one vendor is '
        'reported as medium and two or more as high, because a single automated '
        'categoriser mislabels lead-generation and local-service sites '
        'routinely.</p>')

    return ('<section class="sec" id="reputation">' + head + score_html
            + '<div class="f" style="border-top:1px solid var(--rule)">'
            '<div class="fbody">' + verdict
            + f'<p class="mnote">{REP_METHOD}</p>'
            + f'<p class="mnote" style="margin-bottom:16px">{REP_CONTROL_NOTE}</p>'
            + table + unread_html + grading
            + '</div></div></section>')


def _depth_chart_generic(items: list[tuple[str, int]]) -> str:
    """Magnitude bars for any label/value pairs — one hue, value at the end."""
    if not items:
        return ""
    top = max(v for _, v in items) or 1
    rows = "".join(
        f'<div class="hbar"><div class="row"><span class="lab">{escape(str(k))}</span>'
        f'<span class="track"><span class="fill" '
        f'style="width:{max(2, 100 * v / top):.3f}%"></span></span></div>'
        f'<span class="val">{v}</span></div>'
        for k, v in items)
    return f'<div class="hbars">{rows}</div>'


def render(result, ctx) -> str:
    cfg = result.config
    base = cfg.base
    host = urlparse(base).netloc
    finished = datetime.fromisoformat(result.finished)
    off = finished.strftime("%z")
    stamp = (f"{finished.strftime('%d %b %Y')} · {finished.strftime('%H:%M:%S')}"
             + (f" UTC{off[:3]}:{off[3:]}" if off else ""))
    short_stamp = finished.strftime("%d %b %Y · %H:%M")

    counts = result.counts
    total_urls = len(result.records)
    n_pages = ctx.n

    by_cat: dict[str, list] = {}
    for f in result.findings:
        by_cat.setdefault(f.category, []).append(f)
    ordered_cats = [c for c in CATEGORY_ORDER if c in by_cat] + \
                   [c for c in by_cat if c not in CATEGORY_ORDER]

    shots_by_id: dict[str, list] = {}
    for s in getattr(result, "shots", None) or []:
        shots_by_id.setdefault(s.finding_id, []).append(s)
    thumbs = getattr(result, "thumbs", None) or {}

    sections, rail_items = [], []
    for cat in ordered_cats:
        items = by_cat[cat]
        anchor = "cat-" + "".join(ch if ch.isalnum() else "-" for ch in cat.lower())
        body = "".join(_finding(f, base, shots_by_id, thumbs) for f in items)
        sections.append(
            f'<section class="sec" id="{anchor}" data-cat="{escape(cat)}">'
            f'<div class="sechead"><h2>{escape(cat)}</h2>'
            f'<span class="count">{len(items)}</span>'
            f'<p>{CATEGORY_BLURB.get(cat, "")}</p></div>{body}'
            f'<div class="nores" hidden>Nothing in this section matches the filter.</div>'
            f'</section>')
        for f in items:
            rail_items.append(
                f'<li class="{f.severity}" data-sev="{f.severity}">'
                f'<a href="#{escape(f.id)}"><span class="sw"></span>'
                f'<span class="id">{escape(f.id)}</span>'
                f'<span class="c">{f.count or ""}</span></a></li>')

    filters = "".join(
        f'<button type="button" class="{s}" data-sev="{s}" aria-pressed="true">'
        f'<span class="sw"></span>{s}</button>' for s in SEVERITIES)

    stages = [f"{total_urls} pages fetched"]
    if cfg.check_links:
        stages.append(f"{len(ctx.link_status)} links verified")
    if cfg.check_security:
        stages.append("endpoint probes")
    # Three states, not two: answered, attempted and refused, never asked. A
    # line that says "1 blocklist lookup" about a 401 tells the reader the host
    # came back clean, which is the same mistake `ERR-14` exists to stop the
    # crawl making about a refused page.
    rep = getattr(result, "reputation", None) or {}
    answered = bool(rep.get("listings") or rep.get("engines")
                    or rep.get("clean") or rep.get("web_risk_clean") is not None)
    if answered:
        n = len(rep.get("clean") or {}) + len({
            r.get("vendor") for r in rep.get("listings") or []})
        unread = len(rep.get("unavailable") or [])
        stages.append(f"{n} blocklist source{'s' if n != 1 else ''} read"
                      + (f" ({unread} unreadable)" if unread else ""))
    elif rep.get("queried"):
        why = (rep.get("unavailable") or ["no answer"])[0]
        stages.append(f"blocklist lookup not completed ({_plain(why)[:120]})")
    elif cfg.check_reputation:
        stages.append("no blocklist lookup (no reputation API key configured)")
    if result.runtime:
        stages.append(f"{len(result.runtime)} pages rendered in Chromium")
    images = getattr(result, "images", None) or {}
    if images:
        stages.append(f"{len(images)} images weighed")
    schema_items = sum(len(r.get("schema_items") or []) for r in ctx.pages)
    if schema_items:
        stages.append(f"{schema_items} structured-data items validated")
    shots = getattr(result, "shots", None) or []
    if shots:
        stages.append(f"{len(shots)} findings screenshotted")

    heavy = sorted((m.get("bytes", 0) for m in images.values()), reverse=True)
    over_limit = [b for b in heavy if b > cfg.image_max_kb * 1024]
    schema_errors = sum(r.get("schema_errors") or 0 for r in ctx.pages)

    trunc = ""
    if getattr(ctx, "partial_reason", ""):
        trunc = (f'<p><strong>Partial crawl.</strong> '
                 f'{escape(ctx.partial_reason)}. Orphan and reachability checks '
                 'need the whole link graph, so they were <strong>skipped</strong> '
                 'rather than reported from a sample.</p>')
    elif getattr(result, "truncated", False):
        trunc = (f'<p><strong>Partial crawl.</strong> {result.discovered} pages were '
                 f'discovered and the first {total_urls} analysed. Orphan and '
                 'reachability checks need the whole site to be sound, so they were '
                 '<strong>skipped</strong> rather than reported from a sample — raise '
                 'the page limit to include them.</p>')

    clean = _clean(result, ctx)
    # A URL count on its own is misleading when the URLs belong to another domain,
    # which is what a staging host serving the production sitemap looks like. The
    # count of URLs that are actually on this host goes in the same row (IDX-06).
    def _sm_count(s) -> str:
        on_host = s.get("host_urls")
        if on_host is None or on_host == s.get("urls", 0):
            return str(s["urls"])
        return (f'{s["urls"]}<span class="sub">{on_host} on this host</span>')

    sm_rows = "".join(
        f'<tr><td class="mono">{escape(str(s["sitemap"]).replace(base, "") or "/")}</td>'
        f'<td class="n">{_sm_count(s)}</td><td class="n">{s["status"]}</td></tr>'
        for s in result.sitemaps) or \
        '<tr><td colspan="3">Discovered by following links from the homepage.</td></tr>'

    # Orphan counts are only meaningful on a complete crawl — on a partial one the
    # graph is a sample and every "orphan" is an artefact of the pages we skipped.
    partial = bool(getattr(result, "truncated", False))
    orphan_n = None if partial else len(result.graph.get("orphans", []))
    orphan_tile = (
        '<div class="tile"><div class="n">&mdash;</div>'
        '<span class="k">Orphan pages</span>'
        '<div class="cap">Not measured on a partial crawl</div></div>'
        if partial else
        f'<div class="tile"><div class="n {"crit" if orphan_n else "ok"}">{orphan_n}</div>'
        '<span class="k">Orphan pages</span>'
        '<div class="cap">No inbound internal links</div></div>')
    # The score leads the readout: it is the first thing anybody looks for, and
    # the per-category numbers are what they compare against last month's run.
    score = getattr(result, "score", None)
    score_cells = ""
    if score:
        score_cells = (
            f'<div class="cell"><span class="k">Overall score</span>'
            f'<div class="v">{escape(score.grade)}</div></div>'
            if score.overall is None else
            f'<div class="cell"><span class="k">Overall score</span>'
            f'<div class="v">{score.overall}<span style="color:var(--ink-faint)">'
            f'/100</span> · {score.grade}</div></div>')
        for cat in score.categories:
            value = "&mdash;" if cat.total is None else str(cat.total)
            score_cells += (
                f'<div class="cell"><span class="k">{escape(cat.name)}</span>'
                f'<div class="v">{value}</div></div>')
        for posture in (getattr(score, "spam", None),
                        getattr(score, "phishing", None)):
            if posture is None:
                continue
            pv = "&mdash;" if posture.score is None else str(posture.score)
            label = posture.name.replace(" posture", "")
            score_cells += (
                f'<div class="cell"><span class="k">{escape(label)} · unweighted'
                f'</span><div class="v">{pv}</div></div>')
        rep = getattr(score, "reputation", None)
        if rep is not None:
            # Labelled "unweighted" in the masthead itself. A bare number next
            # to SEO and Performance reads as a third weighted category, and
            # this one deliberately moves nothing.
            rv = "&mdash;" if rep.score is None else str(rep.score)
            score_cells += (
                '<div class="cell"><span class="k">Reputation · unweighted</span>'
                f'<div class="v">{rv}</div></div>')
    # Built here rather than inline below, because the "Jump to" rail must not
    # offer a link to a section that was not rendered. The report asserts it has
    # no dead in-page anchors, and a site where nothing could be read has no
    # structured-data section to jump to.
    sd_section = _schema_section(result, ctx)
    rep_section = _reputation_section(result, ctx)
    mail_section = _mail_section(result, ctx)
    jump = ['<a href="#seo-score">SEO score</a>',
            '<a href="#performance-score">Performance</a>']
    if mail_section:
        jump.append('<a href="#mail-auth">Spam &amp; phishing</a>')
    if rep_section:
        jump.append('<a href="#reputation">Reputation</a>')
    if sd_section:
        jump.append('<a href="#structured-data">Structured data</a>')
    jump_html = "".join(jump)

    score_notes = ""
    if score and score.notes:
        score_notes = ('<div class="callout"><p><strong>'
                       + ('Why there is no score.' if score.overall is None
                          else 'How the score was bounded.')
                       + '</strong> ' + " ".join(score.notes) + '</p></div>')

    findings_html = "".join(sections) or (
        '<section class="sec"><div class="sechead"><h2>No issues found</h2>'
        '<span class="count">0</span><p>Every check passed on this crawl.</p></div>'
        '</section>')

    return f"""<title>{escape(host)} — Site Audit</title>
<style>{theme_css()}{PAGE_CSS}</style>
<script>{theme_script()}</script>
<div class="wrap">

<header class="mast">
  <div class="top">
    <div class="eyebrow"><span class="k">Site audit</span><span class="dash"></span>
      <span class="k">{escape(stamp)}</span>{theme_toggle()}</div>
    <h1><a class="host" href="{escape(base)}" target="_blank" rel="noopener"
      style="color:inherit;text-decoration:none">{escape(host)}</a></h1>
    <p class="lede">Every discoverable page crawled and read, the internal link graph
    rebuilt to find orphans, common endpoints probed, and key templates loaded in a
    real browser. Each finding below lists why it matters, how to fix it, and the
    exact pages it was found on.</p>
  </div>
  <div class="readout">
    {score_cells}
    <div class="cell"><span class="k">Completed</span><div class="v">{escape(short_stamp)}</div></div>
    <div class="cell"><span class="k">Pages analysed</span><div class="v">{n_pages}
      <span style="color:var(--ink-faint)">/ {max(result.discovered, total_urls)}</span></div></div>
    <div class="cell"><span class="k">Findings</span><div class="v">{len(result.findings)}</div></div>
    <div class="cell"><span class="k">Run time</span><div class="v">{result.elapsed_s}s</div></div>
  </div>
</header>

{_score_tiles(score)}
{_gate_callout(score)}
{score_notes}

<section class="summary">
  <h2>Findings by severity</h2>
  {_propbar(counts)}
</section>

<div class="tiles">
  <div class="tile"><div class="n {'crit' if counts.get('critical') else ''}">
    {counts.get('critical', 0) + counts.get('high', 0)}</div>
    <span class="k">Need attention now</span>
    <div class="cap">Critical and high severity</div></div>
  <div class="tile"><div class="n">{sum(f.count for f in result.findings)}</div>
    <span class="k">Page-level issues</span>
    <div class="cap">Across all findings</div></div>
  {orphan_tile}
  {_schema_tile(schema_errors, schema_items)}
  {_image_tile(over_limit, heavy, cfg.image_max_kb, bool(images))}
  <div class="tile"><div class="n ok">{n_pages}</div>
    <span class="k">Pages at HTTP 200</span>
    <div class="cap">of {total_urls} fetched</div></div>
  <div class="tile"><div class="n">{result.elapsed_s}s</div>
    <span class="k">Crawl time</span>
    <div class="cap">{escape(result.method)} discovery</div></div>
</div>

<div class="callout{' warn' if trunc else ''}">
  <p><strong>How this was measured.</strong> Discovery by {escape(result.method)};
  {escape(", ".join(stages))}.</p>
  {trunc}{clean}
</div>

<div class="cols">
  <aside class="rail">
    <div class="box">
      <h2>Jump to</h2>
      <div class="jump">
        {jump_html}
      </div>
      <h2>Findings</h2>
      <div class="filters" id="filters">{filters}</div>
      <ol id="railList">{"".join(rail_items)}</ol>
    </div>
  </aside>

  <main>
    {_seo_section(score)}

    {_performance_section(score, base)}

    {findings_html}

    {mail_section}

    {rep_section}

    {sd_section}

    <section class="sec">
      <div class="sechead"><h2>Crawl coverage</h2>
      <span class="count">{total_urls}</span>
      <p>What was fetched, and how far each page sits from the homepage.</p></div>
      <div class="f" style="border-top:1px solid var(--rule)"><div class="fbody">
        <span class="k">Sources</span>
        <div class="scroll" style="margin:9px 0 22px">
          <table><thead><tr><th>Sitemap or method</th><th class="n">URLs</th>
          <th class="n">Status</th></tr></thead><tbody>{sm_rows}</tbody></table>
        </div>
        <span class="k">Click depth from the homepage</span>
        <div style="margin-top:11px">{_depth_chart(result.graph.get('depth_histogram', {}))}</div>
      </div></div>
    </section>
  </main>
</div>

<div class="foot">
  Generated {escape(stamp)} · {escape(base)} · {total_urls} URLs fetched in
  {result.elapsed_s}s. Response times are single samples taken from one location, not
  field data — confirm against real-user metrics before treating them as a baseline.
  Counts shown against each finding are the number of distinct pages it affects.
  Scored by {escape(score_mod.MODEL)}.
  {_evidence_note(shots, thumbs)}
  {_check_errors(ctx)}
</div>
</div>

<script>
(() => {{
  const active = new Set({list(SEVERITIES)!r});
  const buttons = [...document.querySelectorAll('#filters button')];
  const findings = [...document.querySelectorAll('article.f[data-sev]')];
  const railItems = [...document.querySelectorAll('#railList li')];

  function apply() {{
    findings.forEach(f => f.classList.toggle('hidden', !active.has(f.dataset.sev)));
    railItems.forEach(li => li.hidden = !active.has(li.dataset.sev));
    document.querySelectorAll('section.sec[data-cat]').forEach(sec => {{
      const shown = [...sec.querySelectorAll('article.f')].some(f => !f.classList.contains('hidden'));
      const empty = sec.querySelector('.nores');
      if (empty) empty.hidden = shown;
    }});
  }}

  buttons.forEach(b => b.addEventListener('click', () => {{
    const s = b.dataset.sev;
    if (active.has(s)) {{ active.delete(s); }} else {{ active.add(s); }}
    if (!active.size) {{ buttons.forEach(x => active.add(x.dataset.sev)); }}
    buttons.forEach(x => x.setAttribute('aria-pressed', active.has(x.dataset.sev)));
    apply();
  }}));
  apply();
}})();
</script>"""


def _check_errors(ctx) -> str:
    """Say when a rule did not run. A check that vanishes from a report without
    saying so makes the whole report untrustworthy — the reader cannot tell a
    clean result from a missing one."""
    errors = getattr(ctx, "check_errors", None) or []
    if not errors:
        return ""
    listed = "; ".join(escape(e) for e in errors[:4])
    return (f'<br><strong>{len(errors)} check'
            f'{"s" if len(errors) != 1 else ""} did not run</strong> and are not '
            f'represented above: {listed}. This is a defect in the tool, not a '
            'result for the site.')


def _evidence_note(shots: list, thumbs: dict) -> str:
    """Say what the embedded pictures are, and that they are embedded."""
    if not shots and not thumbs:
        return ""
    embedded = sum(len(s.b64) for s in shots) + sum(
        len(t["b64"]) for t in thumbs.values())
    parts = []
    if shots:
        parts.append(f"{len(shots)} screenshot{'s' if len(shots) != 1 else ''} of the "
                     "findings, taken at 1440px in Chromium")
    if thumbs:
        parts.append(f"{len(thumbs)} image preview{'s' if len(thumbs) != 1 else ''}")
    return (" ".join(parts).capitalize()
            + f" are embedded in this file ({embedded * 3 // 4 / 1048576:.1f} MB), so "
              "it stays readable offline and nothing is fetched when you open it.")


def _clean(result, ctx) -> str:
    fired = {f.id for f in result.findings}
    clean = []
    if "CNT-02" not in fired:
        clean.append("no misspellings matched in published copy")
    if "CNT-04" not in fired:
        clean.append("no character-encoding damage")
    if "ERR-04" not in fired and result.runtime:
        clean.append("no uncaught JavaScript exceptions")
    if "ERR-01" not in fired:
        clean.append("no URLs returning HTTP errors")
    if "ERR-02" not in fired and ctx.link_status:
        clean.append("every internal link resolves")
    if "SEC-01" not in fired:
        clean.append("no exposed configuration or log files")
    if "ORP-01" not in fired and not getattr(result, "truncated", False):
        clean.append("no orphan pages")
    if "IDX-04" not in fired:
        clean.append("every page carries a canonical")
    schema_items = sum(len(r.get("schema_items") or []) for r in ctx.pages)
    if schema_items and not any(f.startswith("SDV-") for f in fired):
        clean.append(f"all {schema_items} structured-data items validate against "
                     "schema.org")
    elif schema_items and "SDV-01" not in fired:
        clean.append("no structured-data block is discarded outright")
    if getattr(result, "images", None) and "MED-06" not in fired:
        clean.append(f"no image exceeds {result.config.image_max_kb} KB")
    if not clean:
        return ""
    return (f'<p><strong>Checked and clean:</strong> {"; ".join(clean)}. '
            'Stated so the scope of the audit is explicit.</p>')
