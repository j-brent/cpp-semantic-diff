#!/usr/bin/env python3
"""Render a cppsd JSON document as a single self-contained HTML file.

Reads the document `cppsd diff --out` produces and emits an HTML view of it.
Every value shown comes from the document; nothing is computed here except the
nesting the `render` stage already describes via `caused_by`, and the layout
widths of the score bars.

    python reports/2026-10-10--up-to-speed/demo-run-a/render_document.py \
        <document.json> <out.html> [display-label]

`display-label` only replaces the page heading. It is needed because the demo
workflow clones into `$RUNNER_TEMP/subject`, so `meta.repo` reads "subject" for
every target it analyzes. The document itself is never modified.

The document is embedded in the output, so the HTML is auditable on its own and
a regenerated file diffs meaningfully against the previous one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CSS = """
:root {
  color-scheme: light;
  --paper: #eceff2; --surface: #ffffff; --raised: #f6f8fa; --sunken: #e3e8ec;
  --ink: #151a1f; --ink-soft: #576269; --ink-faint: #868f98;
  --rule: #d2d8de; --rule-soft: #e4e9ed;
  --base-pt: #44538a; --head-pt: #17706a; --signal: #a85d28;
  --f-blast: #2f6fc4; --f-exposure: #d4652a; --f-novelty: #0f9c76; --f-uncert: #c9971b;
  --display: "Spectral", "Iowan Old Style", Georgia, serif;
  --body: "IBM Plex Sans", "Segoe UI", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "SF Mono", Menlo, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --paper: #11151a; --surface: #191e24; --raised: #20262d; --sunken: #0d1115;
    --ink: #e4e9ee; --ink-soft: #99a3ad; --ink-faint: #6d7780;
    --rule: #2b3239; --rule-soft: #222930;
    --base-pt: #8d9cd2; --head-pt: #49b3a9; --signal: #dc8e52;
    --f-blast: #5b94e0; --f-exposure: #d4733c; --f-novelty: #1fa87f; --f-uncert: #b0871f;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --paper: #11151a; --surface: #191e24; --raised: #20262d; --sunken: #0d1115;
  --ink: #e4e9ee; --ink-soft: #99a3ad; --ink-faint: #6d7780;
  --rule: #2b3239; --rule-soft: #222930;
  --base-pt: #8d9cd2; --head-pt: #49b3a9; --signal: #dc8e52;
  --f-blast: #5b94e0; --f-exposure: #d4733c; --f-novelty: #1fa87f; --f-uncert: #b0871f;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; scroll-padding-top: 20px; }
body { margin: 0; background: var(--paper); color: var(--ink); font-family: var(--body);
       font-size: 15px; line-height: 1.55; -webkit-font-smoothing: antialiased; }

/* ---- shell: contents rail + content ------------------------------------- */

.shell { max-width: 1340px; margin: 0 auto; padding: 0 26px 90px;
         display: grid; grid-template-columns: 216px minmax(0, 1fr); gap: 40px; align-items: \
start; }

nav.toc { position: sticky; top: 18px; max-height: calc(100vh - 36px); overflow-y: auto;
          padding: 18px 0 10px; font-size: .84rem; }
nav.toc .tochead { font-family: var(--mono); font-size: .66rem; letter-spacing: .1em;
                   text-transform: uppercase; color: var(--ink-faint); margin-bottom: 12px; }
nav.toc ol { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: \
1px; }
nav.toc a { display: flex; justify-content: space-between; gap: 10px; align-items: baseline;
            text-decoration: none; color: var(--ink-soft); padding: 6px 10px;
            border-left: 2px solid transparent; border-radius: 0 4px 4px 0; }
nav.toc a:hover { background: var(--surface); color: var(--ink); }
nav.toc a:focus-visible { outline: 2px solid var(--signal); outline-offset: -2px; }
nav.toc a.here { background: var(--surface); color: var(--ink); border-left-color: var(--signal); \
font-weight: 500; }
nav.toc a .c { font-family: var(--mono); font-size: .72rem; color: var(--ink-faint);
               font-variant-numeric: tabular-nums; white-space: nowrap; }
nav.toc .tocfoot { margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--rule);
                   display: flex; flex-direction: column; gap: 7px; }

main { min-width: 0; }

h1 { font-family: var(--display); font-weight: 600; margin: 0; line-height: 1.2; text-wrap: balance;
     font-size: clamp(1.7rem, 3.4vw, 2.3rem); letter-spacing: -.015em; }
p { margin: 0; max-width: 74ch; }
code { font-family: var(--mono); font-size: .86em; background: var(--sunken);
       padding: .1em .34em; border-radius: 3px; }

.field { font-family: var(--mono); font-size: .69rem; letter-spacing: .09em;
         text-transform: uppercase; color: var(--ink-faint); font-weight: 500; }
.note { font-size: .84rem; color: var(--ink-soft); }

header.top { padding: 44px 0 0; }
header.top h1 { margin-top: 10px; }
.idline { display: flex; flex-wrap: wrap; gap: 7px 14px; align-items: baseline; margin-top: 18px;
          padding-top: 16px; border-top: 1px solid var(--rule); font-family: var(--mono);
          font-size: .78rem; color: var(--ink-soft); }
.idline b { color: var(--ink); font-weight: 500; }
.idline .sep { color: var(--ink-faint); }

/* ---- collapsible section ------------------------------------------------ */

details.sec { margin-top: 26px; scroll-margin-top: 16px; }
details.sec > summary { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap;
                        cursor: pointer; padding: 10px 0; border-bottom: 1px solid var(--rule);
                        list-style: none; }
details.sec > summary::-webkit-details-marker { display: none; }
details.sec > summary::before { content: "+"; font-family: var(--mono); font-size: .9rem;
                                color: var(--ink-faint); width: 11px; flex: none; }
details.sec[open] > summary::before { content: "\\2013"; }
details.sec > summary:hover::before { color: var(--signal); }
details.sec > summary:focus-visible { outline: 2px solid var(--signal); outline-offset: 2px; }
details.sec > summary .sc-count { font-family: var(--mono); font-size: .74rem;
                                  color: var(--ink-faint); margin-left: auto; }
.secbody { padding-top: 18px; }

.card { background: var(--surface); border: 1px solid var(--rule); border-radius: 6px; }
.pad { padding: 20px 22px; }

.kv { display: grid; grid-template-columns: auto 1fr; gap: 7px 18px; margin: 0; font-size: .86rem; }
.kv dt { font-family: var(--mono); font-size: .78rem; color: var(--ink-soft); white-space: nowrap; }
.kv dd { margin: 0; font-family: var(--mono); font-variant-numeric: tabular-nums; word-break: \
break-word; }

.sides { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.side { background: var(--surface); border: 1px solid var(--rule); border-radius: 6px; padding: \
18px 20px; }
.side > .tag { font-family: var(--mono); font-size: .7rem; letter-spacing: .11em;
               text-transform: uppercase; display: block; margin-bottom: 12px; }
.side.base > .tag { color: var(--base-pt); }
.side.head > .tag { color: var(--head-pt); }

.strings { display: flex; flex-direction: column; gap: 9px; margin: 0; padding: 0; list-style: \
none; }
.strings li { font-size: .855rem; color: var(--ink-soft); padding-left: 15px; position: relative; \
max-width: 86ch; }
.strings li::before { content: "\\00b7"; position: absolute; left: 2px; color: var(--ink-faint); }

/* ---- tables ------------------------------------------------------------- */

.tablewrap { overflow: auto; max-height: 70vh; }
table { border-collapse: collapse; width: 100%; font-size: .85rem; }
th, td { text-align: left; padding: 8px 14px; border-bottom: 1px solid var(--rule-soft); }
thead th { position: sticky; top: 0; z-index: 1;
           font-family: var(--mono); font-size: .67rem; letter-spacing: .09em; text-transform: \
uppercase;
           color: var(--ink-faint); font-weight: 500; background: var(--raised);
           border-bottom: 1px solid var(--rule); white-space: nowrap; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover { background: var(--raised); }
td.n { font-family: var(--mono); font-variant-numeric: tabular-nums; text-align: right; \
white-space: nowrap; }
td.m { font-family: var(--mono); font-size: .8rem; }
td.sub { font-family: var(--mono); font-size: .78rem; word-break: break-word; }

/* ---- controls ----------------------------------------------------------- */

.toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 14px; }
button.ctl { font-family: var(--mono); font-size: .74rem; background: var(--surface); color: \
var(--ink);
             border: 1px solid var(--rule); border-radius: 5px; padding: 6px 11px; cursor: \
pointer; }
button.ctl:hover { border-color: var(--ink-faint); }
button.ctl[aria-pressed="true"] { background: var(--ink); color: var(--paper); border-color: \
var(--ink); }
button.ctl:focus-visible { outline: 2px solid var(--signal); outline-offset: 2px; }

.selfcheck { font-family: var(--mono); font-size: .74rem; color: var(--ink-soft); margin-bottom: \
12px; }
.selfcheck.bad { color: var(--signal); font-weight: 500; }

/* ---- components --------------------------------------------------------- */

details.comp { background: var(--surface); border: 1px solid var(--rule); border-radius: 6px;
               margin-bottom: 9px; overflow: hidden; }
details.comp > summary { display: grid; grid-template-columns: 13px 86px minmax(0, 1fr) auto; gap: \
14px;
                         align-items: baseline; padding: 11px 16px; cursor: pointer;
                         background: var(--raised); list-style: none;
                         font-family: var(--mono); font-size: .78rem; color: var(--ink-soft); }
details.comp > summary::-webkit-details-marker { display: none; }
details.comp[open] > summary { border-bottom: 1px solid var(--rule); }
details.comp > summary:hover { color: var(--ink); }
details.comp > summary:focus-visible { outline: 2px solid var(--signal); outline-offset: -2px; }
details.comp > summary .tw { color: var(--ink-faint); }
details.comp[open] > summary .tw::before { content: "\\2013"; }
details.comp:not([open]) > summary .tw::before { content: "+"; }
details.comp > summary .top { color: var(--ink); overflow: hidden; text-overflow: ellipsis;
                              white-space: nowrap; min-width: 0; }
details.comp > summary .sz { font-variant-numeric: tabular-nums; white-space: nowrap; }

.ev { display: grid; grid-template-columns: 74px 136px minmax(0, 1fr); gap: 0 14px;
      padding: 12px 16px; border-bottom: 1px solid var(--rule-soft); align-items: start; }
.ev:last-child { border-bottom: none; }
.ev.cons { background: var(--raised); }
.ev.d1 { padding-left: 38px; } .ev.d2 { padding-left: 60px; } .ev.d3 { padding-left: 82px; }
.ev.d4 { padding-left: 104px; } .ev.d5 { padding-left: 126px; }
.ev.hidden { display: none; }

.sc { font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: .88rem; \
font-weight: 500; }
.sc .imp { display: block; font-size: .7rem; color: var(--ink-soft); font-weight: 400; }
.bar { height: 6px; background: var(--sunken); border-radius: 3px; overflow: hidden; margin-top: \
6px; }
.bar > i { display: block; height: 100%; background: var(--signal); border-radius: 0 3px 3px 0; }
.ev.cons .bar > i { background: var(--ink-faint); }

.kind { font-family: var(--mono); font-size: .74rem; padding-top: 2px; color: var(--ink); }
.ev.cons .kind { color: var(--ink-soft); }

.subj { font-family: var(--mono); font-size: .83rem; line-height: 1.5; word-break: break-word; }
.subj .rel { color: var(--signal); }
.usr { font-family: var(--mono); font-size: .71rem; color: var(--ink-faint); word-break: break-all;
       margin-top: 4px; display: block; }
.attrs { display: flex; flex-wrap: wrap; gap: 3px 14px; margin-top: 6px; font-size: .75rem;
         color: var(--ink-soft); font-family: var(--mono); }
.factors { display: grid; grid-template-columns: repeat(auto-fit, minmax(188px, 1fr)); gap: 6px \
16px; margin-top: 9px; }
.fac { font-size: .73rem; font-family: var(--mono); color: var(--ink-soft); display: flex; gap: \
7px; align-items: baseline; }
.fac i { width: 9px; height: 9px; border-radius: 2px; flex: none; transform: translateY(1px); }
.fac b { color: var(--ink); font-weight: 500; }
.fac-blast i { background: var(--f-blast); }
.fac-exposure i { background: var(--f-exposure); }
.fac-novelty i { background: var(--f-novelty); }
.fac-uncertainty i { background: var(--f-uncert); }

.stack { display: flex; gap: 2px; height: 10px; margin-top: 8px; max-width: 520px; }
.stack > span { border-radius: 2px; min-width: 2px; }
.stack .rest { background: var(--sunken); }
.st-blast { background: var(--f-blast); } .st-exposure { background: var(--f-exposure); }
.st-novelty { background: var(--f-novelty); } .st-uncertainty { background: var(--f-uncert); }

details.raw { margin-top: 9px; }
details.raw summary { font-family: var(--mono); font-size: .71rem; color: var(--ink-faint);
                      cursor: pointer; width: max-content; }
details.raw summary:focus-visible { outline: 2px solid var(--signal); outline-offset: 2px; }
pre { font-family: var(--mono); font-size: .73rem; line-height: 1.5; background: var(--sunken);
      padding: 12px 14px; border-radius: 5px; overflow: auto; max-height: 60vh; margin: 8px 0 0; \
color: var(--ink); }

.legend { display: flex; flex-wrap: wrap; gap: 8px 20px; margin-bottom: 14px; font-size: .78rem; \
color: var(--ink-soft); }
.legend .lg { display: flex; gap: 7px; align-items: baseline; font-family: var(--mono); }
.legend .lg i { width: 10px; height: 10px; border-radius: 2px; flex: none; transform: \
translateY(1px); }

.empty { font-family: var(--mono); font-size: .8rem; color: var(--ink-faint); }

footer { margin-top: 44px; padding-top: 18px; border-top: 1px solid var(--rule);
         font-family: var(--mono); font-size: .72rem; color: var(--ink-faint);
         display: flex; flex-wrap: wrap; gap: 5px 16px; }

/* ---- narrow ------------------------------------------------------------- */

@media (max-width: 1020px) {
  .shell { grid-template-columns: minmax(0, 1fr); gap: 0; }
  nav.toc { top: 0; z-index: 5; max-height: none;
            background: var(--paper); border-bottom: 1px solid var(--rule);
            margin: 0 -26px; padding: 0 26px; }
  nav.toc .tochead { display: none; }
  nav.toc ol { flex-direction: row; overflow-x: auto; gap: 0; padding: 7px 0; }
  nav.toc a { border-left: none; border-bottom: 2px solid transparent; border-radius: 0;
              white-space: nowrap; padding: 5px 11px; }
  nav.toc a.here { border-left: none; border-bottom-color: var(--signal); background: transparent; }
  nav.toc a .c { display: none; }
  nav.toc .tocfoot { flex-direction: row; border-top: none; margin: 0 0 8px; padding: 0; }
  header.top { padding-top: 26px; }
}
@media (max-width: 760px) {
  .sides { grid-template-columns: 1fr; }
  .ev { grid-template-columns: 68px minmax(0, 1fr); }
  .ev .kind { grid-column: 2; margin-top: 2px; }
  details.comp > summary { grid-template-columns: 13px minmax(0, 1fr); }
  details.comp > summary .sz { grid-column: 2; }
}
@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  * { transition: none !important; animation: none !important; }
}
"""

JS = """
const DOC = JSON.parse(document.getElementById("document-json").textContent);

const FACTOR_ORDER = ["blast_radius", "exposure", "novelty", "uncertainty"];
const FACTOR_CLASS = { blast_radius: "blast", exposure: "exposure", novelty: "novelty", \
uncertainty: "uncertainty" };

const entities = DOC.entities || {};
const scores = Object.fromEntries(
  Array.isArray(DOC.scores) ? DOC.scores : Object.entries(DOC.scores || {})
);
const events = Object.fromEntries((DOC.events || []).map(e => [e.id, e]));
const rank = Object.fromEntries((DOC.ranking || []).map((id, i) => [id, i]));

const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': \
"&quot;" }[c]));
const num = (v, d = 4) => (v === null || v === undefined ? "null" : Number(v).toFixed(d));

function entityName(usr) {
  const e = entities[usr];
  if (!e) return null;
  return e.qualified_name || e.name || null;
}

/* an edge subject is `src|kind|dst`; a node subject is a bare USR */
function subjectParts(subject) {
  const bits = subject.split("|");
  if (bits.length === 3) return { edge: true, src: bits[0], rel: bits[1], dst: bits[2] };
  return { edge: false, usr: subject };
}

function plainSubject(subject) {
  const p = subjectParts(subject);
  return p.edge
    ? `${entityName(p.src) || p.src} ${p.rel} ${entityName(p.dst) || p.dst}`
    : (entityName(p.usr) || p.usr);
}

function renderSubject(subject) {
  const p = subjectParts(subject);
  if (!p.edge) {
    const nm = entityName(p.usr);
    return `<div class="subj">${esc(nm || p.usr)}</div><span class="usr">${esc(p.usr)}</span>`;
  }
  const s = entityName(p.src), d = entityName(p.dst);
  return `<div class="subj">${esc(s || p.src)} <span class="rel">${esc(p.rel)}</span> ${esc(d || \
p.dst)}</div>` +
         `<span class="usr">${esc(p.src)}</span><span class="usr">${esc(p.dst)}</span>`;
}

function renderEvent(id, depth, nKids) {
  const isCons = depth > 0;
  const ev = events[id], sc = scores[id] || {};
  const byName = Object.fromEntries((sc.factors || []).map(f => [f.name, f]));

  const stack = FACTOR_ORDER.map(n => {
    const f = byName[n];
    if (!f || f.contribution <= 0) return "";
    return `<span class="st-${FACTOR_CLASS[n]}" style="flex:0 0 ${(f.contribution * \
100).toFixed(2)}%"></span>`;
  }).join("");
  const rest = 1 - (sc.total || 0);

  const facRows = FACTOR_ORDER.map(n => {
    const f = byName[n];
    if (!f) return "";
    return `<span class="fac fac-${FACTOR_CLASS[n]}"><i></i><span>${esc(n)} <b>${num(f.value, \
3)}</b>` +
           ` ×${num(f.weight, 2)} = <b>${num(f.contribution)}</b> · ${esc(f.detail)}</span></span>`;
  }).join("");

  const prov = (ev.provenance || []).map(p =>
    p.location ? `${p.side} ${p.location.file}:${p.location.line}` : `${p.side}`).join(" · ");

  const attrs = [
    sc.exposure ? `exposure ${esc(sc.exposure)}` : "",
    `component ${sc.component}`,
    prov ? `provenance ${esc(prov)}` : "provenance none",
    `confidence tier ${ev.confidence ? esc(String(ev.confidence.tier)) : "–"}, ` +
      `models_complete ${ev.confidence ? ev.confidence.models_complete : "–"}`,
    `bounded_by ${esc(String(sc.bounded_by))}`,
    ev.caused_by ? `caused_by ${esc(ev.caused_by)}` : "",
    Object.keys(ev.payload || {}).length ? `payload ${esc(JSON.stringify(ev.payload))}` : "",
  ].filter(Boolean).map(t => `<span>${t}</span>`).join("");

  return `<div class="ev${isCons ? ` cons d${Math.min(depth, 5)}` : ""}" data-cons="${isCons}">
    <div class="sc">${num(sc.total, 4)}<span class="imp">impact ${num(sc.impact, 3)}</span>
      <span class="bar"><i style="width:${((sc.total || 0) * 100).toFixed(1)}%"></i></span></div>
    <div class="kind">${isCons ? "└ " : ""}${esc(ev.kind)}</div>
    <div>
      ${renderSubject(ev.subject)}
      <div class="attrs">${attrs}</div>
      <div class="stack">${stack}<span class="rest" style="flex:0 0 ${(rest * \
100).toFixed(2)}%"></span></div>
      <div class="factors">${facRows}</div>
      ${nKids ? `<div class="attrs"><span>${nKids} event${nKids > 1 ? "s" : ""} list this as \
caused_by</span></div>` : ""}
      <details class="raw"><summary>events[] · scores[] entry</summary><pre>${
        esc(JSON.stringify({ event: ev, score: sc }, null, 2))}</pre></details>
    </div>
  </div>`;
}

function renderComponents() {
  const host = document.getElementById("components");
  const comps = DOC.components || [];
  let rendered = 0;

  host.innerHTML = comps.map(c => {
    const ids = [...c.events].sort((a, b) => rank[a] - rank[b]);
    const inComp = new Set(ids);

    const kids = {};
    for (const id of ids) {
      const parent = events[id].caused_by;
      if (parent && inComp.has(parent)) (kids[parent] = kids[parent] || []).push(id);
    }
    /* a root has no cause, or a cause sitting in another component */
    const roots = ids.filter(id => {
      const p = events[id].caused_by;
      return !p || !inComp.has(p);
    });

    /* descend to any depth; `seen` guards a cycle the data should not contain */
    const seen = new Set();
    const walk = (id, depth) => {
      if (seen.has(id)) return "";
      seen.add(id);
      rendered += 1;
      const ks = kids[id] || [];
      return renderEvent(id, depth, ks.length) + ks.map(k => walk(k, depth + 1)).join("");
    };
    const rows = roots.map(r => walk(r, 0)).join("") +
      ids.filter(id => !seen.has(id)).map(id => walk(id, 1)).join("");

    const lead = ids[0];

    return `<details class="comp">
      <summary>
        <span class="tw"></span>
        <span>index <b style="color:var(--ink)">${c.index}</b></span>
        <span class="top">${esc(events[lead].kind)} · \
${esc(plainSubject(events[lead].subject))}</span>
        <span class="sz">${c.events.length} ev · ${roots.length} top · top_score \
${num(c.top_score, 3)}</span>
      </summary>
      <div class="compbody">${rows}</div>
    </details>`;
  }).join("");

  const total = (DOC.events || []).length;
  const el = document.getElementById("selfcheck");
  el.textContent = `${rendered} of ${total} events[] rendered below` +
    (rendered === total ? "" : ` — ${total - rendered} NOT SHOWN`);
  el.className = rendered === total ? "selfcheck" : "selfcheck bad";
}

function renderMeta() {
  const m = DOC.meta || {};
  document.getElementById("idline").innerHTML = [
    `<b>${esc(m.repo)}</b>`,
    `<span>${esc(m.base_ref)}…${esc(m.head_ref)}</span>`,
    `<span>base <b>${esc((m.base_rev || "").slice(0, 12))}</b> → head <b>${esc((m.head_rev || \
"").slice(0, 12))}</b></span>`,
    `<span>${esc(m.tool)} ${esc(m.tool_version)}</span>`,
    `<span>schema ${esc(m.schema_version)}</span>`,
  ].join('<span class="sep">·</span>');

  document.getElementById("timings").innerHTML = Object.entries(m.timings || {})
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${num(v, 3)} s</dd>`).join("");
}

function sideBlock(side) {
  const c = (DOC.completeness || {})[side] || {};
  const rows = [
    ["rev", (c.rev || "").slice(0, 12)],
    ["tus_total", c.tus_total], ["tus_parsed", c.tus_parsed],
    ["tus_failed", c.tus_failed], ["tus_cached", c.tus_cached],
    ["tus_complete", c.tus_complete],
    ["edges_total", (c.edges_total || 0).toLocaleString()],
    ["edges_dropped", (c.edges_dropped || 0).toLocaleString()],
    ["build_available", c.build_available],
    ["producer_version", c.producer_version],
    ["language_modes", (c.language_modes || []).length ? c.language_modes.join(", ") : "[]"],
  ];
  const notes = (c.notes || []).map(n => `<li>${esc(n)}</li>`).join("");
  const fails = (c.failures || []).map(f => `<li>${esc(JSON.stringify(f))}</li>`).join("");
  return `<div class="side ${side}">
    <span class="tag">completeness.${side}</span>
    <dl class="kv">${rows.map(([k, v]) => \
`<dt>${esc(k)}</dt><dd>${esc(String(v))}</dd>`).join("")}</dl>
    ${notes ? `<div class="field" style="margin:16px 0 8px">notes</div><ul \
class="strings">${notes}</ul>` : ""}
    ${fails ? `<div class="field" style="margin:16px 0 8px">failures</div><ul \
class="strings">${fails}</ul>` : ""}
  </div>`;
}

function renderCompleteness() {
  document.getElementById("sides").innerHTML = sideBlock("base") + sideBlock("head");
  const lim = ((DOC.completeness || {}).limits || []).map(l => `<li>${esc(l)}</li>`).join("");
  document.getElementById("limits").innerHTML = lim || '<li class="empty">[]</li>';
}

function renderSummary() {
  const s = DOC.summary || {};
  document.getElementById("summaryKv").innerHTML = Object.entries(s)
    .filter(([k]) => k !== "residue")
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(String(v))}</dd>`).join("");
  document.getElementById("residueKv").innerHTML = Object.entries(s.residue || {})
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(Array.isArray(v) ? v.join(", ") : \
String(v))}</dd>`).join("");
}

function renderFindings() {
  const f = DOC.findings || [];
  document.getElementById("findings").innerHTML = f.length
    ? `<pre>${esc(JSON.stringify(f, null, 2))}</pre>`
    : '<p class="empty">findings: [] — the document carries no findings for this comparison.</p>';
}

function renderRanking() {
  document.getElementById("rankingBody").innerHTML = (DOC.ranking || []).map((id, i) => {
    const ev = events[id], sc = scores[id] || {};
    return `<tr>
      <td class="n">${i + 1}</td>
      <td class="n">${num(sc.total, 4)}</td>
      <td class="n">${num(sc.impact, 3)}</td>
      <td class="m">${esc(ev.kind)}</td>
      <td class="m">${esc(sc.exposure || "")}</td>
      <td class="n">${sc.component}</td>
      <td class="m">${ev.caused_by ? "yes" : ""}</td>
      <td class="sub">${esc(plainSubject(ev.subject))}</td>
    </tr>`;
  }).join("");
}

/* ---- contents counts ---------------------------------------------------- */

function renderTocCounts() {
  const s = DOC.summary || {};
  const set = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = \
text; };
  set("c-meta", `${Object.keys((DOC.meta || {}).timings || {}).length} fields`);
  set("c-completeness", "2 sides");
  set("c-summary", `${Object.keys(s).length} fields`);
  set("c-components", `${(DOC.components || []).length} / ${(DOC.events || []).length} ev`);
  set("c-ranking", `${(DOC.ranking || []).length} rows`);
  set("c-findings", `${(DOC.findings || []).length}`);
  set("c-raw", `${Math.round(JSON.stringify(DOC).length / 1024)} KB`);
}

/* ---- toggles ------------------------------------------------------------ */

function wireToggle(btnId, labels, apply) {
  const btn = document.getElementById(btnId);
  if (!btn) return;
  let on = false;
  btn.addEventListener("click", () => {
    on = !on;
    btn.setAttribute("aria-pressed", String(on));
    btn.textContent = on ? labels[1] : labels[0];
    apply(on);
  });
}

wireToggle("expandComps", ["expand all components", "collapse all components"], on => {
  document.querySelectorAll("details.comp").forEach(d => { d.open = on; });
});

wireToggle("toggleRaw", ["per-event JSON: closed", "per-event JSON: open"], on => {
  document.querySelectorAll("details.raw").forEach(d => { d.open = on; });
});

/* consequences start shown; the button hides them, as the terminal does by default */
(() => {
  const btn = document.getElementById("toggleCons");
  let shown = true;
  btn.addEventListener("click", () => {
    shown = !shown;
    btn.setAttribute("aria-pressed", String(shown));
    btn.textContent = shown ? "caused_by rows: shown" : "caused_by rows: hidden";
    document.querySelectorAll('.ev[data-cons="true"]').forEach(el => el.classList.toggle("hidden", \
!shown));
  });
})();

(() => {
  const btn = document.getElementById("secAll");
  btn.addEventListener("click", () => {
    const open = btn.getAttribute("aria-pressed") !== "true";
    btn.setAttribute("aria-pressed", String(open));
    btn.textContent = open ? "collapse all sections" : "expand all sections";
    document.querySelectorAll("details.sec").forEach(d => { d.open = open; });
  });
})();

/* ---- which section am I in --------------------------------------------- */

function wireScrollSpy() {
  const links = [...document.querySelectorAll("nav.toc a[data-sec]")];
  const secs = links.map(a => document.getElementById(a.dataset.sec)).filter(Boolean);
  if (!("IntersectionObserver" in window) || !secs.length) return;

  const mark = id => links.forEach(a => a.classList.toggle("here", a.dataset.sec === id));
  const io = new IntersectionObserver(entries => {
    const visible = entries.filter(e => e.isIntersecting)
      .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
    if (visible.length) mark(visible[0].target.id);
  }, { rootMargin: "-8% 0px -70% 0px", threshold: 0 });
  secs.forEach(s => io.observe(s));
  mark(secs[0].id);
}

renderMeta();
renderCompleteness();
renderSummary();
renderComponents();
renderRanking();
renderFindings();
renderTocCounts();
wireScrollSpy();
document.getElementById("fullJson").textContent = JSON.stringify(DOC, null, 2);
"""  # noqa: RUF001 -- the multiplication sign and en dash are shown on the page on purpose

TOC = [
    ("sec-meta", "meta", "c-meta"),
    ("sec-completeness", "completeness", "c-completeness"),
    ("sec-summary", "summary", "c-summary"),
    ("sec-components", "components", "c-components"),
    ("sec-ranking", "ranking", "c-ranking"),
    ("sec-findings", "findings", "c-findings"),
    ("sec-raw", "the document", "c-raw"),
]


def build(doc: dict, label: str | None = None) -> str:
    meta = doc.get("meta", {})
    name = label or meta.get("repo", "document")
    title = f"{name} {meta.get('base_ref', '')}…{meta.get('head_ref', '')}"
    # guard the one sequence that would close the host <script> element early
    embedded = json.dumps(doc, indent=2, sort_keys=False).replace("</", "<\\/")

    toc = "\n".join(
        f'          <li><a href="#{sid}" data-sec="{sid}">{text}'
        f'<span class="c" id="{cid}"></span></a></li>'
        for sid, text, cid in TOC
    )

    note = ""
    if label and label != meta.get("repo"):
        note = (
            f'      <p class="note" style="margin-top:10px">meta.repo in this document reads '
            f"<code>{meta.get('repo', '')}</code>; the heading uses the display label "
            f"<code>{name}</code>. Nothing else is overridden.</p>\n"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · cppsd document</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" \
href="https://fonts.googleapis.com/css2?family=Spectral:wght@600&family=IBM+Plex+Sans:wght@400;500&\
family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{CSS}</style>
</head>
<body>
<div class="shell">

  <nav class="toc" aria-label="Contents">
    <div class="tochead">contents</div>
        <ol>
{toc}
        </ol>
    <div class="tocfoot">
      <button class="ctl" id="secAll" aria-pressed="true">collapse all sections</button>
    </div>
  </nav>

  <main>
    <header class="top">
      <div class="field">cppsd json document · schema {meta.get("schema_version", "")}</div>
      <h1>{title}</h1>
{note}      <div class="idline" id="idline"></div>
    </header>

    <details class="sec" id="sec-meta" open>
      <summary><span class="field">meta.timings</span><span class="sc-count">stage \
seconds</span></summary>
      <div class="secbody"><div class="card pad"><dl class="kv" id="timings"></dl></div></div>
    </details>

    <details class="sec" id="sec-completeness" open>
      <summary><span class="field">completeness</span><span class="sc-count">base · head · \
limits</span></summary>
      <div class="secbody">
        <div class="sides" id="sides"></div>
        <div class="card pad" style="margin-top:14px">
          <div class="field" style="margin-bottom:10px">completeness.limits</div>
          <ul class="strings" id="limits"></ul>
        </div>
      </div>
    </details>

    <details class="sec" id="sec-summary" open>
      <summary><span class="field">summary</span><span class="sc-count">+ \
summary.residue</span></summary>
      <div class="secbody">
        <div class="sides">
          <div class="card pad"><dl class="kv" id="summaryKv"></dl></div>
          <div class="card pad">
            <div class="field" style="margin-bottom:10px">summary.residue</div>
            <dl class="kv" id="residueKv"></dl>
          </div>
        </div>
      </div>
    </details>

    <details class="sec" id="sec-components" open>
      <summary><span class="field">components · events · scores</span>
        <span class="sc-count">one row per component — open one to read its events</span></summary>
      <div class="secbody">
        <div class="toolbar">
          <button class="ctl" id="expandComps" aria-pressed="false">expand all components</button>
          <button class="ctl" id="toggleCons" aria-pressed="true">caused_by rows: shown</button>
          <button class="ctl" id="toggleRaw" aria-pressed="false">per-event JSON: closed</button>
        </div>
        <div class="legend">
          <span class="lg"><i style="background:var(--f-blast)"></i>blast_radius ×0.35</span>
          <span class="lg"><i style="background:var(--f-exposure)"></i>exposure ×0.25</span>
          <span class="lg"><i style="background:var(--f-novelty)"></i>novelty ×0.25</span>
          <span class="lg"><i style="background:var(--f-uncert)"></i>uncertainty ×0.15</span>
          <span class="lg"><i style="background:var(--sunken);border:1px solid \
var(--rule)"></i>unscored remainder</span>
        </div>
        <div class="selfcheck" id="selfcheck"></div>
        <div id="components"></div>
      </div>
    </details>

    <details class="sec" id="sec-ranking" open>
      <summary><span class="field">ranking</span><span class="sc-count">the document's own \
order</span></summary>
      <div class="secbody">
        <div class="card tablewrap">
          <table>
            <thead><tr>
              <th>#</th><th>total</th><th>impact</th><th>kind</th>
              <th>exposure</th><th>comp</th><th>caused_by</th><th>subject</th>
            </tr></thead>
            <tbody id="rankingBody"></tbody>
          </table>
        </div>
      </div>
    </details>

    <details class="sec" id="sec-findings" open>
      <summary><span class="field">findings</span><span class="sc-count"></span></summary>
      <div class="secbody"><div class="card pad" id="findings"></div></div>
    </details>

    <details class="sec" id="sec-raw">
      <summary><span class="field">the document</span><span class="sc-count">embedded \
source</span></summary>
      <div class="secbody"><div class="card pad"><pre id="fullJson"></pre></div></div>
    </details>

    <footer>
      <span>{meta.get("tool", "")} {meta.get("tool_version", "")}</span>
      <span>schema {meta.get("schema_version", "")}</span>
      <span>{meta.get("base_rev", "")[:12]} → {meta.get("head_rev", "")[:12]}</span>
      <span>every value on this page is read from the embedded document</span>
    </footer>
  </main>
</div>

<script type="application/json" id="document-json">{embedded}</script>
<script>{JS}</script>
</body>
</html>
"""  # noqa: RUF001 -- the multiplication sign and en dash are shown on the page on purpose


def main() -> int:
    if len(sys.argv) not in (3, 4):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    label = sys.argv[3] if len(sys.argv) == 4 else None
    doc = json.loads(src.read_text(encoding="utf-8"))
    dst.write_text(build(doc, label), encoding="utf-8")
    print(f"{src} -> {dst}  ({dst.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
