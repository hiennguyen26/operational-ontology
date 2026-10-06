"""Repo-local kit handoff: the topic repo's own kit serves its data.

A topic repo made from the template carries the kit it was written with (``plugins/general-ontology/``). The kit
installed as a Claude Code plugin can be older or newer. ``maybe_handoff`` runs first in the three launchers
(``bin/onto``, ``bin/onto-mcp`` and ``bin/onto-session-start``): when a topic repo is discovered (``--repo`` in the
arguments, then ``$ONTO_REPO``, then the working directory and ``$CLAUDE_PROJECT_DIR``, walking up) and it holds
``plugins/general-ontology/ontokit/__init__.py`` whose real path differs from the running kit, the process is
replaced (``os.execv``; on Windows, a child process on the same streams whose exit code is returned) by that repo's
``bin/<entry>`` with the same arguments and ``ONTO_HANDOFF=1`` set, so the repo's kit never hands off again. It logs one line on stderr and never writes to stdout.

Three cases adjust that rule:

- ``onto init`` (typed by a person or a skill, never started with the session) is served by the kit that will
  serve the new topic: the one vendored in the folder the topic is created in (``--path``, else the working
  directory), such as a checkout of the template (``plugins/general-ontology/ontokit/`` and no
  ``ontology.json``). So ``ontology.json`` records that kit's version, and later commands, handed off to the same
  kit, report no version skew. Discovery is not used for ``init``.
- The session-start hook hands off only to a topic whose ``ontology.json`` passes its schema
  (``hook.checked_manifest``), since it prints nothing anywhere else. Like ``onto-mcp``, which also starts with the
  session, it never runs code from a folder that is not a topic (a template checkout included).
- ``onto setup`` and ``onto doctor`` look for the topic from ``--repo`` or the working directory only (as
  ``doctor.locate`` does), never ``$ONTO_REPO`` or ``$CLAUDE_PROJECT_DIR``: they act on the folder they run in.

Nothing happens when no repo is found, the repo has no kit of its own, the kit is this one, the launcher is
missing or leaves the repo (a symlink), or ``ONTO_HANDOFF`` is already set. Any failure falls back to the
running kit.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from . import store

GUARD = "ONTO_HANDOFF"
KIT_REL = "plugins/general-ontology"
HOOK_ENTRY = "onto-session-start"
ENTRIES = ("onto", "onto-mcp", HOOK_ENTRY)
RUNNING_KIT = os.path.dirname(os.path.realpath(__file__))
_windows: Callable[[], bool] = lambda: os.name == "nt"  # seams the tests swap
_call: Callable[..., int] = subprocess.call
VALUE_OPTIONS = ("--repo", "--limit", "--offset")  # the options before an onto subcommand that take a value
LOCAL_COMMANDS = ("setup", "doctor")  # found from --repo or the working directory only (doctor.locate)


def _repo_arg(argv: Sequence[str]) -> Optional[str]:
    """The value of ``--repo PATH`` or ``--repo=PATH`` in the arguments, else None."""
    args = list(argv)
    for i, arg in enumerate(args):
        if arg == "--":
            break
        if arg == "--repo" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--repo="):
            return arg[len("--repo="):]
    return None


def _is_path_flag(arg: str) -> bool:
    """``--path`` or an abbreviation argparse accepts for it (``--p``, ``--pa``, ``--pat``: no other option of
    ``init`` starts with p)."""
    name = arg.split("=", 1)[0]
    return len(name) >= 3 and "--path".startswith(name)


def _subcommand(argv: Sequence[str]) -> Tuple[Optional[str], int]:
    """``(name, index)`` of the onto subcommand in the arguments (the first word that is not an option or an
    option's value), or ``(None, -1)``."""
    args = list(argv)
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return None, -1
        if arg in VALUE_OPTIONS:
            i += 2
        elif arg.startswith("-"):
            i += 1
        else:
            return arg, i
    return None, -1


def _init_folder(argv: Sequence[str], cwd: Optional[str]) -> Optional[str]:
    """For ``onto [options] init ...``: the folder the topic will be created in, resolved the way ``cmd_init``
    resolves it (``--path``, else the working directory); None for any other command."""
    args = list(argv)
    name, i = _subcommand(args)
    if name != "init":
        return None
    folder: Optional[str] = None
    rest = args[i + 1:]
    for j, arg in enumerate(rest):
        if arg == "--":
            break
        if arg.startswith("--") and _is_path_flag(arg):
            if "=" in arg:
                folder = arg.split("=", 1)[1]
            elif j + 1 < len(rest):
                folder = rest[j + 1]
    base = cwd or os.getcwd()
    return os.path.abspath(os.path.join(base, os.path.expanduser(folder)) if folder else base)


def _launcher(root: str, entry: str) -> Optional[str]:
    """``<root>/plugins/general-ontology/bin/<entry>`` when ``root`` vendors a kit other than the running one and
    both that kit and the launcher stay inside ``root``, else None."""
    marker = os.path.join(root, *KIT_REL.split("/"), "ontokit", "__init__.py")
    if not os.path.isfile(marker):
        return None
    their_kit = os.path.dirname(os.path.realpath(marker))
    if their_kit == RUNNING_KIT:
        return None
    launcher = os.path.join(root, *KIT_REL.split("/"), "bin", entry)
    if not os.path.isfile(launcher) or not store.inside(root, launcher) or not store.inside(root, their_kit):
        return None
    return launcher


def target(argv: Sequence[str], entry: str, cwd: Optional[str] = None,
           env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The repo launcher to hand off to, or None (see the module docstring)."""
    env = os.environ if env is None else env
    if entry not in ENTRIES or env.get(GUARD):
        return None
    if entry == "onto":
        folder = _init_folder(argv, cwd)
        if folder is not None:
            return _launcher(folder, entry)
    if entry == "onto" and _subcommand(argv)[0] in LOCAL_COMMANDS:
        # setup and doctor act on the folder they run in (or --repo), never on a topic $ONTO_REPO names elsewhere
        env = {k: v for k, v in env.items() if k not in (store.ENV_VAR, store.PROJECT_ENV)}
    try:
        root = store.find_root(_repo_arg(argv), cwd, env)
    except Exception:  # a bad --repo or $ONTO_REPO is the running kit's error to report
        return None
    if not root:
        return None
    if entry == HOOK_ENTRY:
        from . import hook

        if hook.checked_manifest(root) is None:  # the hook prints nothing here, so none of the repo's code runs
            return None
    return _launcher(root, entry)


def maybe_handoff(argv: Sequence[str], entry: str, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None,
                  execv: Optional[Callable[..., Any]] = None, stderr: Any = None) -> None:
    """Replace this process by the topic repo's own ``bin/<entry>`` when that kit differs from the running one
    (``entry`` is one of ``ENTRIES``). Returns only when there is nothing to hand off to."""
    environ = os.environ if env is None else env
    launcher = target(argv, entry, cwd, environ)
    if not launcher:
        return None
    err = stderr if stderr is not None else sys.stderr
    try:
        err.write("[onto] handing off to the kit in %s\n" % os.path.dirname(os.path.dirname(launcher)))
        err.flush()
    except (OSError, ValueError, AttributeError):
        pass
    environ[GUARD] = "1"
    exe = sys.executable or "python3"
    if execv is None and _windows():
        # os.exec* on Windows starts a new process and ends this one at once: Claude Code would see its MCP server
        # or hook exit, and the exit code would be lost. There the repo's launcher runs as a child on the same
        # standard streams, and its exit code is this process's.
        try:
            code = _call([exe, launcher] + list(argv), env=dict(environ))
        except OSError as exc:
            try:
                err.write("[onto] handoff failed (%s); using the running kit\n" % exc)
            except (OSError, ValueError, AttributeError):
                pass
            return None
        sys.exit(int(code))
    run = execv if execv is not None else os.execv
    try:
        run(exe, [exe, launcher] + list(argv))
    except OSError as exc:  # the running kit carries on
        try:
            err.write("[onto] handoff failed (%s); using the running kit\n" % exc)
        except (OSError, ValueError, AttributeError):
            pass
    return None
