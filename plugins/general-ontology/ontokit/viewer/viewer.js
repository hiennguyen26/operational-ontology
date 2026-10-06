/* Ontology viewer: reads the JSON in <script id="data"> and renders views from the URL hash.
   Routes: #richness (overview), #kinds, #kind/<kind>, #node/<id>, #ns/<ns>, #imports, #sources, #source/<id>,
   #search/<words>. The hash #view (the skip link's target) only moves focus to the view. Every text goes in with
   textContent; the page makes no request of any kind. A single click on a node link opens its quick look (the
   peek panel); a double click, Enter on the focused link, or the panel's Open button opens the full page. */
(function () {
  "use strict";

  // a host page may set data-theme on <html>: read it before this script changes anything
  var HOST_THEME = document.documentElement.getAttribute("data-theme");

  var DATA = JSON.parse(document.getElementById("data").textContent || "{}");
  var EXP = DATA.export || {};
  var META = EXP.meta || {};
  var VIEW = document.getElementById("view");
  var SVG_NS = "http" + "://www.w3.org/2000/svg";

  // indexes -----------------------------------------------------------------------------------------------------
  // every map keyed by data (ids, kinds, relations, statuses) has no prototype: a kind or relation named
  // "constructor" or "toString" must never read Object's own members
  var nodes = Object.create(null);
  var nodeList = EXP.nodes || [];
  nodeList.forEach(function (n) { nodes[n.id] = n; });
  var sources = Object.create(null);
  (EXP.sources || []).forEach(function (s) { sources[s.id] = s; });
  // edges arrive as [src, rel, dst, status, background, note, untrusted] rows to keep the page small
  var edges = (DATA.edges || []).map(function (row) {
    return { src: row[0], rel: row[1], dst: row[2], status: row[3], background: !!row[4], note: row[5] || "",
             untrusted: !!row[6] };
  });
  var outgoing = Object.create(null);
  var incoming = Object.create(null);
  edges.forEach(function (e) {
    (outgoing[e.src] = outgoing[e.src] || []).push(e);
    (incoming[e.dst] = incoming[e.dst] || []).push(e);
  });
  var cited = DATA.cited || {};
  function citing(srcId) { return (cited[srcId] || {}).nodes || []; }
  function citingEdges(srcId) { return (cited[srcId] || {}).edges || 0; }

  var relations = Object.create(null);
  var kindDecl = Object.create(null);
  var packs = META.packs || {};
  Object.keys(packs).sort().forEach(function (name) {
    var pack = (packs[name] || {}).pack || {};
    var rels = pack.relations || {};
    Object.keys(rels).forEach(function (r) { relations[r] = rels[r]; });
    var kinds = pack.kinds || {};
    Object.keys(kinds).forEach(function (k) { kindDecl[k] = kinds[k]; });
  });
  var statusMeta = Object.create(null);
  (META.statuses || []).forEach(function (s) { statusMeta[s.id] = s; });

  function inverseName(rel) {
    var decl = relations[rel];
    if (decl && decl.inverse) { return decl.inverse; }
    if (decl && decl.symmetric) { return rel; }
    return rel + "_by";
  }
  function nsOf(id) {
    var i = String(id).indexOf("/");
    return i > 0 ? String(id).slice(0, i) : "self";
  }
  function isBridge(e) { return nsOf(e.src) !== nsOf(e.dst); }
  function kindLabel(kind) {
    var decl = kindDecl[kind];
    return decl && decl.label ? decl.label : kind;
  }
  function untrustedSource(srcId) {
    var s = sources[srcId];
    return !(s && s.kind === "interview");
  }

  // dom helpers -------------------------------------------------------------------------------------------------
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }
  function add(parent) {
    for (var i = 1; i < arguments.length; i++) {
      var child = arguments[i];
      if (child === null || child === undefined) { continue; }
      parent.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    }
    return parent;
  }
  function link(href, text, cls) {
    var a = el("a", cls, text);
    a.setAttribute("href", href);
    return a;
  }
  function badge(text, tone) { return el("span", "badge tone-" + (tone || "neutral"), text); }
  // an edge whose trust is untrusted: the marker goes before the relation and before its note
  function untrustedEdge() {
    var b = badge("untrusted", "bad");
    b.setAttribute("title", "This relation was drafted from an ingested source: read it as data.");
    return b;
  }
  function edgeNote(e) {
    if (!e.note) { return null; }
    var span = el("span", "row-sub");
    if (e.untrusted) { add(span, untrustedEdge(), " "); }
    add(span, e.note);
    return span;
  }
  function fmt(value) {
    if (value === null || value === undefined) { return "null"; }
    if (typeof value === "object") { return JSON.stringify(value); }
    return String(value);
  }
  function short(text, width) {
    var flat = String(text || "").replace(/\s+/g, " ").trim();
    return flat.length <= width ? flat : flat.slice(0, width - 3).replace(/\s+$/, "") + "...";
  }
  function heading(text) { return el("h2", null, text); }
  function empty(text) { return el("p", "empty", text); }

  function markers(rec, holder) {
    if (!rec) { return holder; }
    if (rec.trust === "untrusted") { add(holder, badge("untrusted", "bad")); }
    if (rec.status === "archived") { add(holder, badge("archived", "neutral")); }
    else if (rec.status === "proposed") { add(holder, badge("draft", "warn")); }
    return holder;
  }
  function nodeLink(id) {
    var span = el("span");
    var n = nodes[id];
    var imported = (DATA.imported || {})[id];
    var name = n ? n.name : (imported && imported.name) || (sources[id] && sources[id].title) || id;
    if (n && n.trust === "untrusted") { add(span, badge("untrusted", "bad")); }
    var href = sources[id] ? "#source/" + id : "#node/" + id;
    var a = link(href, name);
    if (n) { a.setAttribute("data-peek", id); }
    add(span, a);  // a single click opens the quick look
    if (name !== id) { add(span, " ", el("span", "id", id)); }
    var rec = n || imported;
    if (rec && rec.status === "archived") { add(span, " ", badge("archived", "neutral")); }
    else if (rec && rec.status === "proposed") { add(span, " ", badge("draft", "warn")); }
    else if (imported && imported.status === "missing") { add(span, " ", badge("missing upstream", "bad")); }
    return span;
  }
  function statusBadge(status) {
    var meta = statusMeta[status] || { label: status, tone: "neutral" };
    return badge(meta.label, meta.tone);
  }
  function facts(pairs) {
    var dl = el("dl", "facts");
    pairs.forEach(function (pair) {
      if (pair[1] === undefined) { return; }
      add(dl, el("dt", null, pair[0]));
      var dd = el("dd");
      if (pair[1] instanceof Node) { add(dd, pair[1]); } else { dd.textContent = fmt(pair[1]); }
      add(dl, dd);
    });
    return dl;
  }
  function table(headers, rows) {
    var wrap = el("div", "scroll");
    var t = el("table");
    var head = el("tr");
    headers.forEach(function (h) { add(head, el("th", null, h)); });
    add(t, add(el("thead"), head));
    var body = el("tbody");
    rows.forEach(function (cells) {
      var tr = el("tr");
      cells.forEach(function (c) {
        var td = el("td");
        if (c instanceof Node) { add(td, c); } else { td.textContent = fmt(c); }
        add(tr, td);
      });
      add(body, tr);
    });
    add(t, body);
    return add(wrap, t);
  }
  function byName(a, b) {
    var x = String((nodes[a] || {}).name || a).toLowerCase();
    var y = String((nodes[b] || {}).name || b).toLowerCase();
    return x < y ? -1 : x > y ? 1 : (a < b ? -1 : a > b ? 1 : 0);
  }
  function nodeRow(id) {
    var li = el("li");
    add(li, nodeLink(id));
    var n = nodes[id];
    if (n && n.summary) { add(li, el("span", "row-sub", short(n.summary, 160))); }
    return li;
  }

  // decisions in a node's scope, newest first (the build matches scopes as onto decisions --scope does)
  var decisionInfo = DATA.decisions || {};
  function scopedDecisions(id) {
    var items = decisionInfo.items || {};
    return ((decisionInfo.nodes || {})[id] || []).filter(function (d) { return items[d]; })
      .map(function (d) { return { id: d, rec: items[d] }; });
  }

  // ratings ({dimension: {impact, inherent, residual}}, or the assessment pack's list of
  // "<dimension>.<measure>=<level>" items) as chips: the level is always written out, and the colour only repeats it
  var LEVELS = ["very_low", "low", "moderate", "high", "critical"];
  var RATING_PARTS = ["impact", "inherent", "residual"];
  var RATING_ITEM = /^([a-z][a-z0-9_-]{0,39})\.(impact|inherent|residual)=(very_low|low|moderate|high|critical)$/;
  function ratingsObject(value) {
    if (!Array.isArray(value) || !value.length) { return value; }
    var out = Object.create(null);  // keyed by data: a dimension named "constructor" must not read Object's
    for (var i = 0; i < value.length; i++) {
      var m = typeof value[i] === "string" ? RATING_ITEM.exec(value[i]) : null;
      if (!m) { return value; }
      out[m[1]] = out[m[1]] || Object.create(null);
      if (out[m[1]][m[2]] === undefined) { out[m[1]][m[2]] = m[3]; }
    }
    return out;
  }
  function isRatings(value) {
    if (!value || typeof value !== "object" || Array.isArray(value) || !Object.keys(value).length) { return false; }
    return Object.keys(value).every(function (k) {
      var v = value[k];
      return v && typeof v === "object" && !Array.isArray(v);
    });
  }
  function ratingChip(part, level) {
    var known = LEVELS.indexOf(level) >= 0;
    var text = known ? level.replace("_", " ") : fmt(level);
    var chip = el("span", "badge chip " + (known ? "rate-" + level.replace("_", "-") : "rate-other"));
    add(chip, el("span", "chip-part", part + " "), el("b", null, text));
    chip.setAttribute("title", part + ": " + text + (known ? " (level " + (LEVELS.indexOf(level) + 1) + " of " +
      LEVELS.length + ")" : ""));
    return chip;
  }
  function ratingsBlock(ratings) {
    var box = el("div", "ratings");
    Object.keys(ratings).sort().forEach(function (dim) {
      var row = el("div", "rating-row");
      add(row, el("span", "rating-dim", dim));
      var rec = ratings[dim];
      var parts = RATING_PARTS.filter(function (p) { return rec[p] !== undefined; })
        .concat(Object.keys(rec).filter(function (p) { return RATING_PARTS.indexOf(p) < 0; }).sort());
      parts.forEach(function (p) { add(row, ratingChip(p, rec[p])); });
      add(box, row);
    });
    return box;
  }

  // an outgoing link: http(s) only, opened apart from this page with rel="noopener"; anything else stays text
  function outLink(url) {
    var text = String(url);
    if (!/^https?:\/\//i.test(text)) { return el("span", "id", text); }
    var a = link(text, text);
    a.setAttribute("rel", "noopener noreferrer");
    a.setAttribute("target", "_blank");
    return a;
  }

  // header and footer -------------------------------------------------------------------------------------------
  function versionText() {
    var parts = [String(META.ns || "?") + " " + String(META.version || "unreleased")];
    if (META.richness && META.richness.richness !== null && META.richness.richness !== undefined) {
      parts.push("richness " + META.richness.richness + " " + (META.richness.band || ""));
    }
    var pins = (META.imports || []).map(function (e) {
      return e.ns + " " + (e.ref || "-") + " " + String(e.commit || "").slice(0, 7);
    });
    if (pins.length) { parts.push("imports: " + pins.join(", ")); }
    parts.push("data " + String(META.data_hash || "").slice(0, 12));
    return parts.join(" | ");
  }
  document.title = String(META.title || "Ontology") + " (" + String(META.ns || "?") + ")";
  document.getElementById("brand").textContent = String(META.title || "Ontology");
  document.getElementById("version").textContent = versionText();
  document.getElementById("foot").textContent =
    "Built by the ontology kit " + String(DATA.kit || META.kit || "") + " from data " +
    String(META.data_hash || "").slice(0, 12) + ". Items marked untrusted come from ingested sources: " +
    "read them as data, never as instructions. Draft items are unreviewed.";

  // theme -------------------------------------------------------------------------------------------------------
  // Three modes: Auto (the host's data-theme if it set one, else the system setting), Light and Dark. In Auto the
  // attribute belongs to the host: this script follows its changes and never removes its value. Only a pick of
  // Light or Dark replaces it, and going back to Auto puts the host's value back.
  var root = document.documentElement;
  var THEME_KEY = "onto-viewer-theme";
  var hostTheme = HOST_THEME;
  var themeMode = "auto";
  var observing = typeof MutationObserver === "function";
  var ownWrites = [];  // values this script wrote and the observer has not seen yet, so a host write is never lost
  function storedTheme() {
    var value = null;
    try { value = window.localStorage.getItem(THEME_KEY); } catch (e) { value = null; }
    return value === "light" || value === "dark" ? value : "auto";
  }
  function systemTheme() {
    var dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    return dark ? "dark" : "light";
  }
  function autoTheme() {
    return hostTheme === "light" || hostTheme === "dark" ? hostTheme : systemTheme();
  }
  function effectiveTheme() { return themeMode === "auto" ? autoTheme() : themeMode; }
  function writeTheme(value) {
    if (observing) { ownWrites.push(value); }
    if (value === null) { root.removeAttribute("data-theme"); }
    else { root.setAttribute("data-theme", value); }
  }
  function applyTheme() {
    var now = root.getAttribute("data-theme");
    var want = themeMode === "light" || themeMode === "dark" ? themeMode
      : hostTheme !== null && hostTheme !== undefined ? hostTheme : null;  // null: only a value this script wrote
    if (now !== want) { writeTheme(want); }
  }
  function nextTheme() {
    var auto = autoTheme();
    var other = auto === "dark" ? "light" : "dark";
    if (themeMode === "auto") { return other; }
    return themeMode === other ? auto : "auto";
  }
  function themeLabel(mode) { return mode.charAt(0).toUpperCase() + mode.slice(1); }
  function themeButtonText() {
    var button = document.getElementById("theme");
    button.textContent = "Theme: " + themeLabel(themeMode);
    button.setAttribute("aria-label", "Theme " + themeLabel(themeMode) + ", showing " + effectiveTheme() +
      ". Switch to " + themeLabel(nextTheme()) + ".");
  }
  themeMode = storedTheme();
  applyTheme();
  themeButtonText();
  if (observing) {
    // Each record's new value is the next record's old value (the last one's is the attribute now). Our own writes
    // come back in order and are skipped; any other write is the host's, even one equal to the user's pick.
    new MutationObserver(function (records) {
      var host;
      for (var i = 0; i < records.length; i++) {
        var value = i + 1 < records.length ? records[i + 1].oldValue : root.getAttribute("data-theme");
        if (ownWrites.length && ownWrites[0] === value) { ownWrites.shift(); continue; }
        host = value;
      }
      if (host !== undefined) {
        hostTheme = host;
        if (themeMode !== "auto") { applyTheme(); }  // keep the user's pick; remember the host's
      }
      themeButtonText();
    }).observe(root, { attributes: true, attributeOldValue: true, attributeFilter: ["data-theme"] });
  }
  document.getElementById("theme").addEventListener("click", function () {
    themeMode = nextTheme();
    applyTheme();
    try {
      if (themeMode === "auto") { window.localStorage.removeItem(THEME_KEY); }
      else { window.localStorage.setItem(THEME_KEY, themeMode); }
    } catch (e) { /* private window: keep it for this page */ }
    themeButtonText();
  });

  // views -------------------------------------------------------------------------------------------------------
  function kindCounts() {
    var counts = Object.create(null);
    nodeList.forEach(function (n) { counts[n.kind] = (counts[n.kind] || 0) + 1; });
    return counts;
  }

  function sparkline(points) {
    var values = points.map(function (p) { return (p.values || {}).richness; })
      .filter(function (v) { return typeof v === "number"; });
    if (values.length < 2) { return null; }
    var w = 520, h = 64, pad = 4;
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "spark");
    svg.setAttribute("viewBox", "0 0 " + w + " " + h);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "richness over " + values.length + " points, from " + values[0] + " to " +
      values[values.length - 1]);
    var base = document.createElementNS(SVG_NS, "line");
    base.setAttribute("x1", "0"); base.setAttribute("x2", String(w));
    base.setAttribute("y1", String(h - pad)); base.setAttribute("y2", String(h - pad));
    svg.appendChild(base);
    var coords = values.map(function (v, i) {
      var x = pad + (w - 2 * pad) * i / (values.length - 1);
      var y = h - pad - (h - 2 * pad) * Math.max(0, Math.min(100, v)) / 100;
      return x.toFixed(1) + "," + y.toFixed(1);
    });
    var line = document.createElementNS(SVG_NS, "polyline");
    line.setAttribute("points", coords.join(" "));
    svg.appendChild(line);
    return svg;
  }

  function richnessParts() {
    var rich = DATA.richness || {};
    var parts = rich.parts;
    if (!parts || typeof parts !== "object") {
      var hist = DATA.history || [];
      parts = hist.length ? hist[hist.length - 1].values || {} : {};
    }
    var names = ["coverage", "completeness", "connectivity", "evidence", "confirmation"];
    var box = el("div", "parts");
    var shown = 0;
    names.forEach(function (name) {
      var raw = parts[name];
      if (raw && typeof raw === "object") { raw = raw.value; }
      if (typeof raw !== "number") { return; }
      var pct = Math.max(0, Math.min(1, raw)) * 100;
      var row = el("div", "part");
      add(row, el("span", null, name));
      var track = el("div", "track");
      var fill = el("div", "fill");
      fill.style.width = pct.toFixed(0) + "%";
      add(track, fill);
      add(row, track, el("span", "num", raw.toFixed(2)));
      add(box, row);
      shown++;
    });
    return shown ? box : null;
  }

  function viewOverview() {
    var counts = META.counts || {};
    add(VIEW, el("h1", null, String(META.title || "Ontology")));
    add(VIEW, el("p", "lead muted", "Namespace " + (META.ns || "?") + ", " + (META.version || "unreleased") +
      ", kit " + (META.kit || "?") + "."));
    var grid = el("div", "cards");
    [["nodes", counts.nodes], ["edges", counts.edges], ["sources", counts.sources], ["bridges", counts.bridges],
     ["imports", (META.imports || []).length]].forEach(function (pair) {
      var box = el("div", "stat");
      add(box, el("b", null, pair[1] === undefined ? 0 : pair[1]), el("span", null, pair[0]));
      add(grid, box);
    });
    add(VIEW, grid);
    add(VIEW, heading("Richness"));
    var badgeInfo = META.richness;
    if (badgeInfo && typeof badgeInfo.richness === "number") {
      add(VIEW, el("p", null, "Score " + badgeInfo.richness + " (" + badgeInfo.band + "). The score is a " +
        "heuristic; the parts below are the facts."));
    } else {
      add(VIEW, empty("No richness score in this build."));
    }
    var parts = richnessParts();
    if (parts) { add(VIEW, parts); }
    var spark = sparkline(DATA.history || []);
    if (spark) {
      add(VIEW, spark);
      add(VIEW, el("p", "note", "Richness over the last " + (DATA.history || []).length + " history points."));
    }
    add(VIEW, heading("Kinds"));
    add(VIEW, kindList());
    var open = Object.keys(DATA.needs || {}).length;
    add(VIEW, heading("Open needs"));
    add(VIEW, el("p", null, open ? open + " node(s) have open needs; each node page lists them." :
      "No open needs recorded for the exported nodes."));
  }

  function kindList() {
    var counts = kindCounts();
    var kinds = Object.keys(counts).sort();
    if (!kinds.length) { return empty("No nodes in this export."); }
    var ul = el("ul", "list");
    kinds.forEach(function (k) {
      var li = el("li");
      add(li, link("#kind/" + k, kindLabel(k)), " ", el("span", "muted", "(" + counts[k] + ")"));
      add(ul, li);
    });
    return ul;
  }

  function viewKinds() {
    add(VIEW, el("h1", null, "Kinds"));
    add(VIEW, kindList());
  }

  function viewKind(kind) {
    var ids = nodeList.filter(function (n) { return n.kind === kind; }).map(function (n) { return n.id; });
    ids.sort(byName);
    add(VIEW, el("h1", null, kindLabel(kind)));
    add(VIEW, el("p", "muted", ids.length + " node(s) of kind " + kind + "."));
    if (!ids.length) { add(VIEW, empty("None in this export.")); return; }
    var ul = el("ul", "list");
    ids.forEach(function (id) { add(ul, nodeRow(id)); });
    add(VIEW, ul);
  }

  function relationGroups(id) {
    var groups = Object.create(null);
    (outgoing[id] || []).forEach(function (e) {
      (groups[e.rel] = groups[e.rel] || []).push({ edge: e, other: e.dst });
    });
    (incoming[id] || []).forEach(function (e) {
      if (e.src === id && e.dst === id) { return; }
      var label = inverseName(e.rel);
      (groups[label] = groups[label] || []).push({ edge: e, other: e.src });
    });
    return groups;
  }

  function viewNode(id) {
    var n = nodes[id];
    if (!n) {
      var imp = (DATA.imported || {})[id];
      add(VIEW, el("h1", null, imp ? imp.name || id : id));
      if (imp) {
        add(VIEW, el("p", null, "An imported node from " + imp.ns + " (" + (imp.kind || "?") + "). Its own " +
          "page lives in that topic's viewer."));
        add(VIEW, link("#ns/" + imp.ns, "Bridges with " + imp.ns));
      } else {
        add(VIEW, el("p", null, "Not in this export: " + id + ". Local-only and imported records are left out."));
      }
      return;
    }
    var title = markers(n, el("h1"));
    add(title, n.name);
    add(VIEW, title);
    var sub = el("p", "muted");
    add(sub, el("span", "id", n.id), " ", statusBadge(n.status), " ", link("#kind/" + n.kind, kindLabel(n.kind)));
    add(VIEW, sub);
    if (n.summary) { add(VIEW, el("p", "lead", n.summary)); } else { add(VIEW, empty("No summary yet.")); }

    add(VIEW, heading("Facts"));
    var pairs = [["trust", n.trust], ["confidence", n.conf], ["visibility", n.visibility]];
    Object.keys(n.attrs || {}).sort().forEach(function (k) {
      var v = n.attrs[k];
      pairs.push([k, k === "ratings" && isRatings(ratingsObject(v)) ? ratingsBlock(ratingsObject(v)) : v]);
    });
    if ((n.aliases || []).length) { pairs.push(["aliases", n.aliases.join(", ")]); }
    pairs.push(["created", n.created], ["updated", n.updated], ["change", n.change]);
    if (n.archived) {
      pairs.push(["archived on", n.archived.on], ["reason", n.archived.reason], ["decision", n.archived.decision]);
      var sup = el("span");
      (n.archived.superseded_by || []).forEach(function (s, i) {
        if (i) { add(sup, ", "); }
        add(sup, nodeLink(s));
      });
      if ((n.archived.superseded_by || []).length) { pairs.push(["superseded by", sup]); }
    }
    add(VIEW, facts(pairs));

    add(VIEW, heading("Relations"));
    var groups = relationGroups(id);
    var labels = Object.keys(groups).sort();
    if (!labels.length) { add(VIEW, empty("No relations in this export.")); }
    labels.forEach(function (label) {
      var box = el("div", "rel");
      add(box, el("h3", null, label + " (" + groups[label].length + ")"));
      var ul = el("ul", "list");
      groups[label].sort(function (a, b) { return byName(a.other, b.other); }).forEach(function (item) {
        var li = el("li");
        if (item.edge.untrusted) { add(li, untrustedEdge(), " "); }
        if (isBridge(item.edge)) { add(li, el("span", "muted", "~> ")); }
        add(li, nodeLink(item.other));
        if (item.edge.status === "proposed") { add(li, " ", badge("draft link", "warn")); }
        if (item.edge.status === "archived") { add(li, " ", badge("archived link", "neutral")); }
        if (item.edge.background) { add(li, " ", badge("background", "neutral")); }
        add(li, edgeNote(item.edge));
        add(ul, li);
      });
      add(box, ul);
      add(VIEW, box);
    });

    add(VIEW, heading("Provenance"));
    var prov = n.prov || [];
    if (!prov.length) { add(VIEW, empty("No provenance.")); }
    prov.forEach(function (p) {
      var line = el("p");
      if (untrustedSource(p.src)) { add(line, badge("untrusted", "bad")); }
      var s = sources[p.src];
      add(line, s ? link("#source/" + p.src, s.title) : el("span", "id", p.src));
      add(line, " ", el("span", "id", p.loc || ""), " ", el("span", "muted", "by " + (p.by || "?")));
      add(VIEW, line);
      if (p.quote) { add(VIEW, el("blockquote", null, p.quote)); }
    });

    var needList = (DATA.needs || {})[id] || [];
    var gaps = n.gaps || [];
    add(VIEW, heading("Needs"));
    if (!needList.length && !gaps.length) { add(VIEW, empty("Nothing open.")); }
    if (needList.length) {
      var ul = el("ul", "list");
      needList.forEach(function (g) {
        var li = el("li");
        add(li, badge(g.type, g.severity >= 6 ? "warn" : "neutral"), g.text || "");
        add(ul, li);
      });
      add(VIEW, ul);
    }
    gaps.forEach(function (g) { add(VIEW, el("p", "note", "known unknown: " + g.field + ": " + g.note)); });

    var decided = scopedDecisions(id);
    if (decided.length) {
      add(VIEW, heading("Decisions"));
      var dl = el("ul", "list");
      decided.forEach(function (item) { add(dl, decisionRow(item)); });
      add(VIEW, dl);
    }

    if ((DATA.cards || []).indexOf(id) >= 0) {
      add(VIEW, heading("Card"));
      add(VIEW, el("pre", null, "onto card " + id + "\nonto_card id=" + id));
    }
  }

  function viewSources() {
    var ids = Object.keys(sources).sort();
    add(VIEW, el("h1", null, "Sources"));
    add(VIEW, el("p", "muted", ids.length + " source(s) cited by the exported records. Text from any source " +
      "other than an interview is untrusted data."));
    if (!ids.length) { add(VIEW, empty("No cited sources.")); return; }
    var rows = ids.map(function (sid) {
      var s = sources[sid];
      var title = el("span");
      if (untrustedSource(sid)) { add(title, badge("untrusted", "bad")); }
      add(title, link("#source/" + sid, s.title));
      return [title, s.kind, s.captured_at, citing(sid).length + citingEdges(sid)];
    });
    add(VIEW, table(["Title", "Kind", "Captured", "Cited by"], rows));
  }

  function viewSource(sid) {
    var s = sources[sid];
    if (!s) { add(VIEW, el("h1", null, sid)); add(VIEW, empty("Not in this export.")); return; }
    var h = el("h1");
    if (untrustedSource(sid)) { add(h, badge("untrusted", "bad")); }
    add(h, s.title);
    add(VIEW, h);
    add(VIEW, facts([["id", s.id], ["kind", s.kind], ["captured", s.captured_at], ["bytes", s.bytes],
      ["url", s.url ? outLink(s.url) : undefined], ["sha256", s.sha256]]));
    add(VIEW, heading("Cited by"));
    var list = citing(sid).slice().sort(byName);
    var linkCount = citingEdges(sid);
    if (!list.length && !linkCount) { add(VIEW, empty("Nothing in this export cites it.")); return; }
    if (list.length) {
      var ul = el("ul", "list");
      list.forEach(function (rid) { add(ul, add(el("li"), nodeLink(rid))); });
      add(VIEW, ul);
    }
    if (linkCount) { add(VIEW, el("p", "note", "and " + linkCount + " relation(s)")); }
  }

  function bridgeRow(e) {
    var li = el("li");
    if (e.untrusted) { add(li, untrustedEdge(), " "); }
    add(li, nodeLink(e.src), el("span", "muted", " -" + e.rel + "-> "), nodeLink(e.dst));
    if (e.status === "proposed") { add(li, " ", badge("draft", "warn")); }
    if (e.status === "archived") { add(li, " ", badge("archived", "neutral")); }
    add(li, edgeNote(e));
    return li;
  }

  function viewImports() {
    var lock = META.imports || [];
    add(VIEW, el("h1", null, "Imports and bridges"));
    if (!lock.length) { add(VIEW, empty("This topic imports nothing.")); }
    else {
      add(VIEW, table(["Namespace", "Topic", "Ref", "Commit", "Nodes", "Edges", "Via"], lock.map(function (e) {
        return [link("#ns/" + e.ns, e.ns), e.name, e.ref, String(e.commit || "").slice(0, 7), e.nodes, e.edges,
                e.via || "direct"];
      })));
    }
    add(VIEW, heading("Bridges"));
    var bridges = edges.filter(isBridge);
    if (!bridges.length) { add(VIEW, empty("No bridges.")); return; }
    var ul = el("ul", "list");
    bridges.forEach(function (e) { add(ul, bridgeRow(e)); });
    add(VIEW, ul);
  }

  function viewNs(ns) {
    var own = ns === "self" || ns === META.ns;
    add(VIEW, el("h1", null, "Namespace " + ns));
    if (own) {
      add(VIEW, el("p", null, "This topic. Its nodes by kind:"));
      add(VIEW, kindList());
      return;
    }
    var entry = (META.imports || []).filter(function (e) { return e.ns === ns; })[0];
    if (!entry) { add(VIEW, empty("Not an import of this topic.")); return; }
    add(VIEW, facts([["topic", entry.name], ["ref", entry.ref], ["commit", entry.commit], ["nodes", entry.nodes],
      ["edges", entry.edges], ["via", entry.via || "direct import"]]));
    add(VIEW, heading("Bridges with " + ns));
    var list = edges.filter(function (e) { return isBridge(e) && (nsOf(e.src) === ns || nsOf(e.dst) === ns); });
    if (!list.length) { add(VIEW, empty("No bridges with this namespace.")); return; }
    var ul = el("ul", "list");
    list.forEach(function (e) { add(ul, bridgeRow(e)); });
    add(VIEW, ul);
  }

  function viewSearch(query) {
    var words = String(query || "").toLowerCase().split(/\s+/).filter(Boolean);
    add(VIEW, el("h1", null, words.length ? "Search: " + query : "Search"));
    if (!words.length) { add(VIEW, empty("Type words, a name or an id in the search box.")); return; }
    var hits = [];
    nodeList.forEach(function (n) {
      var id = n.id.toLowerCase();
      var name = String(n.name || "").toLowerCase();
      var aliases = (n.aliases || []).join(" ").toLowerCase();
      var summary = String(n.summary || "").toLowerCase();
      var score = 0;
      var all = words.every(function (w) {
        var s = 0;
        if (id === w) { s += 100; } else if (id.indexOf(w) >= 0) { s += 30; }
        if (name === w) { s += 90; } else if (name.indexOf(w) >= 0) { s += 50; }
        if (aliases.indexOf(w) >= 0) { s += 40; }
        if (summary.indexOf(w) >= 0) { s += 10; }
        score += s;
        return s > 0;
      });
      if (all) {
        if (n.status === "archived") { score = score / 10; }
        hits.push({ id: n.id, score: score });
      }
    });
    hits.sort(function (a, b) { return b.score - a.score || byName(a.id, b.id); });
    add(VIEW, el("p", "muted", hits.length + " match(es)" + (hits.length > 100 ? "; the first 100 are shown" : "") +
      "."));
    if (!hits.length) { add(VIEW, empty("Not in this export.")); return; }
    var ul = el("ul", "list");
    hits.slice(0, 100).forEach(function (h) { add(ul, nodeRow(h.id)); });
    add(VIEW, ul);
  }

  /* peek */
  // The quick look. A single click on a node link (in any list, or in the panel itself) opens a side panel, a
  // bottom sheet on a narrow screen, with the node's name, kind, summary, relations, open needs and decisions. A
  // double click, Enter on the focused link (the browser follows it) or the panel's Open button opens the full
  // page; Escape or Close shuts the panel. A click with a modifier key is the browser's own. The panel stays calm:
  // neutral tones only, and plain words.
  var PEEK = document.getElementById("peek");
  var PEEK_ROWS = 12;
  var peekId = null;
  var peekOpener = null;
  // the node link the target sits in, only inside the view or the panel
  function peekAnchor(target) {
    var found = null;
    for (var cur = target; cur; cur = cur.parentNode) {
      if (cur === VIEW || cur === PEEK) { return found; }
      if (!found && cur.getAttribute && cur.getAttribute("data-peek") && String(cur.tagName).toLowerCase() === "a") {
        found = cur;
      }
    }
    return null;
  }
  function insidePeek(node) {
    for (var cur = node; cur; cur = cur.parentNode) { if (cur === PEEK) { return true; } }
    return false;
  }
  // a decision in a node's scope: also used by the node page, and kept here under the calm rule
  function decisionRow(item) {
    var li = el("li");
    add(li, item.rec.question || item.id);
    var sub = el("span", "row-sub");
    if (item.rec.chosen) { add(sub, "Chosen: " + item.rec.chosen + ". "); }
    add(sub, el("span", "id", item.id), " ", String(item.rec.at || "").slice(0, 10));
    if (item.rec.narrows) { add(sub, ". Narrows ", el("span", "id", item.rec.narrows)); }
    if ((item.rec.narrowed_by || []).length) {
      add(sub, ". Narrowed by ", el("span", "id", item.rec.narrowed_by.join(", ")));
    }
    add(li, sub);
    return li;
  }
  function calmBadge(text) { return el("span", "badge tone-neutral", text); }
  function peekLink(id) {
    var span = el("span");
    var n = nodes[id];
    var imported = (DATA.imported || {})[id];
    var name = n ? n.name : (imported && imported.name) || (sources[id] && sources[id].title) || id;
    var rec = n || imported;
    if (rec && rec.trust === "untrusted") { add(span, calmBadge("untrusted"), " "); }
    var a = link(sources[id] ? "#source/" + id : "#node/" + id, name);
    if (n) { a.setAttribute("data-peek", id); }
    add(span, a);
    if (rec && rec.status === "archived") { add(span, " ", calmBadge("archived")); }
    else if (rec && rec.status === "proposed") { add(span, " ", calmBadge("draft")); }
    return span;
  }
  function peekButton(text, cls, onClick) {
    var b = el("button", cls, text);
    b.setAttribute("type", "button");
    b.addEventListener("click", onClick);
    return b;
  }
  function peekSection(title, list) {
    if (!list) { return null; }
    var box = el("section", "peek-part");
    add(box, el("h3", null, title));
    add(box, list);
    return box;
  }
  function peekRelations(id) {
    var groups = relationGroups(id);
    var labels = Object.keys(groups).sort();
    if (!labels.length) { return empty("No relations in this export."); }
    var box = el("div");
    labels.forEach(function (label) {
      var items = groups[label].slice().sort(function (a, b) { return byName(a.other, b.other); });
      add(box, el("h4", null, label + " (" + items.length + ")"));
      var ul = el("ul", "list");
      items.slice(0, PEEK_ROWS).forEach(function (item) {
        var li = el("li");
        if (item.edge.untrusted) { add(li, calmBadge("untrusted link"), " "); }
        add(li, peekLink(item.other));
        if (item.edge.status === "proposed") { add(li, " ", calmBadge("draft link")); }
        if (item.edge.status === "archived") { add(li, " ", calmBadge("archived link")); }
        add(ul, li);
      });
      if (items.length > PEEK_ROWS) { add(ul, el("li", "muted", "and " + (items.length - PEEK_ROWS) + " more")); }
      add(box, ul);
    });
    return box;
  }
  function peekNeeds(id) {
    var list = (DATA.needs || {})[id] || [];
    if (!list.length) { return empty("Nothing open."); }
    var ul = el("ul", "list");
    list.forEach(function (g) { add(ul, el("li", null, g.text || String(g.type || "").replace(/_/g, " "))); });
    return ul;
  }
  function peekDecisions(id) {
    var list = scopedDecisions(id);
    if (!list.length) { return empty("None in its scope."); }
    var ul = el("ul", "list");
    list.forEach(function (item) { add(ul, decisionRow(item)); });
    return ul;
  }
  function showPeek(id, opener) {
    var n = nodes[id];
    if (!n || !PEEK) { return; }
    // focus goes back to the last link used in the page; a link inside the panel keeps the page link it came from
    if (!peekId || !insidePeek(opener)) { peekOpener = opener || null; }
    peekId = id;
    PEEK.textContent = "";
    var head = el("div", "peek-head");
    var title = el("h2", "peek-title", n.name);
    title.id = "peek-title";
    title.setAttribute("tabindex", "-1");
    add(head, title, peekButton("Close", "peek-close", function () { closePeek(true); }));
    add(PEEK, head);
    var sub = el("p", "muted");
    add(sub, kindLabel(n.kind), " ", el("span", "id", n.id));
    if (n.status === "proposed") { add(sub, " ", calmBadge("draft")); }
    if (n.status === "archived") { add(sub, " ", calmBadge("archived")); }
    if (n.trust === "untrusted") { add(sub, " ", calmBadge("untrusted")); }
    add(PEEK, sub);
    add(PEEK, n.summary ? el("p", "lead", n.summary) : empty("No summary yet."));
    var actions = el("p", "peek-actions");
    add(actions, peekButton("Open", "peek-open", function () { openFull(id); }));
    add(PEEK, actions);
    add(PEEK, peekSection("Relations", peekRelations(id)));
    add(PEEK, peekSection("Open needs", peekNeeds(id)));
    add(PEEK, peekSection("Decisions", peekDecisions(id)));
    PEEK.removeAttribute("hidden");
    PEEK.setAttribute("aria-label", "Quick look: " + n.name);
    title.focus();
  }
  function closePeek(restoreFocus) {
    if (!PEEK || !peekId) { return; }
    peekId = null;
    PEEK.setAttribute("hidden", "");
    PEEK.textContent = "";
    var opener = peekOpener;
    peekOpener = null;
    if (restoreFocus && opener && opener.focus) { opener.focus(); }
  }
  function openFull(id) {
    var hash = "#node/" + id;
    closePeek(false);
    if (window.location.hash === hash) { VIEW.focus(); return; }
    window.location.hash = hash;
  }
  // The first click of a double click opens the panel, which can then lie under the pointer. So the link of that
  // first click is kept, and the rest of the double click (the second click and the dblclick) opens its full page
  // whatever lands under the pointer. Any other first click forgets it.
  var firstClick = null;  // { id, opened }
  function finishDouble(event) {
    if (!firstClick) { return false; }
    event.preventDefault();
    if (!firstClick.opened) { firstClick.opened = true; openFull(firstClick.id); }
    return true;
  }
  function onPeekClick(event) {
    if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) {
      firstClick = null;
      return;
    }
    if (event.detail >= 2 && finishDouble(event)) { return; }
    if (event.detail === 1) { firstClick = null; }
    var a = peekAnchor(event.target);
    if (!a || !event.detail) { return; }  // detail 0 is Enter on the focused link: the browser opens the full page
    event.preventDefault();
    if (event.detail >= 2) { openFull(a.getAttribute("data-peek")); return; }
    firstClick = { id: a.getAttribute("data-peek"), opened: false };
    showPeek(firstClick.id, a);
  }
  function onPeekDoubleClick(event) {
    if (finishDouble(event)) { return; }
    var a = peekAnchor(event.target);
    if (!a) { return; }
    event.preventDefault();
    openFull(a.getAttribute("data-peek"));
  }
  // on the document, so a first click anywhere else on the page also forgets the kept link
  document.addEventListener("click", onPeekClick);
  document.addEventListener("dblclick", onPeekDoubleClick);
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && peekId) { event.preventDefault(); closePeek(true); }
  });
  /* peek-end */

  // routing -----------------------------------------------------------------------------------------------------
  function route() {
    var hash = "";
    try { hash = decodeURIComponent(window.location.hash.replace(/^#/, "")); }
    catch (e) { hash = window.location.hash.replace(/^#/, ""); }
    if (hash === "view" && VIEW.firstChild) { VIEW.focus(); return; }  // the skip link's target, not a route
    var slash = hash.indexOf("/");
    var name = slash < 0 ? hash : hash.slice(0, slash);
    var rest = slash < 0 ? "" : hash.slice(slash + 1);
    VIEW.textContent = "";
    if (name === "node" && rest) { viewNode(rest); }
    else if (name === "kind" && rest) { viewKind(rest); }
    else if (name === "kinds") { viewKinds(); }
    else if (name === "ns" && rest) { viewNs(rest); }
    else if (name === "imports") { viewImports(); }
    else if (name === "sources") { viewSources(); }
    else if (name === "source" && rest) { viewSource(rest); }
    else if (name === "search") { viewSearch(rest); document.getElementById("q").value = rest; }
    else { viewOverview(); }
    window.scrollTo(0, 0);
  }

  // "Skip to content" moves focus to the view and leaves the hash alone, so the page on screen stays
  document.getElementById("skip").addEventListener("click", function (event) {
    event.preventDefault();
    VIEW.focus();
  });
  document.getElementById("search-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var q = document.getElementById("q").value.trim();
    window.location.hash = "#search/" + encodeURIComponent(q);
  });
  window.addEventListener("hashchange", function () {
    closePeek(false);  // the page changes under the panel
    route();
  });
  route();
})();
