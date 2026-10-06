"""build and release: the export is byte-identical twice, passes its schema and leaves local records out; bundles
are flattened; the viewer keeps ``</script>`` inside its data and respects its size limits; the release dry run
writes nothing; tag naming; MANIFEST hashes match the commit; tracked ``.onto/`` and ``inbox/`` files fail the
paths check (and, with ``--push``, commits not yet on origin that touch them, or that hold content ``onto erase``
removed since, with the squash that clears them); ``--commit`` refuses unrelated dirty
files; a failed commit puts the outputs back (also on a branch with no commit yet); push; the scan exits 2 with
prefix-only output, including denylist hits, and 1 when a file cannot be read; notes with a hit are never echoed;
notes go through the source sanitizer (a credential or a refused personal kind fails the release unechoed, other
personal data is redacted in VERSIONS.md, the change, the commit and the tag message);
the viewer marks untrusted edges and keeps the page on the skip link; it keeps a host data-theme in Auto; a single
click on a node link opens the calm quick look and a double click, Enter or Open opens the full page; ratings show
as labelled chips; outgoing links use rel="noopener" (run under node when it is installed)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import types
import unittest
from unittest import mock

from tests import _support
from ontokit import (__version__, build, commands, graph, history, ingest, ledger, lockfile, mutate, packs, records,
                     release, sources, store, util)
from ontokit.errors import DataError, GitError, Refused, UsageError

LOCAL_ID = "person:volunteer-lead"
LOCAL_NAME = "Volunteer lead"
LOCAL_EDGES = ("e:24a1c050e1c3", "e:538b24db831f")
GITIGNORE = ".onto/\ninbox/\nbuild/index.html\n"
DENY_TERM = "zanzibar-orchard"
NOTES_EMAIL = "desk" + "@" + "allotment.invalid"
NOTES_PHONE = "(555) 555-0142"
NODE = shutil.which("node")
UNTRUSTED_EDGE = "e:7b631b79092b"  # role:plot-coordinator owns dataset:harvest-log, both ends trusted
WATER_DEC = "dec-20260928-keep-the-monthly-steward-rota-or-water-e-916a"  # scope: process:old-rota, process:watering
CALM_BANNED = ("warning", "alert", "failed", "overdue", "stale", "error", "danger", "urgent")
ALERT_TOKENS = ("--warn", "--bad", "tone-warn", "tone-bad", "--rate-", "rate-", '"warn"', '"bad"')
RATING_TOKENS = tuple("--rate-%s-%s" % (lvl, part) for lvl in ("vlow", "low", "mod", "high", "crit")
                      for part in ("bg", "fg"))
# helpers outside the peek region that the panel calls; the calm gate reads their bodies too
PEEK_SHARED_HELPERS = ("el", "add", "link", "empty", "kindLabel", "relationGroups", "scopedDecisions", "byName")


def js_function(js, name):
    """The text of a top-level ``function name(...)`` in viewer.js: its one line, or up to its closing brace."""
    start = js.index("\n  function %s(" % name) + 1
    line_end = js.index("\n", start)
    if js[start:line_end].rstrip().endswith("}"):
        return js[start:line_end + 1]
    return js[start:js.index("\n  }\n", start) + 5]


def luminance(hex_colour):
    def channel(value):
        value /= 255.0
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


POSIX_PERMS = os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0

# A minimal DOM for running viewer.js under node: elements, text nodes, bubbling events, the hash, focus, local
# storage and a MutationObserver for the theme attribute. It prints, per step, the view as lines (badges as
# [text]), the quick-look panel, the theme state, the hash and the focused element.
DOM_HARNESS = r"""
"use strict";
var fs = require("fs");
var vm = require("vm");
var input = JSON.parse(fs.readFileSync(0, "utf8"));
function Node_() {}
function Text_(t) { this.data = String(t); }
Text_.prototype = Object.create(Node_.prototype);
Object.defineProperty(Text_.prototype, "textContent", { get: function () { return this.data; } });
function El(tag) {
  this.tagName = tag; this.children = []; this.attrs = {}; this.className = ""; this.style = {};
  this.listeners = {}; this.value = ""; this.id = ""; this.parentNode = null;
}
El.prototype = Object.create(Node_.prototype);
Object.defineProperty(El.prototype, "textContent", {
  get: function () { return this.children.map(function (c) { return c.textContent; }).join(""); },
  set: function (v) {
    this.children.forEach(function (c) { c.parentNode = null; });
    this.children = (v === "" || v === null || v === undefined) ? [] : [new Text_(v)];
  }
});
Object.defineProperty(El.prototype, "firstChild", { get: function () { return this.children[0] || null; } });
El.prototype.appendChild = function (c) { this.children.push(c); c.parentNode = this; return c; };
El.prototype.setAttribute = function (k, v) {
  var old = this.getAttribute(k);
  this.attrs[k] = String(v);
  if (doc && this === doc.documentElement) { mutated(k, old); }
};
El.prototype.getAttribute = function (k) {
  return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null;
};
El.prototype.hasAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); };
El.prototype.removeAttribute = function (k) {
  var had = this.hasAttribute(k);
  var old = this.getAttribute(k);
  delete this.attrs[k];
  if (had && doc && this === doc.documentElement) { mutated(k, old); }
};
El.prototype.addEventListener = function (t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); };
El.prototype.focus = function () { doc.activeElement = this; };
// an event goes to the target, then up through its parents, then to the document
function dispatch(target, type, props) {
  var prevented = false;
  var ev = { type: type, target: target, detail: 0, button: 0, key: "",
             preventDefault: function () { prevented = true; } };
  Object.keys(props || {}).forEach(function (k) { ev[k] = props[k]; });
  var chain = [];
  for (var cur = target; cur; cur = cur.parentNode) { chain.push(cur); }
  chain.push(doc);
  chain.forEach(function (node) { (node.listeners[type] || []).forEach(function (f) { f(ev); }); });
  return prevented;
}
var observers = [];
var pending = [];
function mutated(name, old) { pending.push({ type: "attributes", attributeName: name, oldValue: old }); }
function MutationObserver_(cb) { this.cb = cb; }
MutationObserver_.prototype.observe = function (target, opts) { observers.push(this); };
var byId = {};
["data", "view", "brand", "version", "foot", "theme", "search-form", "q", "skip", "peek"].forEach(function (id) {
  var e = new El(id === "view" ? "main" : id === "peek" ? "aside" : "div");
  e.id = id;
  byId[id] = e;
});
byId.skip.setAttribute("href", "#view");
byId.peek.setAttribute("hidden", "");
byId.data.textContent = input.data;
var doc = {
  activeElement: null, title: "", documentElement: new El("html"), listeners: {},
  getElementById: function (id) { return byId[id] || null; },
  createElement: function (t) { return new El(t); },
  createElementNS: function (ns, t) { return new El(t); },
  createTextNode: function (t) { return new Text_(t); },
  addEventListener: El.prototype.addEventListener
};
if (input.root_theme !== undefined && input.root_theme !== null) {
  doc.documentElement.attrs["data-theme"] = input.root_theme;
}
var store = {};
if (input.stored) { store["onto-viewer-theme"] = input.stored; }
var hashValue = input.initial || "";
var hashDirty = false;
var win = {
  listeners: {}, addEventListener: El.prototype.addEventListener,
  location: {},
  scrollTo: function () {}, matchMedia: function () { return { matches: !!input.system_dark }; },
  localStorage: {
    getItem: function (k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
    setItem: function (k, v) { store[k] = String(v); },
    removeItem: function (k) { delete store[k]; }
  }
};
// setting the hash to a new value fires hashchange once the step is over, as a browser does
Object.defineProperty(win.location, "hash", {
  get: function () { return hashValue; },
  set: function (v) {
    v = String(v);
    if (v && v.charAt(0) !== "#") { v = "#" + v; }
    if (v !== hashValue) { hashValue = v; hashDirty = true; }
  }
});
function flush() {
  for (var round = 0; round < 10 && (pending.length || hashDirty); round++) {
    if (pending.length) {
      var records = pending;
      pending = [];
      observers.forEach(function (o) { o.cb(records, o); });
    }
    if (hashDirty) { hashDirty = false; dispatch(win, "hashchange"); }
  }
}
var ctx = { window: win, document: doc, Node: Node_ };
if (!input.no_observer) { ctx.MutationObserver = MutationObserver_; }
vm.runInNewContext(input.js, ctx);
flush();
var BLOCK = { li: 1, p: 1, h1: 1, h2: 1, h3: 1, h4: 1, blockquote: 1, pre: 1, dt: 1, dd: 1, tr: 1, button: 1 };
function inline(n) {
  if (n instanceof Text_) { return n.data; }
  var t = n.children.map(inline).join("");
  return /\bbadge\b/.test(n.className) ? "[" + t + "]" : t;
}
function lines(n, out) {
  if (n instanceof Text_) { return out; }
  if (BLOCK[n.tagName]) { out.push(inline(n)); return out; }
  n.children.forEach(function (c) { lines(c, out); });
  return out;
}
function walk(n, out) {
  if (n instanceof Text_) { return out; }
  out.push(n);
  n.children.forEach(function (c) { walk(c, out); });
  return out;
}
function anchor(href) {
  var all = walk(byId.peek, []).concat(walk(byId.view, []));
  if (input.prefer_view) { all = walk(byId.view, []).concat(walk(byId.peek, [])); }
  var hit = all.filter(function (n) { return n.tagName === "a" && n.getAttribute("href") === href; })[0];
  if (!hit) { throw new Error("no link to " + href); }
  return hit;
}
function button(text) {
  var hit = walk(byId.peek, []).filter(function (n) { return n.tagName === "button" && n.textContent === text; })[0];
  if (!hit) { throw new Error("no button " + text); }
  return hit;
}
function follow(a) { win.location.hash = a.getAttribute("href") || ""; }
function snap() {
  var open = !byId.peek.hasAttribute("hidden");
  var active = doc.activeElement;
  return {
    hash: win.location.hash, lines: lines(byId.view, []), focus: active ? active.id : null,
    focus_href: active && active.getAttribute ? active.getAttribute("href") : null,
    peek: open ? lines(byId.peek, []) : null,
    peek_classes: walk(byId.peek, []).map(function (n) { return n.className; }).filter(Boolean),
    peek_attrs: walk(byId.peek, []).map(function (n) { return n.attrs; }),
    view_attrs: walk(byId.view, []).filter(function (n) { return n.tagName === "a"; })
      .map(function (n) { return n.attrs; }),
    view_classes: walk(byId.view, []).map(function (n) { return n.className; }).filter(Boolean),
    theme: doc.documentElement.getAttribute("data-theme"), theme_button: byId.theme.textContent,
    theme_label: byId.theme.getAttribute("aria-label"), stored: store["onto-viewer-theme"] || null
  };
}
var out = [snap()];
input.steps.forEach(function (s) {
  if (s.hash !== undefined) { hashValue = s.hash; dispatch(win, "hashchange"); }
  if (s.click) {
    var target = byId[s.click];
    if (!dispatch(target, "click", { detail: 1 })) { follow(target); }
  }
  if (s.peek) {
    var a = anchor(s.peek);
    if (!dispatch(a, "click", { detail: s.detail === undefined ? 1 : s.detail, ctrlKey: !!s.ctrl })) { follow(a); }
  }
  if (s.dblclick) {
    // the second click and the dblclick land on s.second (a link, or "button:<text>" in the panel) when given,
    // as when the panel the first click opened lies under the pointer
    var d = anchor(s.dblclick);
    if (!dispatch(d, "click", { detail: 1 })) { follow(d); }
    var d2 = !s.second ? d : s.second.indexOf("button:") === 0 ? button(s.second.slice(7)) : anchor(s.second);
    if (!dispatch(d2, "click", { detail: 2 }) && d2.tagName === "a") { follow(d2); }
    dispatch(d2, "dblclick", { detail: 2 });
  }
  if (s.enter) {
    var e = anchor(s.enter);
    e.focus();
    if (!dispatch(e, "keydown", { key: "Enter" }) && !dispatch(e, "click", { detail: 0 })) { follow(e); }
  }
  if (s.key) { dispatch(doc.activeElement || byId.view, "keydown", { key: s.key }); }
  if (s.button) { dispatch(button(s.button), "click", { detail: 1 }); }
  if (s.host_theme !== undefined) {
    if (s.host_theme === null) { doc.documentElement.removeAttribute("data-theme"); }
    else { doc.documentElement.setAttribute("data-theme", s.host_theme); }
  }
  flush();
  out.push(snap());
});
process.stdout.write(JSON.stringify(out));
"""


def clear():
    graph.clear_cache()
    store.clear_cache()
    lockfile.clear_cache()


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def edit_node(root, node_id, **fields):
    """Change a node in place, keeping the file sorted and canonical."""
    path = os.path.join(root, "graph", "nodes.jsonl")
    rows, _problems = store.read_jsonl(path)
    for row in rows:
        if row["id"] == node_id:
            row.update(fields)
    store.write_jsonl(path, rows)
    clear()


def edit_edge(root, edge_id, **fields):
    """Change an edge in place, keeping the file sorted and canonical."""
    path = os.path.join(root, "graph", "edges.jsonl")
    rows, _problems = store.read_jsonl(path)
    for row in rows:
        if row["id"] == edge_id:
            row.update(fields)
    store.write_jsonl(path, rows)
    clear()


def lock_entry(ns, name, data, export_obj, via=None):
    return {"ns": ns, "name": name, "from": None, "ref": "v1", "commit": "ab" * 20,
            "export_sha256": util.sha256_hex(data), "manifest_sha256": None, "kit": "0.1.0", "format": 1,
            "packs": {k: v["sha256"] for k, v in export_obj["meta"]["packs"].items()},
            "nodes": len(export_obj["nodes"]), "edges": len(export_obj["edges"]), "via": via, "override": None,
            "locked_at": "2026-09-28"}


class Base(_support.TempCase):
    def setUp(self):
        super().setUp()
        clear()
        self.addCleanup(clear)

    def mini(self, ns="mini"):
        return _support.make_topic(self.tmp, "mini", ns)

    def repo(self, root):
        return store.Repo.open(root)

    def git_topic(self, ns="mini"):
        root = self.mini(ns)
        write_text(os.path.join(root, ".gitignore"), GITIGNORE)
        _support.git_init(root)
        _support.commit_all(root, "topic data")
        return root

    def cli(self, root, *args):
        clear()
        return _support.run_cli(list(args), root)


# the export ----------------------------------------------------------------------------------------------------
class ExportTest(Base):
    def test_export_is_byte_identical_twice(self):
        root = self.mini()
        first = build.write(self.repo(root))
        a = read_bytes(os.path.join(root, "build", "export.json"))
        clear()
        second = build.write(self.repo(root))
        b = read_bytes(os.path.join(root, "build", "export.json"))
        self.assertEqual(a, b)
        self.assertEqual(first["files"], second["files"])
        self.assertEqual(a, util.canonical_bytes(json.loads(a.decode("utf-8"))))
        code, out, err = self.cli(root, "build", "--check", "--html")
        self.assertEqual(code, 0, err)
        self.assertIn("same bytes twice", out)
        self.assertIn("build/export.json: same", out)

    def test_export_contract(self):
        root = self.mini()
        repo = self.repo(root)
        exp = build.export(repo)
        self.assertEqual(records.check(exp, "export"), [])
        meta = exp["meta"]
        self.assertEqual((meta["version"], meta["ns"], meta["name"], meta["format"]),
                         ("unreleased", "mini", "mini-garden", 1))
        self.assertEqual(meta["data_hash"], store.data_hash(repo))
        self.assertEqual([s["id"] for s in meta["statuses"]], ["confirmed", "proposed", "archived"])
        self.assertEqual(sorted(meta["packs"]), ["core", "discovery", "local"])
        onto = graph.Ontology.load(repo)
        for name, block in meta["packs"].items():
            self.assertEqual(block["sha256"], packs.sha_of(block["pack"]))
            self.assertEqual(block["sha256"], onto.registry.pack_sha(name))
        self.assertEqual(meta["counts"], {"nodes": 11, "edges": 13, "sources": 2, "bridges": 0})
        self.assertEqual([n["id"] for n in exp["nodes"]], sorted(n["id"] for n in exp["nodes"]))
        self.assertEqual([e["id"] for e in exp["edges"]], sorted(e["id"] for e in exp["edges"]))
        self.assertEqual(exp["bundled"], {})
        self.assertEqual(meta["imports"], [])
        # archived and draft records are exported; the quotes stay
        ids = {n["id"] for n in exp["nodes"]}
        self.assertIn("process:old-rota", ids)
        self.assertIn("deliverable:harvest-report", ids)
        self.assertTrue(all(p.get("quote") for n in exp["nodes"] for p in n["prov"]))
        # no clock: only the sources' captured_at is a time
        text = json.dumps(exp)
        self.assertEqual(set(re.findall(r"\d{4}-\d\d-\d\dT[\d:]+Z", text)),
                         {s["captured_at"] for s in exp["sources"]})
        with self.assertRaises(UsageError):
            build.export(repo, "latest")
        self.assertEqual(build.export(repo, "v3")["meta"]["version"], "v3")

    def test_last_change_ignores_checkpoints_and_releases(self):
        root = self.mini()
        repo = self.repo(root)
        before = build.export(repo)["meta"]["last_change"]
        self.assertEqual(before, ledger.last_change(repo))
        ledger.checkpoint(repo, ["did a thing"], ["next thing"], [])
        ledger.append_change(repo, "release", "user", [], "release v1: first", extra={"version": "v1"})
        clear()
        self.assertEqual(build.export(repo)["meta"]["last_change"], before)

    def test_local_visibility_is_left_out(self):
        root = self.mini()
        report = build.write(self.repo(root), html=True)
        exp = build.export(self.repo(root))
        self.assertNotIn(LOCAL_ID, {n["id"] for n in exp["nodes"]})
        self.assertFalse(set(LOCAL_EDGES) & {e["id"] for e in exp["edges"]})
        for name in ("export.json", "index.html", "cards.json"):
            path = os.path.join(root, "build", name)
            if not os.path.exists(path):
                continue
            text = read_bytes(path).decode("utf-8")
            self.assertNotIn(LOCAL_ID, text, name)
            self.assertNotIn(LOCAL_NAME, text, name)
        self.assertEqual(report["counts"]["nodes"], 11)

    def test_sources_are_the_cited_ones_and_erased_ones_are_left_out(self):
        root = self.mini()
        repo = self.repo(root)
        self.assertEqual([s["id"] for s in build.export(repo)["sources"]], ["src-000a61a61d03", "src-e08998852112"])
        path = os.path.join(root, "sources", "index.jsonl")
        rows, _p = store.read_jsonl(path)
        rows.append(dict(rows[0], id="src-" + "1" * 12, title="Never cited"))
        rows[0] = dict(rows[0], erased=True)
        store.write_jsonl(path, rows)
        clear()
        exported = build.export(repo)["sources"]
        self.assertEqual([s["id"] for s in exported], ["src-e08998852112"])
        self.assertEqual(sorted(exported[0]), ["bytes", "captured_at", "id", "kind", "sha256", "title", "url"])

    def test_bundles_flatten_every_lock_entry(self):
        garden = self.mini("garden")
        g_repo = self.repo(garden)
        g_export = build.export(g_repo, "v1")
        g_data = util.canonical_bytes(g_export)
        g2t = _support.init_topic(self.tmp, "g2t")
        write_text(os.path.join(g2t, "imports", "garden", "export.json"), g_data.decode("utf-8"))
        store.write_json(os.path.join(g2t, "imports", "lock.json"),
                         {"format": 1, "imports": [lock_entry("garden", "mini-garden", g_data, g_export)]})
        clear()
        t_export = build.export(self.repo(g2t), "v1")
        self.assertEqual(sorted(t_export["bundled"]), ["garden"])
        t_data = util.canonical_bytes(t_export)
        market = _support.init_topic(self.tmp, "market")
        flat = t_export["bundled"]["garden"]["export"]
        flat_data = util.canonical_bytes(flat)
        write_text(os.path.join(market, "imports", "g2t", "export.json"), t_data.decode("utf-8"))
        write_text(os.path.join(market, "imports", "garden", "export.json"), flat_data.decode("utf-8"))
        store.write_json(os.path.join(market, "imports", "lock.json"), {"format": 1, "imports": [
            lock_entry("g2t", "test-g2t", t_data, t_export),
            lock_entry("garden", "mini-garden", flat_data, flat, via="g2t")]})
        clear()
        m_repo = self.repo(market)
        report = build.write(m_repo)  # validate passes: pins, bundles and shas agree
        self.assertEqual(report["imports"], ["g2t", "garden"])
        m_export = json.loads(read_bytes(os.path.join(market, "build", "export.json")).decode("utf-8"))
        self.assertEqual(records.check(m_export, "export"), [])
        self.assertEqual(sorted(m_export["bundled"]), ["g2t", "garden"])
        for ns, item in m_export["bundled"].items():
            self.assertEqual(item["export"]["bundled"], {}, ns)
            self.assertEqual(item["sha256"], lockfile.bundle_sha(item["export"]), ns)
        # a via entry's vendored file is the bundle object, so its pin is the bundle's sha
        self.assertEqual(m_export["bundled"]["garden"]["sha256"], util.sha256_hex(flat_data))
        self.assertEqual([e["ns"] for e in m_export["meta"]["imports"]], ["g2t", "garden"])
        self.assertEqual(build.bundle_from_lock(m_repo), m_export["bundled"])
        if commands.optional("compose") is not None:
            self.assertEqual(build._check_bundle(commands.optional("compose").bundle(m_repo), ["g2t", "garden"],
                                                 "compose.bundle"), m_export["bundled"])

    def test_a_bad_bundle_from_a_peer_is_refused(self):
        garden = self.mini("garden")
        g_export = build.export(self.repo(garden), "v1")
        good = {"garden": {"sha256": lockfile.bundle_sha(dict(g_export, bundled={})), "export": g_export}}
        self.assertEqual(sorted(build._check_bundle(good, ["garden"], "x")), ["garden"])
        with self.assertRaises(DataError):
            build._check_bundle({"garden": {"sha256": "0" * 64, "export": g_export}}, ["garden"], "x")
        with self.assertRaises(DataError):
            build._check_bundle(good, ["garden", "kitchen"], "x")

    def test_build_is_refused_while_validate_has_problems(self):
        root = _support.make_topic(self.tmp, "mini-broken", "mini")
        code, out, err = self.cli(root, "build")
        self.assertEqual(code, 1)
        self.assertIn("validate found", err)
        self.assertFalse(os.path.exists(os.path.join(root, "build", "export.json")))
        with self.assertRaises(Refused):
            build.write(self.repo(root))

    def test_out_folder(self):
        root = self.mini()
        code, _out, err = self.cli(root, "build", "--out", os.path.join(root, "graph"))
        self.assertEqual(code, 2, err)
        self.assertIn("inside the topic repo", err)
        elsewhere = os.path.join(self.tmp, "elsewhere")
        code, out, err = self.cli(root, "build", "--out", elsewhere)
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.isfile(os.path.join(elsewhere, "export.json")))
        self.assertFalse(os.path.exists(os.path.join(root, "build", "export.json")))

    def test_writes_are_all_or_nothing(self):
        root = self.mini()
        build.write(self.repo(root))
        before = read_bytes(os.path.join(root, "build", "export.json"))
        edit_node(root, "topic:mini", summary="A changed summary for the all-or-nothing check.")
        real = store.write_bytes
        calls = []

        def failing(path, data):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("disk full")
            return real(path, data)

        with mock.patch.object(store, "write_bytes", side_effect=failing):
            with self.assertRaises(OSError):
                build.write(self.repo(root), html=True)
        self.assertEqual(read_bytes(os.path.join(root, "build", "export.json")), before)
        self.assertFalse(os.path.exists(os.path.join(root, "build", "index.html")))


# cards ---------------------------------------------------------------------------------------------------------
class CardsTest(Base):
    def test_cards_file(self):
        if commands.optional("answers") is None:
            self.skipTest("the answers module is not built")
        root = self.mini()
        repo = self.repo(root)
        report = build.write(repo)
        cards = json.loads(read_bytes(os.path.join(root, "build", "cards.json")).decode("utf-8"))
        self.assertEqual(records.check(cards, "cards_file"), [])
        self.assertEqual(cards["meta"], {"data_hash": store.data_hash(repo), "kit": __version__})
        self.assertEqual(report["cards"], len(cards["cards"]))
        exported = {n["id"] for n in build.export(repo)["nodes"]}
        for card in cards["cards"]:
            self.assertIn(card["id"], exported)
            self.assertLessEqual(len(card["body"]) + len(card["follow"]) + 1, 1000)
            self.assertNotIn(LOCAL_ID, card["body"] + card["follow"])
        self.assertEqual([c["id"] for c in cards["cards"]], sorted(c["id"] for c in cards["cards"]))

    def test_mention_filter_is_whole_id(self):
        # the cards' filter is Redactor.scrub, the path every published text takes (review round 8)
        onto = types.SimpleNamespace(
            local_ids=["person:alex", "person:alex.b"], imports=[{"ns": "market"}],
            nodes={"person:alex": {"visibility": "local", "name": "Al"},
                   "person:alex.b": {"visibility": "shared", "name": "Alex B"}},
            repo=types.SimpleNamespace(manifest={"ns": "garden"}))
        red = build.Redactor(onto)
        self.assertTrue(red.names_one("has_member: person:alex; role:x"))
        self.assertTrue(red.names_one("ends with person:alex."))
        self.assertFalse(red.names_one("person:alex.b"))  # another record's id
        # no record has person:alex-2, so it is the left-out id with "-2" joined (review round 9)
        self.assertTrue(red.names_one("person:alex-2"))
        onto.nodes["person:alex-2"] = {"visibility": "shared", "name": "Alex Two"}
        self.assertFalse(build.Redactor(onto).names_one("person:alex-2 and person:alex.b"))  # records' own ids
        self.assertTrue(red.names_one("garden/person:alex"))  # the topic's own namespace: the left-out record
        self.assertFalse(red.names_one("market/person:alex"))  # an import's namespace: that import's record
        self.assertFalse(build.Redactor(hidden=[]).names_one("person:alex"))

    def test_cards_for_is_linear_in_the_node_count(self):
        # the ids left out of the export were found with a set rebuilt per id: 20,000 ids took seconds
        if os.environ.get("ONTO_SKIP_PERF") == "1":
            self.skipTest("ONTO_SKIP_PERF=1")
        repo = self.repo(self.mini())
        local = ["term:t%05d" % i for i in range(20000)]
        onto = types.SimpleNamespace(nodes={}, local_ids=local)
        answers = types.SimpleNamespace(build_cards=lambda _onto: {"cards": []})
        with mock.patch.object(build, "optional", return_value=answers):
            start = time.perf_counter()
            out = build.cards_for(repo, onto, local[:-5], [])
            took = time.perf_counter() - start
        self.assertEqual(out["cards"], [])
        self.assertLess(took, 1.0)

    def test_the_redactor_builds_its_patterns_only_when_a_text_needs_them(self):
        # a build that redacts no text pays nothing for a topic with many local records; the first text that
        # needs the patterns builds them, and the result is the same as building them up front
        local = ["person:p%05d" % i for i in range(2000)]
        lazy = build.Redactor(hidden=local, names=["Pat Green"])
        self.assertFalse(lazy._ready)
        self.assertEqual(lazy.hidden, set(local))
        self.assertTrue(lazy.scope_left_out(["person:p00001"]))
        self.assertFalse(lazy._ready)  # the scope check uses the cheap sets only
        text = "ask @person:p00007 and Pat Green about notes/person:p00042.txt"
        eager = build.Redactor(hidden=local, names=["Pat Green"])
        eager._prepare()
        self.assertEqual(lazy.scrub(text), eager.scrub(text))
        self.assertTrue(lazy._ready)
        self.assertNotIn("p00007", lazy.scrub(text))
        self.assertEqual(build.Redactor().scrub("plain"), "plain")

    def test_cards_without_the_answers_module(self):
        root = self.mini()
        notes = []
        with mock.patch.object(build, "optional", return_value=None):
            self.assertIsNone(build.cards_for(self.repo(root), graph.Ontology.load(self.repo(root)), [], notes))
        self.assertIn("cards: not built", notes[0])


# the viewer ----------------------------------------------------------------------------------------------------
class ViewerTest(Base):
    def parts(self):
        return build._read_viewer_parts()

    def test_template_contract(self):
        template, css, js = self.parts()
        for sentinel in (build.CSS_SENTINEL, build.JS_SENTINEL, build.DATA_SENTINEL):
            self.assertEqual(template.count(sentinel), 1, sentinel)
        self.assertEqual(template.count(build.DATA_TAG + build.DATA_SENTINEL + "</script>"), 1)
        self.assertIn('name="viewport"', template)
        self.assertIn("default-src 'none'", template)
        self.assertIn('id="theme"', template)
        # tokens on :root, redefined for dark mode twice: by the system setting (unless light is forced) and by
        # the toggle; an explicit body background
        self.assertRegex(css, r":root\s*\{[^}]*--bg:")
        self.assertRegex(css, r"@media \(prefers-color-scheme: dark\)\s*\{\s*:root:not\(\[data-theme=\"light\"\]\)")
        self.assertRegex(css, r":root\[data-theme=\"dark\"\]\s*\{[^}]*--bg:")
        self.assertRegex(css, r"body\s*\{[^}]*background: var\(--bg\)")
        self.assertIn("--gutter: 16px", css)
        # no external requests and no HTML parsing of data
        for name, text in (("template", template), ("css", css), ("js", js)):
            for banned in ("http://", "https://", "//cdn", "@import", "url(", "src=\""):
                self.assertNotIn(banned, text, (name, banned))
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "fetch(",
                       "XMLHttpRequest", "WebSocket", "import(", "sendBeacon", "new Image"):
            self.assertNotIn(banned, js, banned)
        self.assertIn("textContent", js)
        for route in ("node/", "kind/", "ns/", "richness"):
            self.assertIn('"%s' % route.rstrip("/"), js)

    def test_script_close_survives_embedding(self):
        root = self.mini()
        tricky = "Ends </script><script>alert(1)</script> and <!--<script> and </SCRIPT > too"
        edit_node(root, "topic:mini", summary=tricky)
        build.write(self.repo(root), html=True)
        html = read_bytes(os.path.join(root, "build", "index.html")).decode("utf-8")
        template, _css, _js = self.parts()
        self.assertEqual(html.lower().count("</script"), template.lower().count("</script"))
        start = html.index(build.DATA_TAG) + len(build.DATA_TAG)
        region = html[start:html.index("</script>", start)]
        self.assertNotIn("<!--", region)
        payload = build.extract_data(html)
        node = next(n for n in payload["export"]["nodes"] if n["id"] == "topic:mini")
        self.assertEqual(node["summary"], tricky)
        self.assertNotIn("bundled", payload["export"])
        # the decoy page of the test harness embeds the same way
        self.assertEqual(build.extract_data(_support.page_html({"a": tricky})), {"a": tricky})

    def test_sentinel_text_in_the_data_is_not_replaced(self):
        root = self.mini()
        edit_node(root, "topic:mini", summary="Mentions /*@CSS@*/ and /*@JS@*/ and @DATA@ literally.")
        build.write(self.repo(root), html=True)
        html = read_bytes(os.path.join(root, "build", "index.html")).decode("utf-8")
        node = next(n for n in build.extract_data(html)["export"]["nodes"] if n["id"] == "topic:mini")
        self.assertEqual(node["summary"], "Mentions /*@CSS@*/ and /*@JS@*/ and @DATA@ literally.")

    def test_viewer_payload(self):
        root = self.mini()
        build.write(self.repo(root), html=True)
        payload = build.extract_data(read_bytes(os.path.join(root, "build", "index.html")).decode("utf-8"))
        self.assertEqual(sorted(payload), ["bundled", "cards", "cited", "decisions", "edges", "export", "history",
                                           "imported", "kit", "needs", "richness"])
        self.assertEqual(sorted(payload["export"]), ["meta", "nodes", "sources"])
        self.assertEqual(len(payload["history"]), len(history.read(self.repo(root))))
        exp = build.export(self.repo(root))
        self.assertEqual(payload["edges"], [[e["src"], e["rel"], e["dst"], e["status"], int(e["background"]),
                                             e["note"], int(e["trust"] == "untrusted")] for e in exp["edges"]])
        self.assertEqual(len(build.EDGE_COLUMNS), 7)
        flagged = {(row[0], row[1], row[2]) for row in payload["edges"] if row[6]}
        self.assertEqual(flagged, {(e["src"], e["rel"], e["dst"]) for e in exp["edges"]
                                   if e["trust"] == "untrusted"})
        self.assertIn(("role:plot-coordinator", "owns", "deliverable:harvest-report"), flagged)
        cited = {sid: {"nodes": sorted(c["nodes"]), "edges": c["edges"]} for sid, c in payload["cited"].items()}
        self.assertEqual(sorted(cited), [s["id"] for s in exp["sources"]])
        self.assertIn("role:bed-steward", cited["src-e08998852112"]["nodes"])
        slim = next(n for n in payload["export"]["nodes"] if n["id"] == "topic:mini")
        self.assertNotIn("gaps", slim)
        exported = {n["id"] for n in payload["export"]["nodes"]}
        self.assertTrue(set(payload["needs"]) <= exported)
        for items in payload["needs"].values():
            self.assertLessEqual(len(items), build.NEEDS_PER_NODE)
        # the decisions in each node's scope, stored once and named per node (archived nodes keep theirs)
        decisions = payload["decisions"]
        self.assertEqual(decisions["nodes"], {"process:old-rota": [WATER_DEC], "process:watering": [WATER_DEC]})
        self.assertEqual(decisions["items"], {WATER_DEC: {
            "at": "2026-09-28T12:00:00Z", "question": "Keep the monthly steward rota or water every morning?",
            "chosen": "Water every morning"}})

    def test_scoped_decisions_match_like_the_ledger(self):
        node_ids = ["crop:mint", "crop:mint-2", "crop:mint.leaf", "bed:north", "Bed:South"]
        scopes = ["crop:mint", "crop", "crop:", "crop:mint.leaf#attrs.colour", "crop:mint-", "bed:north/east",
                  "./bed:south", "", "bed", "mint", "crop:mint.leaf.tip", "BED:NORTH"]
        decs = [{"id": "dec-%02d" % i, "at": "2026-09-%02dT00:00:00Z" % (i + 1), "question": "Q %d" % i,
                 "chosen": "yes", "options": [], "scope": [sc]} for i, sc in enumerate(scopes)]
        newest_first = sorted(decs, key=lambda d: d["at"], reverse=True)
        with mock.patch.object(ledger, "read_decisions", return_value=newest_first):
            got = build._node_decisions(None, node_ids)
        for nid in node_ids:
            want = [d["id"] for d in newest_first if ledger.scope_overlaps(d["scope"], [nid])]
            self.assertEqual(got["nodes"].get(nid, []), want[:build.DECISIONS_PER_NODE], nid)
        self.assertEqual(sorted(got["items"]), sorted({d for ids_ in got["nodes"].values() for d in ids_}))
        # an empty scope matches nothing, and the chosen option's label is what the page shows
        self.assertNotIn("dec-07", got["items"])
        labelled = dict(decs[0], options=[{"id": "yes", "label": "Yes, keep it"}])
        with mock.patch.object(ledger, "read_decisions", return_value=[labelled]):
            self.assertEqual(build._node_decisions(None, ["crop:mint"])["items"]["dec-00"]["chosen"], "Yes, keep it")
        own_words = dict(decs[0], chosen="other", chosen_text="Only in spring")
        with mock.patch.object(ledger, "read_decisions", return_value=[own_words]):
            self.assertEqual(build._node_decisions(None, ["crop:mint"])["items"]["dec-00"]["chosen"],
                             "Only in spring")

    def test_scoped_decisions_keep_the_newest_and_skip_superseded_ones(self):
        root = self.mini()
        repo = self.repo(root)
        made = []
        for i in range(build.DECISIONS_PER_NODE + 2):
            made.append(ledger.decide(repo, "Water bed %d at dawn?" % i, [], "yes", scope=["process:watering"]))
        newer = ledger.decide(repo, "Water bed 0 at dusk instead?", [], "yes", scope=["process:watering"],
                              supersedes=made[0]["id"])
        self.assertEqual(newer["supersedes"], made[0]["id"])
        clear()
        got = build._node_decisions(self.repo(root), ["process:watering", "role:bed-steward"])
        listed = got["nodes"]["process:watering"]
        self.assertEqual(len(listed), build.DECISIONS_PER_NODE)
        self.assertNotIn(made[0]["id"], got["items"])  # superseded
        active = ledger.read_decisions(self.repo(root), scope=["process:watering"])
        self.assertEqual(listed, [d["id"] for d in active][:build.DECISIONS_PER_NODE])  # newest first
        self.assertNotIn("role:bed-steward", got["nodes"])

    def test_peek_region_is_calm(self):
        _template, css, js = self.parts()
        for name, text in (("viewer.js", js), ("viewer.css", css)):
            self.assertEqual(text.count("/* peek */"), 1, name)
            self.assertEqual(text.count("/* peek-end */"), 1, name)
            region = text[text.index("/* peek */"):text.index("/* peek-end */")]
            self.assertGreater(len(region), 400, name)
            for word in CALM_BANNED:
                self.assertNotIn(word, region.lower(), (name, word))
            for token in ALERT_TOKENS:
                self.assertNotIn(token, region, (name, token))
            self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", region), name)  # tokens only, no literal colours
        js_region = js[js.index("/* peek */"):js.index("/* peek-end */")]
        self.assertIn("  function decisionRow(", js_region)
        # the shared helpers the panel calls are held to the same rule
        shared = "".join(js_function(js, name) for name in PEEK_SHARED_HELPERS)
        for helper in PEEK_SHARED_HELPERS:
            self.assertRegex(js_region, r"(?<![A-Za-z])" + helper + r"\(", helper)
        for word in CALM_BANNED:
            self.assertNotIn(word, shared.lower(), word)
        for token in ALERT_TOKENS:
            self.assertNotIn(token, shared, token)
        # the panel builds its own calm rows; the shared helpers with alert tones are never called from it
        for helper in ("nodeLink(", "markers(", "statusBadge(", "untrustedEdge(", "edgeNote(", "badge("):
            self.assertIsNone(re.search(r"(?<![A-Za-z])" + re.escape(helper), js_region + shared), helper)
        css_region = css[css.index("/* peek */"):css.index("/* peek-end */")]
        self.assertIn("@media (max-width: 719.98px)", css_region)  # the bottom sheet
        self.assertIn('.peek[hidden] { display: none; }', css_region)

    def test_rating_colours_are_readable_and_ordered(self):
        _template, css, _js = self.parts()
        blocks = {"light": css[:css.index("@media (prefers-color-scheme: dark)")],
                  "dark (system)": css[css.index("@media (prefers-color-scheme: dark)"):
                                       css.index(':root[data-theme="dark"]')],
                  "dark (picked)": css[css.index(':root[data-theme="dark"]'):css.index("*, *::before")]}
        for name, block in blocks.items():
            tokens = dict(re.findall(r"--(rate-[a-z]+-(?:bg|fg)):\s*(#[0-9a-fA-F]{6})", block))
            for lvl in ("vlow", "low", "mod", "high", "crit"):
                ratio = contrast(tokens["rate-%s-fg" % lvl], tokens["rate-%s-bg" % lvl])
                self.assertGreaterEqual(ratio, 4.5, (name, lvl, ratio))
            # critical is the deepest red in every theme
            self.assertLess(luminance(tokens["rate-crit-bg"]), luminance(tokens["rate-high-bg"]), name)
        # and it has a heavier border, so it never rests on colour alone
        rule = re.search(r"\.rate-critical \{([^}]*)\}", css).group(1)
        self.assertIn("border: 2px solid", rule)

    def test_peek_panel_and_theme_contract(self):
        template, css, js = self.parts()
        self.assertIn('<aside id="peek" class="peek" role="dialog" aria-label="Quick look" hidden></aside>', template)
        # the host's data-theme is read before anything else in the script
        first = js.index('document.documentElement.getAttribute("data-theme")')
        for later in ("JSON.parse(", "setAttribute(", "removeAttribute(", "textContent ="):
            self.assertLess(first, js.index(later), later)
        self.assertIn("MutationObserver", js)
        # rating colours are tokens, defined for light and twice for dark
        blocks = [css[:css.index("@media (prefers-color-scheme: dark)")],
                  css[css.index("@media (prefers-color-scheme: dark)"):css.index(':root[data-theme="dark"]')],
                  css[css.index(':root[data-theme="dark"]'):css.index("*, *::before")]]
        for block in blocks:
            for token in RATING_TOKENS:
                self.assertIn(token + ":", block, token)
        # every link opened apart from the page carries rel="noopener"
        self.assertEqual(js.count('"_blank"'), 1)
        self.assertEqual(js.count('setAttribute("rel", "noopener noreferrer")'), 1)

    def test_size_limits(self):
        root = self.mini()
        with mock.patch.object(build, "WARN_BYTES", 1000):
            report = build.write(self.repo(root), html=True)
        self.assertTrue(any("soft limit" in w for w in report["warnings"]))
        os.unlink(os.path.join(root, "build", "index.html"))
        before = read_bytes(os.path.join(root, "build", "export.json"))
        edit_node(root, "topic:mini", summary="Changed before an oversized build.")
        with mock.patch.object(build, "MAX_BYTES", 2000):
            with self.assertRaises(Refused) as caught:
                build.write(self.repo(root), html=True)
        self.assertIn("over the", caught.exception.message)
        self.assertFalse(os.path.exists(os.path.join(root, "build", "index.html")))
        self.assertEqual(read_bytes(os.path.join(root, "build", "export.json")), before)
        self.assertEqual((build.WARN_BYTES, build.MAX_BYTES), (6 * 1024 * 1024, 10 * 1024 * 1024))

    def test_skip_link_is_not_a_route(self):
        _template, _css, js = self.parts()
        self.assertIn('id="skip"', self.parts()[0])
        self.assertIn('getElementById("skip")', js)
        self.assertIn('hash === "view"', js)


@unittest.skipIf(NODE is None, "node is not installed; the viewer DOM checks need it")
class ViewerDomTest(Base):
    def page(self, root, initial, steps=(), html=None, **opts):
        """Run the built viewer under a minimal DOM; one snapshot for the load, then one per step. ``opts`` go to
        the harness (``root_theme``, ``stored``, ``system_dark``, ``no_observer``, ``prefer_view``)."""
        if html is None:
            html = read_bytes(os.path.join(root, "build", "index.html")).decode("utf-8")
        start = html.index(build.DATA_TAG) + len(build.DATA_TAG)
        data = html[start:html.index("</script>", start)]
        _template, _css, js = build._read_viewer_parts()
        harness = os.path.join(self.tmp, "dom-harness.js")
        write_text(harness, DOM_HARNESS)
        feed = dict(opts, js=js, data=data, initial=initial, steps=list(steps))
        feed = json.dumps(feed).encode("utf-8")
        proc = subprocess.run([NODE, harness], input=feed, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        return json.loads(proc.stdout.decode("utf-8"))

    def test_untrusted_edges_are_marked(self):
        root = self.mini()
        edit_edge(root, UNTRUSTED_EDGE, status="proposed", trust="untrusted", note="Seen in the handbook draft.")
        build.write(self.repo(root), html=True)
        lines = self.page(root, "#node/role:plot-coordinator")[0]["lines"]
        row = next(ln for ln in lines if "dataset:harvest-log" in ln)
        # both ends are trusted, so each marker belongs to the edge: one before it and one before its note
        self.assertTrue(row.startswith("[untrusted] "), row)
        self.assertEqual(row.count("[untrusted]"), 2, row)
        self.assertIn("[untrusted] Seen in the handbook draft.", row)
        self.assertIn("[draft link]", row)
        trusted = next(ln for ln in lines if "process:watering" in ln)
        self.assertNotIn("[untrusted]", trusted)
        # the fixture's own untrusted edge ends on an untrusted node: the edge and the node are both marked
        both = next(ln for ln in lines if "deliverable:harvest-report" in ln)
        self.assertEqual(both.count("[untrusted]"), 2, both)
        # the inverse side marks it as well
        lines = self.page(root, "#node/dataset:harvest-log")[0]["lines"]
        row = next(ln for ln in lines if "role:plot-coordinator" in ln)
        self.assertTrue(row.startswith("[untrusted] "), row)

    def test_skip_link_keeps_the_page(self):
        root = self.mini()
        build.write(self.repo(root), html=True)
        load, clicked, hashed = self.page(root, "#node/role:plot-coordinator", [{"click": "skip"}, {"hash": "#view"}])
        self.assertEqual(load["lines"][0], "Plot coordinator")
        self.assertEqual(clicked["hash"], "#node/role:plot-coordinator")  # the click changes no hash
        self.assertEqual(clicked["lines"], load["lines"])
        self.assertEqual(clicked["focus"], "view")
        self.assertEqual(hashed["lines"], load["lines"])  # #view reached another way still keeps the page
        self.assertEqual(hashed["focus"], "view")
        # a first load on #view has nothing to keep, so it shows the overview
        first = self.page(root, "#view")[0]
        self.assertEqual(first["lines"][0], "Mini garden")

    # the quick look ----------------------------------------------------------------------------------------------
    def built(self):
        root = self.mini()
        build.write(self.repo(root), html=True)
        return root

    def test_a_single_click_opens_the_quick_look(self):
        root = self.built()
        load, peeked, closed = self.page(root, "#kind/process", [{"peek": "#node/process:watering"},
                                                                {"key": "Escape"}])
        self.assertIsNone(load["peek"])
        # links in lists stay plain links to the full page, marked for the quick look
        link_ = next(a for a in load["view_attrs"] if a.get("href") == "#node/process:watering")
        self.assertEqual(link_.get("data-peek"), "process:watering")
        self.assertEqual(peeked["hash"], "#kind/process")  # the page under the panel stays
        self.assertEqual(peeked["lines"], load["lines"])
        panel = peeked["peek"]
        self.assertEqual(panel[:2], ["Watering", "Close"])
        self.assertIn("Beds are watered every morning before nine.", panel)
        self.assertIn("owned_by (1)", panel)  # relations grouped, with their counts
        self.assertIn("Nothing open.", panel)
        self.assertEqual(peeked["focus"], "peek-title")
        for part in ("Close", "Open", "Relations", "Open needs", "Decisions"):
            self.assertIn(part, panel, part)
        self.assertTrue(any(ln.startswith("Process process:watering") for ln in panel), panel)
        self.assertIn("Keep the monthly steward rota or water every morning?Chosen: Water every morning. " +
                      WATER_DEC + " 2026-09-28", panel)
        # Escape shuts it and gives focus back to the link that opened it
        self.assertIsNone(closed["peek"])
        self.assertEqual(closed["focus_href"], "#node/process:watering")
        self.assertEqual(closed["hash"], "#kind/process")

    def test_double_click_enter_and_open_go_to_the_full_page(self):
        root = self.built()
        target = "#node/process:watering"
        _load, dbl = self.page(root, "#kind/process", [{"dblclick": target}])
        self.assertEqual(dbl["hash"], target)
        self.assertIsNone(dbl["peek"])
        self.assertEqual(dbl["lines"][0], "Watering")
        self.assertIn("Facts", dbl["lines"])
        _load, entered = self.page(root, "#kind/process", [{"enter": target}])
        self.assertEqual(entered["hash"], target)
        self.assertIsNone(entered["peek"])
        _load, peeked, opened = self.page(root, "#kind/process", [{"peek": target}, {"button": "Open"}])
        self.assertEqual(peeked["hash"], "#kind/process")
        self.assertEqual(opened["hash"], target)
        self.assertIsNone(opened["peek"])
        self.assertIn("Facts", opened["lines"])
        # Open on the page already shown only shuts the panel and keeps the page
        here = "#node/role:plot-coordinator"
        load, _peeked, back, same = self.page(root, here, [{"peek": target}, {"peek": here}, {"button": "Open"}])
        self.assertEqual(back["peek"][0], "Plot coordinator")
        self.assertEqual((same["hash"], same["peek"], same["focus"]), (here, None, "view"))
        self.assertEqual(same["lines"], load["lines"])
        _load, peeked, closed = self.page(root, "#kind/process", [{"peek": target}, {"button": "Close"}])
        self.assertIsNone(closed["peek"])
        self.assertEqual(closed["hash"], "#kind/process")
        self.assertEqual(closed["focus_href"], target)
        # a click with a modifier key is the browser's own (a new tab), so no panel opens
        _load, ctrl = self.page(root, "#kind/process", [{"peek": target, "ctrl": True}])
        self.assertIsNone(ctrl["peek"])

    def test_a_double_click_opens_the_first_link_when_the_panel_lies_under_the_pointer(self):
        root = self.built()
        target = "#node/process:watering"
        other = "#node/role:plot-coordinator"  # a link inside the panel the first click opened
        for second in (other, "button:Close", "button:Open"):
            _load, dbl = self.page(root, "#kind/process", [{"dblclick": target, "second": second}])
            self.assertEqual((dbl["hash"], dbl["peek"]), (target, None), second)
            self.assertEqual(dbl["lines"][0], "Watering", second)
        # a later single click in the panel is a new first click: it moves the quick look, as before, and a double
        # click there opens the link it started on
        log = "#node/dataset:harvest-log"
        steps = [{"peek": target}, {"peek": other}, {"dblclick": log, "second": "button:Close"}]
        _load, first, inner, dbl = self.page(root, "#kind/process", steps)
        self.assertEqual((first["peek"][0], inner["peek"][0]), ("Watering", "Plot coordinator"))
        self.assertEqual((dbl["hash"], dbl["peek"], dbl["lines"][0]), (log, None, "Harvest log"))
        # a first click outside the links forgets the kept one, so a stray second click opens nothing
        steps = [{"peek": target}, {"click": "skip"}, {"peek": other, "detail": 2}]
        _load, _first, _skip, stray = self.page(root, "#kind/process", steps)
        self.assertEqual(stray["hash"], other)  # the old rule: a second click on a link opens that link

    def test_the_quick_look_in_a_folder_with_a_space_and_a_curly_apostrophe(self):
        base = os.path.join(self.tmp, "Shared drive \u2019s garden")
        os.makedirs(base)
        root = _support.make_topic(base, "mini", "mini")
        build.write(self.repo(root), html=True)
        _load, peeked = self.page(root, "#kind/process", [{"peek": "#node/process:watering"}])
        self.assertEqual(peeked["peek"][0], "Watering")

    def test_the_quick_look_moves_inside_itself_and_closes_on_a_new_page(self):
        root = self.built()
        steps = [{"peek": "#node/role:plot-coordinator"}, {"peek": "#node/dataset:harvest-log"},
                 {"key": "Escape"}, {"peek": "#node/role:plot-coordinator"}, {"hash": "#kinds"}]
        _load, first, inner, closed, again, moved = self.page(root, "#kind/role", steps)
        self.assertEqual(first["peek"][0], "Plot coordinator")
        self.assertEqual(inner["peek"][0], "Harvest log")
        self.assertEqual(inner["hash"], "#kind/role")
        self.assertEqual(closed["focus_href"], "#node/role:plot-coordinator")  # the first opener
        self.assertIsNotNone(again["peek"])
        self.assertIsNone(moved["peek"])
        self.assertEqual(moved["lines"][0], "Kinds")

    def test_the_quick_look_stays_calm(self):
        root = self.mini()
        edit_edge(root, UNTRUSTED_EDGE, status="proposed", trust="untrusted", note="Seen in the handbook draft.")
        edit_node(root, "role:plot-coordinator", status="proposed")
        build.write(self.repo(root), html=True)
        _load, peeked = self.page(root, "#kind/role", [{"peek": "#node/role:plot-coordinator"}])
        panel = peeked["peek"]
        row = next(ln for ln in panel if "dataset:harvest-log" in ln or "Harvest log" in ln)
        self.assertIn("[untrusted link]", row)  # still marked, in a neutral tone
        self.assertIn("[draft link]", row)
        self.assertTrue(any("[draft]" in ln for ln in panel), panel)
        for cls in peeked["peek_classes"]:
            for token in ("tone-warn", "tone-bad", "tone-ok", "rate-"):
                self.assertNotIn(token, cls)
        text = " ".join(panel).lower()
        for word in CALM_BANNED:
            self.assertNotIn(word, text, word)
        # the full page keeps its alert tones
        full = self.page(root, "#node/role:plot-coordinator")[0]
        self.assertIn("badge tone-bad", full["view_classes"])

    # theme -------------------------------------------------------------------------------------------------------
    def test_auto_keeps_and_follows_a_host_theme(self):
        root = self.built()
        steps = [{"host_theme": "light"}, {"host_theme": None}, {"host_theme": "dark"}]
        load, light, gone, dark = self.page(root, "#kinds", steps, root_theme="dark")
        self.assertEqual(load["theme"], "dark")  # never removed on load
        self.assertEqual(load["theme_button"], "Theme: Auto")
        self.assertIn("showing dark", load["theme_label"])
        self.assertIsNone(load["stored"])
        self.assertEqual(light["theme"], "light")
        self.assertIn("showing light", light["theme_label"])
        self.assertIsNone(gone["theme"])
        self.assertIn("showing light", gone["theme_label"])  # the system setting (light here)
        self.assertEqual(dark["theme"], "dark")
        self.assertEqual(dark["theme_button"], "Theme: Auto")
        # without a host value Auto writes nothing, as before
        plain = self.page(root, "#kinds", system_dark=True)[0]
        self.assertIsNone(plain["theme"])
        self.assertIn("showing dark", plain["theme_label"])
        # a browser without MutationObserver still keeps the host value
        old = self.page(root, "#kinds", root_theme="dark", no_observer=True)[0]
        self.assertEqual(old["theme"], "dark")

    def test_only_a_pick_replaces_the_host_theme(self):
        root = self.built()
        steps = [{"click": "theme"}, {"host_theme": "light"}, {"click": "theme"}, {"click": "theme"}]
        load, picked, host, second, auto = self.page(root, "#kinds", steps, root_theme="light")
        self.assertEqual(load["theme"], "light")
        self.assertEqual((picked["theme"], picked["stored"], picked["theme_button"]), ("dark", "dark", "Theme: Dark"))
        self.assertEqual(host["theme"], "dark")  # the user's pick holds over a later host write
        self.assertEqual((second["theme"], second["stored"]), ("light", "light"))
        # back to Auto: the host's value comes back and the stored pick is cleared
        self.assertEqual((auto["theme"], auto["stored"], auto["theme_button"]), ("light", None, "Theme: Auto"))
        # a stored pick wins over the host value on load
        stored = self.page(root, "#kinds", root_theme="dark", stored="light")[0]
        self.assertEqual((stored["theme"], stored["theme_button"]), ("light", "Theme: Light"))
        # with no host value, going back to Auto removes only the value the viewer wrote
        steps = [{"click": "theme"}, {"click": "theme"}, {"click": "theme"}]
        _load, one, two, three = self.page(root, "#kinds", steps)
        self.assertEqual([one["theme"], two["theme"], three["theme"]], ["dark", "light", None])
        self.assertIsNone(three["stored"])

    def test_a_host_write_equal_to_the_pick_is_still_the_hosts(self):
        root = self.built()
        steps = [{"click": "theme"}, {"host_theme": "light"}, {"click": "theme"}]
        _load, picked, host, auto = self.page(root, "#kinds", steps, root_theme="dark")
        self.assertEqual((picked["theme"], picked["theme_button"]), ("light", "Theme: Light"))
        self.assertEqual(host["theme"], "light")
        # back to Auto shows the host's last value, not the one it had before the pick
        self.assertEqual((auto["theme"], auto["theme_button"], auto["stored"]), ("light", "Theme: Auto", None))
        # the viewer's own writes are never taken for the host's: Auto after a pick brings the first value back
        steps = [{"click": "theme"}, {"click": "theme"}, {"click": "theme"}]
        _load, one, two, three = self.page(root, "#kinds", steps, root_theme="dark")
        self.assertEqual([one["theme"], two["theme"], three["theme"]], ["light", "dark", "dark"])
        self.assertEqual(three["theme_button"], "Theme: Auto")

    # ratings and outgoing links ----------------------------------------------------------------------------------
    def test_ratings_show_as_labelled_chips(self):
        root = self.mini()
        exp = build.export(self.repo(root))
        node = next(n for n in exp["nodes"] if n["id"] == "process:watering")
        node["attrs"] = dict(node["attrs"], ratings={
            "financial": {"impact": "critical", "inherent": "high", "residual": "moderate"},
            "customer": {"impact": "low", "inherent": "very_low", "residual": "very_low"},
            "regulatory": {"impact": "severe"}})
        html = build.viewer_html(exp)
        load = self.page(root, "#node/process:watering", html=html)[0]
        lines = load["lines"]
        dd = next(ln for ln in lines if "[impact critical]" in ln)
        self.assertIn("financial[impact critical][inherent high][residual moderate]", dd)
        self.assertIn("customer[impact low][inherent very low][residual very low]", dd)
        self.assertIn("regulatory[impact severe]", dd)  # an unknown level is shown as written, in a neutral tone
        classes = " ".join(load["view_classes"])
        for cls in ("rate-very-low", "rate-low", "rate-moderate", "rate-high", "rate-critical", "rate-other"):
            self.assertIn("badge chip " + cls, classes)
        self.assertNotIn('{"', dd)  # no raw JSON
        # the assessment pack's list form ("<dimension>.<measure>=<level>") shows as the same chips
        node["attrs"]["ratings"] = ["financial.impact=critical", "financial.inherent=high",
                                    "financial.residual=moderate", "customer.impact=low"]
        load = self.page(root, "#node/process:watering", html=build.viewer_html(exp))[0]
        dd = next(ln for ln in load["lines"] if "[impact critical]" in ln)
        self.assertIn("financial[impact critical][inherent high][residual moderate]", dd)
        self.assertIn("customer[impact low]", dd)
        self.assertNotIn("financial.impact", dd)
        self.assertIn("badge chip rate-critical", " ".join(load["view_classes"]))
        # a list that is not all rating items, and other object attrs, still show as JSON
        node["attrs"]["ratings"] = ["financial.impact=critical", "high"]
        load = self.page(root, "#node/process:watering", html=build.viewer_html(exp))[0]
        self.assertIn('["financial.impact=critical","high"]', load["lines"])
        node["attrs"]["ratings"] = ["high"]
        load = self.page(root, "#node/process:watering", html=build.viewer_html(exp))[0]
        self.assertIn('["high"]', load["lines"])

    def test_outgoing_links_use_noopener(self):
        root = self.mini()
        exp = build.export(self.repo(root))
        exp["sources"][0]["url"] = "https://garden.invalid/handbook"
        exp["sources"][1]["url"] = "javascript:alert(1)"
        html = build.viewer_html(exp)
        first = self.page(root, "#source/" + exp["sources"][0]["id"], html=html)[0]
        out = [a for a in first["view_attrs"] if a.get("href") == "https://garden.invalid/handbook"]
        self.assertEqual(out, [{"href": "https://garden.invalid/handbook", "rel": "noopener noreferrer",
                                "target": "_blank"}])
        second = self.page(root, "#source/" + exp["sources"][1]["id"], html=html)[0]
        self.assertFalse([a for a in second["view_attrs"] if "javascript" in a.get("href", "")])
        self.assertIn("javascript:alert(1)", second["lines"])  # shown as text only


# release -------------------------------------------------------------------------------------------------------
class TagTest(unittest.TestCase):
    def test_tag_naming(self):
        self.assertEqual(release.next_tag([]), ("v1", None))
        self.assertEqual(release.next_tag(["v1", "v3", "v10", "v2-rc", "version-9", "v0", "snapshot-7"]),
                         ("v11", "v10"))
        self.assertEqual(release.next_tag(["v0"]), ("v1", "v0"))

    def test_versions_rows(self):
        meta = {"data_hash": "a" * 64, "counts": {"nodes": 3, "edges": 2}, "richness": {"richness": 41,
                                                                                         "band": "working"}}
        row1 = release.versions_row("v1", "2026-09-28", meta, "first  |  cut")
        self.assertEqual(row1, "| v1 | 2026-09-28 | aaaaaaaaaaaa | 3 | 2 | 41 working | first \\| cut |")
        row2 = release.versions_row("v2", "2026-09-29", dict(meta, richness=None), "second")
        text = release.versions_text(None, "v2", row2)
        text = release.versions_text(text, "v1", row1)
        again = release.versions_text(text, "v1", row1.replace("first", "redone"))
        lines = [ln for ln in again.splitlines() if ln.startswith("| v")]
        self.assertEqual([ln.split(" | ")[0] for ln in lines], ["| v1", "| v2"])
        self.assertIn("redone", lines[0])
        self.assertIn("| n/a |", lines[1])
        self.assertTrue(again.startswith("# Versions\n"))


class ReleaseTest(Base):
    def test_dry_run_writes_nothing(self):
        root = self.git_topic()
        before = _support.snapshot(root, skip=(".git",))
        head = _support.git(root, "rev-parse", "HEAD")
        code, out, err = self.cli(root, "release", "--notes", "first")
        self.assertEqual(code, 0, err)
        self.assertIn("release v1 (dry run)", out)
        self.assertIn("dry run ok", out)
        self.assertEqual(_support.snapshot(root, skip=(".git",)), before)
        self.assertEqual(_support.git(root, "rev-parse", "HEAD"), head)
        self.assertEqual(_support.git(root, "tag", "--list"), "")

    def test_existing_tags_set_the_next_one(self):
        root = self.git_topic()
        _support.tag(root, "v1")
        _support.tag(root, "v3")
        _support.tag(root, "v4-rc")
        report = release.plan(self.repo(root))
        # tags are repo-wide; "previous" is this topic's own last release (its MANIFEST.json), and none was made
        self.assertEqual((report["tag"], report["previous"]), ("v4", None))
        with open(os.path.join(root, "MANIFEST.json"), "w", encoding="utf-8") as fh:
            json.dump({"version": "v3"}, fh)
        report = release.plan(self.repo(root))
        self.assertEqual((report["tag"], report["previous"]), ("v4", "v3"))

    def test_write_needs_notes(self):
        root = self.git_topic()
        code, out, _err = self.cli(root, "release", "--write")
        self.assertEqual(code, 1)
        self.assertIn("--notes is required", out)
        self.assertFalse(os.path.exists(os.path.join(root, "MANIFEST.json")))

    def test_validate_problems_fail_the_release(self):
        root = _support.make_topic(self.tmp, "mini-broken", "mini")
        code, out, _err = self.cli(root, "release", "--write", "--notes", "x")
        self.assertEqual(code, 1)
        self.assertIn("validate found 1 problem", out)
        self.assertIn("P10", out)
        self.assertFalse(os.path.exists(os.path.join(root, "MANIFEST.json")))

    def test_commit_writes_manifest_hashes_of_the_commit(self):
        root = self.git_topic()
        code, out, err = self.cli(root, "release", "--write", "--commit", "--notes", "first")
        self.assertEqual(code, 0, out + err)
        self.assertIn("committed", out)
        repo = self.repo(root)
        manifest = json.loads(read_bytes(os.path.join(root, "MANIFEST.json")).decode("utf-8"))
        self.assertEqual(records.check(manifest, "manifest"), [])
        self.assertEqual((manifest["version"], manifest["ns"], manifest["data_hash"]),
                         ("v1", "mini", store.data_hash(repo)))
        self.assertEqual(manifest["checks"], {"validate": "ok", "scan": "ok", "paths": "ok", "tests": "skipped"})
        committed = set(_support.git(root, "ls-tree", "-r", "--name-only", "HEAD").splitlines())
        self.assertEqual(set(manifest["files"]), committed - {"MANIFEST.json"})
        for rel, sha in manifest["files"].items():
            blob = _support.git(root, "rev-parse", "HEAD:%s" % rel)
            data = subprocess.run(["git", "-C", root, "cat-file", "blob", blob], stdout=subprocess.PIPE).stdout
            self.assertEqual(util.sha256_hex(data), sha, rel)
        for rel in ("build/export.json", "VERSIONS.md", "ledger/changes.jsonl", "metrics/history.jsonl",
                    "graph/nodes.jsonl", ".gitignore"):
            self.assertIn(rel, manifest["files"])
        export = json.loads(read_bytes(os.path.join(root, "build", "export.json")).decode("utf-8"))
        self.assertEqual(export["meta"]["version"], "v1")
        self.assertEqual(manifest["last_change"], export["meta"]["last_change"])
        self.assertEqual(manifest["packs"], {k: v["sha256"] for k, v in export["meta"]["packs"].items()})
        # the tag, a clean tree, the release change and point
        self.assertEqual(_support.git(root, "cat-file", "-t", "v1"), "tag")
        self.assertEqual(_support.git(root, "rev-parse", "v1^{commit}"), _support.git(root, "rev-parse", "HEAD"))
        self.assertEqual(_support.git(root, "status", "--porcelain"), "")
        last = ledger.read_changes(repo)[-1]
        self.assertEqual((last["type"], last["extra"]["version"]), ("release", "v1"))
        point = history.read(repo)[-1]
        self.assertEqual((point["kind"], point["label"]), ("release", "v1"))
        stamp = store.version_stamp(repo)
        self.assertEqual((stamp["version"], stamp["matches_release"], stamp["changes_after"]), ("v1", True, 0))
        versions = read_bytes(os.path.join(root, "VERSIONS.md")).decode("utf-8")
        self.assertIn("| v1 | 2026-09-28 | %s |" % manifest["data_hash"][:12], versions)
        # validate stays clean after a release, and a second release is v2
        code, out, err = self.cli(root, "validate")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.cli(root, "release", "--commit", "--notes", "second")
        self.assertEqual(code, 0, out + err)
        self.assertIn("the data is unchanged since v1", out)
        versions = read_bytes(os.path.join(root, "VERSIONS.md")).decode("utf-8")
        self.assertEqual(re.findall(r"^\| (v\d+) \|", versions, re.M), ["v1", "v2"])

    def test_write_alone_commits_nothing(self):
        root = self.git_topic()
        head = _support.git(root, "rev-parse", "HEAD")
        changes = read_bytes(os.path.join(root, "ledger", "changes.jsonl"))
        code, out, err = self.cli(root, "release", "--write", "--notes", "first")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(_support.git(root, "rev-parse", "HEAD"), head)
        self.assertEqual(_support.git(root, "tag", "--list"), "")
        self.assertEqual(read_bytes(os.path.join(root, "ledger", "changes.jsonl")), changes)
        manifest = json.loads(read_bytes(os.path.join(root, "MANIFEST.json")).decode("utf-8"))
        wanted = set(release.release_files(self.repo(root))) - {"MANIFEST.json"}
        self.assertEqual(set(manifest["files"]), wanted)
        for rel, sha in manifest["files"].items():
            self.assertEqual(store.file_sha256(os.path.join(root, rel)), sha, rel)
        self.assertFalse(any(release._is_runtime(rel) for rel in manifest["files"]))

    def test_commit_refuses_unrelated_dirty_files(self):
        root = self.git_topic()
        write_text(os.path.join(root, "draft-notes.txt"), "an unrelated draft\n")
        head = _support.git(root, "rev-parse", "HEAD")
        before = _support.snapshot(root, skip=(".git",))
        code, out, _err = self.cli(root, "release", "--commit", "--notes", "first")
        self.assertEqual(code, 1)
        self.assertIn("draft-notes.txt", out)
        self.assertIn("--allow-dirty", out)
        self.assertEqual(_support.snapshot(root, skip=(".git",)), before)
        self.assertEqual(_support.git(root, "rev-parse", "HEAD"), head)
        code, out, err = self.cli(root, "release", "--commit", "--allow-dirty", "--notes", "first")
        self.assertEqual(code, 0, out + err)
        changed = set(_support.git(root, "show", "--name-only", "--format=", "HEAD").splitlines())
        self.assertTrue(changed <= set(release.OUTPUTS), changed)
        self.assertTrue({"build/export.json", "VERSIONS.md", "MANIFEST.json", "ledger/changes.jsonl"} <= changed)
        self.assertIn("?? draft-notes.txt", _support.git(root, "status", "--porcelain"))
        manifest = json.loads(read_bytes(os.path.join(root, "MANIFEST.json")).decode("utf-8"))
        self.assertNotIn("draft-notes.txt", manifest["files"])

    def test_a_dirty_tracked_file_is_hashed_as_committed(self):
        root = self.git_topic()
        path = os.path.join(root, "README.txt")
        write_text(path, "committed text\n")
        _support.commit_all(root, "readme")
        write_text(path, "edited, not committed\n")
        code, out, err = self.cli(root, "release", "--commit", "--allow-dirty", "--notes", "first")
        self.assertEqual(code, 0, out + err)
        manifest = json.loads(read_bytes(os.path.join(root, "MANIFEST.json")).decode("utf-8"))
        self.assertEqual(manifest["files"]["README.txt"], util.sha256_hex(b"committed text\n"))

    def test_a_topic_inside_a_larger_work_tree(self):
        work = os.path.join(self.tmp, "work")
        topics = os.path.join(work, "topics")
        os.makedirs(topics)
        root = _support.make_topic(topics, "mini", "mini")
        write_text(os.path.join(work, ".gitignore"), "topics/*/.onto/\ntopics/*/inbox/\n")
        write_text(os.path.join(work, "README.txt"), "the work tree\n")
        _support.git_init(work)
        _support.commit_all(work, "work tree")
        write_text(os.path.join(work, "README.txt"), "edited outside the topic\n")
        code, out, _err = self.cli(root, "release", "--commit", "--notes", "first")
        self.assertEqual(code, 1)
        self.assertIn(":/README.txt", out)
        code, out, err = self.cli(root, "release", "--commit", "--allow-dirty", "--notes", "first")
        self.assertEqual(code, 0, out + err)
        manifest = json.loads(read_bytes(os.path.join(root, "MANIFEST.json")).decode("utf-8"))
        in_head = set(_support.git(work, "ls-tree", "-r", "--name-only", "HEAD", "--", "topics/mini").splitlines())
        self.assertEqual({"topics/mini/" + rel for rel in manifest["files"]}, in_head - {"topics/mini/MANIFEST.json"})
        self.assertIn("build/export.json", manifest["files"])
        changed = set(_support.git(work, "show", "--name-only", "--format=", "HEAD").splitlines())
        self.assertTrue(all(p.startswith("topics/mini/") for p in changed), changed)
        self.assertEqual(_support.git(work, "status", "--porcelain"), "M README.txt")  # still uncommitted

    def test_outputs_are_committed_even_when_build_is_ignored(self):
        root = self.git_topic()
        write_text(os.path.join(root, ".gitignore"), GITIGNORE + "build/\n")
        _support.commit_all(root, "ignore build")
        code, out, err = self.cli(root, "release", "--commit", "--notes", "first")
        self.assertEqual(code, 0, out + err)
        self.assertIn("build/export.json", _support.git(root, "ls-tree", "-r", "--name-only", "HEAD").splitlines())

    def test_a_failed_tag_says_the_commit_landed(self):
        root = self.git_topic()
        real = release._git_write

        def failing(where, *args):
            if args and args[0] == "tag":
                raise GitError("git tag failed: no signing key")
            return real(where, *args)

        with mock.patch.object(release, "_git_write", side_effect=failing):
            with self.assertRaises(GitError) as caught:
                release.run(self.repo(root), commit=True, notes="first")
        self.assertIn("tag that commit by hand", caught.exception.message)
        self.assertEqual(_support.git(root, "log", "-1", "--format=%s"), "v1: first")

    def test_a_failed_commit_puts_the_outputs_back(self):
        root = self.git_topic()
        before = _support.snapshot(root, skip=(".git", ".onto"))
        real = release._git_write

        def failing(where, *args):
            if args and args[0] == "commit":
                raise GitError("git commit failed: a hook said no")
            return real(where, *args)

        with mock.patch.object(release, "_git_write", side_effect=failing):
            with self.assertRaises(GitError):
                release.run(self.repo(root), commit=True, notes="first")
        clear()
        self.assertEqual(_support.snapshot(root, skip=(".git", ".onto")), before)
        self.assertEqual(_support.git(root, "tag", "--list"), "")
        self.assertEqual(_support.git(root, "status", "--porcelain"), "")

    def test_push(self):
        root = self.git_topic()
        origin = os.path.join(self.tmp, "origin.git")
        _support.git(self.tmp, "init", "-q", "--bare", origin)
        _support.git(root, "remote", "add", "origin", origin)
        code, out, err = self.cli(root, "release", "--push", "--notes", "first")
        self.assertEqual(code, 0, out + err)
        self.assertIn("pushed origin main and v1", out)
        self.assertEqual(_support.git(origin, "rev-parse", "refs/heads/main"), _support.git(root, "rev-parse", "HEAD"))
        self.assertEqual(_support.git(origin, "tag", "--list"), "v1")

    def test_push_needs_a_branch_and_origin(self):
        root = self.git_topic()
        code, out, _err = self.cli(root, "release", "--push", "--notes", "first")
        self.assertEqual(code, 1)
        self.assertIn("no remote named origin", out)
        _support.git(root, "checkout", "-q", "--detach")
        code, out, _err = self.cli(root, "release", "--push", "--notes", "first")
        self.assertEqual(code, 1)
        self.assertIn("HEAD is detached", out)
        self.assertEqual(_support.git(root, "tag", "--list"), "")
        self.assertFalse(os.path.exists(os.path.join(root, "MANIFEST.json")))

    def test_tracked_runtime_files_fail_the_paths_check(self):
        # a folder without the template's .gitignore, where "git add -A" committed the inbox and the kit state
        root = self.mini()
        _support.git_init(root)
        raw = "Call Pat on 212-555-0142 or write to pat@example.invalid\n"
        write_text(os.path.join(root, "inbox", "raw.md"), raw)
        write_text(os.path.join(root, ".onto", "ops.json"), "{}\n")
        _support.commit_all(root, "everything, the inbox too")
        origin = os.path.join(self.tmp, "origin.git")
        _support.git(self.tmp, "init", "-q", "--bare", origin)
        _support.git(root, "remote", "add", "origin", origin)
        before = _support.snapshot(root, skip=(".git",))
        advice = "git rm -r --cached --ignore-unmatch -- .onto inbox"
        for flags, problems in (((), 2), (("--write",), 2), (("--push",), 3)):  # --push: and the commit
            with self.subTest(flags=flags):
                code, out, err = self.cli(root, "release", "--notes", "first", *flags)
                self.assertEqual(code, 1, out + err)
                self.assertIn("paths: %d problem(s)" % problems, out)
                self.assertIn("inbox/raw.md: tracked by git", out)
                self.assertIn(".onto/ops.json: tracked by git", out)
                self.assertIn(advice, out)
                self.assertNotIn(raw.strip(), out + err)
                self.assertEqual(_support.snapshot(root, skip=(".git",)), before)
                self.assertEqual(_support.git(root, "tag", "--list"), "")
                self.assertEqual(_support.git(origin, "for-each-ref"), "")
        plan = json.loads(self.cli(root, "release", "--json")[1])["plan"]
        self.assertEqual(plan["paths"]["tracked"], [".onto/ops.json", "inbox/raw.md"])
        self.assertEqual(plan["checks"]["paths"], "2 problem(s)")
        # follow the advice: the tip is clean, but the unpushed history still holds the inbox, so --push is refused
        _support.git(root, *advice.split()[1:])
        write_text(os.path.join(root, ".gitignore"), GITIGNORE)
        _support.commit_all(root, "untrack the runtime paths")
        self.assertTrue(os.path.isfile(os.path.join(root, "inbox", "raw.md")))
        code, out, err = self.cli(root, "release", "--notes", "first")
        self.assertEqual(code, 0, out + err)
        self.assertIn("paths: ok", out)
        code, out, _err = self.cli(root, "release", "--push", "--notes", "first")
        self.assertEqual(code, 1, out)
        self.assertIn("paths: 2 problem(s)", out)  # the commit that added the files and the one that removed them
        self.assertIn("--push would publish that text in the branch history", out)
        self.assertEqual(_support.git(origin, "for-each-ref"), "")
        self.assertFalse(os.path.exists(os.path.join(root, "MANIFEST.json")))
        # a local release commits and tags a tree without them
        code, out, err = self.cli(root, "release", "--commit", "--notes", "first")
        self.assertEqual(code, 0, out + err)
        tree = _support.git(root, "ls-tree", "-r", "--name-only", "v1").splitlines()
        self.assertFalse([rel for rel in tree if rel.startswith(release.PRIVATE_RUNTIME)], tree)
        manifest = json.loads(read_bytes(os.path.join(root, "MANIFEST.json")).decode("utf-8"))
        self.assertFalse([rel for rel in manifest["files"] if rel.startswith(release.PRIVATE_RUNTIME)])

    def test_pushed_history_does_not_block_a_later_push(self):
        # runtime files that reached origin before stay there; only commits not yet on origin are checked
        root = self.git_topic()
        origin = os.path.join(self.tmp, "origin.git")
        _support.git(self.tmp, "init", "-q", "--bare", origin)
        _support.git(root, "remote", "add", "origin", origin)
        write_text(os.path.join(root, "inbox", "old.md"), "an old note\n")
        _support.git(root, "add", "-f", "inbox/old.md")
        _support.git(root, "commit", "-q", "-m", "an old inbox note")
        _support.git(root, "rm", "-q", "--cached", "inbox/old.md")
        _support.git(root, "commit", "-q", "-m", "untrack it")
        self.assertEqual(len(release.unpushed_private_commits(root)), 2)
        _support.git(root, "push", "-q", "origin", "main")
        self.assertEqual(release.unpushed_private_commits(root), [])
        code, out, err = self.cli(root, "release", "--push", "--notes", "first")
        self.assertEqual(code, 0, out + err)
        self.assertIn("pushed origin main and v1", out)

    def test_outside_git(self):
        root = self.mini()
        code, out, err = self.cli(root, "release")
        self.assertEqual(code, 0, out + err)
        self.assertIn("outside git", out)
        code, out, _err = self.cli(root, "release", "--commit", "--notes", "first")
        self.assertEqual(code, 1)
        self.assertIn("not in a git work tree", out)
        code, out, err = self.cli(root, "release", "--write", "--notes", "first")
        self.assertEqual(code, 0, out + err)
        manifest = json.loads(read_bytes(os.path.join(root, "MANIFEST.json")).decode("utf-8"))
        self.assertIn("graph/nodes.jsonl", manifest["files"])
        self.assertNotIn(".onto/lock", manifest["files"])

    def test_scan_hits_stop_the_release(self):
        root = self.git_topic()
        secret = _support.fake_secret("github")
        write_text(os.path.join(root, "handoff.md"), "token: %s\n" % secret)
        before = _support.snapshot(root, skip=(".git",))
        code, out, err = self.cli(root, "release", "--commit", "--allow-dirty", "--notes", "first")
        self.assertEqual(code, 2, out + err)
        self.assertIn('handoff.md: github (starts "%s")' % secret[:6], out)
        self.assertNotIn(secret, out + err)
        self.assertEqual(_support.snapshot(root, skip=(".git",)), before)
        code, out, _err = self.cli(root, "release", "--json")
        self.assertEqual(code, 2)
        self.assertNotIn(secret, out)

    def test_notes_are_scanned(self):
        root = self.git_topic()
        secret = _support.fake_secret("anthropic")
        code, out, err = self.cli(root, "release", "--write", "--notes", "key %s" % secret)
        self.assertEqual(code, 2, out + err)
        self.assertIn("--notes: anthropic", out)
        self.assertNotIn(secret, out + err)
        self.assertFalse(os.path.exists(os.path.join(root, "VERSIONS.md")))

    def test_notes_with_a_hit_are_never_echoed(self):
        root = self.git_topic()
        secret = _support.fake_secret("github")
        code, out, err = self.cli(root, "release", "--notes", "fix %s" % secret, "--json")
        self.assertEqual(code, 2, out + err)
        self.assertNotIn(secret, out + err)
        plan = json.loads(out)["plan"]
        self.assertEqual(plan["notes"], release.NOTES_WITHHELD % 1)
        self.assertIn({"path": "--notes", "kind": "github", "prefix": secret[:6], "line": None}, plan["scan"]["hits"])
        deny = os.path.join(self.tmp, "deny2.txt")
        write_text(deny, DENY_TERM + "\n")
        code, out, err = self.cli(root, "release", "--notes", "the %s plan" % DENY_TERM, "--denylist", deny, "--json")
        self.assertEqual(code, 2, out + err)
        self.assertNotIn(DENY_TERM, out + err)
        self.assertEqual(json.loads(out)["plan"]["notes"], release.NOTES_WITHHELD % 1)
        code, out, err = self.cli(root, "release", "--notes", "the %s plan" % DENY_TERM, "--denylist", deny)
        self.assertEqual(code, 2)
        self.assertNotIn(DENY_TERM, out + err)
        # clean notes are shown as given
        code, out, err = self.cli(root, "release", "--notes", "first", "--json")
        self.assertEqual((code, json.loads(out)["plan"]["notes"]), (0, "first"), err)

    def test_notes_go_through_the_sanitizer(self):
        # the notes land in VERSIONS.md, the change log, the commit and the tag message (which erase cannot reach):
        # they get the check decision and checkpoint text get, not only the release patterns
        root = self.git_topic()
        head = _support.git(root, "rev-parse", "HEAD")
        before = _support.snapshot(root, skip=(".git",))
        spoken = "First release; the shed door code is 4812 and the wifi password is Garden2026"
        self.assertEqual(release.scan_text("--notes", spoken), [])  # the release patterns alone miss it
        for flags in ((), ("--commit",)):
            with self.subTest(flags=flags):
                code, out, err = self.cli(root, "release", "--notes", spoken, "--json", *flags)
                self.assertEqual(code, 2, out + err)
                self.assertNotIn("Garden2026", out + err)
                self.assertNotIn("4812", out + err)
                plan = json.loads(out)["plan"]
                self.assertEqual(plan["notes"], release.NOTES_REFUSED % "credential")
                self.assertEqual(plan["notes_check"], {"refused": ["credential"], "redacted": []})
        code, out, err = self.cli(root, "release", "--commit", "--notes", spoken)
        self.assertEqual(code, 2)
        self.assertIn("--notes holds data that may not be stored (credential)", out)
        self.assertNotIn("Garden2026", out + err)
        self.assertEqual(_support.snapshot(root, skip=(".git",)), before)
        self.assertEqual((_support.git(root, "rev-parse", "HEAD"), _support.git(root, "tag", "--list")), (head, ""))
        # personal data is redacted as policy.personal says, in every place the notes land
        notes = "First release. Questions to %s or %s" % (NOTES_EMAIL, NOTES_PHONE)
        code, out, err = self.cli(root, "release", "--notes", notes, "--json")
        plan = json.loads(out)["plan"]
        self.assertEqual((code, plan["notes_check"]), (0, {"refused": [], "redacted": ["email", "phone"]}), err)
        self.assertNotIn(NOTES_EMAIL, out + err)
        code, out, err = self.cli(root, "release", "--commit", "--notes", notes)
        self.assertEqual(code, 0, out + err)
        self.assertIn("--notes: redacted email, phone", out)
        self.assertNotIn(NOTES_EMAIL, out + err)
        clean = "First release. Questions to [redacted:email] or [redacted:phone]"
        places = {
            "VERSIONS.md": read_bytes(os.path.join(root, "VERSIONS.md")).decode("utf-8"),
            "commit": _support.git(root, "log", "-1", "--format=%B"),
            "tag": _support.git(root, "cat-file", "tag", "v1"),
            "change": ledger.read_changes(self.repo(root))[-1]["summary"],
        }
        for where, text in places.items():
            self.assertIn(clean, text, where)
            self.assertNotIn(NOTES_EMAIL, text, where)
            self.assertNotIn("555-0142", text, where)
        code, out, err = self.cli(root, "validate")
        self.assertEqual(code, 0, out + err)

    def test_notes_with_personal_data_the_policy_refuses_fail(self):
        root = self.git_topic()
        path = os.path.join(root, "ontology.json")
        manifest = json.loads(read_bytes(path).decode("utf-8"))
        personal = dict(store.DEFAULT_POLICY["personal"], email="refuse")
        manifest["policy"] = dict(manifest.get("policy") or {}, personal=personal)
        store.write_json(path, manifest)
        code, out, err = self.cli(root, "release", "--write", "--notes", "Questions to %s" % NOTES_EMAIL, "--json")
        self.assertEqual(code, 1, out + err)
        self.assertNotIn(NOTES_EMAIL, out + err)
        plan = json.loads(out)["plan"]
        self.assertEqual(plan["notes"], release.NOTES_REFUSED % "email")
        self.assertIn("--notes holds data that may not be stored (email)", " ".join(plan["failures"]))
        self.assertFalse(os.path.exists(os.path.join(root, "VERSIONS.md")))

    @unittest.skipIf(os.name != "posix", "needs an executable git hook")
    def test_a_failed_first_commit_restores_the_index(self):
        # a repo with no commit yet: "git reset" has no HEAD, so the staged outputs must be put back another way
        hooks = os.path.join(self.tmp, "hooks")
        write_text(os.path.join(hooks, "pre-commit"), "#!/bin/sh\nexit 1\n")
        os.chmod(os.path.join(hooks, "pre-commit"), 0o755)
        for ns, staged_first in (("fresh", False), ("staged", True)):
            with self.subTest(staged_first=staged_first):
                root = self.mini(ns)
                write_text(os.path.join(root, ".gitignore"), GITIGNORE)
                _support.git_init(root)
                _support.git(root, "config", "core.hooksPath", hooks)
                if staged_first:
                    _support.git(root, "add", "-A")
                index = _support.git(root, "ls-files", "-s")
                status = _support.git(root, "status", "--porcelain")
                before = _support.snapshot(root, skip=(".git", ".onto"))
                code, out, err = self.cli(root, "release", "--commit", "--allow-dirty", "--notes", "first")
                self.assertEqual(code, 1, out + err)
                self.assertIn("git commit failed", err)
                self.assertEqual(_support.git(root, "ls-files", "-s"), index)
                self.assertEqual(_support.git(root, "status", "--porcelain"), status)
                self.assertEqual(_support.snapshot(root, skip=(".git", ".onto")), before)
                self.assertEqual(_support.git(root, "tag", "--list"), "")
                if staged_first:
                    self.assertIn("A  ledger/changes.jsonl", status)
                else:
                    self.assertEqual(index, "")

    @unittest.skipIf(not POSIX_PERMS, "needs file permissions that bind (posix, not root)")
    def test_an_unreadable_file_fails_the_release(self):
        root = self.git_topic()
        locked = os.path.join(root, "locked.txt")
        write_text(locked, "text the scan cannot see\n")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o644)
        code, out, err = self.cli(root, "release")
        self.assertEqual(code, 1, out + err)
        self.assertIn("locked.txt: cannot read", out)
        self.assertIn("cannot read 1 file(s) of the commit set", out)
        with self.assertRaises(DataError):
            release.scan(self.repo(root))

    def test_denylist(self):
        root = self.git_topic()
        deny = os.path.join(self.tmp, "denylist.txt")
        write_text(deny, "# private terms\n%s\n" % DENY_TERM)
        code, out, err = self.cli(root, "release", "--denylist", deny)
        self.assertEqual(code, 0, out + err)
        self.assertIn("denylist on", out)
        edit_node(root, "topic:mini", summary="Planted near the %s gate." % DENY_TERM)
        _support.commit_all(root, "a denylisted term")
        code, out, _err = self.cli(root, "release", "--denylist", deny)
        self.assertEqual(code, 2)
        self.assertIn('denylist (term starts "%s")' % DENY_TERM[:6], out)
        self.assertNotIn(DENY_TERM, out)
        with mock.patch.dict(os.environ, {"ONTO_DENYLIST": deny}):
            code, out, _err = self.cli(root, "release")
        self.assertEqual(code, 2)
        inside = os.path.join(root, "inbox", "denylist.txt")
        write_text(inside, DENY_TERM + "\n")
        code, out, _err = self.cli(root, "release", "--denylist", inside)
        self.assertEqual(code, 1)
        self.assertIn("keep it outside the repo", out)
        code, out, _err = self.cli(root, "release", "--denylist", os.path.join(self.tmp, "missing.txt"))
        self.assertEqual(code, 1)
        self.assertIn("cannot read the denylist", out)


# erased content in history a push would publish ----------------------------------------------------------------
WATERER = "person:ottoline-brackwater"
WATERER_NAME = "Ottoline Brackwater"
WATERER_TEXT = "Ottoline Brackwater waters the north beds.\n"


class ErasedHistoryPushTest(Base):
    """Regression: --push published a person added in one commit and erased in a later one, both not yet on origin,
    because the paths check only looked at .onto/ and inbox/ files in the unpushed history."""

    def origin(self, root):
        origin = os.path.join(self.tmp, "origin.git")
        _support.git(self.tmp, "init", "-q", "--bare", origin)
        _support.git(root, "remote", "add", "origin", origin)
        return origin

    def add_source(self, root):
        clear()
        entry, _dup = sources.add(self.repo(root), WATERER_TEXT, "note", "Rota")
        clear()
        return entry["id"]

    def add_person(self, root):
        sid = self.add_source(root)
        quote = {"src": sid, "loc": "L1-L1", "quote": "Ottoline Brackwater waters the north beds", "by": "user"}
        mutate.apply_ops(self.repo(root), [{"n": 1, "op": "add_node", "node": {
            "id": WATERER, "kind": "person", "name": WATERER_NAME, "summary": "Waters the north beds."},
            "status": "confirmed", "trust": "user", "conf": 0.9, "prov": [quote]}],
            by="user", change_type="apply", summary="add the waterer")
        clear()
        return sid

    def erase(self, root, *ids):
        repo = self.repo(root)
        dec = ledger.decide(repo, "Erase personal data?", [], "yes", scope=list(ids))
        clear()
        ingest.erase(repo, ids[0], dec["id"])
        clear()

    def found(self, root):
        clear()
        repo = self.repo(root)
        return [(hit["commit"], hit["ids"]) for hit in release.unpushed_erased_commits(repo, release.git_state(root))]

    def origin_text(self, origin):
        return _support.git(origin, "log", "-p", "--all") if _support.git(origin, "for-each-ref") else ""

    def test_push_refuses_unpushed_commits_that_hold_erased_content(self):
        root = self.git_topic()
        origin = self.origin(root)
        sid = self.add_person(root)
        people = _support.commit_all(root, "people")
        self.erase(root, WATERER, sid)
        # the erase is not committed yet: HEAD still holds the person, so the advice starts with committing it
        code, out, err = self.cli(root, "release", "--push", "--allow-dirty", "--notes", "first")
        self.assertEqual(code, 1, out + err)
        self.assertIn("commit the erase, then git update-ref -d HEAD", out)
        _support.commit_all(root, "erase")
        self.assertEqual(self.found(root), [(people[:7], sorted([WATERER, sid]))])
        # the dry run says so; it is a note, not a failure
        code, out, err = self.cli(root, "release")
        self.assertEqual(code, 0, out + err)
        self.assertIn("note: 1 commit(s) not yet on origin hold content that onto erase removed since", out)
        self.assertIn("--push will refuse until they are squashed into one", out)
        plan = json.loads(self.cli(root, "release", "--json")[1])["plan"]
        self.assertEqual(plan["paths"]["erased"], [{"commit": people[:7], "ids": sorted([WATERER, sid])}])
        self.assertEqual(plan["checks"]["paths"], "ok")
        # --push is refused, names the commit and gives the squash for the user to run; nothing is written
        before = _support.snapshot(root, skip=(".git",))
        code, out, err = self.cli(root, "release", "--write", "--commit", "--push", "--notes", "first")
        self.assertEqual(code, 1, out + err)
        self.assertIn("paths: 1 problem(s)", out)
        self.assertIn("commit %s: holds content onto erase removed since (%s, %s)" % (people[:7], WATERER, sid), out)
        self.assertIn("--push would publish it in the branch history", out)
        self.assertIn("git update-ref -d HEAD (nothing on origin precedes them), then git commit", out)
        self.assertNotIn(WATERER_NAME, out + err)
        self.assertEqual(_support.snapshot(root, skip=(".git",)), before)
        self.assertEqual(_support.git(root, "tag", "--list"), "")
        self.assertEqual(_support.git(origin, "for-each-ref"), "")
        # following the advice pushes a history without the erased content
        _support.git(root, "update-ref", "-d", "HEAD")
        _support.git(root, "commit", "-q", "-m", "topic data, erased")
        self.assertEqual(self.found(root), [])
        code, out, err = self.cli(root, "release", "--push", "--notes", "first")
        self.assertEqual(code, 0, out + err)
        self.assertIn("pushed origin main and v1", out)
        pushed = self.origin_text(origin)
        self.assertNotIn(WATERER_NAME, pushed)
        self.assertNotIn("waters the north beds", pushed)

    def test_the_squash_goes_onto_the_branch_on_origin(self):
        root = self.git_topic()
        origin = self.origin(root)
        _support.git(root, "push", "-q", "origin", "main")
        sid = self.add_person(root)
        people = _support.commit_all(root, "people")
        self.erase(root, WATERER, sid)
        _support.commit_all(root, "erase")
        code, out, err = self.cli(root, "release", "--push", "--notes", "first")
        self.assertEqual(code, 1, out + err)
        self.assertIn("for example %s" % people[:7], out)
        self.assertIn("git reset --soft origin/main, then git commit -m", out)
        self.assertNotIn(WATERER_NAME, self.origin_text(origin))
        _support.git(root, "reset", "-q", "--soft", "origin/main")
        _support.git(root, "commit", "-q", "-m", "people, erased")
        code, out, err = self.cli(root, "release", "--push", "--notes", "first")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn(WATERER_NAME, self.origin_text(origin))

    def test_content_already_on_origin_does_not_block_a_push(self):
        # the erase cannot take back what origin has published; a later commit that only carries it over is fine
        root = self.git_topic()
        self.origin(root)
        sid = self.add_person(root)
        _support.commit_all(root, "people")
        _support.git(root, "push", "-q", "origin", "main")
        mutate.apply_ops(self.repo(root), [{"n": 1, "op": "add_node", "node": {
            "id": "term:mulch", "kind": "term", "name": "Mulch"}, "status": "confirmed", "trust": "user",
            "conf": 0.7, "prov": [{"src": sid, "loc": "L1-L1", "by": "user"}]}],
            by="user", change_type="apply", summary="add mulch")
        _support.commit_all(root, "mulch")
        self.erase(root, WATERER, sid)
        _support.commit_all(root, "erase")
        self.assertEqual(self.found(root), [])
        code, out, err = self.cli(root, "release", "--push", "--notes", "first")
        self.assertEqual(code, 0, out + err)
        self.assertIn("pushed origin main and v1", out)

    def test_a_kept_original_and_a_merged_branch_are_checked(self):
        root = self.git_topic()
        self.origin(root)
        sid = self.add_source(root)
        _support.commit_all(root, "a source")
        _support.git(root, "push", "-q", "origin", "main")
        write_text(os.path.join(root, "sources", sid + ".orig.md"), WATERER_TEXT)
        kept = _support.commit_all(root, "keep the original")
        _support.git(root, "checkout", "-q", "-b", "side")
        self.add_person(root)
        side = _support.commit_all(root, "people on a side branch")
        _support.git(root, "checkout", "-q", "main")
        _support.git(root, "merge", "-q", "--no-ff", "-m", "merge the side branch", "side")
        self.erase(root, WATERER, sid)
        self.assertFalse(os.path.exists(os.path.join(root, "sources", sid + ".orig.md")))
        _support.commit_all(root, "erase")
        # the side commit holds the person and a quote of the erased source
        self.assertEqual(self.found(root), [(kept[:7], [sid]), (side[:7], sorted([WATERER, sid]))])

    def test_a_topic_inside_a_larger_work_tree(self):
        work = os.path.join(self.tmp, "work")
        root = _support.make_topic(os.path.join(work, "topics"), "mini", "mini")
        write_text(os.path.join(work, ".gitignore"), "topics/*/.onto/\ntopics/*/inbox/\n")
        write_text(os.path.join(work, "other", "README.txt"), "another folder\n")
        _support.git_init(work)
        _support.commit_all(work, "work tree")
        self.origin(work)
        _support.git(work, "push", "-q", "origin", "main")
        sid = self.add_person(root)
        people = _support.commit_all(work, "people")
        write_text(os.path.join(work, "other", "README.txt"), "edited\n")
        _support.commit_all(work, "outside the topic")
        self.erase(root, WATERER, sid)
        _support.commit_all(work, "erase")
        self.assertEqual(self.found(root), [(people[:7], sorted([WATERER, sid]))])
        code, out, _err = self.cli(root, "release", "--push", "--notes", "first")
        self.assertEqual(code, 1, out)
        self.assertIn("git reset --soft origin/main, then git commit", out)

    def test_a_history_that_cannot_be_read_refuses_the_push(self):
        root = self.git_topic()
        origin = self.origin(root)
        sid = self.add_person(root)
        self.erase(root, WATERER, sid)
        _support.commit_all(root, "people, erased")
        with mock.patch.object(release, "_raw_log", return_value=None):
            code, out, err = self.cli(root, "release", "--push", "--notes", "first")
            self.assertEqual(code, 1, out + err)
            self.assertIn("could not read the commits not yet on origin", out)
            self.assertEqual(self.cli(root, "release")[0], 0)  # a plan without --push only skips the note
        self.assertEqual(_support.git(origin, "for-each-ref"), "")
        code, out, err = self.cli(root, "release", "--push", "--notes", "first")
        self.assertEqual(code, 0, out + err)


# scan ----------------------------------------------------------------------------------------------------------
class ScanTest(Base):
    def folder(self):
        folder = os.path.join(self.tmp, "scan-me")
        write_text(os.path.join(folder, "clean.md"), "Nothing to see here.\n")
        return folder

    def test_clean(self):
        code, out, err = _support.run_cli(["scan", self.folder()])
        self.assertEqual(code, 0, err)
        self.assertIn("clean", out)
        self.assertEqual(release.scan([self.folder()]), [])

    def test_a_gitdir_pointer_file_is_skipped_like_the_git_folder(self):
        # a git worktree (or submodule) holds a .git file naming the main checkout's path: git metadata, never
        # committed, so it is skipped like a .git folder
        folder = self.folder()
        write_text(os.path.join(folder, ".git"), "gitdir: /elsewhere/%s/.git/worktrees/x\n"
                   % _support.fake_secret("github"))
        code, out, err = _support.run_cli(["scan", folder])
        self.assertEqual(code, 0, err)
        self.assertEqual(release.scan([folder]), [])

    def test_hits_exit_2_with_prefix_only_output(self):
        folder = self.folder()
        secrets_ = [_support.fake_secret(kind) for kind in ("github", "aws")]
        write_text(os.path.join(folder, "sub", "notes.txt"), "a %s\nb %s\n" % tuple(secrets_))
        code, out, err = _support.run_cli(["scan", folder])
        self.assertEqual(code, 2, err)
        self.assertIn('notes.txt: github (starts "%s")' % secrets_[0][:6], out)
        self.assertIn("aws", out)
        self.assertIn("2 hit(s)", out)
        code, jout, _err = _support.run_cli(["scan", folder, "--json"])
        self.assertEqual(code, 2)
        hits = json.loads(jout)["hits"]
        self.assertEqual(sorted(h["kind"] for h in hits), ["aws", "github"])
        for text in (out, err, jout):
            for secret in secrets_:
                self.assertNotIn(secret, text)
        self.assertTrue(all(len(h["prefix"]) <= 6 for h in release.scan([folder])))

    def test_denylist_hit(self):
        folder = self.folder()
        write_text(os.path.join(folder, "plan.md"), "line one\nvisit the %s soon\n" % DENY_TERM.upper())
        write_text(os.path.join(folder, "%s.md" % DENY_TERM), "fine\n")
        deny = os.path.join(self.tmp, "private-denylist.txt")
        write_text(deny, "# comment\n\n%s\n" % DENY_TERM)
        code, out, err = _support.run_cli(["scan", folder, "--denylist", deny])
        self.assertEqual(code, 2, err)
        self.assertIn('plan.md:2: denylist (term starts "%s")' % DENY_TERM[:6], out)
        self.assertEqual(len([ln for ln in out.splitlines() if "denylist (term" in ln]), 2)
        self.assertNotIn("visit the", out.lower())
        self.assertNotIn(DENY_TERM, out.replace("%s.md" % DENY_TERM, ""))
        with mock.patch.dict(os.environ, {"ONTO_DENYLIST": deny}):
            code, _out, _err = _support.run_cli(["scan", folder])
        self.assertEqual(code, 2)

    def test_denylist_inside_the_scanned_folder_is_refused(self):
        folder = self.folder()
        deny = os.path.join(folder, "denylist.txt")
        write_text(deny, DENY_TERM + "\n")
        code, _out, err = _support.run_cli(["scan", folder, "--denylist", deny])
        self.assertEqual(code, 1)
        self.assertIn("keep it outside", err)

    @unittest.skipIf(not POSIX_PERMS, "needs file permissions that bind (posix, not root)")
    def test_an_unreadable_file_is_exit_1(self):
        folder = self.folder()
        locked = os.path.join(folder, "locked.txt")
        secret = _support.fake_secret("github")
        write_text(locked, secret + "\n")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o644)
        code, out, err = _support.run_cli(["scan", folder])
        self.assertEqual(code, 1, out + err)
        self.assertIn("locked.txt: cannot read", out)
        self.assertIn("scan: 1 file(s), no hits, 1 unreadable: the scan is incomplete", out)
        self.assertNotIn("clean", out)
        with self.assertRaises(DataError):
            release.scan([folder])
        # a hit elsewhere still wins (exit 2), and the unreadable file is named too
        write_text(os.path.join(folder, "open.txt"), _support.fake_secret("aws") + "\n")
        code, out, _err = _support.run_cli(["scan", folder, "--json"])
        self.assertEqual(code, 2)
        result = json.loads(out)
        self.assertEqual([os.path.basename(p) for p in result["unreadable"]], ["locked.txt"])
        self.assertEqual([h["kind"] for h in result["hits"]], ["aws"])
        # once readable, the secret is found
        os.chmod(locked, 0o644)
        code, out, _err = _support.run_cli(["scan", folder])
        self.assertEqual(code, 2)
        self.assertIn('locked.txt: github (starts "%s")' % secret[:6], out)

    @unittest.skipIf(not POSIX_PERMS, "needs file permissions that bind (posix, not root)")
    def test_a_folder_that_cannot_be_listed_is_exit_1(self):
        folder = self.folder()
        closed = os.path.join(folder, "closed")
        write_text(os.path.join(closed, "inside.txt"), "hidden\n")
        os.chmod(closed, 0)
        self.addCleanup(os.chmod, closed, 0o755)
        code, out, err = _support.run_cli(["scan", folder])
        self.assertEqual(code, 1, out + err)
        self.assertIn("closed: cannot read", out)
        self.assertIn("scan: 1 file(s)", out)

    def test_missing_path_is_exit_1(self):
        code, _out, err = _support.run_cli(["scan", os.path.join(self.tmp, "nope")])
        self.assertEqual(code, 1)
        self.assertIn("no such file or folder", err)

    def test_default_is_the_repo(self):
        root = self.mini()
        write_text(os.path.join(root, "inbox", "dropped.txt"), _support.fake_secret("slack") + "\n")
        code, out, _err = _support.run_cli(["scan"], root)
        self.assertEqual(code, 2)
        self.assertIn("inbox/dropped.txt: slack", out)
        # a repo scan as a release sees it leaves the gitignored inbox out
        self.assertEqual(release.scan(self.repo(root)), [])


if __name__ == "__main__":
    unittest.main()
