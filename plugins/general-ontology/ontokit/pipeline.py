"""Proposals: prepare, review and commit (C.12). The graph changes only through ``commit``.

``prepare`` checks a draft (schema, references, quotes, the reason rule), fills the kit's annotations
(``annot.expect``, ``matches``, ``conflict``, ``assigned_id``), the impact, the new terms and the priority, and saves
the proposal to ``proposals/pending/``. A structural problem (schema, ids, kinds, relations, unknown references,
quote failures, read-only or archived targets, a missing reason) refuses the draft: the problems are attached to the
``Refused`` error with the annotated proposal, and nothing is saved.

Before any check, the author's text goes through the rules source text follows (``sanitize_draft``): a credential
anywhere refuses the draft naming only its kind, personal data the policy redacts is redacted in names, summaries,
aliases, values, notes and reasons, and NaN or Infinity refuse it (P02), as does a lone surrogate (half a character,
which no file can hold). Invisible characters (a soft hyphen, a zero-width space) leave names and aliases
(``strip_invisible_names``). Control characters in that text, and line
breaks in one-line fields, are P02; a provenance location that leads nowhere (a line span outside the text, ``Q:``
on a source that is not an interview answer, a node id without an ``imp:`` source) is P11.

The same draft proposed again while it is open returns that proposal with ``note: "already proposed"``, unless the
data moved under the open copy since (``stale_ops``: applying it would raise ``Conflict``): then the draft is saved
anew, checked against the current data, and the stale copy is marked ``superseded``. A draft whose id hash another
proposal holds with other content gets a longer id (C.2); one whose hash a proposal with the same content holds (a
rejected or replaced copy) hashes a repeat counter instead, so the same draft can always be proposed again. The id is
picked and the file saved under the write lock.

``review`` records one verdict per op (``accept``, ``draft``, ``reject`` or ``edit`` with a replacement op, which is
checked again). ``commit`` resolves ``$ref`` handles, assigns node ids, decides status and trust per C.7, and hands
the resolved ops to ``mutate.apply_ops``; the proposal then moves to ``proposals/done/`` with its ``applied`` record,
and each applied op keeps the annotations of the checks run at apply time (``annot.assigned_id`` names the node it
created, ``-2`` when the planned id was taken since). A merge that would move an edge onto an archived one is refused
(``archived``).
A drafted update passes its trust on as ``draft_trust``: text it changes is only as trusted as its source. An
``add_edge`` naming an existing edge adds provenance only (its note goes through ``update_edge``), and one naming an
imported edge is refused (``read_only``). Committing an applied proposal returns the stored record with
``note: "already applied"``; a proposal counts as applied only when ``apply_ops`` logged its change (the change line
carries the data hashes before and after), never because a record-only line names it.

Statuses move forward only: ``pending`` -> ``reviewed`` -> ``applied``; ``pending`` or ``reviewed`` may also end as
``rejected`` (every op rejected) or ``superseded`` (a later proposal names it in ``supersedes``).

Problems and warnings are ``{n, code, message}``. Codes: ``P02`` schema or text, ``P07`` id grammar, ``P08`` kind
or attrs (the message lists the known kinds), ``P09`` relation (it lists the known relations, or what a relation
links), ``P11`` a location that leads nowhere or a source that is erased (``onto erase``: nothing is drafted or
applied from a forgotten source, so a proposal that cites one, pending since before the erase or new, is refused),
``P12`` archive block, ``P20`` pack change, ``ref`` unknown or
duplicate reference, ``quote`` quote not found in the cited lines, ``read_only`` an imported target, ``archived`` an
archived target, ``reason`` the reason rule, ``prov`` provenance required, ``status`` a proposal in the wrong status;
the warning ``redacted`` counts what the sanitizer redacted, and ``invisible`` the invisible characters (a soft hyphen,
a zero-width space) taken out of names and aliases, which print as nothing and would key as another name.
"""

from __future__ import annotations

import copy
import glob
import math
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import entities, ids, ledger, lockfile, mutate, needs, packs, records, render, schema_lite, secrets, sources
from . import store, util
from .errors import Conflict, DataError, NotFound, OntoError, Refused, UsageError
from .graph import EDGES, NODES, Ontology

PENDING = "proposals/pending"
DONE = "proposals/done"
OPEN = ("pending", "reviewed")
FINAL = ("applied", "rejected", "superseded")
VERDICTS = ("accept", "draft", "reject", "edit")
KIT_KEYS = ("id", "status", "created", "base", "priority", "checks", "impact", "new_terms", "review", "applied")
DEFAULT_CONF = 0.7
REASON_MIN = 20
MAX_MATCHES = 10
PACK_OPS = ("add_kind", "add_relation", "add_field", "map_kinds")
NODE_SET = ("name", "summary", "aliases", "visibility", "conf")
EDGE_SET = ("note", "background", "conf")
STRUCTURAL = ("P02", "P07", "P08", "P09", "P11", "P12", "P20", "ref", "quote", "read_only", "archived", "reason",
              "prov", "status")
LINES_RE = re.compile(r"^L([1-9][0-9]*)-L([1-9][0-9]*)\Z")
ID_LOC_RE = re.compile(r"^(?:[a-z][a-z0-9-]{0,31}/)?[a-z][a-z0-9-]{0,31}:")
LIST_MAX = 400  # characters of a "known kinds" or "known relations" list in a message


# files ---------------------------------------------------------------------------------------------------------
def _file(repo: store.Repo, folder: str, prop_id: str) -> str:
    return repo.path("%s/%s.json" % (folder, prop_id))


def _read_all(repo: store.Repo) -> Dict[str, Tuple[str, Dict[str, Any]]]:
    """``{id: (folder, proposal)}`` for every readable proposal file."""
    out: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    for folder in (PENDING, DONE):
        for path in sorted(glob.glob(os.path.join(repo.path(folder), "prop-*.json"))):
            try:
                value = store.read_json(path)
            except (DataError, OSError):
                continue
            if isinstance(value, dict) and isinstance(value.get("id"), str):
                out.setdefault(value["id"], (folder, value))
    return out


def load(repo: store.Repo, prop_id: str) -> Dict[str, Any]:
    """A proposal by id, from ``pending/`` or ``done/``."""
    if not isinstance(prop_id, str) or not ids.PROP_RE.match(prop_id):
        raise UsageError("not a proposal id: %r (expected prop-YYYYMMDD-xxxxxx)" % (prop_id,))
    for folder in (PENDING, DONE):
        path = _file(repo, folder, prop_id)
        if os.path.isfile(path):
            try:
                value = store.read_json(path)
            except FileNotFoundError:  # moved from pending/ to done/ between the check and the read
                continue
            if not isinstance(value, dict):
                raise DataError("%s: not a JSON object" % path)
            return value
    raise NotFound("proposal %s: not found in proposals/pending or proposals/done" % prop_id, searched=prop_id)


def _save(repo: store.Repo, proposal: Dict[str, Any]) -> None:
    """Write the proposal into the folder its status belongs to and remove it from the other one."""
    errors = records.check(proposal, "proposal")
    if errors:
        raise DataError("the proposal fails its schema (a kit bug): %s" % "; ".join(errors[:3]), problems=errors)
    folder = PENDING if proposal["status"] in OPEN else DONE
    other = DONE if folder == PENDING else PENDING
    with store.write_lock(repo):
        store.write_json(_file(repo, folder, proposal["id"]), proposal)
        stale = _file(repo, other, proposal["id"])
        if os.path.isfile(stale):
            os.unlink(stale)


def pending(repo: store.Repo) -> List[Dict[str, Any]]:
    """Open proposals (pending or reviewed), sorted by (-priority, created, id)."""
    out = [p for folder, p in _read_all(repo).values() if folder == PENDING and p.get("status") in OPEN]
    out.sort(key=lambda p: (-int(p.get("priority") or 0), str(p.get("created") or ""), str(p.get("id"))))
    return out


# the checks ----------------------------------------------------------------------------------------------------
def _author(op: Any) -> Any:
    """An op without the fields the kit writes."""
    if not isinstance(op, dict):
        return op
    return {k: v for k, v in op.items() if k not in ("annot", "n")}


def _normalized(op: Any) -> Any:
    """The author fields of an op with the defaults filled, so the same proposal hashes the same once annotated."""
    out = _author(op)
    if isinstance(out, dict) and out.get("op") in ("add_node", "add_edge"):
        out = dict(out)
        out.setdefault("conf", DEFAULT_CONF)
        out.setdefault("basis", "inferred")
    return out


def _problem(out: List[Dict[str, Any]], n: Any, code: str, message: str) -> None:
    out.append({"n": n, "code": code, "message": message})


def _name_list(names: Sequence[str]) -> str:
    text = ", ".join(names)
    if len(text) <= LIST_MAX:
        return text
    return text[:LIST_MAX].rsplit(", ", 1)[0] + ", ..."


# the author's text ---------------------------------------------------------------------------------------------
_CONTROL_ANY_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f  ]")


def _non_finite(value: Any, path: Tuple[Any, ...] = ()) -> List[str]:
    """``$.path`` of every NaN or infinite number in ``value``: JSON has no such numbers, so none may be stored."""
    if isinstance(value, float) and not math.isfinite(value):
        return [schema_lite.json_path(path)]
    if isinstance(value, dict):
        return [p for k in sorted(value, key=str) for p in _non_finite(value[k], path + (str(k),))]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _non_finite(v, path + (i,))]
    return []


def _defang(value: Any) -> Any:
    """A copy of ``value`` whose strings show control characters as ``\\uXXXX`` (a refused draft is echoed back in
    its preview; its text must not drive a terminal)."""
    if isinstance(value, str):
        return _CONTROL_ANY_RE.sub(lambda m: "\\u%04x" % ord(m.group(0)), value)
    if isinstance(value, dict):
        return {k: _defang(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_defang(v) for v in value]
    return value


def sanitize_draft(repo: store.Repo, obj: Dict[str, Any], shape: str = "draft") -> Dict[str, int]:
    """Run the source sanitizer over the free text of a draft, a proposal or one op (``records.text_slots``), in
    place, before anything is checked or saved. A credential anywhere (quotes and pack text included) refuses the
    whole draft, naming only its kinds, never the value. Personal data the policy redacts is redacted in names,
    summaries, aliases, values, notes and reasons, exactly as ingest redacts it in source text; quotes are left as
    they are (a quote must match its source, which was sanitized when it was stored). Returns ``{kind: count}``."""
    slots = records.text_slots(obj, shape)
    kinds = sorted({kind for container, key, _role, _path in slots for kind, _m in secrets.scan_str(container[key])})
    if kinds:
        raise Refused("the proposal text holds credentials (%s); nothing was saved. Remove them and propose again"
                      % ", ".join(kinds), kinds=kinds, problems=[{"n": None, "code": "P17", "message":
                                                                  "holds %s" % k} for k in kinds])
    sanitizer = sources.default_sanitizer()
    policy = repo.policy
    counts: Dict[str, int] = {}
    for container, key, role, path in slots:
        if role not in (records.LINE, records.BLOCK):
            continue
        text = container[key]
        try:
            clean, found = sanitizer(text, policy)
        except Refused as exc:
            found_kinds = list(exc.extra.get("kinds") or []) if hasattr(exc, "extra") else []
            raise Refused("%s holds data the policy refuses (%s); nothing was saved. Remove it and propose again"
                          % (schema_lite.json_path(path), ", ".join(found_kinds) or "see the policy"),
                          kinds=found_kinds)
        if clean.endswith("\n") and not text.endswith("\n"):
            clean = clean[:-1]
        if clean != text:
            container[key] = clean
        for kind, count in (found or {}).items():
            if count:
                counts[kind] = counts.get(kind, 0) + int(count)
    if counts:
        _dedupe_alias_lists(obj)  # a redacted alias may now equal another one
    return counts


def _name_slot(path: Tuple[Any, ...]) -> bool:
    """True for the path of a name or an alias in a draft or an op (not an attrs value called ``name``)."""
    if "attrs" in path:
        return False
    return (bool(path) and path[-1] == "name") or (len(path) > 1 and path[-2] == "aliases") or \
        (bool(path) and path[-1] == "aliases")


def strip_invisible_names(obj: Dict[str, Any], shape: str = "draft") -> int:
    """Drop invisible characters (``util.strip_invisible``: a soft hyphen or a zero-width space copied from a web
    page) from the names and aliases of a draft or an op, in place: "Ba\u00adsil" prints as "Basil" and must not
    become a second node beside it. Returns how many characters went."""
    count = 0
    for container, key, role, path in records.text_slots(obj, shape):
        if role != records.LINE or not _name_slot(path):
            continue
        text = container[key]
        clean = util.strip_invisible(text)
        if clean != text:
            container[key] = clean
            count += len(text) - len(clean)
    if count:
        _dedupe_alias_lists(obj)
    return count


def _dedupe_alias_lists(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "aliases" and isinstance(item, list):
                value[key] = [a for i, a in enumerate(item) if a not in item[:i]]
            else:
                _dedupe_alias_lists(item)
    elif isinstance(value, list):
        for item in value:
            _dedupe_alias_lists(item)


def _redaction_note(counts: Dict[str, int]) -> str:
    return "redacted %s (policy.personal); the proposal holds the redacted text" % ", ".join(
        "%s %d" % kv for kv in sorted(counts.items()))


class _Checker(object):
    """Runs the C.12 checks over a list of ops against one graph."""

    def __init__(self, repo: store.Repo, onto: Ontology, source: Optional[str], mcp: bool = False) -> None:
        self.repo = repo
        self.mcp = mcp  # follow-up calls in the caller's words (``render.call``)
        self.onto = onto
        self.registry = onto.registry
        self.source = source
        self.problems: List[Dict[str, Any]] = []
        self.warnings: List[Dict[str, Any]] = []
        self.quotes_checked = 0
        self.quotes_failed: List[Dict[str, Any]] = []
        self.handles: Dict[str, str] = {}  # $ref -> assigned id
        self.handle_kinds: Dict[str, str] = {}
        self.handle_op: Dict[str, Any] = {}
        self.assigned: Set[str] = set()
        self.impact: List[str] = []
        self.new_terms: List[str] = []
        self.severity = 0
        self.texts: Dict[str, str] = {}
        self.pack = copy.deepcopy(store.read_json(repo.path(packs.LOCAL_PACK), None) or packs.empty_local_pack())
        self.new_kinds: Dict[str, Dict[str, Any]] = {}
        self.new_relations: Dict[str, Dict[str, Any]] = {}
        self.question_ids = {q.get("id") for q in self.registry.questions()}
        self.impsrcs = {ids.impsrc(str(e["ns"]), str(e.get("commit") or "")) for e in onto.imports
                        if e.get("commit")}
        self.decisions = ledger.all_decisions(repo)
        self.rejected_handles: Dict[str, Any] = {}

    # helpers -------------------------------------------------------------------------------------------------
    def bad(self, n: Any, code: str, message: str) -> None:
        _problem(self.problems, n, code, message)

    def warn(self, n: Any, code: str, message: str) -> None:
        _problem(self.warnings, n, code, message)

    def touch(self, rid: str) -> None:
        if rid and not rid.startswith("$") and rid not in self.impact:
            self.impact.append(rid)

    def text_of(self, src: str) -> Optional[str]:
        if src not in self.texts:
            try:
                self.texts[src] = sources.read(self.repo, src)
            except (NotFound, DataError, OSError, UsageError):
                self.texts[src] = None  # type: ignore
        return self.texts[src]

    def import_from(self, rid: str) -> str:
        ns = self.onto.ns_of(rid)
        entry = lockfile.entry({"imports": self.onto.imports}, ns) if ns != "self" else None
        if entry:
            return str(entry.get("from") or entry.get("name") or ns)
        return ns

    def kind_decl(self, kind: str) -> Optional[Dict[str, Any]]:
        if kind in self.new_kinds:
            return self.new_kinds[kind]
        return self.registry.kind(kind)

    def fields(self, kind: str) -> Dict[str, Any]:
        decl = self.kind_decl(kind) or {}
        return dict(decl.get("fields") or {})

    def relation(self, rel: str, ns: Optional[str] = None) -> Optional[Dict[str, Any]]:
        return self.new_relations.get(rel) or self.registry.relation(rel, ns)

    def rel_ns(self, src: Optional[str], dst: Optional[str]) -> Optional[str]:
        """The import whose declaration of a relation governs an edge between ``src`` and ``dst``: the one import
        both ends sit in, else None (``graph.Ontology.rel_ns``)."""
        if not src or not dst:
            return None
        ns = self.onto.ns_of(src)
        return ns if ns != "self" and ns == self.onto.ns_of(dst) else None

    def known_kinds(self) -> str:
        """The kinds a local node may take, for a P08 message (so an agent picks one instead of inventing one)."""
        names = sorted({k for k in self.registry.kinds() if "/" not in k} | set(self.new_kinds))
        return _name_list(names)

    def known_relations(self) -> str:
        names = sorted({r for r in self.registry.relations() if "/" not in r} | set(self.new_relations))
        return _name_list(names)

    def links(self, rel: str, ns: Optional[str] = None) -> str:
        """``from role, process to dataset`` for a relation, for a P09 message."""
        decl = self.relation(rel, ns) or {}

        def side(value: Any) -> str:
            return "any kind" if value == "*" or (isinstance(value, list) and "*" in value) else \
                ", ".join(str(v) for v in value or []) or "none"

        return "from %s to %s%s" % (side(decl.get("from")), side(decl.get("to")),
                                    " (either way)" if decl.get("symmetric") else "")

    def fitting(self, sk: str, dk: str, ns: Optional[str] = None) -> str:
        """The relations that may link a ``sk`` to a ``dk``, for a P09 message (so an agent picks one that fits)."""
        names = sorted({r for r in self.registry.relations() if "/" not in r} | set(self.new_relations))
        return _name_list([r for r in names if self.allowed(r, sk, dk, ns)])

    def kind_of(self, ref: str) -> str:
        if ref.startswith("$"):
            return self.handle_kinds.get(ref, "")
        return self.onto.kind_of(ref)

    def allowed(self, rel: str, src_kind: str, dst_kind: str, ns: Optional[str] = None) -> bool:
        if rel in self.new_relations:
            decl = self.new_relations[rel]

            def ok(side: Any, kind: str) -> bool:
                return side == "*" or (isinstance(side, list) and ("*" in side or kind in side))

            if ok(decl.get("from"), src_kind) and ok(decl.get("to"), dst_kind):
                return True
            return bool(decl.get("symmetric")) and ok(decl.get("from"), dst_kind) and ok(decl.get("to"), src_kind)
        return self.registry.allowed(rel, src_kind, dst_kind, ns)

    def resolve(self, n: Any, ref: Any, what: str, allow_source: bool = False,
                allow_edge: bool = False) -> Optional[str]:
        """The id a reference names (a ``$ref`` becomes its assigned id), or None with a problem."""
        if not isinstance(ref, str) or not ref:
            self.bad(n, "ref", "%s: no id given" % what)
            return None
        if ref.startswith("$"):
            if ref in self.handles:
                return self.handles[ref]
            if ref in self.rejected_handles:
                self.bad(n, "ref", "%s: %s comes from op %s, which is rejected"
                         % (what, ref, self.rejected_handles[ref]))
            else:
                self.bad(n, "ref", "%s: %s is not created by an earlier op of this proposal" % (what, ref))
            return None
        rid = self.onto.own_local(ref)
        if rid in self.onto.nodes and (allow_source or rid not in self.onto.virtual):
            return rid
        if allow_edge and rid in self.onto.edges:
            return rid
        if rid in self.assigned:
            self.bad(n, "ref", "%s: %s is created by this proposal; refer to it by its $ref" % (what, rid))
            return None
        candidates: List[str] = []
        try:
            res = self.onto.resolve(rid)
            candidates = [res["id"]] if res.get("id") else list(res.get("candidates") or [])[:5]
        except NotFound as exc:
            self.bad(n, "ref", "%s: %s is %s" % (what, rid, exc.message))
            return None
        if not candidates:
            candidates = self.close_ids(rid)
        hint = "; did you mean %s?" % ", ".join(candidates) if candidates else ""
        self.bad(n, "ref", "%s: %s is not in the ontology%s" % (what, rid, hint))
        return None

    def close_ids(self, rid: str, most: int = 5) -> List[str]:
        """Ids of the same kind within edit distance 2 of a missed id (a typo in the slug)."""
        kind, _sep, slug = rid.rpartition(":")
        if not kind:
            return []
        prefix = kind + ":"
        found = []
        for nid in self.onto.nodes:
            if nid.startswith(prefix) and util.edit_distance(slug, nid[len(prefix):], cap=2) <= 2:
                found.append(nid)
        return sorted(found)[:most]

    def target(self, n: Any, ref: Any, what: str, edge: bool = False, node: bool = True) -> Optional[str]:
        """A writable local target: exists, local, not archived."""
        rid = self.resolve(n, ref, what, allow_edge=edge)
        if rid is None:
            return None
        if isinstance(ref, str) and ref.startswith("$"):
            return rid
        if rid in self.onto.edges:
            if not edge:
                self.bad(n, "ref", "%s: %s is an edge; use update_edge" % (what, rid))
                return None
            if rid in self.onto.edge_origin:
                self.bad(n, "read_only", "%s: imported edges are read-only; change it in %s"
                         % (what, self.onto.edge_origin[rid][0]))
                return None
        elif not node:
            self.bad(n, "ref", "%s: %s is a node; use update_node" % (what, rid))
            return None
        elif self.onto.ns_of(rid) != "self":
            self.bad(n, "read_only", "%s: imported nodes are read-only; change it in %s"
                     % (what, self.import_from(rid)))
            return None
        rec = self.onto.record(rid) or {}
        if rec.get("status") == "archived":
            self.bad(n, "archived", "%s: %s is archived, and archived is final" % (what, rid))
            return None
        self.touch(rid)
        return rid

    def check_prov(self, n: Any, prov: Any) -> None:
        for p in prov or []:
            if not isinstance(p, dict):
                continue
            src = str(p.get("src") or "")
            loc = str(p.get("loc") or "")
            quote = p.get("quote")
            if src.startswith("imp:"):
                if src not in self.impsrcs:
                    m = ids.IMPSRC_RE.match(src)
                    pinned = [e for e in self.onto.imports if m and e.get("ns") == m.group(1)]
                    self.bad(n, "ref", "provenance %s is not a pinned import%s" % (src, (
                        "; %s is now pinned at %s: run %s to re-cite it"
                        % (m.group(1), str(pinned[0].get("commit") or "")[:12] or "no commit",
                           render.call(self.mcp, "import", action="update", ns=m.group(1),
                                       ref=pinned[0].get("ref") or "<its ref>"))) if pinned else ""))
                elif quote:
                    self.bad(n, "quote", "a quote needs a stored source; %s is an import" % src)
                continue
            if src not in self.onto.sources:
                self.bad(n, "ref", "provenance %s is not in the sources index" % src)
                continue
            entry = self.onto.sources.get(src) or {}
            if entry.get("erased"):  # forgotten under a decision: nothing may be drafted from it any more (G.6)
                self.bad(n, "P11", "provenance %s: the source is erased; cite a source that is kept" % src)
                continue
            stamp = sources.is_stamp(loc)  # a T<hh:mm:ss> cue: checked against the text's cue lines
            text = self.text_of(src) if stamp or (LINES_RE.match(loc) and not isinstance(entry.get("lines"), int)) \
                else None
            where = sources.loc_problem(src, loc, entry, None if text is None else text.count("\n"),
                                        source_text=text if stamp else None)
            if where:
                self.bad(n, "P11", "provenance %s: %s" % (src, where))
                continue
            if quote is None:
                continue
            self.quotes_checked += 1
            text = self.text_of(src)
            if text is None or not sources.quote_found(text, str(quote), loc):
                short = util.normalize_ws(str(quote))[:120]
                self.quotes_failed.append({"n": n, "src": src, "loc": loc, "quote": short})
                self.bad(n, "quote", "quote not found in %s %s: %r" % (src, loc, util.normalize_ws(str(quote))[:80]))

    def closes(self, node_id: str, predicate: Any) -> int:
        """The severity of the gaps of an existing local node that ``predicate(gap)`` says an op closes."""
        if node_id not in self.onto.nodes or self.onto.ns_of(node_id) != "self" or node_id in self.onto.virtual:
            return 0
        try:
            found = needs.needs(self.onto, node_id)
        except Exception:  # needs is advisory here; a bug must not refuse a proposal
            return 0
        return sum(int(g.get("severity") or 0) for g in found.get("gaps") or [] if predicate(g))

    # ops -----------------------------------------------------------------------------------------------------
    def add_node(self, n: Any, op: Dict[str, Any], annot: Dict[str, Any]) -> None:
        node = op.get("node") or {}
        kind = str(node.get("kind") or "")
        name = str(node.get("name") or "")
        ref = op.get("ref")
        decl = self.kind_decl(kind)
        if "/" in kind:
            self.bad(n, "P08", "a local node needs a local kind; %s comes from an import (add the kind to the "
                               "local pack, or link to the imported node)" % kind)
        elif decl is None:
            self.bad(n, "P08", "unknown kind %r; use a known kind (%s) or add it with an add_kind op first"
                     % (kind, self.known_kinds()))
        else:
            for message in packs.field_errors(node.get("attrs") or {}, decl.get("fields") or {}):
                self.bad(n, "P08", "attrs %s" % message)
        given = node.get("id")
        assigned = None
        if isinstance(given, str):
            if given.split(":", 1)[0] != kind:
                self.bad(n, "P07", "the id %s does not match kind %r" % (given, kind))
            elif given in self.onto.nodes or given in self.assigned:
                self.bad(n, "ref", "%s exists already; use update_node" % given)
            else:
                assigned = given
        elif kind and "/" not in kind:
            try:
                assigned = ids.new_node_id(kind, name, set(self.onto.nodes) | self.assigned)
            except UsageError as exc:
                self.bad(n, "P08", exc.message)
        if assigned:
            self.assigned.add(assigned)
        annot["assigned_id"] = assigned
        if ref is not None:
            if ref in self.handles or ref in self.rejected_handles:
                self.bad(n, "ref", "%s is used by an earlier op; each $ref names one new node" % ref)
            elif assigned:
                self.handles[ref] = assigned
                self.handle_kinds[ref] = kind
                self.handle_op[ref] = n
        if decl is not None and "/" not in kind:
            idx = entities.index(self.onto)
            found = entities.match(idx, kind, name, node.get("aliases") or [])
            annot["matches"] = [{"id": m["id"], "score": round(float(m["score"]), 2), "why": m.get("why") or ""}
                                for m in found[:MAX_MATCHES]]
            for m in found:
                if m["score"] >= 1.0:
                    self.warn(n, "match", "%s may already exist as %s (%s)" % (name, m["id"], m.get("why") or ""))
                    break
            key = util.name_key(name)
            if key and not idx.by_key.get(key) and name not in self.new_terms:
                self.new_terms.append(name)
        if not op.get("prov"):
            self.warn(n, "prov", "no provenance: this node can only be drafted")

    def add_edge(self, n: Any, op: Dict[str, Any], annot: Dict[str, Any]) -> None:
        edge = op.get("edge") or {}
        rel = str(edge.get("rel") or "")
        src = self.resolve(n, edge.get("src"), "edge src", allow_source=True)
        dst = self.resolve(n, edge.get("dst"), "edge dst", allow_source=True)
        rel_ns = self.rel_ns(src, dst)  # both ends in one import: its own declaration of the name governs
        decl = self.relation(rel, rel_ns)
        if decl is None:
            self.bad(n, "P09", "unknown relation %r; use a known relation (%s) or add it with an add_relation op "
                               "first" % (rel, self.known_relations()))
        for ref, rid in ((edge.get("src"), src), (edge.get("dst"), dst)):
            if rid and not str(ref).startswith("$"):
                rec = self.onto.record(rid) or {}
                if rec.get("status") == "archived":
                    self.bad(n, "archived", "edge endpoint %s is archived" % rid)
                self.touch(rid)
        if decl is not None and src and dst:
            sk = self.kind_of(str(edge.get("src"))) if str(edge.get("src")).startswith("$") else self.kind_of(src)
            dk = self.kind_of(str(edge.get("dst"))) if str(edge.get("dst")).startswith("$") else self.kind_of(dst)
            if not self.allowed(rel, sk, dk, rel_ns):
                self.bad(n, "P09", "%s does not link a %s to a %s (it links %s); relations that link a %s to a %s: %s"
                         % (rel, sk, dk, self.links(rel, rel_ns), sk, dk, self.fitting(sk, dk, rel_ns) or "none"))
            if src == dst:
                self.bad(n, "P09", "an edge cannot link %s to itself" % src)
            if not str(edge.get("src")).startswith("$") and not str(edge.get("dst")).startswith("$"):
                a, b = (dst, src) if decl.get("symmetric") and src > dst else (src, dst)
                eid = ids.edge_id_in(self.onto.edges, a, rel, b, str(edge.get("key") or ""))
                existing = self.onto.edges.get(eid)
                if existing is not None and eid in self.onto.edge_origin and eid not in self.onto.lines:
                    self.bad(n, "read_only", "the edge %s is imported from %s; imported edges are read-only, so "
                                             "change it in its own topic" % (eid, self.onto.edge_origin[eid][0]))
                elif existing is not None:
                    if existing.get("status") == "archived":
                        self.bad(n, "archived", "the edge %s exists and is archived, and archived is final" % eid)
                    else:
                        note = str(edge.get("note") or "")
                        extra = ""
                        if note and note != str(existing.get("note") or ""):
                            extra = "; its note is not (change a note with update_edge)"
                        self.warn(n, "exists", "the edge %s exists; its provenance will be added%s" % (eid, extra))
                        self.touch(eid)
            self.severity += self.closes(src, lambda g: g["type"] == "orphan"
                                         or (g["type"] == "missing_relation" and g.get("rel") == rel
                                             and g.get("dir") == "out"))
            self.severity += self.closes(dst, lambda g: g["type"] == "orphan"
                                         or (g["type"] == "missing_relation" and g.get("rel") == rel
                                             and g.get("dir") == "in"))
        if not op.get("prov"):
            self.warn(n, "prov", "no provenance: this edge can only be drafted")

    def update(self, n: Any, op: Dict[str, Any], annot: Dict[str, Any], kind: str) -> None:
        is_edge = kind == "edge"
        rid = self.target(n, op.get("id"), "update_%s" % kind, edge=is_edge, node=not is_edge)
        sets = op.get("set") or {}
        unsets = list(op.get("unset") or [])
        allowed = EDGE_SET if is_edge else NODE_SET
        for path in list(sets) + unsets:
            if path in allowed or (not is_edge and path.startswith("attrs.")):
                continue
            if not is_edge and path not in sets and (path == "gaps" or path.startswith("gaps.")):
                continue  # closing recorded gaps (unset only)
            self.bad(n, "P02", "%s cannot be changed on a%s %s" % (path, "n" if kind == "edge" else "", kind))
        if not sets and not unsets and not op.get("prov"):
            self.bad(n, "P02", "update_%s changes nothing: give set, unset or prov" % kind)
        for path in unsets:
            if path in ("name", "conf", "visibility") or (path in sets):
                self.bad(n, "P02", "%s cannot be unset%s" % (path, "" if path not in sets else " and set at once"))
        if rid is None:
            return
        handle = str(op.get("id") or "").startswith("$")
        rec = {} if handle else (self.onto.record(rid) or {})
        node_kind = self.handle_kinds.get(str(op.get("id")), "") if handle else str(rec.get("kind") or "")
        if not is_edge:
            fields = self.fields(node_kind)
            attr_sets = {p[6:]: v for p, v in sets.items() if p.startswith("attrs.")}
            for name in sorted(attr_sets):
                if name not in fields:
                    self.bad(n, "P08", "kind %s has no field %s (add it with add_field)" % (node_kind, name))
            for message in packs.field_errors({k: v for k, v in attr_sets.items() if k in fields}, fields):
                self.bad(n, "P08", "attrs %s" % message)
        if not handle:
            trial = copy.deepcopy(rec)
            for path, value in sets.items():
                if path.startswith("attrs."):
                    trial.setdefault("attrs", {})[path[6:]] = value
                elif path in allowed:
                    trial[path] = value
            for message in records.check(trial, "edge" if is_edge else "node"):
                self.bad(n, "P02", message)
            confirmed = rec.get("status") == "confirmed"
            if confirmed and len(str(op.get("reason") or "").strip()) < REASON_MIN:
                self.bad(n, "reason", "%s is confirmed: give a reason of %d or more characters" % (rid, REASON_MIN))
            if confirmed and not op.get("prov"):
                self.bad(n, "prov", "%s is confirmed: an update needs provenance" % rid)
            for path in unsets:
                if path.startswith("gaps.") and mutate.get_path(rec, path) is None:
                    held = sorted({str(g.get("field")) for g in rec.get("gaps") or [] if isinstance(g, dict)})
                    self.bad(n, "ref", "%s has no recorded gap on %s (it has: %s)"
                             % (rid, path[5:], ", ".join(held) or "none"))
            expect: Dict[str, Any] = {path: mutate.get_path(rec, path) for path in list(sets) + unsets}
            expect["change"] = rec.get("change")
            expect["status"] = rec.get("status")
            annot["expect"] = expect
            if confirmed:
                for path in sorted(sets):
                    current = mutate.get_path(rec, path)
                    if current not in (None, "", [], {}) and current != sets[path]:
                        annot["conflict"] = {"path": path, "current": current, "proposed": sets[path]}
                        break
            if not is_edge:
                fields_set = {"attrs." + p[6:] for p in sets if p.startswith("attrs.")}
                self.severity += self.closes(
                    rid, lambda g: (g["type"] == "missing_field" and g.get("field") in fields_set)
                    or (g["type"] == "thin" and ("summary" in sets or bool(fields_set))))

    def merge(self, n: Any, op: Dict[str, Any], annot: Dict[str, Any]) -> None:
        keep = self.target(n, op.get("keep"), "merge keep")
        drop_ref = op.get("drop")
        if isinstance(drop_ref, str) and drop_ref.startswith("$"):
            self.bad(n, "ref", "merge drop must be an existing node, not %s" % drop_ref)
            return
        drop = self.target(n, drop_ref, "merge drop")
        if keep is None or drop is None:
            return
        if keep == drop:
            self.bad(n, "ref", "merge needs two different nodes")
            return
        keep_kind = self.kind_of(str(op.get("keep"))) if str(op.get("keep")).startswith("$") else self.kind_of(keep)
        drop_kind = self.kind_of(drop)
        idx = entities.index(self.onto)
        if drop_kind not in idx.same_kinds(keep_kind) and keep_kind != drop_kind:
            self.bad(n, "P08", "merge needs the same kind (or kinds mapped by kind_map): %s is a %s, %s a %s"
                     % (keep, keep_kind, drop, drop_kind))
        drop_rec = self.onto.record(drop) or {}
        expect = {"change": drop_rec.get("change"), "status": drop_rec.get("status")}
        if not str(op.get("keep")).startswith("$"):
            keep_rec = self.onto.record(keep) or {}
            expect["keep.change"] = keep_rec.get("change")
            expect["keep.status"] = keep_rec.get("status")
        annot["expect"] = expect
        for e in self.onto.edges_of(drop):
            if self.onto.ns_of(e["edge"].get("id", "")) == "self" and e["edge"].get("id") in self.onto.edges:
                self.touch(str(e["edge"]["id"]))
        if not str(op.get("keep")).startswith("$"):
            self.merge_targets(n, keep, drop)
        self.severity += 3  # a duplicate gap closes

    def merge_targets(self, n: Any, keep: str, drop: str) -> None:
        """A merge re-keys each active local edge of ``drop`` onto ``keep``; one whose new id is held by an archived
        edge cannot move (archived is final), so the merge is refused instead of retiring that fact (``archived``)."""
        for eid in sorted(self.onto.local_edge_ids):
            edge = self.onto.edges.get(eid) or {}
            if edge.get("status") == "archived" or drop not in (edge.get("src"), edge.get("dst")):
                continue
            src = keep if edge.get("src") == drop else str(edge.get("src"))
            dst = keep if edge.get("dst") == drop else str(edge.get("dst"))
            if src == dst:
                continue
            rel, key = str(edge.get("rel") or ""), str(edge.get("key") or "")
            if (self.relation(rel, self.rel_ns(src, dst)) or {}).get("symmetric") and src > dst:
                src, dst = dst, src
            new_id = ids.edge_id_in(self.onto.edges, src, rel, dst, key)
            target = self.onto.edges.get(new_id)
            if target is not None and target.get("status") == "archived":
                self.bad(n, "archived", mutate.merge_blocked(eid, new_id, target, keep, drop))

    def archive(self, n: Any, op: Dict[str, Any], annot: Dict[str, Any]) -> None:
        ref = op.get("id")
        if isinstance(ref, str) and ref.startswith("$"):
            self.bad(n, "ref", "archive needs an existing record, not %s" % ref)
            return
        rid = self.target(n, ref, "archive", edge=True, node=True)
        block = op.get("archived") or {}
        if len(str(block.get("reason") or "").strip()) < REASON_MIN:
            self.bad(n, "reason", "archived.reason must be %d or more characters" % REASON_MIN)
        decision = block.get("decision")
        superseded = block.get("superseded_by") or []
        if decision is None:
            if not superseded:
                self.bad(n, "P12", "an archive needs an active decision (record one with onto decide) or "
                                   "superseded_by")
        else:
            found = self.decisions.get(decision)
            if found is None:
                self.bad(n, "P12", "decision %s is not in the ledger" % decision)
            elif found.get("status") != "active":
                self.bad(n, "P12", "decision %s is not active (superseded by %s)"
                         % (decision, found.get("superseded_by")))
        for s in superseded:
            other = self.resolve(n, s, "archived.superseded_by", allow_edge=True)
            if other is not None and other == rid:
                self.bad(n, "P12", "a record cannot supersede itself")
            elif other is not None and not str(s).startswith("$"):
                if (self.onto.record(other) or {}).get("status") == "archived":
                    self.bad(n, "P12", "archived.superseded_by %s is archived; name an active record" % other)
        if rid is None:
            return
        rec = self.onto.record(rid) or {}
        annot["expect"] = {"change": rec.get("change"), "status": rec.get("status")}
        if rid in self.onto.nodes:
            for e in self.onto.edges_of(rid):
                eid = str(e["edge"].get("id") or "")
                if eid in self.onto.edges and eid not in self.onto.edge_origin:
                    self.touch(eid)

    def add_gap(self, n: Any, op: Dict[str, Any], annot: Dict[str, Any]) -> None:
        self.target(n, op.get("id"), "add_gap")

    def pack_op(self, n: Any, op: Dict[str, Any], annot: Dict[str, Any]) -> None:
        kind = op.get("op")
        if kind == "add_kind" and self.registry.kind(str(op.get("name") or "")) is not None:
            self.bad(n, "P20", "kind %s exists already (in pack %s)"
                     % (op.get("name"), self.registry.kind_pack(str(op.get("name")))))
            return
        if kind == "add_relation" and self.registry.relation(str(op.get("name") or "")) is not None:
            self.bad(n, "P20", "relation %s exists already" % op.get("name"))
            return
        if kind == "map_kinds":
            for side in ("a", "b"):
                name = str(op.get(side) or "")
                if self.registry.kind_key(name) is None and name not in self.new_kinds:
                    self.bad(n, "P08", "map_kinds: %s is not a known kind" % name)
        try:
            self.pack = packs.apply_op(self.pack, op)
        except (Refused, UsageError) as exc:
            self.bad(n, "P20", exc.message)
            return
        if kind == "add_kind":
            self.new_kinds[str(op.get("name"))] = dict(op.get("kind") or {})
        elif kind == "add_relation":
            self.new_relations[str(op.get("name"))] = dict(op.get("relation") or {})
        elif kind == "add_field":
            name = str(op.get("kind"))
            if name in self.new_kinds:
                self.new_kinds[name] = (self.pack.get("kinds") or {}).get(name) or self.new_kinds[name]

    def add_question(self, n: Any, op: Dict[str, Any], annot: Dict[str, Any]) -> None:
        question = op.get("question") or {}
        for message in packs.check_question(question):
            self.bad(n, "P02", "question: %s" % message)
        qid = question.get("id")
        if qid in self.question_ids:
            self.bad(n, "ref", "question %s exists already" % qid)
        self.question_ids.add(qid)

    def run(self, ops: Sequence[Dict[str, Any]], skip: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        """Check and annotate ``ops`` in order (``n`` kept); ops numbered in ``skip`` are left out, and handles they
        would create count as rejected."""
        out: List[Dict[str, Any]] = []
        for op in ops:
            n = op.get("n")
            if n in skip:
                if op.get("op") == "add_node" and op.get("ref"):
                    self.rejected_handles[str(op["ref"])] = n
                continue
            clean = copy.deepcopy(_author(op))
            clean["n"] = n
            if clean.get("op") in ("add_node", "add_edge"):
                clean.setdefault("conf", DEFAULT_CONF)
                clean.setdefault("basis", "inferred")
            errors = records.check(clean, "op")
            annot: Dict[str, Any] = {"matches": [], "expect": {}, "conflict": None, "stale": False,
                                     "assigned_id": None}
            for message in records.control_problems(clean, "op"):
                self.bad(n, "P02", message)
            if errors:
                for message in errors[:5]:
                    self.bad(n, "P02", message)
                if clean.get("op") == "add_node" and isinstance(clean.get("ref"), str):
                    self.rejected_handles[clean["ref"]] = n
                clean["annot"] = annot
                out.append(clean)
                continue
            kind = clean["op"]
            self.check_prov(n, clean.get("prov"))
            if kind == "add_node":
                self.add_node(n, clean, annot)
            elif kind == "add_edge":
                self.add_edge(n, clean, annot)
            elif kind == "update_node":
                self.update(n, clean, annot, "node")
            elif kind == "update_edge":
                self.update(n, clean, annot, "edge")
            elif kind == "merge":
                self.merge(n, clean, annot)
            elif kind == "archive":
                self.archive(n, clean, annot)
            elif kind == "add_gap":
                self.add_gap(n, clean, annot)
            elif kind in PACK_OPS:
                self.pack_op(n, clean, annot)
            elif kind == "add_question":
                self.add_question(n, clean, annot)
            clean["annot"] = annot
            out.append(clean)
        return out


def _open_checks(repo: store.Repo, onto: Ontology, draft: Dict[str, Any], checker: _Checker) -> None:
    source = draft.get("source")
    if source is not None and source not in onto.sources:
        checker.bad(None, "ref", "source %s is not in the sources index" % source)
    elif source is not None and (onto.sources.get(source) or {}).get("erased"):
        checker.bad(None, "P11", "source %s is erased; nothing may be proposed from it" % source)
    sup = draft.get("supersedes")
    if sup is not None:
        found = _read_all(repo).get(sup)
        if found is None:
            checker.bad(None, "ref", "supersedes %s: no such proposal" % sup)
        elif found[1].get("status") not in OPEN:
            checker.bad(None, "status", "supersedes %s: it is %s already" % (sup, found[1].get("status")))
    policy = repo.policy
    open_count = len(pending(repo))
    limit = policy.get("max_pending")
    if isinstance(limit, int) and open_count >= limit:
        checker.warn(None, "backlog", "%d proposals are pending (max_pending %d); review them with onto review"
                     % (open_count, limit))


def _summary(ops: Sequence[Dict[str, Any]]) -> str:
    counts: Dict[str, int] = {}
    for op in ops:
        counts[str(op.get("op"))] = counts.get(str(op.get("op")), 0) + 1
    return "%d op%s: %s" % (len(ops), "" if len(ops) == 1 else "s",
                            ", ".join("%d %s" % (v, k) for k, v in sorted(counts.items())))


def prepare(repo: store.Repo, draft: Dict[str, Any], *, by: str = "agent", save: bool = True,
            mcp: bool = False) -> Dict[str, Any]:
    """Check a draft and save it as a pending proposal (see the module docstring). Returns the proposal; a draft
    with structural problems raises ``Refused`` carrying ``problems`` and the annotated ``proposal``. With
    ``save=False`` nothing is written: the annotated proposal is returned for a preview (for example to learn
    ``destructive(proposal)`` before a confirm gate). ``mcp`` words the follow-up calls its problems name as tool
    calls (``onto_import action=update ...``) instead of CLI commands."""
    if not isinstance(draft, dict):
        raise UsageError("a proposal is a JSON object with an ops list")
    clean = copy.deepcopy({k: v for k, v in draft.items() if k not in KIT_KEYS})
    clean.setdefault("by", by)
    raw_ops = clean.get("ops")
    if not isinstance(raw_ops, list) or not raw_ops:
        raise UsageError("a proposal needs a non-empty ops list")
    odd = _non_finite(clean)
    if odd:
        problems = [{"n": _op_number(p), "code": "P02", "message": "%s: not a finite number (NaN and Infinity are "
                                                                     "not JSON numbers)" % p} for p in odd]
        raise Refused("proposal refused: %d problem(s); fix them and propose again. First: %s"
                      % (len(problems), _problem_text(problems[0])), problems=problems)
    half = util.surrogate_problem(clean)
    if half:  # no file can hold it, and the proposal id hashes the text: refuse before anything else
        problems = [{"n": _op_number(half), "code": "P02", "message": half}]
        raise Refused("proposal refused: 1 problem(s); fix them and propose again. First: %s"
                      % _problem_text(problems[0]), problems=problems)
    redactions = sanitize_draft(repo, clean, "draft")
    invisible = strip_invisible_names(clean, "draft")
    ops = []
    for i, op in enumerate(raw_ops, start=1):
        if isinstance(op, dict):
            op = dict(_author(op), n=i)
        ops.append(op)
    author = {"by": clean["by"], "source": clean.get("source"), "ops": [_normalized(o) for o in raw_ops]}
    content = _content_key(clean["by"], clean.get("source"), raw_ops)
    mutate.check_format(repo.reload())
    onto = Ontology.load(repo)
    mutate.refuse_damaged(onto, (NODES, EDGES))
    checker = _Checker(repo, onto, clean.get("source"), mcp=mcp)
    draft_errors = records.check(dict(clean, ops=[_author(o) for o in raw_ops]), "proposal_draft")
    checked = checker.run([o if isinstance(o, dict) else {"n": i + 1, "op": None} for i, o in enumerate(ops)])
    for message in draft_errors:
        if "$.ops" not in message:
            checker.bad(None, "P02", message)
    summary_problem = records.control_problem(str(clean.get("summary") or ""), records.BLOCK)
    if summary_problem:
        checker.bad(None, "P02", "$.summary: %s" % summary_problem)
    if redactions:
        checker.warn(None, "redacted", _redaction_note(redactions))
    if invisible:
        checker.warn(None, "invisible", "removed %d invisible character%s (a soft hyphen, a zero-width space) from "
                                        "names and aliases" % (invisible, "" if invisible == 1 else "s"))
    _open_checks(repo, onto, clean, checker)
    now = util.now_iso()
    wanted_sup = clean.get("supersedes")
    if not checker.problems:
        found, _stale = _same_open(_read_all(repo), content, onto)
        if found is not None and found["id"] != wanted_sup:
            return found
    prop_id = _prop_id(author, content, now[:10], _read_all(repo))
    proposal: Dict[str, Any] = {
        "id": prop_id,
        "status": "pending",
        "by": clean["by"] if clean["by"] in ("user", "agent") else "agent",
        "created": now,
        "source": clean.get("source"),
        "summary": util.normalize_ws(str(clean.get("summary") or "") or _summary(checked))[:600],
        "supersedes": clean.get("supersedes"),
        "base": store.data_hash(repo),
        "priority": int(round(10 * checker.severity + 5 * len(checked))),
        "ops": checked,
        "checks": {
            "problems": checker.problems,
            "warnings": checker.warnings,
            "quotes": {"checked": checker.quotes_checked, "failed": checker.quotes_failed},
        },
        "impact": sorted(checker.impact),
        "new_terms": checker.new_terms,
        "review": None,
        "applied": None,
    }
    if checker.problems:
        raise Refused(
            "proposal refused: %d problem(s); fix them and propose again. First: %s"
            % (len(checker.problems), _problem_text(checker.problems[0])),
            problems=checker.problems, proposal=_defang(proposal),
        )
    if not save:
        return proposal
    with store.write_lock(repo):
        # the id is picked again under the lock, from the folders as they are now: another writer may have saved a
        # proposal since, and a hash that another proposal holds with other content gets a longer id (C.2)
        existing = _read_all(repo)
        found, stale = _same_open(existing, content, Ontology.load(repo))
        if found is not None and found["id"] != wanted_sup:
            return found
        if stale and not proposal.get("supersedes"):
            proposal["supersedes"] = stale[0]  # the same draft again, against the data as it is now
        proposal["id"] = _prop_id(author, content, now[:10], existing)
        _save(repo, proposal)
        for sup in _dedupe_ids([proposal.get("supersedes")] + stale):
            old = load(repo, sup)
            if old.get("status") in OPEN:
                _save(repo, dict(old, status="superseded"))
    if stale:
        return dict(proposal, note="replaces %s, which went stale (the data changed under it since it was "
                                   "prepared); it is marked superseded" % ", ".join(stale))
    return proposal


def _dedupe_ids(values: Sequence[Any]) -> List[str]:
    out: List[str] = []
    for v in values:
        if isinstance(v, str) and v and v not in out:
            out.append(v)
    return out


def _prop_id(author: Dict[str, Any], content: str, day: str, existing: Dict[str, Tuple[str, Dict[str, Any]]]) -> str:
    """The id of a new proposal. Proposals with other content that hold its hash give it a longer hash (C.2); one
    with the same content (closed, or stale and being replaced) is no collision: the hash takes a repeat counter
    instead, as an answer id does, so the same draft can be proposed again any number of times."""
    others = {pid for pid, (_folder, value) in existing.items()
              if _content_key(value.get("by"), value.get("source"), value.get("ops") or []) != content}
    for n in range(1, 1000):
        body = author if n == 1 else {"author": author, "repeat": n}
        try:
            candidate = ids.record_id("prop", body, date=day, taken=others)
        except DataError:
            continue
        if candidate not in existing:
            return candidate
    raise DataError("cannot make a unique prop id for this content (%d proposals hold it)" % len(existing))


def stale_ops(onto: Ontology, proposal: Dict[str, Any]) -> List[Any]:
    """Op numbers of an open proposal whose expectations (``annot.expect``) the current graph no longer meets: the
    data moved under it since it was prepared, so applying it raises ``Conflict``."""
    review_rec = proposal.get("review") or {}
    verdicts = dict(review_rec.get("verdicts") or {}) or {str(op.get("n")): "accept"
                                                         for op in proposal.get("ops") or [] if isinstance(op, dict)}
    try:
        return mutate.expectation_failures(onto, _expected_ops(proposal, verdicts,
                                                              dict(review_rec.get("edits") or {})))
    except (AttributeError, TypeError, KeyError):  # a malformed proposal is validate's to report
        return []


def _content_key(by: Any, source: Any, ops: Sequence[Any]) -> str:
    """What makes two proposals the same proposal: the author, the source and the normalized author ops."""
    return util.canonical_line({"by": by if by in ("user", "agent") else "agent", "source": source,
                                "ops": [_normalized(o) for o in ops or []]})


def _same_open(existing: Dict[str, Tuple[str, Dict[str, Any]]], content: str,
               onto: Optional[Ontology] = None) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """``(the open proposal with this content marked "already proposed" or None, the ids of open proposals with
    this content that went stale)``. A proposal that only shares the hash holds other content and is not it. With
    ``onto``, an open copy whose expectations the graph no longer meets (``stale_ops``) is not returned: applying
    it would only raise ``Conflict`` again, so the draft is saved anew and replaces it."""
    stale: List[str] = []
    for pid in sorted(existing):
        _folder, value = existing[pid]
        if value.get("status") not in OPEN:
            continue
        if _content_key(value.get("by"), value.get("source"), value.get("ops") or []) != content:
            continue
        if onto is not None and stale_ops(onto, value):
            stale.append(pid)
            continue
        return dict(value, note="already proposed"), stale
    return None, stale


def _problem_text(p: Dict[str, Any]) -> str:
    where = "op %s: " % p["n"] if p.get("n") is not None else ""
    return "%s%s %s" % (where, p.get("code"), p.get("message"))


def _op_number(path: str) -> Optional[int]:
    """The op number a ``$.ops[i]...`` path points into, or None."""
    m = re.match(r"^\$\.ops\[([0-9]+)\]", path)
    return int(m.group(1)) + 1 if m else None


def clean_edits(repo: store.Repo, edits: Dict[str, Any]) -> Dict[str, Any]:
    """Copies of the replacement ops of a review, run through the same text rules as a new proposal: no NaN or
    Infinity, no credentials, personal data redacted per the policy (``sanitize_draft``)."""
    out: Dict[str, Any] = {}
    for key, op in edits.items():
        op = copy.deepcopy(op)
        if isinstance(op, dict):
            odd = _non_finite(op)
            if odd:
                raise Refused("the edit for op %s: %s is not a finite number (NaN and Infinity are not JSON numbers)"
                              % (key, odd[0]), problems=[{"n": key, "code": "P02", "message": p} for p in odd])
            half = util.surrogate_problem(op)
            if half:
                raise Refused("the edit for op %s: %s" % (key, half),
                              problems=[{"n": key, "code": "P02", "message": half}])
            sanitize_draft(repo, op, "op")
            strip_invisible_names(op, "op")
        out[str(key)] = op
    return out


# review --------------------------------------------------------------------------------------------------------
def _normalize_verdicts(verdicts: Dict[Any, Any], n_ops: int) -> Dict[str, str]:
    if not isinstance(verdicts, dict):
        raise UsageError("verdicts must map op numbers to accept, draft, reject or edit")
    out: Dict[str, str] = {}
    for key, value in verdicts.items():
        try:
            n = int(key)
        except (TypeError, ValueError):
            raise UsageError("verdict key %r is not an op number" % (key,))
        if n < 1 or n > n_ops:
            raise UsageError("op %d does not exist (the proposal has %d op%s)" % (n, n_ops, "" if n_ops == 1 else "s"))
        if value not in VERDICTS:
            raise UsageError("op %d: verdict %r must be one of %s" % (n, value, ", ".join(VERDICTS)))
        out[str(n)] = value
    missing = [str(n) for n in range(1, n_ops + 1) if str(n) not in out]
    if missing:
        raise UsageError("every op needs a verdict; missing: %s" % ", ".join(missing))
    return out


def _effective_ops(proposal: Dict[str, Any], verdicts: Dict[str, str],
                   edits: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for op in proposal.get("ops") or []:
        n = str(op.get("n"))
        if verdicts.get(n) == "edit":
            out.append(dict(_author(edits[n]), n=op.get("n")))
        else:
            out.append(op)
    return out


def _expected_ops(proposal: Dict[str, Any], verdicts: Dict[str, str], edits: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The ops that will apply, with the expectations recorded when they were checked (prepare, or the review for
    an edit)."""
    out = []
    for op in proposal.get("ops") or []:
        key = str(op.get("n"))
        verdict = verdicts.get(key)
        if verdict == "reject":
            continue
        if verdict == "edit":
            edit = edits.get(key)
            if isinstance(edit, dict) and (edit.get("annot") or {}).get("expect"):
                out.append(dict(edit, n=op.get("n")))
            continue
        out.append(op)
    return out


def _check_review(repo: store.Repo, onto: Ontology, proposal: Dict[str, Any], verdicts: Dict[str, str],
                  edits: Dict[str, Any]) -> _Checker:
    checker = _Checker(repo, onto, proposal.get("source"))
    ops = _effective_ops(proposal, verdicts, edits)
    skip = [op.get("n") for op in ops if verdicts.get(str(op.get("n"))) == "reject"]
    checker.checked = checker.run(ops, skip)  # type: ignore[attr-defined]
    source = proposal.get("source")
    if source and (onto.sources.get(source) or {}).get("erased"):
        checker.bad(None, "P11", "source %s is erased; nothing may be applied from it" % source)
    return checker


def review(repo: store.Repo, prop_id: str, verdicts: Dict[Any, Any], edits: Optional[Dict[Any, Any]] = None,
           by: str = "user", reason: str = "") -> Dict[str, Any]:
    """Record the verdicts (one per op). Edited ops are checked again; a problem refuses the review. All ops
    rejected makes the proposal ``rejected`` (moved to done); otherwise it is ``reviewed``."""
    proposal = load(repo, prop_id)
    if proposal.get("status") not in OPEN:
        raise Refused("%s is %s; only pending or reviewed proposals take verdicts" % (prop_id, proposal.get("status")))
    if by not in ("user", "agent"):
        raise UsageError("by must be user or agent")
    n_ops = len(proposal.get("ops") or [])
    norm = _normalize_verdicts(verdicts, n_ops)
    edit_map: Dict[str, Any] = {}
    for key, op in (edits or {}).items():
        k = str(key)
        if norm.get(k) != "edit":
            raise UsageError("op %s has an edit but its verdict is %s; use verdict edit" % (k, norm.get(k)))
        if not isinstance(op, dict):
            raise UsageError("edit for op %s must be a full op object" % k)
        edit_map[k] = op
    for k, v in norm.items():
        if v == "edit" and k not in edit_map:
            raise UsageError("op %s: verdict edit needs a replacement op in edits" % k)
    edit_map = clean_edits(repo, edit_map)
    holder = {"summary": str(reason or "")}
    sanitize_draft(repo, holder)
    reason = holder["summary"]
    with store.write_lock(repo):
        # read it again under the lock: a concurrent apply may have moved it to done/ since the checks above
        proposal = load(repo, prop_id)
        if proposal.get("status") not in OPEN:
            raise Refused("%s is %s; only pending or reviewed proposals take verdicts"
                          % (prop_id, proposal.get("status")))
        onto = Ontology.load(repo)
        checker = _check_review(repo, onto, proposal, norm, edit_map)
        edited = {k for k in edit_map}
        bad = [p for p in checker.problems if str(p.get("n")) in edited]
        if bad:
            raise Refused("an edited op has problems: %s" % _problem_text(bad[0]), problems=bad)
        stored_edits: Dict[str, Any] = {}
        for op in checker.checked:  # type: ignore[attr-defined]
            if str(op.get("n")) in edited:
                stored_edits[str(op["n"])] = op
        record = {"by": by, "at": util.now_iso(), "verdicts": norm, "edits": stored_edits,
                  "reason": util.normalize_ws(reason or "")[:600]}
        errors = records.check(record, "review")
        if errors:
            raise UsageError("the review fails its schema: %s" % "; ".join(errors[:3]), problems=errors)
        proposal = dict(proposal, review=record)
        proposal["status"] = "rejected" if all(v == "reject" for v in norm.values()) else "reviewed"
        _save(repo, proposal)
    return proposal


# commit --------------------------------------------------------------------------------------------------------
def destructive(proposal: Dict[str, Any]) -> List[Any]:
    """Op numbers that merge, archive, or update a confirmed record (edits count as their replacement)."""
    edits = ((proposal.get("review") or {}).get("edits") or {})
    out = []
    for op in proposal.get("ops") or []:
        eff = edits.get(str(op.get("n"))) or op
        kind = eff.get("op")
        expect = (eff.get("annot") or {}).get("expect") or {}
        if kind in ("merge", "archive"):
            out.append(op.get("n"))
        elif kind in ("update_node", "update_edge") and expect.get("status") == "confirmed":
            out.append(op.get("n"))
    return out


def _answer_source(onto: Ontology, source: Optional[str]) -> bool:
    entry = onto.sources.get(source or "") or {}
    return entry.get("kind") == "interview"


def _stated(op: Dict[str, Any], source: Optional[str], checker: _Checker) -> bool:
    """A stated fact quoted verbatim from the answer text: every quote from the answer source is found, and every
    provenance entry cites that answer or an import (an op that also cites another stored source carries that
    source's text, which the user did not say)."""
    if op.get("basis") != "stated" or not source:
        return False
    prov = [p for p in op.get("prov") or [] if isinstance(p, dict)]
    quotes = [p for p in prov if p.get("src") == source and p.get("quote")]
    if not quotes or any(p.get("src") != source and not str(p.get("src") or "").startswith("imp:") for p in prov):
        return False
    failed = {(f.get("n"), f.get("src")) for f in checker.quotes_failed}
    return (op.get("n"), source) not in failed


def _cites_untrusted(onto: Ontology, op: Dict[str, Any]) -> bool:
    """True when a provenance entry cites a stored source that is not an interview answer (an ingested file, note,
    url or transcript: trust ``untrusted``). An op's text is only as trusted as the least trusted source it cites
    (C.7), so one quote from an answer never lifts the text of a file it also cites."""
    for p in op.get("prov") or []:
        src = p.get("src") if isinstance(p, dict) else None
        if not isinstance(src, str) or src.startswith("imp:"):
            continue
        entry = onto.sources.get(src) or {}
        if entry.get("kind") != "interview" or entry.get("trust") == "untrusted":
            return True
    return False


def _resolve_ref(value: Any, handles: Dict[str, str]) -> Any:
    if isinstance(value, str):
        if value.startswith("$"):
            return handles.get(value, value)
        if value.startswith("self/"):
            return value[5:]
    return value


def _resolved_ops(onto: Ontology, proposal: Dict[str, Any], checker: _Checker, verdicts: Dict[str, str],
                  by: str, change_type: str = "apply") -> List[Dict[str, Any]]:
    source = proposal.get("source")
    answer = _answer_source(onto, source)
    handles = checker.handles

    def _resolve(value: Any, handles: Dict[str, str]) -> Any:  # own-ns ids ("g2t/goal:x" in g2t) read as local
        value = _resolve_ref(value, handles)
        return onto.own_local(value) if isinstance(value, str) else value

    out: List[Dict[str, Any]] = []
    for op in checker.checked:  # type: ignore[attr-defined]
        n = op.get("n")
        verdict = verdicts.get(str(n))
        kind = op.get("op")
        accept = verdict in ("accept", "edit")
        if not accept and kind in ("merge", "archive", "add_question") + PACK_OPS:
            raise Refused("op %s (%s) takes accept or reject, not %s" % (n, kind, verdict))
        untrusted = _cites_untrusted(onto, op)
        if accept:
            status = "confirmed"
            if answer and by == "user" and _stated(op, source, checker):
                trust = "user"
            elif untrusted and change_type == "answer":
                # accepted on its own as an answer's stated fact, but it also carries an ingested source's text,
                # which nobody reviewed: that text keeps its [untrusted] marker (C.7)
                trust = "untrusted"
            else:
                trust = "reviewed"
        else:
            # C.7: a draft is from the answer only when its provenance cites interview sources only (this answer or
            # an earlier one), or when it has none; a draft that carries another source's content stays untrusted,
            # even when it also quotes the answer
            cites_answer = any(isinstance(p, dict) and _answer_source(onto, p.get("src"))
                               for p in op.get("prov") or [])
            status = "proposed"
            trust = "agent" if answer and not untrusted and (cites_answer or not op.get("prov")) else "untrusted"
        annot = op.get("annot") or {}
        if kind == "add_node":
            if accept and not op.get("prov"):
                raise Refused("op %s: a confirmed node needs provenance; add prov or draft it" % n)
            node = copy.deepcopy(op.get("node") or {})
            node["id"] = annot.get("assigned_id")
            out.append({"n": n, "op": kind, "node": node, "status": status, "trust": trust,
                        "conf": op.get("conf", DEFAULT_CONF), "prov": op.get("prov") or []})
        elif kind == "add_edge":
            if accept and not op.get("prov"):
                raise Refused("op %s: a confirmed edge needs provenance; add prov or draft it" % n)
            edge = copy.deepcopy(op.get("edge") or {})
            edge["src"] = _resolve(edge.get("src"), handles)
            edge["dst"] = _resolve(edge.get("dst"), handles)
            out.append({"n": n, "op": kind, "edge": edge, "status": status, "trust": trust,
                        "conf": op.get("conf", DEFAULT_CONF), "prov": op.get("prov") or []})
        elif kind in ("update_node", "update_edge"):
            target = _resolve(op.get("id"), handles)
            rec = onto.record(str(target)) or {}
            if not accept and rec.get("status") == "confirmed":
                raise Refused("op %s updates the confirmed record %s: accept or reject it" % (n, target))
            out.append({"n": n, "op": kind, "id": target, "set": op.get("set") or {}, "unset": op.get("unset") or [],
                        "prov": op.get("prov") or [], "status": "confirmed" if accept else None,
                        "trust": trust if accept else None, "draft_trust": None if accept else trust,
                        "annot": {"expect": annot.get("expect") or {}}})
        elif kind == "merge":
            out.append({"n": n, "op": kind, "keep": _resolve(op.get("keep"), handles), "drop": _resolve(op.get("drop"),
                        handles), "reason": op.get("reason") or "", "annot": {"expect": annot.get("expect") or {}}})
        elif kind == "archive":
            block = dict(op.get("archived") or {})
            block["superseded_by"] = [_resolve(s, handles) for s in block.get("superseded_by") or []]
            out.append({"n": n, "op": kind, "id": _resolve(op.get("id"), handles), "archived": block,
                        "annot": {"expect": annot.get("expect") or {}}})
        elif kind == "add_gap":
            out.append({"n": n, "op": kind, "id": _resolve(op.get("id"), handles), "gap": op.get("gap")})
        else:
            out.append({k: v for k, v in op.items() if k != "annot"})
    return out


def trial_problems(repo: store.Repo, proposal: Dict[str, Any], verdicts: Dict[Any, Any], *, by: str = "user",
                   change_type: str = "answer", unstored: Optional[str] = None) -> List[Dict[str, Any]]:
    """The problems the final graph check of ``commit`` would refuse ``proposal`` with under ``verdicts`` (a P23 of
    the assessment pack, say), for a proposal that need not be saved (``prepare(save=False)``): the review checks,
    then ``mutate.apply_ops`` with ``dry_run``. Nothing is written. ``[{code, message}]``, empty when it applies.
    ``unstored`` names a source the confirmed call stores first (an answer's): problems about it are left out."""
    onto = Ontology.load(repo)
    norm = _normalize_verdicts(verdicts, len(proposal.get("ops") or []))
    checker = _check_review(repo, onto, proposal, norm, {})

    def kept(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [p for p in items if not (unstored and unstored in str(p.get("message") or ""))]

    found = kept([{"code": p.get("code"), "message": p.get("message"), "n": p.get("n")} for p in checker.problems])
    if found:
        return found
    try:
        resolved = _resolved_ops(onto, proposal, checker, norm, by, change_type)
        if not resolved:
            return []
        dry = mutate.apply_ops(repo, resolved, by=by, change_type=change_type, summary="preview",
                               proposal_id=proposal.get("id"), source=proposal.get("source"), dry_run=True)
    except Conflict:
        return []  # the data changed under it: the real call reports that itself
    except OntoError as exc:
        return kept([{"code": exc.kind, "message": exc.message}])
    return kept([{"code": p.code, "message": p.text()} for p in dry.get("problems") or []])


def commit(repo: store.Repo, prop_id: str, *, preview: bool = False, verdicts: Optional[Dict[Any, Any]] = None,
           edits: Optional[Dict[Any, Any]] = None, by: Optional[str] = None,
           change_type: str = "apply", summary_prefix: str = "") -> Dict[str, Any]:
    """Apply a reviewed proposal (see the module docstring). ``preview`` returns ``{would_change, destructive}`` and
    writes nothing; it may take ``verdicts`` (and ``edits``) that are not recorded yet. ``change_type`` is
    ``answer`` when the interview applies an answer, and ``summary_prefix`` then names the question and what it
    added (``q.people.key, added Club secretary: 2 ops applied, 0 rejected``), so ``onto log`` shows it.

    Applying is idempotent: the proposal is read again under the write lock, and one the change log already names
    (a run that stopped after its change line, before the move to ``proposals/done/``) is finished from that change
    instead of being applied twice. In the normal run the move to ``done/`` is part of the change's own
    all-or-nothing write (``mutate.apply_ops`` ``finish``)."""
    load(repo, prop_id)  # NotFound early, before waiting for the lock
    with store.write_lock(repo):
        proposal = load(repo, prop_id)
        status = proposal.get("status")
        if status == "applied":
            return dict(proposal.get("applied") or {}, note="already applied", proposal=prop_id)
        if status in ("rejected", "superseded"):
            raise Refused("%s is %s; nothing to apply" % (prop_id, status))
        review_rec = proposal.get("review") or {}
        if verdicts is not None:
            norm = _normalize_verdicts(verdicts, len(proposal.get("ops") or []))
            edit_map = clean_edits(repo, {str(k): v for k, v in (edits or {}).items()})
            who = by or "user"
        else:
            if not review_rec:
                raise Refused("%s has no verdicts yet; review it first (onto apply --accept, --draft, --reject)"
                              % prop_id)
            norm = dict(review_rec.get("verdicts") or {})
            edit_map = dict(review_rec.get("edits") or {})
            who = by or review_rec.get("by") or "user"
        if not preview:
            logged = _logged_change(repo, prop_id)
            if logged is not None:
                return _finish_logged(repo, proposal, logged, norm, edit_map, who, verdicts is not None)
        onto = Ontology.load(repo)
        stale = mutate.expectation_failures(onto, _expected_ops(proposal, norm, edit_map))
        if stale and not preview:
            raise Conflict(
                "the data changed under op%s %s since %s was prepared; nothing was written. Propose it again against "
                "the current data (the same draft proposed again replaces %s, which is marked superseded), or "
                "reject it (onto apply %s --all reject)"
                % ("" if len(stale) == 1 else "s", ", ".join(str(n) for n in stale), prop_id, prop_id, prop_id),
                ops=stale, proposal=prop_id,
            )
        checker = _check_review(repo, onto, proposal, norm, edit_map)
        if checker.problems:
            raise Refused("%s cannot be applied to the current data: %s"
                          % (prop_id, _problem_text(checker.problems[0])), problems=checker.problems)
        stored = {str(op.get("n")): op for op in proposal.get("ops") or []}
        for op in checker.checked:  # type: ignore[attr-defined]
            key = str(op.get("n"))
            if norm.get(key) != "edit" and key in stored:
                op["annot"]["expect"] = (stored[key].get("annot") or {}).get("expect") or {}
            elif key in edit_map:
                op["annot"]["expect"] = ((edit_map[key].get("annot") or {}).get("expect")
                                         or op["annot"].get("expect") or {})
        resolved = _resolved_ops(onto, proposal, checker, norm, who, change_type)
        skipped = {n: {"skipped": "rejected"} for n, v in norm.items() if v == "reject"}
        if preview:
            failed = mutate.expectation_failures(onto, resolved)
            would = []
            for op in resolved:
                target = (op.get("node") or {}).get("id") or op.get("id") or op.get("keep") \
                    or op.get("name") or (op.get("question") or {}).get("id")
                if op.get("op") == "add_edge":
                    e = op["edge"]
                    target = "%s -%s-> %s" % (e.get("src"), e.get("rel"), e.get("dst"))
                would.append({"n": op["n"], "op": op["op"], "id": target, "verdict": norm.get(str(op["n"])),
                              "status": op.get("status")})
            prop_view = dict(proposal, review={"edits": {k: v for k, v in edit_map.items()}})
            problems: List[Dict[str, Any]] = []
            if resolved and not failed:
                # the final graph check of mutate, run without writing, so the preview warns before confirm
                try:
                    dry = mutate.apply_ops(repo, resolved, by=who, change_type=change_type, summary="preview",
                                           proposal_id=prop_id, source=proposal.get("source"), dry_run=True)
                    problems = [{"code": p.code, "message": p.text()} for p in dry.get("problems") or []]
                except OntoError as exc:
                    problems = [{"code": exc.kind, "message": exc.message}]
            return {"would_change": would, "destructive": destructive(prop_view), "skipped": sorted(skipped),
                    "conflicts": failed, "problems": problems, "proposal": prop_id}
        rejected = len(skipped)
        summary = "%s%d op%s applied, %d rejected" % (summary_prefix, len(resolved), "" if len(resolved) == 1 else "s",
                                                     rejected)
        final_review = review_rec
        if verdicts is not None:
            final_review = {"by": who if who in ("user", "agent") else "user", "at": util.now_iso(),
                            "verdicts": norm, "edits": dict(edit_map), "reason": review_rec.get("reason") or ""}
        kept: Dict[str, Any] = {}
        # the annotations of this run's checks (a node id planned at prepare may be taken since, so it got "-2")
        fresh = {str(op.get("n")): op.get("annot") or {} for op in checker.checked}  # type: ignore[attr-defined]

        def finish(change_id: Optional[str], at: str, results: Dict[str, Any]) -> List[Tuple[str, Optional[bytes]]]:
            merged = dict(results)
            merged.update(skipped)
            applied = {"at": at, "change": change_id, "results": {k: merged[k] for k in sorted(merged, key=int)}}
            final = _final(proposal, applied, norm, edit_map, who, verdicts is not None, fresh)
            errors = records.check(final, "proposal")
            if errors:
                raise DataError("the proposal fails its schema (a kit bug): %s" % "; ".join(errors[:3]),
                                problems=errors)
            kept["applied"] = applied
            return [("%s/%s.json" % (DONE, prop_id), util.canonical_bytes(final)),
                    ("%s/%s.json" % (PENDING, prop_id), None)]

        if resolved:
            done = mutate.apply_ops(repo, resolved, by=who, change_type=change_type, summary=summary,
                                    proposal_id=prop_id, source=proposal.get("source"),
                                    in_flight=dict(proposal, review=final_review), finish=finish)
        else:
            done = {"change": None, "results": {}, "ids": []}
            finish(None, util.now_iso(), {})
            _save(repo, _final(proposal, kept["applied"], norm, edit_map, who, verdicts is not None, fresh))
        applied = kept["applied"]
    return dict(applied, ids=done.get("ids") or [], proposal=prop_id)


FRESH_ANNOT = ("assigned_id", "matches")  # what the checks at apply time know better than the prepare-time run


def _final(proposal: Dict[str, Any], applied: Dict[str, Any], norm: Dict[str, str], edit_map: Dict[str, Any],
           who: str, new_review: bool, fresh: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The proposal as it is stored once applied. ``fresh`` holds the annotations of the checks run at apply time
    by op number: an applied op (or its edit) keeps those, so ``annot.assigned_id`` names the node it created, not
    one planned at prepare time and taken since."""
    final = dict(proposal, status="applied", applied=applied)
    if new_review:
        final["review"] = {"by": who if who in ("user", "agent") else "user", "at": applied["at"],
                           "verdicts": norm, "edits": {k: v for k, v in edit_map.items()}, "reason": ""}
    if not fresh:
        return final

    def refreshed(op: Any, key: str) -> Any:
        if not isinstance(op, dict) or key not in fresh or norm.get(key) == "reject":
            return op
        annot = dict(op.get("annot") or {})
        annot.update({k: copy.deepcopy(fresh[key][k]) for k in FRESH_ANNOT if k in fresh[key]})
        return dict(op, annot=annot)

    final["ops"] = [op if norm.get(str(op.get("n"))) == "edit" else refreshed(op, str(op.get("n")))
                    for op in proposal.get("ops") or []]
    review = final.get("review")
    if isinstance(review, dict) and isinstance(review.get("edits"), dict):
        final["review"] = dict(review, edits={k: refreshed(v, str(k)) if norm.get(str(k)) == "edit" else v
                                              for k, v in review["edits"].items()})
    return final


def _logged_change(repo: store.Repo, prop_id: str) -> Optional[Dict[str, Any]]:
    """The last change ``mutate.apply_ops`` wrote for ``prop_id`` (type apply or answer, with the data hashes it
    records before and after the write), or None. A record-only line that only names the proposal (an answer whose
    ops wait for review has ``before`` and ``after`` null) is no proof that the ops were applied."""
    rows, _problems = store.read_jsonl(ledger.changes_path(repo))
    found = [r for r in rows if r.get("proposal") == prop_id and r.get("type") in ("apply", "answer")
             and isinstance(r.get("before"), str) and isinstance(r.get("after"), str)]
    return found[-1] if found else None


def _finish_logged(repo: store.Repo, proposal: Dict[str, Any], change: Dict[str, Any], norm: Dict[str, str],
                   edit_map: Dict[str, Any], who: str, new_review: bool) -> Dict[str, Any]:
    """Finish a proposal whose change is logged but which never reached ``proposals/done/`` (the run stopped
    between the two): move it there under that change instead of applying its ops a second time. The per-op
    results were not kept, so each op records the change it went in with."""
    results: Dict[str, Any] = {}
    for op in proposal.get("ops") or []:
        key = str(op.get("n"))
        results[key] = {"skipped": "rejected"} if norm.get(key) == "reject" else {"recovered": change.get("id")}
    applied = {"at": change.get("at") or util.now_iso(), "change": change.get("id"),
               "results": {k: results[k] for k in sorted(results, key=int)}}
    _save(repo, _final(proposal, applied, norm, edit_map, who, new_review and not proposal.get("review")))
    return dict(applied, ids=list(change.get("ids") or []), proposal=proposal["id"], note="already applied")


def conflict_ops(exc: Conflict) -> List[Any]:
    """The op numbers a ``Conflict`` names (a convenience for renderers)."""
    return list(exc.ops)


def age_days(proposal: Dict[str, Any]) -> int:
    """Whole days since the proposal was created (0 when unknown)."""
    try:
        delta = util.now() - util.parse_ts(str(proposal.get("created")))
    except ValueError:
        return 0
    return max(0, int(math.floor(delta.total_seconds() / 86400.0)))
