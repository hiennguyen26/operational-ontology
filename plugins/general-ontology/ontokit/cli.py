"""The ``onto`` command line, built from the command registry (``commands.COMMANDS``).

Every command takes ``--json``, ``--text`` or ``--compact`` (the default), ``--limit``, ``--offset`` and ``--repo``,
before or after the subcommand. Booleans are switches (``--full``, and ``--no-<name>`` to turn one off; help lists
``--no-<name>`` only for switches that are on by default). A list flag may repeat (``--scope a --scope b``); one
value is split at commas (``--kinds crop,plot``), except that ``id=label`` items split only where a comma starts the
next ``id=`` (``--options "roof=On the roof, wrapped,shed=In the shed"`` is two options), and a JSON list
(``'["a, b","c"]'``) is taken as it is. Object arguments take a JSON string or ``@file`` (NaN and Infinity are
refused: they are not JSON). ``onto ingest -`` reads the text from stdin, and ``onto ingest --body "..."`` takes it
inline (an argument named like a reserved flag takes the flag in ``CLI_NAMES``).

Output starts with the version line. Exit codes: 0 ok, 1 domain error, 2 usage error, 3 not built (and a
command's own code, such as 1 when ``validate`` finds problems or 2 when ``scan`` finds hits). Errors print the
version line and the message on stderr, or an error object on stdout under ``--json``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, TextIO

from . import __version__, commands, render, store, util
from .errors import OntoError, UsageError

JOINED = ("subject", "task", "text", "question")  # positionals that take several words
# ``id=label`` items: a comma splits only when the next item starts with its own ``id=``
LABELLED_RE = re.compile(r"^\s*[A-Za-z0-9][A-Za-z0-9_.:@/-]*=")
NEXT_LABELLED_RE = re.compile(r",(?=\s*[A-Za-z0-9][A-Za-z0-9_.:@/-]*=)")
RESERVED_FLAGS = ("json", "text", "compact", "repo", "limit", "offset")
# an argument whose name is a reserved flag takes this flag on the CLI (``onto ingest --body "..."``: ``--text``
# picks the full text output)
CLI_NAMES = {"text": "body"}
SUPPRESS = argparse.SUPPRESS


def _common(parser: argparse.ArgumentParser) -> None:
    """The flags every command takes, before or after the subcommand (``SUPPRESS`` keeps either position)."""
    parser.add_argument("--json", dest="fmt_json", action="store_true", default=SUPPRESS, help="print JSON")
    parser.add_argument("--text", dest="fmt_text", action="store_true", default=SUPPRESS,
                        help="print the full text form")
    parser.add_argument("--compact", dest="fmt_compact", action="store_true", default=SUPPRESS,
                        help="print compact text: ids plus one-line facts (the default)")
    parser.add_argument("--limit", type=int, default=SUPPRESS, help="items per page (0 = no cap)")
    parser.add_argument("--offset", type=int, default=SUPPRESS, help="skip this many items")
    parser.add_argument("--repo", default=SUPPRESS,
                        help="topic repo (default: $ONTO_REPO, then the working directory and its parents)")


def _flag(name: str) -> str:
    return "--" + name.replace("_", "-")


def _add_props(parser: argparse.ArgumentParser, cmd: commands.Command) -> None:
    for name in cmd.positional:
        schema = cmd.props.get(name) or {}
        kind = schema.get("type")
        required = name in cmd.required
        default = None if required else SUPPRESS
        if kind == "array":
            parser.add_argument(name, nargs="*", default=SUPPRESS, metavar=name, help=schema.get("description"))
        elif name in JOINED:
            parser.add_argument(name, nargs="+" if required else "*", default=default, metavar=name,
                                help=schema.get("description"))
        else:
            parser.add_argument(name, nargs=None if required else "?", default=default, metavar=name,
                                choices=schema.get("enum") if required else None, help=schema.get("description"))
    for name, schema in cmd.props.items():
        if name in RESERVED_FLAGS:
            if name in CLI_NAMES and name not in cmd.positional:
                parser.add_argument(_flag(CLI_NAMES[name]), dest=name, default=SUPPRESS,
                                    metavar=CLI_NAMES[name].upper(),
                                    help="%s; the %s argument (--%s picks the output form)"
                                    % (schema.get("description") or name, name, name))
            continue
        if name in cmd.positional and (name in cmd.required or schema.get("type") == "array"):
            continue  # an optional positional is also a flag, so follow-up calls may name it either way
        kind = schema.get("type")
        text = schema.get("description") or ""
        if kind == "array" and (schema.get("items") or {}).get("type") != "object":
            # repeatable, so no item is ever dropped by a second flag, and a label may hold commas
            parser.add_argument(_flag(name), dest=name, action="append", default=SUPPRESS,
                                metavar=name.upper(), help=text)
        elif kind == "boolean":
            parser.add_argument(_flag(name), dest=name, action="store_true", default=SUPPRESS, help=text)
            # every switch has its --no- form, so a follow-up call (render.call) can always spell False
            parser.add_argument("--no-" + name.replace("_", "-"), dest=name, action="store_false",
                                default=SUPPRESS,
                                help="turn %s off" % name if schema.get("default") is True else SUPPRESS)
        elif kind == "integer":
            parser.add_argument(_flag(name), dest=name, type=int, default=SUPPRESS, help=text)
        elif kind == "number":
            parser.add_argument(_flag(name), dest=name, type=float, default=SUPPRESS, help=text)
        else:
            parser.add_argument(_flag(name), dest=name, default=SUPPRESS, choices=schema.get("enum"),
                                metavar=name.upper() if not schema.get("enum") else None, help=text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="onto", description="Build and read a topic ontology. Every output starts with the version line.")
    parser.add_argument("--version", action="version", version="onto %s" % __version__)
    _common(parser)
    sub = parser.add_subparsers(dest="_command", metavar="<command>")
    for cmd in commands.COMMANDS:
        names = [(cmd.name, {})] + [(alias, preset) for alias, preset in sorted(cmd.aliases.items())]
        for name, preset in names:
            help_text = cmd.title if not preset else "%s (%s)" % (cmd.title, ", ".join(
                "%s=%s" % kv for kv in sorted(preset.items())))
            p = sub.add_parser(name, help=help_text, description=cmd.short)
            _add_props(p, cmd)
            _common(p)
            p.set_defaults(_cmd=cmd.name, _preset=dict(preset))
    return parser


def _read_text_file(path: str, what: str, stdin: Optional[TextIO]) -> str:
    """The text of ``path`` (``-`` reads stdin) without its final line break; ``UsageError`` naming ``what``."""
    if path == "-":
        return (stdin or sys.stdin).read().rstrip("\r\n")
    try:
        with open(os.path.expanduser(str(path)), encoding="utf-8") as fh:
            return fh.read().rstrip("\r\n")
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError("%s: cannot read %s: %s" % (what, path, getattr(exc, "strerror", None) or exc))


def _decide_text(args: Dict[str, Any], key: str, stdin: Optional[TextIO]) -> None:
    """``onto decide --<key>-file PATH`` reads that field from a file, so the user's words never pass through a
    shell. A ``--<key>`` of ``@<an existing file>`` is refused: decide stores its text as given, and the path
    would become the record."""
    flag = "--%s" % key.replace("_", "-")
    if "%s_file" % key in args:
        path = args.pop("%s_file" % key)
        if args.get(key):
            raise UsageError("onto decide: give %s or %s-file, not both" % (flag, flag))
        args[key] = _read_text_file(path, "onto decide %s-file" % flag, stdin)
        return
    value = args.get(key)
    if isinstance(value, str) and value.startswith("@") and os.path.isfile(os.path.expanduser(value[1:])):
        raise UsageError("onto decide %s %s: decide stores the text as given, not a file's contents; pass %s-file %s "
                         "to read the file" % (flag, value, flag, value[1:]))


def _checkpoint_lines(args: Dict[str, Any], key: str, stdin: Optional[TextIO]) -> None:
    """``onto log --checkpoint --<key>-file PATH`` reads that list from a file, one item per line (blank lines
    skipped, no comma splitting), so the user's words never pass through a shell: ``$``, backticks and quotes stay
    as typed."""
    flag = "--%s" % key.replace("_", "-")
    if "%s_file" % key not in args:
        return
    path = args.pop("%s_file" % key)
    if args.get(key):
        raise UsageError("onto log: give %s or %s-file, not both" % (flag, flag))
    text = _read_text_file(path, "onto log %s-file" % flag, stdin)
    args[key] = [line.strip() for line in text.splitlines() if line.strip()]


def _caller_relative(ns: Dict[str, Any]) -> None:
    """``onto setup --answers @file`` with a relative path names a file in the folder the user started in: the
    new-topic launchers cd to the kit checkout first and pass that folder in ``ONTO_SETUP_CWD``."""
    value = ns.get("answers")
    base = os.environ.get("ONTO_SETUP_CWD") or ""
    if not (isinstance(value, str) and value.startswith("@")) or not (os.path.isabs(base) and os.path.isdir(base)):
        return
    path = os.path.expanduser(value[1:])
    if path and not os.path.isabs(path):
        ns["answers"] = "@" + os.path.join(base, path)


def _load_json(value: Any, what: str) -> Any:
    if not isinstance(value, str):
        return value
    text = value
    if value.startswith("@"):
        path = os.path.expanduser(value[1:])
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            raise UsageError("%s: cannot read %s: %s" % (what, path, exc.strerror or exc))
    try:
        return util.loads_strict(text)
    except ValueError as exc:
        raise UsageError("%s: not valid JSON (%s); give a JSON string or @file" % (what, exc))


def split_list(value: str) -> List[str]:
    """One list flag value as items (see the module docstring): a JSON list as it is; ``id=label`` items split only
    where a comma starts the next ``id=``; anything else at every comma."""
    text = str(value)
    if text.lstrip().startswith("["):
        try:
            loaded = util.loads_strict(text)
        except ValueError:
            loaded = None
        if isinstance(loaded, list) and all(isinstance(x, str) for x in loaded):
            return [x.strip() for x in loaded if x.strip()]
    parts = NEXT_LABELLED_RE.split(text) if LABELLED_RE.match(text) else text.split(",")
    return [part.strip() for part in parts if part.strip()]


def _convert(cmd: commands.Command, name: str, value: Any) -> Any:
    schema = cmd.props.get(name) or {}
    kind = schema.get("type")
    if name in cmd.positional and name in JOINED and isinstance(value, list):
        return " ".join(value) if value else None
    if kind == "array":
        items = (schema.get("items") or {}).get("type")
        if items == "object":
            loaded = _load_json(value, name)
            if not isinstance(loaded, list):
                raise UsageError("%s must be a JSON list" % name)
            return loaded
        if isinstance(value, list) and name in cmd.positional:
            return value  # positional words, one item each
        values = value if isinstance(value, list) else [value]
        return [item for each in values for item in split_list(str(each))]
    if kind == "object":
        loaded = _load_json(value, name)
        if not isinstance(loaded, dict):
            raise UsageError("%s must be a JSON object" % name)
        return loaded
    return value


def _args(cmd: commands.Command, ns: Dict[str, Any], stdin: Optional[TextIO]) -> Dict[str, Any]:
    args: Dict[str, Any] = {}
    for name in list(cmd.props) + list(commands.COMMON_ARGS):
        if name in ns and ns[name] is not None:
            args[name] = _convert(cmd, name, ns[name])
    for key, value in (ns.get("_preset") or {}).items():
        args.setdefault(key, value)
    if cmd.name == "answer" and "text_file" in args:
        # the user's words from a file (or stdin), never through a shell: $, backticks and quotes stay as typed
        path = args.pop("text_file")
        if args.get("text"):
            raise UsageError("onto answer: give the answer text or --text-file, not both")
        if path == "-":
            text = (stdin or sys.stdin).read()
        else:
            try:
                with open(os.path.expanduser(str(path)), encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError) as exc:
                raise UsageError("onto answer --text-file: cannot read %s: %s" % (path, getattr(exc, "strerror", None)
                                                                                    or exc))
        args["text"] = text.rstrip("\r\n")
    if cmd.name == "decide":
        for key in ("chosen_text", "rationale"):
            _decide_text(args, key, stdin)
    if cmd.name == "log":
        for key in ("done", "next", "open_questions"):
            _checkpoint_lines(args, key, stdin)
    if cmd.name == "ingest" and args.get("path") == "-":
        args.pop("path")
        args["text"] = (stdin or sys.stdin).read()
    elif cmd.name == "ingest" and ns.get("fmt_text") and isinstance(args.get("path"), str) and "text" not in args:
        path = os.path.expanduser(args["path"])
        bases = [os.getcwd()] + ([ns["repo"]] if isinstance(ns.get("repo"), str) else [])
        if not any(os.path.exists(os.path.join(base, path)) for base in bases):
            # never echo the words: they may hold what the credential check was meant to refuse
            raise UsageError("onto ingest: --text picks the full text output, so the words after it were read as a "
                             "path. Pass the text with --body \"...\", or on stdin: onto ingest - --title T < file")
    return args


def _write(stream: TextIO, text: str) -> None:
    stream.write(text if text.endswith("\n") else text + "\n")


def main(argv: Optional[Sequence[str]] = None, stdout: Optional[TextIO] = None, stderr: Optional[TextIO] = None,
         stdin: Optional[TextIO] = None) -> int:
    """Run one command; returns the exit code. Streams are injected; ``sys.stdout`` and ``sys.stderr`` are swapped
    only while argparse runs (it prints help and usage errors itself)."""
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = build_parser()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout, stderr
    try:
        parsed = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    finally:
        sys.stdout, sys.stderr = saved
    ns = vars(parsed)
    name = ns.get("_cmd")
    if not name:
        parser.print_help(stdout)
        return 0
    cmd = commands.get(name)
    fmt = "json" if ns.get("fmt_json") else ("text" if ns.get("fmt_text") else "compact")
    if not store.is_placeholder(ns.get("repo")):
        # absolute once, from the working directory: doctor and setup join --repo onto a start folder again
        ns["repo"] = store.resolve(str(ns["repo"]))
    ctx = commands.Context(mcp=False, profile="cli", explicit=ns.get("repo"))
    if cmd.name == "setup":
        _caller_relative(ns)
    try:
        args = _args(cmd, ns, stdin)
    except OntoError as exc:
        return _emit_error(ctx, exc.to_json(), exc.code, commands.error_text(exc), fmt, stdout, stderr)
    if cmd.name == "setup" and isinstance(ns.get("answers"), str) and ns["answers"].startswith("@"):
        # setup moves a consumed .onto/setup.json aside after a successful run, so it needs the file's path
        ctx.answers_path = os.path.abspath(os.path.expanduser(ns["answers"][1:]))
    try:
        text, is_error, obj = commands.dispatch(cmd, args, ctx, fmt)
    except Exception as exc:  # a kit bug: say so plainly; ONTO_DEBUG shows the traceback
        if os.environ.get("ONTO_DEBUG"):
            raise
        message = "internal error in onto %s: %s: %s" % (cmd.name, type(exc).__name__, exc)
        return _emit_error(ctx, {"error": "internal", "message": message}, 1, message, fmt, stdout, stderr)
    code = int(obj.get("exit_code") or 0)
    if "error" in obj and code:
        if fmt == "json":
            _write(stdout, text)
        else:
            _write(stderr, text)
        return code
    _write(stdout, text)
    if is_error and not code:
        return 1
    # start Claude Code once the checklist is printed; setup sets exec only when nothing but the plugin install failed
    if obj.get("exec") and cmd.name == "setup":
        from . import onboard

        stdout.flush()
        launched = onboard.launch(obj["exec"])
        if launched is not None:  # Windows: Claude Code ran as a child; its exit code is the command's
            return launched
    return code


def _emit_error(ctx: commands.Context, obj: Dict[str, Any], code: int, message: str, fmt: str, stdout: TextIO,
                stderr: TextIO) -> int:
    try:
        stamp = ctx.stamp()
    except Exception:  # the error must still reach the user
        stamp = None
    if fmt == "json":
        _write(stdout, json.dumps(dict(obj, exit_code=code, version=stamp), sort_keys=True, ensure_ascii=False,
                                  indent=1, default=str))
    else:
        _write(stderr, render.version_line(stamp) + "\n" + message)
    return code


def run(argv: List[str]) -> int:  # pragma: no cover - thin wrapper for ``python -m ontokit``
    return main(argv)
