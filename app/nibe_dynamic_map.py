"""
nibe_dynamic_map.py
===================
DynamicPointMap — replaces the flat ``known_dynamic_points`` set with a
rich causal table that records, for every writable switch and select point,
whether writing a specific value causes dynamic points to appear or disappear
in the firmware's bulk fetch.

This enables:
- Near-instantaneous activation/deactivation of known dynamic points via
  single-point probing (bypasses the bulk fetch cycle entirely).
- Permanent fast-pathing of non-controlling switches/selects (no detection
  overhead after first observation).
- Clean startup reconciliation that self-heals after firmware updates.
- A learning mode for confident causal discovery under controlled conditions.

Public surface
--------------
DynamicPointEntry           — dataclass, one row in the table
DynamicPointMap             — the table itself; load/save, lookup, update

The module has NO I/O of its own — all persistence calls are delegated to
the caller (EntityManager).  The only external imports are from the standard
library and nibe_utils.
"""

import json
import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger('nibe.dynamic_map')


# ============================================================================
# DATA MODEL
# ============================================================================

@dataclass
class DynamicPointEntry:
    """One row in the DynamicPointMap table.

    Represents what the bridge has learned about a single writable
    switch or select point's relationship to dynamic firmware points.

    Attributes
    ----------
    point_id :
        variableId of the controlling switch or select.
    title :
        Human-readable title from the firmware (after clean_string).
    entity_type :
        ``'switch'`` or ``'select'``.
    processed_values :
        Values for which discovery has been run and the outcome recorded.
    unprocessed_values :
        Values that have not yet been through a discovery cycle.
        Together with ``processed_values`` these cover all possible
        values for this point.  For a switch: ``{0, 1}``.  For a select:
        all valid option indices from firmware metadata.
        A point is **fully processed** when ``unprocessed_values`` is empty.
    is_controlling :
        ``True``  — at least one value caused dynamic points to appear.
        ``False`` — all processed values produced no dynamic points.
        ``None``  — not yet processed for any value.
    dynamic_points_by_value :
        Maps each processed integer value to the list of point_ids that
        appeared in the bulk fetch when that value was written.  An empty
        list means the value was observed and produced no dynamic points.
    firmware_removed :
        Set to True when this point no longer appears in the bulk fetch
        (e.g. removed by a firmware update).  Suppresses probe attempts.
        Entry is retained for historical reference.
    """

    point_id:                int
    title:                   str
    entity_type:             str                      # 'switch' | 'select'
    processed_values:        set[int]                 = field(default_factory=set)
    unprocessed_values:      set[int]                 = field(default_factory=set)
    is_controlling:          bool | None              = None
    dynamic_points_by_value: dict[int, list[int]]     = field(default_factory=dict)
    firmware_removed:        bool                     = False

    # ------------------------------------------------------------------
    # Convenience queries
    # ------------------------------------------------------------------

    def is_fully_processed(self) -> bool:
        """Return True when every possible value has been through discovery."""
        return len(self.unprocessed_values) == 0 and len(self.processed_values) > 0

    def dynamic_points_for_value(self, value: int) -> list[int] | None:
        """Return the dynamic point_ids for a specific value, or None if unprocessed."""
        return self.dynamic_points_by_value.get(value)

    def all_known_dynamic_points(self) -> set[int]:
        """Return the union of all dynamic point_ids across all values."""
        result: set[int] = set()
        for pts in self.dynamic_points_by_value.values():
            result.update(pts)
        return result

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialise to a JSON-compatible dict."""
        return {
            'point_id':                self.point_id,
            'title':                   self.title,
            'entity_type':             self.entity_type,
            'processed_values':        sorted(self.processed_values),
            'unprocessed_values':      sorted(self.unprocessed_values),
            'is_controlling':          self.is_controlling,
            'dynamic_points_by_value': {
                str(k): v
                for k, v in self.dynamic_points_by_value.items()
            },
            'firmware_removed':        self.firmware_removed,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'DynamicPointEntry':
        """Deserialise from a dict produced by ``to_dict``."""
        return cls(
            point_id                = int(d['point_id']),
            title                   = str(d.get('title', '')),
            entity_type             = str(d.get('entity_type', 'switch')),
            processed_values        = set(int(v) for v in d.get('processed_values', [])),
            unprocessed_values      = set(int(v) for v in d.get('unprocessed_values', [])),
            is_controlling          = d.get('is_controlling'),
            dynamic_points_by_value = {
                int(k): list(v)
                for k, v in d.get('dynamic_points_by_value', {}).items()
            },
            firmware_removed        = bool(d.get('firmware_removed', False)),
        )


# ============================================================================
# TABLE
# ============================================================================

_FILE_FALLBACK = '/data/dynamic_point_map.json'


class DynamicPointMap:
    """The full causal table of switch/select → dynamic point relationships.

    Keyed by ``point_id`` (int).  Wraps a ``dict[int, DynamicPointEntry]``
    with load/save, population, and lookup helpers.

    Persistence strategy
    --------------------
    The table is persisted to two stores (write-through):

    1. MQTT retained topic ``nibe/browser/dynamic_point_map`` (primary).
    2. ``/data/dynamic_point_map.json`` (filesystem fallback).

    Actual I/O is delegated to the caller — this class provides
    ``serialise()`` / ``deserialise()`` and ``to_file()`` / ``from_file()``.
    The EntityManager calls these at the right points in its lifecycle.
    """

    def __init__(self) -> None:
        self._table: dict[int, DynamicPointEntry] = {}

    # ------------------------------------------------------------------
    # Dict-like access
    # ------------------------------------------------------------------

    def __contains__(self, point_id: int) -> bool:
        return point_id in self._table

    def __getitem__(self, point_id: int) -> DynamicPointEntry:
        return self._table[point_id]

    def __len__(self) -> int:
        return len(self._table)

    def get(self, point_id: int, default=None) -> DynamicPointEntry | None:
        return self._table.get(point_id, default)

    def values(self):
        return self._table.values()

    def items(self):
        return self._table.items()

    # ------------------------------------------------------------------
    # Lookup helpers used by EntityManager
    # ------------------------------------------------------------------

    def is_known_dynamic(self, point_id: int) -> bool:
        """Return True if point_id appears as a dynamic point in any entry."""
        for entry in self._table.values():
            if point_id in entry.all_known_dynamic_points():
                return True
        return False

    def all_known_dynamic_point_ids(self) -> set[int]:
        """Return the union of all known dynamic point_ids across all entries."""
        result: set[int] = set()
        for entry in self._table.values():
            result.update(entry.all_known_dynamic_points())
        return result

    def controlling_entry_for_dynamic(self, dynamic_point_id: int) -> DynamicPointEntry | None:
        """Return the entry whose dynamic_points_by_value contains dynamic_point_id."""
        for entry in self._table.values():
            if dynamic_point_id in entry.all_known_dynamic_points():
                return entry
        return None

    def expected_active_dynamic_points(
        self,
        current_values: dict[int, int],
    ) -> set[int]:
        """Derive the expected active dynamic point set from current bulk fetch values.

        Parameters
        ----------
        current_values :
            Mapping of ``point_id → current_raw_value`` from the latest bulk fetch.
            Only controlling entries whose point_id is present here are evaluated.

        Returns
        -------
        The union of all dynamic point_ids that should currently be active
        given the current values of their controlling switches/selects.
        """
        active: set[int] = set()
        for entry in self._table.values():
            if not entry.is_controlling or entry.firmware_removed:
                continue
            current_value = current_values.get(entry.point_id)
            if current_value is None:
                continue
            pts = entry.dynamic_points_for_value(current_value)
            if pts:
                active.update(pts)
        log.debug("DynamicPointMap: expected active set = %d points", len(active))  # pragma: no mutate
        return active

    # ------------------------------------------------------------------
    # Population
    # ------------------------------------------------------------------

    def populate_from_bulk(
        self,
        all_points_by_id: dict[int, dict],
        entity_types: dict[int, str],
    ) -> int:
        """Add unprocessed skeleton entries for all writable switches and selects
        not yet in the table.

        Called at startup (first run: table is empty, all entries created)
        and after firmware updates (new switches/selects discovered in bulk
        fetch are added incrementally).

        Parameters
        ----------
        all_points_by_id :
            The EntityManager's full point index, keyed by variableId.
        entity_types :
            Mapping of ``point_id → entity_type`` string as resolved by
            detect_entity_type.  Only ``'switch'`` and ``'select'`` entries
            are added.

        Returns
        -------
        Number of new entries added.
        """
        added = 0
        for point_id, point in all_points_by_id.items():
            etype = entity_types.get(point_id)
            if etype not in ('switch', 'select'):
                continue
            if point_id in self._table:
                continue
            meta      = point.get('metadata', {})
            min_val   = meta.get('minValue', 0)
            max_val   = meta.get('maxValue', 1)
            all_vals: set[int] = set(range(min_val, max_val + 1))
            if not all_vals:
                all_vals = {0, 1}
            entry = DynamicPointEntry(
                point_id           = point_id,
                title              = point.get('display_title', f'Point {point_id}'),
                entity_type        = etype,
                processed_values   = set(),
                unprocessed_values = all_vals,
                is_controlling     = None,
                firmware_removed   = False,
            )
            self._table[point_id] = entry
            added += 1
        if added:
            log.debug("DynamicPointMap: added %d new skeleton entries", added)  # pragma: no mutate
        return added

    def mark_firmware_removed(self, point_id: int) -> None:
        """Mark a point as removed by a firmware update.

        Suppresses future probe attempts.  The entry is retained.
        """
        entry = self._table.get(point_id)
        if entry and not entry.firmware_removed:
            entry.firmware_removed = True
            log.debug("DynamicPointMap: point %d marked firmware_removed", point_id)  # pragma: no mutate

    def restore_from_bulk(self, bulk_point_ids: set[int]) -> None:
        """Clear firmware_removed for points that have reappeared in a bulk fetch.

        Handles the (unlikely) case where a point disappears and reappears
        across firmware updates.
        """
        for point_id, entry in self._table.items():
            if entry.firmware_removed and point_id in bulk_point_ids:
                entry.firmware_removed = False
                log.debug("DynamicPointMap: point %d restored (reappeared in bulk)", point_id)  # pragma: no mutate

    # ------------------------------------------------------------------
    # Recording learning outcomes
    # ------------------------------------------------------------------

    def record_outcome(
        self,
        point_id:      int,
        value:         int,
        new_point_ids: list[int],
    ) -> None:
        """Record the result of a learning-mode discovery cycle.

        Parameters
        ----------
        point_id :
            The controlling switch/select that was written.
        value :
            The integer value that was written.
        new_point_ids :
            List of dynamic point_ids that appeared after the write.
            Empty list means the write produced no dynamic points.
        """
        entry = self._table.get(point_id)
        if entry is None:
            log.warning(
                "DynamicPointMap.record_outcome: point %d not in table", point_id
            )  # pragma: no mutate
            return

        entry.processed_values.add(value)
        entry.unprocessed_values.discard(value)
        entry.dynamic_points_by_value[value] = list(new_point_ids)

        if new_point_ids:
            entry.is_controlling = True
            log.debug(
                "DynamicPointMap: recorded %d dynamic point(s) for controlling point %d (%s) value=%d",
                len(new_point_ids), point_id, entry.title, value,
            )  # pragma: no mutate
        else:
            # Non-controlling for this value — update is_controlling only
            # when the point is now fully processed and no value was controlling.
            if entry.is_controlling is None and not entry.unprocessed_values:
                all_empty = all(
                    len(pts) == 0
                    for pts in entry.dynamic_points_by_value.values()
                )
                if all_empty:
                    entry.is_controlling = False
            log.debug(
                "DynamicPointMap: value %d for point %d (%s) produced no dynamic points",
                value, point_id, entry.title,
            )  # pragma: no mutate

        # For switches (exactly 2 values), the inverse value is implied:
        # the dynamic points present when value=A are absent when value=B
        # and vice versa.  Mark the inverse as processed immediately so
        # learning mode does not run an unnecessary detection window for it.
        # For selects with >2 options, each value is independent — no inference.
        all_values = entry.processed_values | entry.unprocessed_values
        if len(all_values) == 2:
            for inverse_value in entry.unprocessed_values.copy():
                # Inverse: no dynamic points (they appear only on the observed value)
                entry.processed_values.add(inverse_value)
                entry.unprocessed_values.discard(inverse_value)
                entry.dynamic_points_by_value[inverse_value] = []
                log.debug(
                    "Learning: %s (point %d) value=%d inferred as inverse "
                    "(no dynamic points — 2-value switch)",
                    entry.title, point_id, inverse_value,
                )  # pragma: no mutate
            # Update is_controlling now that all values are processed
            if entry.is_controlling is None:
                entry.is_controlling = False

    # ------------------------------------------------------------------
    # Debug flush
    # ------------------------------------------------------------------

    def flush(self, all_points_by_id: dict[int, dict], entity_types: dict[int, str]) -> None:
        """Reset all entries to unprocessed state.

        Wipes processed_values, dynamic_points_by_value, and is_controlling
        for every entry.  Repopulates unprocessed_values from current bulk
        data.  Does NOT disturb currently active dynamic points in the
        EntityManager — those remain active until a probe or bulk fetch
        removes them.

        Debug use only.  Called by the flush management button when
        log_level == 'debug'.
        """
        log.warning("DynamicPointMap: FLUSH requested — resetting all entries to unprocessed")  # pragma: no mutate
        for entry in self._table.values():
            meta    = all_points_by_id.get(entry.point_id, {}).get('metadata', {})
            min_val = meta.get('minValue', 0)
            max_val = meta.get('maxValue', 1)
            all_vals: set[int] = set(range(min_val, max_val + 1))
            if not all_vals:
                all_vals = {0, 1}
            entry.processed_values        = set()
            entry.unprocessed_values      = all_vals
            entry.is_controlling          = None
            entry.dynamic_points_by_value = {}
        # Add any new entries that appeared since the table was built
        self.populate_from_bulk(all_points_by_id, entity_types)
        log.warning("DynamicPointMap: flush complete — %d entries reset", len(self._table))  # pragma: no mutate

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def serialise(self) -> str:
        """Serialise the full table to a JSON string for MQTT persistence."""
        payload = {
            str(pid): entry.to_dict()
            for pid, entry in self._table.items()
        }
        return json.dumps(payload)

    def deserialise(self, json_str: str) -> int:
        """Load the table from a JSON string.  Returns number of entries loaded.

        Merges into the existing table — does not clear first.  New entries
        from a fresh bulk populate are preserved; persisted entries overwrite
        skeleton entries.
        """
        try:
            raw = json.loads(json_str)
            if not isinstance(raw, dict):
                log.warning("DynamicPointMap.deserialise: expected dict, got %s", type(raw))  # pragma: no mutate
                return 0
            loaded = 0
            for pid_str, entry_dict in raw.items():
                try:
                    entry = DynamicPointEntry.from_dict(entry_dict)
                    self._table[entry.point_id] = entry
                    loaded += 1
                except Exception as e:
                    log.warning(
                        "DynamicPointMap: could not deserialise entry %s: %s", pid_str, e
                    )  # pragma: no mutate
            log.debug("DynamicPointMap: loaded %d entries from JSON", loaded)  # pragma: no mutate
            return loaded
        except json.JSONDecodeError as e:
            log.warning("DynamicPointMap.deserialise: JSON parse error: %s", e)  # pragma: no mutate
            return 0

    def to_file(self, path: str = _FILE_FALLBACK) -> bool:
        """Write the table to a JSON file.  Returns True on success."""
        try:
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:  # pragma: no mutate
                f.write(self.serialise())
            os.replace(tmp, path)
            log.debug("DynamicPointMap: saved to %s", path)  # pragma: no mutate
            return True
        except OSError as e:
            log.warning("DynamicPointMap: could not write to %s: %s", path, e)  # pragma: no mutate
            return False

    def from_file(self, path: str = _FILE_FALLBACK) -> int:
        """Load the table from a JSON file.  Returns number of entries loaded."""
        try:
            with open(path, 'r', encoding='utf-8') as f:  # pragma: no mutate
                data = f.read()
            count = self.deserialise(data)
            log.info("DynamicPointMap: loaded %d entries from file %s", count, path)  # pragma: no mutate
            return count
        except FileNotFoundError:
            log.debug("DynamicPointMap: no file at %s (first run)", path)  # pragma: no mutate
            return 0
        except OSError as e:
            log.warning("DynamicPointMap: could not read %s: %s", path, e)  # pragma: no mutate
            return 0
