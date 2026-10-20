"""Pure Today aggregation; no runtime, storage, scheduling or execution imports.

Contract (version 1): inputs are iterables of JSON-object mappings. Invalid
records/non-finite or non-JSON values raise ValueError (bad iterables TypeError).
Records are copied, including every source field/ID/provenance, under ``record``.
Missing kind means commitment; done/completed/cancelled/canceled tasks are omitted.
Other kinds are not commitments. Domain defaults to 'unspecified'.

Each section is independently capped by limit (nonnegative integer, bool rejected).
Totals/omitted describe all eligible records; groups describe visible commitments
in alphabetical domain order, preserving priority within each group. Zero hides
all cards without implying an empty source. Empty sections have state 'empty';
nonempty hidden sections 'limited'; visible sections 'available'.

Priority: valid explicit deadlines first, then in_progress/in-progress work,
then deadline class (aware, date_only, naive), chronological value within that
class, string ID, title, canonical source JSON. Exact duplicates retain input
order. Classes are deliberately separate: date-only/naive values are never
assigned a timezone or fabricated time. Aware deadlines compare UTC instants;
only aware deadlines receive overdue/upcoming/current relative to aware now.
Missing and malformed deadlines sort last and remain verbatim. Date-only and
naive deadlines have relation 'unknown'. Now defaults to current UTC; pass an
aware datetime or ISO datetime for reproducible results (naive now rejected).

Output: version, as_of, limit, commitments/activities/needs_preparation sections,
domain_groups and learning. Cards contain record, deadline (value/classification/
relation), effort (minutes or null, known/unknown), and next_step (action/text).
Contexts with nonblank next_action are activities; others need preparation.
The internal device-mode-authority ID or kind is excluded (hyphen/underscore
equivalent). No context creates a commitment. Learning comprehension and
retention are explicitly 'unmeasured', regardless of supplied evidence.
"""

from collections.abc import Mapping
from datetime import date, datetime, timezone
import json
import math


def _validate_json(value):
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError('JSON object keys must be strings')
        for item in value.values():
            _validate_json(item)
    elif isinstance(value, list):
        for item in value:
            _validate_json(item)
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise ValueError('Expected JSON data')


def _records(values):
    if isinstance(values, (str, bytes, Mapping)):
        raise TypeError('Expected an iterable of record mappings')
    result = []
    for value in values:
        if not isinstance(value, Mapping):
            raise ValueError('Each record must be a mapping')
        try:
            # Round trip both validates and detaches nested source data.
            _validate_json(dict(value))
            result.append(json.loads(json.dumps(dict(value), allow_nan=False)))
        except (TypeError, ValueError) as exc:
            raise ValueError('Records must contain finite JSON data') from exc
    return result


def _deadline(value, now):
    classification, relation, order = 'malformed', 'unknown', ''
    if value is None or value == '':
        classification = 'missing'
    elif isinstance(value, str):
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if parsed.utcoffset() is None:
                    classification, order = 'naive', parsed.isoformat()
                else:
                    classification = 'aware'
                    utc = parsed.astimezone(timezone.utc)
                    order = utc.isoformat()
                    relation = 'overdue' if utc < now else 'upcoming' if utc > now else 'current'
            except (ValueError, OverflowError):
                pass
        else:
            classification, order = 'date_only', parsed_date.isoformat()
    return {'value': value, 'classification': classification, 'relation': relation}, order


def _card(record, due_field, now, context=False):
    deadline, order = _deadline(record.get(due_field), now)
    minutes = record.get('estimated_minutes')
    known = (isinstance(minutes, (int, float)) and not isinstance(minutes, bool)
             and (isinstance(minutes, int) or math.isfinite(minutes)) and minutes > 0)
    action = record.get('next_action')
    ready = isinstance(action, str) and bool(action.strip())
    if ready:
        step = {'action': 'continue' if context else 'work', 'text': action}
    elif context:
        step = {'action': 'prepare', 'text': 'Choose a concrete next action for this activity.'}
    else:
        step = {'action': 'review', 'text': 'Review this commitment and choose its next action.'}
    card = {'record': record, 'deadline': deadline,
            'effort': {'minutes': minutes if known else None,
                       'status': 'known' if known else 'unknown'}, 'next_step': step}
    ranks = {'aware': 0, 'date_only': 1, 'naive': 2, 'malformed': 3, 'missing': 4}
    valid = deadline['classification'] in {'aware', 'date_only', 'naive'}
    progress = record.get('status', record.get('last_state')) in ('in_progress', 'in-progress')
    key = (not valid, not progress, ranks[deadline['classification']], order,
           str(record.get('id', '')), str(record.get('title', '')),
           json.dumps(record, sort_keys=True, ensure_ascii=True))
    return key, card, ready


def _section(cards, limit):
    cards.sort(key=lambda item: item[0])
    visible = [item[1] for item in cards[:limit]]
    return {'items': visible, 'total': len(cards), 'omitted': len(cards) - len(visible),
            'state': 'empty' if not cards else 'available' if visible else 'limited'}


def build_today(tasks, activities, *, now=None, limit=5):
    """Return JSON-serializable advisory cards without modifying supplied records."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError('limit must be an integer')
    if limit < 0:
        raise ValueError('limit must be nonnegative')
    if now is None:
        now = datetime.now(timezone.utc)
    elif isinstance(now, str):
        now = datetime.fromisoformat(now.replace('Z', '+00:00'))
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError('now must be an aware datetime or ISO datetime')
    now = now.astimezone(timezone.utc)
    tasks, contexts = _records(tasks), _records(activities)
    commitments, ready, preparation = [], [], []
    for task in tasks:
        if task.get('kind', 'commitment') != 'commitment' or task.get('status') in (
                'done', 'completed', 'cancelled', 'canceled'):
            continue
        key, card, _ = _card(task, 'due_at', now)
        commitments.append((key, card))
    for context in contexts:
        if any(str(context.get(field, '')).replace('_', '-').casefold() ==
               'device-mode-authority' for field in ('id', 'kind')):
            continue
        key, card, actionable = _card(context, 'due', now, context=True)
        (ready if actionable else preparation).append((key, card))
    result = {'version': 1, 'as_of': now.isoformat(), 'limit': limit,
              'commitments': _section(commitments, limit),
              'activities': _section(ready, limit),
              'needs_preparation': _section(preparation, limit),
              'learning': {'comprehension': 'unmeasured', 'retention': 'unmeasured'}}
    groups = {}
    for card in result['commitments']['items']:
        domain = card['record'].get('domain')
        domain = domain if isinstance(domain, str) and domain.strip() else 'unspecified'
        groups.setdefault(domain, []).append(card)
    result['domain_groups'] = [{'domain': domain, 'items': groups[domain]}
                               for domain in sorted(groups)]
    return result
