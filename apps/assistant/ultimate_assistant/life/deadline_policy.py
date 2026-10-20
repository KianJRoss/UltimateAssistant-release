"""Pure deadline ranking and bounded, advisory work allocation.

``rank_commitments`` accepts an iterable of finite JSON object records.  The
deadline field is ``due_at`` and the effort field is ``estimated_minutes``.
Deadlines may be ISO 8601 date strings, or ISO 8601 datetime strings with an
explicit UTC offset (``Z`` is accepted).  Missing, malformed, and offset-free
datetime values are retained and classified, never guessed.  Positive finite
numeric estimates are known effort; all other values are retained but unknown.

``now`` is required and must be an aware :class:`datetime.datetime` or an ISO
8601 datetime string with an explicit offset.  ``timezone_name`` is an IANA
name loaded with :mod:`zoneinfo`.  This depends on the host's timezone data;
``ZoneInfoNotFoundError`` is allowed to surface if it is unavailable rather
than silently substituting another zone.  Date-only deadlines are compared as
calendar dates in that named timezone and are never assigned a time of day.
Aware deadlines are compared as instants.

``plan_work`` additionally accepts a JSON object mapping ISO dates to
nonnegative finite minute capacities.  Past dates are not usable.  Known
effort is allocated in ranking order and chronological capacity order, never
above a day's supplied capacity.  Work through a date-only deadline date is
on time.  Work on the local date of a still-future aware deadline is treated
as feasible because capacities have no time-of-day precision; work after that
date, and all work on an already overdue item, is explicitly marked late.
Unknown effort is not allocated.  Remaining known work and unused capacity
are reported without inventing effort, equal costs, or extra capacity.

Results contain only JSON values.  Every source record is deep-copied, keeping
all original fields, nested provenance, and raw deadline/estimate values.  No
input is mutated.
"""

from collections.abc import Mapping
from datetime import date, datetime, timezone
import json
import math
from zoneinfo import ZoneInfo


def _json_copy(value, message):
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(message) from exc


def _records(records):
    if isinstance(records, (str, bytes, Mapping)):
        raise TypeError('records must be an iterable of JSON objects')
    try:
        iterator = iter(records)
    except TypeError as exc:
        raise TypeError('records must be an iterable of JSON objects') from exc
    result = []
    for record in iterator:
        if not isinstance(record, Mapping):
            raise ValueError('each record must be a JSON object')
        copied = _json_copy(dict(record), 'records must contain finite JSON data')
        if not isinstance(copied, dict):  # Defensive for unusual Mapping types.
            raise ValueError('each record must be a JSON object')
        result.append(copied)
    return result


def _context(now, timezone_name):
    if isinstance(now, str):
        try:
            now = datetime.fromisoformat(now.replace('Z', '+00:00'))
        except ValueError as exc:
            raise ValueError('now must be an aware ISO datetime') from exc
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError('now must be an aware datetime or aware ISO datetime')
    if not isinstance(timezone_name, str) or not timezone_name:
        raise ValueError('timezone_name must be a nonempty IANA timezone name')
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        if timezone_name == 'America/Indianapolis':
            zone = ZoneInfo('America/Indiana/Indianapolis')
        else:
            raise
    return now.astimezone(timezone.utc), zone


def _deadline(raw, now_utc, zone):
    classification = 'malformed'
    relation = 'unknown'
    order = None
    local_date = None
    if raw is None or raw == '':
        classification = 'missing'
    elif isinstance(raw, str):
        try:
            parsed_date = date.fromisoformat(raw)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            except (ValueError, OverflowError):
                pass
            else:
                if parsed.utcoffset() is None:
                    classification = 'naive'
                else:
                    classification = 'aware'
                    instant = parsed.astimezone(timezone.utc)
                    local_date = parsed.astimezone(zone).date()
                    order = instant.timestamp()
                    relation = ('overdue' if instant < now_utc else
                                'upcoming' if instant > now_utc else 'current')
        else:
            classification = 'date_only'
            local_date = parsed_date
            order = parsed_date.toordinal()
            today = now_utc.astimezone(zone).date()
            relation = ('overdue' if parsed_date < today else
                        'upcoming' if parsed_date > today else 'current')
    return ({'value': raw, 'classification': classification, 'relation': relation},
            order, local_date)


def _effort(raw, *, allow_zero=False):
    valid = (isinstance(raw, (int, float)) and not isinstance(raw, bool) and
             (not isinstance(raw, float) or math.isfinite(raw)) and (raw >= 0 if allow_zero else raw > 0))
    return {'value': raw, 'minutes': raw if valid else None,
            'status': 'known' if valid else 'unknown'}


def _ranked(records, now_utc, zone):
    entries = []
    class_rank = {'aware': 0, 'date_only': 1, 'naive': 2,
                  'malformed': 3, 'missing': 4}
    relation_rank = {'overdue': 0, 'current': 1, 'upcoming': 2, 'unknown': 3}
    for index, record in enumerate(_records(records)):
        deadline, order, local_date = _deadline(record.get('due_at'), now_utc, zone)
        effort = _effort(record.get('remaining_effort_minutes'),allow_zero=True) if 'remaining_effort_minutes' in record else _effort(record.get('estimated_minutes'))
        # Stable sort means exact ties retain input order. Unknown classes are
        # deliberately not ordered by their raw, potentially misleading value.
        key = (relation_rank[deadline['relation']],
               class_rank[deadline['classification']],
               order if order is not None else 0)
        entries.append((key, {'record': record, 'deadline': deadline,
                              'effort': effort}, local_date))
    entries.sort(key=lambda item: item[0])
    return entries


def rank_commitments(records, *, now, timezone_name='America/Indianapolis'):
    """Return detached JSON ranking cards; see the module contract."""
    now_utc, zone = _context(now, timezone_name)
    return [entry for _, entry, _ in _ranked(records, now_utc, zone)]


def _capacities(available_minutes):
    if not isinstance(available_minutes, Mapping):
        raise TypeError('available_minutes must be a JSON object')
    copied = _json_copy(dict(available_minutes),
                        'available_minutes must contain finite JSON data')
    parsed = []
    for raw_date, minutes in copied.items():
        try:
            day = date.fromisoformat(raw_date)
        except (TypeError, ValueError) as exc:
            raise ValueError('capacity keys must be ISO calendar dates') from exc
        if (isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or
                (isinstance(minutes, float) and not math.isfinite(minutes)) or minutes < 0):
            raise ValueError('capacities must be nonnegative finite numbers')
        parsed.append((day, raw_date, minutes))
    parsed.sort()
    return copied, parsed


def plan_work(records, *, now, available_minutes,
              timezone_name='America/Indianapolis'):
    """Allocate only known supplied effort into supplied daily capacities."""
    now_utc, zone = _context(now, timezone_name)
    supplied, capacities = _capacities(available_minutes)
    today = now_utc.astimezone(zone).date()
    remaining = {raw_date: minutes for day, raw_date, minutes in capacities
                 if day >= today}
    ranked = _ranked(records, now_utc, zone)
    items = []
    known_effort = allocated = late_allocated = unknown_count = 0

    for _, card, deadline_date in ranked:
        effort = card['effort']['minutes']
        allocations = []
        if effort is None:
            unknown_count += 1
            unallocated = None
        else:
            known_effort += effort
            left = effort
            relation = card['deadline']['relation']
            # Feasible days first, then late/ambiguous days. For ambiguous
            # deadlines no allocation is called late because lateness is unknown.
            usable = [(day, raw) for day, raw, _ in capacities
                      if day >= today and remaining.get(raw, 0) > 0]
            if deadline_date is not None and relation != 'overdue':
                usable.sort(key=lambda pair: (pair[0] > deadline_date, pair[0]))
            for day, raw_date in usable:
                if left <= 0:
                    break
                amount = min(left, remaining[raw_date])
                if amount <= 0:
                    continue
                late = (relation == 'overdue' or
                        (deadline_date is not None and day > deadline_date))
                allocations.append({'date': raw_date, 'minutes': amount, 'late': late})
                remaining[raw_date] -= amount
                left -= amount
                allocated += amount
                if late:
                    late_allocated += amount
            unallocated = left
        item = dict(card)
        item.update({'allocations': allocations, 'allocated_minutes':
                     sum(part['minutes'] for part in allocations),
                     'unallocated_minutes': unallocated,
                     'insufficient_capacity': unallocated is not None and unallocated > 0})
        items.append(item)

    elapsed = {raw: minutes for day, raw, minutes in capacities if day < today}
    usable_capacity = sum(minutes for day, _, minutes in capacities if day >= today)
    unallocated_known = known_effort - allocated
    return {
        'version': 1,
        'as_of': now_utc.isoformat(),
        'timezone': timezone_name,
        'items': items,
        'capacity': {'supplied': supplied, 'elapsed': elapsed,
                     'usable_minutes': usable_capacity,
                     'unused_minutes': sum(remaining.values())},
        'totals': {'known_effort_minutes': known_effort,
                   'allocated_minutes': allocated,
                   'late_allocated_minutes': late_allocated,
                   'unallocated_known_effort_minutes': unallocated_known,
                   'unknown_effort_count': unknown_count,
                   'insufficient_capacity': unallocated_known > 0},
    }
