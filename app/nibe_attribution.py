"""
nibe_attribution.py
===================
Pure decisions for linking points that appear in a bulk poll to the switch
or select whose change made them appear (its dynamic points).

Nothing in this module performs I/O, holds state, reads the clock, or
imports from the rest of the bridge. EntityManager gathers the inputs from
its own state (under its locks), calls these, and applies the result.

Public surface
--------------
poll_verdict(...)               → "ok" | "skip" | "block"
version_changed(...)            → bool
changed_candidates(...)         → list[(point_id, old, new)]
controller_side_decision(...)   → ControllerSide | None
late_arrival_decision(...)      → LateArrival
learning_outcome(...)           → "record" | "learned" | "skip"
late_tail_until(...)            → float
recordable_value(...)           → int | None
ScanWindow, LateTail            — the window and late-arrival state, as values
"""

from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Literal

PollVerdict = Literal["ok", "skip", "block"]
LearningOutcome = Literal["record", "learned", "skip"]


def poll_verdict(
    *,
    discovery_complete: bool,
    published: AbstractSet[int],
    now: float,
    blocked_until: float,
    other_write_pending: bool,
    served: AbstractSet[int],
    known_dynamic: AbstractSet[int],
    versions_changed: bool,
) -> PollVerdict:
    """Whether points appearing in a poll may be linked to a switch changed
    before it.

    "skip": not now — before initial discovery, while attribution is blocked,
    or with another HA write pending or queued (that write opens its own
    window). "block": a previously served point is missing — other than a
    known dynamic one — or a firmware version register changed: a restarting
    or updating controller, whose new points are the firmware's. The caller
    then blocks attribution for the absence grace and drops a late-arrival
    period. "ok": link away.
    """
    if not discovery_complete or not published:
        return "skip"
    if now < blocked_until:
        return "skip"
    if other_write_pending:
        return "skip"
    if published - served - known_dynamic or versions_changed:
        return "block"
    return "ok"


def version_changed(
    previous: Mapping[int, object],
    current: Mapping[int, object],
    version_points: Iterable[int],
) -> bool:
    """Whether a firmware version register previously served now reads
    differently (a missing one counts as changed)."""
    return any(current.get(pid) != previous[pid] for pid in version_points if pid in previous)


def changed_candidates(
    candidates: Iterable[tuple[int, bool]],
    previous: Mapping[int, int | None],
    current: Mapping[int, int | None],
) -> list[tuple[int, int, int]]:
    """The candidate switches and selects — ``(point_id, firmware_removed)``
    — whose value differs between the previous poll and this one, as
    ``(point_id, old, new)``. One not served in either poll, or without a
    value in either, hasn't changed."""
    changed: list[tuple[int, int, int]] = []
    for pid, firmware_removed in candidates:
        if firmware_removed:
            continue
        old = previous.get(pid)
        new = current.get(pid)
        if old is None or new is None or new == old:
            continue
        changed.append((pid, int(old), int(new)))
    return changed


@dataclass(frozen=True)
class ControllerSide:
    """What a switch or select changed on the controller itself calls for.

    ``open_window``: open a scan window to learn what ``new`` shows (record
    ``(point_id, new)`` as tried). Otherwise start a late-arrival period for
    ``(point_id, new)``."""

    point_id: int
    old: int
    new: int
    open_window: bool


def controller_side_decision(
    changed: list[tuple[int, int, int]],
    unprocessed: Mapping[int, AbstractSet[int]],
    tried: AbstractSet[tuple[int, int]],
) -> ControllerSide | None:
    """For an attributable poll: the one candidate changed on the controller,
    or None when it isn't exactly one — then which one shows what can't be
    told. A value not learned yet opens a window, once per (point, value) per
    run, so a switch the controller flips by itself can't keep polling at
    5 s. A learned or already tried value still links points that arrive
    late."""
    if len(changed) != 1:
        return None
    pid, old, new = changed[0]
    fresh = new in unprocessed.get(pid, set()) and (pid, new) not in tried
    return ControllerSide(pid, old, new, open_window=fresh)


@dataclass(frozen=True)
class LateArrival:
    """``keep_tail``: the late-arrival period goes on. ``new_points``: points
    to link to its switch now (the period then ends)."""

    keep_tail: bool
    new_points: list[int]


def late_arrival_decision(
    *,
    until: float,
    value: int,
    now: float,
    served: AbstractSet[int] | None,
    current_value: int | None,
    others_changed: bool,
    published: AbstractSet[int],
    baseline: AbstractSet[int],
    known_dynamic: AbstractSet[int],
) -> LateArrival:
    """Points that arrive after a switch's scan window ended, during its
    late-arrival period, ``(until, value)``.

    ``served`` is None when the poll isn't attributable (poll_verdict). The
    period ends when it expires, when the switch no longer has ``value``,
    when another candidate changed, or at its first use. Points are new when
    not served before, not indexed and not known dynamic.
    """
    if now > until:
        return LateArrival(keep_tail=False, new_points=[])
    if served is None:
        return LateArrival(keep_tail=True, new_points=[])
    if current_value != value or others_changed:
        return LateArrival(keep_tail=False, new_points=[])
    new = sorted(served - published - baseline - known_dynamic)
    if not new:
        return LateArrival(keep_tail=True, new_points=[])
    return LateArrival(keep_tail=False, new_points=new)


def learning_outcome(
    *,
    unprocessed: bool,
    saw_points: bool,
    blocked: bool,
) -> LearningOutcome:
    """What the end of a learning window means for the value written.

    "learned": recorded during the window already. "record": no point
    appeared, so record that it shows nothing (plus the switch's known
    points still present). "skip": the window proves nothing — points
    appeared but weren't recorded under this value, or attribution was
    blocked — so the value is learned on a later write.
    """
    if not unprocessed:
        return "learned"
    if saw_points or blocked:
        return "skip"
    return "record"


def recordable_value(window_value: int | None, current_value: int | None) -> int | None:
    """The value to record points appearing in a window under, or None to
    record nothing: only when the window knows the value its switch was set
    to and the controller still reports it. The REST API's point list lags
    up to a minute behind its values, so points a switch showed for one
    value can first appear when it already reads another."""
    if window_value is None or current_value != window_value:
        return None
    return window_value


def late_tail_until(now: float, after: float, late_arrival_s: float, bulk_interval: float) -> float:
    """When a late-arrival period ends: ``after`` seconds (a window still to
    run), then the period itself, plus one poll interval so a poll at its
    edge still counts."""
    return now + after + late_arrival_s + bulk_interval


@dataclass(frozen=True)
class ScanWindow:
    """An open post-write scan window: points appearing while it lasts are
    linked to ``point``. Replaced whole, never changed in place, so a reader
    always sees one consistent window.

    ``value``: the value the switch was set to; None when not known, or when
    two writes in the window set it to different values — points appearing
    then can't be told apart and nothing is recorded (recordable_value).
    ``controller_side``: opened by a change on the controller's own display.
    ``learning_value``: the value an HA write set, when it wasn't learned yet;
    its outcome is recorded when the window ends. ``saw_points``: points
    appeared during the window."""

    point: int | None
    until: float
    controller_side: bool = False
    learning_value: int | None = None
    saw_points: bool = False
    value: int | None = None


@dataclass(frozen=True)
class LateTail:
    """A late-arrival period: new points are still linked to ``point`` at
    ``value`` until ``until`` (late_arrival_decision)."""

    point: int
    value: int
    until: float
    controller_side: bool
