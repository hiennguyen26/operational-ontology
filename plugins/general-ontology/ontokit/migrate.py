"""Format migrations for topic repos.

``ontology.json`` records the data ``format`` and the ``kit`` version that last wrote it. ``MIGRATIONS[n]`` turns
a repo at format ``n - 1`` into format ``n``; the kit ships format 1, so the table is empty. A repo newer than the
running kit is refused ("kit too old: upgrade the kit"). ``run`` applies the missing steps under the write lock,
stamps ``format`` and ``kit`` in ``ontology.json`` and appends a ``migrate`` change.

``KIT_STEPS[v]`` lists the names the built-in core pack gained in kit ``v``. A topic stamped by an older kit is
checked against every newer entry up to the running kit: a local kind, alias, relation or question that already
uses one of those names would now clash with the core pack (P20). No pack op renames or removes a local name, so
``migrate`` folds each clash into the core pack itself (``fold_plan``), but only when both mean the same: the local
kind of that name (or a local kind named like a core alias) is dropped, so its records read as the core kind and
keep their ids; a local alias of that name is dropped from its kind; the local relation of that name is dropped, so
its edges read with the core one; and the local question with that id is dropped, so the core question takes its
place and its answers. A folded question takes the core ``priority``, which orders the interview, so the fold
line names the change when the priorities differ.

The same meaning is checked strictly. A local question folds only when it has the same ``ask`` (spacing collapsed,
case folded), ``fills``, ``options`` and ``for_gap`` as the core question, and the same interview rules
(``repeatable``, ``quick``, ``follow_ups``, ``when``, ``until``, the stage it is asked in and its ``dimension``)
(``question_differs``); otherwise its answers would attach to a question that asks something else, or the
interview would ask it another way, so ``migrate`` refuses and names
``--rename-question OLD=NEW``, which moves the local question to a new local id with its log lines and every
reference to it (``rename_plan``) before the folds are planned. A local kind or relation that an active record uses
folds only when its declaration states nothing else than the core one: a kind's ``label`` and ``dimension``
(``kind_differs``), a relation's ``symmetric`` (``relation_differs``). A fold is also made only when the folded
topic validates with no problem the topic does not have now (checked on a scratch copy, so ``--check`` writes
nothing); otherwise ``migrate`` refuses, names the records that do not fit and writes nothing. The renamed files,
the folded pack and questions, the stamp and the change line are one write (``store.begin_write``): a failure puts
every file back, and a kill half way leaves an intent the next writer rolls back. The plan that is written is made
under the write lock, so a write another command makes meanwhile is never overwritten (``run``).

``--check`` lists the renames and the folds as steps. Kit 0.2.0 needs no other data step: its new fields
(``narrows`` on decisions, ``checked_on`` on premises) are optional, and the ``assessment`` pack is opt in (``onto
pack add``), so a clean 0.1.0 topic only gets the stamp. Kit 0.3.0 adds no core pack name and changes no data
format (it adds the agent harnesses, which live outside the topic data), so it has no ``KIT_STEPS`` entry: its one
step is the stamp, ``stamp kit 0.3.0 (was 0.2.0)``.
"""

from __future__ import annotations

import copy
import glob
import os
import re
import shutil
import tempfile
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import FORMAT, __version__, ids, store, util
from .errors import DataError, UsageError

MIGRATIONS: Dict[int, Callable[[store.Repo], None]] = {}
KIT_STEPS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    # "kinds" holds kind names and their aliases (one name space); "aliases" maps an alias to its kind
    "0.2.0": {"kinds": ("premise", "assumption"), "aliases": ("assumption=premise",), "relations": ("rests_on",),
              "questions": ("q.constraints.premises", "q.gap.archived_premise", "q.vocab.kinds")},
}
LOCAL_PACK = "packs/local.pack.json"
LOCAL_QUESTIONS = "packs/local.questions.jsonl"
INTERVIEW_LOG = "interview/log.jsonl"
CHANGES = "ledger/changes.jsonl"
QID_RE = re.compile(r"^q\.[a-z0-9][a-z0-9._-]{0,79}\Z")  # interview.QID_RE (a test keeps them equal)


def _version_key(value: object) -> Optional[Tuple[int, ...]]:
    parts = str(value).split(".")
    return tuple(int(p) for p in parts) if len(parts) == 3 and all(p.isdigit() for p in parts) else None


def kit_steps(repo: store.Repo) -> List[str]:
    """The ``KIT_STEPS`` versions newer than the topic's ``kit`` stamp and not newer than the running kit, in
    order. A missing or unreadable stamp checks them all."""
    have, running = _version_key(repo.manifest.get("kit")), _version_key(__version__)
    out = []
    for version in sorted(KIT_STEPS, key=lambda v: _version_key(v) or (0,)):
        key = _version_key(version)
        if key is None or (running is not None and key > running):
            continue
        if have is None or have < key:
            out.append(version)
    return out


def _local_names(repo: store.Repo) -> Tuple[Dict[str, str], List[str], List[str]]:
    """``({kind name or alias: what declares it}, relation names, question ids)`` of the topic's local pack."""
    kinds: Dict[str, str] = {}
    relations: List[str] = []
    try:
        pack = store.read_json(repo.path(LOCAL_PACK), None)
    except (DataError, ValueError):
        pack = None
    if isinstance(pack, dict):
        for name, decl in sorted((pack.get("kinds") or {}).items()):
            kinds.setdefault(str(name), "kind %s" % name)
            for alias in (decl.get("aliases") or []) if isinstance(decl, dict) else []:
                kinds.setdefault(str(alias), "alias %s of kind %s" % (alias, name))
        relations = sorted(str(r) for r in (pack.get("relations") or {}))
    rows, _bad = store.read_jsonl(repo.path(LOCAL_QUESTIONS))
    questions = sorted(str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("id"))
    return kinds, relations, questions


def clashes(repo: store.Repo, versions: Sequence[str]) -> List[str]:
    """One line per local name that a core pack name added in ``versions`` collides with (empty when none)."""
    kinds, relations, questions = _local_names(repo)
    out: List[str] = []
    for version in versions:
        new = KIT_STEPS[version]
        alias_of = dict(a.split("=", 1) for a in new.get("aliases", ()))
        for name in new.get("kinds", ()):
            if name in kinds:
                theirs = ("the alias %s of its kind %s" % (name, alias_of[name]) if name in alias_of
                          else "the kind %s" % name)
                out.append("kit %s: the core pack now declares %s, and %s in %s uses that name"
                           % (version, theirs, kinds[name], LOCAL_PACK))
        for name in new.get("relations", ()):
            if name in relations:
                out.append("kit %s: the core pack now declares the relation %s, and %s declares it too"
                           % (version, name, LOCAL_PACK))
        for qid in new.get("questions", ()):
            if qid in questions:
                out.append("kit %s: the core pack now ships the question %s, and %s holds that id too"
                           % (version, qid, LOCAL_QUESTIONS))
    return out


# folding a clash into the core pack -----------------------------------------------------------------------------
SKIP_COPY = {".git", "plugins", "inbox", ".onto", "build", ".claude", "examples"}  # not read by validate


def _read_local(repo: store.Repo) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    try:
        pack = store.read_json(repo.path(LOCAL_PACK), None)
    except (DataError, ValueError):
        pack = None
    rows, _bad = store.read_jsonl(repo.path(LOCAL_QUESTIONS))
    return (pack if isinstance(pack, dict) else {}), [r for r in rows if isinstance(r, dict)]


def _local_nodes(repo: store.Repo) -> List[Dict[str, Any]]:
    rows, _bad = store.read_jsonl(repo.path("graph/nodes.jsonl"))
    return [r for r in rows if isinstance(r, dict)]


def _norm(value: Any) -> str:
    """Text compared for meaning: spacing collapsed, case folded."""
    return " ".join(str(value or "").split()).casefold()


def _norm_value(value: Any) -> Any:
    """A JSON value with every string normalized (``_norm``), for a strict but spacing and case blind compare."""
    if isinstance(value, str):
        return _norm(value)
    if isinstance(value, list):
        return [_norm_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _norm_value(v) for k, v in sorted(value.items())}
    return value


def _fills(value: Any) -> Any:
    """``fills`` as a set-like value: missing is ``{}``, and each list of names is sorted (the order says nothing)."""
    if not isinstance(value, dict):
        return {}
    return {str(k): sorted(str(x) for x in v) if isinstance(v, list) else v for k, v in sorted(value.items())}


def _predicates(value: Any) -> List[str]:
    """``when`` or ``until`` as a set: missing is empty, and the order says nothing (every predicate must hold)."""
    return sorted(util.canonical_line(p) if isinstance(p, dict) else repr(p)
                  for p in (value if isinstance(value, list) else []))


DEEPEN = 9  # the stage of a question with no stage and no staged dimension (``interview.DEEPEN``)


def _stage(question: Dict[str, Any], dimension_stages: Dict[str, Any]) -> int:
    """The stage a question is asked in, worked out as ``interview._State.bank_stage`` does: its own ``stage``,
    else its dimension's stage, else the open-ended deepening stage."""
    if isinstance(question.get("stage"), int) and not isinstance(question.get("stage"), bool):
        return int(question["stage"])
    dim = question.get("dimension")
    stage = dimension_stages.get(str(dim)) if dim else None
    try:
        return int(stage) if stage is not None and not isinstance(stage, bool) else DEEPEN
    except (TypeError, ValueError):
        return DEEPEN


def _priority(question: Dict[str, Any]) -> int:
    """A question's priority as the interview reads it (missing is 0)."""
    try:
        return int(question.get("priority") or 0)
    except (TypeError, ValueError):
        return 0


def question_differs(local: Dict[str, Any], core: Optional[Dict[str, Any]],
                     dimension_stages: Optional[Dict[str, Any]] = None) -> List[str]:
    """The parts in which a local question means something else than the core question of its id, or is asked
    another way (empty when both are the same): the ``ask`` text (compared with spacing collapsed and case folded),
    the ``fills``, the ``options`` (none, null and an empty list are the same), the gap a gap template asks about
    (``for_gap``), and the interview rules: ``repeatable`` and ``quick`` (missing is false), the ``follow_ups``
    and the ``when`` and ``until`` predicates (missing is none; the order of the predicates says nothing), the
    stage each is asked in (its ``stage``, else its dimension's stage from ``dimension_stages``, else the deepening
    stage, as the interview works it out, so leaving the stage out does not mean "any stage") and the ``dimension``
    (missing is none: closing a dimension or scoring its coverage counts the questions in it). ``why`` only
    explains a question and may differ; ``priority`` may differ too, and the fold line names it (``_folded``)."""
    if core is None:
        return ["the core question is missing"]
    stages = dimension_stages or {}
    out = []
    if _norm(local.get("ask")) != _norm(core.get("ask")):
        out.append("the ask")
    if _fills(local.get("fills")) != _fills(core.get("fills")):
        out.append("the fills")
    if _norm_value(local.get("options") or None) != _norm_value(core.get("options") or None):
        out.append("the options")
    if (local.get("for_gap") or None) != (core.get("for_gap") or None):
        out.append("the gap it asks about")
    if bool(local.get("repeatable")) != bool(core.get("repeatable")):
        out.append("the repeat rule")
    if bool(local.get("quick")) != bool(core.get("quick")):
        out.append("the quick start flag")
    if _stage(local, stages) != _stage(core, stages):
        out.append("the stage")
    if str(local.get("dimension") or "") != str(core.get("dimension") or ""):
        out.append("the dimension")
    for key, what in (("when", "the when rule"), ("until", "the until rule")):
        if _predicates(local.get(key)) != _predicates(core.get(key)):
            out.append(what)
    ups = local.get("follow_ups") if isinstance(local.get("follow_ups"), list) else []
    if [str(f) for f in ups] != [str(f) for f in (core.get("follow_ups") or [])]:
        out.append("the follow-ups")
    return out


def kind_differs(name: str, local: Any, core: Optional[Dict[str, Any]]) -> List[str]:
    """What a local kind declaration states that the core kind does not mean: a ``label`` other than the core
    kind's (or than the name itself, which is the default label), or another ``dimension``. A label or dimension
    the local declaration leaves out states nothing. The description is free prose and is not compared; the fields
    are checked by validating the folded topic."""
    if not isinstance(local, dict) or core is None:
        return []
    out = []
    label = local.get("label")
    if label and _norm(label) not in {_norm(core.get("label")), _norm(name), _norm(name.replace("_", " "))}:
        out.append("label %r (the core kind's is %r)" % (str(label), str(core.get("label"))))
    dim = local.get("dimension")
    if dim and str(dim) != str(core.get("dimension")):
        out.append("dimension %s (the core kind's is %s)" % (dim, core.get("dimension")))
    return out


def relation_differs(local: Any, core: Optional[Dict[str, Any]]) -> List[str]:
    """What a local relation declaration states that the core relation does not mean: the other direction rule
    (``symmetric``). Which kinds it links is checked by validating the folded topic."""
    if not isinstance(local, dict) or core is None:
        return []
    if bool(local.get("symmetric")) != bool(core.get("symmetric")):
        return ["is symmetric and the core relation {name} is not" if local.get("symmetric")
                else "is not symmetric and the core relation {name} is"]
    return []


def _core() -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """``(core pack, {question id: core question})`` of the running kit."""
    from . import packs

    pack, numbered, _errors = packs._read_builtin("core")
    return (pack or {}), {str(q.get("id")): q for _n, q in numbered if isinstance(q, dict)}


def _active(rows: Sequence[Dict[str, Any]], field: str, name: str) -> List[str]:
    return sorted(str(r.get("id")) for r in rows if r.get(field) == name and r.get("status") != "archived")


def _taken_question_ids(repo: store.Repo) -> Set[str]:
    """Every question id the topic's banks hold (core and the other packs, the local bank) or its log names."""
    from . import packs

    taken = {str(q.get("id")) for q in packs.load(repo).questions()}
    taken |= {str(q.get("id")) for q in _read_local(repo)[1] if q.get("id")}
    rows, _bad = store.read_jsonl(repo.path(INTERVIEW_LOG))
    taken |= {str(r.get("q")) for r in rows if isinstance(r, dict) and r.get("q")}
    return taken


def suggest_id(qid: str, taken: Set[str], below: Optional[str] = None) -> str:
    """A free local id to move ``qid`` to: ``q.local.<rest>``, with ``-2``, ``-3`` ... when that is taken too.
    With ``below`` (``template_floor``), the id must also sort before it: then ``q.gap.local.<rest>`` and
    ``q.0.local.<rest>`` are tried in turn. Returns "" when none of them sorts before ``below``."""
    rest = qid[2:] if qid.startswith("q.") else qid
    tail = qid[len("q.gap."):] if qid.startswith("q.gap.") else rest
    bases = ["q.local." + rest]
    if below is not None:
        bases += ["q.gap.local." + tail, "q.0.local." + tail]
    for base in bases:
        base = base[:76]
        out, n = base, 1
        while out in taken or not QID_RE.match(out):
            n += 1
            out = "%s-%d" % (base, n)
        if below is None or out < below:
            return out
    return ""


def template_floor(repo: store.Repo, qid: str, olds: Set[str]) -> Optional[str]:
    """The id a new id for the local gap template ``qid`` must sort before, else None. A gap type's template is the
    lowest bank id carrying its ``for_gap`` (``needs._gap_templates``), and its gap questions are ``<id>.<detail>``.
    When ``qid`` sorts before (or is) every other template of its type, its answered gap questions are the ones
    asked, so a new id that sorts after another template hands the gap to that template and the answers no longer
    close it. The other templates are those of the packs (core first, a core question of the same id included)
    and the local ones not in ``olds``."""
    from . import packs

    local = {str(q.get("id")): q for q in _read_local(repo)[1] if q.get("id")}
    gap = (local.get(qid) or {}).get("for_gap")
    if not gap:
        return None
    others = {str(q.get("id")) for q in packs.load(repo, local_questions=[]).questions()
              if q.get("for_gap") == gap and q.get("id")}
    others |= {i for i, q in local.items() if q.get("for_gap") == gap and i not in olds}
    if not others or qid > min(others):
        return None
    return min(others)


def _definitions(repo: store.Repo) -> Tuple[Set[str], Set[str]]:
    """``(question ids, gap template ids)`` of the topic's banks (the packs and the local bank), not the log."""
    from . import packs

    rows = list(packs.load(repo).questions()) + list(_read_local(repo)[1])
    bank = {str(q.get("id")) for q in rows if q.get("id")}
    return bank, {str(q.get("id")) for q in rows if q.get("id") and q.get("for_gap")}


def _example(qid: str, taken: Set[str], floor: Optional[str]) -> str:
    new = suggest_id(qid, taken, floor)
    return "for example %s" % new if new else "no id of the q.local, q.gap.local or q.0.local forms is free"


def _folded(repo: store.Repo, versions: Sequence[str]) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[str],
                                                                 List[str], List[Tuple[str, str]]]:
    """``(folded pack, folded questions, step lines, blockers, moves)``: the local pack and questions with every
    clashing name of ``versions`` dropped, one line per fold, the reasons a fold of a kind or relation cannot be
    made from the files alone, and ``(question id, reason)`` for each local question that means something else than
    the core question of its id (``question_differs``): its answers and references would attach to the core
    question, so it has to move to another id first (``--rename-question``)."""
    pack, questions = _read_local(repo)
    pack = copy.deepcopy(pack)
    nodes = _local_nodes(repo)
    edges, _bad = store.read_jsonl(repo.path("graph/edges.jsonl"))
    edges = [e for e in edges if isinstance(e, dict)]
    core_pack, core_questions = _core()
    core_kinds = core_pack.get("kinds") if isinstance(core_pack.get("kinds"), dict) else {}
    core_relations = core_pack.get("relations") if isinstance(core_pack.get("relations"), dict) else {}
    core_dimensions = core_pack.get("dimensions") if isinstance(core_pack.get("dimensions"), dict) else {}
    local_dimensions = pack.get("dimensions") if isinstance(pack.get("dimensions"), dict) else {}
    # the topic registry loads the core pack first, so a core dimension wins over a local one of the same name
    dimension_stages = {str(d): decl.get("stage") for dims in (local_dimensions, core_dimensions)
                        for d, decl in dims.items() if isinstance(decl, dict)}
    lines: List[str] = []
    blockers: List[str] = []
    moves: List[Tuple[str, str]] = []
    kinds = pack.get("kinds") if isinstance(pack.get("kinds"), dict) else {}
    relations = pack.get("relations") if isinstance(pack.get("relations"), dict) else {}
    for version in versions:
        new = KIT_STEPS[version]
        alias_of = dict(a.split("=", 1) for a in new.get("aliases", ()))
        for name in new.get("kinds", ()):
            core = alias_of.get(name, name)
            if name in kinds:
                count = sum(1 for n in nodes if n.get("kind") == name)
                used = _active(nodes, "kind", name)
                differs = kind_differs(name, kinds[name], core_kinds.get(core))
                if used and differs:
                    blockers.append("the local kind %s states %s, so it does not mean the core kind %s, and %d "
                                    "active record%s use%s it (%s)"
                                    % (name, " and ".join(differs), core, len(used), "" if len(used) == 1 else "s",
                                       "s" if len(used) == 1 else "", ", ".join(used[:3])))
                    continue
                del kinds[name]
                lines.append("fold the local kind %s into the core kind %s (%d record%s keep their ids)"
                             % (name, core, count, "" if count == 1 else "s"))
            for owner in sorted(kinds):
                decl = kinds[owner]
                aliases = decl.get("aliases") if isinstance(decl, dict) else None
                if isinstance(aliases, list) and name in aliases:
                    stored = sorted(str(n.get("id")) for n in nodes if n.get("kind") == name)
                    if stored:
                        blockers.append("%d record%s stored under the alias %s of the local kind %s (%s) would read "
                                        "as the core kind %s" % (len(stored), "" if len(stored) == 1 else "s", name,
                                                                 owner, ", ".join(stored[:3]), core))
                        continue
                    decl["aliases"] = [a for a in aliases if a != name]
                    lines.append("drop the alias %s from the local kind %s (the core kind %s has it)"
                                 % (name, owner, core))
        for name in new.get("relations", ()):
            if name in relations:
                used = _active(edges, "rel", name)
                differs = relation_differs(relations[name], core_relations.get(name))
                if used and differs:
                    blockers.append("the local relation %s %s, so its %d active edge%s (%s) would read the other "
                                    "way" % (name, differs[0].format(name=name), len(used),
                                             "" if len(used) == 1 else "s", ", ".join(used[:3])))
                    continue
                del relations[name]
                lines.append("fold the local relation %s into the core relation %s" % (name, name))
        for qid in new.get("questions", ()):
            mine = next((q for q in questions if q.get("id") == qid), None)
            if mine is None:
                continue
            differs = question_differs(mine, core_questions.get(qid), dimension_stages)
            if differs:
                moves.append((qid, "the local question %s asks %r and the core question %s asks %r (%s differ%s)"
                              % (qid, util.normalize_ws(str(mine.get("ask") or ""))[:160], qid,
                                 util.normalize_ws(str((core_questions.get(qid) or {}).get("ask") or ""))[:160],
                                 " and ".join(differs), "s" if len(differs) == 1 else "")))
                continue
            questions = [q for q in questions if q.get("id") != qid]
            mine_priority, core_priority = _priority(mine), _priority(core_questions.get(qid) or {})
            order = ("" if mine_priority == core_priority else
                     " (its priority %d gives way to the core priority %d, which changes where the interview asks it)"
                     % (mine_priority, core_priority))
            lines.append("drop the local question %s: the core question %s takes its place%s" % (qid, qid, order))
    return pack, questions, lines, blockers, moves


def _problem_keys(report: Any) -> List[Tuple[str, str, str]]:
    return [(p.code, p.file, p.message) for p in report.problems]


def _copy_topic(repo: store.Repo, files: Dict[str, bytes]) -> Tuple[str, str]:
    """``(scratch folder, copy root)``: the topic's data copied to a scratch folder with ``files`` (``{rel:
    bytes}``) written over it. The caller removes the scratch folder."""
    scratch = tempfile.mkdtemp(prefix="onto-migrate-")
    try:
        copy_root = os.path.join(scratch, "topic")
        os.makedirs(copy_root)
        for name in sorted(os.listdir(repo.root)):
            if name in SKIP_COPY:
                continue
            src = os.path.join(repo.root, name)
            if os.path.isdir(src) and not os.path.islink(src):
                shutil.copytree(src, os.path.join(copy_root, name), symlinks=True)
            elif os.path.isfile(src):
                shutil.copy2(src, os.path.join(copy_root, name))
        for rel, data in sorted(files.items()):
            store.write_bytes(os.path.join(copy_root, *rel.split("/")), data)
    except BaseException:
        shutil.rmtree(scratch, True)
        raise
    return scratch, copy_root


def _clear_caches() -> None:
    from . import graph, validate as validate_mod

    graph.clear_cache()
    store.clear_cache()
    validate_mod.clear_cache()


def fold_plan(repo: store.Repo, versions: Sequence[str]) -> Tuple[List[str], List[str], Dict[str, Any],
                                                                  List[Dict[str, Any]], List[Tuple[str, str]]]:
    """``(step lines, blockers, folded pack, folded questions, moves)`` for the clashes of ``versions``. The fold
    is tried on a scratch copy of the topic and validated there: a problem the folded copy has and the topic does
    not (a record whose attrs the core kind does not declare, an edge the core relation does not allow) is a
    blocker. ``moves`` names the local questions that mean something else than the core question of their id
    (``_folded``). Nothing in the topic is written."""
    from . import validate as validate_mod  # lazy: validate imports this module's peers

    pack, questions, lines, blockers, moves = _folded(repo, versions)
    if blockers or moves or not lines:
        return lines, blockers, pack, questions, moves
    # the records that use a folded name: a problem the topic has on them now comes from the clash itself (the
    # core declaration already reads them), so it is no excuse for the same problem after the fold
    names = {n for v in versions for n in KIT_STEPS[v].get("kinds", ()) + KIT_STEPS[v].get("relations", ())}
    users = {str(n.get("id")) for n in _local_nodes(repo) if n.get("kind") in names}
    rows, _bad = store.read_jsonl(repo.path("graph/edges.jsonl"))
    users |= {str(e.get("id")) for e in rows if isinstance(e, dict) and e.get("rel") in names}
    before = {key for key in _problem_keys(validate_mod.validate(repo))
              if key[0] == "P20" or not any(key[2].startswith(rid + ":") for rid in users)}
    scratch, copy_root = _copy_topic(repo, {LOCAL_PACK: util.canonical_bytes(pack),
                                            LOCAL_QUESTIONS: store.jsonl_bytes(questions, "id")})
    try:
        report = validate_mod.validate(store.Repo.open(copy_root))
        for code, file, message in _problem_keys(report):
            if (code, file, message) not in before:
                blockers.append("%s %s: %s" % (code, file, message))
    finally:
        shutil.rmtree(scratch, True)
        _clear_caches()
    return lines, blockers[:8], pack, questions, moves


# moving a local question to a new id ----------------------------------------------------------------------------
def parse_renames(values: Any) -> List[Tuple[str, str]]:
    """``[(old, new)]`` from ``--rename-question OLD=NEW`` values. Raises ``UsageError`` on a malformed one."""
    if values is None:
        return []
    out: List[Tuple[str, str]] = []
    for value in values if isinstance(values, (list, tuple)) else [values]:
        old, sep, new = str(value).partition("=")
        old, new = old.strip(), new.strip()
        if not sep or not old or not new:
            raise UsageError("--rename-question takes OLD=NEW, two question ids, got %r" % (value,))
        if old == new:
            raise UsageError("--rename-question %s=%s keeps the id; NEW must be a new id" % (old, new))
        for qid in (old, new):
            if not QID_RE.match(qid):
                raise UsageError("%r is not a question id (q. then lowercase letters, digits, dots, - and _)" % qid)
        out.append((old, new))
    olds, news = [o for o, _n in out], [n for _o, n in out]
    for seen in (olds, news):
        twice = sorted({q for q in seen if seen.count(q) > 1})
        if twice:
            raise UsageError("--rename-question names %s twice" % ", ".join(twice))
    both = sorted(set(olds) & set(news))
    if both:
        raise UsageError("--rename-question moves %s and also moves a question to it; rename in two runs" % both[0])
    return out


class _Mapper(object):
    """Maps the stored forms of the renamed question ids: an id, a gap template's ``id.<detail>``, and a
    provenance location ``Q:<id>`` (with an optional ``@<node>``)."""

    def __init__(self, pairs: Sequence[Tuple[str, str]], gap: Set[str], bank: Set[str],
                 templates: Optional[Set[str]] = None) -> None:
        self.pairs = dict(pairs)
        self.gap = gap  # the old ids that are gap templates: their gap questions are ``<id>.<detail>``
        self.bank = bank  # question definitions (not log ids): questions of their own, never a gap question
        # the other gap templates: a gap question belongs to the longest template id it starts with
        self.templates = set(templates or ()) - set(self.pairs)
        self.counts: Dict[str, Dict[str, int]] = {old: {} for old in self.pairs}
        self.hit: Set[str] = set()  # the old ids mapped since the last ``tally``

    def qid(self, value: Any) -> Optional[str]:
        """The new form of ``value`` when it names a renamed question, else None."""
        if not isinstance(value, str):
            return None
        if value in self.pairs:
            self.hit.add(value)
            return self.pairs[value]
        # the longest renamed template first: ``q.gap.orphan.bed.owner`` is a gap question of ``q.gap.orphan.bed``
        # when both are renamed, not of ``q.gap.orphan``
        for old in sorted(self.gap, key=lambda o: (-len(o), o)):
            if value.startswith(old + ".") and value not in self.bank and not any(
                    len(t) > len(old) and value.startswith(t + ".") for t in self.templates):
                self.hit.add(old)
                return self.pairs[old] + value[len(old):]
        return None

    def tally(self, what: str, skip: Optional[str] = None) -> None:
        """Count one ``what`` (a log line, a record, a proposal ...) for each old id mapped since the last call."""
        for old in self.hit:
            if old != skip:
                self.counts[old][what] = self.counts[old].get(what, 0) + 1
        self.hit = set()

    def loc(self, value: Any) -> Optional[str]:
        if not isinstance(value, str) or not value.startswith("Q:"):
            return None
        head, sep, tail = value[2:].partition("@")
        new = self.qid(head)
        return None if new is None else "Q:" + new + sep + tail

    def question(self, q: Dict[str, Any]) -> bool:
        """Rewrite one question definition in place (its id, follow-ups and ``when``/``until`` predicates)."""
        changed = False
        new = self.qid(q.get("id"))
        if new is not None and q.get("id") in self.pairs:
            changed = True
            q["id"] = new
        if isinstance(q.get("follow_ups"), list):
            ups = []
            for f in q["follow_ups"]:
                mapped = self.qid(f)
                if mapped is not None:
                    changed = True
                ups.append(f if mapped is None else mapped)
            q["follow_ups"] = ups
        for key in ("when", "until"):
            for pred in q.get(key) or [] if isinstance(q.get(key), list) else []:
                if not isinstance(pred, dict):
                    continue
                for name in ("answered", "not_answered"):
                    mapped = self.qid(pred.get(name))
                    if mapped is not None:
                        pred[name] = mapped
                        changed = True
        return changed

    def walk(self, value: Any) -> bool:
        """Rewrite every provenance location and every ``add_question`` op's question in a stored record."""
        changed = False
        if isinstance(value, dict):
            loc = self.loc(value.get("loc"))
            if loc is not None:
                value["loc"] = loc
                changed = True
            if value.get("op") == "add_question" and isinstance(value.get("question"), dict):
                changed = self.question(value["question"]) or changed
            for key in sorted(value):
                if key != "loc":
                    changed = self.walk(value[key]) or changed
        elif isinstance(value, list):
            for item in value:
                changed = self.walk(item) or changed
        return changed

    def prefixed(self, text: Any, prefix: str) -> Optional[str]:
        """``text`` with the id after ``prefix`` (up to a space, ``:`` or ``@``) mapped, when it names one."""
        if not isinstance(text, str) or not text.startswith(prefix):
            return None
        rest = text[len(prefix):]
        cut = min([i for i in (rest.find(" "), rest.find(":"), rest.find("@")) if i >= 0] or [len(rest)])
        new = self.qid(rest[:cut])
        return None if new is None else prefix + new + rest[cut:]


def _rewrite_lines(data: Optional[bytes], fix: Callable[[Dict[str, Any]], bool]) -> Optional[bytes]:
    """A JSONL file's bytes with ``fix`` applied to each row: a row it changes is written as one canonical line,
    every other line keeps its bytes. None when nothing changed. Raises ``DataError`` on a line that is not a JSON
    object, so nothing is ever dropped."""
    if data is None:
        return None
    out: List[bytes] = []
    changed = False
    for number, raw in enumerate(data.split(b"\n"), start=1):
        if not raw.strip():
            out.append(raw)
            continue
        try:
            row = util.loads_record(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            row = None
        if not isinstance(row, dict):
            raise DataError("line %d is not a JSON object; fix it (onto validate names it) and run migrate again"
                            % number)
        if fix(row):
            changed = True
            out.append(util.canonical_line(row).encode("utf-8"))
        else:
            out.append(raw)
    return b"\n".join(out) if changed else None


def rename_plan(repo: store.Repo, pairs: Sequence[Tuple[str, str]]) -> Tuple[List[str], Dict[str, bytes]]:
    """``(step lines, {rel: new bytes})`` that move each local question ``old`` to the local id ``new``: its line in
    the local bank, the follow-ups and ``when``/``until`` predicates of the local questions that name it, the
    interview log lines (same ids, same order), the ``Q:<id>`` provenance on nodes and edges, the proposals (their
    ops' provenance, ``add_question`` ops and the answer summary) and the titles of the answer sources. Decisions
    hold no question id (``settles`` names node fields), and the change log is history, so neither changes.
    Raises ``UsageError`` when ``old`` is not a local question or ``new`` is taken. Nothing is written."""
    if not pairs:
        return [], {}
    from . import pipeline

    numbered, bad = store.read_jsonl_lines(repo.path(LOCAL_QUESTIONS))
    if bad:
        raise DataError("%s:%d: %s; fix it and run migrate again" % (LOCAL_QUESTIONS, bad[0][0], bad[0][1]))
    local = {str(q.get("id")): q for _n, q in numbered}
    taken = _taken_question_ids(repo)
    log_ids = {str(r.get("q")) for r in store.read_jsonl(repo.path(INTERVIEW_LOG))[0] if r.get("q")}
    for old, new in pairs:
        if old not in local:
            raise UsageError("%s is not a local question: %s holds %s" % (
                old, LOCAL_QUESTIONS, ", ".join(sorted(local)) or "none"))
        if new in log_ids or any(q.startswith(new + ".") for q in log_ids):
            raise UsageError("the interview log already holds %s; pick another id for %s" % (new, old))
        olds = {o for o, _n in pairs}
        floor = template_floor(repo, old, olds)
        if new in taken:
            raise UsageError("%s is already a question id; pick another id for %s (%s)"
                             % (new, old, _example(old, taken | {n for _o, n in pairs}, floor)))
        if floor is not None and not new < floor:
            raise UsageError(
                "%s is the gap template for %s (the lowest id carrying for_gap is the template), but %s sorts "
                "after the template %s, which would then take its gap questions and ask every gap it answered "
                "again; pick an id that sorts before %s (%s)" % (old, local[old]["for_gap"], new, floor, floor,
                                                               _example(old, taken | {n for _o, n in pairs}, floor)))
    gap = {old for old, _new in pairs if local[old].get("for_gap")}
    bank, templates = _definitions(repo)
    mapper = _Mapper(pairs, gap, bank - set(dict(pairs)), templates)
    files: Dict[str, bytes] = {}
    rows = [copy.deepcopy(q) for _n, q in numbered]
    changed = False
    for q in rows:
        own = q.get("id")
        changed = mapper.question(q) or changed
        mapper.tally("question", skip=own)  # the other local questions that name it
    if changed:
        files[LOCAL_QUESTIONS] = store.jsonl_bytes(rows, "id")

    def counted(what: str, fix: Callable[[Dict[str, Any]], bool]) -> Callable[[Dict[str, Any]], bool]:
        def run_fix(row: Dict[str, Any]) -> bool:
            done = fix(row)
            mapper.tally(what)
            return done
        return run_fix

    def log_fix(row: Dict[str, Any]) -> bool:
        new = mapper.qid(row.get("q"))
        if new is None:
            return False
        row["q"] = new
        return True

    def record_fix(row: Dict[str, Any]) -> bool:
        return mapper.walk(row.get("prov"))

    def source_fix(row: Dict[str, Any]) -> bool:
        title = mapper.prefixed(row.get("title"), "Answer to ") if row.get("kind") == "interview" else None
        if title is None:
            return False
        row["title"] = title
        return True

    for rel, fix in ((INTERVIEW_LOG, counted("interview line", log_fix)),
                     ("graph/nodes.jsonl", counted("record", record_fix)),
                     ("graph/edges.jsonl", counted("record", record_fix)),
                     ("sources/index.jsonl", counted("source", source_fix))):
        data = _rewrite_lines(store._read_or_none(repo.path(rel)), fix)
        if data is not None:
            files[rel] = data
    for folder in (pipeline.PENDING, pipeline.DONE):
        for path in sorted(glob.glob(os.path.join(repo.path(folder), "prop-*.json"))):
            rel = "%s/%s" % (folder, os.path.basename(path))
            try:
                prop = store.read_json(path)
            except (DataError, OSError) as exc:
                raise DataError("%s cannot be read (%s); fix it and run migrate again, so no reference to a "
                                "renamed question is left behind" % (rel, exc))
            if not isinstance(prop, dict):
                raise DataError("%s is not a JSON object; fix it and run migrate again" % rel)
            changed = mapper.walk(prop.get("ops"))
            changed = mapper.walk(prop.get("review")) or changed
            summary = mapper.prefixed(prop.get("summary"), "answer to ")
            if summary is not None:
                prop["summary"] = summary
                changed = True
            mapper.tally("proposal")
            if changed:
                files[rel] = util.canonical_bytes(prop)
    lines = []
    for old, new in pairs:
        counts = mapper.counts[old]
        what = ", ".join("%d %s%s" % (counts[k], k, "" if counts[k] == 1 else "s") for k in sorted(counts))
        lines.append("rename the local question %s to %s (%s)" % (old, new, what or "nothing else names it"))
    return lines, files


def _rename_problems(repo: store.Repo, copy_root: str, pairs: Sequence[Tuple[str, str]]) -> List[str]:
    """The problems the renamed copy has and the topic does not (a problem that only names the new id in place of
    the old one is the same problem)."""
    from . import validate as validate_mod

    before = set()
    for code, file, message in _problem_keys(validate_mod.validate(repo)):
        for old, new in pairs:
            message = message.replace(old, new)
        before.add((code, file, message))
    out = []
    for key in _problem_keys(validate_mod.validate(store.Repo.open(copy_root))):
        if key not in before:
            out.append("%s %s: %s" % key)
    return out[:8]


def _format_of(repo: store.Repo) -> int:
    value = repo.manifest.get("format")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise DataError("ontology.json: format must be a whole number of at least 1, got %r" % (value,))
    return value


def needed(repo: store.Repo) -> List[int]:
    """The formats still to migrate to, in order (empty when current). Raises ``DataError`` when the repo is newer
    than the kit or a step is missing."""
    current = _format_of(repo)
    if current > FORMAT:
        raise DataError(
            "kit too old: upgrade the kit (the topic is format %d, this kit %s reads format %d)"
            % (current, __version__, FORMAT)
        )
    steps = list(range(current + 1, FORMAT + 1))
    missing = [n for n in steps if n not in MIGRATIONS]
    if missing:
        raise DataError("no migration to format %d in kit %s" % (missing[0], __version__))
    return steps


def _append_change(repo: store.Repo, summary: str) -> None:
    """Log the migration through the ledger when it is built, else write the same change line directly."""
    try:
        from . import ledger  # peer module; lazily imported so migrate works before it exists
    except ImportError:
        ledger = None  # type: ignore
    if ledger is not None and hasattr(ledger, "append_change"):
        ledger.append_change(repo, "migrate", "kit", [], summary)
        return
    path = repo.path("ledger/changes.jsonl")
    rows, _problems = store.read_jsonl(path)
    body = {
        "at": util.now_iso(),
        "type": "migrate",
        "by": "kit",
        "proposal": None,
        "source": None,
        "ids": [],
        "summary": summary,
        "before": None,
        "after": None,
    }
    taken = {r.get("id") for r in rows if isinstance(r.get("id"), str)}
    change = dict(body, id=ids.record_id("chg", body, date=body["at"][:10], taken=taken))
    store.append_jsonl(path, change)


RECORD_ADVICE = (
    "Through a reviewed proposal, change those records (update_node can set or unset their attrs), or archive the "
    "record (archived records are history and the fold does not check them) and add what still holds again under "
    "another kind or relation (an edge cannot change its relation), then run onto migrate again")
UNDO = "while the kit merge is not committed yet, undo it (git merge --abort) and keep the old kit for now"


def _refusal(found: Sequence[str], blockers: Sequence[str], moves: Sequence[Tuple[str, str]],
             taken: Set[str], floors: Optional[Dict[str, str]] = None) -> str:
    floors = floors or {}
    parts = ["cannot migrate to kit %s, nothing was written: %s." % (__version__, "; ".join(found))]
    if moves:
        picked: Set[str] = set()
        flags = []
        for qid, _why in moves:
            new = suggest_id(qid, taken | picked, floors.get(qid))
            if not new:
                flags.append("--rename-question %s=NEW (NEW a free id that sorts before %s, which would otherwise "
                             "take its gap questions)" % (qid, floors[qid]))
                continue
            picked.add(new)
            flags.append("--rename-question %s=%s" % (qid, new))
        parts.append(
            "A local question gives way to the core question of its id only when both mean the same and are asked "
            "the same way (the same ask, ignoring case and spacing, the same fills, options and gap, and the "
            "same repeat, quick start, stage, dimension, follow-up, when and until rules), and here %s. Its "
            "answers and every reference to it would attach to the core question, so move it to a new local id "
            "first: onto migrate %s "
            "moves the question, its interview log lines and every reference to it (follow-ups, when and until, "
            "Q: provenance, proposals and answer source titles), checks the result and then migrates, all in one "
            "write (add --check to see the steps first)." % ("; ".join(why for _q, why in moves), " ".join(flags)))
    if blockers:
        parts.append("The kit folds a local name into the core pack only when every active record that uses it fits "
                     "the core declaration and means the same, and here %s. %s." % ("; ".join(blockers),
                                                                                  RECORD_ADVICE))
    parts.append("Or, %s." % UNDO)
    return " ".join(parts)


def _plan(repo: store.Repo, pairs: Sequence[Tuple[str, str]]) -> Tuple[List[int], List[str], Dict[str, bytes],
                                                                     List[str], Dict[str, Any], List[Dict[str, Any]]]:
    """``(format steps, step lines, renamed files, folds, folded pack, folded questions)`` for ``run``, read from
    the topic as it is now. Raises like ``run``; nothing in the topic is written."""
    steps = needed(repo)
    renamed, files = rename_plan(repo, pairs)
    versions = kit_steps(repo)
    folds: List[str] = []
    pack: Dict[str, Any] = {}
    questions: List[Dict[str, Any]] = []
    scratch = None
    try:
        target = repo
        if files:
            scratch, copy_root = _copy_topic(repo, files)
            problems = _rename_problems(repo, copy_root, pairs)
            if problems:
                raise DataError("cannot rename %s, nothing was written: the renamed topic would have these problems: "
                                "%s" % (", ".join("%s to %s" % p for p in pairs), "; ".join(problems)))
            target = store.Repo.open(copy_root)
        found = clashes(target, versions)
        if found:
            folds, blockers, pack, questions, moves = fold_plan(target, versions)
            if blockers or moves:
                olds = {q for q, _why in moves}
                floors = {q: f for q, f in ((q, template_floor(target, q, olds)) for q in sorted(olds))
                          if f is not None}
                raise DataError(_refusal(found, blockers, moves,
                                         _taken_question_ids(target) if moves else set(), floors))
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, True)
            _clear_caches()
    lines = ["migrate to format %d" % n for n in steps] + renamed + folds
    if repo.manifest.get("kit") != __version__:
        lines.append("stamp kit %s (was %s)" % (__version__, repo.manifest.get("kit")))
    return steps, lines, files, folds, pack, questions


def run(repo: store.Repo, check: bool = False, renames: Any = None) -> List[str]:
    """Apply the missing migrations. Returns one line per step (one per renamed question, one per fold of a clash,
    and one for a kit stamp change); with ``check`` nothing is written. Returns ``[]`` when the repo is current.
    ``renames`` (``--rename-question OLD=NEW``) first moves local questions to new ids (``rename_plan``); the
    renamed topic is validated on a scratch copy and the folds are planned on it. Raises ``UsageError`` on a
    rename that cannot be made and ``DataError``, in both modes, when a local name clashes with a core pack name a
    newer kit added and the clash cannot be folded into the core pack (``fold_plan``). The files are written in one
    write: a failure or a kill half way puts every file back.

    The plan is made twice. The first one, without the lock, answers ``check`` and refuses a migration that cannot
    be made, so neither takes the lock. The plan that is written is made again under the write lock, from the
    files as they are then: every byte it writes was read under the lock, so an answer or another write another
    command made after the first plan is moved or kept, never overwritten with stale bytes."""
    needed(repo)
    pairs = parse_renames(renames)
    lines = _plan(repo, pairs)[1]
    if check or not lines:
        return lines
    with store.write_lock(repo):
        repo.reload()
        _clear_caches()
        steps, lines, files, folds, pack, questions = _plan(repo, pairs)
        if not lines:
            return lines
        for n in steps:
            MIGRATIONS[n](repo)
        planned = dict(files)
        if folds:
            planned[LOCAL_PACK] = util.canonical_bytes(pack)
            planned[LOCAL_QUESTIONS] = store.jsonl_bytes(questions, "id")
        repo.reload()
        manifest = dict(repo.manifest, format=FORMAT, kit=__version__)
        planned[store.MANIFEST] = util.canonical_bytes(manifest)
        saved = {rel: store._read_or_none(repo.path(rel)) for rel in sorted(planned)}
        saved_log = store._read_or_none(repo.path(CHANGES))
        rewrites = [(rel, saved[rel], planned[rel]) for rel in sorted(planned) if saved[rel] != planned[rel]]
        store.begin_write(repo, rewrites, [(CHANGES, saved_log)])
        try:
            for rel, _old, data in rewrites:
                store.write_bytes(repo.path(rel), data)
            repo.manifest = manifest
            _append_change(repo, "; ".join(lines))
        except BaseException:
            for rel, old, _data in rewrites + [(CHANGES, saved_log, None)]:
                _put_back(repo.path(rel), old)
            store.end_write(repo)
            repo.reload()
            _clear_caches()
            raise
        store.end_write(repo)
    if folds or files:
        _clear_caches()
    return lines


def _put_back(path: str, data: Optional[bytes]) -> None:
    """Restore a file's bytes (None: the file did not exist)."""
    if data is None:
        if os.path.exists(path):
            os.unlink(path)
        return
    store.write_bytes(path, data)
