"""The ``ingest`` and ``erase`` commands.

``ingest`` stores text, a file or a folder as sources: each input is read as text (``formats``), sanitized
(``sanitize``: credentials refuse, personal data follows the policy), hashed and written by ``sources.add``. It
returns the chunks to read and the calls that read them; it never reads the text for instructions and never drafts
an op. Ingested text is data: every surface marks it ``[untrusted]``.

- A folder is swept in sorted order, non-hidden files only, and is all or nothing: every file is read and checked
  before the first one is stored, so one credential or one unreadable format stores nothing.
- The same text stored twice returns the existing source (``duplicate``) and writes nothing. The same ``url``, or
  the same ``(kind, title)``, with new text supersedes the older source; ``cited_by_old`` lists the records whose
  provenance cites the older one, so their quotes can be checked again.
- Paths must sit inside the topic repo (``inbox/`` is the drop folder); ``--allow-any-path`` lifts that on the
  command line only. Dotfiles, credential files and key folders are always refused.
- The ``url`` is stored as given, so it is checked like the text: a credential in it, or personal data the policy
  does not keep, refuses the input (a redacted address would not work).
- A kept original (``keep_original``) is stored byte for byte, so it is checked before anything is written: a
  secret pattern refuses the input, and so does anything ``sanitize.check_text`` finds in an original of a text
  format. The kit cannot read a binary original (pdf, docx, images, audio) for personal data, so keeping one is
  refused while ``policy.personal`` redacts or refuses any kind; the text alone can still be ingested, and the
  original is then hashed. An original over ``policy.keep_original_max_bytes`` is hashed but not kept, and the
  result says so.
- Interview sources come from ``onto answer`` only: ingested text never gets user trust.
- Each ingest that stores something appends one ``ingest`` change and a richness point, in one all-or-nothing write:
  ``plan_sources`` runs ``sources.add`` on a copy of the index under ``.onto/stage/`` (the topic is not touched),
  then ``write_planned`` records the write intent (``store.begin_write``) and writes the text files, the index, the
  change line and the point. A process killed half way is rolled back by the next writer, so a source is never left
  stored without its ``ingest`` change.
- A folder lists one short line per source (id, file, lines) with the change id on top; over MCP the list stops at
  ``FOLDER_CHARS`` and a ``[page]`` line names the call that lists the rest (``list_call``), so no id is lost to the
  size cap. The JSON result carries every id in file order (``ids``) and ``list_call``.
- Redaction is pattern based, so the output of a new source reminds the reader what it covers (``CONTACT_CHECK``)
  and to check each chunk for contact details it missed.

``erase`` (G.6) needs an active decision whose scope covers the id (equal to it, or a prefix of it ending at one of
``/ : . # @``; the topic's own ``<ns>/`` covers every local id). It goes through ``mutate`` like every graph write.
The id may be written bare, or with the topic's own ``<ns>/`` or ``self/``. Erasing a node also erases the nodes
merged into it (they hold the same thing's name, summary and quotes) when the scope covers them, in an ``erase``
change of their own that comes first; the others are listed as still holding data. The sources those nodes and
their edges cite keep their full text (an interview answer that introduced a person names that person), so erasing
a node also erases each of them the scope covers, in the node's own change, and lists the others as still holding
data with the ``onto erase`` call that clears them. A source can name other things too, so it is never erased
unless the scope covers it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, TypeVar

from . import formats, history, ids, ledger, mutate, render, sanitize, secrets, sources, store, util
from .errors import NotFound, OntoError, Refused, UsageError
from .graph import Ontology
from .graph import clear_cache as clear_graph_cache

INGEST_KINDS = ("note", "file", "url", "transcript")
TITLE_MAX = 200
URL_MAX = 2000
ERASE_NOTE = ("Vendored exports in other repos are not touched, and build outputs and git history keep the old "
              "content until you rebuild and rewrite history. Ids are permanent, so an erased node's id (a slug of "
              "its old name) stays in the files listed as still naming it (graph, change log, decisions, "
              "proposals). onto release --push refuses while unpushed commits still hold it.")
_TOOL_KIND = re.compile(r"^(?:[a-z][a-z0-9-]{0,31}/)?tool$")
# What the redactor finds (``sanitize``, under the default policy), shown after a new source is stored.
CONTACT_CHECK = ("Redaction covers e-mail, street and PO box addresses, cards, government ids, and phone numbers "
                 "written North American style, with a +country code, as national numbers in groups with a leading "
                 "0 (07700 900123, 06 12 34 56 78), or after a word or field name such as phone, mobile, call me "
                 "on or my number is. Check each chunk for contact details it missed: never copy them into ops, and "
                 "tell the user, who can erase the source.")
STAGE_REL = ".onto/stage"  # where plan_sources runs sources.add on a copy of the index (gitignored, kit state)
FOLDER_CHARS = 16000  # over MCP, a folder's source lines stop here (under the server's cap) and a [page] line follows
_OF_N = re.compile(r" \(\d+ of \d+\)\Z")
T = TypeVar("T")


# inputs --------------------------------------------------------------------------------------------------------
class _Item(object):
    """One text to store: the text, its title, its ``original`` block for ``sources.add``, where it came from (a
    path relative to the swept folder, for messages only; it is never stored) and a note for the result."""

    def __init__(self, text: str, title: str, original: Optional[Dict[str, Any]], label: Optional[str],
                 locator: str, note: Optional[str] = None) -> None:
        self.text, self.title, self.original, self.label, self.locator = text, title, original, label, locator
        self.note = note


def _utf8(value: str, what: str) -> None:
    """``UsageError`` when ``value`` cannot be written as UTF-8 (a lone surrogate, such as half an emoji)."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise UsageError("the %s is not valid Unicode text (it holds a lone surrogate, such as half an emoji); fix "
                         "its encoding and try again" % what)


def _timestamp(value: Any, name: str) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        return util.fmt_ts(util.parse_ts(str(value)))
    except ValueError:
        raise UsageError("%s must be a timestamp such as 2026-09-28T14:02:11Z, got %r" % (name, value))


def _days(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        days = int(value)
    except (TypeError, ValueError):
        days = 0
    if isinstance(value, bool) or days < 1 or str(days) != str(value).strip():
        raise UsageError("stale_after_days must be a whole number of days, 1 or more; got %r" % (value,))
    return days


def _via(repo: store.Repo, via: Any) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """The ``tool:`` node that produced the text: ``(id, resolved note or None)``. It must exist."""
    if via in (None, ""):
        return None, None
    text = str(via).strip()
    onto = Ontology.load(repo)
    res = onto.resolve(text, accept=lambda i: bool(_TOOL_KIND.match(onto.kind_of(i))))
    found = res.get("id")
    if not found:
        raise NotFound("%s: not in the ontology as a tool; propose the tool node first" % text,
                       res.get("candidates") or (), ambiguous=bool(res.get("ambiguous")), searched=text)
    if not _TOOL_KIND.match(onto.kind_of(found)):
        raise UsageError("via must name a tool node; %s is a %s" % (found, onto.kind_of(found)))
    note = None if found == text else {"query": text, "id": found, "also": list(res.get("candidates") or [])[:3]}
    return found, note


def _meta(repo: store.Repo, kind: Any, title: Any, url: Any, fetched_at: Any, via: Any,
          stale_after_days: Any) -> Dict[str, Any]:
    kind = str(kind or "note")
    if kind == "interview":
        raise UsageError("interview sources are written by onto answer; ingested text is a note, file, url or "
                         "transcript")
    if kind not in INGEST_KINDS:
        raise UsageError("kind must be one of %s" % ", ".join(INGEST_KINDS))
    title = util.normalize_ws(str(title or ""))
    if not title:
        raise UsageError("ingest needs a neutral title (the file name is never stored)")
    if len(title) > TITLE_MAX - 12:
        raise UsageError("the title is longer than %d characters" % (TITLE_MAX - 12))
    _utf8(title, "title")
    url = str(url).strip() if url not in (None, "") else None
    if url is not None and (len(url) > URL_MAX or any(ch.isspace() for ch in url)):
        raise UsageError("url must be one address of at most %d characters" % URL_MAX)
    if url is not None:
        _utf8(url, "url")
    via_id, resolved = _via(repo, via)
    return {"kind": kind, "title": title, "url": url, "fetched_at": _timestamp(fetched_at, "fetched_at"),
            "via": via_id, "stale_after_days": _days(stale_after_days), "resolved": resolved}


def _hidden(rel: str) -> bool:
    return any(part.startswith(".") for part in rel.replace(os.sep, "/").split("/"))


def sweep(folder: str) -> List[Tuple[str, str]]:
    """``(path, relative path)`` for every non-hidden file under ``folder`` (hidden folders are skipped, linked
    folders are not followed), sorted by relative path."""
    found: List[Tuple[str, str]] = []
    for dirpath, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(names):
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, folder).replace(os.sep, "/")
            if not _hidden(rel):
                found.append((full, rel))
    found.sort(key=lambda pair: pair[1])
    return found


def _converter(path: str) -> Optional[str]:
    """How the kit turned ``path`` into text, when it did more than decode it."""
    media = formats.media_type_of(path) or ""
    reader = formats.reader_of(media)
    return None if reader in (None, "plain") else "onto:%s" % reader


def _original_block(repo: store.Repo, original: Any, keep: bool, path: Optional[str],
                    allow_any: bool) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """``(block, note)``: the ``original`` argument of ``sources.add`` (the file the text was converted from, hashed
    by the kit, or with ``keep`` and no original, the ingested file itself), and a note when ``keep`` was asked for
    but the file is over ``policy.keep_original_max_bytes`` (it is hashed, not kept)."""
    if original not in (None, ""):
        real = store.guard_input_path(str(original), repo, allow_any)
        if not os.path.isfile(real):
            raise UsageError("original must be a file: %s" % original)
        same = path is not None and os.path.realpath(path) == real
        block = {"path": real, "keep": bool(keep), "converter": _converter(real) if same else "agent",
                 "media_type": formats.media_type_of(real)}
    elif keep:
        if path is None:
            raise UsageError("keep_original needs a file: pass path, or original with the text")
        block = {"path": path, "keep": True, "converter": _converter(path), "media_type": formats.media_type_of(path)}
    else:
        return None, None
    note = None
    if block["keep"]:
        size, limit = os.path.getsize(block["path"]), int(repo.policy.get("keep_original_max_bytes") or 0)
        if size > limit:
            block["keep"] = False
            note = "original not kept: %d bytes is over keep_original_max_bytes (%d); it is hashed only" % (
                size, limit)
    return block, note


def _kept_original_problems(block: Optional[Dict[str, Any]], policy: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """``(problems, kinds)`` for an original that will be stored byte for byte: secret patterns in its bytes; for a
    text format, anything ``sanitize.check_text`` finds (the stored text is sanitized, the original is not); and for
    a binary format, which nothing here can read for personal data, any personal kind the policy does not keep.
    Every problem refuses the input."""
    if not block or not block.get("keep"):
        return [], []
    with open(block["path"], "rb") as fh:
        data = fh.read()
    secret_kinds = sorted({kind for kind, _match in secrets.scan_bytes(data)})
    if secret_kinds:
        return (["the original file holds credentials (%s); nothing was stored" % ", ".join(secret_kinds)],
                secret_kinds)
    text: Optional[str] = None
    if formats.media_type_of(block["path"]) is not None and not formats.is_binary(data):
        try:
            text = formats.decode(data)
        except OntoError:
            text = data.decode("utf-8", "replace")
    if text is None:
        return _binary_original_problems(block["path"], policy), []
    found = sanitize.check_text(text, policy)
    if not found:
        return [], []
    return (["the original file holds %s, which keep_original would store unredacted; nothing was stored. Ingest "
             "without keeping the original, or remove it" % _kinds_text(found)], found)


def _binary_original_problems(path: str, policy: Dict[str, Any]) -> List[str]:
    """A binary original may hold personal data that only a converter can see (a name in a scan, an address in a
    photo's metadata); keeping it is refused unless the policy keeps every personal kind."""
    actions = sanitize.personal_policy(policy)
    guarded = sorted(kind for kind in sanitize.PERSONAL_KINDS if actions.get(kind) != "keep")
    if not guarded:
        return []
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return ["the original file is binary%s, so the kit cannot check it for personal data, and policy.personal "
            "redacts or refuses %s; keep_original would store it unredacted, so nothing was stored. Ingest the text "
            "without keeping the original (it is still hashed), or set those kinds to keep in policy.personal" % (
                " (%s)" % ext if re.fullmatch(r"[a-z0-9]{1,10}", ext) else "", ", ".join(guarded))]


def _kinds_text(kinds: List[str]) -> str:
    """``credentials (a, b) and personal data (c)``: kind names only, never the matched text."""
    creds = [k for k in kinds if k not in sanitize.PERSONAL_KINDS]
    people = [k for k in kinds if k in sanitize.PERSONAL_KINDS]
    parts = []
    if creds:
        parts.append("credentials (%s)" % ", ".join(creds))
    if people:
        parts.append("personal data (%s)" % ", ".join(people))
    return " and ".join(parts)


def _resolve_input(path: str, repo: store.Repo, cwd: Optional[str], mcp: bool) -> str:
    """An input path as given: absolute (or ``~``) paths stay; a relative one is tried against the repo root and
    the working directory, the repo first over MCP and the working directory first on the command line."""
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        return expanded
    bases = [repo.root, cwd or os.getcwd()]
    if not mcp:
        bases.reverse()
    for base in bases:
        candidate = os.path.join(base, expanded)
        if os.path.lexists(candidate):
            return candidate
    return os.path.join(bases[0], expanded)


# storing -------------------------------------------------------------------------------------------------------
def _check_items(repo: store.Repo, items: List[_Item], url: Optional[str] = None) -> None:
    """Check the url, and sanitize every item, title and kept original, before anything is written; one refusal
    refuses the whole call."""
    policy = repo.policy
    problems: List[str] = []
    kinds: List[str] = []
    refused = False  # a kept original that cannot be stored: a refusal even when no kind was found
    if url:
        found = sanitize.check_text(url, policy)
        if found:
            problems.append("the url holds %s; a redacted address would not work, so nothing was stored. Pass the "
                            "url without them" % _kinds_text(found))
            kinds.extend(found)
    for item in items:
        where = "%s: " % item.label if item.label else ""
        if not item.text.strip():
            problems.append("%sno text to store" % where)
            continue
        try:
            _utf8(item.text, "text")
        except UsageError as exc:
            problems.append(where + exc.message)
            continue
        for what in (sources.normalize_text(item.text), item.title):
            try:
                sanitize.sanitize_text(what, policy)
            except Refused as exc:
                problems.append(where + exc.message)
                kinds.extend(exc.extra.get("kinds") or [])
                break
        else:
            found_problems, found = _kept_original_problems(item.original, policy)
            problems.extend(where + p for p in found_problems)
            kinds.extend(found)
            refused = refused or bool(found_problems)
    if problems:
        found = sorted(set(kinds))
        if len(items) == 1 and not items[0].label:
            if not found and not refused:
                raise UsageError("; ".join(problems))
            raise Refused("; ".join(problems), problems=problems, kinds=found)
        raise Refused("nothing was stored: %s" % "; ".join(problems), problems=problems, kinds=found)


def _cited_by(onto: Ontology, src_id: Optional[str]) -> List[Dict[str, str]]:
    if not src_id:
        return []
    seen = {}
    for rid, loc in onto.prov_index.get(src_id, []):
        seen.setdefault(rid, loc)
    return [{"id": rid, "loc": seen[rid]} for rid in sorted(seen)]


def _public(entry: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(entry)
    out.update(render.flags(entry))
    return out


def _chunks(repo: store.Repo, src_id: str) -> List[Dict[str, Any]]:
    return [{"n": c["n"], "loc": c["loc"], "chars": len(c["text"])}
            for c in sources.chunks_of(sources.read(repo, src_id))]


def plan_sources(repo: store.Repo, adds: Sequence[Dict[str, Any]]
                 ) -> Tuple[List[Tuple[Dict[str, Any], bool]], List[Tuple[str, bytes]]]:
    """``sources.add`` for each of ``adds`` (its keyword arguments, in order) without touching the topic: the adds run
    on a copy of the source index under ``STAGE_REL``, which is removed afterwards. Returns ``(added, planned)``:
    ``(entry, duplicate)`` per add, and the ``(rel, bytes)`` writes that store the new ones (each new text file and
    kept original, then the index; empty when every add is a duplicate) for ``write_planned``. A refusal raises
    before anything in the topic is written. Call it under the write lock."""
    stage = repo.path(STAGE_REL)
    _clear_stage(stage)
    try:
        os.makedirs(stage, mode=0o700)
        index = _snapshot(repo.path(sources.INDEX))
        staged_index = os.path.join(stage, *sources.INDEX.split("/"))
        if index is not None:
            store.write_bytes(staged_index, index)
        staged = store.Repo(stage, repo.manifest)
        added = [sources.add(staged, **kwargs) for kwargs in adds]
        planned: List[Tuple[str, bytes]] = []
        folder = os.path.dirname(staged_index)
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            rel = "sources/%s" % name
            if rel != sources.INDEX and not name.startswith("."):
                planned.append((rel, store.read_bytes(os.path.join(folder, name))))
        new_index = _snapshot(staged_index)
        if new_index is not None and new_index != index:
            planned.append((sources.INDEX, new_index))
        return added, planned
    finally:
        _clear_stage(stage, quiet=True)


def _clear_stage(path: str, quiet: bool = False) -> None:
    """Remove the stage of a planning run (or the one a process killed while planning left behind; the next ingest
    removes it before it plans). ``quiet`` ignores a failure, so it never hides the error that ended the run."""
    try:
        if os.path.islink(path) or os.path.isfile(path):
            os.unlink(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
    except OSError:
        if not quiet:
            raise


def write_planned(repo: store.Repo, planned: Sequence[Tuple[str, bytes]], appends: Sequence[str],
                  finish: Callable[[], T]) -> T:
    """Write ``planned`` (from ``plan_sources``) and run ``finish``, which appends to the ``appends`` logs (the change
    line, the history point), as one all-or-nothing write: the write intent (``store.begin_write``) is recorded
    before the first byte lands, so a process killed half way is rolled back by the next writer, and an exception
    puts every file back here. Returns what ``finish`` returns. Call it under the write lock."""
    keep = _unique([rel for rel, _data in planned] + list(appends))
    saved = {rel: _snapshot(repo.path(rel)) for rel in keep}
    rewrites = [(rel, saved[rel], data) for rel, data in planned if saved[rel] != data]
    store.begin_write(repo, rewrites, [(rel, saved[rel]) for rel in _unique(appends)])
    try:
        for rel, data in planned:
            if saved[rel] != data:
                store.write_bytes(repo.path(rel), data)
        out = finish()
    except BaseException:
        for rel in keep:
            _restore(repo.path(rel), saved[rel])
        store.end_write(repo)
        store.clear_cache()
        clear_graph_cache()
        raise
    store.end_write(repo)
    return out


def _unique(values: Sequence[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def _shown_label(repo: store.Repo, label: str) -> str:
    """A swept file's path as the result shows it (it is never stored): redacted like a title, or withheld when the
    policy refuses what it holds."""
    try:
        return sanitize.sanitize_text(label, repo.policy)[0]
    except Refused:
        return "(file name withheld)"


def _store(repo: store.Repo, items: List[_Item], meta: Dict[str, Any], by: str) -> Dict[str, Any]:
    """Check every item, then, under the write lock, plan them all (``plan_sources``) and write the new sources, one
    ``ingest`` change and a point as one all-or-nothing write (``write_planned``)."""
    _check_items(repo, items, meta.get("url"))
    with store.write_lock(repo):
        repo.reload()
        mutate.check_format(repo)
        before = store.data_hash(repo)
        added, planned = plan_sources(repo, [
            dict(text=item.text, kind=meta["kind"], title=item.title, url=meta["url"],
                 fetched_at=meta["fetched_at"], original=item.original, via=meta["via"],
                 stale_after_days=meta["stale_after_days"], sanitizer=sanitize.sanitize_text) for item in items])
        stored = [(item, entry, duplicate) for item, (entry, duplicate) in zip(items, added)]
        new_ids = [entry["id"] for _item, entry, duplicate in stored if not duplicate]
        change = None
        if new_ids:
            redacted = sum(sum((e.get("redactions") or {}).values()) for _i, e, d in stored if not d)
            summary = "ingested %s (%s%s)" % (
                new_ids[0] if len(new_ids) == 1 else "%d sources" % len(new_ids), meta["kind"],
                ", %d redaction%s" % (redacted, "" if redacted == 1 else "s") if redacted else "")

            def finish() -> Dict[str, Any]:
                logged = ledger.append_change(repo, "ingest", by, new_ids, summary,
                                              source=new_ids[0] if len(new_ids) == 1 else None, before=before,
                                              after=store.data_hash(repo))
                clear_graph_cache()
                history.append_point(repo, mutate.history_point(Ontology.load(repo), "ingest"))
                return logged

            change = write_planned(repo, planned, (ledger.CHANGES, history.HISTORY), finish)
    clear_graph_cache()
    onto = Ontology.load(repo)
    results = []
    for item, entry, duplicate in stored:
        old = entry.get("supersedes") if not duplicate else None
        result = {
            "source": _public(entry),
            "duplicate": duplicate,
            "supersedes": entry.get("supersedes"),
            "redactions": dict(entry.get("redactions") or {}),
            "chunks": _chunks(repo, entry["id"]),
            "cited_by_old": _cited_by(onto, old),
            "locator": item.locator,
        }
        if item.label is not None:
            result["file"] = _shown_label(repo, item.label)
        notes = []
        if duplicate and entry.get("erased"):
            notes.append("this text was erased from the topic; nothing was stored")
        if item.note and not duplicate:
            notes.append(item.note)
        if notes:
            result["note"] = "; ".join(notes)
        results.append(result)
    return _combine(results, change, meta)


def _combine(results: List[Dict[str, Any]], change: Optional[Dict[str, Any]], meta: Dict[str, Any]) -> Dict[str, Any]:
    """One input gives its own result (``source``, ``duplicate``, ``supersedes``, ``redactions``, ``chunks``,
    ``cited_by_old``, ``locator``, and ``file`` for a swept file). A folder gives ``sources`` (one such result per
    file) and ``ids`` (every source id in file order, short enough to survive a size cut) with the totals on top and
    ``source`` null."""
    if len(results) == 1:
        out = dict(results[0])
    else:
        redactions: Dict[str, int] = {}
        cited: Dict[str, str] = {}
        for r in results:
            for kind, n in r["redactions"].items():
                redactions[kind] = redactions.get(kind, 0) + int(n)
            for c in r["cited_by_old"]:
                cited.setdefault(c["id"], c["loc"])
        out = {
            "source": None,
            "duplicate": all(r["duplicate"] for r in results),
            "supersedes": None,
            "redactions": dict(sorted(redactions.items())),
            "chunks": [],
            "cited_by_old": [{"id": rid, "loc": cited[rid]} for rid in sorted(cited)],
            "sources": results,
            "ids": [r["source"]["id"] for r in results],
        }
    out["change"] = change["id"] if change else None
    if meta.get("resolved"):
        out["resolved"] = meta["resolved"]
    return out


def items_of(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The per-source results of an ingest result (one for a single input)."""
    if result.get("sources"):
        return list(result["sources"])
    return [result] if result.get("source") else []


def _snapshot(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _restore(path: str, data: Optional[bytes]) -> None:
    if data is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    store.write_bytes(path, data)


# the public functions ------------------------------------------------------------------------------------------
def ingest_text(repo: store.Repo, text: str, *, title: str, kind: str = "note", url: Optional[str] = None,
                fetched_at: Optional[str] = None, original: Optional[str] = None, keep_original: bool = False,
                via: Optional[str] = None, stale_after_days: Optional[int] = None, allow_any: bool = False,
                by: str = "agent") -> Dict[str, Any]:
    """Store ``text`` (already text: the agent converts other formats) as one source. See the module docstring."""
    if not isinstance(text, str) or not text.strip():
        raise UsageError("ingest needs non-empty text or a path")
    meta = _meta(repo, kind, title, url, fetched_at, via, stale_after_days)
    orig, note = _original_block(repo, original, keep_original, None, allow_any)
    return _store(repo, [_Item(text, meta["title"], orig, None, formats.LINES, note)], meta, by)


def ingest_path(repo: store.Repo, path: str, *, title: str, kind: str = "note", url: Optional[str] = None,
                fetched_at: Optional[str] = None, original: Optional[str] = None, keep_original: bool = False,
                via: Optional[str] = None, stale_after_days: Optional[int] = None, allow_any: bool = False,
                by: str = "agent") -> Dict[str, Any]:
    """Store a file, or every non-hidden file of a folder (sorted by relative path; titles ``<title> (N of M)``),
    as sources. The folder is all or nothing. See the module docstring."""
    real = store.guard_input_path(path, repo, allow_any)
    meta = _meta(repo, kind, title, url, fetched_at, via, stale_after_days)
    if not os.path.isdir(real):
        text, locator = formats.to_text(real)
        orig, note = _original_block(repo, original, keep_original, real, allow_any)
        return _store(repo, [_Item(text, meta["title"], orig, None, locator, note)], meta, by)
    if original not in (None, ""):
        raise UsageError("original goes with one file, not a folder")
    if meta["url"]:
        raise UsageError("url goes with one source, not a folder")
    files = sweep(real)
    if not files:
        raise UsageError("%s holds no files to ingest (hidden files are skipped)" % path)
    items: List[_Item] = []
    problems: List[str] = []
    for n, (full, rel) in enumerate(files, start=1):
        try:
            checked = store.guard_input_path(full, repo, allow_any)
            text, locator = formats.to_text(checked)
        except OntoError as exc:
            problems.append("%s: %s" % (rel, exc.message))
            continue
        orig, note = _original_block(repo, None, True, checked, allow_any) if keep_original else (None, None)
        items.append(_Item(text, "%s (%d of %d)" % (meta["title"], n, len(files)), orig, rel, locator, note))
    if problems:
        raise Refused("nothing was stored: %s" % "; ".join(problems), problems=problems)
    return _store(repo, items, meta, by)


def _covers(scope: Any, target: str) -> bool:
    """A decision scope covers ``target`` when it equals it or is a prefix of it that ends at, or is followed by,
    one of ``/ : . # @``."""
    s = re.sub(r"^\./", "", str(scope or "").strip().lower())
    t = target.lower()
    return bool(s) and t.startswith(s) and ledger.scope_overlaps(s, t)


def _own_id(repo: store.Repo, target: str) -> str:
    """The bare local id for ``<own ns>/<id>`` or ``self/<id>``; any other id as given."""
    for prefix in ("%s/" % repo.ns, "self/"):
        if target.startswith(prefix) and len(target) > len(prefix):
            return target[len(prefix):]
    return target


def _scope_covers(repo: store.Repo, scope: List[Any], target: str) -> bool:
    forms = [target, "%s/%s" % (repo.ns, target), "self/%s" % target]
    return any(_covers(s, f) for s in scope for f in forms)


def _merged_into(onto: Ontology, target: str, erased: bool = False) -> List[str]:
    """Local nodes merged into ``target``, directly or through a chain of merges, that are not erased yet (every
    one of them with ``erased``): the archived nodes whose ``archived.superseded_by`` names it (or names one of
    them)."""
    by_target: Dict[str, List[str]] = {}
    for nid in sorted(onto.nodes):
        rec = onto.nodes[nid]
        if not onto.is_local(nid) or rec.get("status") != "archived":
            continue
        for ref in (rec.get("archived") or {}).get("superseded_by") or []:
            by_target.setdefault(str(ref), []).append(nid)
    found: List[str] = []
    todo = [target]
    while todo:
        for nid in by_target.get(todo.pop(), []):
            if nid != target and nid not in found:
                found.append(nid)
                todo.append(nid)
    return sorted(n for n in found if erased or not onto.nodes[n].get("erased"))


def _cited_sources(onto: Ontology, nodes: List[str]) -> Dict[str, List[str]]:
    """The sources that ``nodes`` and their local edges cite, and are in the index and not erased yet, each with
    the records among them that cite it (sorted). An erase scrubs those records' quotes, but a source keeps its
    full text, so it still holds what the quotes held."""
    wanted = set(nodes)
    records = list(nodes) + [eid for eid in onto.local_edge_ids
                             if (onto.edges.get(eid) or {}).get("src") in wanted
                             or (onto.edges.get(eid) or {}).get("dst") in wanted]
    found: Dict[str, List[str]] = {}
    for rid in records:
        rec = onto.nodes.get(rid) if rid in wanted else onto.edges.get(rid)
        for p in (rec or {}).get("prov") or []:
            sid = p.get("src") if isinstance(p, dict) else None
            if not isinstance(sid, str) or sid not in onto.sources or onto.sources[sid].get("erased"):
                continue
            if rid not in found.setdefault(sid, []):
                found[sid].append(rid)
    return {sid: sorted(found[sid]) for sid in sorted(found)}


def _erase_call(id: str) -> str:
    """The CLI call that erases ``id`` once a decision whose scope names it is recorded (``erase`` is CLI only)."""
    return "onto erase %s --decision <dec>" % render.quote(id)


def erase(repo: store.Repo, id: str, decision: str, by: str = "user") -> Dict[str, Any]:
    """Erase a local node's or a source's content under ``decision`` (G.6). Returns ``{erased, kind, decision,
    quotes_scrubbed, edges_archived, merged_erased, sources_erased, still_holding, change, changes, already, note,
    id_kept_in, id_note, names_scrubbed_in, proposals_closed}``: ``names_scrubbed_in`` lists the decisions, changes
    and proposals whose free text named an erased node or held text drafted from an erased source (now ``[erased]``
    or cleared); ``proposals_closed`` lists the open proposals that cited an erased source, closed as
    ``superseded``; ``id_kept_in`` lists the files that still spell an erased
    node's id (ids are
    permanent), ``merged_erased`` are the nodes merged into the erased node and erased with it (in their own change,
    listed first in ``changes``), ``sources_erased`` the sources the erased nodes and their edges cite that the scope
    covers (erased in the node's change, ``change``). ``still_holding`` lists what the scope does not cover: merged
    nodes (``{id, kind: "node", merged_into, call}``) and cited sources, which keep their full text (``{id, kind:
    "source", cited_by, other_citers, call}``); ``call`` is the erase to run under a decision that names it."""
    target = _own_id(repo, str(id or "").strip())
    dec_id = str(decision or "").strip()
    if not target:
        raise UsageError("erase needs the id of a node or a source")
    if not ids.DEC_RE.match(dec_id):
        raise UsageError("erase needs --decision with a decision id (dec-YYYYMMDD-...)")
    dec = ledger.load_decision(repo, dec_id)
    if dec.get("status") != "active":
        raise Refused("%s is %s; erase needs an active decision" % (dec_id, dec.get("status")))
    onto = Ontology.load(repo)
    if target in onto.sources:
        kind, rec = "source", onto.sources[target]
        op = {"n": 1, "op": "erase_source", "src": target, "decision": dec_id}
    elif onto.is_local(target):
        kind, rec = "node", onto.nodes[target]
        op = {"n": 1, "op": "erase_node", "id": target, "decision": dec_id}
    elif ids.is_qualified(target) or (target in onto.nodes and onto.is_imported(target)):
        ns = onto.ns_of(target)
        raise Refused("imported nodes are read-only; erase it in %s" % onto.import_label(ns))
    elif target in onto.edges:
        raise UsageError("erase takes a node or a source id; archive an edge through a proposal")
    else:
        res = onto.resolve(target)
        near = [res["id"]] if res.get("id") else list(res.get("candidates") or [])
        raise NotFound("%s: not in the ontology; erase needs the exact id of a local node or a source" % target,
                       near, searched=target)
    scope = list(dec.get("scope") or [])
    if not _scope_covers(repo, scope, target):
        raise Refused("the scope of %s (%s) does not cover %s; record a decision whose scope names it" % (
            dec_id, ", ".join(str(s) for s in scope) or "empty", target))
    if target == mutate.root_id(repo):
        raise Refused(mutate.ROOT_ERASE % target)
    merged = _merged_into(onto, target) if kind == "node" else []
    covered = [m for m in merged if _scope_covers(repo, scope, m)]
    still = [{"id": m, "kind": "node", "merged_into": target, "call": _erase_call(m)}
             for m in merged if m not in covered]
    # The nodes this erase leaves erased (the target and the merged nodes erased now or before) and the sources
    # they and their edges cite: the scope's own go with the node, the others are listed with their call.
    gone: List[str] = []
    if kind == "node":
        gone = [target] + [m for m in _merged_into(onto, target, erased=True)
                           if m in covered or onto.nodes[m].get("erased")]
    cited = _cited_sources(onto, gone)
    src_covered = [sid for sid in cited if _scope_covers(repo, scope, sid)]
    for sid in cited:
        if sid in src_covered:
            continue
        others = {rid for rid, _loc in onto.prov_index.get(sid, [])} - set(gone) - set(cited[sid])
        others = {rid for rid in others if not (onto.nodes.get(rid) or {}).get("erased")}
        still.append({"id": sid, "kind": "source", "cited_by": cited[sid], "other_citers": len(others),
                      "call": _erase_call(sid)})
    base = {"erased": target, "kind": kind, "decision": dec_id, "note": ERASE_NOTE, "still_holding": still,
            "already": bool(rec.get("erased"))}
    # Two all-or-nothing writes: the merged nodes first (each still points at the active node it was merged into,
    # so the graph check passes), then the node itself with the sources it cites that the scope covers. A failure
    # in between leaves only the node (and those sources) to erase again.
    steps: List[Tuple[List[Dict[str, Any]], str]] = []
    if covered:
        steps.append(([{"n": n, "op": "erase_node", "id": m, "decision": dec_id} for n, m in enumerate(covered, 1)],
                      "erased %s, merged into %s, under %s" % (", ".join(covered), target, dec_id)))
    last: List[Dict[str, Any]] = [] if rec.get("erased") else [op]
    last.extend({"n": n, "op": "erase_source", "src": sid, "decision": dec_id}
                for n, sid in enumerate(src_covered, len(last) + 1))
    if last:
        summary = "erased %s under %s" % (target, dec_id)
        if src_covered:
            summary = "erased %s%s, the source%s citing %s, under %s" % (
                "" if rec.get("erased") else target + " and ", ", ".join(src_covered),
                "" if len(src_covered) == 1 else "s", target, dec_id)
        steps.append((last, summary))
    if not steps:
        return dict(base, quotes_scrubbed=0, edges_archived=[], merged_erased=[], sources_erased=[], change=None,
                    changes=[], id_kept_in=[], id_note="", names_scrubbed_in=[], proposals_closed=[])
    scrubbed, edges, changes = 0, [], []  # type: Tuple[int, List[str], List[str]]
    kept_in, id_note = [], ""  # type: Tuple[List[str], str]
    named_in, closed = [], []  # type: Tuple[List[str], List[str]]
    follow_ups = []  # type: List[str]
    for ops, summary in steps:
        out = mutate.apply_ops(repo, ops, by=by, change_type="erase", summary=summary)
        changes.append(out["change"])
        follow_ups.extend(f for f in out.get("follow_ups") or [] if f not in follow_ups)
        for each in ops:
            result = out["results"].get(str(each["n"])) or {}
            scrubbed += int(result.get("quotes_scrubbed") or 0)
            edges.extend(e for e in result.get("edges_archived") or [] if e not in edges)
            kept_in.extend(f for f in result.get("id_kept_in") or [] if f not in kept_in)
            named_in.extend(r for r in result.get("names_scrubbed_in") or [] if r not in named_in)
            closed.extend(r for r in result.get("proposals_closed") or [] if r not in closed)
            if each.get("id") == target and result.get("id_note"):
                id_note = str(result["id_note"])
    done = dict(base, quotes_scrubbed=scrubbed, edges_archived=edges, merged_erased=covered,
                sources_erased=src_covered, change=changes[-1], changes=changes, id_kept_in=kept_in, id_note=id_note,
                names_scrubbed_in=named_in, proposals_closed=closed)
    if follow_ups:
        done["calibration_follow_ups"] = follow_ups
    return done


def scrub(repo: store.Repo, text: str, decision: str, by: str = "user") -> Dict[str, Any]:
    """Take ``text`` out of the free text that holds it under ``decision`` (G.6, ``onto erase --scrub``), keeping
    the records: it becomes ``[erased]`` in the summaries, attrs, gap notes and edge notes of the local records, the
    provenance quotes holding it are dropped, and it leaves the proposals, the decisions and the change log, in one
    all-or-nothing write. The scope must cover every record it changes (an edge: its id or one of its ends). Names
    stay (erase that node to forget a name) and so do source texts (erase that source). Returns ``{scrubbed,
    decision, records, texts_rewritten, quotes_scrubbed, names_scrubbed_in, change, still}``: ``still`` is what
    the finder still lists afterwards."""
    text = util.normalize_ws(str(text or ""))
    dec_id = str(decision or "").strip()
    if len(text) < mutate.FIND_MIN:
        raise UsageError("give at least %d characters to scrub" % mutate.FIND_MIN)
    if not ids.DEC_RE.match(dec_id):
        raise UsageError("a scrub needs --decision with a decision id (dec-YYYYMMDD-...)")
    dec = ledger.load_decision(repo, dec_id)
    if dec.get("status") != "active":
        raise Refused("%s is %s; a scrub needs an active decision" % (dec_id, dec.get("status")))
    onto = Ontology.load(repo)
    targets = mutate.scrub_targets(onto, text)
    scope = list(dec.get("scope") or [])
    uncovered = []
    for rid in targets:
        ends = [rid]
        if rid in onto.edges:
            ends += [str(onto.edges[rid].get("src") or ""), str(onto.edges[rid].get("dst") or "")]
        if not any(_scope_covers(repo, scope, e) for e in ends if e):
            uncovered.append(rid)
    if uncovered:
        raise Refused("the scope of %s (%s) does not cover %s; record a decision whose scope names %s" % (
            dec_id, ", ".join(str(sc) for sc in scope) or "empty", ", ".join(uncovered[:8]),
            "it" if len(uncovered) == 1 else "them"))
    op = {"n": 1, "op": "scrub_text", "text": text, "decision": dec_id, "ids": targets}
    out = mutate.apply_ops(repo, [op], by=by, change_type="erase",
                           summary="scrubbed a text from %d record%s under %s" % (
                               len(targets), "" if len(targets) == 1 else "s", dec_id))
    result = out["results"].get("1") or {}
    clear_graph_cache()
    still = mutate.find_text(repo, text)
    return {"scrubbed": True, "decision": dec_id, "records": result.get("text_scrubbed_in") or [],
            "texts_rewritten": int(result.get("texts_rewritten") or 0),
            "quotes_scrubbed": int(result.get("quotes_scrubbed") or 0),
            "names_scrubbed_in": result.get("names_scrubbed_in") or [], "change": out["change"], "still": still}


def render_scrub(result: Dict[str, Any], mode: str) -> List[str]:
    records = result.get("records") or []
    lines = ["scrubbed the text under %s: %d field%s rewritten to %s, %d quote%s dropped, in %d record%s%s; change %s"
             % (result["decision"], result["texts_rewritten"], "" if result["texts_rewritten"] == 1 else "s",
                mutate.ERASED_NAME, result["quotes_scrubbed"], "" if result["quotes_scrubbed"] == 1 else "s",
                len(records), "" if len(records) == 1 else "s",
                " (%s)" % ", ".join(records[:8]) if records else "", result["change"])]
    named = result.get("names_scrubbed_in") or []
    if named:
        more = render.more(len(named), min(len(named), 8))
        lines.append("also taken out of: %s%s" % (", ".join(named[:8]), " " + more if more else ""))
    still = result.get("still") or {}
    for hit in still.get("hits") or []:
        place = "%s%s" % (hit["file"], ":%d" % hit["line"] if hit.get("line") else "")
        lines.append("still holding it: %s (%s%s)" % (place, hit.get("what"), ", %s" % hit["id"] if hit.get("id")
                                                      else ""))
    if still.get("erase"):
        lines.append("  a name or a source text holds it: record a decision naming %s and onto erase each"
                     % ", ".join(still["erase"][:6]))
    lines.append("The records keep their names and ids. Build outputs and git history keep the old text until you "
                 "rebuild and rewrite history.")
    return lines


# commands ------------------------------------------------------------------------------------------------------
def cmd_ingest(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    repo = ctx.repo
    path, text = args.get("path"), args.get("text")
    if path not in (None, "") and text not in (None, ""):
        raise UsageError("give path or text, not both")
    allow_any = bool(args.get("allow_any_path")) and not ctx.mcp  # a CLI-only switch, never over MCP
    by = "agent" if ctx.mcp else "user"
    common = dict(title=args.get("title"), kind=args.get("kind") or "note", url=args.get("url"),
                  fetched_at=args.get("fetched_at"), keep_original=bool(args.get("keep_original")),
                  via=args.get("via"), stale_after_days=args.get("stale_after_days"), allow_any=allow_any, by=by)
    original = args.get("original")
    if original not in (None, ""):
        original = _resolve_input(str(original), repo, ctx.cwd, ctx.mcp)
    if path not in (None, ""):
        result = ingest_path(repo, _resolve_input(str(path), repo, ctx.cwd, ctx.mcp), original=original, **common)
    else:
        if text in (None, "") or not str(text).strip():
            raise UsageError("ingest needs path or text (on the command line, '-' reads stdin)")
        result = ingest_text(repo, str(text), original=original, **common)
    result["next"] = _next_calls(ctx, result)
    if result.get("sources"):
        result["list_call"] = _list_call(ctx, result["sources"])
    return result


def _list_call(ctx: Any, items: List[Dict[str, Any]]) -> str:
    """The read call that lists every source of a folder ingest (each is titled ``<title> (N of M)``), for the
    ``[page]`` line and for a JSON result whose lists were cut to fit."""
    base = _OF_N.sub("", str(items[0]["source"].get("title") or ""))
    return ctx.call("search", text=base, kinds=["source"], limit=0)


def _next_calls(ctx: Any, result: Dict[str, Any]) -> List[str]:
    calls = []
    for r in items_of(result)[:5]:
        if r["chunks"] and not r["source"].get("erased"):
            calls.append(ctx.call("get", id=r["source"]["id"], chunk=1))
    return calls


def cmd_erase(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    return erase(ctx.repo, args.get("id"), args.get("decision"), by="agent" if ctx.mcp else "user")


# renderers -----------------------------------------------------------------------------------------------------
def _source_line(r: Dict[str, Any]) -> str:
    src = r["source"]
    head = render.mark(src, "%s %s" % (src["id"], src.get("kind")))
    facts = "%d lines, %d bytes, %d chunk%s" % (src.get("lines") or 0, src.get("bytes") or 0, len(r["chunks"]),
                                                "" if len(r["chunks"]) == 1 else "s")
    if r["locator"] == formats.TIMES:
        facts += ", cite T<hh:mm:ss>"
    state = "already stored, nothing written" if r["duplicate"] else "stored"
    if r["redactions"]:
        facts += "; redacted " + ", ".join("%s %d" % kv for kv in sorted(r["redactions"].items()))
    line = "%s %s: %s (%s)" % (head, json.dumps(render.trunc(src.get("title") or "", 80), ensure_ascii=False),
                                facts, state)
    if r.get("note"):
        line += "; " + r["note"]
    return line


def _supersedes_line(r: Dict[str, Any]) -> Optional[str]:
    if not r["supersedes"] or r["duplicate"]:
        return None
    cited = r["cited_by_old"]
    text = "  supersedes %s" % r["supersedes"]
    if cited:
        shown = ", ".join(c["id"] for c in cited[:8])
        more = render.more(len(cited), min(len(cited), 8))
        text += "; cited by %d record(s): %s%s; check their quotes against the new text" % (
            len(cited), shown, " " + more if more else "")
    return text


def _folder_line(r: Dict[str, Any]) -> str:
    """One short line for a swept file: its source id, the file and how long it is (the chunk count when there is
    more than one; the title, ``<title> (N of M)``, follows from the order)."""
    src = r["source"]
    n, chunks = int(src.get("lines") or 0), len(r["chunks"])
    facts = "%d line%s" % (n, "" if n == 1 else "s") + (", %d chunks" % chunks if chunks > 1 else "")
    if r["locator"] == formats.TIMES:
        facts += ", cite T<hh:mm:ss>"
    if r["redactions"]:
        facts += "; redacted " + ", ".join("%s %d" % kv for kv in sorted(r["redactions"].items()))
    if r["duplicate"]:
        facts += "; already stored, nothing written"
    if r.get("note"):
        facts += "; " + r["note"]
    where = render.trunc(r.get("file") or src.get("title") or "", 80)
    return "%s %s: %s" % (render.mark(src, src["id"]), json.dumps(where, ensure_ascii=False), facts)


def _folder_lines(result: Dict[str, Any], items: List[Dict[str, Any]], ctx: Any) -> List[str]:
    """The head (the totals and the change id), then one line per file in file order. Over MCP the lines stop at
    ``FOLDER_CHARS``, under the server's size cap, and a ``[page]`` line names the call that lists every source of
    the ingest, so no id is lost to the cut (the JSON result carries them all in ``ids``)."""
    new = sum(1 for r in items if not r["duplicate"])
    head = "ingested %d file(s): %d stored, %d already stored" % (len(items), new, len(items) - new)
    if result.get("change"):
        head += "; change %s" % result["change"]
    lines = [head]
    budget = FOLDER_CHARS if getattr(ctx, "mcp", False) else 0
    used = shown = 0
    for r in items:
        block = [_folder_line(r)]
        extra = _supersedes_line(r)
        if extra:
            block.append(extra)
        size = sum(len(line) + 1 for line in block)
        if budget and shown and used + size > budget:
            break
        lines.extend(block)
        used, shown = used + size, shown + 1
    if shown < len(items):
        base = _OF_N.sub("", str(items[0]["source"].get("title") or ""))
        lines.append("[page] sources 1-%d of %d; next: %s lists every source titled %s" % (
            shown, len(items), result.get("list_call") or _list_call(ctx, items),
            json.dumps("%s (n of %d)" % (base, len(items)), ensure_ascii=False)))
    return lines


def render_ingest(result: Dict[str, Any], mode: str, ctx: Any) -> List[str]:
    items = items_of(result)
    if len(items) > 1:
        lines = _folder_lines(result, items, ctx)
    else:
        lines = []
        for r in items:
            lines.append(_source_line(r))
            extra = _supersedes_line(r)
            if extra:
                lines.append(extra)
            if mode == "text":
                for c in r["chunks"]:
                    lines.append("  chunk %d %s (%d chars): %s" % (
                        c["n"], c["loc"], c["chars"], ctx.call("get", id=r["source"]["id"], chunk=c["n"])))
            elif len(r["chunks"]) > 1:
                locs = ", ".join(c["loc"] for c in r["chunks"][:6])
                more = render.more(len(r["chunks"]), min(len(r["chunks"]), 6))
                lines.append("  chunks: %s%s" % (locs, " " + more if more else ""))
    if result.get("resolved"):
        res = result["resolved"]
        lines.append("via: %s resolved to %s" % (json.dumps(res["query"], ensure_ascii=False), res["id"]))
    if result.get("change") and len(items) <= 1:
        lines.append("change %s" % result["change"])
    calls = result.get("next") or []
    if calls:
        lines.append("Next: read %s; search the key terms (%s) before drafting ops with quotes and locs, then "
                     "propose." % ("; ".join(calls), "onto_search" if getattr(ctx, "mcp", False) else "onto search"))
    if any(r["chunks"] and not r["duplicate"] and not r["source"].get("erased") for r in items):
        lines.append(CONTACT_CHECK)
    return lines


def render_erase(result: Dict[str, Any], mode: str, ctx: Any) -> List[str]:
    if result.get("already"):
        head = "%s (%s) was already erased" % (result["erased"], result["kind"])
        head += "; change %s" % result["change"] if result.get("change") else "; nothing written"
    else:
        head = "erased %s (%s) under %s: %d quote%s scrubbed" % (
            result["erased"], result["kind"], result["decision"], result["quotes_scrubbed"],
            "" if result["quotes_scrubbed"] == 1 else "s")
        if result["kind"] == "node":
            head += ", %d edge%s archived" % (len(result["edges_archived"]),
                                              "" if len(result["edges_archived"]) == 1 else "s")
        if result.get("change"):
            head += "; change %s" % result["change"]
    lines = [head]
    if result.get("merged_erased"):
        lines.append("also erased, merged into %s: %s (change %s)" % (
            result["erased"], ", ".join(result["merged_erased"]), (result.get("changes") or [None])[0]))
    if result.get("sources_erased"):
        lines.append("also erased, the source%s citing %s: %s" % (
            "" if len(result["sources_erased"]) == 1 else "s", result["erased"], ", ".join(result["sources_erased"])))
    for held in result.get("still_holding") or []:
        if held.get("kind") == "source":
            others = int(held.get("other_citers") or 0)
            what = "a source cited by %s; it keeps its full text" % ", ".join(held.get("cited_by") or [])
            if others:
                what += ", and erasing it also scrubs the quotes of %d other record%s citing it" % (
                    others, "" if others == 1 else "s")
        else:
            what = "merged into %s" % held["merged_into"]
        line = "still holding data: %s (%s); the scope of %s does not cover it, so record a decision that names it" % (
            held["id"], what, result["decision"])
        lines.append(line + (", then run: %s" % held["call"] if held.get("call") else " and erase it too"))
    for item in result.get("calibration_follow_ups") or []:
        lines.append("follow-up: %s; the erase went through, so fix the risk next (link another implemented or "
                     "partial control, or raise its residual)" % item)
    if mode == "text" and result.get("edges_archived"):
        lines.append("edges archived: %s" % ", ".join(result["edges_archived"]))
    named = result.get("names_scrubbed_in") or []
    if named:
        more = render.more(len(named), min(len(named), 8))
        lines.append("name scrubbed from: %s%s" % (", ".join(named[:8]), " " + more if more else ""))
    closed = result.get("proposals_closed") or []
    if closed:
        more = render.more(len(closed), min(len(closed), 8))
        lines.append("closed (drafted from the erased source): %s%s" % (", ".join(closed[:8]),
                                                                      " " + more if more else ""))
    kept = result.get("id_kept_in") or []
    if kept:
        shown = ", ".join(kept[:8])
        more = render.more(len(kept), min(len(kept), 8))
        lines.append("id still named in: %s%s" % (shown, " " + more if more else ""))
        if result.get("id_note"):
            lines.append(result["id_note"])
    lines.append(result["note"])
    return lines
