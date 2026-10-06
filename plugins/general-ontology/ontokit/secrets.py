"""Secret-like string patterns shared by the secret scan, the ingest redactor and validate, plus a denylist scan.

Standard library only (Python 3.9+). The scans read bytes with ``SECRET_PATTERNS`` and the redactor works on text
with ``TEXT_PATTERNS``. Both are compiled from the same sources below, so a pattern added here is scanned for at
release time, refused or redacted at ingest, and reported by validate.

The lookbehinds keep words such as "task-list-..." from matching "sk-". A token right after an escape
sequence (``\\n``, ``\\t``, the ``\\u`` form, ``%3D``) still counts as the start of a word, so a key inside
double-encoded JSON or a URL-encoded value is found (``start()`` and ``edge()``).

The denylist is a private file kept outside the repo: one term or regex per line, ``#`` for comments, matched
case-insensitively (``load_denylist``, ``scan_denylist``). Output never prints a match: a secret hit shows its
kind and first 6 characters, and a denylist hit shows the file, the line and the first 6 characters of the term.

The file scans (``scan_file``, ``scan_paths``) also run the source sanitizer's credential rules over text files
(``credential_kinds``: ``NAME_PASSWORD=...``, ``scheme://user:pass@``, Authorization and cookie headers, base64 key
blocks), so a file a release carries meets the rule a source meets at ingest; they report the kind only. Files
inside a copy of the kit's plugin folder skip those rules: its tests and sanitizer spell such examples on purpose.

CLI: ``python3 -m ontokit.secrets scan PATH...`` reads every file under each path (``.git`` skipped) and prints
each hit as ``file: kind (starts XXXXXX)``, never the match. Exit codes: 0 clean, 1 hits, 2 usage (no path, or a
path that does not exist).
"""

from __future__ import annotations

import os
import re
import sys
from typing import Dict, List, Optional, Sequence, Tuple

# Escape sequences that end in a letter or digit: JSON and Python escapes inside double-encoded text (``\n``,
# ``\t``, the ``\u`` form, ``\x22``) and URL escapes (``%3D``). A token right after one starts a new word.
_ESCAPES = (r"\\[ntrfbv]", r"\\u[0-9A-Fa-f]{4}", r"\\x[0-9A-Fa-f]{2}", r"%[0-9A-Fa-f]{2}")
ESCAPE_LOOKBEHINDS = tuple("(?<=%s)" % e for e in _ESCAPES)


def start(chars: str) -> str:
    """Regex for the start of a token: not right after one of ``chars`` (a character class body), or right
    after an escape sequence (``ESCAPE_LOOKBEHINDS``), so ``"a\\nghp_..."`` in double-encoded JSON is found."""
    return "(?:(?<![%s])|%s)" % (chars, "|".join(ESCAPE_LOOKBEHINDS))


def edge(chars: str, width: int) -> str:
    """``start(chars)`` checked after the first ``width`` characters of a token, looking back over them. A pattern
    that begins with the token's literal start lets the regex engine jump straight to candidates, which makes
    the scans many times faster than a pattern that begins with a lookbehind."""
    skip = ".{%d}" % width
    return "(?:(?<![%s]%s)|%s)" % (chars, skip, "|".join("(?<=%s%s)" % (e, skip) for e in _ESCAPES))


_W = "A-Za-z0-9_"
_WD = "A-Za-z0-9_-"
_AN = "A-Za-z0-9"
# (kind, regex source). Every source is ASCII-only, so it compiles the same way for bytes and for str.
_SOURCES: Tuple[Tuple[str, str], ...] = (
    (
        "github",
        r"(?:gh[oprsu]_" + edge(_W, 4) + r"[A-Za-z0-9]{20,}|github_pat_" + edge(_W, 11) + r"[A-Za-z0-9_]{20,})",
    ),
    ("anthropic", r"sk-ant-" + edge(_WD, 7) + r"[A-Za-z0-9_-]{20,}"),
    ("openai", r"sk-" + edge(_WD, 3) + r"(?!ant-)[A-Za-z0-9_-]{20,}"),
    ("google", r"AIza" + edge(_WD, 4) + r"[0-9A-Za-z_-]{35}"),
    ("google_token", r"ya29\." + edge(_WD, 5) + r"[0-9A-Za-z_-]{20,}"),
    ("aws", r"AKIA" + edge(_AN, 4) + r"[0-9A-Z]{16}"),
    # PEM (RSA, EC, OPENSSH, ENCRYPTED, PKCS#8), PGP armor (``... PRIVATE KEY BLOCK-----``), the SSH2 form of
    # ssh.com (four dashes and spaces) and PuTTY key files
    (
        "private_key",
        r"(?:-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|---- BEGIN SSH2 ENCRYPTED PRIVATE KEY -{4}"
        r"|PuTTY-User-Key-File-[0-9]+: )",
    ),
    ("slack", r"xox[a-z]-" + edge(_AN, 5) + r"[A-Za-z0-9-]{10,}"),
    ("slack_webhook", r"hooks\.slack\.com/(?:services|workflows|triggers)/[A-Za-z0-9_-]+/[A-Za-z0-9/_-]{16,}"),
    (
        "stripe",
        r"(?:[sr]k_(?:live|test)_" + edge(_W, 8) + r"[A-Za-z0-9]{16,}|whsec_" + edge(_W, 6) + r"[A-Za-z0-9]{24,})",
    ),
    ("linear", r"lin_(?:api_" + edge(_W, 8) + r"|oauth_" + edge(_W, 10) + r")[A-Za-z0-9]{32,}"),
    ("gitlab", r"glpat-" + edge(_WD, 6) + r"[A-Za-z0-9_-]{20,}"),
    ("jwt", r"eyJ" + edge(_WD, 3) + r"[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ("npm", r"npm_" + edge(_W, 4) + r"[A-Za-z0-9]{36}(?![A-Za-z0-9])"),
    ("sendgrid", r"SG\." + edge(_WD, 3) + r"[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}"),
    ("twilio", r"SK" + edge(_AN, 2) + r"[0-9a-f]{32}(?![A-Za-z0-9])"),
    ("google_oauth", r"GOCSPX-" + edge(_WD, 7) + r"[A-Za-z0-9_-]{28}"),
    ("huggingface", r"hf_" + edge(_W, 3) + r"[A-Za-z0-9]{30,}"),
    ("digitalocean", r"do[opr]_v1_" + edge(_W, 7) + r"[A-Za-z0-9]{32,}"),
)

# Substrings one of which every match contains. The redactor skips a pattern for text that holds none of them (a
# fast ``in`` test instead of a regex scan); the scans never use them, so a wrong hint cannot hide a secret there.
HINTS: Dict[str, Tuple[str, ...]] = {
    "github": ("ghp_", "gho_", "ghr_", "ghs_", "ghu_", "github_pat_"),
    "anthropic": ("sk-ant-",),
    "openai": ("sk-",),
    "google": ("AIza",),
    "google_token": ("ya29.",),
    "aws": ("AKIA",),
    "private_key": ("PRIVATE KEY", "PuTTY-User-Key-File-"),
    "slack": ("xox",),
    "slack_webhook": ("hooks.slack.com/",),
    "stripe": ("sk_live_", "sk_test_", "rk_live_", "rk_test_", "whsec_"),
    "linear": ("lin_api_", "lin_oauth_"),
    "gitlab": ("glpat-",),
    "jwt": ("eyJ",),
    "npm": ("npm_",),
    "sendgrid": ("SG.",),
    "twilio": ("SK",),
    "google_oauth": ("GOCSPX-",),
    "huggingface": ("hf_",),
    "digitalocean": ("dop_v1_", "doo_v1_", "dor_v1_"),
}

KINDS: Tuple[str, ...] = tuple(kind for kind, _ in _SOURCES)
SECRET_PATTERNS: Tuple[Tuple[str, "re.Pattern[bytes]"], ...] = tuple(
    (kind, re.compile(source.encode("ascii"))) for kind, source in _SOURCES
)
TEXT_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = tuple(
    (kind, re.compile(source)) for kind, source in _SOURCES
)


def scan_bytes(data: bytes) -> List[Tuple[str, bytes]]:
    """(kind, match) for every secret-like string in ``data``. Callers print a prefix, never the match."""
    return [(kind, m.group(0)) for kind, rx in SECRET_PATTERNS for m in rx.finditer(data)]


def scan_str(text: str) -> List[Tuple[str, str]]:
    """(kind, match) for every secret-like string in ``text``."""
    return [(kind, m.group(0)) for kind, rx in TEXT_PATTERNS for m in rx.finditer(text)]


def is_kit_folder(folder: str) -> bool:
    """True for a copy of the kit's plugin folder (it holds ``ontokit/__init__.py`` and ``bin/onto``)."""
    return os.path.isfile(os.path.join(folder, "ontokit", "__init__.py")) and \
        os.path.isfile(os.path.join(folder, "bin", "onto"))


def in_kit(path: str, cache: Optional[Dict[str, bool]] = None) -> bool:
    """True when ``path`` sits inside a copy of the kit's plugin folder (``is_kit_folder``)."""
    cache = {} if cache is None else cache
    folder = os.path.dirname(os.path.abspath(path))
    seen: List[str] = []
    found = False
    while True:
        if folder in cache:
            found = cache[folder]
            break
        seen.append(folder)
        if is_kit_folder(folder):
            found = True
            break
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    for item in seen:
        cache[item] = found
    return found


def credential_kinds(data: bytes) -> List[str]:
    """The credential kinds the source sanitizer refuses in ``data`` (``sanitize.check_text`` with every personal
    kind kept) beyond the patterns above: ``NAME_PASSWORD=...``, JSON password keys, ``scheme://user:pass@``,
    Authorization, Bearer, Basic and cookie headers, base64 key blocks. Empty for data that is not UTF-8 text.
    So a file the release carries meets the rule a source meets at ingest."""
    if b"\0" in data[:8192]:
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []
    from . import sanitize  # sanitize builds on this module

    keep = {"personal": {kind: "keep" for kind in sanitize.PERSONAL_KINDS}}
    return [kind for kind in sanitize.check_text(text, keep) if kind not in KINDS]


def scan_file(path: str, data: bytes, kit_cache: Optional[Dict[str, bool]] = None) -> List[Tuple[str, str]]:
    """(kind, first 6 characters) for every secret-like string in one file's ``data``, plus the sanitizer's
    credential kinds (``credential_kinds``, prefix empty) unless ``path`` sits inside a copy of the kit, whose
    tests and sanitizer spell such examples on purpose (the patterns above still scan it)."""
    hits = [(kind, match[:6].decode("ascii", "replace")) for kind, match in scan_bytes(data)]
    if not in_kit(path, kit_cache):
        hits.extend((kind, "") for kind in credential_kinds(data))
    return hits


def scan_paths(paths: Sequence[str]) -> Tuple[int, List[Tuple[str, str, str]]]:
    """(files read, [(file, kind, first 6 characters)]) for the files under ``paths``, ``.git`` skipped
    (``scan_file``: the sanitizer's credential kinds count too, with an empty prefix)."""
    files = _walk_files(paths)
    hits: List[Tuple[str, str, str]] = []
    cache: Dict[str, bool] = {}
    for name in files:
        try:
            with open(name, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        hits.extend((name, kind, prefix) for kind, prefix in scan_file(name, data, cache))
    return len(files), hits


def load_denylist(path: str) -> List["re.Pattern[str]"]:
    """Compiled, case-insensitive patterns from a denylist file: one term or regex per line; blank lines and lines
    starting with ``#`` are skipped. A line that is not a valid regex is matched as a literal term."""
    patterns: List["re.Pattern[str]"] = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                patterns.append(re.compile(line, re.I))
            except re.error:
                patterns.append(re.compile(re.escape(line), re.I))
    return patterns


SKIP_DIRS = (".git", "__pycache__")


def _walk(paths: Sequence[str]) -> List[Tuple[str, str]]:
    """(file, path relative to the scanned argument) for every file under ``paths``, sorted. ``.git`` is skipped, and
    so is ``__pycache__``: bytecode compiled from the ``.py`` beside it (scanned itself), gitignored and never
    released, where a newer Python folds a split test constant such as ``"AK" + "IA" + ...`` into one string."""
    files: List[Tuple[str, str]] = []
    for path in paths:
        if os.path.isfile(path):
            files.append((path, os.path.basename(path)))
        for root, dirs, names in os.walk(path):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(n for n in names if n != ".git"):  # a worktree's or submodule's gitdir pointer
                full = os.path.join(root, name)
                files.append((full, os.path.relpath(full, path)))
    return files


def _walk_files(paths: Sequence[str]) -> List[str]:
    return [full for full, _rel in _walk(paths)]


def scan_denylist(paths: Sequence[str], patterns: Sequence["re.Pattern[str]"]) -> List[Tuple[str, int, str]]:
    """(file, line, first 6 characters of the term) for every denylist hit under ``paths`` (``.git`` skipped).
    Line 0 means the hit is in the file's path below the scanned folder. Never returns the matched text."""
    hits: List[Tuple[str, int, str]] = []
    if not patterns:
        return hits
    for name, rel in _walk(paths):
        for rx in patterns:
            if rx.search(rel):
                hits.append((name, 0, rx.pattern[:6]))
        try:
            with open(name, "rb") as fh:
                text = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for rx in patterns:
                if rx.search(line):
                    hits.append((name, number, rx.pattern[:6]))
    return hits


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2 or args[0] != "scan":
        print("usage: python3 -m ontokit.secrets scan PATH...", file=sys.stderr)
        return 2
    missing = [p for p in args[1:] if not os.path.exists(p)]
    if missing:
        print("scan: no such path: %s" % ", ".join(missing), file=sys.stderr)
        return 2
    count, hits = scan_paths(args[1:])
    for name, kind, prefix in hits:
        print("%s: %s (starts %r)" % (name, kind, prefix) if prefix else "%s: %s" % (name, kind))
    print("scan: %d file(s), %s" % (count, "%d secret-like string(s)" % len(hits) if hits else "clean"))
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
