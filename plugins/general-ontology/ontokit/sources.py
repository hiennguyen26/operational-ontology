"""The source store: ``sources/index.jsonl`` (one line per source) and ``sources/src-<12hex>.txt`` (sanitized text).

A source id is ``src-`` plus the first 12 hex of the sha256 of its stored text, so the same text is stored once.
Text is sanitized before it is stored (``sanitize.sanitize_text`` when that module is built; otherwise a secret scan
that refuses on any hit), written to a temp file, scanned again, then renamed into place. The original file name is
never stored: ``title`` is neutral. When the same ``url``, or the same ``(kind, title)``, comes back with other text,
the new entry names the one it replaces in ``supersedes``.

Ingested text is data. Nothing here interprets it; readers mark it ``[untrusted]``.

``quote_found`` is the quote check (C.12): after whitespace normalization the quote must be a substring of the cited
lines (``L<a>-L<b>``), or of the whole text for ``Q:``, ``T`` and ``P`` locations.
"""

from __future__ import annotations

import contextlib
import mimetypes
import os
import re
import tempfile
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import records, secrets, store, util
from .errors import DataError, NotFound, Refused, UsageError

INDEX = "sources/index.jsonl"
KINDS = ("note", "file", "url", "transcript", "interview")
TEXT_NAME = "sources/%s.txt"
ORIG_NAME = "sources/%s.orig.%s"
CHUNK_SIZE = 1500
_LINES_RE = re.compile(r"^L([1-9][0-9]*)-L([1-9][0-9]*)\Z")
# a cue of a converted .vtt or .srt file (``formats.subtitle_text`` writes one ``T<hh:mm:ss> <text>`` line per cue)
_STAMP_RE = re.compile(r"^T[0-9]{2,}:[0-5][0-9]:[0-5][0-9]\Z")


def is_stamp(loc: Any) -> bool:
    """True for a ``T<hh:mm:ss>`` location (a cue of a converted .vtt or .srt file)."""
    return bool(_STAMP_RE.match(str(loc or "")))


def cue_lines(text: str, loc: str) -> List[str]:
    """The lines of ``text`` that are cues at the ``T<hh:mm:ss>`` location ``loc`` (several cues may share a
    second); empty for any other location or when no line starts with that stamp."""
    if not _STAMP_RE.match(loc or ""):
        return []
    return [line for line in _split(text or "") if line.startswith(loc + " ")]
_EXT_RE = re.compile(r"^[a-z0-9]{1,8}\Z")
_ID_LOC_RE = re.compile(r"^(?:[a-z][a-z0-9-]{0,31}/)?[a-z][a-z0-9-]{0,31}:")
DAMAGED = ("%s has unreadable lines (a merge conflict?); resolve them as AGENTS.md describes under \"Merging topic "
           "branches\", run onto validate, then retry. Nothing was stored: %s")

Sanitizer = Callable[[str, Dict[str, Any]], Tuple[str, Dict[str, int]]]


def _optional(name: str) -> Any:
    """A package module when it is built, else None (the lazy peer rule; ``commands.optional`` sits higher). A
    module that exists but fails to import raises."""
    return util.optional_module(name)


def text_path(repo: store.Repo, src_id: str) -> str:
    return repo.path(TEXT_NAME % src_id)


def index(repo: store.Repo) -> Dict[str, Dict[str, Any]]:
    """``{id: entry}`` for every readable index line (bad lines are skipped; ``validate`` reports them)."""
    rows, _problems = store.read_jsonl(repo.path(INDEX))
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if isinstance(row.get("id"), str) and row["id"] not in out:
            out[row["id"]] = row
    return out


def entry(repo: store.Repo, src_id: str) -> Dict[str, Any]:
    found = index(repo).get(src_id)
    if found is None:
        raise NotFound("%s is not in the sources index" % src_id, searched=src_id)
    return found


def normalize_text(text: str) -> str:
    """Stored form: ``\\r\\n`` and ``\\r`` become ``\\n``, and the text ends with one newline."""
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    return body if body.endswith("\n") else body + "\n"


def _secret_only(text: str, policy: Dict[str, Any]) -> Tuple[str, Dict[str, int]]:
    hits = secrets.scan_str(text)
    if hits:
        kinds = sorted({kind for kind, _match in hits})
        raise Refused("the text holds credentials (%s); nothing was stored" % ", ".join(kinds), kinds=kinds)
    return text, {}


def default_sanitizer() -> Sanitizer:
    module = _optional("sanitize")
    if module is not None and hasattr(module, "sanitize_text"):
        return module.sanitize_text
    return _secret_only


def _heads(rows: List[Dict[str, Any]], match: Callable[[Dict[str, Any]], bool]) -> Optional[Dict[str, Any]]:
    """The latest entry matching ``match`` that no other entry supersedes."""
    replaced = {r.get("supersedes") for r in rows if r.get("supersedes")}
    found = [r for r in rows if match(r) and r.get("id") not in replaced]
    found.sort(key=lambda r: (str(r.get("captured_at") or ""), str(r.get("id") or "")))
    return found[-1] if found else None


def _original(repo: store.Repo, original: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Tuple[str, str]]]:
    """(the index ``original`` block, (file to copy, extension) or None). With ``path`` the kit hashes the raw file
    itself; ``keep`` asks to store it when it is under ``policy.keep_original_max_bytes``."""
    if not isinstance(original, dict):
        raise UsageError("original must be an object")
    copy_from: Optional[Tuple[str, str]] = None
    if original.get("path"):
        raw_path = str(original["path"])
        with open(raw_path, "rb") as fh:
            data = fh.read()
        media = original.get("media_type") or mimetypes.guess_type(raw_path)[0] or "application/octet-stream"
        ext = os.path.splitext(raw_path)[1].lstrip(".").lower()
        keep = bool(original.get("keep")) and len(data) <= int(repo.policy.get("keep_original_max_bytes") or 0)
        if keep and not _EXT_RE.match(ext):
            raise UsageError("cannot keep an original without a short extension (%r)" % ext)
        block = {"sha256": util.sha256_hex(data), "bytes": len(data), "media_type": str(media),
                 "stored": keep, "converter": original.get("converter") or None}
        if keep:
            copy_from = (raw_path, ext)
        return block, copy_from
    block = {k: original.get(k) for k in ("sha256", "bytes", "media_type", "stored", "converter")}
    block["stored"] = False
    return block, None


def add(
    repo: store.Repo,
    text: str,
    kind: str,
    title: str,
    url: Optional[str] = None,
    fetched_at: Optional[str] = None,
    original: Optional[Dict[str, Any]] = None,
    via: Optional[str] = None,
    stale_after_days: Optional[int] = None,
    sanitizer: Optional[Sanitizer] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Store a source; returns ``(entry, duplicate)``. The same sanitized text returns the existing entry with
    ``duplicate`` True and writes nothing. Raises ``Refused`` on credentials, ``UsageError`` on bad input.
    Appending the ``ingest`` or ``answer`` change is the caller's job."""
    if not isinstance(text, str) or not text.strip():
        raise UsageError("a source needs non-empty text")
    if kind not in KINDS:
        raise UsageError("source kind must be one of %s" % ", ".join(KINDS))
    title = util.normalize_ws(title or "")
    if not title:
        raise UsageError("a source needs a neutral title")
    clean = sanitizer or default_sanitizer()
    policy = repo.policy
    body, redactions = clean(normalize_text(text), policy)
    body = normalize_text(body)
    title, _title_redactions = clean(title, policy)
    title = util.normalize_ws(title)[:200]
    data = body.encode("utf-8")
    sha = util.sha256_hex(data)
    src_id = "src-" + sha[:12]
    with store.write_lock(repo):
        rows, bad_lines = store.read_jsonl(repo.path(INDEX))
        for row in rows:
            if row.get("id") == src_id:
                if row.get("sha256") != sha:
                    raise DataError("source id %s is already taken by other text (a hash collision); nothing "
                                    "was stored" % src_id)
                return row, True
        if bad_lines:  # the index is rewritten below from the lines that parsed: never drop the others silently
            raise Refused(DAMAGED % (INDEX, "; ".join("%s:%d: P01 %s" % (INDEX, n, m) for n, m in bad_lines[:3])),
                          problems=["%s:%d: P01 %s" % (INDEX, n, m) for n, m in bad_lines])
        orig_block, copy_from = _original(repo, original) if original else (None, None)
        if url:
            head = _heads(rows, lambda r: r.get("url") == url)
        else:
            head = _heads(rows, lambda r: r.get("kind") == kind and r.get("title") == title and not r.get("url"))
        new: Dict[str, Any] = {
            "id": src_id,
            "kind": kind,
            "title": title,
            "sha256": sha,
            "bytes": len(data),
            "lines": body.count("\n"),
            "captured_at": util.now_iso(),
            "url": url or None,
            "fetched_at": fetched_at or None,
            "via": via or None,
            "stale_after_days": stale_after_days,
            "trust": "user" if kind == "interview" else "untrusted",
            "redactions": {k: int(v) for k, v in sorted((redactions or {}).items()) if v},
            "supersedes": head["id"] if head and head.get("sha256") != sha else None,
            "erased": False,
        }
        if orig_block is not None:
            new["original"] = orig_block
        errors = records.check(new, "source")
        if errors:
            raise UsageError("the source entry fails its schema: %s" % "; ".join(errors[:3]), problems=errors)
        orig_bytes = None
        if copy_from is not None:
            with open(copy_from[0], "rb") as fh:
                orig_bytes = fh.read()
            hits = secrets.scan_bytes(orig_bytes)  # a kept original is stored byte for byte: scan it first
            if hits:
                kinds = sorted({kind for kind, _match in hits})
                raise Refused("the original file holds credentials (%s); nothing was stored" % ", ".join(kinds),
                              kinds=kinds)
        _write_text(repo, src_id, data)
        if copy_from is not None:
            store.write_bytes(repo.path(ORIG_NAME % (src_id, copy_from[1])), orig_bytes or b"")
        store.write_jsonl(repo.path(INDEX), rows + [new])
    return new, False


def _write_text(repo: store.Repo, src_id: str, data: bytes) -> None:
    """Write to a temp file in ``sources/``, scan it again, then rename it into place."""
    target = text_path(repo, src_id)
    folder = os.path.dirname(target)
    os.makedirs(folder, exist_ok=True)
    mode = store.file_mode(target)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".incoming.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            if hasattr(os, "fchmod"):  # not on Windows, where mkstemp's mode is not an issue
                os.fchmod(fh.fileno(), mode)
            os.fsync(fh.fileno())
        with open(tmp, "rb") as fh:
            hits = secrets.scan_bytes(fh.read())
        if hits:
            kinds = sorted({kind for kind, _match in hits})
            raise Refused("the stored text still holds credentials (%s); nothing was stored" % ", ".join(kinds),
                          kinds=kinds)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read(repo: store.Repo, src_id: str) -> str:
    """The stored text. Raises ``NotFound`` when the file is missing."""
    try:
        with open(text_path(repo, src_id), "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except FileNotFoundError:
        raise NotFound("%s has no stored text" % src_id, searched=src_id)


def _split(text: str) -> List[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def lines(repo: store.Repo, src_id: str, a: int, b: int) -> List[str]:
    """Lines ``a`` to ``b`` (1-based, inclusive) of the stored text."""
    if a < 1 or b < a:
        raise UsageError("line range must be a-b with 1 <= a <= b, got %s-%s" % (a, b))
    return _split(read(repo, src_id))[a - 1: b]


def chunks_of(text: str, size: int = CHUNK_SIZE) -> List[Dict[str, Any]]:
    """Paragraph-aligned chunks of about ``size`` characters: ``[{n, loc, text}]`` with 1-based line spans.
    Paragraphs are separated by blank lines; one longer than ``size`` is cut at line breaks."""
    all_lines = _split(text)
    paragraphs: List[Tuple[int, int]] = []
    start: Optional[int] = None
    for i, line in enumerate(all_lines, start=1):
        if line.strip():
            if start is None:
                start = i
        elif start is not None:
            paragraphs.append((start, i - 1))
            start = None
    if start is not None:
        paragraphs.append((start, len(all_lines)))
    pieces: List[Tuple[int, int]] = []
    for a, b in paragraphs:
        cur_a, cur_len = a, 0
        for i in range(a, b + 1):
            length = len(all_lines[i - 1]) + 1
            if cur_len and cur_len + length > size:
                pieces.append((cur_a, i - 1))
                cur_a, cur_len = i, 0
            cur_len += length
        pieces.append((cur_a, b))
    out: List[Dict[str, Any]] = []
    cur: Optional[List[int]] = None
    cur_len = 0

    def span_len(a: int, b: int) -> int:
        return sum(len(all_lines[i - 1]) + 1 for i in range(a, b + 1))

    for a, b in pieces:
        length = span_len(a, b)
        if cur is not None and cur_len + length > size:
            out.append({"a": cur[0], "b": cur[1]})
            cur = None
        if cur is None:
            cur, cur_len = [a, b], length
        else:
            cur[1] = b
            cur_len = span_len(cur[0], b)
    if cur is not None:
        out.append({"a": cur[0], "b": cur[1]})
    return [
        {"n": n, "loc": "L%d-L%d" % (c["a"], c["b"]), "text": "\n".join(all_lines[c["a"] - 1: c["b"]])}
        for n, c in enumerate(out, start=1)
    ]


def chunk(repo: store.Repo, src_id: str, n: Optional[int] = None, size: int = CHUNK_SIZE) -> List[Dict[str, Any]]:
    """Every chunk of a source, or ``[chunk n]`` (1-based); ``UsageError`` when ``n`` is out of range."""
    found = chunks_of(read(repo, src_id), size)
    if n is None:
        return found
    if not isinstance(n, int) or n < 1 or n > len(found):
        raise UsageError("%s has %d chunk(s); chunk %r does not exist" % (src_id, len(found), n))
    return [found[n - 1]]


def quote_found(text: str, quote: str, loc: str) -> bool:
    """The quote check: ``normalize_ws(quote)`` is a substring of the normalized cited lines (``L<a>-L<b>``) or of the
    whole text (``Q:`` and ``P`` locations, and anything else). A ``T<hh:mm:ss>`` location checks the cue lines at
    that stamp only (``cue_lines``). An empty quote, a line span outside the text, or a stamp no cue has, fails."""
    needle = util.normalize_ws(quote or "")
    if not needle:
        return False
    m = _LINES_RE.match(loc or "")
    if _STAMP_RE.match(loc or ""):
        hay = "\n".join(cue_lines(text, loc))
        if not hay:
            return False
    elif m:
        a, b = int(m.group(1)), int(m.group(2))
        all_lines = _split(text or "")
        if b < a or b > len(all_lines):
            return False
        hay = "\n".join(all_lines[a - 1: b])
    else:
        hay = text or ""
    return needle in util.normalize_ws(hay)


def loc_problem(src: str, loc: Any, entry: Optional[Dict[str, Any]], line_count: Optional[int] = None,
                source_text: Optional[str] = None) -> Optional[str]:
    """Why a provenance location cannot be followed into its source, or None. A line span must sit inside the text
    (``1 <= a <= b <= lines``, from the index or ``line_count``); a ``Q:`` location cites an interview answer only; a
    node id is the location of an import's pseudo-source (``imp:``) only; with the stored ``source_text``, a
    ``T<hh:mm:ss>`` location must name a cue it holds (only a .vtt or .srt file ingested as a path has cues). An
    unknown or erased source is left to the other checks."""
    text = str(loc or "")
    if src.startswith("imp:"):
        return None
    if _ID_LOC_RE.match(text):
        return "location %s names a node; only an import's provenance (imp:...) cites a node" % text
    if entry is None or entry.get("erased"):
        return None
    m = _LINES_RE.match(text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        total = entry.get("lines") if isinstance(entry.get("lines"), int) else line_count
        if b < a:
            return "location %s ends before it starts" % text
        if isinstance(total, int) and not isinstance(total, bool) and b > total:
            return "location %s is past the end of %s (%d line%s)" % (text, src, total, "" if total == 1 else "s")
    elif text.startswith("Q:") and entry.get("kind") != "interview":
        return ("location %s cites a question, but %s is a %s source, not an interview answer; cite its lines "
                "(L<a>-L<b>)" % (text, src, entry.get("kind")))
    elif source_text is not None and _STAMP_RE.match(text) and not cue_lines(source_text, text):
        return ("location %s names no cue of %s (only a .vtt or .srt file ingested as a path has T lines); cite its "
                "lines (L<a>-L<b>)" % (text, src))
    return None


def superseded(rows: Any) -> set:
    """Ids of the sources a later entry replaces (``supersedes``)."""
    values = rows.values() if isinstance(rows, dict) else rows
    return {str(r.get("supersedes")) for r in values if isinstance(r, dict) and r.get("supersedes")}


def stale(entry: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """True when ``captured_at + stale_after_days`` is before ``now``."""
    days = entry.get("stale_after_days")
    if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
        return False
    try:
        captured = util.parse_ts(str(entry.get("captured_at")))
    except ValueError:
        return False
    return captured + timedelta(days=days) < (now or util.now())
