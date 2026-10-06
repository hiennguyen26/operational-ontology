"""The redactor and the personal-data policy: one mode for ingest, validate and export.

``sanitize_text(text, policy)`` is what ``sources.add`` stores text through. Credentials refuse the whole text
(``Refused`` naming their kinds; nothing is written). Personal data follows ``policy.personal`` per kind: ``keep``
leaves it, ``redact`` replaces each hit with ``[redacted:<kind>]`` and counts it, ``refuse`` refuses the text.
``check_text(text, policy)`` lists the kinds ``sanitize_text`` would act on, so ``validate`` (P18) and the export
use the very same rules: text that ``check_text`` passes is stored, exported and validated unchanged.

Credential kinds:

- every pattern of ``secrets`` (the release scan), under its own kind name, and whole private key blocks
  (``private_key``) or runs of 60 to 76 character base64 lines (``key_block``); ``EXTRA_TOKENS`` adds token
  formats the release scan does not list yet (``huggingface``, ``digitalocean``);
- ``auth_header``, ``cookie``, ``bearer``, ``basic``: Authorization and API-key headers with a token-like value,
  header-shaped cookies (``name=value`` pairs with a token-like value), Bearer tokens, and Basic credentials (padded,
  or base64 of ``user:pass``) anywhere;
- ``url_token``: URL query values named ``*token``, ``*key``, ``*sig``, ``*signature``, ``*secret``,
  ``*password``, ``*auth``, ``X-Amz-*`` and ``X-Goog-*`` that hold a digit or are 12 or more characters long;
  ``url_credentials``: ``scheme://user:pass@``;
- ``credential``: ``NAME=value`` or ``"name": "value"`` where the name ends in key, token, secret or password; an
  unquoted ``*PASSWORD=value`` (also ``*PASSWD``, ``*PWD``, ``*SECRET``) of 6 or more characters; in JSON, any
  real string under such a name and a number under a name ending in password, passwd, pwd, secret or key
  (``db_password``), except the redactor's own kind names (a count such as ``{"private_key": 1}``) and structural
  names (``SORT_KEY``); ``curl -u user:pass``, ``.netrc`` passwords, ``.pgpass`` lines, ``mysql -pSECRET``,
  ``sshpass -p SECRET`` and ``SSHPASS=``. In prose, a password, passphrase, passcode, PIN or door, gate, alarm,
  lock, keypad or access code that ``is``, ``was`` or is ``set to`` (or ``:``, ``=``) a value with a digit: 4 to 12
  digits, or 6 or more characters with a letter (``the wifi password is Garden2026``).
  Only ``$NAME``, ``${NAME}`` and ``$(...)`` count as placeholders, so ``"$3cret..."`` is a credential.

Personal kinds (``PERSONAL_KINDS``): ``email``; ``phone``: North American numbers (``(212) 555-0142``), ``+``
country code numbers of 8 to 15 digits in groups of up to 12, with an optional ``(0)`` or bracketed area code
(``+44 7700 900123``, ``+44 (0)20 7946 0958``), national numbers with a trunk 0 in groups of one separator kind
(``07700 900123``, ``020 7946 0958``, ``06 12 34 56 78``, ``(02) 9876 5432``: 10 to 12 digits, not a zero-padded
list), and any number of 7 to 15 digits after a phone word (``phone``, ``tel``, ``mobile``, ``cell``, ``fax``,
``WhatsApp``, ``my number is``, ``call me on``, ``reach Lee at``) or a field name holding one of those words
(``phone_number=``, ``contactPhone:``, and in JSON ``{"mobile": 2125550142}``), except a date: ``07700900123``,
``0044 20 7946 0958``, ``98765 43210``; ``address`` (a numbered street line such as ``12 Elm Street``, or a PO
box); ``name`` (the user name in a home-directory path, and the login of a ``.netrc`` entry); ``payment_card`` (13
to 19 digits with a card network's prefix and length that pass the Luhn check, bare or in groups split by one kind
of space or hyphen, but not inside a longer list of such groups); ``government_id`` (a US SSN or ITIN written
``123-45-6789`` anywhere, or as 9 digits after "SSN", "social security", "tax id" or "ITIN"; a passport, driver's
license, national insurance or national id number after its name). A kind a topic's ``policy.personal`` does not
list gets ``DEFAULT_PERSONAL`` (``redact`` for the kinds added after ``store.DEFAULT_POLICY``).

A hit counts right after an escape sequence too (``\\n``, ``\\t``, ``\\u0022``, ``%3D``), so JSON inside a JSON
string, or URL-encoded text, is treated like plain text. Text holding ``%XX`` escapes is also decoded, and the
personal rules run on the decoded copy (``+1%20212%20555%200142``); a hit redacts the encoded span.

The rules run again until nothing changes (at most ``MAX_PASSES`` times): a redaction can open a token boundary
for a rule that already ran (``12 Elm Street.(212) 555-0142``), and what ``sanitize_text`` returns must pass
``check_text``. Text that still changes after ``MAX_PASSES`` passes is refused. Every rule is linear in the text
length: a token start never sits inside a run of the characters the token itself is made of.

JSON is read as JSON: a whole document, or each JSONL line, is parsed and every string (and key) is checked, so
the key-name rules apply. Every copy of a repeated object key is checked (a rewritten line names the later copies
``"<key> (dup)"``). A line or document with nothing to redact keeps its exact characters, and a rewritten one keeps
every number as it was spelled (``2.50``, ``1e400``). JSON nested too deeply to walk is read as plain text.

``redact_file(src, dst, policy)`` writes the sanitized copy to a temp file next to ``dst``, scans it again with the
release patterns, and only then renames it into place.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import json
import os
import re
import tempfile
from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import secrets, store
from .errors import Refused, UsageError

MARK = "[redacted:%s]"
MAX_PASSES = 8
ACTIONS = ("keep", "redact", "refuse")
PERSONAL_KINDS = ("email", "phone", "name", "address", "payment_card", "government_id")
# The policy of a topic written before a kind existed does not name it: such a kind is redacted.
DEFAULT_PERSONAL = dict(store.DEFAULT_POLICY["personal"])
for _kind in PERSONAL_KINDS:
    DEFAULT_PERSONAL.setdefault(_kind, "redact")
del _kind

PLACEHOLDER_PREFIXES = ("[redacted", "<", "{", "%(", "%{", "***", "...", "your", "example", "placeholder", "dummy")
PLACEHOLDER_WORDS = {
    "none", "null", "nil", "true", "false", "undefined", "changeme", "password", "secret", "token", "redacted",
    "hidden", "masked", "string", "str",
}
PASSWORD_NAMES = ("password", "passwd", "pwd", "secret")
AUTH_SCHEMES = {"bearer", "basic", "token", "digest", "apikey", "key", "negotiate"}
SAFE_EMAIL_LOCALS = {"git", "noreply", "no-reply"}
SAFE_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "users.noreply.github.com")
FILE_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "js", "mjs", "cjs", "ts", "tsx", "jsx", "py", "css", "scss",
    "json", "md", "html", "txt", "yaml", "yml", "toml", "lock", "map",
}
COMMON_USER_WORDS = {
    "user", "users", "admin", "root", "runner", "home", "shared", "public", "guest", "test", "dev", "ubuntu", "name",
    "username", "me", "you", "default", "all",
}
# A shell or template variable (``$NAME``, ``${NAME}``, ``$(cmd)``) stands for a value; "$3cretPass" is one.
_VARIABLE = re.compile(r"\$(?:[A-Za-z_]|\{|\()")
# JSON fields whose value is always a credential (the key is compared lower-case, letters and digits only).
SENSITIVE_KEYS = {
    "password", "passwd", "pwd", "secret", "clientsecret", "apikey", "xapikey", "apitoken", "accesskey", "secretkey",
    "secretaccesskey", "privatekey", "accesstoken", "refreshtoken", "idtoken", "authtoken", "sessiontoken",
    "bearertoken", "token", "authorization", "proxyauthorization", "cookie", "setcookie", "passphrase", "passcode",
    "auth",  # docker's config.json: base64 of user:password
}
# A JSON key whose whole name says credential (``SERVICE_API_KEY``, ``db_password``, ``refresh_token``). A full
# match, so ``input_tokens`` and ``max_tokens`` are kept.
_SENSITIVE_KEY_NAME = re.compile(
    r"(?i:[A-Za-z0-9_.-]*(?:api[_-]?key|access[_-]?key|secret[_-]?key|private[_-]?key|auth[_-]?key"
    r"|client[_-]?secret|secret|token|password|passwd|pwd|credentials?))|[A-Z0-9_]*_KEY"
)
# ``*_KEY`` names that hold structure, not a secret: a lowercase identifier under one (``SORT_KEY=created_at2``), or
# a number, is configuration. A longer name ending in one counts too (``S3_OBJECT_KEY``). Every other ``*_KEY``
# (``JWT_KEY``, ``SIGNING_KEY``, ``ENCRYPTION_KEY``) is a credential whatever its value looks like.
STRUCTURAL_KEYS = (
    "SORT_KEY", "CACHE_KEY", "PARTITION_KEY", "PRIMARY_KEY", "FOREIGN_KEY", "HASH_KEY", "RANGE_KEY", "SHARD_KEY",
    "ROUTING_KEY", "GROUP_KEY", "IDEMPOTENCY_KEY", "DEDUP_KEY", "OBJECT_KEY", "S3_KEY",
)
# A number under a credential name that ends in one of these is a secret (``{"db_password": 84736251}``); a
# ``*token`` or ``*credentials`` name holds a count (``max_token``).
NUMERIC_SECRET_SUFFIXES = ("password", "passwd", "pwd", "secret", "key")


def _is_placeholder(value: str) -> bool:
    low = value.lower()
    if not value or low.startswith(PLACEHOLDER_PREFIXES) or low in PLACEHOLDER_WORDS or _VARIABLE.match(value):
        return True
    return len(set(value)) <= 2  # "xxxxxxxx", "********"


def _sensitive_key(key: Any) -> bool:
    """A JSON key whose value is a credential by name alone."""
    if not isinstance(key, str):
        return False
    return _exact_sensitive_key(key) or bool(_SENSITIVE_KEY_NAME.fullmatch(key))


def _exact_sensitive_key(key: Any) -> bool:
    return isinstance(key, str) and re.sub(r"[^a-z0-9]", "", key.lower()) in SENSITIVE_KEYS


def _structural_key(name: Any) -> bool:
    if not isinstance(name, str):
        return False
    upper = name.upper()
    return any(upper == k or upper.endswith("_" + k) for k in STRUCTURAL_KEYS)


def _numeric_secret_key(key: Any) -> bool:
    """A JSON key under which a number is a secret: a credential name ending in password, passwd, pwd, secret or
    key. Not one of the redactor's own kind names (``RULE_KINDS``: records keep redaction counts such as
    ``{"private_key": 1}``), and not a ``STRUCTURAL_KEYS`` name."""
    if not _sensitive_key(key) or key in RULE_KINDS or _structural_key(key):
        return False
    return re.sub(r"[^a-z0-9]", "", key.lower()).endswith(NUMERIC_SECRET_SUFFIXES)


def _decodes_to_pair(value: str) -> bool:
    """``value`` is base64 of printable ``user:password`` text (HTTP Basic, docker's ``auth``)."""
    token = value.rstrip("=")
    if len(token) < 6 or not re.fullmatch(r"[A-Za-z0-9+/]+", token):
        return False
    try:
        text = base64.b64decode(token + "=" * (-len(token) % 4), validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return False
    _user, sep, secret = text.partition(":")
    return bool(sep and secret) and text.isprintable()


def _has_digit(value: str) -> bool:
    return any("0" <= ch <= "9" for ch in value)


def _token_like(value: str) -> bool:
    """A value that looks like a secret rather than a word: 8 or more characters with a digit, or 16 or more."""
    return not _is_placeholder(value) and (len(value) >= 16 or (len(value) >= 8 and _has_digit(value)))


# The escape-sequence alternatives of secrets.start(). Each needs a backslash or a percent sign, so a rule runs a
# copy of its pattern without them on text that has neither (most text): the same hits, found faster.
_ESCAPE_ALTERNATIVES = tuple("|" + alt for alt in secrets.ESCAPE_LOOKBEHINDS)


class Rule(object):
    """One redaction: ``pattern`` finds candidates, ``group`` is the part replaced, ``check`` can veto, and
    ``hints`` (substrings every match holds) let a rule skip text that cannot match."""

    def __init__(
        self,
        kind: str,
        pattern: "re.Pattern[str]",
        group: int = 0,
        check: Optional[Callable[[str, "re.Match[str]"], bool]] = None,
        hints: Tuple[str, ...] = (),
    ) -> None:
        self.kind, self.rx, self.group, self.check, self.hints = kind, pattern, group, check, hints
        plain = pattern.pattern
        for alternative in _ESCAPE_ALTERNATIVES:
            plain = plain.replace(alternative, "")
        self.plain = pattern if plain == pattern.pattern else re.compile(plain, pattern.flags)

    def apply(self, text: str, counts: Counter) -> str:
        if self.hints and not any(h in text for h in self.hints):
            return text
        rx = self.rx if ("\\" in text or "%" in text) else self.plain

        def sub(m: "re.Match[str]") -> str:
            value = m.group(self.group)
            if value is None or (self.check is not None and not self.check(value, m)):
                return m.group(0)
            counts[self.kind] += 1
            whole, base = m.group(0), m.start()
            start, end = m.span(self.group)
            return whole[: start - base] + MARK % self.kind + whole[end - base:]

        return rx.sub(sub, text)

    def spans(self, text: str) -> List[Tuple[int, int]]:
        """The ``(start, end)`` of every part of ``text`` this rule would replace."""
        out = []
        for m in self.rx.finditer(text):
            part = m.group(self.group)
            if part is None or (self.check is not None and not self.check(part, m)):
                continue
            start, end = m.span(self.group)
            if end > start:
                out.append((start, end))
        return out


# detection-only folding: one character for one, so a span found in the folded copy is the same span of the text
FOLD = {cp: 0x20 for cp in (0x00A0, 0x1680, 0x202F, 0x205F, 0x3000) + tuple(range(0x2000, 0x200B))}
FOLD.update({cp: ord("-") for cp in tuple(range(0x2010, 0x2016)) + (0x2212, 0xFE58, 0xFE63)})
FOLD.update({cp: cp - 0xFEE0 for cp in range(0xFF01, 0xFF5F)})  # fullwidth ASCII forms
_FOLD_RE = re.compile("[%s]" % "".join("\\u%04x" % cp for cp in sorted(FOLD)))


# rule checks ---------------------------------------------------------------------------------------------------
def _check_bearer(value: str, m: "re.Match[str]") -> bool:
    return _has_digit(value) and not _is_placeholder(value)


def _check_basic(value: str, m: "re.Match[str]") -> bool:
    # HTTP Basic is base64 of user:pass; "Basic Kitchen101 course" is prose
    return value.endswith("=") or _decodes_to_pair(value)


def _check_header(value: str, m: "re.Match[str]") -> bool:
    if _is_placeholder(value) or value.lower() in AUTH_SCHEMES:
        return False
    # "Authorization: the process of..." and "Authorization: 123 forms" are prose; "Basic dXNlcjpwYXNz" (user:pass)
    # has no digit and is short
    return (len(value) >= 8 and _has_digit(value)) or len(value) >= 20 or _decodes_to_pair(value)


def _check_auth_value(value: str, m: "re.Match[str]") -> bool:
    return _decodes_to_pair(value)


def _check_user_pass(value: str, m: "re.Match[str]") -> bool:
    """``-u user:pass``: not a placeholder, and not ``uid:gid`` (``docker run -u 1000:1000``)."""
    return not _is_placeholder(value) and not value.isdigit()


def _check_key_block(value: str, m: "re.Match[str]") -> bool:
    return bool(re.search(r"[G-Zg-z+/]", value))  # base64, not a column of hex digests


# one ``name=value`` pair of a Cookie header (RFC 6265 token characters in the name)
_COOKIE_PAIR = re.compile(r"[ \t]*[!#$%&'*+.^_`|~0-9A-Za-z-]+=([^\s;,]*)[ \t]*$")


def _check_cookie(value: str, m: "re.Match[str]") -> bool:
    """A header-shaped cookie: ``;``-separated pieces that start with a ``name=value`` pair, one of whose values is
    token-like. "Cookie: butter=200g, sugar=150g" on a recipe card is prose."""
    pieces = value.split(";")
    if _COOKIE_PAIR.match(pieces[0]) is None:
        return False
    return any(pair is not None and _token_like(pair.group(1)) for pair in map(_COOKIE_PAIR.match, pieces))


def _check_value(value: str, m: "re.Match[str]") -> bool:
    return not _is_placeholder(value)


# a dotted name such as ``process.env.DB_PASSWORD`` or ``settings.password``: code, not a value
_DOTTED_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


def _check_credential(value: str, m: "re.Match[str]") -> bool:
    """``NAME=value``: token-like values, not code (``os.getenv``), paths or placeholders. A password needs no
    token shape when it is quoted, or written ``*PASSWORD=value`` with nothing around the ``=`` (a shell or env
    line)."""
    if _is_placeholder(value) or value.startswith(("/", "~", "./", "../")) or "://" in value:
        return False
    name, sep = m.group(1).lower(), m.group(2)
    if name.endswith(PASSWORD_NAMES) and len(value) >= 6:
        if sep[-1:] in ('"', "'"):
            return True  # a quoted password need not look like a token
        after = m.string[m.end(3): m.end(3) + 1]
        if sep == "=" and after not in ("(", "[", "{", "<") and not _DOTTED_NAME.fullmatch(value):
            return True  # DB_PASSWORD=letmein
    return len(value) >= 8 and _has_digit(value) and any(ch.isalpha() for ch in value)


# A name the credential rule matches as an API, access, secret, private or auth key, a token or a password (not
# only through its ``*_KEY`` form), and a lowercase identifier (``created_at2``), the value of a structural key.
_TOKEN_NAME = re.compile(
    r"(?i:[A-Za-z0-9_.-]*(?:api[_-]?key|access[_-]?key|secret[_-]?key|private[_-]?key|auth[_-]?key"
    r"|client[_-]?secret|secret|token|password|passwd|pwd|credentials?))"
)
_IDENTIFIER = re.compile(r"[a-z]+[0-9]*(?:[_.][a-z]+[0-9]*)+")


def _check_record_credential(value: str, m: "re.Match[str]") -> bool:
    """``NAME=value``: as ``_check_credential``, except that a ``STRUCTURAL_KEYS`` setting whose value is a
    lowercase identifier (``SORT_KEY=created_at2``) is kept. The name decides, not the value's shape:
    ``JWT_KEY=super_secret1`` is a credential."""
    if not _check_credential(value, m):
        return False
    name = m.group(1)
    if _TOKEN_NAME.fullmatch(name) or not _structural_key(name):
        return True
    return not _IDENTIFIER.fullmatch(value)


def _check_query(value: str, m: "re.Match[str]") -> bool:
    if _is_placeholder(value):
        return False
    return _has_digit(value) or len(value) >= 12  # "?key=value" and "?key=north-bed" are documentation


def _check_email(value: str, m: "re.Match[str]") -> bool:
    local, domain = m.group(1).lower(), m.group(2).lower()
    if local in SAFE_EMAIL_LOCALS or any(domain == d or domain.endswith("." + d) for d in SAFE_EMAIL_DOMAINS):
        return False
    if not any(ch.isalnum() for ch in local):
        return False  # "+@pytest.fixture" in a diff is a decorator
    # image@2x.png and icon@1.5x.png are file names; someone@clinic.co.py is an address
    scale = re.fullmatch(r"[0-9]+(?:\.[0-9]+)?x\.([a-z0-9]+)", domain)
    return not (scale and scale.group(1) in FILE_EXTENSIONS)


def _phone_digits(value: str) -> int:
    """The digits of a phone number, not counting a ``(0)`` trunk written inside an international one."""
    return sum(1 for ch in value.replace("(0)", "") if "0" <= ch <= "9")


def _check_intl_phone(value: str, m: "re.Match[str]") -> bool:
    return 8 <= _phone_digits(value) <= 15


def _check_national_phone(value: str, m: "re.Match[str]") -> bool:
    """A national number with its trunk 0 and no cue word (``07700 900123``, ``020 7946 0958``, ``06 12 34 56 78``,
    ``(02) 9876 5432``): 10 to 12 digits in 2 to 5 groups, one kind of separator after the area code, a subscriber
    number that does not start with 0, and 2-digit groups only in a number of 4 or more groups. Zero-padded lists
    (``010 020 030 040``, ``01 02 03 04 05``) are kept."""
    body, groups = value, []
    if body.startswith("("):
        area, _close, body = body[1:].partition(")")
        groups.append(area)
        body = body.lstrip(" ")
    groups += re.findall(r"[0-9]+", body)
    if len(set(re.findall(r"[^0-9]+", body))) > 1:
        return False
    if not (10 <= sum(map(len, groups)) <= 12 and 2 <= len(groups) <= 5) or groups[1].startswith("0"):
        return False
    return len(groups) >= 4 or all(len(g) >= 3 for g in groups[1:])


# a date written with one separator (``12.10.2026``, ``2026 10 12``), or a US SSN or ITIN (``123-45-6789``, which
# a policy may keep): not a phone number after "call me on"
_NOT_A_PHONE = re.compile(
    r"[0-9]{1,2}([ ./-])[0-9]{1,2}\1[0-9]{2,4}|[0-9]{4}([ ./-])[0-9]{1,2}\2[0-9]{1,2}|[0-9]{3}-[0-9]{2}-[0-9]{4}"
)


def _check_cued_phone(value: str, m: "re.Match[str]") -> bool:
    """Any number of 7 to 15 digits after a phone word (``my number is 07700900123``), except a date or an SSN."""
    return 7 <= _phone_digits(value) <= 15 and not _NOT_A_PHONE.fullmatch(value)


# The words of a field name that holds a phone number (``phone``, ``phone_number``, ``contactPhone``, ``Mobile``).
PHONE_KEY_WORDS = {"phone", "telephone", "mobile", "cell", "cellphone", "fax", "tel", "whatsapp", "landline"}
_KEY_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+")


def _phone_key(key: Any) -> bool:
    return isinstance(key, str) and any(w.lower() in PHONE_KEY_WORDS for w in _KEY_WORD.findall(key[:200]))


def _phone_value(value: Any) -> bool:
    """A JSON value under a phone field that is a phone number: a string of the cue rule's number shape, or a whole
    number, of 7 to 15 digits (``"07700900123"``, ``2125550142``)."""
    if isinstance(value, str):
        text = value.strip()
        return len(text) <= 40 and _PHONE_VALUE.fullmatch(text) is not None and _check_cued_phone(text, None)
    raw = value.raw if isinstance(value, _Num) else value if isinstance(value, int) and not isinstance(
        value, bool) else None
    return raw is not None and str(raw).isdigit() and 7 <= len(str(raw)) <= 15


def _check_user(value: str, m: "re.Match[str]") -> bool:
    return value.lower() not in COMMON_USER_WORDS


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _card_network(digits: str) -> bool:
    """A prefix and length some card network issues: Visa, Mastercard, Amex, Diners, JCB, Discover, UnionPay and
    Maestro."""
    n, two, four = len(digits), digits[:2], digits[:4]
    if digits[0] == "4":
        return n in (13, 16, 19)
    if two in ("34", "37"):
        return n == 15
    if two in ("30", "36", "38", "39"):
        return 14 <= n <= 19
    if two == "35":
        return 16 <= n <= 19
    if "51" <= two <= "55" or "2221" <= four <= "2720":
        return n == 16
    if digits[0] == "6" or two in ("50", "56", "57", "58"):
        return 13 <= n <= 19
    return False


def _check_card(value: str, m: "Optional[re.Match[str]]") -> bool:
    """A payment card number: a network's prefix and length, the Luhn check digit, and either no separator or one
    kind (space or hyphen) between groups of 4 to 6 digits (the last group 3 to 6), as cards are printed."""
    digits = "".join(ch for ch in value if "0" <= ch <= "9")
    if not (13 <= len(digits) <= 19 and _card_network(digits) and _luhn(digits)):
        return False
    seps = {ch for ch in value if not ("0" <= ch <= "9")}
    if not seps:
        return True
    if len(seps) > 1:
        return False
    groups = value.split(seps.pop())
    return len(groups[0]) == 4 and all(4 <= len(g) <= 6 for g in groups[1:-1]) and 3 <= len(groups[-1]) <= 6


_DIGITS = re.compile(r"[0-9]+")


def _card_spans(run: str) -> List[Tuple[int, int]]:
    """The payment card numbers in ``run``, a run of digit groups joined by single spaces or hyphens: windows of 1 to
    5 groups that ``_check_card`` accepts, longest first, left to right. A window of several groups is not a card
    when a neighbouring group of 4 or more digits continues it with the same separator (a list of numbers), so
    "4111 1111 1111 1111 12/29" holds a card and "4111 1111 1111 1111 1111 1111" does not."""
    groups = [(g.start(), g.end()) for g in _DIGITS.finditer(run)]
    sizes = [end - start for start, end in groups]
    seps = [run[end] for _start_, end in groups[:-1]]
    found: List[Tuple[int, int]] = []
    i, n = 0, len(groups)
    while i < n:
        taken = 0
        for k in range(min(5, n - i), 0, -1):
            if not 13 <= sum(sizes[i:i + k]) <= 19 or (k > 1 and len(set(seps[i:i + k - 1])) != 1):
                continue
            start, end = groups[i][0], groups[i + k - 1][1]
            if not _check_card(run[start:end], None):
                continue
            if k > 1:
                sep = seps[i]
                before = i > 0 and seps[i - 1] == sep and sizes[i - 1] >= 4
                after = i + k < n and seps[i + k - 1] == sep and sizes[i + k] >= 4
                if before or after:
                    continue
            found.append((start, end))
            taken = k
            break
        i += taken or 1
    return found


class _CardRule(Rule):
    """``payment_card``: finds runs of digit groups, then the card numbers inside each run (``_card_spans``). A
    regex check cannot do this: its veto of a whole run cannot try the shorter windows inside it."""

    def __init__(self, pattern: "re.Pattern[str]") -> None:
        super().__init__("payment_card", pattern)

    def spans(self, text: str) -> List[Tuple[int, int]]:
        out = []
        for m in self.rx.finditer(text):
            if len(m.group(0)) >= 13:
                out.extend((m.start() + a, m.start() + b) for a, b in _card_spans(m.group(0)))
        return out

    def apply(self, text: str, counts: Counter) -> str:
        hits = self.spans(text)
        if not hits:
            return text
        out, last = [], 0
        for start, end in hits:
            out.append(text[last:start])
            out.append(MARK % self.kind)
            counts[self.kind] += 1
            last = end
        out.append(text[last:])
        return "".join(out)


def _check_ssn(value: str, m: "re.Match[str]") -> bool:
    """A US SSN (area not 000, 666 or 9xx; group not 00; serial not 0000) or an ITIN (9xx, group 50 to 65, 70 to
    88, 90 to 92 or 94 to 99)."""
    digits = "".join(ch for ch in value if "0" <= ch <= "9")
    if len(digits) != 9:
        return False
    area, group, serial = digits[:3], int(digits[3:5]), digits[5:]
    if serial == "0000" or group == 0:
        return False
    if area[0] == "9":
        return 50 <= group <= 65 or 70 <= group <= 88 or 90 <= group <= 92 or 94 <= group <= 99
    return area not in ("000", "666")


def _check_id_number(value: str, m: "re.Match[str]") -> bool:
    """A passport, license, insurance or id number after its name: 5 or more digits, not a placeholder."""
    digits = sum(1 for ch in value if "0" <= ch <= "9")
    return digits >= 5 and not _is_placeholder(value.replace(" ", ""))


# what a password "is" in prose that describes it: "4-digit", "12chars", "2FA-protected", "case-sensitive"
_SECRET_DESCRIPTION = re.compile(
    r"(?i)[0-9]+[ -]?(?:digits?|characters?|chars?|letters?|numbers?|words?|long|factor|fa|steps?)"
    r"|.*-(?:protected|enabled|based|secured|encrypted|only|free|compliant|required|sensitive|insensitive|long)"
)


def _check_spoken_secret(value: str, m: "re.Match[str]") -> bool:
    """"The wifi password is Garden2026!Shed": a value with a digit, either 4 to 12 digits (a PIN, a door code) or 6
    or more characters that hold a letter too. Words ("is required", "is case-sensitive"), descriptions ("is
    4-digit", "was 2FA-protected"), paths, links and placeholders are prose."""
    if _is_placeholder(value) or value.startswith(("/", "~", "./", "../")) or "://" in value:
        return False
    if _SECRET_DESCRIPTION.fullmatch(value):
        return False
    if value.isdigit():
        return 4 <= len(value) <= 12
    return _has_digit(value) and len(value) >= 6 and any(ch.isalpha() for ch in value)


# rules ---------------------------------------------------------------------------------------------------------
_start = secrets.start  # a token boundary that also counts "\\n", "\\t", "%3D" and similar escapes
_SEP = r"(\\?[\"']?[ \t]*[:=][ \t]*\\?[\"']?)"  # ": ", "=", "\": \"" (JSON inside a string)
# A credential name: a whole run of name characters ending in a credential word (the run starts where the name
# characters start, so a run of "a.a.a..." or "----" is tried once, not from every "." or "-" in it), or an
# upper-case ``*_KEY`` word.
_CRED_WORDS = (r"api[_-]?key|access[_-]?key|secret[_-]?key|private[_-]?key|auth[_-]?key|client[_-]?secret|secret"
               r"|token|password|passwd|pwd|credentials?")
_CRED_NAME = r"((?:%s(?i:[A-Za-z0-9_.-]*(?:%s)))|(?:%s[A-Z0-9_]*_KEY))(?![A-Za-z0-9_])" % (
    _start("A-Za-z0-9_.-"), _CRED_WORDS, _start("A-Za-z0-9_"))
# An e-mail local part starts after a character that cannot be in an address, after a backslash that opens no
# escape, or right after a backslash escape, so "\nsomeone@..." keeps its "\n" at any depth of encoding. Not after
# a %XX escape: "%" is a local-part character, so an encoded address is matched from the start of its run, and a
# start after every %XX of a long run would retry the whole run each time.
_EMAIL_START = "(?:%s)" % "|".join(
    (r"(?<![A-Za-z0-9._%+\\-])", r"(?<=\\)(?![ntrfbv]|u[0-9A-Fa-f]{4}|x[0-9A-Fa-f]{2})")
    + tuple(alt for alt in secrets.ESCAPE_LOOKBEHINDS if "%" not in alt)
)

PRIVATE_KEY_BLOCK = Rule(
    "private_key",
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----(?:.*?-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|.*)", re.S),
    hints=("PRIVATE KEY",),
)
# A key pasted in pieces: a later piece holds body lines and the END line only.
PRIVATE_KEY_TAIL = Rule(
    "private_key",
    re.compile(r"\A(?:(?!-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----).)*?-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----", re.S),
    hints=("PRIVATE KEY",),
)
# ...and a middle piece holds body lines only: two or more base64 lines of PEM width (a line-number prefix is
# allowed), plus a shorter last line.
_B64_PREFIX = r"[ \t]*(?:[0-9]+(?:\u2192|\t)[ \t]?)?"
KEY_BLOCK = Rule(
    "key_block",
    re.compile(
        r"(?m)^%s[A-Za-z0-9+/]{60,76}={0,2}[ \t]*(?:\r?\n%s[A-Za-z0-9+/]{60,76}={0,2}[ \t]*)+"
        r"(?:\r?\n%s[A-Za-z0-9+/]{4,59}={0,2}[ \t]*$)?" % (_B64_PREFIX, _B64_PREFIX, _B64_PREFIX)
    ),
    0,
    _check_key_block,
    hints=("\n",),
)
# A hit also takes the rest of its token, so a key longer than its pattern leaves no tail behind.
SECRET_RULES = [
    Rule(kind, re.compile(rx.pattern + r"[A-Za-z0-9_-]*"), hints=secrets.HINTS[kind])
    for kind, rx in secrets.TEXT_PATTERNS
    if kind != "private_key"
]
# Token formats the release scan does not list yet: (kind, regex source, hints). A kind that ``secrets`` lists under
# the same name is left out here, so a pattern moved there is not applied twice.
EXTRA_TOKENS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("huggingface", r"hf_" + secrets.edge("A-Za-z0-9_", 3) + r"[A-Za-z0-9]{30,}", ("hf_",)),
    ("digitalocean", r"do[opr]_v1_" + secrets.edge("A-Za-z0-9_", 7) + r"[A-Za-z0-9]{32,}",
     ("dop_v1_", "doo_v1_", "dor_v1_")),
)
SECRET_RULES += [
    Rule(kind, re.compile(source + r"[A-Za-z0-9_-]*"), hints=hints)
    for kind, source, hints in EXTRA_TOKENS
    if kind not in secrets.KINDS
]
CREDENTIAL_PATTERN = re.compile(_CRED_NAME + _SEP + r"([^\s\"'`\\,;()\[\]{}<>]+)")
CREDENTIAL = Rule("credential", CREDENTIAL_PATTERN, 3, _check_record_credential, hints=("=", ":"))
CREDENTIAL_RULES = [
    Rule(
        "url_credentials",
        re.compile(r"\b([a-z][a-z0-9+.-]*://)([^/\s:@\"'<>]+):([^/\s@\"'<>]+)@"),
        3,
        _check_value,
        hints=("://",),
    ),
    Rule(
        "auth_header",
        re.compile(
            r"(?i)"
            + _start("A-Za-z0-9_")
            + r"((?:proxy-)?authorization|x-api-key|api-key|x-auth-token|x-access-token|private-token)"
            + _SEP
            + r"(?:(?:bearer|basic|token|digest|apikey|key)[ \t]+)?([^\s\"'\\,;]+)"
        ),
        3,
        _check_header,
    ),
    Rule(
        "cookie",
        re.compile(r"(?i)" + _start("A-Za-z0-9_") + r"((?:set-)?cookie)([ \t]*:[ \t]*)([^\r\n\"'\\]+)"),
        3,
        _check_cookie,
        hints=("ookie", "OOKIE"),
    ),
    Rule(
        "bearer",
        re.compile(_start("A-Za-z0-9") + r"([Bb]earer[ \t]+)([A-Za-z0-9._~+/-]{12,}=*)"),
        2,
        _check_bearer,
        hints=("earer",),
    ),
    Rule(
        "basic",
        re.compile(_start("A-Za-z0-9") + r"(Basic[ \t]+)([A-Za-z0-9+/]{8,}={0,2})(?![A-Za-z0-9+/=])"),
        2,
        _check_basic,
        hints=("Basic",),
    ),
    Rule(
        "url_token",
        re.compile(
            r"((?:[?&;]|\\u0026|&amp;)((?i:[A-Za-z0-9_.-]*(?:token|key|sig|signature|secret|password|passwd"
            r"|auth|credential))|X-Amz-[A-Za-z-]+|X-Goog-[A-Za-z-]+)=)([^&#\s\"'<>\\]+)"
        ),
        3,
        _check_query,
        hints=("=",),
    ),
    CREDENTIAL,
    # curl -u user:pass, --user user:pass (and -uuser:pass)
    Rule(
        "credential",
        re.compile(r"(?<![A-Za-z0-9_-])(?:-u[ \t]*|--user(?:[ \t]+|=))[\"']?[^\s:\"'\\]+:([^\s\"'\\]+)"),
        1,
        _check_user_pass,
        hints=("-u",),
    ),
    # .netrc: machine <host> login <user> password <secret>, on one line or several
    Rule(
        "credential",
        re.compile(
            r"(?<![A-Za-z0-9_])(?:machine[ \t]+\S+|default)\s+login\s+\S+(?:\s+account\s+\S+)?\s+password\s+"
            r"([^\s\"'\\]+)"
        ),
        1,
        _check_value,
        hints=("password",),
    ),
    # docker's config.json printed as text: "auth": "<base64 of user:password>"
    Rule(
        "credential",
        re.compile(r"\\?[\"']auth\\?[\"'][ \t]*:[ \t]*\\?[\"']([A-Za-z0-9+/]{6,}={0,2})(?![A-Za-z0-9+/=])"),
        1,
        _check_auth_value,
        hints=("auth",),
    ),
    # .pgpass: host:port:database:user:password, a whole line
    Rule(
        "credential",
        re.compile(r"(?m)^[ \t]*(?=[^:\s]*[A-Za-z.*])[^:\s]+:(?:[0-9]{1,5}|\*):[^:\s]+:[^:\s]+:([^\s]+?)[ \t]*$"),
        1,
        _check_value,
        hints=(":",),
    ),
    # mysql -u root -pSECRET (a bare -p prompts for the password and holds none); the option sits within 300
    # characters of the command, which keeps a line of many "mysql" words linear
    Rule(
        "credential",
        re.compile(
            r"(?<![A-Za-z0-9_-])(?:mysql|mysqladmin|mysqldump|mariadb)(?![A-Za-z0-9_-])[^\n|;&]{0,300}?[ \t]-p"
            r"([^\s\"'\\-][^\s\"'\\]*)"
        ),
        1,
        _check_value,
        hints=("-p",),
    ),
    # sshpass -p SECRET (and -pSECRET): its own options come before the command it runs, so an ssh "-p 2222" after
    # the command is a port
    Rule(
        "credential",
        re.compile(
            r"(?<![A-Za-z0-9_-])sshpass(?:[ \t]+-(?:[vVh]|e[A-Za-z0-9_]*|[fdP][ \t]*[^\s-][^\s]*))*[ \t]+-p[ \t]*"
            r"[\"']?([^\s\"'\\-][^\s\"'\\]*)"
        ),
        1,
        _check_value,
        hints=("sshpass",),
    ),
    Rule(
        "credential",
        re.compile(r"(?<![A-Za-z0-9_])SSHPASS[ \t]*=[ \t]*[\"']?([^\s\"'\\;&|]+)"),
        1,
        _check_value,
        hints=("SSHPASS",),
    ),
    # a secret said in prose: "the wifi password is Garden2026", "the gate code was 4821", "PIN: 4821"
    Rule(
        "credential",
        re.compile(
            r"(?i)(?<![A-Za-z0-9_-])(pass(?:word|wd|phrase|code)|pin(?:[ \t]+(?:code|number))?"
            r"|(?:door|gate|alarm|lock|keypad|entry|access)[ \t]+code)"
            r"(?:[ \t]+(?:for|to|of|on)(?:[ \t]+[A-Za-z0-9_'-]+){1,3}?)?"
            r"(?:[ \t]*[:=][ \t]*|[ \t]+(?:is|was|has[ \t]+been)[ \t]+(?:changed|set|reset)[ \t]+to[ \t]+"
            r"|[ \t]+(?:is|was)(?:[ \t]+now)?[ \t]+)"
            r"[\"'\u201c\u2018]?([^\s\"'`<>\u201d\u2019]*[^\s\"'`<>\u201d\u2019.,;:)\]}])"
        ),
        2,
        _check_spoken_secret,
    ),
]
# A numbered street line: "12 Elm Street", "221B Garden Lane", "7 N. Main St."; and PO boxes. Capitalized words
# only, so "12 bags per lane" in prose is kept. "Row" is not a street type here: "Plant 4 Tomato Row" is a garden.
_STREET_TYPES = (
    r"(?:Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Drive|Dr|Boulevard|Blvd|Court|Ct|Place|Pl|Terrace|Way|Parkway|Pkwy"
    r"|Highway|Hwy|Square|Sq|Circle|Cir|Crescent|Close|Alley)"
)
_ID_SEP = r"[ \t]*(?:[:#=-][ \t]*|(?:is|was)[ \t]+)?"
# A word that says a phone number follows: "phone", "tel.", "mobile no.", "cell", "fax", "WhatsApp", "my number",
# "office line"; "call", "ring", "text" or "dial" right before the number; or "call me on", "reach Lee at",
# "contact the office on" (a pronoun or up to 3 capitalized names). Then ":", "=", "#", "-", "is", "on" or "at".
# "Contact: ..." alone is not a cue: what follows may be anything.
_PHONE_OBJECT = (
    r"[ \t]+(?:(?i:me|us|him|her|them|the[ \t]+(?:office|desk))|[A-Z][A-Za-z'-]+(?:[ \t]+[A-Z][A-Za-z'-]+){0,2})"
    r"[ \t]+(?i:on|at)"
)
_PHONE_CUE = (
    r"(?:(?i:(?:tele)?phone|tel\.?|mob(?:ile)?|cell(?:[ \t]?phone)?|fax|whats[ \t]?app|sms|landline"
    r"|(?:my|his|her|their|your|our|home|work|office|direct|contact|emergency)[ \t]+(?:number|no\.|line))"
    r"(?:[ \t]*(?i:number|no\.|nr\.|#))?"
    r"|(?i:call|ring|text|dial)(?:%s)?|(?i:reach|contact|message)%s)"
    r"[ \t]*(?:[:=#-][ \t]*(?:\r?\n[ \t]*)?)?(?:(?i:is|was|on|at)[ \t]+)?"
) % (_PHONE_OBJECT, _PHONE_OBJECT)
# The number after a cue: a "+" or "00" prefix, an area code in brackets, a "(0)" trunk, groups joined by one space,
# dot, hyphen or slash; not a window of a longer run of numbers.
_PHONE_SHAPE = (
    r"(?:\+|00)?(?:\([0-9]{1,5}\)[ ./-]?)?[0-9]{1,15}(?:[ ./-]?\(0\)[ ./-]?[0-9]{1,12}|[ ./-][0-9]{1,12}){0,6}"
)
_PHONE_NUMBER = r"(%s)(?![A-Za-z0-9_]|[ ./-]?[0-9])" % _PHONE_SHAPE
_PHONE_VALUE = re.compile(_PHONE_SHAPE)
# A field name that holds a phone number, then ":" or "=" ("phone_number=", "contactPhone:", "\"MOBILE_1\": \""): up
# to 3 words before the phone word, joined by "_", "." or "-" or in camelCase, and up to 3 after it.
_PHONE_FIELD = (
    r"(?:[A-Za-z0-9]+[_.-]){0,3}(?:[a-z][a-z0-9]*(?=[A-Z]))?"
    r"(?i:telephone|phone|mobile|cellphone|cell|fax|tel|whatsapp|landline)"
    r"(?:[_.-][A-Za-z0-9]+|[A-Z][a-z]*|[0-9]+){0,3}[\"']?[ \t]*[:=][ \t]*[\"']?"
)
PERSONAL_RULES = [
    # a payment card number, bare or in groups: a whole run of digit groups joined by single spaces or hyphens,
    # not glued to a word, a "+" (a phone) or a decimal point, then the card-shaped windows inside it
    _CardRule(
        re.compile(
            _start("A-Za-z0-9_+") + r"(?<![0-9][ -])(?<![0-9]\.)[0-9]+(?:[ -][0-9]+)*(?![A-Za-z0-9_]|\.[0-9])"
        )
    ),
    # a US SSN or ITIN written 123-45-6789, anywhere
    Rule(
        "government_id",
        re.compile(
            _start("A-Za-z0-9_+") + r"(?<![0-9][-.])([0-9]{3}-[0-9]{2}-[0-9]{4})(?![A-Za-z0-9_]|[-.][0-9])"
        ),
        1,
        _check_ssn,
        hints=("-",),
    ),
    # ...or 9 digits after its name ("SSN 123456789", "social security no. 123 45 6789")
    Rule(
        "government_id",
        re.compile(
            r"(?i)(?<![A-Za-z0-9_])(?:ssn|ss#|social[ \t]+security(?:[ \t]+(?:number|num|no\.?|#))?"
            r"|tax(?:payer)?[ \t]+id(?:entification)?(?:[ \t]+(?:number|num|no\.?|#))?|itin)"
            + _ID_SEP
            + r"([0-9]{3}([ .-]?)[0-9]{2}\2[0-9]{4})(?![A-Za-z0-9_]|[.-][0-9])"
        ),
        1,
        _check_ssn,
    ),
    # a passport, driver's license, national insurance or national id number after its name
    Rule(
        "government_id",
        re.compile(
            r"(?i)(?<![A-Za-z0-9_])(?:passport|driver'?s?[ \t]+licen[cs]e|driving[ \t]+licen[cs]e"
            r"|national[ \t]+insurance|nino|ni(?=[ \t]+(?:number|no))|national[ \t]+id(?:entity)?(?:[ \t]+card)?)"
            r"(?:[ \t]+(?:number|num|no\.?|#))?"
            + _ID_SEP
            + r"((?:[A-Za-z]{2}(?:[ \t]?[0-9]{2}){3}[ \t]?[A-Da-d])|[A-Za-z0-9]{6,12})(?![A-Za-z0-9_])"
        ),
        1,
        _check_id_number,
    ),
    # the login of a .netrc entry (its password is a credential above)
    Rule(
        "name",
        re.compile(
            r"(?<![A-Za-z0-9_])(?:machine[ \t]+\S+|default)\s+login\s+([^\s\"'\\]+)"
            r"(?=(?:\s+account\s+\S+)?\s+password\s)"
        ),
        1,
        _check_user,
        hints=("password",),
    ),
    Rule(
        "email",
        re.compile(
            _EMAIL_START + r"([A-Za-z0-9._%+-]+)(?:@|%40)([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24})"
            r"(?![A-Za-z0-9-])"
        ),
        0,
        _check_email,
        hints=("@", "%40"),
    ),
    Rule(
        "phone",
        re.compile(
            _start("A-Za-z0-9_+.-")
            + r"(?:\+?1[ .-]?)?(?:\([2-9][0-9]{2}\)[ ]?|[2-9][0-9]{2}[ .-])[2-9][0-9]{2}[ .-][0-9]{4}"
            r"(?![A-Za-z0-9_-])"
        ),
    ),
    Rule("phone", re.compile(_start("A-Za-z0-9_+") + r"\+[1-9][0-9]{7,14}(?![A-Za-z0-9_])"), hints=("+",)),
    # "+44 7700 900123", "+49 151 23456789", "+91 98765 43210", "+44 (0)20 7946 0958", "+49 (30) 1234567"
    Rule(
        "phone",
        re.compile(
            _start("A-Za-z0-9_+") + r"\+[1-9][0-9]{0,2}(?:[ .-]?\([0-9]{1,5}\)[ .-]?[0-9]{1,12}|[ .-][0-9]{1,12})"
            r"(?:[ .-][0-9]{1,12}){0,5}(?![A-Za-z0-9_])"
        ),
        0,
        _check_intl_phone,
        hints=("+",),
    ),
    # a national number with its trunk 0, in groups: "07700 900123", "020 7946 0958", "06 12 34 56 78",
    # "0412 345 678", "030/1234567", "(02) 9876 5432"; not a window of a longer list of numbers
    Rule(
        "phone",
        re.compile(
            _start("A-Za-z0-9_+./-")
            + r"(?:\(0[1-9][0-9]{0,4}\)[ ]?|(?<![0-9][ .,/-])0[1-9][0-9]{0,4}[ ./-])[0-9]{2,8}"
            r"(?:[ ./-][0-9]{2,8}){0,3}(?![A-Za-z0-9_]|[ .,/-]?[0-9])"
        ),
        0,
        _check_national_phone,
        hints=("0",),
    ),
    # any other number after a phone word: "my number is 07700900123", "mobile: 98765 43210", "tel. 0044 20 7946
    # 0958", "call me on 612 34 56 78", "reach Lee on 0412345678", "ring 2125550142"
    Rule("phone", re.compile(_start("A-Za-z0-9_") + _PHONE_CUE + _PHONE_NUMBER), 1, _check_cued_phone),
    # ...and after a field name: "phone_number=07700900123" (a table row), "contactPhone: 9876543210"
    Rule("phone", re.compile(_start("A-Za-z0-9_.-") + _PHONE_FIELD + _PHONE_NUMBER), 1, _check_cued_phone),
    Rule(
        "address",
        re.compile(
            _start("A-Za-z0-9_.-") + r"[0-9]{1,6}[A-Z]?(?:-[0-9]{1,4})?[ \t]+(?:[NSEW]\.?[ \t]+)?"
            r"(?:[A-Z][A-Za-z'-]*[ \t]+){1,3}" + _STREET_TYPES + r"\.?(?![A-Za-z0-9])"
        ),
    ),
    Rule(
        "address",
        re.compile(r"(?i)" + _start("A-Za-z0-9") + r"P\.?[ \t]?O\.?[ \t]+Box[ \t]+[0-9]{1,8}(?![0-9])"),
        hints=("Box", "BOX", "box"),
    ),
    # the user name in a home-directory path, keeping the path readable
    Rule(
        "name",
        re.compile(_start("A-Za-z0-9_.-") + r"(?:/Users/|/home/)([^/\s\"'\\:<>\[\]]+)"),
        1,
        _check_user,
        hints=("/Users/", "/home/"),
    ),
    Rule(
        "name",
        re.compile(_start("A-Za-z0-9") + r"[A-Za-z]:(?:\\{1,2})Users(?:\\{1,2})([^\\/\s\"':<>\[\]]+)"),
        1,
        _check_user,
        hints=("Users",),
    ),
]
# Every kind a rule writes as ``[redacted:<kind>]``; a redaction count is keyed by these names.
RULE_KINDS = frozenset(
    [r.kind for r in [PRIVATE_KEY_BLOCK, PRIVATE_KEY_TAIL, KEY_BLOCK] + SECRET_RULES + CREDENTIAL_RULES
     + PERSONAL_RULES]
    + [kind for kind, _ in secrets.TEXT_PATTERNS]
)


# policy --------------------------------------------------------------------------------------------------------
def personal_policy(policy: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """``{kind: keep|redact|refuse}`` for every personal kind: the topic's ``policy.personal`` over the defaults. An
    unknown action refuses (the most careful reading)."""
    given = (policy or {}).get("personal") if isinstance(policy, dict) else None
    out = dict(DEFAULT_PERSONAL)
    if isinstance(given, dict):
        for kind, action in given.items():
            out[str(kind)] = action if action in ACTIONS else "refuse"
    return out


# the redactor --------------------------------------------------------------------------------------------------
_PERCENT = re.compile(r"%[0-9A-Fa-f]{2}")


def _percent_decode(text: str) -> Tuple[str, List[Tuple[int, int]]]:
    """``text`` with its ``%XX`` escapes decoded as UTF-8, and for each decoded character its span in ``text``."""
    out: List[str] = []
    spans: List[Tuple[int, int]] = []
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    start = -1  # where the pending (incomplete) UTF-8 sequence began, or -1

    i, n = 0, len(text)
    while i <= n:
        escape = i < n and text[i] == "%" and _PERCENT.match(text, i) is not None
        if not escape and start >= 0:  # an incomplete sequence ends here: decode it as U+FFFD
            for ch in decoder.decode(b"", final=True):
                out.append(ch)
                spans.append((start, i))
            decoder.reset()
            start = -1
        if i == n:
            break
        if escape:
            seq = start if start >= 0 else i
            chars = decoder.decode(bytes([int(text[i + 1: i + 3], 16)]))
            pending = bool(decoder.getstate()[0])
            for ch in chars:  # when this byte starts a new sequence, what came out ended the previous one
                out.append(ch)
                spans.append((seq, i if pending else i + 3))
            start = (i if chars else seq) if pending else -1
            i += 3
        else:
            out.append(text[i])
            spans.append((i, i + 1))
            i += 1
    return "".join(out), spans


DUP_SUFFIX = " (dup)"


class _Pairs(object):
    """``object_pairs_hook`` that keeps every copy of a repeated key (``json.loads`` keeps only the last, and a
    value it drops would pass unread): later copies become ``"<key> (dup)"``. ``repeated`` says whether any object
    had one."""

    def __init__(self) -> None:
        self.repeated = False

    def __call__(self, pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        out = dict(pairs)
        if len(out) == len(pairs):
            return out
        self.repeated = True
        out = {}
        for key, val in pairs:
            while key in out:
                key += DUP_SUFFIX
            out[key] = val
        return out


class _Num(object):
    """A JSON number or constant as spelled in the text (``2.50``, ``1e400``, ``NaN``): a rewritten line or
    document keeps every number it did not redact exactly as it was."""

    __slots__ = ("raw",)

    def __init__(self, raw: str) -> None:
        self.raw = raw

    def __repr__(self) -> str:
        return self.raw


def _loads(text: str, pairs: Optional[_Pairs] = None) -> Any:
    """``json.loads`` keeping number spellings (``_Num``) and, with ``pairs``, every copy of a repeated key."""
    return json.loads(text, object_pairs_hook=pairs, parse_float=_Num, parse_int=_Num, parse_constant=_Num)


def _json_str(value: str) -> str:
    out = json.dumps(value, ensure_ascii=False)
    try:
        out.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate from a \\ud83d escape: keep it escaped
        out = json.dumps(value, ensure_ascii=True)
    return out


def _encode(obj: Any, indent: Optional[int], item_sep: str, key_sep: str) -> str:
    """JSON text for a redacted value, written as ``json.dumps`` writes it with the same ``indent`` and separators,
    except that a ``_Num`` keeps its spelling."""
    parts: List[str] = []

    def put(value: Any, level: int) -> None:
        if isinstance(value, _Num):
            parts.append(value.raw)
        elif isinstance(value, str):
            parts.append(_json_str(value))
        elif isinstance(value, (dict, list)):
            is_dict = isinstance(value, dict)
            items = list(value.items()) if is_dict else list(value)
            if not items:
                parts.append("{}" if is_dict else "[]")
                return
            parts.append("{" if is_dict else "[")
            pad = "\n" + " " * (indent * (level + 1)) if indent is not None else ""
            for i, item in enumerate(items):
                if i:
                    parts.append(item_sep)
                parts.append(pad)
                if is_dict:
                    key, item = item
                    parts.append(_json_str(str(key)))
                    parts.append(key_sep)
                put(item, level + 1)
            if indent is not None:
                parts.append("\n" + " " * (indent * level))
            parts.append("}" if is_dict else "]")
        else:
            parts.append(json.dumps(value))  # null, true, false and numbers built by a caller

    put(obj, 0)
    return "".join(parts)


class Redactor(object):
    """Redacts strings and JSON values, counting every hit by kind.

    There is one mode (``record=True``, the careful one): a number is a secret only under a password, secret or
    key name, so redaction counts such as ``{"credential": 2}`` and settings such as ``{"max_tokens": 4096}`` stay;
    a bare 10-digit number is an id, not a phone; and a ``STRUCTURAL_KEYS`` setting whose value is an identifier is
    kept. ``policy`` decides the personal kinds: a ``keep`` kind is never looked for. ``unsettled`` turns true when
    a text still changed after ``MAX_PASSES`` passes."""

    def __init__(self, record: bool = True, policy: Optional[Dict[str, Any]] = None) -> None:
        if record is not True:
            raise UsageError("the redactor has one mode (record=True): write, validate and export must agree")
        self.record = True
        self.personal = personal_policy(policy)
        active = [r for r in PERSONAL_RULES if self.personal.get(r.kind, "redact") != "keep"]
        self.rules: List[Rule] = [PRIVATE_KEY_BLOCK, PRIVATE_KEY_TAIL, KEY_BLOCK] + SECRET_RULES + CREDENTIAL_RULES
        self.rules += active
        self.decoded_rules = active
        self.counts: Counter = Counter()
        self.unsettled = False

    # kinds ---------------------------------------------------------------------------------------------------
    def action(self, kind: str) -> str:
        """``redact`` or ``keep`` for a personal kind the policy allows, ``refuse`` for everything else."""
        if kind in PERSONAL_KINDS:
            return self.personal.get(kind, "redact")
        return "refuse"

    def refused(self) -> List[str]:
        """The kinds found so far that refuse the text: credentials, and personal kinds the policy refuses."""
        return sorted(k for k, n in self.counts.items() if n and self.action(k) == "refuse")

    def redactions(self) -> Dict[str, int]:
        """``{kind: count}`` of the personal data redacted so far."""
        return {k: int(n) for k, n in sorted(self.counts.items()) if n and self.action(k) == "redact"}

    def found(self) -> List[str]:
        return sorted(k for k, n in self.counts.items() if n)

    # strings and values ----------------------------------------------------------------------------------------
    def text(self, value: str) -> str:
        """``value`` with every rule applied, pass after pass, until a pass changes nothing (at most
        ``MAX_PASSES``): a redaction can open a token boundary for a rule that already ran."""
        for _pass in range(MAX_PASSES):
            before = value
            for rule in self.rules:
                value = rule.apply(value, self.counts)
            if "%" in value and _PERCENT.search(value):
                value = self._decoded_pass(value)
            if _FOLD_RE.search(value):
                value = self._folded_pass(value)
            if value == before:
                return value
        self.unsettled = True
        return value

    def _decoded_pass(self, value: str) -> str:
        """Run the personal rules on the percent-decoded copy of ``value``; each hit redacts the span of ``value``
        it was decoded from (``+1%20212%20555%200142`` goes whole)."""
        if not self.decoded_rules:
            return value
        decoded, spans = _percent_decode(value)
        hits: List[Tuple[int, int, str]] = []
        for rule in self.decoded_rules:
            if rule.hints and not any(h in decoded for h in rule.hints):
                continue
            for start, end in rule.spans(decoded):
                hits.append((spans[start][0], spans[end - 1][1], rule.kind))
        return self._replace(value, hits)

    def _folded_pass(self, value: str) -> str:
        """Run the personal rules on a copy of ``value`` with typographic separators and fullwidth forms folded to
        ASCII (``FOLD``: a no-break space, an en dash or a non-breaking hyphen inside a number, ``６１７``, ``＠``),
        one character for one, so each hit redacts the same span of ``value``. Text pasted from a word processor, a
        PDF or a web page writes numbers this way."""
        if not self.decoded_rules:
            return value
        folded = value.translate(FOLD)
        if folded == value:
            return value
        hits: List[Tuple[int, int, str]] = []
        for rule in self.decoded_rules:
            if rule.hints and not any(h in folded for h in rule.hints):
                continue
            hits.extend((start, end, rule.kind) for start, end in rule.spans(folded))
        return self._replace(value, hits)

    def _replace(self, value: str, hits: List[Tuple[int, int, str]]) -> str:
        """``value`` with each ``(start, end, kind)`` hit written as its mark (the later of two overlapping hits is
        dropped), counting each."""
        if not hits:
            return value
        out, last = [], len(value)
        for start, end, kind in sorted(hits, reverse=True):
            if end > last:  # overlaps a hit already taken
                continue
            out.append(value[end:last])
            out.append(MARK % kind)
            self.counts[kind] += 1
            last = start
        out.append(value[:last])
        return "".join(reversed(out))

    def value(self, obj: Any) -> Any:
        """A redacted copy of a parsed JSON value; keys are checked too. A number is a ``_Num`` (as parsed here) or
        a plain int or float. Nesting deeper than the recursion limit raises ``RecursionError``."""
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, list):
            items = []
            for v in obj:
                items.append(self.value(v))
            return items
        if isinstance(obj, dict):
            out: Dict[str, Any] = {}
            for key, val in obj.items():
                new_key = self.text(key) if isinstance(key, str) else key
                while new_key in out:  # two keys that redact to the same text
                    new_key += DUP_SUFFIX
                number = isinstance(val, _Num) or (isinstance(val, (int, float)) and not isinstance(val, bool))
                if (isinstance(val, str) and not _is_placeholder(val) and _sensitive_key(key)) or (
                        number and _numeric_secret_key(key)):
                    self.counts["credential"] += 1
                    out[new_key] = MARK % "credential"
                elif self.personal.get("phone", "redact") != "keep" and _phone_key(key) and _phone_value(val):
                    self.counts["phone"] += 1  # {"phone": "07700900123"}: the field name is the cue
                    out[new_key] = MARK % "phone"
                else:
                    out[new_key] = self.value(val)
            return out
        return obj

    # whole texts -----------------------------------------------------------------------------------------------
    def _total(self) -> int:
        return sum(self.counts.values())

    def _json(self, parsed: Any, indent: Optional[int], item_sep: str, key_sep: str) -> Optional[str]:
        """The redacted JSON text of a parsed value, or None when nothing in it was redacted. A value nested too
        deeply to walk raises ``RecursionError`` with the counts put back, so the caller can read it as text."""
        counts, unsettled = Counter(self.counts), self.unsettled
        try:
            redacted = self.value(parsed)
            if self._total() == sum(counts.values()):
                return None
            return _encode(redacted, indent, item_sep, key_sep)
        except RecursionError:
            self.counts, self.unsettled = counts, unsettled
            raise

    def scrub(self, text: str) -> str:
        """The redacted form of a whole text: one JSON document, or lines where each JSON line is read as JSON and
        each run of other lines is redacted as one piece (so a multi-line key block is found whole). Parts with
        nothing to redact keep their exact characters."""
        doc = _whole_json(text)
        if doc is not None:
            try:
                redacted = self._json(doc, 1, ",", ": ")
                # nothing redacted: every copy of a repeated key was read, so the text can stay as it is
                return text if redacted is None else redacted + "\n"
            except RecursionError:
                pass  # nested too deeply to walk: read it line by line, as text
        out: List[str] = []
        run: List[str] = []

        def flush() -> None:
            if run:
                out.append(self.text("".join(run)))
                del run[:]

        for line in text.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            ending = line[len(body):]
            stripped = body.strip()
            parsed: Any = None
            if stripped[:1] in ("{", "["):
                try:
                    parsed = _loads(stripped, _Pairs())
                except (ValueError, RecursionError):
                    parsed = None
            if not isinstance(parsed, (dict, list)):
                run.append(line)
                continue
            flush()
            seps = (", ", ": ") if '": ' in body else (",", ":")
            try:
                redacted = self._json(parsed, None, *seps)
            except RecursionError:
                run.append(line)  # nested too deeply to walk: redacted as text
                continue
            out.append(line if redacted is None else redacted + ending)  # nothing redacted: the exact characters
        flush()
        return "".join(out)


def _whole_json(text: str) -> Any:
    """The parsed document when ``text`` is one JSON object or array spread over several lines, else None. A first
    line that is a complete value makes the text JSONL, read line by line; so does JSON nested too deeply."""
    stripped = text.lstrip()
    if stripped[:1] not in ("{", "["):
        return None
    first = stripped.split("\n", 1)[0].strip()
    try:
        json.loads(first)
        return None  # the first line is a complete value: JSONL
    except (ValueError, RecursionError):
        pass
    try:
        doc = _loads(text, _Pairs())
    except (ValueError, RecursionError):
        return None
    return doc if isinstance(doc, (dict, list)) else None


# the contract --------------------------------------------------------------------------------------------------
def _refusal(kinds: Sequence[str]) -> Refused:
    creds = [k for k in kinds if k not in PERSONAL_KINDS]
    people = [k for k in kinds if k in PERSONAL_KINDS]
    parts = []
    if creds:
        parts.append("credentials (%s)" % ", ".join(creds))
    if people:
        parts.append("personal data the policy refuses (%s)" % ", ".join(people))
    return Refused("the text holds %s; nothing was stored. Remove it and try again" % " and ".join(parts),
                   problems=["holds %s" % k for k in kinds], kinds=list(kinds))


def sanitize_text(text: str, policy: Optional[Dict[str, Any]] = None) -> Tuple[str, Dict[str, int]]:
    """``(clean text, {kind: redactions})``. Raises ``Refused`` (with ``kinds``) when the text holds a credential
    or personal data the policy refuses, or data packed so tightly that ``MAX_PASSES`` passes do not settle it;
    nothing is returned then, so nothing can be stored. The clean text always passes ``check_text``."""
    if not isinstance(text, str):
        raise UsageError("sanitize_text needs a string")
    redactor = Redactor(policy=policy)
    clean = redactor.scrub(text)
    leftovers = sorted({kind for kind, _match in secrets.scan_str(clean)})
    refused = sorted(set(redactor.refused()) | set(leftovers))
    if refused:
        raise _refusal(refused)
    if redactor.unsettled:
        kinds = redactor.found()
        raise Refused("the text holds personal data packed too tightly to redact in %d passes (%s); nothing was "
                      "stored. Put spaces between the items and try again" % (MAX_PASSES, ", ".join(kinds)),
                      problems=["holds %s" % k for k in kinds], kinds=kinds)
    return clean, redactor.redactions()


def check_text(text: str, policy: Optional[Dict[str, Any]] = None) -> List[str]:
    """The kinds ``sanitize_text`` would redact or refuse in ``text`` (empty when it would store the text
    unchanged). ``validate`` reports them as P18; the rules and the policy are the ones ingest uses."""
    redactor = Redactor(policy=policy)
    redactor.scrub(text or "")
    kinds = set(redactor.found()) | {kind for kind, _match in secrets.scan_str(text or "")}
    return sorted(kinds)


def redact_file(src: str, dst: str, policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Sanitize the text file ``src`` into ``dst``: ``{src, dst, redactions, bytes_in, bytes_out, lines}``.

    The output goes to a temp file in ``dst``'s folder, is scanned again with the release patterns and is renamed
    into place only when that scan is clean. A credential (or a refused personal kind) raises ``Refused`` and
    writes nothing; so does a destination that is the source itself."""
    src_real, dst_real = os.path.realpath(src), os.path.realpath(dst)
    if src_real == dst_real:
        raise UsageError("refusing to overwrite the source %s" % src)
    with open(src, "rb") as fh:
        raw = fh.read()
    clean, redactions = sanitize_text(raw.decode("utf-8", "replace"), policy)
    data = clean.encode("utf-8")
    folder = os.path.dirname(dst_real) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(dst_real) + ".", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        with open(tmp, "rb") as fh:
            leftovers = sorted({kind for kind, _match in secrets.scan_bytes(fh.read())})
        if leftovers:
            raise Refused("the sanitized copy still matches %s; nothing was written" % ", ".join(leftovers),
                          kinds=leftovers)
        os.chmod(tmp, store.file_mode(dst_real))
        os.replace(tmp, dst_real)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {
        "src": src,
        "dst": dst,
        "redactions": redactions,
        "bytes_in": len(raw),
        "bytes_out": len(data),
        "lines": clean.count("\n"),
    }
