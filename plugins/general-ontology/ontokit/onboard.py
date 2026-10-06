"""``onto setup``: turn a template clone into a ready topic, or make a new topic folder from a template checkout.

Every step checks first and reports ``done``, ``already``, ``skipped`` (with the reason) or ``failed``, so a second
run only does what is missing. The steps, in order:

1. ``preflight``: python and git present, with a name and an email the user set; the target folder is not inside a
   cloud-synced folder (unless the user confirms: a prompt, or ``--cloud-ok``) and is absent or empty (or a clone
   an earlier run made); files the OS writes into a folder it shows (``OS_JUNK``) count as empty.
2. ``clone`` (``--new`` only): ``git clone --single-branch --no-tags -b general-ontology <template checkout> DIR``,
   from the local checkout, so no network is needed. The source is never a topic (its general-ontology branch stays
   at the kit it was made with), never holds an older kit than the running one, and never holds the target.
3. ``branch``: ``git checkout -b main`` (a ``--here`` clone on another branch gets its own ``main`` too, with no
   upstream), ``git remote rename origin kit`` (or ``set-url kit``) pointing at the kit URL, and
   ``git remote add origin URL`` for ``--origin``. Nothing is ever pushed.
4. ``init``: the vendored kit's ``onto init`` (as ``handoff`` serves ``init``), with ``--personal`` when given.
5. ``packs``: ``--packs`` names turned on through ``onto pack add`` (``add_builtin_packs``: one logged ``pack``
   change each; unknown names are refused).
6. ``answers``: ``--answers`` replayed (each answer through ``onto answer --apply`` in the user's words, each
   decision through ``onto decide``), then setup's own choices (location, remote, personal data, packs, plugin
   mode) recorded as decisions by the user, only when they came from the user: a typed prompt answer, or a flag
   passed together with ``--answers`` (the agent's setup interview hands its answers over that way). A scripted
   ``--yes`` call with flags and no answers file records nothing, and neither do defaults.
7. ``commit``: only when ``inbox/`` and ``.onto/`` are ignored. The first commit ("Start <name>") takes only the
   topic's own files (``ontology.json``, the topic folders, the git rules init wrote); any other uncommitted file
   in the folder is left out and named ("not committed: N files setup did not write"). A later run commits only
   the files it wrote ("Record the setup of <name>"), and none when other files were uncommitted before it ran.
   Nothing is committed while a file to commit holds secret-like or credential-like text; a rerun commits that
   file once the user took the text out.
8. ``plugin``: ``project`` (default) runs ``claude plugin marketplace add`` and ``claude plugin install`` with
   ``--scope project``, or writes ``.claude/settings.json`` itself when ``claude`` is not on ``PATH``, then commits
   it; ``local`` uses ``--scope local`` (or ``settings.local.json``, which stays out of git); ``plugin-dir`` installs
   nothing; ``skip`` does nothing. A kit URL that is a local folder is never made the marketplace: setup falls
   back to ``plugin-dir``, says why, and does not record the asked mode; so does a ``general-ontology`` marketplace
   ``claude`` lists from a folder. Without ``--plugin`` the topic's recorded setup decision applies; in a committed
   topic with none, the wiring is left as it is. A failed install names its fixes and leaves the topic ready to
   run with ``--plugin-dir`` (the launch and the ``next`` lines use it).
9. ``launch``: with a terminal, ``claude`` on ``PATH`` and ``--launch auto``, the result carries ``exec`` and the CLI
   starts ``claude "Start the ontology"`` in the folder once the checklist is printed; otherwise the ``next`` lines
   say what to open.

Without ``--new`` or ``--here``: in a template checkout still on the template branch, a terminal asks "new folder
or here?" and ``--yes`` picks ``--new ~/Ontologies/<name>``; in a finished topic only the missing steps run. When
that default folder already holds a finished topic (or other files), the name moves on to ``<name>-2`` (and so on),
so a second one-click run makes a new topic; an explicit ``--new DIR`` or ``--name`` never moves.
Every value is checked (``mutate._check_new_topic``, the pack names, every value of the answers file: its types,
the question ids, each choice against its options, and its text through the credential scan, the personal-data
policy and the control-character rules; the remote URLs) before anything is written. A password or token, a space,
a line break or a leading ``-`` in ``--kit-url`` or ``--origin`` is refused; a token in the template's own remote is
left out of everything setup writes. A relative ``--new`` folder is read from ``ONTO_SETUP_CWD`` (the launchers'
starting folder); a name typed at the folder prompt goes under ``~/Ontologies``, and a prompt answer that is not a
valid name, ns or folder is asked again. ``setup.skipped`` in the answers file: a skipped question 3 takes the
default folder, and skipped questions 4 to 7 are recorded as ``skipped`` decisions. After a run with no failed
step, a ``.onto/setup.json`` it read is moved to ``.onto/setup.<name>.done.json`` (redacted under a policy that does
not keep personal data).
When the target already holds ``ontology.json``, its name, ns and title win: a ``--name``, ``--ns`` or ``--title``
that differs is refused, and a ``--personal`` that differs from the topic's policy is reported as not applied.
"""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__, agents, doctor, gitutil, handoff, sources, store, util
from .commands import Context
from .errors import Refused, UsageError

TEMPLATE_BRANCH = "general-ontology"
HOME_FOLDER = "Ontologies"
START_PROMPT = "Start the ontology"
PLUGIN_DIR_REL = "./plugins/general-ontology"
MARKETPLACE = doctor.MARKETPLACE
PLUGIN_KEY = doctor.PLUGIN_KEY
PLUGIN_MODES = ("project", "local", "plugin-dir", "skip")
STEPS = ("preflight", "clone", "branch", "init", "packs", "answers", "commit", "plugin", "agents", "launch")
CLAUDE_TIMEOUT = 300
SCOPE = ["setup"]
GITHUB_RE = re.compile(r"^(?:https?://github\.com/|ssh://git@github\.com/|git@github\.com:)"
                       r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
SHORTHAND_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")
HOST_PATH_RE = re.compile(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?::[0-9]+)?/\S+$")  # github.com/owner/repo
PASSWORD_RE = doctor.PASSWORD_RE
SLUG_DROP = re.compile(r"[^a-z0-9-]+")
REDACTED_RE = re.compile(r"\[redacted:[a-z_]+\]")  # the sanitizer's placeholder; a slug leaves it out
CALLER_CWD = "ONTO_SETUP_CWD"  # the folder new-topic was started from (it cds to the kit checkout first)
GOAL_Q = "q.frame.goal"
GOAL_NAME_MAX = 80
QUOTE_MAX = 300  # records.schema.json: a provenance quote
SUMMARY_MAX = 600  # records.schema.json: a node summary
THIN_Q = "q.gap.thin"
SETUP_KEYS = ("title", "summary", "name", "ns", "new", "here", "origin", "personal", "packs", "plugin", "cloud_ok",
              "skipped", "agent")
SETUP_QUESTIONS = 7  # the setup interview's questions, numbered 1 to 7 (AGENTS.md, section 1)

Q_LOCATION = "Where should it live?"
Q_REMOTE = "Will anyone else work on it, and where is it backed up?"
Q_PERSONAL = "How sensitive is what you will feed it?"
Q_PACKS = "Which extra built-in packs should it use?"
Q_PLUGIN = doctor.Q_PLUGIN
Q_AGENTS = doctor.Q_AGENTS  # question 7; the plugin question (7a) follows only for Claude Code
# the setup questions a skip is recorded for (setup.skipped), so the topic's first session does not ask them again
SKIP_QUESTIONS = {4: Q_REMOTE, 5: Q_PERSONAL, 6: Q_PACKS, 7: Q_PLUGIN}
SKIPPED = "skipped"
SKIPPED_TEXT = "skipped in the setup interview; the default applies"
OS_JUNK = (".DS_Store", "Thumbs.db", "desktop.ini")  # files the OS writes into a folder it shows
URL_BAD_RE = re.compile(r"[\s\x00-\x1f\x7f-\x9f]")

# seams the tests swap: prompts, the terminal check, the final exec, and the Windows check and call
_input: Callable[[str], str] = lambda prompt: (sys.stderr.write(prompt), sys.stderr.flush(),
                                               sys.stdin.readline())[2]
_interactive: Callable[[], bool] = lambda: bool(sys.stdin and sys.stdin.isatty() and sys.stdout.isatty())
_execvp: Callable[..., Any] = os.execvp
_windows: Callable[[], bool] = lambda: os.name == "nt"
_call: Callable[..., int] = subprocess.call


# names ---------------------------------------------------------------------------------------------------------
def slug(text: str, limit: int = 64) -> str:
    """A lower case slug of letters, digits and ``-`` (at most ``limit``, cut at a ``-`` when it can be)."""
    if not (text or "").strip():
        return ""
    s = SLUG_DROP.sub("-", util.slugify(text)).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if len(s) > limit:
        cut = s[:limit]
        s = cut[:cut.rfind("-")] if "-" in cut else cut
    return s.strip("-")


def derive_ns(name: str) -> str:
    """A namespace from a slug: starts with a letter, at most 32, not ``self`` or ``imp``."""
    from . import ids

    ns = slug(re.sub(r"^[^a-z]+", "", name or ""), 32) or "topic"
    if ns in ids.RESERVED_NS:
        ns = ns + "-topic"
    return ns


def clean_title(title: str, policy: Optional[Dict[str, Any]] = None) -> str:
    """``title`` as ``init`` stores it under ``policy`` (``mutate._clean_title``: a credential refuses it, personal
    data is redacted or refused as the policy says)."""
    from . import mutate

    return mutate._clean_title(title, policy)


def title_from(name: str) -> str:
    words = (name or "my-topic").replace("-", " ").strip()
    return words[:1].upper() + words[1:]


def short_path(path: str, env: Optional[Dict[str, str]] = None) -> str:
    """``path`` with the home folder written as ``~`` (for decisions and messages that may be shared)."""
    home = doctor.home_dir(env)
    if path == home or path.startswith(home.rstrip(os.sep) + os.sep):
        return "~" + path[len(home.rstrip(os.sep)):]
    return path


def display_url(url: Optional[str]) -> str:
    """A remote URL without user or password (``git@host:x`` -> ``host:x``)."""
    text = doctor.safe_url(url or "")
    return re.sub(r"^[^/@\s:]+@([^:/\s]+):", r"\1:", text)


def remote_text(url: Optional[str]) -> str:
    """How a remote is named in a decision that may be shared: a URL without its user part, or, for a folder on
    this machine, only its last name (the folders above it can name a person or a machine)."""
    if url and url.strip() and doctor.local_source(url):
        return "a folder named %s" % (re.split(r"[/\\]", url.strip().rstrip("/\\"))[-1] or "the remote")
    return display_url(url)


def _cut_words(words: str, limit: int) -> str:
    """``words`` cut at the last space within ``limit`` characters (a prefix of ``words``, so a quote cut this way
    still occurs in the text)."""
    if len(words) <= limit:
        return words
    cut = words[:limit]
    return (cut[:cut.rfind(" ")] if " " in cut else cut).rstrip()


def goal_name(words: str) -> str:
    """A short name for a goal answer: the whole answer when it fits ``GOAL_NAME_MAX``, else its first sentence,
    else the text up to the last clause break (a comma, a semicolon, a colon or a dash) that leaves at least 24
    characters, else the words that fit followed by ``...``. Never a cut that drops the object of a clause without
    saying so."""
    words = words.strip()
    if len(words) <= GOAL_NAME_MAX:
        return words.rstrip(" .;:,") or words
    first = re.split(r"(?<=[.!?])\s+", words, maxsplit=1)[0].rstrip(" .!?;:,")
    if 0 < len(first) <= GOAL_NAME_MAX:
        return first
    head = first[:GOAL_NAME_MAX + 1]
    breaks = [m.start() for m in re.finditer(r"\s*(?:[,;:]|\s-\s)", head) if m.start() >= 24]
    if breaks:
        return head[:breaks[-1]].rstrip(" .;:,")
    return _cut_words(first, GOAL_NAME_MAX - 3).rstrip(" .;:,") + "..."


def _before_redaction(words: str) -> str:
    """``words`` up to the clause that holds the first ``[redacted:<kind>]`` mark (cut at the last ``.``, ``;``,
    ``:`` or ``,`` before it), so a goal's name and id never keep the words around a redaction. The whole text when
    it holds no mark or the mark is in its first clause."""
    at = words.find("[redacted:")
    if at < 0:
        return words
    head = words[:at]
    cut = max(head.rfind(c) for c in ".;:,")
    return head[:cut].strip() if cut > 0 and head[:cut].strip() else words


FILLER_RE = re.compile(r"^(?:(?:like|um+|erm*|uh+|well|so|okay|ok|i guess|i think|i suppose|you know|basically|"
                       r"honestly|mostly|just)(?=[\s,.;:])[\s,.;:]*)+", re.IGNORECASE)


def _drop_filler(words: str) -> str:
    """``words`` without the spoken fillers it starts with (``Like``, ``Erm``, ``I guess``), first letter in upper
    case; the words themselves when nothing else is left."""
    rest = FILLER_RE.sub("", words).strip()
    if not rest or rest == words:
        return words
    return rest[:1].upper() + rest[1:]


def goal_ops(text: str, ns: str, name: Optional[str] = None) -> List[Dict[str, Any]]:
    """The ops that record the setup interview's goal answer as a stated ``goal`` node, linked ``part_of`` the
    topic's root node, so the goal ranks the questions near it from the first session. The name is ``goal_name``;
    the summary keeps the whole answer up to the 600 characters a summary holds, and the quote is the whole answer,
    or past the 300 characters a quote holds its first sentence, or its first 300 characters cut at a word, so it
    still occurs in the stored answer."""
    words = " ".join(str(text or "").split())
    name = " ".join(str(name).split()) if name and str(name).strip() else (
        goal_name(_drop_filler(_before_redaction(words))) or words[:GOAL_NAME_MAX])
    first = re.split(r"(?<=[.!?])\s+", words, maxsplit=1)[0]
    quote = [{"quote": words if len(words) <= QUOTE_MAX else first if len(first) <= QUOTE_MAX else
              _cut_words(words, QUOTE_MAX)}]
    summary = _cut_words(words, SUMMARY_MAX)
    return [{"op": "add_node", "ref": "$goal", "basis": "stated",
             "node": {"kind": "goal", "name": name, "summary": summary}, "prov": quote},
            {"op": "add_edge", "basis": "stated", "edge": {"src": "$goal", "rel": "part_of", "dst": "topic:%s" % ns},
             "prov": quote}]


def summary_ops(text: str, ns: str) -> List[Dict[str, Any]]:
    """The op that writes the setup interview's first answer (what the topic is about, in a sentence) into the
    summary of the root node ``topic:<ns>``, quoting it, as an answer to that node's gap question
    ``q.gap.thin@topic:<ns>``. The root node is confirmed, so the answer needs ``--confirm``."""
    words = " ".join(str(text or "").split())
    return [{"op": "update_node", "id": "topic:%s" % ns, "basis": "stated", "set": {"summary": _cut_words(
        words, SUMMARY_MAX)}, "reason": "The user said what the topic is about in the setup interview.",
             "prov": [{"quote": _cut_words(words, QUOTE_MAX)}]}]


def marketplace_source(url: str) -> Dict[str, Any]:
    """The ``extraKnownMarketplaces`` source for a kit URL: GitHub (a GitHub URL or ``owner/repo``) or git."""
    found = GITHUB_RE.match(url) or (SHORTHAND_RE.match(url) if not doctor.local_source(url) else None)
    if found:
        repo = found.group(2)[:-4] if found.group(2).endswith(".git") else found.group(2)
        return {"source": "github", "repo": "%s/%s" % (found.group(1), repo), "ref": TEMPLATE_BRANCH}
    return {"source": "git", "url": url, "ref": TEMPLATE_BRANCH}


def _version_key(version: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"[0-9]+", str(version or ""))[:3])


def git_url(url: Optional[str]) -> Optional[str]:
    """The URL git can fetch for a kit URL: ``owner/repo`` (GitHub shorthand) becomes
    ``https://github.com/owner/repo.git``, which git would otherwise read as a local path."""
    if url and SHORTHAND_RE.match(url) and not doctor.local_source(url):
        return "https://github.com/%s.git" % url[:-4] if url.endswith(".git") else "https://github.com/%s.git" % url
    return url


def url_problem(url: Any) -> Optional[str]:
    """Why a remote URL from a flag is refused before git or claude sees it, or None: whitespace or a control
    character (a line break would forge lines in the checklist and in ``.claude/settings.json``), or a leading
    ``-`` (git and claude would read it as an option)."""
    text = str(url or "")
    if URL_BAD_RE.search(text):
        return "holds a space, a line break or a control character"
    if text.startswith("-"):
        return "starts with '-', which git would read as an option"
    return None


# packs ---------------------------------------------------------------------------------------------------------
def available_packs(kit_root: str) -> List[str]:
    """The built-in packs of the kit vendored in ``kit_root`` (falls back to the running kit)."""
    from . import packs

    folder = os.path.join(kit_root, *doctor.KIT_REL, "ontokit", "packs")
    if not os.path.isdir(folder):
        folder = packs.BUILTIN_DIR
    return sorted(n[: -len(".pack.json")] for n in os.listdir(folder) if n.endswith(".pack.json"))


def check_packs(names: List[str], kit_root: str) -> List[str]:
    """``names`` without duplicates; ``UsageError`` naming the available packs for an unknown one."""
    have = available_packs(kit_root)
    out: List[str] = []
    for name in names:
        name = str(name).strip()
        if not name or name in out:
            continue
        if name not in have or name == "local":
            raise UsageError("unknown pack %r; the built-in packs are: %s" % (name, ", ".join(have)),
                             available=have)
        out.append(name)
    return out


def add_builtin_packs(root: str, names: List[str]) -> List[str]:
    """Turn on built-in packs with ``onto pack add`` (``mutate.add_pack``, served by the kit vendored in ``root``
    as ``run_onto`` does), so each one is one logged ``pack`` change with its history point, listed before
    ``local``. Returns the names added; a pack already on is not. ``UsageError`` with the kit's message when one
    is refused. The names are checked by ``check_packs`` first."""
    added: List[str] = []
    for name in names:
        obj = run_onto(root, ["pack", "add", name, "--json", "--repo=%s" % root])
        if obj.get("exit_code"):
            raise UsageError("onto pack add %s failed: %s" % (name, _message(obj)))
        if obj.get("added"):
            added.append(name)
    return added


def identity_at(target: str) -> Optional[str]:
    """``gitutil.identity_problem`` for commits in a repo at ``target``, which may not exist yet. Outside a repo an
    identity set only for some folders (``includeIf "gitdir:~/Ontologies/"``) does not apply, so when git cannot
    name the author there, a throwaway repo at ``target`` is asked (made and removed again, with any folders made
    for it); the answer is the one the new topic's commits get."""
    if os.path.exists(os.path.join(target, ".git")):
        return gitutil.identity_problem(target)
    problem = gitutil.identity_problem(os.path.abspath(os.sep))
    if problem is None:
        return None
    missing: List[str] = []
    path = target
    while path and not os.path.exists(path) and os.path.dirname(path) != path:
        missing.append(path)
        path = os.path.dirname(path)
    if not os.path.isdir(path) or not os.access(path, os.W_OK):
        return problem
    made: List[str] = []
    probe = os.path.join(target, ".git")
    try:
        for folder in reversed(missing):
            os.mkdir(folder)
            made.append(folder)
        if not gitutil.git_ok(target, "init", "-q")[0]:
            return problem
        return gitutil.identity_problem(target)
    except OSError:
        return problem
    finally:
        if made:
            shutil.rmtree(made[0], True)
        elif os.path.isdir(probe):
            shutil.rmtree(probe, True)


def branch_worktree(top: str, branch: str) -> Optional[str]:
    """The other worktree of the repo at ``top`` that has ``branch`` checked out (``git worktree list``), or None.
    git refuses to move a branch that another worktree has checked out."""
    here = os.path.realpath(top)
    path = None
    for line in gitutil.git(top, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == "branch refs/heads/%s" % branch and path and os.path.realpath(path) != here:
            return path
    return None


def _move_branch_fix(top: str, commit: str) -> str:
    """The command that moves the template branch of ``top`` to ``commit``: ``git branch -f`` there, or, when
    another worktree has the branch checked out (git refuses ``branch -f`` then), a fast-forward in that worktree."""
    other = branch_worktree(top, TEMPLATE_BRANCH)
    if other:
        if commit == "HEAD":  # HEAD in the other worktree is another commit: name this checkout's
            commit = gitutil.git(top, "rev-parse", "--short", "HEAD") or commit
        return "git -C %s merge --ff-only %s (%s is checked out in that worktree, so git branch -f cannot move it)" % (
            doctor.shell_quote(other), commit, TEMPLATE_BRANCH)
    return "git -C %s branch -f %s %s" % (doctor.shell_quote(top), TEMPLATE_BRANCH, commit)


# the answers file ----------------------------------------------------------------------------------------------
def _text_or_absent(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _check_setup_object(value: Any) -> Dict[str, Any]:
    """The ``setup`` object of the answers file: the setup interview's answers that map to flags (questions 1, 3,
    5, 6 and 7, and 4's URL), so a stopped interview resumes from the file. Flags on the command line win."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise UsageError("--answers: setup must be an object of setup flags (%s)" % ", ".join(SETUP_KEYS))
    extra = sorted(set(value) - set(SETUP_KEYS))
    if extra:
        raise UsageError("--answers: setup: unknown key %s; accepted: %s" % (", ".join(extra), ", ".join(SETUP_KEYS)))
    out: Dict[str, Any] = {}
    for key, item in sorted(value.items()):
        if item is None:
            continue
        if key in ("here", "cloud_ok"):
            if not isinstance(item, bool):
                raise UsageError("--answers: setup.%s must be true or false" % key)
        elif key == "skipped":
            # the setup questions the user skipped, so a resumed interview does not ask them again; no flag
            if not isinstance(item, list) or not all(isinstance(n, int) and not isinstance(n, bool) and
                                                     1 <= n <= SETUP_QUESTIONS for n in item):
                raise UsageError("--answers: setup.skipped must be a list of setup question numbers (1 to %d)"
                                 % SETUP_QUESTIONS)
            item = sorted(set(item))
        elif key == "packs":
            if not isinstance(item, list) or not all(isinstance(p, str) for p in item):
                raise UsageError("--answers: setup.packs must be a list of pack names")
        elif key == "agent":
            # question 7: which agents will open the topic (several may be picked)
            if not isinstance(item, list) or not item or not all(isinstance(a, str) for a in item):
                raise UsageError("--answers: setup.agent must be a list of agent names (%s)" % ", ".join(agents.NAMES))
            try:
                item = agents.parse_agents(item)
            except UsageError as exc:
                raise UsageError("--answers: setup.agent: %s" % exc.message.split(": ", 1)[-1])
        elif not isinstance(item, str) or not item.strip():
            raise UsageError("--answers: setup.%s must be a non-empty string" % key)
        if key == "new" and not (item.startswith("/") or item.startswith("~") or re.match(r"^[A-Za-z]:[\\/]", item)):
            raise UsageError("--answers: setup.new must be a full path (starting with / or ~), so it means the same "
                             "folder from wherever setup runs")
        if key == "personal" and item not in ("keep", "redact", "refuse"):
            raise UsageError("--answers: setup.personal must be keep, redact or refuse")
        if key == "plugin" and item not in PLUGIN_MODES:
            raise UsageError("--answers: setup.plugin must be one of %s" % ", ".join(PLUGIN_MODES))
        out[key] = item
    return out


def _flag_backed(item: Dict[str, Any]) -> Optional[str]:
    """The flag a decisions item stands for when setup derives that choice from a flag (and only then applies it),
    else None: the location, the personal-data policy, a remote URL, a pack name and the plugin mode. The answers
    with no flag ("just me", "decide later", "no", "later") stay in the decisions list."""
    question = " ".join(str(item.get("question") or "").split())
    chosen = str(item.get("chosen") or "").strip().lower()
    if question == Q_LOCATION:
        return "--new or --here (setup.new, setup.here)"
    if question == Q_PERSONAL:
        return "--personal (setup.personal)"
    if question == Q_PLUGIN:
        return "--plugin (setup.plugin)"
    if question == Q_AGENTS:
        return "--agent (setup.agent)"
    if question == Q_REMOTE and chosen == "remote":
        return "--origin (setup.origin)"
    if question == Q_PACKS and chosen not in ("none", "no", "later"):
        return "--packs (setup.packs)"
    return None


def check_answers(data: Any) -> Dict[str, Any]:
    """``{answers, decisions}`` (and ``setup`` when the file has one) from the ``--answers`` object, checked (every value with its type, so nothing
    fails half way through a run); ``UsageError`` on anything else."""
    if data in (None, {}):
        return {"answers": [], "decisions": []}
    if not isinstance(data, dict):
        raise UsageError("--answers must be a JSON object {answers: [...], decisions: [...], setup: {...}}")
    unknown = sorted(set(data) - {"answers", "decisions", "setup"})
    if unknown:
        raise UsageError("--answers: unknown key%s %s; accepted: answers, decisions, setup" % (
            "" if len(unknown) == 1 else "s", ", ".join(unknown)))
    answers = [] if data.get("answers") is None else data["answers"]
    decisions = [] if data.get("decisions") is None else data["decisions"]
    if not isinstance(answers, list) or not isinstance(decisions, list):
        raise UsageError("--answers: answers and decisions must be lists")
    for i, item in enumerate(answers):
        if not isinstance(item, dict) or not isinstance(item.get("q"), str) or not item["q"].strip():
            raise UsageError("--answers: answers[%d] needs q (a question id)" % i)
        status = item.get("status") or "answered"
        if status not in ("answered", "skipped", "na", "later", "skip_stage"):
            raise UsageError("--answers: answers[%d] has an unknown status %r" % (i, status))
        if not _text_or_absent(item.get("text")):
            raise UsageError("--answers: answers[%d]: text must be a string (the user's words)" % i)
        if status == "answered" and not (isinstance(item.get("text"), str) and item["text"].strip()):
            raise UsageError("--answers: answers[%d] needs text (the user's words)" % i)
        extra = sorted(set(item) - {"q", "text", "status", "name"})
        if extra:
            raise UsageError("--answers: answers[%d]: unknown key %s" % (i, ", ".join(extra)))
        if "name" in item:  # the goal's short name, as the user confirmed it (the goal's id is made from it)
            if item["q"].strip() != GOAL_Q:
                raise UsageError("--answers: answers[%d]: name is only for %s (the goal's short name)" % (i, GOAL_Q))
            if not isinstance(item["name"], str) or not item["name"].strip() or len(item["name"]) > GOAL_NAME_MAX \
                    or "\n" in item["name"]:
                raise UsageError("--answers: answers[%d]: name must be one line of 1 to %d characters"
                                 % (i, GOAL_NAME_MAX))
    for i, item in enumerate(decisions):
        if not isinstance(item, dict) or not isinstance(item.get("question"), str) or not item["question"].strip() \
                or not isinstance(item.get("chosen"), str) or not item["chosen"].strip():
            raise UsageError("--answers: decisions[%d] needs question and chosen (strings)" % i)
        flagged = _flag_backed(item)
        if flagged:
            raise UsageError("--answers: decisions[%d]: %r is recorded from %s, never from the decisions list, so "
                             "what it records is also what setup applies" % (i, item["question"], flagged))
        extra = sorted(set(item) - {"question", "options", "chosen", "chosen_text", "scope", "rationale",
                                    "recommended", "decided_by"})
        if extra:
            raise UsageError("--answers: decisions[%d]: unknown key %s" % (i, ", ".join(extra)))
        for key in ("chosen_text", "rationale", "recommended", "decided_by"):
            if not _text_or_absent(item.get(key)):
                raise UsageError("--answers: decisions[%d]: %s must be a string" % (i, key))
        options, scope = item.get("options") or [], item.get("scope") or []
        if not isinstance(options, list) or not isinstance(scope, list):
            raise UsageError("--answers: decisions[%d]: options and scope must be lists" % i)
        for opt in options:
            if isinstance(opt, dict):
                if not isinstance(opt.get("id"), str) or not _text_or_absent(opt.get("label")):
                    raise UsageError("--answers: decisions[%d]: an option object needs id and label strings" % i)
            elif not isinstance(opt, str):
                raise UsageError("--answers: decisions[%d]: options must be strings (id=label) or {id, label}" % i)
        if not all(isinstance(s, str) for s in scope):
            raise UsageError("--answers: decisions[%d]: scope must be a list of strings" % i)
        _check_decision_values(i, item)
    out: Dict[str, Any] = {"answers": answers, "decisions": decisions}
    setup = _check_setup_object(data.get("setup"))
    if setup:
        out["setup"] = setup
    return out


def _check_decision_values(i: int, item: Dict[str, Any]) -> None:
    """The values ``onto decide`` refuses, checked before setup writes anything: ``decided_by`` one of the
    deciders, ``chosen`` (and ``recommended``) one of the option ids or ``other`` (which needs ``chosen_text``)."""
    from . import ledger

    decided_by = item.get("decided_by")
    if decided_by is not None and decided_by not in ledger.DECIDERS:
        raise UsageError("--answers: decisions[%d]: decided_by must be one of %s" % (i, ", ".join(ledger.DECIDERS)))
    try:
        opts = ledger._options(_option_texts(item.get("options") or []))
    except UsageError as exc:
        raise UsageError("--answers: decisions[%d]: %s" % (i, exc.message))
    if not opts:
        return
    ids = [o["id"] for o in opts]
    chosen = item["chosen"].strip()
    if chosen not in ids + ["other"]:
        raise UsageError("--answers: decisions[%d]: chosen %r is not one of the options (%s) or 'other'"
                         % (i, chosen, ", ".join(ids)))
    if chosen == "other" and not str(item.get("chosen_text") or "").strip():
        raise UsageError("--answers: decisions[%d]: chosen other needs chosen_text with the user's own answer" % i)
    recommended = str(item.get("recommended") or "").strip()
    if recommended and recommended.lower() not in ("none", "null") and recommended not in ids:
        raise UsageError("--answers: decisions[%d]: recommended %r is not one of the options (%s)"
                         % (i, recommended, ", ".join(ids)))


def bank_question_ids(kit_root: str, names: List[str], topic: Optional[str] = None) -> List[str]:
    """The question ids of the packs ``names`` (as the kit vendored in ``kit_root`` ships them, else the running
    kit's) plus the topic's own ``packs/local.questions.jsonl`` when ``topic`` is given."""
    from . import packs

    folder = os.path.join(kit_root, *doctor.KIT_REL, "ontokit", "packs")
    if not os.path.isdir(folder):
        folder = packs.BUILTIN_DIR
    paths = [os.path.join(folder, "%s.questions.jsonl" % n) for n in names if n != "local"]
    if topic:
        paths.append(os.path.join(topic, "packs", "local.questions.jsonl"))
    out: List[str] = []
    for path in paths:
        rows, _problems = store.read_jsonl(path)
        out.extend(str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("id"))
    return sorted(set(out))


def _option_texts(options: List[Any]) -> List[str]:
    out = []
    for opt in options or []:
        if isinstance(opt, dict):
            out.append("%s=%s" % (opt.get("id"), opt.get("label") or opt.get("id")))
        else:
            out.append(str(opt))
    return out


# setup's own state ---------------------------------------------------------------------------------------------
STATE_REL = (".onto", "setup-state.json")
SETTINGS_REL = ".claude/settings.json"


def read_state(folder: str) -> Dict[str, Any]:
    """``.onto/setup-state.json`` of ``folder`` (``{}`` when absent or unreadable): what setup did there on this
    machine: ``made_by`` (its clone step made the folder), ``plugin`` (the mode its plugin step last tried),
    ``done`` (a ``--new`` run there finished with no failed step), ``settings_sha`` (the settings.json it wrote) and
    ``wrote`` (``{path: sha256}`` of the uncommitted files it wrote, so a rerun after a failed commit commits
    them) and ``held`` (the files it wrote whose credential-like text stopped its commit: the user was told to edit
    them, so a rerun commits them whatever they hold once the scan comes back clean)."""
    try:
        with open(os.path.join(folder, *STATE_REL), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(folder: str, values: Dict[str, Any]) -> None:
    data = read_state(folder)
    data.update(values)
    path = os.path.join(folder, *STATE_REL)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        store.write_bytes(path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    except OSError:
        pass  # a lost mark only means a rerun asks again


def _unquote_path(answer: str) -> str:
    """A folder typed or dragged into a terminal: a path the terminal escaped (``My\\ Drag``) or quoted
    (``'My Drag'``), as the shell would read it, when it reads as one word; else the text as typed. A Windows path
    keeps its backslashes."""
    text = answer.strip()
    if _windows() or not ("\\" in text or text[:1] in ("'", '"')):
        return text
    try:
        words = shlex.split(text)
    except ValueError:
        return text
    return words[0] if len(words) == 1 else text


def _real_entries(folder: str) -> List[str]:
    """The entries of ``folder`` other than the files the OS writes into a folder it shows (``OS_JUNK``: Finder's
    ``.DS_Store``, Windows' ``Thumbs.db`` and ``desktop.ini``), sorted."""
    return sorted(n for n in os.listdir(folder) if n not in OS_JUNK)


def _unwritable_parent(target: str) -> Optional[str]:
    """Why ``target`` cannot be made (its nearest existing parent is a file, or a folder this user cannot write
    in), or None."""
    here = os.path.dirname(os.path.abspath(target))
    while here and not os.path.exists(here) and os.path.dirname(here) != here:
        here = os.path.dirname(here)
    if not here:
        return None
    if not os.path.isdir(here):
        return "%s is a file, so %s cannot be made; pick another folder" % (here, target)
    if not os.access(here, os.W_OK | os.X_OK):
        return "%s is not writable, so %s cannot be made there; pick another folder" % (here, target)
    return None


# processes -----------------------------------------------------------------------------------------------------
def git_env() -> Dict[str, str]:
    return dict(os.environ, GIT_TERMINAL_PROMPT="0")


def _progress_stream() -> Any:
    """Where progress lines go: stderr when it is a terminal (the checklist itself goes to stdout at the end), else
    nowhere, so piped and ``--json`` output stays as it was."""
    try:
        return sys.stderr if sys.stderr.isatty() else None
    except (AttributeError, ValueError):
        return None


def progress(text: str) -> None:
    stream = _progress_stream()
    if stream is not None:
        stream.write("[onto] %s\n" % text)
        stream.flush()


def run_git(cwd: str, *args: str) -> Tuple[bool, str]:
    """(ok, output) of a git command that may write (gitutil only reads)."""
    try:
        proc = subprocess.run(["git", "-C", cwd] + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, env=git_env(), timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return proc.returncode == 0, proc.stdout.decode("utf-8", "replace").strip()


def git_failure(out: str) -> str:
    """What a failed git command said, short: its first ``error:`` line (the cause, such as ``gpg failed to sign the
    data``) and its last line, else the last line, else ``no output``."""
    lines = [line.strip() for line in (out or "").splitlines() if line.strip()]
    if not lines:
        return "no output"
    first = next((line for line in lines if line.startswith(("error:", "fatal:"))), lines[-1])
    return first if first == lines[-1] else "%s; %s" % (first, lines[-1])


def commit_fix(out: str, root: str) -> Optional[str]:
    """The fix for a commit git refused for a reason the user set up: commit signing (``commit.gpgsign``) or a
    hook (``core.hooksPath`` or ``.git/hooks``), else None."""
    text = (out or "").lower()
    if "gpg" in text or "sign" in text:
        return ("commit signing failed (commit.gpgsign is on): unlock or fix your gpg or ssh signing key, or turn "
                "signing off for this topic with git -C %s config commit.gpgsign false" % doctor.shell_quote(root))
    if "hook" in text:
        return "a git hook refused the commit (see git config core.hooksPath and .git/hooks): fix the hook"
    return None


def run_onto(target: str, argv: List[str]) -> Dict[str, Any]:
    """``onto <argv> --json`` served by the kit vendored in ``target`` (a subprocess, as ``handoff`` hands off),
    or by this kit in process when the vendored kit is this one. Returns the JSON object (``exit_code`` set)."""
    argv = list(argv)
    bad = [a for a in argv if not isinstance(a, str)]
    if bad:  # check_answers refuses these first; a step fails rather than the whole run stopping half way
        return {"exit_code": 2, "message": "onto %s: an argument is not text (%s)" % (
            argv[0] if argv else "", type(bad[0]).__name__)}
    launcher = handoff._launcher(target, "onto")
    if launcher:
        env = dict(os.environ)
        env[handoff.GUARD] = "1"
        try:
            proc = subprocess.run([sys.executable or "python3", launcher] + argv, cwd=target, env=env,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=600)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
            return {"exit_code": 1, "message": str(exc)}
        out, err, code = proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace"), \
            proc.returncode
    else:
        from . import cli

        out_s, err_s = io.StringIO(), io.StringIO()
        try:
            code = cli.main(argv, stdout=out_s, stderr=err_s)
        except Exception as exc:  # a step fails with the reason; the checklist and the next line still print
            return {"exit_code": 1, "message": "%s: %s" % (type(exc).__name__, exc)}
        out, err = out_s.getvalue(), err_s.getvalue()
    try:
        obj = json.loads(out)
    except ValueError:
        obj = {"message": (err or out).strip().splitlines()[-1] if (err or out).strip() else "no output"}
    if not isinstance(obj, dict):
        obj = {"result": obj}
    obj["exit_code"] = code
    return obj


def _problem_text(problem: Any) -> str:
    if isinstance(problem, dict):
        return " ".join(str(problem.get(k)) for k in ("code", "message") if problem.get(k))
    return str(problem)


def _message(obj: Dict[str, Any]) -> str:
    """The reason a kit call failed: its message, then its first two problems (the message alone may only count
    them, and a failed step's detail must say what to change)."""
    text = str(obj.get("message") or obj.get("error") or "exit %s" % obj.get("exit_code"))
    problems = [p for p in (obj.get("problems") or []) if p] if isinstance(obj.get("problems"), list) else []
    shown = [t for t in (_problem_text(p) for p in problems[:2]) if t]
    if shown:
        text += " (%s%s)" % ("; ".join(shown), "; +%d more" % (len(problems) - 2) if len(problems) > 2 else "")
    return text


# the plan ------------------------------------------------------------------------------------------------------
class Setup(object):
    """One run: ``plan`` checks everything and fixes the target; ``run`` does the steps."""

    def __init__(self, ctx: Context, args: Dict[str, Any]) -> None:
        self.ctx = ctx
        self.args = args
        self.env = ctx.env
        self.yes = bool(args.get("yes"))
        self.ask = (not self.yes) and _interactive()
        self.steps: List[Dict[str, Any]] = []
        self.user: Dict[str, Any] = {}  # setup choices that came from the user (flag, prompt, answers file)
        self.wrote = False
        self.exec_spec: Optional[Dict[str, Any]] = None
        self.claude = shutil.which("claude", path=self.env.get("PATH"))
        self.next_lines: List[str] = []
        self.plugin_mode: Optional[str] = args.get("plugin") or "project"  # settled by plan (_plan_plugin)
        self.effective_plugin = self.plugin_mode
        self.plugin_fallback = ""
        self.plugin_from = "flag"
        self.listed: Optional[Dict[str, Any]] = None
        self.kit_url_note = ""
        self.source_note = ""
        self.pre_dirty: List[str] = []  # the user's uncommitted files: never in a commit setup makes
        self.own_left: List[str] = []  # uncommitted files an earlier run wrote, unchanged since (wrote)
        self.changed_since: List[str] = []  # files an earlier run wrote that the user changed since
        self.held: List[str] = []  # files setup wrote whose credential-like text stopped the commit (held)
        self.existing = False  # the target held ontology.json before this run
        self.personal_note = ""
        self.name_note = ""  # the default folder was taken, so the name moved on (_free_name)
        self.skipped: List[int] = []  # the setup questions the user skipped (setup.skipped of the answers file)
        self.location_note = ""  # --new or --here overrode the answers file's setup.new or setup.here
        self.agents: List[str] = ["claude"]  # settled by plan (_plan_agents)
        self.agents_from = "default"
        self.paste: List[Dict[str, str]] = []  # the agents' settings that live in their own apps (do_agents)

    # prompts
    def prompt(self, question: str, default: str) -> Tuple[str, bool]:
        """(answer, typed): the default without a terminal or with --yes."""
        if not self.ask:
            return default, False
        answer = (_input("%s [%s]: " % (question, default)) or "").strip()
        return (answer, True) if answer else (default, False)

    def _ask_new_or_here(self) -> Tuple[str, bool]:
        """("new" | "here", typed). Only n, new, h or here count; anything else asks again (an empty answer, or
        the end of input, is the default "new"). "here" turns this kit checkout itself into the topic, so it is
        confirmed once more."""
        question = "Make a new folder (recommended) or use this one here? new/here"
        while True:
            answer, typed = self.prompt(question, "new")
            word = answer.strip().lower()
            if word in ("n", "new"):
                return "new", typed
            if word in ("h", "here"):
                sure, _typed = self.prompt("This turns this kit checkout itself into the topic (it leaves the %s "
                                           "branch; new topics need another kit checkout). Use it here? y/N"
                                           % TEMPLATE_BRANCH, "n")
                if sure.strip().lower() in ("y", "yes"):
                    return "here", True
                return "new", True
            question = "Please answer new or here. Make a new folder (recommended) or use this one here? new/here"

    def step(self, sid: str, status: str, detail: str) -> Dict[str, Any]:
        item = {"id": sid, "status": status, "detail": detail}
        self.steps.append(item)
        return item

    # plan
    def resolve_path(self, path: str) -> str:
        """``path`` made absolute: ``~`` expanded, a relative path read from the folder the user started in (the
        new-topic launchers cd to the kit checkout first and pass that folder in ``ONTO_SETUP_CWD``)."""
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            base = self.env.get(CALLER_CWD) or ""
            if base and os.path.isabs(base) and os.path.isdir(base):
                path = os.path.join(base, path)
        return os.path.abspath(path)

    def _merge_setup_object(self) -> None:
        """The answers file's ``setup`` object fills the flags the command line leaves out (the command line wins);
        ``--new`` and ``--here`` come from one place only."""
        given = dict(self.answers.get("setup") or {})
        self.skipped = list(given.get("skipped") or [])
        # where the summary came from, so a refusal names the place to fix: the flag or the answers file
        self.summary_from_file = bool(given.get("summary")) and not self.args.get("summary")
        if not given:
            return
        args = dict(self.args)
        if args.get("new") or args.get("here"):
            self._other_location(given)
            given.pop("new", None)
            given.pop("here", None)
        for key, value in given.items():
            if key == "skipped":
                continue  # the questions the user skipped: kept in the file for the interview, no flag
            if args.get(key) in (None, "", False, []):
                args[key] = value
        self.args = args

    def _other_location(self, given: Dict[str, Any]) -> None:
        """A folder the user typed (``setup.new``) or picked (``setup.here``) is never dropped without a word: when
        ``--new`` or ``--here`` names another place, the command line still wins, the preflight step says which
        answer it overrode, and no location decision is recorded (nobody can tell which place the user chose)."""
        new, here = self.args.get("new"), self.args.get("here")
        if not (given.get("new") or given.get("here")):
            return
        same = (bool(here) and bool(given.get("here"))) or (
            bool(new) and bool(given.get("new")) and self.resolve_path(new) == self.resolve_path(given["new"]))
        if not same:
            self.location_note = (
                "note: %s overrides the answers file's %s, and no location decision is recorded; when the user's "
                "answer was the file's, run setup again without %s" % (
                    "--here" if here else "--new", "setup.here" if given.get("here") else "setup.new %s" % (
                        short_path(self.resolve_path(given["new"]), self.env)), "--here" if here else "--new"))

    def plan(self) -> None:
        self.answers = check_answers(self.args.get("answers"))
        self._merge_setup_object()
        args = self.args
        start = os.path.abspath(os.path.expanduser(self.ctx.explicit or self.ctx.cwd or os.getcwd()))
        self.where, self.here_root = doctor.locate(start, self.env, self.ctx.explicit)
        if args.get("new") and args.get("here"):
            raise UsageError("onto setup: pass --new DIR or --here, not both")
        # flags count as the user's choices only when they come with the agent's answers file
        self.flags_from_user = args.get("answers") is not None
        if self.where == "topic" and doctor.manifest_problem(self.here_root):
            raise UsageError("onto setup: %s/ontology.json cannot be read (%s); fix it (resolve a merge conflict, "
                             "or run onto validate), then run onto setup again" % (
                                 self.here_root, doctor.manifest_problem(self.here_root)))
        mode = None
        if args.get("new"):
            mode = "new"
        elif args.get("here"):
            if self.where == "neither":
                raise UsageError("onto setup --here: %s is neither a template checkout nor a topic" % start)
            mode = "here"
        elif self.where == "topic":
            mode = "topic"
        elif self.where == "template":
            remotes = gitutil.git(self.here_root, "remote").split()
            if "kit" in remotes and gitutil.branch(self.here_root) not in (None, TEMPLATE_BRANCH):
                mode = "here"  # an earlier --here run did step 1
            elif self.yes or 3 in self.skipped:
                mode = "new"  # a skipped "Where should it live?" takes the default, ~/Ontologies/<name>
            elif self.ask:
                mode, typed = self._ask_new_or_here()
                if typed:
                    self.user["location_prompt"] = True
            else:
                raise UsageError("onto setup: pass --new DIR (a new topic folder) or --here (this clone), or --yes "
                                 "for --new ~/%s/<name>" % HOME_FOLDER)
        else:
            raise UsageError("onto setup: run it in a template checkout (or pass --new DIR from one): %s is neither "
                             "a template checkout nor a topic" % start)
        if mode == "here" and self.where == "topic":
            mode = "topic"
        if mode == "here" and not gitutil.git(self.here_root, "rev-parse", "--show-toplevel"):
            raise UsageError("onto setup --here: %s is not a git repo (a downloaded ZIP of the template?); setup "
                             "needs a clone: git clone -b %s --single-branch <repo-url> ~/general-ontology-kit, then "
                             "run ./new-topic there" % (self.here_root, TEMPLATE_BRANCH))
        self.mode = mode
        if mode == "new":
            self.source = self._template_source()
            self.kit_root = self.source
        else:
            self.source = None
            self.kit_root = self.here_root
        self._names(start)
        if mode == "new":
            given = args.get("new")
            if given:
                self.target = self.resolve_path(given)
                if self.flags_from_user and not self.location_note:
                    self.user["location"] = True
            else:
                default = os.path.join(doctor.home_dir(self.env), HOME_FOLDER, self.name)
                question = "Folder for the topic (a name alone goes under ~/%s)" % HOME_FOLDER
                while True:
                    answer, typed = self.prompt(question, short_path(default, self.env))
                    self.target = self._typed_folder(answer) if typed else default
                    try:
                        self._refuse_target_in_source()
                        self._refuse_target_in_topic()
                    except UsageError as exc:
                        if not typed:
                            raise
                        # a typed folder that cannot hold the topic is asked again, so no answer is lost
                        question = "%s. Pick another folder (a name alone goes under ~/%s)" % (
                            exc.message.split(";")[0], HOME_FOLDER)
                        continue
                    if self.ask:  # what the preflight would refuse is asked again here, with the reason
                        problem = self._new_folder_problem(self.target)
                        if problem:
                            question = "%s. Pick another folder (a name alone goes under ~/%s)" % (
                                problem.split(";")[0], HOME_FOLDER)
                            continue
                        if not self._ask_cloud(self.target):
                            question = "Pick a folder outside the cloud-synced one (a name alone goes under ~/%s)" % (
                                HOME_FOLDER)
                            continue
                    break
                if typed or self.user.get("location_prompt"):
                    self.user["location"] = True
            self._refuse_target_in_source()
            self._refuse_target_in_topic()
        else:
            self.target = self.here_root
            if (args.get("here") and self.flags_from_user and not self.location_note) or self.user.get(
                    "location_prompt"):
                self.user["location"] = True
        if not self.existing and os.path.isfile(os.path.join(self.target, store.MANIFEST)):
            self._adopt(self.target)
        if self.existing:
            self.user.pop("location", None)  # the topic is already there: nothing was decided now
        personal = args.get("personal")
        if personal and self.existing:
            have = (store.read_json(os.path.join(self.target, store.MANIFEST)).get("policy") or {}).get(
                "personal") or {}
            if not have or set(have.values()) != {personal}:
                personal = None
                self.personal_note = ("--personal %s not applied: the topic already has its personal-data policy "
                                      "(change policy.personal in ontology.json)" % args["personal"])
        if personal and self.flags_from_user:
            self.user["personal"] = personal
        self.packs = check_packs(list(args.get("packs") or []), self.kit_root)
        if self.packs and self.flags_from_user:
            self.user["packs"] = list(self.packs)
        for flag in ("origin", "kit_url"):
            value = args.get(flag)
            bad = url_problem(value) if value else None
            if bad:  # git would read a leading '-' as an option, and a line break forges checklist lines
                raise UsageError("--%s %s; pass the URL alone" % (flag.replace("_", "-"), bad))
            why = doctor.url_credential(value) if value else None
            if why:
                raise Refused("--%s holds %s; use a git credential helper and pass the URL without it"
                              % (flag.replace("_", "-"), why))
        self._check_answer_content()
        if args.get("origin") and self.flags_from_user:
            self.user["origin"] = args["origin"]
        self.kit_url_note = ""
        if args.get("kit_url"):
            args["kit_url"] = self._kit_url_form(args["kit_url"])
        self.kit_url = args.get("kit_url") or self._default_kit_url()
        self.kit_remote = git_url(self.kit_url)
        self._plan_agents()
        self._plan_plugin()

    def _typed_folder(self, answer: str) -> str:
        """The folder typed at the prompt: a full path (``/`` or ``~``) as typed, a name or a relative path under
        ``~/Ontologies`` (the prompt shows that folder), never under the kit checkout the launchers start in."""
        path = os.path.expanduser(_unquote_path(answer))
        if os.path.isabs(path):
            return os.path.abspath(path)
        return os.path.abspath(os.path.join(doctor.home_dir(self.env), HOME_FOLDER, path))

    def _content_policy(self) -> Dict[str, Any]:
        """The personal-data policy the answers will be stored under: the topic's own, else the one init writes."""
        if self.existing:
            manifest = store.read_json(os.path.join(self.target, store.MANIFEST))
            policy = dict(store.DEFAULT_POLICY)
            policy.update(manifest.get("policy") if isinstance(manifest.get("policy"), dict) else {})
            return policy
        return store.personal_policy(self.args.get("personal"))

    def _check_answer_content(self) -> None:
        """What ``onto answer`` and ``onto decide`` would refuse in the answers file, found before anything is
        written: a question id no pack of the topic has, credential-like text anywhere, personal data the policy
        refuses, and control or invisible formatting characters in the text that becomes a summary or a goal. The
        values are never echoed; each problem names its place."""
        from . import pipeline, records, secrets, sources

        problems: List[str] = []
        policy = self._content_policy()
        sanitizer = sources.default_sanitizer()

        def text_problem(where: str, text: Any) -> None:
            if not isinstance(text, str) or not text.strip():
                return
            if "\x00" in text:  # a command line cannot carry it: every answer and decision goes through one
                problems.append("%s holds the control character U+0000; write plain text" % where)
                return
            kinds = sorted({kind for kind, _m in secrets.scan_str(text)})
            if kinds:
                problems.append("%s holds credential-like text (%s)" % (where, ", ".join(kinds)))
                return
            try:
                sanitizer(text, policy)
            except Refused as exc:
                found = list(exc.extra.get("kinds") or []) if hasattr(exc, "extra") else []
                problems.append("%s holds personal data the policy refuses (%s)" % (
                    where, ", ".join(found) or "see policy.personal"))

        def ops_problem(where: str, ops: List[Dict[str, Any]]) -> None:
            draft = {"ops": json.loads(json.dumps(ops))}
            pipeline.strip_invisible_names(draft, "draft")
            for op in draft["ops"]:
                for message in records.control_problems(op, "op"):
                    problems.append("%s %s" % (where, message.split(": ", 1)[-1]))

        summary = " ".join(str(self.args.get("summary") or "").split())
        summary_place = "setup.summary" if getattr(self, "summary_from_file", False) else "--summary"
        if summary:
            text_problem(summary_place, summary)
            ops_problem(summary_place, summary_ops(summary, self.ns))
        if self.existing:
            manifest = store.read_json(os.path.join(self.target, store.MANIFEST))
            names = [str(n) for n in manifest.get("packs") or []]
        else:
            names = list(store.new_manifest("x", "x", "x").get("packs") or [])
        bank = set(bank_question_ids(self.kit_root, names + list(self.packs),
                                     self.target if self.existing else None))
        from . import interview

        for i, item in enumerate(self.answers["answers"]):
            q = item["q"].strip()
            base = q.split("@", 1)[0]
            if base not in bank and not base.startswith(interview.GAP_PREFIX) and base != interview.CATCH_ALL:
                problems.append("answers[%d]: unknown question %s (no pack of this topic asks it)" % (i, q))
            text_problem("answers[%d].text" % i, item.get("text"))
            if q == GOAL_Q and (item.get("status") or "answered") == "answered" and item.get("text"):
                ops_problem("answers[%d].text" % i, goal_ops(item["text"], self.ns, item.get("name")))
        for i, item in enumerate(self.answers["decisions"]):
            for key in ("question", "chosen", "chosen_text", "rationale"):
                text_problem("decisions[%d].%s" % (i, key), item.get(key))
            for j, label in enumerate(_option_texts(item.get("options") or [])):
                text_problem("decisions[%d].options[%d]" % (i, j), label)
        problems = list(dict.fromkeys(problems))  # one line per place: the ops quote one text more than once
        if problems:
            shown = "; ".join(problems[:5]) + ("; +%d more" % (len(problems) - 5) if len(problems) > 5 else "")
            flag_only = all(p.startswith("--summary ") for p in problems)
            if flag_only:
                fix = "Fix the --summary text"
            elif any(p.startswith("--summary ") for p in problems):
                fix = "Fix the --summary text and the answers file"
            else:
                fix = "Fix them in the answers file"
            raise UsageError("%s: %d value%s setup cannot record, so nothing was written: %s. %s and run onto setup "
                             "again" % ("--summary" if flag_only else "--answers", len(problems),
                                        "" if len(problems) == 1 else "s", shown, fix), problems=problems)

    def _refuse_target_in_source(self) -> None:
        """A new topic is its own repo: never the template checkout itself, nor a folder inside it."""
        source_forms = doctor._forms(self.source)
        for form in doctor._forms(self.target):
            for base in source_forms:
                if doctor._under(form, base):
                    raise UsageError("onto setup --new: %s is %s %s; a topic is its own repo, so pick a folder "
                                     "outside it (for example ~/%s/%s)" % (
                                         self.target, "the template checkout" if form == base else
                                         "inside the template checkout", self.source, HOME_FOLDER,
                                         self.name or "<name>"))

    def _refuse_target_in_topic(self) -> None:
        """A new topic is never made inside another topic (or inside a kit checkout): the outer repo would record
        it as an embedded repository at its next commit."""
        here = os.path.dirname(self.target)
        while here and not os.path.isdir(here) and os.path.dirname(here) != here:
            here = os.path.dirname(here)
        outer = None
        probe = here
        while probe:
            if os.path.isfile(os.path.join(probe, store.MANIFEST)):
                outer = probe
                break
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        if outer is None and here and os.path.isdir(here):
            top = gitutil.git(here, "rev-parse", "--show-toplevel")
            if top and os.path.isdir(os.path.join(top, *doctor.KIT_REL, "ontokit")):
                outer = top
        if outer:
            raise UsageError("onto setup --new: %s is inside %s, which is already a topic or a kit checkout; a topic "
                             "is its own repo, so pick a folder outside it (for example ~/%s/%s)" % (
                                 self.target, outer, HOME_FOLDER, self.name or "<name>"))

    def _template_source(self) -> str:
        """The template checkout to clone: this folder's (when it is one), else the running kit's checkout. A topic
        is never the source (its own general-ontology branch stays at the kit it was made with), and a source
        whose general-ontology branch holds an older kit than the one running is refused."""
        if shutil.which("git") is None:
            raise UsageError("onto setup --new: git is not on PATH (install git; on macOS: xcode-select --install), "
                             "so setup cannot find or clone the template checkout")
        candidates = []
        if self.where == "template":
            candidates.append(self.here_root)
        running = os.path.dirname(os.path.dirname(os.path.dirname(handoff.RUNNING_KIT)))
        candidates.append(running)
        topics = []
        branchless: List[str] = []
        tops: List[str] = []
        for root in candidates:
            top = gitutil.git(root, "rev-parse", "--show-toplevel")
            if not top or top in tops:
                continue
            tops.append(top)
            if os.path.isfile(os.path.join(top, store.MANIFEST)):
                topics.append(top)
                continue
            if gitutil.git(top, "rev-parse", "--verify", "--quiet", "refs/heads/%s" % TEMPLATE_BRANCH):
                self._check_branch_kit(top)
                return top
            if os.path.isdir(os.path.join(top, *doctor.KIT_REL, "ontokit")):
                branchless.append(top)
        if topics or self.where == "topic":
            raise UsageError("onto setup --new: run it from your kit checkout (the clone of the template), not from "
                             "inside a topic: a topic's %s branch stays at the kit it was made with" % TEMPLATE_BRANCH)
        if branchless:
            # a plain git clone checked out on another branch has the kit but only a remote-tracking copy
            raise UsageError("onto setup --new: %s is a kit checkout without a local %s branch, which setup clones "
                             "new topics from; create it with %s, then run onto setup again" % (
                                 branchless[0], TEMPLATE_BRANCH, doctor.missing_branch_fix(branchless[0])))
        raise UsageError("onto setup --new: no template checkout with a %s branch here (%s); run it in a clone of "
                         "the template" % (TEMPLATE_BRANCH, ", ".join(dict.fromkeys(tops or candidates))))

    def _check_branch_kit(self, top: str) -> None:
        """Refuse a source whose general-ontology branch holds an older kit than the running one: the topic would
        get the old kit (its manual, its launchers and its commands)."""
        text = gitutil.git(top, "show", "refs/heads/%s:%s" % (TEMPLATE_BRANCH, "/".join(
            doctor.KIT_REL + ("ontokit", "__init__.py"))))
        found = doctor.VERSION_RE.search(text or "")
        if not found:
            return
        have, running = found.group(1), __version__
        q = doctor.shell_quote(top)
        on_branch = gitutil.branch(top) == TEMPLATE_BRANCH
        head_kit = doctor.VERSION_RE.search(gitutil.git(top, "show", "HEAD:%s" % "/".join(
            doctor.KIT_REL + ("ontokit", "__init__.py"))) or "")
        if _version_key(have) < _version_key(running):
            if not on_branch and head_kit and _version_key(head_kit.group(1)) >= _version_key(running):
                # this checkout runs the newer kit from another branch: move the template branch there
                fix = _move_branch_fix(top, "HEAD")
            elif on_branch:
                fix = "git -C %s pull" % q
            else:
                fix = _move_branch_fix(top, "<the commit with kit %s>" % running)
            raise UsageError("onto setup --new: the %s branch of %s holds kit %s, older than this kit (%s), so the new "
                             "topic would get the old kit; update the branch (%s), then run onto setup again" % (
                                 TEMPLATE_BRANCH, top, have, running, fix))
        self.source_note = self._branch_lag_note(top, on_branch)

    def _branch_lag_note(self, top: str, on_branch: bool) -> str:
        """The commit the new topic gets, and a note when this checkout runs its kit from a newer commit than the
        general-ontology branch (same version, other code): the topic would get the branch's older code."""
        branch_sha = gitutil.git(top, "rev-parse", "--short", "refs/heads/%s" % TEMPLATE_BRANCH)
        note = "at %s" % branch_sha if branch_sha else ""
        running_top = gitutil.git(os.path.dirname(handoff.RUNNING_KIT), "rev-parse", "--show-toplevel")
        if on_branch or not branch_sha or running_top != top:
            return note
        head_sha = gitutil.git(top, "rev-parse", "--short", "HEAD")
        kit = "/".join(doctor.KIT_REL)
        same = gitutil.git_ok(top, "diff", "--quiet", "refs/heads/%s" % TEMPLATE_BRANCH, "HEAD", "--", kit)[0]
        if head_sha and head_sha != branch_sha and not same:
            note += ("; note: this kit runs from %s, whose kit differs from the %s branch (%s), and the topic gets "
                     "the branch's kit; to give new topics this kit: %s" % (
                         head_sha, TEMPLATE_BRANCH, branch_sha, _move_branch_fix(top, "HEAD")))
        return note

    def _adopt(self, root: str) -> None:
        """Take name, ns and title from the topic already at ``root``; refuse a flag that names another one."""
        problem = doctor.manifest_problem(root)
        if problem:
            raise UsageError("onto setup: %s/ontology.json cannot be read (%s); fix it (resolve a merge conflict, or "
                             "run onto validate), then run onto setup again" % (short_path(root, self.env), problem))
        manifest = store.read_json(os.path.join(root, store.MANIFEST))
        have = {"name": manifest.get("name"), "ns": manifest.get("ns"), "title": manifest.get("title")}
        policy = dict(store.DEFAULT_POLICY)
        policy.update(manifest.get("policy") if isinstance(manifest.get("policy"), dict) else {})
        for key in ("name", "ns", "title"):
            given = self.args.get(key)
            if key == "title" and given:
                # the stored title went through the topic's personal-data policy: compare what it would store, and
                # never print the raw flag (it may hold the data the policy keeps out)
                try:
                    given = clean_title(util.normalize_ws(given), policy)
                except Refused:
                    given = "(a title this topic's policy refuses)"
            if given and given != have[key]:
                raise UsageError("onto setup: %s already holds the topic %s (ns %s); --%s %r does not match its "
                                 "%s %r (drop the flag, or pick another folder)" % (
                                     short_path(root, self.env), have["name"], have["ns"], key, given, key,
                                     have[key]))
        self.name, self.ns, self.title = have["name"], have["ns"], have["title"]
        self.existing = True

    def _ask_valid(self, question: str, default: str, valid: Callable[[str], Any], fix: Callable[[str], str],
                   rule: str) -> str:
        """A prompt answer that ``valid`` accepts: one that does not is asked again, with ``fix`` of it as the new
        default, so a typo never ends the run after every question was answered."""
        while True:
            answer, typed = self.prompt(question, default)
            if not typed or valid(answer):
                return answer
            default = fix(answer) or default
            question = "Please use %s. %s" % (rule, question.split(". ")[-1])

    def _names(self, start: str) -> None:
        from . import ids, mutate

        args = self.args
        known = None
        if self.mode in ("topic", "here"):
            known = self.here_root
        elif self.mode == "new" and args.get("new"):
            known = self.resolve_path(args["new"])
        if known and os.path.isfile(os.path.join(known, store.MANIFEST)):
            self._adopt(known)
            return
        title = util.normalize_ws(args.get("title") or "")
        if not title and self.ask:
            title, _typed = self.prompt("In a sentence, what is this ontology about?", "")
        if title:
            # the title goes through the personal-data policy before anything is made from it: the folder, the
            # name, the ns, the root id and the first commit message never hold what the policy redacts
            title = clean_title(util.normalize_ws(title), store.personal_policy(self.args.get("personal")))
        name = args.get("name")
        if not name:
            base = os.path.basename(self.resolve_path(args["new"])) if args.get("new") else ""
            if self.mode == "here":
                base = os.path.basename(self.here_root)
            name = slug(REDACTED_RE.sub(" ", title)) or slug(base) or "my-topic"
            if self.mode == "new" and not args.get("new"):
                name = self._free_name(name)
            name = self._ask_valid("Short name (a slug)", name, mutate.NAME_RE.match, slug,
                                   "lower case letters, digits and '-'")
        title = title or title_from(name)
        ns = args.get("ns") or derive_ns(slug(name) or name)
        if not args.get("ns"):
            ns = self._ask_valid("Namespace (other topics import it under this)", ns,
                                 lambda v: ids.NS_RE.match(v) and v not in ids.RESERVED_NS, derive_ns,
                                 "lower case letters, digits and '-', starting with a letter (at most 32)")
        mutate._check_new_topic(name, ns, title, self.args.get("personal"))
        self.name, self.ns, self.title = name, ns, title

    def _taken(self, folder: str) -> bool:
        """True when the default folder ``folder`` cannot take a new topic: it holds a topic whose setup finished
        (or one setup did not make), or other files. An absent or empty folder, or setup's own clone whose earlier
        run did not finish, is free (setup then resumes there)."""
        if not os.path.exists(folder):
            return False
        if not os.path.isdir(folder):
            return True
        try:
            if not _real_entries(folder):
                return False
        except OSError:
            return True
        if not self._own_clone(folder):
            return True
        if not os.path.isfile(os.path.join(folder, store.MANIFEST)):
            return False
        state = read_state(folder)
        return bool(state.get("done")) or not state.get("made_by")

    def _free_name(self, name: str) -> str:
        """``name``, or ``name-2``, ``name-3`` ... when ``~/Ontologies/<name>`` is taken, so a second one-click run
        with the same (or no) title makes a new topic instead of reopening the first one."""
        folder = os.path.join(doctor.home_dir(self.env), HOME_FOLDER)
        if not self._taken(os.path.join(folder, name)):
            return name
        base = name[:60].rstrip("-") or "my-topic"
        for n in range(2, 1000):
            candidate = "%s-%d" % (base, n)
            if not self._taken(os.path.join(folder, candidate)):
                self.name_note = ("~/%s/%s already holds a topic, so this one is %s (to open that one instead, run "
                                  "onto setup --new ~/%s/%s)" % (HOME_FOLDER, name, candidate, HOME_FOLDER, name))
                return candidate
        return name

    def _kit_url_form(self, url: str) -> str:
        """``--kit-url`` as setup stores it: a URL (``scheme://`` or ``user@host:path``) or ``owner/repo`` as given;
        a host and path typed without a scheme (``github.com/owner/repo``) with ``https://``; a folder as its full
        path, read from where the user started (``resolve_path``). A folder that does not exist is refused, since
        ``git fetch kit`` (how a topic gets a newer kit) could never read it."""
        text = url.strip()
        if re.match(r"^[a-z][a-z0-9+.-]*://", text) or re.match(r"^[^/@\s]+@[^:/\s]+:", text):
            return text
        if text.startswith("file:"):
            return text
        if SHORTHAND_RE.match(text) and "." not in text.split("/", 1)[0]:
            return text
        path = self.resolve_path(text)
        if os.path.isdir(path):
            return path
        if HOST_PATH_RE.match(text):
            return "https://" + text
        raise UsageError("--kit-url %s is not a git URL, and no folder %s exists; pass the kit's URL "
                         "(https://host/owner/repo.git, git@host:owner/repo.git or owner/repo) or an existing folder"
                         % (text, path))

    def _default_kit_url(self) -> Optional[str]:
        """The kit URL from the template's ``kit`` or ``origin`` remote. A user part or token in it never goes on:
        setup uses the URL without it (the clone's own git config keeps what it had), and says so."""
        root = self.source if self.mode == "new" else self.here_root
        remotes = doctor._remotes(root) if root else {}
        # a topic's origin is the user's own remote, never the kit: in a topic only its kit remote counts
        in_topic = self.mode != "new" and self.existing
        url = remotes.get("kit") or (None if in_topic else remotes.get("origin")) or None
        if url and doctor.url_credential(url):
            clean = doctor.strip_credentials(url)
            if doctor.url_credential(clean):
                raise Refused("the template's kit remote URL holds a credential setup cannot take out; set a clean "
                              "URL with git remote set-url, or pass --kit-url without the credential")
            self.kit_url_note = "the template's remote URL holds a credential; setup uses it without: %s" % (
                display_url(clean))
            return clean
        return url

    def _recorded_agents(self) -> Optional[List[str]]:
        rec = self._active_decisions().get(Q_AGENTS) if self.target else None
        try:
            names = agents.parse_agents((rec or {}).get("chosen") or "")
        except UsageError:
            return None
        return names or None

    def _wired_agents(self) -> Optional[List[str]]:
        """The agents an earlier run already wired in this topic (their files are in the repo, or Claude Code's
        settings enable the plugin), in the kit's order; None when nothing is wired. A scripted ``--agent`` run
        records no decision, so this is what keeps its choice on a bare rerun."""
        if not self.target or not os.path.isdir(self.target):
            return None
        names = [n for n in agents.NAMES if n != "generic"
                 and agents.wired(self.target, n) not in ("not wired", "no files")]
        return names or None

    def _plan_agents(self) -> None:
        """The agents to wire (question 7): ``--agent`` (or ``setup.agent``), else the topic's recorded setup
        decision, else the agents an earlier run already wired here (``_wired_agents``), else Claude Code alone.
        Unknown names are refused here, before anything is written. The choice is recorded as a decision only when
        it came from the user (with ``--answers``)."""
        given = self.args.get("agent")
        if given:
            self.agents, self.agents_from = agents.parse_agents(given), "flag"
            if not self.agents:
                raise UsageError("--agent needs at least one of %s" % ", ".join(agents.NAMES))
            if self.flags_from_user:
                self.user["agent"] = list(self.agents)
            return
        recorded = self._recorded_agents()
        wired = None if recorded else self._wired_agents()
        if recorded:
            self.agents, self.agents_from = recorded, "decision"
        elif wired:
            self.agents, self.agents_from = wired, "wired"
        else:
            self.agents, self.agents_from = ["claude"], "default"

    def _claude_chosen(self) -> bool:
        """Claude Code is one of the agents, or the plugin step was asked for by name (``--plugin``)."""
        chosen = getattr(self, "agents", None) or ["claude"]
        return "claude" in chosen or bool((getattr(self, "args", None) or {}).get("plugin"))

    def _recorded_plugin(self) -> Optional[str]:
        rec = self._active_decisions().get(" ".join(Q_PLUGIN.split())) if self.target else None
        chosen = (rec or {}).get("chosen")
        return chosen if chosen in PLUGIN_MODES else None

    def _plan_plugin(self) -> None:
        """The plugin mode: ``--plugin``; else the topic's recorded setup decision; else the mode an earlier setup
        run on this machine tried (``.onto/setup-state.json``, so a failed install is tried again and a scripted
        ``--plugin skip`` stays skipped); else, in a topic setup already committed, nothing (the wiring is left as
        it is); else ``project``. A fallback to ``plugin-dir`` (a local kit URL, or a folder marketplace for
        ``local``) is known here, so a choice setup cannot apply is never recorded as applied."""
        self.plugin_from = "flag"
        mode = self.args.get("plugin")
        if not self._claude_chosen():
            # question 7a is asked only for Claude Code: no plugin wiring for a topic other agents open
            self.plugin_mode = self.effective_plugin = None
            self.plugin_from, self.plugin_fallback = "agents", ""
            return
        if not mode:
            mode = self._recorded_plugin()
            self.plugin_from = "decision"
        if not mode and self.target and os.path.isdir(self.target):
            tried = read_state(self.target).get("plugin")
            if tried in PLUGIN_MODES:
                mode, self.plugin_from = tried, "state"
        if not mode:
            committed = os.path.isdir(os.path.join(self.target, ".git")) and gitutil.git_ok(
                self.target, "cat-file", "-e", "HEAD:%s" % store.MANIFEST)[0]
            if self.existing and committed:
                mode, self.plugin_from = None, "none"
            else:
                mode, self.plugin_from = "project", "default"
        self.plugin_mode = mode
        self.effective_plugin = mode
        self.plugin_fallback = ""
        if mode in ("project", "local") and doctor.local_source(self.kit_url):
            self.effective_plugin = "plugin-dir"
            self.plugin_fallback = (
                ("the kit URL %s is a local folder" % display_url(self.kit_url) if self.kit_url else
                 "the template has no git remote, so there is no kit URL to install the plugin from") +
                "; adding a local folder as the marketplace would take over the one general-ontology marketplace on "
                "this machine, so start Claude Code with --plugin-dir instead (give the template a git remote to "
                "install it later)")
        elif mode in ("project", "local") and self.claude:
            # a general-ontology marketplace claude already lists from a folder would load that folder's skills
            # in this topic too, whichever scope the install uses: never report it as installed
            wiring = doctor.settings_wiring(self.target) if os.path.isdir(self.target) else {}
            if not (wiring.get(mode) and not wiring.get("folder_source")):
                progress("Checking the plugin marketplaces with claude...")
                listed = doctor.claude_marketplace(self.claude)
                if listed and listed.get("folder"):
                    self.effective_plugin = "plugin-dir"
                    self.plugin_fallback = (
                        "claude lists the general-ontology marketplace from a local folder, so installing would load "
                        "that folder; start with claude --plugin-dir %s for now. To repair: %s"
                        % (PLUGIN_DIR_REL, doctor.REPAIR))
                self.listed = listed
        if self.args.get("plugin") and self.flags_from_user and not self.plugin_fallback:
            self.user["plugin"] = self.args["plugin"]

    # run
    def run(self) -> Dict[str, Any]:
        if os.path.exists(os.path.join(self.target, ".git")):
            # what was uncommitted before setup wrote anything: later commits never sweep it in
            self.pre_dirty = gitutil.dirty_paths(self.target)
            if SETTINGS_REL in self.pre_dirty and self._settings_are_setups():
                # left by an earlier run (an install that failed after marketplace add wrote it, or a commit that
                # failed): setup's own file, so this run commits it with the wiring commit
                self.pre_dirty.remove(SETTINGS_REL)
            # an agent file an earlier run wrote and did not commit (its bytes unchanged since) is setup's too
            own = self._agent_shas()
            self.pre_dirty = [p for p in self.pre_dirty if not (p in agents.WIRED_PATHS and own.get(p) and
                                                                own.get(p) == self._file_sha(p))]
            self._claim_own_files()
        try:
            for sid in STEPS:
                try:
                    ok = getattr(self, "do_" + sid.replace("-", "_"))()
                except KeyboardInterrupt:  # Ctrl-C in the window: say where it stopped, never a traceback
                    self.step(sid, "failed", "stopped (Ctrl-C); run the same onto setup command again")
                    break
                if ok is False:
                    break
        finally:
            self._record_written()  # whatever stopped this run, the next one knows which files are setup's
        done = {s["id"] for s in self.steps}
        for sid in STEPS:
            if sid not in done:
                self.step(sid, "skipped", "not run: an earlier step failed")
        order = {sid: i for i, sid in enumerate(STEPS)}
        self.steps.sort(key=lambda s: order[s["id"]])
        failed = [s for s in self.steps if s["status"] == "failed"]
        if not failed:
            self._retire_answers_file()
            if self.mode == "new" and os.path.isdir(self.target):
                self._write_state({"done": True})  # a later default run picks a free name (_free_name)
        result: Dict[str, Any] = {
            "steps": self.steps,
            "topic": {"path": self.target, "name": self.name, "ns": self.ns, "title": self.title},
            "next": self.next_lines or self._next(bool(failed)),
            "exit_code": 1 if failed else 0,
        }
        if self.paste:
            result["paste"] = self.paste
        if self.exec_spec and all(s["id"] == "plugin" for s in failed):
            # only the install failed: the topic is ready, and the session starts with --plugin-dir
            result["exec"] = self.exec_spec
        return result

    def do_preflight(self) -> bool:
        problems = []
        if tuple(sys.version_info[:2]) < doctor.MINIMUM:
            problems.append("python %s is older than 3.9" % sys.version.split()[0])
        if shutil.which("git") is None:
            problems.append("git is not on PATH (install git)")
        else:
            # the commit step needs a name and an email; a clone a --new run makes has no repo config of its own yet
            ident = identity_at(self.target)
            if ident:
                problems.append("git cannot name the author of a commit (%s); %s, then run onto setup again" % (
                    ident, gitutil.IDENTITY_FIX))
        folder = self._new_folder_problem(self.target) if self.mode == "new" else None
        if folder:
            problems.append(folder)
        reason = doctor.cloud_synced(self.target, self.env)
        notes = [n for n in (self.name_note, self.location_note) if n]
        if reason:
            confirmed = bool(self.args.get("cloud_ok")) or getattr(self, "cloud_confirmed", False)
            if not confirmed and self.ask:
                answer, _typed = self.prompt("%s: %s. Cloud sync evicts files and makes git hang. Use it anyway? "
                                             "y/N" % (short_path(self.target, self.env), reason), "n")
                confirmed = answer.lower().startswith("y")
            if confirmed:
                notes.append("cloud-synced folder accepted (%s)" % reason)
            else:
                problems.append("%s is in a cloud-synced folder: %s; %s, or confirm with --cloud-ok" % (
                    short_path(self.target, self.env), reason, doctor.MOVE_FIX))
        if problems:
            self.step("preflight", "failed", "; ".join(problems))
            return False
        self.step("preflight", "done", "python %s, git, target %s%s" % (
            ".".join(str(n) for n in sys.version_info[:3]), self.target,
            "; " + "; ".join(notes) if notes else ""))
        return True

    def _new_folder_problem(self, target: str) -> Optional[str]:
        """Why a new topic cannot go in ``target`` (a file, a folder that holds files, or a parent this user cannot
        write in), or None."""
        if os.path.exists(target):
            if not os.path.isdir(target):
                return "%s is a file; pick a new folder" % target
            found = _real_entries(target)
            if found and not self._own_clone(target):
                return "%s is not empty (it holds %s%s); pick a new folder" % (
                    target, ", ".join(found[:3]), ", ..." if len(found) > 3 else "")
            return None
        return _unwritable_parent(target)

    def _ask_cloud(self, target: str) -> bool:
        """At the folder prompt: True when ``target`` is not in a cloud-synced folder, or the user said to use it
        anyway (remembered, so the preflight does not ask again)."""
        reason = doctor.cloud_synced(target, self.env)
        if not reason or self.args.get("cloud_ok"):
            return True
        answer, _typed = self.prompt("%s: %s. Cloud sync evicts files and makes git hang. Use it anyway? y/N" % (
            short_path(target, self.env), reason), "n")
        self.cloud_confirmed = answer.lower().startswith("y")
        return self.cloud_confirmed

    def _own_clone(self, folder: str) -> bool:
        """True when ``folder`` is a clone an earlier run of setup made (its clone step leaves a mark in the
        gitignored ``.onto/``), or a topic already (setup then does only what is missing). Any other kit checkout
        is not empty."""
        if not (os.path.isdir(os.path.join(folder, *doctor.KIT_REL, "ontokit")) and os.path.exists(
                os.path.join(folder, ".git"))):
            return False
        return bool(read_state(folder).get("made_by")) or os.path.isfile(os.path.join(folder, store.MANIFEST))

    def _settings_bytes(self) -> Optional[bytes]:
        try:
            with open(os.path.join(self.target, *SETTINGS_REL.split("/")), "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def _settings_are_setups(self) -> bool:
        """True when ``.claude/settings.json`` is the file setup (or claude on its behalf) left there: its bytes match
        the hash an earlier run recorded, or it holds only setup's own entries (the general-ontology marketplace and
        plugin)."""
        data = self._settings_bytes()
        if data is None:
            return False
        if read_state(self.target).get("settings_sha") == util.sha256_hex(data):
            return True
        try:
            doc = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return False
        if not isinstance(doc, dict) or not doc or set(doc) - {"extraKnownMarketplaces", "enabledPlugins"}:
            return False
        markets, enabled = doc.get("extraKnownMarketplaces", {}), doc.get("enabledPlugins", {})
        return (isinstance(markets, dict) and isinstance(enabled, dict) and set(markets) <= {MARKETPLACE}
                and set(enabled) <= {PLUGIN_KEY})

    def _record_settings(self) -> None:
        """Remember the bytes of the settings.json this run wrote or had claude write, so a rerun after a failed
        install or commit knows the file is setup's own (never for a file with the user's uncommitted changes)."""
        data = self._settings_bytes()
        if data is not None and SETTINGS_REL not in self.pre_dirty:
            self._write_state({"settings_sha": util.sha256_hex(data)})

    def _file_sha(self, rel: str) -> Optional[str]:
        path = os.path.join(self.target, *rel.split("/"))
        if not os.path.isfile(path) or os.path.islink(path):
            return None
        try:
            with open(path, "rb") as fh:
                return util.sha256_hex(fh.read())
        except OSError:
            return None

    def _claim_own_files(self) -> None:
        """Take out of ``pre_dirty`` the files an earlier run wrote (``wrote`` in the state) whose bytes are
        unchanged since: a commit that failed (no git identity, a failing hook) left them uncommitted, and they are
        setup's to commit. A file the user changed since then is theirs (``changed_since``), except one in ``held``:
        its credential-like text stopped the commit and the user was told to take it out, so the edit is expected."""
        state = read_state(self.target)
        recorded = state.get("wrote")
        recorded = recorded if isinstance(recorded, dict) else {}
        held = state.get("held")
        held = {p for p in held if isinstance(p, str)} if isinstance(held, list) else set()
        own, changed = [], []
        for rel in self.pre_dirty:
            if rel == SETTINGS_REL:
                continue
            if rel in held:
                own.append(rel)
            elif rel in recorded:
                (own if self._file_sha(rel) == recorded[rel] else changed).append(rel)
        self.own_left, self.changed_since = own, changed
        self.held = sorted(held & set(own))
        self.pre_dirty = [p for p in self.pre_dirty if p not in own]

    def _record_written(self) -> None:
        """Record in the state the uncommitted files this run (or an earlier one) wrote, with their hashes: every
        dirty path that was not the user's when the run started, except ``.claude/settings.json`` (``settings_sha``
        covers it) and setup's own ``.onto/``, plus the ``held`` ones still uncommitted. A commit that went through
        leaves an empty record."""
        if not os.path.isdir(os.path.join(self.target, ".git")):
            return
        pre = set(self.pre_dirty)
        wrote: Dict[str, str] = {}
        for rel in gitutil.dirty_paths(self.target):
            if rel in pre or rel == SETTINGS_REL or rel in agents.WIRED_PATHS or rel.startswith(".onto/"):
                continue  # settings_sha and agents_sha cover setup's own wiring files
            sha = self._file_sha(rel)
            if sha:
                wrote[rel] = sha
        held = [p for p in self.held if p in wrote]
        state = read_state(self.target)
        if wrote or state.get("wrote") or held or state.get("held"):
            self._write_state({"wrote": wrote, "held": held})

    def _write_state(self, values: Dict[str, Any]) -> None:
        """Merge ``values`` into the target's ``.onto/setup-state.json`` (gitignored, this machine only)."""
        write_state(self.target, values)

    def do_clone(self) -> bool:
        if self.mode != "new":
            self.step("clone", "skipped", "using this folder")
            return True
        if os.path.isdir(self.target) and self._own_clone(self.target):
            self.step("clone", "already", "%s is a clone of the template" % self.target)
            return True
        parent = os.path.dirname(self.target)
        if parent and not os.path.isdir(parent):
            try:
                os.makedirs(parent)
            except OSError as exc:
                self.step("clone", "failed", "cannot make the folder %s: %s; pick another folder" % (
                    parent, exc.strerror or exc))
                return False
        if os.path.isdir(self.target):
            for name in OS_JUNK:  # only what the OS wrote is there (preflight checked): git clone needs it empty
                try:
                    os.remove(os.path.join(self.target, name))
                except OSError:
                    pass
        # --no-tags: the checkout may hold another project's release tags, which a topic must not inherit.
        # --no-local: a clone from a path otherwise copies (or hardlinks) every object of the source repo, the
        # other branches' history included; through git's own transport only the template branch comes over.
        progress("Copying the kit into %s..." % self.target)
        ok, out = run_git(parent or ".", "clone", "-q", "--no-local", "--single-branch", "--no-tags", "-b",
                          TEMPLATE_BRANCH, self.source, self.target)
        if not ok:
            self.step("clone", "failed", "git clone failed: %s" % (out.splitlines()[-1] if out else "no output"))
            return False
        self._write_state({"made_by": "onto setup"})
        note = getattr(self, "source_note", "")
        self.step("clone", "done", "cloned the %s branch of %s%s" % (TEMPLATE_BRANCH, self.source,
                                                                    " " + note if note else ""))
        return True

    def do_branch(self) -> bool:
        t = self.target
        changed: List[str] = []
        notes: List[str] = []
        branch = gitutil.branch(t)
        fresh = self.mode == "here" and not os.path.isfile(os.path.join(t, store.MANIFEST))
        if fresh and branch not in (TEMPLATE_BRANCH, None, "main"):
            # a clone checked out on another branch: the topic still gets its own main, without that branch's
            # upstream (a git pull in the topic would merge kit branches into it)
            if gitutil.git(t, "rev-parse", "--verify", "--quiet", "refs/heads/main"):
                self.step("branch", "failed", "this checkout is on %s and already has a main branch; check out %s "
                                              "(git checkout %s), then run onto setup again" % (
                                                  branch, TEMPLATE_BRANCH, TEMPLATE_BRANCH))
                return False
            ok, out = run_git(t, "checkout", "-q", "--no-track", "-b", "main")
            if not ok:
                self.step("branch", "failed", "git checkout -b main failed: %s" % out)
                return False
            changed.append("on branch main (made from %s)" % branch)
        elif branch == TEMPLATE_BRANCH or branch is None:
            exists = gitutil.git(t, "rev-parse", "--verify", "--quiet", "refs/heads/main")
            if exists and not (gitutil.git(t, "merge-base", "HEAD", "main") or
                               gitutil.git(t, "cat-file", "-t", "main:%s" % store.MANIFEST)):
                # a main with no history in common with the template (the default branch of the repo the template
                # was published in): checking it out would replace this kit checkout with another project
                self.step("branch", "failed", "this checkout already has a main branch that is not made from the "
                                              "template (it shares no history with %s); nothing was changed. Rename "
                                              "that branch (git branch -m main other-main), or clone the template "
                                              "alone (git clone -b %s --single-branch <repo-url>), then run onto "
                                              "setup again" % (TEMPLATE_BRANCH, TEMPLATE_BRANCH))
                return False
            ok, out = run_git(t, "checkout", "-q", "main") if exists else run_git(t, "checkout", "-q", "-b", "main")
            if not ok:
                self.step("branch", "failed", "git checkout main failed: %s" % out)
                return False
            changed.append("on branch main")
        else:
            notes.append("on branch %s" % branch)
        remotes = doctor._remotes(t)
        # only a template clone becoming a topic has the kit as its origin; a topic's origin is the user's own
        # remote (a backup, or a teammate's shared copy), which setup never renames or re-points
        if not self.existing and "kit" not in remotes and "origin" in remotes and (
                self.mode == "new" or gitutil.git(t, "rev-parse", "--verify", "--quiet",
                                                  "refs/remotes/origin/%s" % TEMPLATE_BRANCH)
                or doctor.strip_credentials(remotes.get("origin") or "") == self.kit_remote):
            ok, out = run_git(t, "remote", "rename", "origin", "kit")
            if not ok:
                self.step("branch", "failed", "git remote rename origin kit failed: %s" % out)
                return False
            changed.append("origin renamed to kit")
            remotes = doctor._remotes(t)
        if self.kit_remote:
            if "kit" not in remotes:
                ok, out = run_git(t, "remote", "add", "--", "kit", self.kit_remote)
                changed.append("kit remote added") if ok else notes.append("kit remote not added: %s" % out)
            elif doctor.strip_credentials(remotes.get("kit") or "") != self.kit_remote:
                ok, out = run_git(t, "remote", "set-url", "--", "kit", self.kit_remote)
                changed.append("kit points at %s" % display_url(self.kit_remote)) if ok else notes.append(
                    "kit remote not changed: %s" % out)
        elif "kit" in remotes:
            notes.append("kit is %s" % display_url(remotes["kit"]))
        else:
            notes.append("no kit remote (pass --kit-url URL to add one)")
        origin = self.args.get("origin")
        remotes = doctor._remotes(t)
        if origin:
            if "origin" not in remotes:
                ok, out = run_git(t, "remote", "add", "--", "origin", origin)
                if not ok:
                    self.step("branch", "failed", "git remote add origin failed: %s" % out)
                    return False
                changed.append("origin is %s (never pushed by setup)" % display_url(origin))
            elif remotes["origin"] != origin:
                self.user.pop("origin", None)  # not applied, so not recorded
                notes.append("origin already points at %s; change it with git remote set-url origin URL"
                             % display_url(remotes["origin"]))
        if self.kit_url_note:
            notes.append(self.kit_url_note)
        detail = "; ".join(changed + notes) or "step 1 done"
        self.step("branch", "done" if changed else "already", detail)
        return True

    def do_init(self) -> bool:
        if os.path.isfile(os.path.join(self.target, store.MANIFEST)):
            self.step("init", "already", "ontology.json exists (ns %s)%s" % (
                self.ns, "; " + self.personal_note if self.personal_note else ""))
            return self._open_repo()
        argv = ["init", "--json", "--path=%s" % self.target, "--name=%s" % self.name, "--ns=%s" % self.ns,
                "--title=%s" % self.title]
        if self.args.get("personal"):
            argv.append("--personal=%s" % self.args["personal"])
        obj = run_onto(self.target, argv)
        if obj.get("exit_code"):
            self.step("init", "failed", "onto init failed: %s" % _message(obj))
            return False
        self.wrote = True
        warnings = [w for w in obj.get("warnings") or [] if "git rules" not in str(w)]
        self.step("init", "done", "created topic %s (%s), %d files%s" % (
            self.ns, self.title, len(obj.get("created") or []), "; " + "; ".join(warnings) if warnings else ""))
        return self._open_repo()

    def _open_repo(self) -> bool:
        try:
            self.ctx.repo = store.Repo.open(self.target)
        except Exception:
            pass
        return True

    def do_packs(self) -> bool:
        if not self.packs:
            self.step("packs", "skipped", "no extra packs asked for")
            return True
        try:
            added = add_builtin_packs(self.target, self.packs)
        except UsageError as exc:
            self.step("packs", "failed", str(exc))
            return False
        if added:
            self.wrote = True
            self.step("packs", "done", "added %s to ontology.json" % ", ".join(added))
        else:
            self.step("packs", "already", "%s already in ontology.json" % ", ".join(self.packs))
        return True

    # answers
    def _last_statuses(self) -> Dict[str, str]:
        """``{question id: its latest status}`` in the interview log (bank questions, not gap questions)."""
        rows, _problems = store.read_jsonl(os.path.join(self.target, "interview", "log.jsonl"))
        out: Dict[str, str] = {}
        for r in rows:
            if r.get("node"):
                continue
            out[str(r.get("q"))] = str(r.get("status"))
        return out

    def _active_decisions(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        folder = os.path.join(self.target, "ledger", "decisions")
        if not os.path.isdir(folder):
            return out
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".json"):
                continue
            try:
                rec = store.read_json(os.path.join(folder, name))
            except Exception:
                continue
            if isinstance(rec, dict) and rec.get("status") == "active":
                out[" ".join(str(rec.get("question") or "").split())] = rec
        return out

    def _decide(self, item: Dict[str, Any], active: Dict[str, Dict[str, Any]]) -> Tuple[str, str]:
        """("done" | "already" | "failed", message) for one decision."""
        question = " ".join(str(item["question"]).split())
        old = active.get(question)
        chosen_text = item.get("chosen_text")
        if old and old.get("chosen") == item["chosen"] and (old.get("chosen_text") or None) == (chosen_text or None):
            return "already", old.get("id")
        argv = ["decide", "--json", "--repo=%s" % self.target, "--question=%s" % question,
                "--chosen=%s" % item["chosen"], "--decided-by=%s" % (item.get("decided_by") or "user"),
                "--scope=%s" % json.dumps(list(item.get("scope") or SCOPE))]
        options = _option_texts(item.get("options") or [])
        if options:
            argv.append("--options=%s" % json.dumps(options, ensure_ascii=False))
        for key in ("chosen_text", "rationale", "recommended"):
            if item.get(key):
                argv.append("--%s=%s" % (key.replace("_", "-"), item[key]))
        if old:
            argv.append("--supersedes=%s" % old.get("id"))
        obj = run_onto(self.target, argv)
        if obj.get("exit_code"):
            return "failed", _message(obj)
        dec = obj.get("decision") or {}
        active[question] = dec
        return "done", str(dec.get("id"))

    def _setup_decisions(self) -> List[Dict[str, Any]]:
        """Setup's own choices that came from the user, as decisions. A choice passed in this run wins over an item
        of the answers file for the same question (``do_answers`` drops the file's item), so a changed choice
        supersedes the old decision."""
        out: List[Dict[str, Any]] = []
        u = self.user
        if u.get("location"):
            home = os.path.join(doctor.home_dir(self.env), HOME_FOLDER, self.name)
            chosen = "here" if self.mode in ("here", "topic") else ("home" if self.target == home else "elsewhere")
            # never the full path: folder names above the topic can name a person or a machine
            text = "~/%s/%s" % (HOME_FOLDER, self.name) if chosen == "home" else "a folder named %s" % (
                os.path.basename(self.target.rstrip(os.sep)) or self.name)
            out.append({"question": Q_LOCATION, "options": [
                "home=~/%s/%s" % (HOME_FOLDER, self.name), "here=this folder", "elsewhere=somewhere else"],
                "recommended": "home", "chosen": chosen, "chosen_text": text})
        if u.get("origin"):
            out.append({"question": Q_REMOTE, "options": [
                "solo=just me, no remote for now", "remote=a private git remote", "later=decide later"],
                "chosen": "remote", "chosen_text": remote_text(u["origin"])})
        if u.get("personal"):
            out.append({"question": Q_PERSONAL, "options": [
                "keep=public or my own notes: nothing redacted",
                "redact=internal, may name people: contact details and user names in paths redacted, names in "
                "prose kept",
                "refuse=confidential: text with contact details or user names in paths refused, names in prose "
                "kept"],
                "recommended": "redact", "chosen": u["personal"]})
        if u.get("packs"):
            out.append({"question": Q_PACKS, "chosen": ", ".join(u["packs"])})
        if u.get("agent"):
            out.append({"question": Q_AGENTS, "chosen": ", ".join(u["agent"]),
                        "chosen_text": ", ".join(agents.harness(n)["label"] for n in u["agent"])})
        if u.get("plugin"):
            out.append({"question": Q_PLUGIN, "options": [
                "project=install it for this repo", "local=install it for me only, in this repo",
                "plugin-dir=run it without installing", "skip=do not wire it"],
                "recommended": "project", "chosen": u["plugin"]})
        # a skipped question leaves its default and is recorded as skipped, so the topic does not ask it again
        answers = getattr(self, "answers", None) or {}
        have = {" ".join(str(d["question"]).split()) for d in out} | {
            " ".join(str(d["question"]).split()) for d in answers.get("decisions") or []}
        args = getattr(self, "args", None) or {}
        for number in sorted(set(getattr(self, "skipped", None) or [])):
            question = SKIP_QUESTIONS.get(number)
            if question and question not in have and not (number == 7 and args.get("plugin")) and not (
                    number == 5 and args.get("personal")) and not (number == 6 and getattr(self, "packs", None)):
                out.append({"question": question, "chosen": SKIPPED, "chosen_text": SKIPPED_TEXT})
            # question 7 is the agents question, and its plugin follow-up (7a) is skipped with it
            if number == 7 and Q_AGENTS not in have and not args.get("agent"):
                out.append({"question": Q_AGENTS, "chosen": SKIPPED, "chosen_text": SKIPPED_TEXT})
        for item in out:
            item["scope"] = list(SCOPE)
        return out

    def _answer_argv(self, item: Dict[str, Any], q: str, status: str) -> List[str]:
        argv = ["answer", "--json", "--repo=%s" % self.target]
        if status == "answered":
            argv.append("--apply")
            if q == GOAL_Q:  # the goal becomes a goal node, so it ranks every later question
                argv.append("--ops=%s" % json.dumps(goal_ops(self._stored_words(item["text"]), self.ns,
                                                             item.get("name")), ensure_ascii=False))
        else:
            argv.append("--status=%s" % status)
        return argv + ["--", q] + ([item["text"]] if item.get("text") else [])

    def _stored_words(self, text: str) -> str:
        """``text`` as the topic will store it: run through the topic's sanitizer under its policy (``--personal
        redact`` writes ``[redacted:<kind>]``), so the ops built from it quote words the stored answer holds and the
        stated goal stays stated. The text itself when the topic cannot be read or the sanitizer refuses it (the
        answer call reports that)."""
        try:
            repo = store.Repo.open(self.target)
            body, _redactions = sources.default_sanitizer()(sources.normalize_text(str(text or "")), repo.policy)
        except Exception:  # the answer call itself refuses or reports it
            return str(text or "")
        return body

    def _summary_answer(self) -> Optional[Tuple[str, str]]:
        """("done" | "already" | "failed", message) for the setup interview's first answer (``--summary``): it
        becomes the summary of ``topic:<ns>`` as the answer to ``q.gap.thin@topic:<ns>``, so the interview never
        asks what the topic is about twice. None without ``--summary``."""
        text = " ".join(str(self.args.get("summary") or "").split())
        if not text:
            return None
        root = "topic:%s" % self.ns
        rows, _problems = store.read_jsonl(os.path.join(self.target, "interview", "log.jsonl"))
        if any(r.get("q") == THIN_Q and r.get("node") == root and r.get("status") in ("answered", "na")
               for r in rows):
            return "already", ""
        nodes, _problems = store.read_jsonl(os.path.join(self.target, "graph", "nodes.jsonl"))
        if any(n.get("id") == root and str(n.get("summary") or "").strip() for n in nodes):
            return "already", ""
        argv = ["answer", "--json", "--repo=%s" % self.target, "--apply", "--confirm",
                "--ops=%s" % json.dumps(summary_ops(self._stored_words(text), self.ns), ensure_ascii=False),
                "--",
                "%s@%s" % (THIN_Q, root), text]
        obj = run_onto(self.target, argv)
        if obj.get("exit_code"):
            return "failed", "--summary: %s" % _message(obj)
        return "done", ""

    def do_answers(self) -> bool:
        answers = list(self.answers["answers"])
        if 2 in (getattr(self, "skipped", None) or []) and not any(
                str(a.get("q") or "").strip() == GOAL_Q for a in answers):
            # a goal skipped in the setup interview is logged as skipped, so the topic does not ask it at once
            answers.append({"q": GOAL_Q, "status": "skipped"})
        own = self._setup_decisions()
        mine = {" ".join(str(d["question"]).split()) for d in own}
        decisions = [d for d in self.answers["decisions"] if " ".join(str(d["question"]).split()) not in mine]
        if not (answers or decisions or own or self.args.get("summary")):
            self.step("answers", "skipped", "no answers given and no choices from the user to record")
            return True
        counts = {"done": 0, "already": 0, "failed": 0}
        failures: List[str] = []
        about = self._summary_answer()
        if about is not None:
            counts[about[0]] += 1
            if about[0] == "failed":
                failures.append(about[1])
        last = self._last_statuses()
        for item in answers:
            q = item["q"].strip()
            status = item.get("status") or "answered"
            have = last.get(q)
            # an answer (or n/a) is never undone; a skip or a later is replayed once it is answered
            if have in ("answered", "na") or have == status:
                counts["already"] += 1
                continue
            obj = run_onto(self.target, self._answer_argv(item, q, status))
            if obj.get("exit_code"):
                counts["failed"] += 1
                failures.append("%s: %s" % (q, _message(obj)))
            else:
                counts["done"] += 1
                last[q] = status
        active = self._active_decisions()
        for item in list(decisions) + own:
            state, message = self._decide(item, active)
            counts[state] += 1
            if state == "failed":
                failures.append("decision %r: %s" % (item["question"], message))
        if counts["done"]:
            self.wrote = True
        parts = ["%d answer%s" % (len(answers), "" if len(answers) == 1 else "s")]
        if about is not None:  # the summary is an answer too (to q.gap.thin on the root node): count it
            parts.append("1 summary")
        parts.append("%d decision%s" % (len(decisions) + len(own), "" if len(decisions) + len(own) == 1 else "s"))
        detail = "%d recorded, %d already there (%s)" % (counts["done"], counts["already"], ", ".join(parts))
        if failures:
            self.step("answers", "failed", detail + "; failed: " + "; ".join(failures))
        else:
            self.step("answers", "done" if counts["done"] else "already", detail)
        return True

    def _retire_answers_file(self) -> None:
        """After a run with no failed step, move ``.onto/setup.json`` aside (``.onto/setup.<name>.done.json``), so a
        later session in the kit checkout starts a new interview instead of resuming this topic's answers. Only a
        file in a ``.onto`` folder is moved."""
        path = getattr(self.ctx, "answers_path", None)
        if not path or os.path.basename(os.path.dirname(path)) != ".onto" or not os.path.isfile(path):
            return
        if self.personal_note:
            # the --personal choice is still to apply (a hand edit of policy.personal with the user's yes), and the
            # same command, run again, records it from this file
            for s in self.steps:
                if s["id"] == "answers":
                    s["detail"] += "; kept %s until the --personal choice is applied" % os.path.basename(path)
            return
        done = os.path.join(os.path.dirname(path), "setup.%s.done.json" % self.name)
        redacted = self._redacted_answers(path)
        try:
            if redacted is None:
                os.replace(path, done)
            else:
                # the file holds the user's raw words, kept outside the topic where onto erase cannot reach: under a
                # policy that redacts or refuses personal data, the copy kept is redacted the same way
                store.write_bytes(done, (json.dumps(redacted, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
                os.remove(path)
        except OSError:
            return
        for s in self.steps:
            if s["id"] == "answers":
                s["detail"] += "; moved %s to %s%s" % (os.path.basename(path), os.path.basename(done),
                                                       " (personal data redacted)" if redacted is not None else "")

    def _redacted_answers(self, path: str) -> Any:
        """The answers file with every string through the source sanitizer, personal data redacted wherever the
        topic's policy does not keep it, or None when the policy keeps everything (or the file cannot be read)."""
        from . import sources

        try:
            manifest = store.read_json(os.path.join(self.target, store.MANIFEST))
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return None
        policy = dict(store.DEFAULT_POLICY)
        policy.update(manifest.get("policy") if isinstance(manifest.get("policy"), dict) else {})
        personal = policy.get("personal") if isinstance(policy.get("personal"), dict) else {}
        if not personal or all(v == "keep" for v in personal.values()):
            return None
        policy["personal"] = {k: ("keep" if v == "keep" else "redact") for k, v in personal.items()}
        sanitizer = sources.default_sanitizer()

        def walk(value: Any) -> Any:
            if isinstance(value, str):
                try:
                    return sanitizer(value, policy)[0]
                except Exception:
                    return "[redacted]"
            if isinstance(value, list):
                return [walk(v) for v in value]
            if isinstance(value, dict):
                return {k: walk(v) for k, v in value.items()}
            return value

        return walk(data)

    def do_commit(self) -> bool:
        t = self.target
        unignored = [rel for rel in ("inbox/", ".onto/") if not gitutil.git_ok(t, "check-ignore", "-q", rel)[0]]
        if unignored:
            self.step("commit", "skipped", "not committed: %s not ignored by git (add them to .gitignore, then run "
                                           "onto setup again)" % " and ".join(unignored))
            return True
        # setup's own .claude/settings.json is the plugin step's to commit, in its own commit, and the agent files
        # are the agents step's
        dirty = [p for p in gitutil.dirty_paths(t)
                 if (p != SETTINGS_REL and p not in agents.WIRED_PATHS) or p in self.pre_dirty]
        # in the last commit, not just the index: a run whose commit failed after ``git add -A`` left it staged
        committed = gitutil.git_ok(t, "cat-file", "-e", "HEAD:%s" % store.MANIFEST)[0]
        if not dirty:
            self.step("commit", "already", "nothing to commit")
            return True
        # this run wrote something, or an earlier run did and its commit failed
        wrote = self.wrote or bool(set(self.own_left) & set(dirty))
        if committed and not wrote:
            self.step("commit", "skipped", "%d uncommitted file%s setup did not write; commit them yourself" % (
                len(dirty), "" if len(dirty) == 1 else "s"))
            return True
        others = sorted(self.pre_dirty) if committed else []
        if others:
            # never sweep the user's own uncommitted work into setup's commit
            shown = ", ".join(others[:3]) + (", ..." if len(others) > 3 else "")
            self.step("commit", "skipped", "not committed: %d file%s were uncommitted before setup ran (%s); show "
                                           "git status --short and commit with the user's yes" % (
                                               len(others), "" if len(others) == 1 else "s", shown))
            return True
        message = "Start %s" % self.name if not committed else "Record the setup of %s" % self.name
        left: List[str] = []
        if not committed:
            # only the topic's files setup wrote: anything else in the folder (notes, drafts, a .env) is the
            # user's, and so is every file that was uncommitted before setup ran, in the topic folders too (a
            # sources/notes.txt, an edit to a kit file or .gitignore)
            pre = set(self.pre_dirty)
            paths = [p for p in dirty if p not in pre and self._setup_path(p)]
            left = [p for p in dirty if p not in paths]
        else:
            paths = list(dirty)
        self._record_written()  # before git runs: a failed commit or a stopped run leaves the record for a rerun
        if not paths:
            self.step("commit", "skipped", "not committed: %s" % self._left_text(left))
            return True
        leaks = self._credential_hits(paths)
        if leaks:
            # the user takes the text out as told, so the rerun commits these files although their bytes changed
            self.held = sorted(set(self.held) | {f for f, _k in leaks})
            self._record_written()
            self.step("commit", "skipped", "not committed: %s hold%s credential-like text (%s); take it out, then "
                                           "run onto setup again" % (
                                               ", ".join(f for f, _k in leaks[:3]), "s" if len(leaks) == 1 else "",
                                               ", ".join(sorted({k for _f, k in leaks}))))
            return True
        ok, out = run_git(t, "--literal-pathspecs", "add", "-A", "--", *paths)
        if ok:
            # --only: what the user staged before setup ran never rides along
            ok, out = run_git(t, "--literal-pathspecs", "commit", "-q", "-m", message, "--only", "--", *paths)
        if not ok:
            if gitutil.identity_problem(t):
                fix = "; %s, then run onto setup again" % gitutil.IDENTITY_FIX
            else:
                fix = "; %s, then run onto setup again" % (commit_fix(out, t) or "fix it")
            self.step("commit", "failed", "git commit failed: %s%s" % (git_failure(out), fix))
            return True
        self.step("commit", "done", "committed %r%s" % (message, "; not committed: %s" % self._left_text(left)
                                                         if left else ""))
        return True

    def _setup_path(self, rel: str) -> bool:
        """True for a place setup's first commit takes files from: ``ontology.json``, the topic's folders, and the
        git rules files init writes. do_commit takes only the ones setup wrote (never a path in ``pre_dirty``)."""
        from . import mutate

        rel = rel.replace(os.sep, "/")
        if rel in (store.MANIFEST, ".gitignore", ".gitattributes"):
            return True
        top = rel.split("/", 1)[0]
        return top in store.TOPIC_DIRS or top in {d.split("/", 1)[0] for d in mutate.EXTRA_DIRS}

    def _left_text(self, left: List[str]) -> str:
        changed = [p for p in left if p in self.changed_since]
        others = [p for p in left if p not in changed]
        parts = []
        for group, what in ((others, "setup did not write"), (changed, "changed since setup wrote them")):
            if group:
                shown = ", ".join(group[:3]) + (", ..." if len(group) > 3 else "")
                parts.append("%d file%s %s (%s)" % (len(group), "" if len(group) == 1 else "s", what, shown))
        return "%s; show git status --short and commit them with the user's yes" % "; ".join(parts)

    def _credential_hits(self, paths: List[str]) -> List[Tuple[str, str]]:
        """(path, kind) for each file about to be committed that holds secret-like or credential-like text (the
        rule ``onto scan`` and a release apply)."""
        from . import secrets

        hits: List[Tuple[str, str]] = []
        cache: Dict[str, bool] = {}
        for rel in paths:
            path = os.path.join(self.target, *rel.split("/"))
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            for kind, _prefix in secrets.scan_file(path, data, cache):
                hits.append((rel, kind))
                break
        return hits

    # plugin
    def do_plugin(self) -> bool:
        mode = self.plugin_mode
        if self.plugin_from == "agents":
            self.step("plugin", "skipped", "Claude Code is not one of the agents (%s)" % ", ".join(self.agents))
            return True
        why = {"flag": "--plugin", "state": "the mode an earlier setup run here used"}.get(
            self.plugin_from, "the recorded setup decision")
        if mode is not None:
            self._write_state({"plugin": mode})  # a rerun without --plugin tries the same mode again
        if mode is None:
            wiring = doctor.settings_wiring(self.target)
            if (wiring["project"] or wiring["local"]) and not wiring["folder_source"]:
                self.step("plugin", "already", "wired in .claude/%s" % (
                    "settings.json" if wiring["project"] else "settings.local.json"))
            else:
                self.effective_plugin = "plugin-dir"
                self.step("plugin", "skipped", "not wired, and this run was not asked to (no --plugin and no "
                                               "recorded choice); ask the user, then pass --plugin project, or start "
                                               "claude --plugin-dir %s" % PLUGIN_DIR_REL)
            return True
        if mode == "skip":
            self.step("plugin", "skipped", "--plugin skip" if self.plugin_from == "flag" else
                      "skip, as %s says%s" % (why, "; to wire it, ask the user, then pass --plugin project"
                                              if self.plugin_from == "state" else ""))
            return True
        if self.plugin_fallback:
            self.step("plugin", "skipped", self.plugin_fallback)
            return True
        if mode == "plugin-dir":
            self.step("plugin", "skipped", "no install (%s): start with claude --plugin-dir %s" % (
                why, PLUGIN_DIR_REL) if self.plugin_from != "flag" else
                "no install: start with claude --plugin-dir %s" % PLUGIN_DIR_REL)
            return True
        wiring = doctor.settings_wiring(self.target)
        wired = wiring["project"] if mode == "project" else wiring["local"]
        if wired and not wiring["folder_source"]:
            status, detail = "already", "wired in .claude/%s" % (
                "settings.json" if mode == "project" else "settings.local.json")
        elif self.claude:
            status, detail = self._claude_install(mode)
            if status == "done" and not doctor.settings_wiring(self.target)[mode]:
                # claude said yes but left the settings file without the plugin entries (an older or different
                # claude): write them as setup does without claude, so the folder is wired for whoever trusts it
                wrote, why = self._write_settings(mode)
                if wrote == "failed":
                    status, detail = "failed", "%s, but %s" % (detail, why)
                else:
                    detail += "; claude left .claude/%s without the plugin entries, so setup wrote them" % (
                        "settings.json" if mode == "project" else "settings.local.json")
        else:
            status, detail = self._write_settings(mode)
        if mode == "project":
            self._record_settings()  # even after a failure: marketplace add may have written it already
        if status != "failed" and mode == "project":
            detail += self._commit_settings()
        if status == "failed":
            self.effective_plugin = "plugin-dir"  # the topic is ready; the launch and Next lines run it in place
        self.step("plugin", status, detail)
        return True

    def _claude(self, *args: str) -> Tuple[bool, str]:
        try:
            proc = subprocess.run([self.claude] + list(args), cwd=self.target, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=CLAUDE_TIMEOUT,
                                  env=claude_env())
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        return proc.returncode == 0, proc.stdout.decode("utf-8", "replace").strip()

    def _claude_install(self, mode: str) -> Tuple[str, str]:
        source = "%s#%s" % (self.kit_url, TEMPLATE_BRANCH)
        progress("Installing the plugin with claude (this needs the network; each claude call may take up to %d "
                 "minutes)..." % (CLAUDE_TIMEOUT // 60))
        if mode == "project":
            ok, out = self._claude("plugin", "marketplace", "add", source, "--scope", "project")
        else:
            listed = self.listed if self.listed is not None else doctor.claude_marketplace(self.claude)
            ok, out = (True, "") if listed and listed.get("found") else self._claude(
                "plugin", "marketplace", "add", source)
        if not ok:
            return "failed", "claude plugin marketplace add failed: %s; %s" % (
                out.splitlines()[-1] if out else "no output", self._install_fix())
        ok, out = self._claude("plugin", "install", PLUGIN_KEY, "--scope", mode)
        if not ok:
            return "failed", "claude plugin install failed: %s; %s" % (
                out.splitlines()[-1] if out else "no output", self._install_fix())
        return "done", "installed with claude (--scope %s) from %s" % (mode, display_url(source))

    def _install_fix(self) -> str:
        """What to do when claude could not install the plugin: it fetches the kit over the network (a private
        template needs stored git credentials), so name that, and the way to go on without installing."""
        return ("claude fetches %s over the network, so check the connection (a private repo needs git credentials "
                "claude can use, for example gh auth setup-git), then run the same onto setup command again; or go "
                "on without installing: run it again with --plugin plugin-dir, or start claude --plugin-dir %s in "
                "the topic (the topic itself is ready)" % (display_url(self.kit_url), PLUGIN_DIR_REL))

    def _write_settings(self, mode: str) -> Tuple[str, str]:
        name = "settings.json" if mode == "project" else "settings.local.json"
        path = os.path.join(self.target, ".claude", name)
        data: Any = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                return "failed", ".claude/%s is not valid JSON (%s); fix it, then run onto setup again" % (name, exc)
            if not isinstance(data, dict):
                return "failed", ".claude/%s is not a JSON object" % name
        markets = data.get("extraKnownMarketplaces")
        enabled = data.get("enabledPlugins")
        if markets is not None and not isinstance(markets, dict) or enabled is not None and not isinstance(
                enabled, dict):
            return "failed", ".claude/%s: extraKnownMarketplaces and enabledPlugins must be objects" % name
        if doctor.url_credential(self.kit_url):  # plan strips or refuses these; never write one
            return "failed", "the kit URL holds a credential; pass --kit-url without it"
        markets = dict(markets or {})
        enabled = dict(enabled or {})
        markets[MARKETPLACE] = {"source": marketplace_source(self.kit_url or "")}
        enabled[PLUGIN_KEY] = True
        data["extraKnownMarketplaces"] = markets
        data["enabledPlugins"] = enabled
        os.makedirs(os.path.dirname(path), exist_ok=True)
        store.write_bytes(path, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        return "done", "wrote .claude/%s (claude is not on PATH; Claude Code offers the plugin when you trust the " \
                       "folder)" % name

    def _commit_settings(self) -> str:
        rel = SETTINGS_REL
        path = os.path.join(self.target, ".claude", "settings.json")
        if not os.path.isfile(path):
            return ""
        if rel not in gitutil.dirty_paths(self.target):
            return ""
        if rel in self.pre_dirty:
            return "; not committed: it had uncommitted changes before setup ran; commit it with the user's yes"
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            return "; not committed: %s" % exc
        from . import secrets

        kinds = sorted({kind for kind, _m in secrets.scan_bytes(data)} | set(secrets.credential_kinds(data)))
        if kinds:  # the file is shared with everyone who clones the repo
            return "; not committed: it holds credential-like text (%s); take it out first" % ", ".join(kinds)
        ok, out = run_git(self.target, "add", "--", rel)
        if ok:
            ok, out = run_git(self.target, "commit", "-q", "-m", "Wire the general-ontology plugin", "--", rel)
        if ok:
            return "; committed it"
        fix = commit_fix(out, self.target)
        return "; not committed: %s%s" % (git_failure(out), "; %s" % fix if fix else "")

    # agents
    def _agent_shas(self) -> Dict[str, str]:
        """``{path: sha256}`` of the agent files setup wrote in this folder (``agents_sha`` in the state)."""
        own = read_state(self.target).get("agents_sha") if self.target else None
        return {k: v for k, v in own.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(
            own, dict) else {}

    def do_agents(self) -> bool:
        """Write the files of every chosen agent but Claude Code (the plugin step wires it), merged into what is
        there, then commit the ones whose bytes setup wrote ("Wire the agents for <name>"). A file the user changed
        is left alone and named. The settings that live in an agent's own app become paste blocks."""
        others = [n for n in self.agents if n != "claude"]
        self.paste = agents.paste_blocks(others)
        if not others:
            self.step("agents", "skipped", "Claude Code only: the plugin step wires it")
            return True
        if not os.path.isfile(os.path.join(self.target, store.MANIFEST)):
            self.step("agents", "skipped", "no topic here yet")
            return True
        own = self._agent_shas()
        wrote, already, refused = [], [], []
        for name in others:
            for item in agents.apply(self.target, name, own):
                if item["status"] == "done":
                    wrote.append(item["path"])
                    if item["path"] not in self.pre_dirty:  # merged into the user's uncommitted file: theirs
                        own[item["path"]] = item["sha"]
                elif item["status"] == "already":
                    already.append(item["path"])
                else:
                    refused.append("%s (%s)" % (item["path"], item["detail"]))
        self._write_state({"agents_sha": own})
        parts = ["%s: %s" % (", ".join(others), "wrote %s" % ", ".join(wrote) if wrote else "nothing to write")]
        if already:
            parts.append("already there: %s" % ", ".join(already))
        commit = self._commit_agents(own)
        if commit:
            parts.append(commit)
        if refused:
            # the user's own version of a file is kept, not a failure: the run did all it may do
            parts.append("kept the user's version of %s; to take the kit's entries, merge them by hand (onto agents "
                         "show <name> prints them) or move the file aside, then run onto setup again" % "; ".join(
                             refused))
            self.step("agents", "done" if wrote or commit.startswith("committed") else "skipped", "; ".join(parts))
            return True
        self.step("agents", "done" if wrote or commit.startswith("committed") else "already", "; ".join(parts))
        return True

    def _commit_agents(self, own: Dict[str, str]) -> str:
        """Commit the agent files whose bytes are the ones setup wrote (this run or an earlier one), never a file
        that had the user's uncommitted changes before the run, one git ignores, or one with credential-like text.
        Returns what happened, or "" when there was nothing to commit."""
        t = self.target
        if not gitutil.git_ok(t, "rev-parse", "--verify", "-q", "HEAD")[0]:
            return "not committed: the topic has no first commit yet (the commit step says why)"
        dirty = set(gitutil.dirty_paths(t))
        paths = sorted(p for p in own if p in dirty and p in agents.WIRED_PATHS and p not in self.pre_dirty
                       and own[p] == self._file_sha(p))
        notes = []
        theirs = sorted(p for p in agents.WIRED_PATHS if p in dirty and p in self.pre_dirty)
        if theirs:
            notes.append("not committed: %s had uncommitted changes before setup ran; commit with the user's yes"
                         % ", ".join(theirs))
        ignored = [p for p in paths if gitutil.git_ok(t, "check-ignore", "-q", "--", p)[0]]
        if ignored:
            notes.append("not committed: git ignores %s" % ", ".join(ignored))
            paths = [p for p in paths if p not in ignored]
        leaks = self._credential_hits(paths)
        if leaks:
            notes.append("not committed: %s hold%s credential-like text; take it out first" % (
                ", ".join(f for f, _k in leaks), "s" if len(leaks) == 1 else ""))
            paths = [p for p in paths if p not in {f for f, _k in leaks}]
        if paths:
            message = "Wire the agents for %s" % self.name
            ok, out = run_git(t, "--literal-pathspecs", "add", "--", *paths)
            if ok:
                ok, out = run_git(t, "--literal-pathspecs", "commit", "-q", "-m", message, "--only", "--", *paths)
            if ok:
                notes.insert(0, "committed %r" % message)
            else:
                fix = commit_fix(out, t)
                notes.insert(0, "not committed: %s%s" % (git_failure(out), "; %s" % fix if fix else ""))
        return "; ".join(notes)

    # launch
    def launch_argv(self) -> List[str]:
        extra = ["--plugin-dir", PLUGIN_DIR_REL] if self.effective_plugin == "plugin-dir" else []
        return ["claude"] + extra + [START_PROMPT]

    def do_launch(self) -> bool:
        argv = self.launch_argv()
        if self.args.get("launch") == "none":
            self.step("launch", "skipped", "--launch none")
        elif not self._claude_chosen():
            self.step("launch", "skipped", "Claude Code is not one of the agents; open the folder in yours")
        elif not _interactive():
            self.step("launch", "skipped", "no terminal to start Claude Code in")
        elif not self.claude:
            self.step("launch", "skipped", "claude is not on PATH")
        else:
            self.exec_spec = {"argv": [self.claude] + argv[1:], "cwd": self.target}
            self.step("launch", "done", "starting Claude Code in %s" % self.target)
        return True

    def _next(self, failed: bool = False) -> List[str]:
        cd = doctor.cd_command(self.target)
        command = " ".join(doctor.shell_quote(a) for a in self.launch_argv())
        failed_ids = [s["id"] for s in self.steps if s["status"] == "failed"]
        if failed and failed_ids == ["plugin"] and os.path.isfile(os.path.join(self.target, store.MANIFEST)):
            # the topic is made and committed; only the install failed, so it runs without installing for now
            return ["The plugin was not installed (the plugin step says why and how to install it later); the topic "
                    "is ready and runs without installing", "Folder: %s" % self.target,
                    "Open it in Claude Code: in a terminal, %s && %s" % (cd, command),
                    "Codex or another agent: open the folder and ask the agent to follow AGENTS.md"]
        if failed:
            # never point at a folder a failed run did not make: fix first, then the same command does the rest
            first = ("Fix the failed step above (its detail says how), then run the same onto setup command again: "
                     "it does only what is missing")
            if not os.path.isfile(os.path.join(self.target, store.MANIFEST)):
                return [first]
            return [first, "Folder: %s" % self.target]
        lines = ["Folder: %s" % self.target]
        if self._claude_chosen():
            if self.effective_plugin == "plugin-dir":
                lines.append("Open it in Claude Code: in a terminal, %s && %s" % (cd, command))
            else:
                lines.append(agents.harness("claude")["open"])
        # each other chosen agent: how to open the topic there (its paste blocks follow the Next lines)
        chosen = getattr(self, "agents", None) or ["claude"]
        # an agent that clones the topic from a git host (Devin) finds nothing there until the topic is pushed
        no_origin = "origin" not in doctor._remotes(self.target)
        quoted = doctor.shell_quote(self.target)
        for n in chosen:
            if n in ("claude", "generic"):
                continue
            h = agents.harness(n)
            if no_origin and h.get("needs_origin"):
                lines.append(h["needs_origin"] % (quoted, quoted))
            lines.append(h["open"])
        if getattr(self, "paste", None):
            lines.append("Paste the blocks below into the agents' own settings (they cannot live in the repo)")
        lines.append("Codex or another agent: open the folder and ask the agent to follow AGENTS.md"
                     if "codex" not in chosen else agents.harness("generic")["open"])
        return lines


def claude_env() -> Dict[str, str]:
    """The environment ``claude`` runs in: without the launchers' folder hint and without the handoff guard (a
    setup handed off to the topic's kit sets it, and the plugin's hook and MCP server must hand off again)."""
    env = dict(os.environ)
    env.pop(CALLER_CWD, None)
    env.pop(handoff.GUARD, None)
    return env


def cmd_setup(ctx: Context, args: Dict[str, Any]) -> Dict[str, Any]:
    setup = Setup(ctx, args)
    setup.plan()
    return setup.run()


def render_setup(result: Dict[str, Any], mode: str, ctx: Context) -> List[str]:
    topic = result.get("topic") or {}
    # every detail on one line: a URL or a git message with a line break never adds lines of its own
    lines = ["setup %s (ns %s) at %s" % (topic.get("name"), topic.get("ns"), one_line(topic.get("path")))]
    for s in result.get("steps") or []:
        lines.append("%-8s %-10s %s" % (s["status"], s["id"], one_line(s["detail"])))
    if result.get("next"):
        lines += ["Next:"] + ["  %s" % one_line(line) for line in result["next"]]
    for block in result.get("paste") or []:
        lines.append("Paste, %s:" % one_line(block.get("title")))
        lines += ["    %s" % one_line(line) for line in str(block.get("text") or "").split("\n")]
    return lines


_LINE_BREAKING_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def one_line(text: Any) -> str:
    """``text`` with every control character and line separator made a space, so it prints as one line (and a
    path keeps its own spaces)."""
    return _LINE_BREAKING_RE.sub(" ", "" if text is None else str(text))


def launch(spec: Dict[str, Any]) -> Optional[int]:
    """Start Claude Code in the topic (the CLI calls it after printing the checklist). On POSIX the process becomes
    Claude Code and this returns only on failure (None). On Windows ``os.exec*`` starts a new process and ends this
    one at once, so the double-click launcher's ``pause`` would share the console with Claude Code; there Claude Code
    runs as a child in the same console (an npm ``claude.cmd`` included) and its exit code is returned."""
    os.environ.pop(CALLER_CWD, None)  # the launchers' folder hint is for setup only
    # a setup handed off to the topic's kit set the handoff guard; the session must hand off again on its own
    os.environ.pop(handoff.GUARD, None)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        os.chdir(spec["cwd"])
        if _windows():
            return int(_call(list(spec["argv"]), cwd=spec["cwd"]))
        _execvp(spec["argv"][0], list(spec["argv"]))
    except OSError as exc:
        sys.stderr.write("[onto] could not start claude: %s\n" % exc)
    return None
