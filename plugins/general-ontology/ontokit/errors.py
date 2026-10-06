"""The kit's error hierarchy and the ``Problem`` record.

Every domain failure is an ``OntoError`` with a machine ``kind``, a process exit ``code`` and extra JSON fields.
``to_json()`` is what the CLI prints under ``--json`` and what the MCP server returns in an error result.

``Problem`` is the one record type for validation findings (``file:line: CODE message``). It lives here, at the
bottom layer, so ``store``, ``packs`` and ``validate`` all return the same type.
"""

from __future__ import annotations

from collections import namedtuple
from typing import Any, Dict, Iterable, Optional


class Problem(namedtuple("Problem", "code file line message")):
    """One validation finding. ``line`` is 0 when the finding is about a whole file."""

    __slots__ = ()

    def text(self) -> str:
        where = "%s:%d" % (self.file, self.line) if self.line else str(self.file)
        return "%s: %s %s" % (where, self.code, self.message)

    def to_json(self) -> Dict[str, Any]:
        return {"code": self.code, "file": self.file, "line": self.line, "message": self.message}


class OntoError(Exception):
    """Base class. ``kind`` names the failure, ``code`` is the exit code, ``extra`` goes into the JSON form."""

    def __init__(self, message: str, kind: str = "error", code: int = 1, **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.code = code
        self.extra = extra

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"error": self.kind, "message": self.message}
        for key, value in self.extra.items():
            out[key] = _jsonable(value)
        return out

    def __str__(self) -> str:
        return self.message


def _jsonable(value: Any) -> Any:
    if isinstance(value, Problem):
        return value.to_json()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


class NotFound(OntoError):
    """An id or text did not resolve. ``ambiguous`` means several candidates matched equally well."""

    def __init__(
        self, text: str, candidates: Iterable[Any] = (), ambiguous: bool = False, searched: Optional[Any] = None
    ) -> None:
        self.text = text
        self.candidates = list(candidates)
        self.ambiguous = bool(ambiguous)
        self.searched = searched
        extra: Dict[str, Any] = {"candidates": self.candidates}
        if searched is not None:
            extra["searched"] = searched
        super().__init__(text, "ambiguous" if ambiguous else "not_found", 1, **extra)


class UsageError(OntoError):
    """Bad arguments or input shape (exit 2)."""

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message, "usage", 2, **extra)


class Refused(OntoError):
    """The kit refuses the request; ``problems`` says why."""

    def __init__(self, message: str, problems: Iterable[Any] = (), **extra: Any) -> None:
        self.problems = list(problems)
        super().__init__(message, "refused", 1, problems=self.problems, **extra)


class Conflict(OntoError):
    """The data changed under a proposal: ``ops`` lists the op numbers whose expectations no longer hold."""

    def __init__(self, message: str, ops: Iterable[Any] = (), **extra: Any) -> None:
        self.ops = list(ops)
        super().__init__(message, "conflict", 1, ops=self.ops, **extra)


class DataError(OntoError):
    """Data on disk is missing, unreadable or inconsistent."""

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message, "data", 1, **extra)


class GitError(OntoError):
    """A git call failed or was refused (for example a ref that is not a tag or a commit id)."""

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message, "git", 1, **extra)


class LockBusy(OntoError):
    """Another writer holds ``.onto/lock``."""

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message, "lock_busy", 1, **extra)


class NotBuilt(OntoError):
    """The command's module is not part of this kit build (exit 3)."""

    def __init__(self, command: str, **extra: Any) -> None:
        self.command = command
        super().__init__("%s: not built in this kit" % command, "not_built", 3, command=command, **extra)
