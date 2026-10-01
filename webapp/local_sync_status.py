"""Compatibility status for the intentionally absent external calendar sync."""
import datetime

def unavailable_status():
    return {'state':'stopped','reason_code':'timer_stopped','checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),'last_success_at':None}

def read_calendar_sync_status():
    return unavailable_status()

def validate_status_document(value):
    if not isinstance(value, dict) or set(value) != {'state','reason_code','checked_at','last_success_at'}:
        raise ValueError('invalid status')
    return value
