"""The decision ledger (``ledger/decisions/dec-*.json``) and the change log (``ledger/changes.jsonl``).

Decisions are immutable. A reversal is a new decision naming the old one in ``supersedes``; the kit then rewrites
only the old decision's ``status`` (to ``superseded``) and ``superseded_by``. A scope entry ``<id>#attrs.<field>``
records which value of a disagreeing field holds (a ``same_as`` conflict); the decision then stores the values it
settled in ``settles``, so a new disagreeing value opens the conflict again. A decision may *narrow* one active
decision (``narrows``): it makes the broader choice more specific, and both stay active. ``narrowed_by`` lists the
active decisions that narrow one (derived, never stored). Scopes are ids, namespaces or areas:
two scopes match when they are equal, or when one is a prefix of the other that ends at, or is followed by, one of
``/ : . # @`` (so ``crop:mint`` matches ``crop:mint.leaf`` but not ``crop:mint-2``). An empty scope list matches
nothing.

Every kit write appends one change line (C.13). A ``checkpoint`` change records ``done``, ``next`` and
``open_questions`` in place of ``ids``; that is how a session is resumed.

Decision and checkpoint text go through the source sanitizer first, as proposal text does: a credential (a token,
or a password, PIN or door code in prose) refuses the write, naming only its kinds, and personal data is redacted
or refused as ``policy.personal`` says. Both files are append-only or immutable, so nothing unsafe may land there.
A decision that supersedes another writes the new decision, the flip of the old one and the change line as one
write: a process killed half way leaves an intent the next writer rolls back (``store.begin_write``).
"""

from __future__ import annotations

import glob
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Union

from . import ids as idmod
from . import records, secrets, store, util
from .errors import DataError, NotFound, Refused, UsageError

DECISIONS_DIR = "ledger/decisions"
CHANGES = "ledger/changes.jsonl"
SCOPE_SEPARATORS = "/:.#@"
CHANGE_TYPES = ("init", "apply", "answer", "ingest", "import", "decide", "erase", "release", "migrate", "checkpoint",
                "pack")
DECIDERS = ("user", "owner", "team", "agent")
_SINCE_RE = re.compile(r"^([0-9]+)([dhw])$")


# decisions -----------------------------------------------------------------------------------------------------
def decision_path(repo: store.Repo, dec_id: str) -> str:
    return repo.path("%s/%s.json" % (DECISIONS_DIR, dec_id))


def all_decisions(repo: store.Repo) -> Dict[str, Dict[str, Any]]:
    """``{id: decision}`` for every readable decision file (bad files are skipped; ``validate`` reports them)."""
    out: Dict[str, Dict[str, Any]] = {}
    for path in sorted(glob.glob(os.path.join(repo.path(DECISIONS_DIR), "dec-*.json"))):
        try:
            value = store.read_json(path)
        except (DataError, OSError):
            continue
        if isinstance(value, dict) and isinstance(value.get("id"), str):
            out[value["id"]] = value
    return out


def load_decision(repo: store.Repo, dec_id: str) -> Dict[str, Any]:
    found = all_decisions(repo).get(dec_id)
    if found is None:
        raise NotFound("decision %s is not in the ledger" % dec_id, searched=dec_id)
    return found


def _options(options: Iterable[Any]) -> List[Dict[str, str]]:
    """``[{id, label}]`` from ``"id=label"`` strings, bare labels or ``{id, label}`` objects."""
    out: List[Dict[str, str]] = []
    for option in options or ():
        if isinstance(option, dict):
            oid, label = str(option.get("id") or "").strip(), str(option.get("label") or "").strip()
        else:
            text = str(option).strip()
            if "=" in text:
                oid, label = [part.strip() for part in text.split("=", 1)]
            else:
                oid, label = util.slugify(text)[:60], text
        if not oid or not label:
            raise UsageError("option %r needs an id and a label (id=label)" % (option,))
        if any(o["id"] == oid for o in out):
            raise UsageError("option id %r is listed twice" % oid)
        out.append({"id": oid, "label": label})
    return out


def _dedupe(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    for value in values or ():
        text = str(value).strip()
        if text and text not in out:
            out.append(text)
    return out


def _refuse_secrets(record: Dict[str, Any], what: str) -> None:
    hits = secrets.scan_str(util.canonical_line(record))
    if hits:
        kinds = sorted({kind for kind, _match in hits})
        raise Refused("%s holds credential-like text (%s); nothing was written" % (what, ", ".join(kinds)),
                      kinds=kinds)


def clean_text(repo: store.Repo, text: str, what: str) -> str:
    """``text`` through the source sanitizer under the topic's policy (as proposal free text is): a credential
    refuses the write, naming only its kinds; personal data is redacted, or refused, as ``policy.personal`` says."""
    from . import sources as sources_mod  # lazy: sources is a sibling at the same layer

    kinds = sorted({kind for kind, _match in secrets.scan_str(text)})
    if kinds:
        raise Refused("%s holds credential-like text (%s); nothing was written" % (what, ", ".join(kinds)),
                      kinds=kinds)
    try:
        clean, _found = sources_mod.default_sanitizer()(text, repo.policy)
    except Refused as exc:
        found = list(exc.extra.get("kinds") or []) if hasattr(exc, "extra") else []
        raise Refused("%s holds data that may not be stored (%s); nothing was written. Remove it and try again"
                      % (what, ", ".join(found) or "see policy.personal"), kinds=found)
    if clean.endswith("\n") and not text.endswith("\n"):
        clean = clean[:-1]
    return clean


def clean_texts(repo: store.Repo, groups: Sequence[Sequence[str]], what: str) -> List[List[str]]:
    """Every text of ``groups`` through ``clean_text``, in the same shape. A refusal names every kind found in any
    of them at once (credentials first, as ``clean_text`` checks them first), so one retry fixes them all."""
    out: List[List[str]] = []
    credentials: Set[str] = set()
    personal: Set[str] = set()
    for group in groups:
        cleaned: List[str] = []
        for text in group:
            kinds = {kind for kind, _match in secrets.scan_str(text)}
            if kinds:
                credentials |= kinds
                continue
            try:
                cleaned.append(clean_text(repo, text, what))
            except Refused as exc:
                found = set(exc.extra.get("kinds") or []) if hasattr(exc, "extra") else set()
                personal |= found or {"see policy.personal"}
        out.append(cleaned)
    if credentials:
        kinds = sorted(credentials)
        raise Refused("%s holds credential-like text (%s); nothing was written" % (what, ", ".join(kinds)),
                      kinds=kinds)
    if personal:
        kinds = sorted(personal)
        raise Refused("%s holds data that may not be stored (%s); nothing was written. Remove it and try again"
                      % (what, ", ".join(kinds)), kinds=[k for k in kinds if k != "see policy.personal"])
    return out


def decide(
    repo: store.Repo,
    question: str,
    options: Iterable[Any],
    chosen: str,
    chosen_text: Optional[str] = None,
    recommended: Optional[str] = None,
    decided_by: str = "user",
    rationale: str = "",
    scope: Sequence[str] = (),
    supersedes: Optional[str] = None,
    narrows: Optional[str] = None,
) -> Dict[str, Any]:
    """Record a decision; flip the one it supersedes; append a ``decide`` change. Returns the decision.

    ``chosen`` is an option id, or ``other`` with ``chosen_text`` in the user's words. Without options, ``chosen``
    is free text. The same active decision recorded twice (same question, choice, scope and narrowed decision) is
    returned as it is. ``narrows`` names one active decision this one makes more specific; it stays active."""
    question = " ".join(str(question or "").split())
    if not question:
        raise UsageError("a decision needs a question")
    question = clean_text(repo, question, "the decision question")
    opts = [dict(o, label=clean_text(repo, o["label"], "option %s" % o["id"])) for o in _options(options)]
    if chosen_text is not None and str(chosen_text).strip():
        chosen_text = clean_text(repo, str(chosen_text), "chosen_text")
    rationale = clean_text(repo, " ".join(str(rationale or "").split()), "the rationale") if rationale else ""
    chosen = str(chosen or "").strip()
    if not chosen:
        raise UsageError("a decision needs chosen")
    if not opts:  # free text: chosen is the answer itself
        chosen = clean_text(repo, chosen, "chosen")
    option_ids = [o["id"] for o in opts]
    if opts and chosen not in option_ids + ["other"]:
        raise UsageError("chosen %r is not one of the options (%s) or 'other'" % (chosen, ", ".join(option_ids)))
    if chosen == "other" and not (chosen_text or "").strip():
        raise UsageError("chosen other needs chosen_text with the user's own answer")
    if recommended is not None:
        recommended = str(recommended).strip()
        if recommended.lower() in ("", "none", "null"):
            recommended = None
        elif opts and recommended not in option_ids:
            raise UsageError("recommended %r is not one of the options (%s)" % (recommended, ", ".join(option_ids)))
    if decided_by not in DECIDERS:
        raise UsageError("decided_by must be one of %s" % ", ".join(DECIDERS))
    scope_refs = _dedupe(scope)
    narrows = str(narrows or "").strip() or None
    if narrows and supersedes and narrows == str(supersedes).strip():
        raise UsageError("narrows and supersedes both name %s; a decision either replaces another or narrows it"
                         % narrows)
    with store.write_lock(repo):
        ledger = all_decisions(repo)
        if narrows:
            target = ledger.get(narrows)
            if target is None:
                raise NotFound("decision %s is not in the ledger" % narrows, searched=narrows)
            if target.get("status") != "active":
                raise Refused("%s is superseded by %s; narrow the active decision instead"
                              % (narrows, target.get("superseded_by")))
            if supersedes:
                # the replacement of X may not narrow a decision that (through its own narrows) narrows X: the new,
                # broader choice would narrow its own narrower, and W10's advice would never settle
                replaced = str(supersedes).strip()
                cur, seen = target, set()
                # a hand-written narrows that is not one id (a list, say) ends the walk: validate reports it (P21)
                while isinstance(cur, dict) and isinstance(cur.get("narrows"), str) and cur.get("narrows") \
                        and isinstance(cur.get("id"), str) and cur.get("id") not in seen:
                    seen.add(cur.get("id"))
                    if cur.get("narrows") == replaced:
                        broader = (ledger.get(replaced) or {}).get("narrows")
                        raise Refused("%s narrows %s (directly or through another decision), which this decision "
                                      "supersedes, so it cannot also narrow %s; "
                                      "%s" % (narrows, replaced, narrows,
                                              "narrow %s (what %s narrows) instead" % (broader, replaced) if broader
                                              else "record it without narrows"))
                    cur = ledger.get(cur.get("narrows"))
        old = None
        if supersedes:
            old = ledger.get(supersedes)
            if old is None:
                raise NotFound("decision %s is not in the ledger" % supersedes, searched=supersedes)
            if old.get("status") != "active":
                raise Refused("%s is already superseded by %s; supersede that one instead"
                              % (supersedes, old.get("superseded_by")))
        at = util.now_iso()
        body = {"question": question, "chosen": chosen, "scope": scope_refs}
        record: Dict[str, Any] = {
            "at": at,
            "question": question,
            "options": opts,
            "recommended": recommended,
            "chosen": chosen,
            "chosen_text": (chosen_text or "").strip() or None,
            "decided_by": decided_by,
            "rationale": " ".join(str(rationale or "").split()),
            "scope": scope_refs,
            "status": "active",
            "supersedes": supersedes or None,
            "superseded_by": None,
        }
        if narrows:  # only when given, so decisions without it keep their bytes
            record["narrows"] = narrows
        same = [
            d for d in ledger.values()
            if d.get("status") == "active" and d.get("question") == question and d.get("chosen") == chosen
            and d.get("scope") == scope_refs and d.get("options") == opts and not supersedes
            and (d.get("narrows") or None) == narrows
        ]
        if same:
            return same[0]
        settles = _conflict_settles(repo, scope_refs)
        if settles:
            record["settles"] = settles
        taken = set(ledger)
        record["id"] = idmod.record_id("dec", body, date=at[:10], slug=question, taken=taken)
        errors = records.check(record, "decision")
        if errors:
            raise UsageError("the decision fails its schema: %s" % "; ".join(errors[:3]), problems=errors)
        _refuse_secrets(record, "the decision")
        planned = [("%s/%s.json" % (DECISIONS_DIR, record["id"]), util.canonical_bytes(record))]
        changed = [record["id"]]
        if old is not None:
            flipped = dict(old, status="superseded", superseded_by=record["id"])
            planned.append(("%s/%s.json" % (DECISIONS_DIR, old["id"]), util.canonical_bytes(flipped)))
            changed.append(old["id"])
        label = next((o["label"] for o in opts if o["id"] == chosen), record["chosen_text"] or chosen)
        # one all-or-nothing write: the new decision, the flip of the old one and the change line (a kill between
        # them would leave two active decisions and a P21 no command can clear)
        saved = {rel: _read_or_none(repo.path(rel)) for rel, _data in planned + [(CHANGES, b"")]}
        store.begin_write(repo, [(rel, saved[rel], data) for rel, data in planned], [(CHANGES, saved[CHANGES])])
        try:
            for rel, data in planned:
                store.write_bytes(repo.path(rel), data)
            append_change(
                repo, "decide", _by(decided_by), changed,
                util.normalize_ws("decided: %s -> %s" % (question[:200], label[:120]))[:600],
            )
        except BaseException:
            for rel, data in saved.items():
                _put_back(repo.path(rel), data)
            store.end_write(repo)
            store.clear_cache()
            raise
        store.end_write(repo)
    return record


def narrowed_by(decisions: Dict[str, Dict[str, Any]]) -> Dict[str, List[str]]:
    """``{decision id: [ids of the active decisions that narrow it]}``, ids sorted."""
    out: Dict[str, List[str]] = {}
    for did, dec in sorted(decisions.items()):
        target = dec.get("narrows") if isinstance(dec, dict) else None
        if isinstance(target, str) and target and dec.get("status") == "active":
            out.setdefault(target, []).append(did)
    return out


def active_end(decisions: Dict[str, Dict[str, Any]], dec_id: str) -> Optional[str]:
    """The decision that now stands for ``dec_id``: ``dec_id`` itself while it is active, else the end of its
    ``superseded_by`` chain when that end is active; None when the chain breaks, loops or ends in a decision that
    is not active (so the advice never names a decision that ``decide`` would refuse to narrow)."""
    seen: Set[str] = set()
    here: Optional[str] = dec_id
    while isinstance(here, str) and here and here not in seen:
        seen.add(here)
        dec = decisions.get(here)
        if not isinstance(dec, dict):
            return None
        if dec.get("status") == "active":
            return here
        here = dec.get("superseded_by")
    return None


def _conflict_settles(repo: store.Repo, scope: Sequence[str]) -> List[Dict[str, Any]]:
    """``[{id, field, values}]`` for the scope entries that name a record with a field (``<id>#attrs.<field>``,
    ``needs.decide_scope``) whose ``same_as`` class disagrees on it now: the values this decision settles. When an
    import update brings another value, the set differs and the conflict opens again (``needs.conflict_settled``)."""
    wanted = [str(s) for s in scope if "#" in str(s)]
    if not wanted:
        return []
    from . import needs  # lazy: needs reads the ledger
    from .graph import Ontology

    try:
        onto = Ontology.load(repo)
    except (DataError, OSError):
        return []
    out: List[Dict[str, Any]] = []
    for item in wanted:
        rid, _sep, field = item.partition("#")
        rid = onto.own_local(rid.strip())
        field = field.strip()
        if rid not in onto.nodes or not field:
            continue
        values = needs.conflict_values(onto, rid, field)
        if values:
            out.append({"id": rid, "field": field if field.startswith("attrs.") else "attrs." + field,
                        "values": values})
    return out


def _read_or_none(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _put_back(path: str, data: Optional[bytes]) -> None:
    """A file as it was before a failed write (None: it did not exist)."""
    if data is None:
        if os.path.exists(path):
            os.unlink(path)
    else:
        store.write_bytes(path, data)


def _by(who: str) -> str:
    return who if who in ("user", "agent", "kit") else "user"


def norm_scope(scope: Any) -> str:
    """A scope as ``scope_overlaps`` compares it: trimmed, lowercased, without a leading ``./``."""
    return re.sub(r"^\./", "", str(scope).strip().lower())


_norm = norm_scope


def _prefix(a: str, b: str) -> bool:
    """``a`` is ``b``, or a prefix of ``b`` that ends at a separator or is followed by one."""
    if a == b:
        return True
    if not a or not b.startswith(a):
        return False
    return a[-1] in SCOPE_SEPARATORS or b[len(a)] in SCOPE_SEPARATORS


def scope_overlaps(a: Union[str, Sequence[str]], b: Union[str, Sequence[str]]) -> bool:
    """True when a scope in ``a`` matches a scope in ``b`` (each a string or a list). An empty list matches
    nothing."""
    left = [a] if isinstance(a, str) else list(a or ())
    right = [b] if isinstance(b, str) else list(b or ())
    for x in left:
        for y in right:
            nx, ny = _norm(x), _norm(y)
            if nx and ny and (_prefix(nx, ny) or _prefix(ny, nx)):
                return True
    return False


def _haystack(rec: Dict[str, Any]) -> str:
    parts = [rec.get("id"), rec.get("question"), rec.get("rationale"), rec.get("chosen"), rec.get("chosen_text")]
    parts += [o.get("label") for o in rec.get("options") or [] if isinstance(o, dict)]
    parts += list(rec.get("scope") or [])
    return " ".join(str(p) for p in parts if p).lower()


def read_decisions(repo: store.Repo, scope: Optional[Sequence[str]] = None, active: bool = True,
                   text: Optional[str] = None) -> List[Dict[str, Any]]:
    """Decisions, newest first. ``scope`` None reads every scope; a list (even empty) keeps the decisions whose scope
    overlaps it (so an empty list reads none). ``active`` keeps only active ones. Every word in ``text`` must
    appear in the decision."""
    if isinstance(scope, str):
        scope = [scope]
    words = (text or "").lower().split()
    out = []
    for rec in all_decisions(repo).values():
        if active and rec.get("status") != "active":
            continue
        if scope is not None and not scope_overlaps(rec.get("scope") or [], list(scope)):
            continue
        if words:
            hay = _haystack(rec)
            if not all(w in hay for w in words):
                continue
        out.append(rec)
    out.sort(key=lambda r: (str(r.get("at") or ""), str(r.get("id") or "")), reverse=True)
    return out


# changes -------------------------------------------------------------------------------------------------------
def changes_path(repo: store.Repo) -> str:
    return repo.path(CHANGES)


def append_change(
    repo: store.Repo,
    type: str,
    by: str,
    ids: Optional[Iterable[str]],
    summary: str,
    proposal: Optional[str] = None,
    source: Optional[str] = None,
    before: Optional[str] = None,
    after: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    **fields: Any,
) -> Dict[str, Any]:
    """Append one change line and return it. ``ids`` lists the records touched (None leaves the key out, as a
    checkpoint does); ``extra`` is stored under the key ``extra``; ``fields`` may add ``done``, ``next`` and
    ``open_questions`` (checkpoints only)."""
    if type not in CHANGE_TYPES:
        raise UsageError("change type must be one of %s" % ", ".join(CHANGE_TYPES))
    body: Dict[str, Any] = {
        "at": util.now_iso(),
        "type": type,
        "by": _by(by),
        "proposal": proposal,
        "source": source,
        "summary": util.normalize_ws(summary)[:600],
        "before": before,
        "after": after,
    }
    if ids is not None:
        body["ids"] = _dedupe(ids)
    if extra:
        body["extra"] = dict(extra)
    for key in ("done", "next", "open_questions"):
        if key in fields:
            body[key] = [util.normalize_ws(str(x))[:600] for x in fields.pop(key) or () if str(x).strip()]
    if fields:
        raise UsageError("unknown change fields: %s" % ", ".join(sorted(fields)))
    with store.write_lock(repo):
        rows, _problems = store.read_jsonl(changes_path(repo))
        taken = {r.get("id") for r in rows if isinstance(r.get("id"), str)}
        change = dict(body, id=idmod.record_id("chg", body, date=body["at"][:10], taken=taken))
        errors = records.check(change, "change")
        if errors:
            raise UsageError("the change fails its schema: %s" % "; ".join(errors[:3]), problems=errors)
        store.append_jsonl(changes_path(repo), change)
    return change


def _since(since: Any) -> Optional[datetime]:
    if since is None or since == "":
        return None
    if isinstance(since, datetime):
        return since
    text = str(since).strip()
    m = _SINCE_RE.match(text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "w": timedelta(weeks=n)}[unit]
        return util.now() - delta
    try:
        return util.parse_ts(text)
    except ValueError:
        raise UsageError("since must be a date, a timestamp or a span such as 7d, got %r" % text)


def read_changes(repo: store.Repo, since: Any = None) -> List[Dict[str, Any]]:
    """Change lines in file order; ``since`` (a date, a timestamp or a span such as ``7d``) keeps the later ones."""
    rows, _problems = store.read_jsonl(changes_path(repo))
    start = _since(since)
    if start is None:
        return rows
    out = []
    for row in rows:
        try:
            at = util.parse_ts(str(row.get("at")))
        except ValueError:
            continue
        if at >= start:
            out.append(row)
    return out


def last_change(repo: store.Repo, exclude: Sequence[str] = ("checkpoint",)) -> Optional[str]:
    """The id of the last change line whose type is not in ``exclude``, or None."""
    rows, _problems = store.read_jsonl(changes_path(repo))
    for row in reversed(rows):
        if row.get("type") not in exclude and isinstance(row.get("id"), str):
            return row["id"]
    return None


def checkpoint(repo: store.Repo, done: Iterable[str], next: Iterable[str], open_questions: Iterable[str],
               by: str = "agent") -> Dict[str, Any]:
    """Append a ``checkpoint`` change: what was done, what comes next and the open questions. Each item goes
    through ``clean_text`` first: the change log is append-only, so a credential there could never be removed."""
    done_l, next_l, open_l = clean_texts(repo, [[str(x) for x in items or () if str(x).strip()]
                                                for items in (done, next, open_questions)], "the checkpoint")
    summary = "checkpoint: %d done, %d next, %d open" % (len(done_l), len(next_l), len(open_l))
    return append_change(repo, "checkpoint", by, None, summary, done=done_l, next=next_l, open_questions=open_l)


def last_checkpoint(repo: store.Repo) -> Optional[Dict[str, Any]]:
    rows, _problems = store.read_jsonl(changes_path(repo))
    for row in reversed(rows):
        if row.get("type") == "checkpoint":
            return row
    return None
