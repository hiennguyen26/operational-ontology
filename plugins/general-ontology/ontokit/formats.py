"""Text extraction for ingest: ``to_text(path_or_bytes, media_type) -> (text, locator_kind)``.

Read as text: txt and md (as written), json (re-indented one key per line, keeping key order, repeated keys and
number spellings, so line spans can point into it), jsonl (as written), csv and tsv (a ``header:`` line, then one
``row N:`` line per record with ``column=value`` cells), html (the visible text through ``html.parser``, with a
line break per block and a blank line between paragraphs; scripts and styles are dropped) and vtt or srt subtitles
(one line per cue, prefixed ``T<hh:mm:ss>``).

``locator_kind`` says how provenance points into the text: ``L`` for line spans (``L3-L5``), ``T`` for transcript
times (``T00:01:02``). Anything else (pdf, docx, audio, images, archives) raises ``Refused`` with
"convert to text first (pdf, docx, audio, images)": the agent converts it with its own tools and passes the text,
with the original file as ``original``.

The output is deterministic: the same bytes give the same text. Line endings become ``\\n``. Nothing here
interprets the text; it is data.
"""

from __future__ import annotations

import csv
import html
import io
import json
import os
import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple, Union

from . import util
from .errors import Refused, UsageError

CONVERT_FIRST = "convert to text first (pdf, docx, audio, images)"
LINES, TIMES = "L", "T"

# extension -> media type, for every format read here
MEDIA_TYPES: Dict[str, str] = {
    "txt": "text/plain",
    "text": "text/plain",
    "md": "text/markdown",
    "markdown": "text/markdown",
    "json": "application/json",
    "jsonl": "application/x-ndjson",
    "ndjson": "application/x-ndjson",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "html": "text/html",
    "htm": "text/html",
    "vtt": "text/vtt",
    "srt": "application/x-subrip",
}
# media type (or alias) -> reader name
_READERS: Dict[str, str] = {
    "text/plain": "plain",
    "text/markdown": "plain",
    "text/x-markdown": "plain",
    "application/json": "json",
    "text/json": "json",
    "application/x-ndjson": "plain",
    "application/jsonl": "plain",
    "application/x-jsonlines": "plain",
    "text/csv": "csv",
    "text/tab-separated-values": "tsv",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/vtt": "vtt",
    "application/x-subrip": "srt",
    "text/srt": "srt",
}
# magic numbers of binary formats that must be converted first, whatever the extension says; long enough that plain
# text starting with "ID3 tags ..." or "RIFF ..." is not taken for audio
_BINARY_MAGIC = (
    b"%PDF-", b"PK\x03\x04", b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"ID3\x02", b"ID3\x03", b"ID3\x04",
    b"OggS\x00", b"fLaC\x00", b"fLaC\x80", b"\x1f\x8b", b"7z\xbc\xaf", b"Rar!\x1a\x07", b"\xd0\xcf\x11\xe0",
)
_RIFF_FORMS = (b"WAVE", b"AVI ", b"WEBP")
_UTF32_BOMS = (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")


def media_type_of(path: str) -> Optional[str]:
    """The media type for ``path``'s extension, or None when it is not a text format read here."""
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return MEDIA_TYPES.get(ext)


def reader_of(media_type: Optional[str]) -> Optional[str]:
    """The reader for a media type or an extension: ``plain``, ``json``, ``csv``, ``tsv``, ``html``, ``vtt`` or
    ``srt``; None when the format is not read here."""
    if not media_type:
        return None
    base = str(media_type).split(";", 1)[0].strip().lower()
    if base in _READERS:
        return _READERS[base]
    if base in MEDIA_TYPES:  # a bare extension such as "csv"
        return _READERS[MEDIA_TYPES[base]]
    return None


def _charset(media_type: Optional[str]) -> Optional[str]:
    m = re.search(r";\s*charset=\"?([A-Za-z0-9._-]+)", media_type or "", re.I)
    return m.group(1) if m else None


def _refuse(what: str) -> Refused:
    return Refused("cannot read %s as text: %s" % (what, CONVERT_FIRST), convert=True)


def _binary(data: bytes) -> bool:
    return data.startswith(_BINARY_MAGIC) or (data[:4] == b"RIFF" and data[8:12] in _RIFF_FORMS)


def is_binary(data: bytes) -> bool:
    """``data`` is a binary format, not text: a known magic number, or a NUL byte in data with no UTF-16 or UTF-32
    byte-order mark. Such bytes cannot be checked for personal data here."""
    if _binary(data):
        return True
    if data.startswith(_UTF32_BOMS) or data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    return b"\x00" in data


def decode(data: bytes, charset: Optional[str] = None, what: str = "the input") -> str:
    """Text from bytes: a byte-order mark decides UTF-8, UTF-32 or UTF-16 (UTF-32 is tested first: its little-endian
    mark starts like UTF-16's), else ``charset``, else strict UTF-8. Binary data (a known magic number, or a NUL
    character in the decoded text) and bytes that are not valid text raise ``Refused``."""
    if _binary(data):
        raise _refuse(what)
    text: Optional[str] = None
    if data.startswith(b"\xef\xbb\xbf"):
        data, charset = data[3:], "utf-8"
    elif data.startswith(_UTF32_BOMS) or data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            text = data.decode("utf-32" if data.startswith(_UTF32_BOMS) else "utf-16")
        except UnicodeDecodeError:
            raise _refuse(what)
    if text is None:
        if b"\x00" in data:
            raise _refuse(what)
        try:
            text = data.decode(charset or "utf-8")
        except (UnicodeDecodeError, LookupError):
            raise Refused("%s is not valid %s text; save it as UTF-8 or %s" % (
                what, charset or "UTF-8", CONVERT_FIRST), convert=True)
    if "\x00" in text:
        raise _refuse(what)
    return text


def _newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def to_text(path_or_bytes: Union[str, bytes], media_type: Optional[str] = None) -> Tuple[str, str]:
    """``(text, locator_kind)`` for a file path or raw bytes. ``media_type`` may be a media type
    (``text/html; charset=utf-8``) or an extension (``csv``); for a path it defaults to the extension's type, for
    bytes to plain text. Unknown and binary formats raise ``Refused`` ("convert to text first ...")."""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        data, what = bytes(path_or_bytes), "the input"
        media_type = media_type or "text/plain"
    elif isinstance(path_or_bytes, str):
        what = os.path.basename(path_or_bytes) or path_or_bytes
        media_type = media_type or media_type_of(path_or_bytes)
        if reader_of(media_type) is None:
            raise _refuse(what)
        try:
            with open(path_or_bytes, "rb") as fh:
                data = fh.read()
        except IsADirectoryError:
            raise UsageError("%s is a folder, not a file" % path_or_bytes)
    else:
        raise UsageError("to_text needs a path or bytes")
    reader = reader_of(media_type)
    if reader is None:
        raise _refuse(what)
    text = _newlines(decode(data, _charset(media_type), what))
    if reader == "plain":
        return text, LINES
    if reader == "json":
        return json_text(text, what), LINES
    if reader in ("csv", "tsv"):
        return csv_text(text, "\t" if reader == "tsv" else None), LINES
    if reader == "html":
        return html_text(text), LINES
    return subtitle_text(text, what), TIMES


# json ----------------------------------------------------------------------------------------------------------
class _Object(list):
    """A JSON object as its list of ``(key, value)`` pairs, so repeated keys and key order survive."""


class _Raw(str):
    """A number or constant as spelled in the source (``1.10`` stays ``1.10``)."""


def _string(value: str) -> str:
    """A JSON string literal. A lone surrogate (from a valid ``\\ud83d`` escape) stays escaped, so the text can be
    written as UTF-8."""
    out = json.dumps(value, ensure_ascii=False)
    try:
        out.encode("utf-8")
    except UnicodeEncodeError:
        out = json.dumps(value, ensure_ascii=True)
    return out


def _dump(value: Any, level: int) -> str:
    pad, inner = " " * level, " " * (level + 1)
    items: List[str] = []
    if isinstance(value, _Object):
        if not value:
            return "{}"
        for k, v in value:
            items.append("%s%s: %s" % (inner, _string(k), _dump(v, level + 1)))
        return "{\n%s\n%s}" % (",\n".join(items), pad)
    if isinstance(value, list):
        if not value:
            return "[]"
        for v in value:
            items.append(inner + _dump(v, level + 1))
        return "[\n%s\n%s]" % (",\n".join(items), pad)
    if isinstance(value, _Raw):
        return str(value)
    if isinstance(value, str):
        return _string(value)
    return json.dumps(value)


def json_text(text: str, what: str = "the input") -> str:
    """A JSON document re-indented with one space per level and one key or item per line. Key order, repeated
    keys and number spellings are kept. Invalid JSON, and JSON nested deeper than ``util.JSON_MAX_DEPTH``, raise
    ``Refused`` (the first names the line)."""
    try:
        util.check_json_depth(text)  # the kit's one limit, the same on every Python version
        doc = json.loads(text, object_pairs_hook=_Object, parse_float=_Raw, parse_int=_Raw, parse_constant=_Raw)
        return _dump(doc, 0) + "\n"
    except RecursionError:
        raise Refused("%s nests JSON too deeply to re-indent; pass it as plain text instead" % what)
    except ValueError as exc:
        line = getattr(exc, "lineno", None)
        raise Refused("%s is not valid JSON%s; pass it as plain text instead" % (
            what, " (line %d)" % line if line else ""))


# csv -----------------------------------------------------------------------------------------------------------
_DELIMITERS = (",", "\t", ";", "|")


def _sniff(first_line: str) -> str:
    """The delimiter used most in the header line (ties: comma, tab, semicolon, bar)."""
    best, count = ",", 0
    for d in _DELIMITERS:
        n = first_line.count(d)
        if n > count:
            best, count = d, n
    return best


def _cell(value: str) -> str:
    return " ".join(value.split())


def csv_text(text: str, delimiter: Optional[str] = None) -> str:
    """``header: a | b | c``, then ``row N: a=1 | c=3`` for each non-empty record (empty cells are left out;
    cells past the header are named ``col<k>``; a cell's own line breaks become spaces)."""
    body = text.lstrip("\n")
    if not body.strip():
        return ""
    delim = delimiter or _sniff(body.split("\n", 1)[0])
    try:
        rows = [r for r in csv.reader(io.StringIO(body), delimiter=delim) if any(c.strip() for c in r)]
    except csv.Error as exc:
        raise Refused("the table cannot be read as CSV (%s); pass it as plain text instead" % exc)
    if not rows:
        return ""
    header: List[str] = []
    seen: Dict[str, int] = {}
    for k, name in enumerate(rows[0], start=1):
        name = _cell(name) or "col%d" % k
        seen[name] = seen.get(name, 0) + 1
        header.append(name if seen[name] == 1 else "%s (%d)" % (name, seen[name]))
    out = ["header: " + " | ".join(header)]
    for n, row in enumerate(rows[1:], start=1):
        cells = []
        for k, value in enumerate(row, start=1):
            value = _cell(value)
            if value:
                cells.append("%s=%s" % (header[k - 1] if k <= len(header) else "col%d" % k, value))
        out.append("row %d: %s" % (n, " | ".join(cells)))
    return "\n".join(out) + "\n"


# html ----------------------------------------------------------------------------------------------------------
# Elements whose content is never shown. ``head`` is not one: its end tag may be left out (HTML5), and its only
# text-bearing child, ``<title>``, is read on purpose; its scripts and styles skip themselves.
_SKIP = {"script", "style", "noscript", "template", "svg", "canvas", "iframe", "object"}
_PARAGRAPH = {
    "p", "div", "section", "article", "blockquote", "pre", "table", "ul", "ol", "dl", "header", "footer", "main",
    "nav", "aside", "figure", "form", "details", "fieldset", "address", "h1", "h2", "h3", "h4", "h5", "h6",
}
_LINE = {"br", "li", "tr", "dt", "dd", "title", "caption", "summary", "hr", "figcaption", "option", "legend"}
_CELLS = {"td", "th"}


class _TextParser(HTMLParser):
    """Visible text with block breaks: a new line per line-level block, a blank line per paragraph-level block,
    `` | `` between table cells and ``- `` before list items. Whitespace collapses outside ``<pre>``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: List[str] = []
        self.cur: List[str] = []
        self.skip = 0
        self.pre = 0
        self.cells = 0

    def _flush(self, blank: bool = False) -> None:
        line = "".join(self.cur)
        self.cur = []
        if self.pre:
            self.lines.extend(line.split("\n"))
        elif line.strip():
            self.lines.append(" ".join(line.split()))
        if blank and self.lines and self.lines[-1] != "":
            self.lines.append("")

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag in _SKIP:
            self.skip += 1
            return
        if self.skip:
            return  # the <title> of an inline <svg> is an icon label, not the page title
        if tag == "title":
            self._flush()
            return
        if tag in _PARAGRAPH:
            self._flush(blank=True)
        elif tag in _LINE:
            self._flush()
        if tag == "tr":
            self.cells = 0
        elif tag in _CELLS:
            if self.cells:
                self.cur.append(" | ")
            self.cells += 1
        elif tag == "li":
            self.cur.append("- ")
        elif tag == "pre":
            self.pre += 1

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if not self.skip and (tag in _LINE or tag in _PARAGRAPH):
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "title":
            self._flush(blank=True)
            return
        if tag == "pre":
            self._flush(blank=True)
            self.pre = max(0, self.pre - 1)
        elif tag in _PARAGRAPH:
            self._flush(blank=True)
        elif tag in _LINE:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        self.cur.append(data)

    def text(self) -> str:
        self._flush()
        lines = [line.rstrip() for line in self.lines]
        out: List[str] = []
        for line in lines:
            if line == "" and (not out or out[-1] == ""):
                continue
            out.append(line)
        while out and out[-1] == "":
            out.pop()
        return "\n".join(out) + "\n" if out else ""


def html_text(text: str) -> str:
    """The visible text of an HTML page (see ``_TextParser``); the title comes first when there is one."""
    parser = _TextParser()
    parser.feed(text)
    parser.close()
    return parser.text()


# vtt and srt ---------------------------------------------------------------------------------------------------
_CUE_TIME = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,]\d{1,3})?\s*-->")
_VOICE = re.compile(r"<v(?:\.[^ >]*)?\s+([^>]+)>")
_TAG = re.compile(r"<[^>]*>")


def subtitle_text(text: str, what: str = "the input") -> str:
    """One line per cue: ``T<hh:mm:ss> <cue text>``. Headers, notes, styles, cue numbers and markup are dropped; a
    voice tag ``<v Speaker>`` becomes ``Speaker: ``. A file with no cue raises ``UsageError``."""
    out: List[str] = []
    for block in re.split(r"\n[ \t]*\n", text.strip("\n")):
        lines = block.split("\n")
        at = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if at is None:
            continue  # WEBVTT header, NOTE, STYLE or REGION blocks
        m = _CUE_TIME.match(lines[at])
        if not m:
            continue
        hours, minutes, seconds = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
        words = " ".join(line.strip() for line in lines[at + 1:] if line.strip())
        words = _VOICE.sub(lambda v: v.group(1).strip() + ": ", words)
        words = " ".join(html.unescape(_TAG.sub("", words)).split())
        if words:
            out.append("T%02d:%02d:%02d %s" % (hours, minutes, seconds, words))
    if not out:
        raise UsageError("%s has no subtitle cues (no line with -->)" % what)
    return "\n".join(out) + "\n"
