"""Pack loading and the kind and relation registry.

A pack declares kinds, relations, dimensions, deliverable templates and (for the local pack) kind maps; a question
bank ``<pack>.questions.jsonl`` sits next to it. ``ontology.json`` lists the packs in use: ``core`` (always first),
``discovery`` (on by default) and ``local`` (``packs/local.pack.json`` in the topic repo). Built-in packs live in
``ontokit/packs/``; an opt-in one (``assessment``: risks, controls and their calibration checks) is turned on with
``onto pack add <name>`` (``mutate.add_pack``), which lists it before ``local``.

Kind identity is ``(pack, kind)``. A pack that arrives with an import is *shared* when a loaded pack has the same
``pack`` name and the same sha256 of its canonical bytes: its kinds keep their bare names. A built-in pack of the same
name whose bytes differ (a kit upgrade on either side, or parents released on different kits) is shared kind by kind:
a kind declared compatibly (the same dimension, and fields where one set holds the other with every shared field
declared alike) keeps its bare name, so bridges between imports keep using the built-in relations. Every other kind
and relation of an unshared pack registers qualified as ``<ns>/<name>`` (relations also keep their bare name when
nothing local uses it), and the kinds its relations name are qualified the same way. Output qualifies a kind only on
a collision, as ``plot (garden)``.

Edges keep the bare relation name, so two imports may declare one name differently (``yields`` with other ends and
another inverse). Every relation lookup takes the namespace whose declaration governs the edge (``ns``: the import
an edge comes from, or the one import both ends of a local edge sit in) and reads ``<ns>/<rel>`` first; a W04 load
warning names the clash. ``allowed`` without a namespace accepts an edge any declaration of the name allows.

Kind aliases (old names) and plurals resolve to the kind. Problems are P20 (P01 for an unreadable file); nothing is
dropped silently.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import ids, schema_lite, store, util
from .errors import DataError, Problem, Refused, UsageError

KIT_DIR = os.path.dirname(os.path.abspath(__file__))
BUILTIN_DIR = os.path.join(KIT_DIR, "packs")
PACK_SCHEMA_PATH = os.path.join(KIT_DIR, "schema", "pack.schema.json")
LOCAL = "local"
LOCAL_PACK = "packs/local.pack.json"
LOCAL_QUESTIONS = "packs/local.questions.jsonl"
DEFAULT_PACKS = ("core", "discovery")
DEFAULT_TEXT = ("name", "summary")

_SCHEMA: Optional[Dict[str, Any]] = None
_BUILTIN_CACHE: Dict[str, Tuple[Tuple[int, int], Any]] = {}


def pack_schema() -> Dict[str, Any]:
    global _SCHEMA
    if _SCHEMA is None:
        with open(PACK_SCHEMA_PATH, encoding="utf-8") as fh:
            _SCHEMA = json.load(fh)
    return _SCHEMA


def check_pack(pack: Any) -> List[str]:
    """``pack.schema.json`` errors for a pack object."""
    return schema_lite.validate(pack, "pack", pack_schema())


def check_question(question: Any) -> List[str]:
    return schema_lite.validate(question, "question", pack_schema())


def sha_of(pack: Dict[str, Any]) -> str:
    """sha256 of a pack's canonical bytes."""
    return util.sha256_hex(util.canonical_bytes(pack))


def empty_local_pack() -> Dict[str, Any]:
    """The local pack ``onto init`` writes: no kinds, no relations."""
    return {
        "pack": LOCAL,
        "version": 1,
        "extends": ["core"],
        "kinds": {},
        "relations": {},
        "dimensions": {},
        "deliverables": {},
        "kind_map": [],
    }


def field_schema_problems(fields: Dict[str, Any]) -> List[str]:
    """Messages for field schemas schema_lite cannot check (unsupported keywords, remote refs)."""
    wrapped = {"type": "object", "additionalProperties": False, "properties": fields}
    try:
        list(schema_lite.iter_errors({}, wrapped, {}))
        for name, schema in sorted(fields.items()):
            list(schema_lite.iter_errors(None, schema, {}))
    except schema_lite.SchemaUnsupported as exc:
        return [str(exc)]
    return []


def field_errors(attrs: Dict[str, Any], fields: Dict[str, Any]) -> List[str]:
    """schema_lite errors for a node's ``attrs`` against its kind's ``fields``."""
    wrapped = {"type": "object", "additionalProperties": False, "properties": fields}
    return ["%s: %s" % (schema_lite.json_path(path), msg) for path, msg in schema_lite.iter_errors(attrs, wrapped, {})]


def _read_builtin(name: str) -> Tuple[Optional[Dict[str, Any]], List[Tuple[int, Dict[str, Any]]], List[str]]:
    """(pack, numbered questions, error messages) for a built-in pack, cached by file mtime and size."""
    path = os.path.join(BUILTIN_DIR, "%s.pack.json" % name)
    qpath = os.path.join(BUILTIN_DIR, "%s.questions.jsonl" % name)
    try:
        st = os.stat(path)
    except OSError:
        return None, [], ["unknown pack %r (no built-in %s.pack.json)" % (name, name)]
    try:
        qst = os.stat(qpath)
        qkey = (qst.st_mtime_ns, qst.st_size)
    except OSError:
        qkey = (0, -1)
    key = (st.st_mtime_ns, st.st_size, qkey)
    hit = _BUILTIN_CACHE.get(name)
    if hit and hit[0] == key:
        pack, questions, errors = hit[1]
        return copy.deepcopy(pack), copy.deepcopy(questions), list(errors)
    errors: List[str] = []
    try:
        pack = store.read_json(path)
    except DataError as exc:
        pack, errors = None, [str(exc)]
    numbered, qproblems = store.read_jsonl_lines(qpath)
    errors.extend("%s:%d: %s" % (os.path.basename(qpath), n, msg) for n, msg in qproblems)
    _BUILTIN_CACHE[name] = (key, (pack, numbered, errors))
    return copy.deepcopy(pack), copy.deepcopy(numbered), list(errors)


def builtin_names() -> List[str]:
    """The built-in packs the kit ships (``ontokit/packs/<name>.pack.json``), sorted."""
    suffix = ".pack.json"
    try:
        names = os.listdir(BUILTIN_DIR)
    except OSError:
        return []
    return sorted(n[:-len(suffix)] for n in names if n.endswith(suffix) and n[:-len(suffix)] != LOCAL)


def available() -> List[Dict[str, Any]]:
    """One entry per built-in pack: ``{name, title, description, kinds, relations, questions}`` (the opt-in packs,
    such as ``assessment``, are turned on with ``onto pack add <name>``)."""
    out = []
    for name in builtin_names():
        pack, numbered, _errors = _read_builtin(name)
        pack = pack if isinstance(pack, dict) else {}
        out.append({"name": name, "title": str(pack.get("title") or name),
                    "description": str(pack.get("description") or ""),
                    "kinds": sorted(pack.get("kinds") or {}), "relations": sorted(pack.get("relations") or {}),
                    "questions": len(numbered)})
    return out


def _rel_shape(decl: Dict[str, Any]) -> str:
    """What makes two declarations of one relation name read alike: ends (kind names without the import namespace
    they were qualified with), inverse, symmetry and brief."""
    out = {k: decl.get(k) for k in ("inverse", "symmetric", "brief")}
    for side in ("from", "to"):
        value = decl.get(side)
        out[side] = sorted(str(k).split("/")[-1] for k in value) if isinstance(value, list) else value
    return util.canonical_line(out)


def _decl_allows(decl: Dict[str, Any], src_kind: str, dst_kind: str) -> bool:
    def ok(side: Any, kind: str) -> bool:
        if side == "*":
            return True
        return isinstance(side, list) and ("*" in side or kind in side)

    frm, to = decl.get("from"), decl.get("to")
    if ok(frm, src_kind) and ok(to, dst_kind):
        return True
    return bool(decl.get("symmetric")) and ok(frm, dst_kind) and ok(to, src_kind)


class Registry(object):
    """Kinds, relations, dimensions, deliverables and questions of the loaded packs (plus imported packs)."""

    def __init__(self) -> None:
        self._packs: Dict[str, Dict[str, Any]] = {}  # "core", "local", "garden/local" -> pack
        self._pack_files: Dict[str, str] = {}
        self._shared: Dict[str, str] = {}  # "garden/core" -> "core" when an imported pack is shared
        self._shared_kinds: Dict[str, Set[str]] = {}  # "garden" -> kinds of its differing built-in packs read bare
        self._kinds: Dict[str, Dict[str, Any]] = {}  # "crop" or "garden/crop" -> declaration
        self._kind_pack: Dict[str, str] = {}
        self._kind_alias: Dict[str, str] = {}
        self._relations: Dict[str, Dict[str, Any]] = {}
        self._rel_pack: Dict[str, str] = {}
        self._rel_alias: Dict[str, str] = {}
        self._dimensions: Dict[str, Dict[str, Any]] = {}
        self._imported_dims: Dict[str, Dict[str, Any]] = {}
        self._deliverables: Dict[str, Dict[str, Any]] = {}
        self._questions: Dict[str, Dict[str, Any]] = {}
        self._kind_map: List[Dict[str, str]] = []
        self._problems: List[Problem] = []
        self._warnings: List[Problem] = []  # W04: two imports declare one relation name differently
        self._import_ns: Set[str] = set()  # namespaces whose packs were loaded (qualified kinds of others are inert)

    # building ----------------------------------------------------------------------------------------------
    def _problem(self, file: str, message: str, line: int = 0, code: str = "P20") -> None:
        self._problems.append(Problem(code, file, line, message))

    def _add_pack(self, key: str, pack: Dict[str, Any], file: str, ns: Optional[str],
                  qualify: Set[str], shared: Iterable[str] = ()) -> None:
        """Register one pack. ``ns`` is None for a loaded pack; ``qualify`` holds the kind names of this import's
        unshared packs (they register as ``ns/kind``); ``shared`` the kinds read as the loaded kind of the same name
        (not registered again)."""
        self._packs[key] = pack
        self._pack_files[key] = file
        shared_set = set(shared)

        def kkey(name: str) -> str:
            if ns and name in qualify and "/" not in name:
                return "%s/%s" % (ns, name)
            return name

        for name, decl in sorted((pack.get("kinds") or {}).items()):
            if not isinstance(decl, dict) or (ns and name in shared_set):
                continue
            key_name = kkey(name)
            if key_name in self._kinds:
                self._problem(file, "duplicate kind %r (already declared by %s)" % (key_name, self._kind_pack[key_name]))
                continue
            self._kinds[key_name] = decl
            self._kind_pack[key_name] = key
            if ns is None:
                problems = field_schema_problems(decl.get("fields") or {})
                for message in problems:
                    self._problem(file, "kind %s: fields: %s" % (name, message))
                fields = decl.get("fields") or {}
                for field in decl.get("expected") or []:
                    if field not in fields:
                        self._problem(file, "kind %s: expected field %r is not in fields" % (name, field))
        for name, decl in sorted((pack.get("kinds") or {}).items()):
            if not isinstance(decl, dict) or (ns and name in shared_set):
                continue
            for alias in decl.get("aliases") or []:
                akey = "%s/%s" % (ns, alias) if ns else alias
                if akey in self._kinds or akey in self._kind_alias:
                    self._problem(file, "kind alias %r of %s collides with a kind or another alias" % (alias, name))
                    continue
                self._kind_alias[akey] = kkey(name)
        for name, decl in sorted((pack.get("relations") or {}).items()):
            if not isinstance(decl, dict):
                continue
            decl = dict(decl)
            if ns:
                for side in ("from", "to"):
                    if isinstance(decl.get(side), list):
                        decl[side] = [k if k == "*" else kkey(k) for k in decl[side]]
                qualified = "%s/%s" % (ns, name)
                self._relations[qualified] = decl
                self._rel_pack[qualified] = key
                if name not in self._relations:
                    self._relations[name] = decl
                    self._rel_pack[name] = key
                elif "/" in self._rel_pack[name] and _rel_shape(self._relations[name]) != _rel_shape(decl):
                    other = self._rel_pack[name].split("/", 1)[0]
                    self._warnings.append(Problem("W04", file, 0, "relation %s: the imports %s and %s declare it "
                                                                  "differently; each edge reads with its own import's "
                                                                  "declaration" % (name, other, ns)))
                continue
            if name in self._relations:
                self._problem(file, "duplicate relation %r (already declared by %s)" % (name, self._rel_pack[name]))
                continue
            self._relations[name] = decl
            self._rel_pack[name] = key
            for alias in decl.get("aliases") or []:
                if alias in self._relations or alias in self._rel_alias:
                    self._problem(file, "relation alias %r of %s collides" % (alias, name))
                    continue
                self._rel_alias[alias] = name
        target = self._imported_dims if ns else self._dimensions
        for name, decl in sorted((pack.get("dimensions") or {}).items()):
            if name in target:
                if not ns:
                    self._problem(file, "duplicate dimension %r" % name)
                continue
            target[name] = decl
        if ns is None:
            for name, decl in sorted((pack.get("deliverables") or {}).items()):
                if name in self._deliverables:
                    self._problem(file, "duplicate deliverable template %r" % name)
                    continue
                self._deliverables[name] = decl

    def _add_questions(self, numbered: Sequence[Tuple[int, Dict[str, Any]]], file: str) -> None:
        for line, question in numbered:
            errors = check_question(question)
            if errors:
                for message in errors:
                    self._problem(file, message, line)
                continue
            qid = question["id"]
            if qid in self._questions:
                self._problem(file, "duplicate question id %r" % qid, line)
                continue
            self._questions[qid] = question

    def _check_references(self) -> None:
        for key, pack in sorted(self._packs.items()):
            if "/" in key:
                continue
            file = self._pack_files[key]
            for name in pack.get("extends") or []:
                if name not in self._packs:
                    self._problem(file, "extends %r, which is not loaded" % name)
            for name, decl in sorted((pack.get("relations") or {}).items()):
                for side in ("from", "to"):
                    value = decl.get(side)
                    for kind in value if isinstance(value, list) else []:
                        if kind != "*" and self.kind_key(kind) is None and not self._inert(kind):
                            self._problem(file, "relation %s: %s kind %r is not declared" % (name, side, kind))
            for name, decl in sorted((pack.get("kinds") or {}).items()):
                for exp in decl.get("expects") or []:
                    if self.relation(exp.get("rel", "")) is None:
                        self._problem(file, "kind %s: expects unknown relation %r" % (name, exp.get("rel")))
                dim = decl.get("dimension")
                if dim and dim not in self._dimensions and key not in DEFAULT_PACKS:
                    self._problem(file, "kind %s: dimension %r is not declared" % (name, dim))
            for entry in pack.get("kind_map") or []:
                for side in ("a", "b"):
                    value = entry.get(side, "")
                    if self.kind_key(value) is None and not self._inert(value):
                        self._problem(file, "kind_map: kind %r is not declared" % value)

    def _inert(self, kind: Any) -> bool:
        """A qualified kind (``garden/crop``) whose namespace is not imported is inert until it is imported again:
        pack ops are additive, so nothing could remove the entry (``import remove`` would stay blocked)."""
        if not isinstance(kind, str) or "/" not in kind:
            return False
        prefix = kind.split("/", 1)[0]
        if prefix == "self":
            return False
        return prefix not in self._import_ns

    # kinds -------------------------------------------------------------------------------------------------
    def kind_key(self, name: str, ns: Optional[str] = None) -> Optional[str]:
        """The registry key of a kind (``crop``, or ``garden/crop`` for an unshared import), following aliases.
        With ``ns`` the import's own kind is tried first, then the shared bare kind."""
        if not isinstance(name, str) or not name:
            return None
        if "/" in name:
            if name in self._kinds:
                return name
            if name in self._kind_alias:
                return self._kind_alias[name]
            prefix, bare = name.split("/", 1)
            if prefix == "self":
                return self.kind_key(bare)
            return self.kind_key(bare) if self._ns_shares(prefix, bare) else None
        if ns and ns != "self":
            qualified = "%s/%s" % (ns, name)
            if qualified in self._kinds:
                return qualified
            if qualified in self._kind_alias:
                return self._kind_alias[qualified]
        if name in self._kinds:
            return name
        if name in self._kind_alias:
            return self._kind_alias[name]
        if ns is None:
            matches = [k for k in self._kinds if "/" in k and k.split("/", 1)[1] == name]
            if len(matches) == 1:
                return matches[0]
        return None

    def _ns_shares(self, ns: str, bare: str) -> bool:
        """True when ``ns/bare`` is a kind of a shared pack (the import uses the same pack as this topic)."""
        if bare not in self._kinds and bare not in self._kind_alias:
            return False
        key = self._kind_alias.get(bare, bare)
        if key in self._shared_kinds.get(ns, set()):
            return True
        pack = self._kind_pack.get(key, "")
        return any(k.startswith(ns + "/") and v == pack for k, v in self._shared.items())

    def namespaces(self) -> List[str]:
        return sorted({k.split("/", 1)[0] for k in list(self._packs) + list(self._shared) if "/" in k})

    def kind(self, name: str, ns: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """The kind declaration, accepting ``ns/kind``, a namespace hint and kind aliases."""
        key = self.kind_key(name, ns)
        return self._kinds.get(key) if key else None

    def kinds(self) -> List[str]:
        return sorted(self._kinds)

    def kind_pack(self, name: str) -> Optional[str]:
        key = self.kind_key(name)
        return self._kind_pack.get(key) if key else None

    def display(self, name: str) -> str:
        """How output names a kind: bare, or ``plot (garden)`` when the bare name collides."""
        key = self.kind_key(name) or name
        if "/" not in key:
            return key
        ns, bare = key.split("/", 1)
        others = [k for k in self._kinds if k != key and (k == bare or k.endswith("/" + bare))]
        return "%s (%s)" % (bare, ns) if others else bare

    def plural_alias(self, word: str) -> Optional[str]:
        """The kind a plural (``datasets``, ``people``) names, or None."""
        text = util.name_key(word or "")
        if not text:
            return None
        found = []
        for key, decl in sorted(self._kinds.items()):
            bare = key.split("/", 1)[-1]
            plurals = {util.name_key(decl.get("plural") or ""), util.name_key(bare + "s")}
            if text in plurals:
                found.append(key)
        local = [k for k in found if "/" not in k]
        if local:
            return local[0]
        return found[0] if len(found) == 1 else None

    def _decl(self, kind: str) -> Dict[str, Any]:
        return self.kind(kind) or {}

    def label(self, kind: str) -> str:
        return str(self._decl(kind).get("label") or kind)

    def expected(self, kind: str) -> List[str]:
        return list(self._decl(kind).get("expected") or [])

    def expects(self, kind: str) -> List[Dict[str, Any]]:
        return [dict(e) for e in self._decl(kind).get("expects") or []]

    def fields(self, kind: str) -> Dict[str, Any]:
        return dict(self._decl(kind).get("fields") or {})

    def text_fields(self, kind: str) -> List[str]:
        return list(self._decl(kind).get("text") or DEFAULT_TEXT)

    def is_hub(self, kind: str) -> bool:
        return bool(self._decl(kind).get("hub"))

    def has_card(self, kind: str) -> bool:
        return bool(self._decl(kind).get("card"))

    def default_visibility(self, kind: str) -> str:
        return str(self._decl(kind).get("visibility") or "shared")

    def priority(self, kind: str) -> int:
        """Tie-break weight for id resolution: higher is preferred. Unknown kinds get 0."""
        return int(self._decl(kind).get("priority") or 0)

    def boost(self, kind: str) -> float:
        return float(self._decl(kind).get("boost") or 0)

    # relations ---------------------------------------------------------------------------------------------
    def relation(self, name: str, ns: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """The declaration of relation ``name``. With ``ns`` (an import namespace) the import's own declaration
        ``<ns>/<name>`` wins over the bare one, which another import may have declared differently."""
        if not isinstance(name, str):
            return None
        if ns and ns != "self" and "/" not in name:
            own = self._relations.get("%s/%s" % (ns, name))
            if own is not None:
                return own
        if name in self._relations:
            return self._relations[name]
        if name in self._rel_alias:
            return self._relations[self._rel_alias[name]]
        return None

    def relations(self) -> List[str]:
        return sorted(self._relations)

    def warnings(self) -> List[Problem]:
        return list(self._warnings)

    def inverse(self, rel: str, ns: Optional[str] = None) -> str:
        """The declared inverse name; a symmetric relation is its own inverse; otherwise ``rel + "_by"``."""
        decl = self.relation(rel, ns)
        if decl:
            if decl.get("inverse"):
                return str(decl["inverse"])
            if decl.get("symmetric"):
                return rel
        return rel + "_by"

    def forward(self, label: str) -> Optional[Tuple[str, bool]]:
        """``(rel, reversed)`` for a relation name or an inverse name; None when unknown."""
        if self.relation(label) is not None:
            return self._rel_alias.get(label, label), False
        for name, decl in sorted(self._relations.items()):
            if decl.get("inverse") == label and "/" not in name:
                return name, True
        for name, decl in sorted(self._relations.items()):
            if decl.get("inverse") == label:
                return name, True
        return None

    def allowed(self, rel: str, src_kind: str, dst_kind: str, ns: Optional[str] = None) -> bool:
        """True when ``rel`` may link a ``src_kind`` node to a ``dst_kind`` node (either way when symmetric), read
        with ``ns``'s declaration. Without ``ns``, any declaration of the name that allows it will do (two imports
        may each declare the name)."""
        decl = self.relation(rel, ns)
        if decl is None:
            return False
        src = self.kind_key(src_kind) or src_kind
        dst = self.kind_key(dst_kind) or dst_kind
        if _decl_allows(decl, src, dst):
            return True
        if ns or "/" in rel:
            return False
        return any(_decl_allows(d, src, dst) for key, d in sorted(self._relations.items())
                   if key.endswith("/" + rel) and key.split("/", 1)[1] == rel)

    def is_symmetric(self, rel: str, ns: Optional[str] = None) -> bool:
        return bool((self.relation(rel, ns) or {}).get("symmetric"))

    def is_brief(self, rel: str, ns: Optional[str] = None) -> bool:
        return bool((self.relation(rel, ns) or {}).get("brief"))

    # dimensions, questions, deliverables -------------------------------------------------------------------
    def dimensions(self) -> Dict[str, Dict[str, Any]]:
        return {k: dict(v) for k, v in self._dimensions.items()}

    def dimension_of(self, kind: str) -> Optional[str]:
        dim = self._decl(kind).get("dimension")
        if dim and (dim in self._dimensions or dim in self._imported_dims):
            return str(dim)
        return None

    def stage_of(self, dimension: str) -> Optional[int]:
        decl = self._dimensions.get(dimension) or self._imported_dims.get(dimension)
        return int(decl["stage"]) if decl and "stage" in decl else None

    def kinds_of_dimension(self, dimension: str) -> List[str]:
        return [k for k in self.kinds() if "/" not in k and self.dimension_of(k) == dimension]

    def questions(self) -> List[Dict[str, Any]]:
        """Every question bank merged, sorted by id."""
        return [copy.deepcopy(self._questions[q]) for q in sorted(self._questions)]

    def deliverables(self) -> Dict[str, Dict[str, Any]]:
        return copy.deepcopy(self._deliverables)

    def kind_map(self) -> List[Dict[str, str]]:
        return [dict(e) for e in self._kind_map]

    # packs -------------------------------------------------------------------------------------------------
    def pack_names(self) -> List[str]:
        """Loaded pack names in load order (imports excluded)."""
        return [k for k in self._packs if "/" not in k]

    def pack(self, name: str) -> Optional[Dict[str, Any]]:
        return self._packs.get(name)

    def pack_sha(self, name: str) -> Optional[str]:
        """sha256 of a pack's canonical bytes (``ns/name`` for an imported pack), None when unknown."""
        if name in self._packs:
            return sha_of(self._packs[name])
        if name in self._shared:
            return sha_of(self._packs[self._shared[name]])
        return None

    def shared(self) -> Dict[str, str]:
        """``{"garden/core": "core"}`` for imported packs identical to a loaded one."""
        return dict(self._shared)

    def problems(self) -> List[Problem]:
        return list(self._problems)


def load(repo_or_none: Optional[store.Repo], manifest: Optional[Dict[str, Any]] = None,
         extra_packs: Iterable[Tuple[str, Dict[str, Any]]] = (), local_pack: Optional[Dict[str, Any]] = None,
         local_questions: Optional[Sequence[Dict[str, Any]]] = None) -> Registry:
    """The registry for a topic: the packs ``manifest["packs"]`` names (``core`` always first), their question
    banks, then ``extra_packs`` as ``(ns, pack)`` pairs from imports. ``local_pack`` and ``local_questions`` stand
    in for ``packs/local.pack.json`` and ``packs/local.questions.jsonl`` (a writer checks a changed local pack in
    memory before it writes it); they apply even without a repo."""
    if manifest is None:
        manifest = repo_or_none.manifest if repo_or_none is not None else {}
    reg = Registry()
    names = [n for n in (manifest.get("packs") or list(DEFAULT_PACKS)) if isinstance(n, str)]
    if not names or names[0] != "core":
        reg._problem("ontology.json", "packs must start with core")
        names = ["core"] + [n for n in names if n != "core"]
    seen: Set[str] = set()
    for name in names:
        if name in seen:
            reg._problem("ontology.json", "pack %r is listed twice" % name)
            continue
        seen.add(name)
        if name == LOCAL:
            if repo_or_none is None and local_pack is None:
                continue
            file = LOCAL_PACK
            if local_pack is not None:
                pack = local_pack
            else:
                try:
                    pack = store.read_json(repo_or_none.path(LOCAL_PACK), None)  # type: ignore[union-attr]
                except DataError as exc:
                    reg._problem(file, str(exc), code="P01")
                    continue
            if pack is None:
                reg._problem(file, "missing; ontology.json lists the local pack")
                continue
            if local_questions is not None:
                numbered = [(n, q) for n, q in enumerate(sorted(
                    (q for q in local_questions if isinstance(q, dict)), key=lambda q: str(q.get("id") or "")),
                    start=1)]
            elif repo_or_none is not None:
                numbered, qproblems = store.read_jsonl_lines(repo_or_none.path(LOCAL_QUESTIONS))
                for line, message in qproblems:
                    reg._problem(LOCAL_QUESTIONS, message, line, code="P01")
            else:
                numbered = []
            qfile = LOCAL_QUESTIONS
        else:
            file = "kit:packs/%s.pack.json" % name
            qfile = "kit:packs/%s.questions.jsonl" % name
            pack, numbered, errors = _read_builtin(name)
            for message in errors:
                reg._problem(file, message)
            if pack is None:
                continue
        errors = check_pack(pack)
        if errors:
            for message in errors:
                reg._problem(file, message)
            if not isinstance(pack, dict):
                continue
        if pack.get("pack") != name:
            reg._problem(file, "declares pack %r but is loaded as %r" % (pack.get("pack"), name))
        reg._add_pack(name, pack, file, None, set())
        if name == LOCAL:
            reg._kind_map = [dict(e) for e in pack.get("kind_map") or [] if isinstance(e, dict)]
        reg._add_questions(numbered, qfile)
    _add_extra(reg, extra_packs)
    reg._check_references()
    return reg


def _add_extra(reg: Registry, extra_packs: Iterable[Tuple[str, Dict[str, Any]]]) -> None:
    by_ns: Dict[str, List[Dict[str, Any]]] = {}
    for ns, pack in extra_packs:
        by_ns.setdefault(ns, []).append(pack)
    reg._import_ns = set(by_ns)
    for ns in sorted(by_ns):
        file = "imports/%s/export.json" % ns
        unshared: List[Dict[str, Any]] = []
        for pack in by_ns[ns]:
            errors = check_pack(pack)
            if errors:
                for message in errors:
                    reg._problem(file, "pack %s: %s" % (pack.get("pack") if isinstance(pack, dict) else "?", message))
                if not isinstance(pack, dict):
                    continue
            name = str(pack.get("pack") or "")
            loaded = reg._packs.get(name)
            if loaded is not None and sha_of(loaded) == sha_of(pack):
                reg._shared["%s/%s" % (ns, name)] = name
            else:
                unshared.append(pack)
        qualify: Set[str] = set()
        same: Set[str] = set()
        for pack in unshared:
            # a built-in pack of another kit version, or the parent's local pack beside this topic's (a composed
            # topic often starts from its parent's pack, then either side grows it): a kind both declare alike
            # stays one kind, so an additive change on either side never turns the bridges on it into P09
            name = str(pack.get("pack") or "")
            same_named = name == LOCAL or reg._pack_files.get(name, "").startswith("kit:")
            loaded_kinds = (reg._packs.get(name) or {}).get("kinds") or {} if same_named else {}
            for kind, decl in (pack.get("kinds") or {}).items():
                if kind in loaded_kinds and compatible_kinds(loaded_kinds[kind], decl):
                    same.add(kind)
                else:
                    qualify.add(kind)
        same -= qualify  # a name another unshared pack of this import declares differently stays qualified
        if same:
            reg._shared_kinds[ns] = same
        for pack in unshared:
            reg._add_pack("%s/%s" % (ns, pack.get("pack")), pack, file, ns, qualify, same)


def compatible_kinds(a: Any, b: Any) -> bool:
    """Two declarations of one kind (in a built-in pack, or in two topics' local packs) that read as the same kind:
    the same dimension, and fields where one set of names holds the other and every shared field is declared alike
    (an additive change, such as a new optional field in a newer kit or a field one topic added)."""
    if not isinstance(a, dict) or not isinstance(b, dict) or a.get("dimension") != b.get("dimension"):
        return False
    fa, fb = a.get("fields") or {}, b.get("fields") or {}
    if not isinstance(fa, dict) or not isinstance(fb, dict) or not (set(fa) <= set(fb) or set(fb) <= set(fa)):
        return False
    return all(util.canonical_line(fa[k]) == util.canonical_line(fb[k]) for k in set(fa) & set(fb))


# changes to the local pack -------------------------------------------------------------------------------------
def check_change(old: Dict[str, Any], new: Dict[str, Any], used_kinds: Iterable[str] = (),
                 used_rels: Optional[Dict[str, Iterable[Tuple[str, str]]]] = None,
                 file: str = LOCAL_PACK) -> List[Problem]:
    """P20 for every kind or relation in use that ``new`` removes or narrows. ``used_kinds`` are kind names with
    nodes; ``used_rels`` maps a relation to the ``(src_kind, dst_kind)`` pairs its edges link."""
    problems: List[Problem] = []
    used = set(used_kinds)
    old_kinds, new_kinds = old.get("kinds") or {}, new.get("kinds") or {}
    for name in sorted(set(old_kinds) & used):
        if name not in new_kinds:
            problems.append(Problem("P20", file, 0, "kind %s is in use and cannot be removed" % name))
            continue
        old_fields = old_kinds[name].get("fields") or {}
        new_fields = new_kinds[name].get("fields") or {}
        for field in sorted(old_fields):
            if field not in new_fields:
                problems.append(Problem("P20", file, 0, "kind %s is in use: field %s cannot be removed" % (name, field)))
            elif util.canonical_line(old_fields[field]) != util.canonical_line(new_fields[field]):
                problems.append(Problem("P20", file, 0, "kind %s is in use: field %s cannot be changed" % (name, field)))
    old_rels, new_rels = old.get("relations") or {}, new.get("relations") or {}
    for rel, pairs in sorted((used_rels or {}).items()):
        if rel not in old_rels:
            continue
        if rel not in new_rels:
            problems.append(Problem("P20", file, 0, "relation %s is in use and cannot be removed" % rel))
            continue
        if bool(old_rels[rel].get("symmetric")) != bool(new_rels[rel].get("symmetric")):
            problems.append(Problem("P20", file, 0, "relation %s is in use: symmetric cannot change" % rel))
        for src, dst in sorted(set(pairs)):
            if not _decl_allows(new_rels[rel], src, dst):
                problems.append(
                    Problem("P20", file, 0, "relation %s is in use from %s to %s and cannot be narrowed" % (rel, src, dst))
                )
    return problems


def apply_op(pack: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """A copy of the local pack with one additive op applied (``add_kind``, ``add_relation``, ``add_field``,
    ``map_kinds``). Anything that would replace or remove is ``Refused``; the result must pass the pack schema."""
    out = copy.deepcopy(pack)
    kind = op.get("op")
    if kind == "add_kind":
        name = op.get("name")
        if not isinstance(name, str) or not ids.KIND_RE.match(name) or name in ids.RESERVED_KINDS:
            raise Refused("add_kind: %r is not a valid kind name" % (name,))
        if name in (out.get("kinds") or {}):
            raise Refused("add_kind: kind %s already exists; packs only grow (use add_field)" % name)
        out.setdefault("kinds", {})[name] = copy.deepcopy(op.get("kind") or {})
    elif kind == "add_relation":
        name = op.get("name")
        if not isinstance(name, str) or not ids.REL_RE.match(name):
            raise Refused("add_relation: %r is not a valid relation name" % (name,))
        if name in (out.get("relations") or {}):
            raise Refused("add_relation: relation %s already exists" % name)
        out.setdefault("relations", {})[name] = copy.deepcopy(op.get("relation") or {})
    elif kind == "add_field":
        name, field = op.get("kind"), op.get("field")
        kinds = out.get("kinds") or {}
        if name not in kinds:
            raise Refused("add_field: kind %s is not declared in the local pack" % (name,))
        fields = kinds[name].setdefault("fields", {})
        if field in fields:
            raise Refused("add_field: %s already has field %s" % (name, field))
        schema = copy.deepcopy(op.get("schema") or {})
        problems = field_schema_problems({str(field): schema})
        if problems:
            raise Refused("add_field: %s" % problems[0], problems=problems)
        fields[field] = schema
    elif kind == "map_kinds":
        entry = {"a": op.get("a"), "b": op.get("b")}
        entries = out.setdefault("kind_map", [])
        if entry not in entries and {"a": entry["b"], "b": entry["a"]} not in entries:
            entries.append(entry)
    else:
        raise UsageError("not a pack op: %r" % (kind,))
    errors = check_pack(out)
    if errors:
        # the reasons, so the op can be fixed ("$.relations.x.brief: 'text' is not of type 'boolean'")
        raise Refused("%s: the local pack would fail its schema: %s%s" % (
            kind, "; ".join(errors[:3]), " (and %d more)" % (len(errors) - 3) if len(errors) > 3 else ""),
            problems=errors)
    return out
