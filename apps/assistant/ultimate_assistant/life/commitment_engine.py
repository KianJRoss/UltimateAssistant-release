"""Pure, advisory daily commitment planning.

The planner only describes a possible schedule.  It does not enforce the
schedule, operate timers, or claim that work or units have been completed.
Timezone conversion depends on the host's IANA timezone data.  An outing with
no reliable return is deliberately treated as occupying the rest of each
applicable work window; this conservative treatment makes uncertainty visible
rather than guessing.  Learning evidence is copied through unchanged and is
never inferred from planning or completion data.
"""

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
import json
import math
from zoneinfo import ZoneInfo

from .deadline_policy import rank_commitments


_MAX_COMMITMENTS = 1000
_MAX_HORIZON = timedelta(days=14)


def _copy_json(value, message):
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(message) from exc


def _aware_now(now):
    if isinstance(now, str):
        try:
            now = datetime.fromisoformat(now.replace('Z', '+00:00'))
        except (ValueError, OverflowError) as exc:
            raise ValueError('now must be an aware ISO datetime') from exc
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError('now must be an aware datetime or aware ISO datetime')
    return now.astimezone(timezone.utc)


def _positive_minutes(value, name):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or value <= 0):
        raise ValueError(f'{name} must be a positive finite number')
    return value


def _nonnegative_minutes(value, name):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or value < 0):
        raise ValueError(f'{name} must be a nonnegative finite number')
    return value


def _timestamp(value, name):
    if not isinstance(value, str):
        raise ValueError(f'{name} must be an aware ISO datetime string')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, OverflowError) as exc:
        raise ValueError(f'{name} must be an aware ISO datetime string') from exc
    if parsed.utcoffset() is None:
        raise ValueError(f'{name} must be an aware ISO datetime string')
    return parsed.astimezone(timezone.utc)


def _object_list(value, name):
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise TypeError(f'{name} must be an iterable of JSON objects')
    copied = _copy_json(list(value), f'{name} must contain finite JSON data')
    if not all(isinstance(item, dict) for item in copied):
        raise ValueError(f'each {name} entry must be a JSON object')
    return copied


def _merge(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _subtract(intervals, blocked):
    result = []
    for start, end in intervals:
        parts = [(start, end)]
        for blocked_start, blocked_end in blocked:
            next_parts = []
            for part_start, part_end in parts:
                if blocked_end <= part_start or blocked_start >= part_end:
                    next_parts.append((part_start, part_end))
                else:
                    if part_start < blocked_start:
                        next_parts.append((part_start, blocked_start))
                    if blocked_end < part_end:
                        next_parts.append((blocked_end, part_end))
            parts = next_parts
        result.extend(parts)
    return result


def _windows(value, now_utc):
    records = _object_list(value, 'work_windows')
    horizon = now_utc + _MAX_HORIZON
    intervals = []
    for item in records:
        start = _timestamp(item.get('start',item.get('starts_at')), 'work window start')
        end = _timestamp(item.get('end',item.get('ends_at')), 'work window end')
        for canonical,alias in [('start','starts_at'),('end','ends_at')]:
            if canonical in item and alias in item and _timestamp(item[canonical],canonical)!=_timestamp(item[alias],alias):
                raise ValueError('Conflicting work-window aliases')
        if end <= start:
            raise ValueError('work windows must end after they start')
        if end > horizon:
            raise ValueError('work windows may not exceed the 14-day planning horizon')
        start = max(start, now_utc)
        if start < end:
            intervals.append((start, end))
    return _merge(intervals)


def _outings(value, now_utc, horizon):
    records = _object_list(value, 'outings')
    intervals = []
    for item in records:
        start = _timestamp(item.get('starts_at'), 'outing starts_at')
        actual_raw = item.get('returned_at')
        expected_raw = item.get('expected_return')
        actual = _timestamp(actual_raw, 'outing returned_at') if actual_raw is not None else None
        expected = (_timestamp(expected_raw, 'outing expected_return')
                    if expected_raw is not None else None)
        if actual is not None and actual < start:
            raise ValueError('outing returned_at may not precede starts_at')
        if expected is not None and expected < start:
            raise ValueError('outing expected_return may not precede starts_at')
        # Once an expected return has passed, only an actual return establishes
        # availability.  ``horizon`` serves as an infinite end for subtraction.
        if actual is not None:
            end = actual
        elif expected is None or (start <= now_utc and expected <= now_utc):
            end = horizon
        else:
            end = expected
        if start < end:
            intervals.append((start, end))
    return _merge(intervals)


def _unit_effort(record):
    if 'units' not in record:
        return None
    units = record['units']
    if not isinstance(units, list):
        raise ValueError('units must be a JSON array')
    result = []
    seen=set()
    for unit in units:
        if not isinstance(unit, dict):
            raise ValueError('each unit must be a JSON object')
        unit_id = unit.get('id')
        if not isinstance(unit_id, str) or not unit_id.strip():
            raise ValueError('unit id must be a nonempty string')
        if unit_id in seen:
            raise ValueError('Unit ids must be distinct')
        seen.add(unit_id)
        minutes = unit.get('estimated_minutes')
        _positive_minutes(minutes, 'unit estimated_minutes')
        if not isinstance(unit.get('completed'), bool):
            raise ValueError('unit completed must be boolean')
        if not unit['completed']:
            result.append({'id': unit_id, 'remaining': minutes})
    return result


def _deadline_late(card, end, zone):
    deadline = card['deadline']
    raw = deadline['value']
    if deadline['classification'] == 'aware':
        due = datetime.fromisoformat(raw.replace('Z', '+00:00')).astimezone(timezone.utc)
        return end > due
    if deadline['classification'] == 'date_only':
        # A date-only deadline includes the entire named-zone calendar date;
        # completing exactly at the following midnight is still on boundary.
        boundary_date = date.fromisoformat(raw) + timedelta(days=1)
        boundary = datetime.combine(boundary_date, datetime.min.time(), tzinfo=zone)
        return end > boundary.astimezone(timezone.utc)
    return False


def build_daily_plan(commitments, *, now, work_windows, outings, learning=None,
                     timezone_name='America/Indianapolis', focus_minutes=25,
                     break_minutes=5):
    """Return a detached finite-JSON advisory plan for supplied availability."""
    now_utc = _aware_now(now)
    if not isinstance(timezone_name, str) or not timezone_name:
        raise ValueError('timezone_name must be a nonempty IANA timezone name')
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        if timezone_name == 'America/Indianapolis':
            zone = ZoneInfo('America/Indiana/Indianapolis')
        else:
            raise
    focus_minutes = _positive_minutes(focus_minutes, 'focus_minutes')
    break_minutes = _nonnegative_minutes(break_minutes, 'break_minutes')

    records = _object_list(commitments, 'commitments')
    if len(records) > _MAX_COMMITMENTS:
        raise ValueError('at most 1000 commitments are supported')
    evidence = _copy_json(learning, 'learning must be finite JSON data')
    windows = _windows(work_windows, now_utc)
    horizon = now_utc + _MAX_HORIZON
    free = _subtract(windows, _outings(outings, now_utc, horizon))

    # Rank the already detached records and then validate/derive explicit unit
    # effort.  Cards retain exactly the ranker's record/deadline/effort fields.
    cards = rank_commitments(records, now=now_utc, timezone_name=timezone_name)
    states = []
    risks = []
    for index, card in enumerate(cards):
        units = _unit_effort(card['record'])
        if units is None:
            effort = card['effort']['minutes']
        else:
            effort = sum(unit['remaining'] for unit in units)
        if effort is None:
            risks.append({'type': 'missing_estimate', 'activity_id': card['record'].get('id')})
        states.append({'card': card, 'units': units, 'remaining': effort,
                       'scheduled': 0, 'late': 0, 'index': index})

    blocks = []
    cursor_index = 0
    cursor = free[0][0] if free else None
    focus_used=0
    last_work_end=None

    def advance_interval():
        nonlocal cursor_index, cursor
        cursor_index += 1
        cursor = free[cursor_index][0] if cursor_index < len(free) else None

    schedulable = [state for state in states
                   if state['remaining'] is not None and state['remaining'] > 0]
    for state_position, state in enumerate(schedulable):
        while state['remaining'] > 0 and cursor is not None:
            interval_end = free[cursor_index][1]
            available = (interval_end - cursor).total_seconds() / 60
            if available <= 0:
                advance_interval()
                continue
            if last_work_end is not None and break_minutes>0 and (cursor-last_work_end).total_seconds()/60>=break_minutes:
                focus_used=0
            if focus_used>=focus_minutes-1e-9:
                rest=(cursor-last_work_end).total_seconds()/60 if last_work_end else 0
                needed=max(0,break_minutes-rest)
                if needed>0:
                    break_end=cursor+timedelta(minutes=min(needed,available))
                    blocks.append({'type':'break','starts_at':cursor.isoformat(),'ends_at':break_end.isoformat(),'minutes':(break_end-cursor).total_seconds()/60})
                    cursor=break_end
                    if cursor>=interval_end:
                        advance_interval()
                    continue
                focus_used=0
            amount = min(state['remaining'], focus_minutes-focus_used, available)
            end = cursor + timedelta(minutes=amount)
            targets = []
            if state['units'] is not None:
                target_left = amount
                for unit in state['units']:
                    if target_left <= 0:
                        break
                    take = min(unit['remaining'], target_left)
                    if take > 0:
                        targets.append({'id': unit['id'], 'minutes': take})
                        unit['remaining'] -= take
                        target_left -= take
            late = _deadline_late(state['card'], end, zone)
            block = {'type': 'focus', 'starts_at': cursor.isoformat(),
                     'ends_at': end.isoformat(), 'minutes': amount,
                     'activity_id': state['card']['record'].get('id'),
                     'late': late}
            if state['units'] is not None:
                block['unit_targets'] = targets
            blocks.append(block)
            state['remaining'] -= amount
            state['scheduled'] += amount
            if late:
                state['late'] += amount
            cursor = end
            focus_used+=amount
            last_work_end=end

            if cursor >= interval_end:
                advance_interval()

    items = []
    for state in states:
        remaining = state['remaining']
        insufficient = remaining is not None and remaining > 0
        if insufficient:
            risks.append({'type': 'insufficient_capacity',
                          'activity_id': state['card']['record'].get('id'),
                          'remaining_minutes': remaining})
        overdue_unsatisfied = (state['card']['deadline']['relation'] == 'overdue' and
                               remaining is not None and
                               (state['scheduled'] > 0 or remaining > 0))
        if state['late'] > 0 or overdue_unsatisfied:
            risks.append({'type': 'deadline_lateness',
                          'activity_id': state['card']['record'].get('id'),
                          'late_minutes': state['late']})
        item = dict(state['card'])
        item.update({'scheduled_minutes': state['scheduled'],
                     'remaining_minutes': remaining,
                     'insufficient_capacity': insufficient})
        items.append(item)

    result = {
        'version': 1,
        'advisory_only': True,
        'as_of': now_utc.isoformat(),
        'timezone': timezone_name,
        'items': items,
        'blocks': blocks,
        'risks': risks,
        'learning': evidence,
    }
    # Assert the public purity/serialization boundary even after arithmetic.
    return _copy_json(result, 'plan could not be represented as finite JSON')
