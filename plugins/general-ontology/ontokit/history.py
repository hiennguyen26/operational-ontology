"""The richness history (``metrics/history.jsonl``): one point per line, append-only.

A point is ``{at, kind, label, values, denominators, breakdowns, missing}`` (C.14). A value that cannot be measured
is null, with the reason under ``missing``. ``append_point`` skips a point that measured the same as the last one
(values, denominators and breakdowns equal) unless it carries a new label, so the file grows only when something
changed. A 7 or 30 day change is measured from the point nearest that many days before the latest one, among the
points at least half the window older (``baseline``); the caller prints that point's date.

``snapshot_bytes`` and ``restore`` let a writer roll the history back when a later step of the same apply fails.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from . import records, store, util
from .errors import UsageError

HISTORY = "metrics/history.jsonl"
MEASURED = ("values", "denominators", "breakdowns")
WINDOWS = (("7d", 7), ("30d", 30))


class NotMeasurable(Exception):
    """A measure cannot be taken from the data; the message is the reason (it goes under ``missing``)."""


def path(repo: store.Repo) -> str:
    return repo.path(HISTORY)


def read(repo: store.Repo) -> List[Dict[str, Any]]:
    """Every readable point, in file order (oldest first). Bad lines are skipped here; ``validate`` reports them."""
    rows, _problems = store.read_jsonl(path(repo))
    return rows


def same_measurement(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Two points measured the same: values, denominators and breakdowns all equal."""
    return all((a.get(key) or {}) == (b.get(key) or {}) for key in MEASURED)


def new_point(kind: str, values: Dict[str, Any], denominators: Optional[Dict[str, Any]] = None,
              breakdowns: Optional[Dict[str, Any]] = None, missing: Optional[Dict[str, str]] = None,
              label: Optional[str] = None, at: Optional[str] = None) -> Dict[str, Any]:
    """A point with every field present, stamped ``util.now_iso()`` unless ``at`` is given."""
    return {
        "at": at or util.now_iso(),
        "kind": kind,
        "label": label,
        "values": dict(values),
        "denominators": dict(denominators or {}),
        "breakdowns": dict(breakdowns or {}),
        "missing": dict(missing or {}),
    }


def append_point(repo: store.Repo, point: Dict[str, Any]) -> bool:
    """Append ``point``; False when it is skipped because it measured the same as the last point and carries no
    new label. Raises ``UsageError`` when the point fails its schema."""
    errors = records.check(point, "point")
    if errors:
        raise UsageError("not a history point: %s" % "; ".join(errors[:3]), problems=errors)
    points = read(repo)
    last = points[-1] if points else None
    label = point.get("label")
    if last is not None and same_measurement(last, point) and (not label or last.get("label") == label):
        return False
    store.append_jsonl(path(repo), point)
    return True


def snapshot_bytes(repo: store.Repo) -> Optional[bytes]:
    """The history file's bytes, or None when it does not exist (for ``restore``)."""
    try:
        with open(path(repo), "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def restore(repo: store.Repo, data: Optional[bytes]) -> None:
    """Put the history file back as ``snapshot_bytes`` read it."""
    target = path(repo)
    if data is None:
        try:
            os.unlink(target)
        except FileNotFoundError:
            pass
        return
    store.write_bytes(target, data)


def merge_points(points: List[Dict[str, Any]], new: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Points plus ``new``, sorted by ``(at, kind)``; a new point replaces one with the same ``(at, kind)``."""
    keys = {(p.get("at"), p.get("kind")) for p in new}
    kept = [p for p in points if (p.get("at"), p.get("kind")) not in keys]
    return sorted(kept + list(new), key=lambda p: (str(p.get("at") or ""), str(p.get("kind") or "")))


def _when(text: Any) -> Optional[datetime]:
    try:
        return util.parse_ts(str(text))
    except ValueError:
        return None


def _value(point: Dict[str, Any], key: str) -> Any:
    return (point.get("values") or {}).get(key)


def series(points: List[Dict[str, Any]], key: str) -> List[Tuple[str, Any]]:
    """``[(at, value)]`` of ``key`` over the points with a value, oldest first."""
    ordered = sorted(points, key=lambda p: str(p.get("at") or ""))
    return [(str(p.get("at")), _value(p, key)) for p in ordered if _value(p, key) is not None]


def baseline(points: List[Dict[str, Any]], key: str, days: int) -> Optional[Dict[str, Any]]:
    """The point a ``days`` change is measured from, or None.

    Among the earlier points with a value that are at least half the window older than the latest point, the one
    nearest to ``days`` before it (on a tie, the older one). Points a week apart and a reading on another weekday
    then give a 7 day change over 6 to 8 days, not 7 to 13."""
    ordered = sorted(points, key=lambda p: str(p.get("at") or ""))
    if not ordered:
        return None
    now_at = _when(ordered[-1].get("at"))
    if now_at is None:
        return None
    target = now_at - timedelta(days=days)
    latest_allowed = now_at - timedelta(days=days / 2.0)
    best: Optional[Dict[str, Any]] = None
    best_gap: Optional[float] = None
    for point in ordered[:-1]:
        at = _when(point.get("at"))
        if at is None or at > latest_allowed or _value(point, key) is None:
            continue
        gap = abs((at - target).total_seconds())
        if best_gap is None or gap < best_gap:  # oldest first, so a tie keeps the older point
            best, best_gap = point, gap
    return best


def change(points: List[Dict[str, Any]], key: str, days: int) -> Optional[Tuple[Any, str]]:
    """``(latest value minus the baseline value, the baseline's at)``, or None when either is missing."""
    ordered = sorted(points, key=lambda p: str(p.get("at") or ""))
    if not ordered:
        return None
    now_value = _value(ordered[-1], key)
    base = baseline(ordered, key, days)
    if now_value is None or base is None:
        return None
    delta = now_value - _value(base, key)
    if isinstance(delta, float):
        delta = round(delta, 6)
    return delta, str(base.get("at"))


def _num(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return "{:,}".format(value) if isinstance(value, int) else str(value)


def fmt_change(delta: Any, at: Any = None) -> str:
    """``+9 since 09-21``, ``-2 since 09-21``, ``+0 since 09-21``; ``-`` when there is no change to report."""
    if delta is None:
        return "-"
    text = ("+" if delta >= 0 else "") + _num(delta)
    return text + " since " + str(at)[5:10] if at else text
