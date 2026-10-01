"""Read-only calendar projection of the existing Markdown Task contract."""
import datetime as dt
import re
from webapp.store import InputError

JST = dt.timezone(dt.timedelta(hours=9))


def _timestamp(value):
    try:
        result = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result.astimezone(JST) if result.tzinfo else None
    except (TypeError, ValueError):
        return None


def calendar_projection(store, view, date, *, now=None):
    """Return Monday-based 1/7/42 day grids, clipping timed items at JST midnight."""
    if view not in {'day', 'week', 'month'} or not isinstance(date, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date):
        raise InputError('invalid calendar view or date')
    try:
        selected = dt.date.fromisoformat(date)
        if not 1900 <= selected.year <= 9998:
            raise ValueError()
        anchor = selected.replace(day=1) if view == 'month' else selected
        start = anchor if view == 'day' else anchor - dt.timedelta(days=anchor.weekday())
        count = {'day': 1, 'week': 7, 'month': 42}[view]
        end = start + dt.timedelta(days=count)
    except (ValueError, OverflowError):
        raise InputError('invalid calendar date') from None
    current = (now or dt.datetime.now(JST)).astimezone(JST)
    snapshot = store.read_snapshot()
    days = [{'date': (start + dt.timedelta(days=i)).isoformat(), 'events': []} for i in range(count)]
    index = {day['date']: day for day in days}

    def all_day(base, kind, value):
        key = str(value)[:10]
        if key in index:
            index[key]['events'].append({**base, 'kind': kind, 'all_day': True})

    def timed(base, kind, first, last, provisional=False):
        if first is None or last is None or last <= first:
            return
        first = max(first, dt.datetime.combine(start, dt.time(), JST))
        last = min(last, dt.datetime.combine(end, dt.time(), JST))
        while first < last:
            midnight = dt.datetime.combine(first.date() + dt.timedelta(days=1), dt.time(), JST)
            segment_end = min(midnight, last)
            index[first.date().isoformat()]['events'].append({**base, 'kind': kind, 'all_day': False, 'start': first.isoformat(), 'end': segment_end.isoformat(), 'minutes': (segment_end-first).total_seconds()/60, 'provisional': provisional})
            first = segment_end

    for entity in snapshot.entities:
        if entity.entity_type != 'task':
            continue
        fm = entity.frontmatter
        archived = entity.relative_path.startswith('archive/')
        base = {'id': entity.entity_id, 'title': fm.get('title', entity.entity_id), 'status': fm.get('status', ''), 'archived': archived}
        if not archived and fm.get('status') != 'done':
            first, last = _timestamp(fm.get('scheduled_start')), _timestamp(fm.get('scheduled_end'))
            if first and last:
                timed(base, 'planned', first, last)
            elif fm.get('action_date'):
                all_day(base, 'action', fm['action_date'])
            if fm.get('due'):
                deadline = _timestamp(fm['due'])
                all_day(base, 'deadline', deadline.date().isoformat() if deadline else fm['due'])
        first = _timestamp(fm.get('work_started_at'))
        last = _timestamp(fm.get('work_ended_at'))
        provisional = fm.get('status') == 'doing' and not fm.get('work_ended_at')
        timed(base, 'actual', first, current if provisional else last, provisional)
    for day in days:
        day['events'].sort(key=lambda e: (not e['all_day'], e.get('start',''), e['kind'], e['id']))
    return {'view': view, 'date': date, 'timezone': 'Asia/Tokyo', 'from': start.isoformat(), 'to_exclusive': end.isoformat(), 'days': days, 'mutation_state': {'recovery_required': snapshot.recovery_required}}
