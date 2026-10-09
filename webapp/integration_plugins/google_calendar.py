"""Compatibility adapter for the existing Google sync service and its safeguards."""
import importlib.util

def describe(context):
    available=importlib.util.find_spec('calendar_sync') is not None
    if not available:return {'available':False,'configured':False,'connection_state':'unavailable'}
    reader=context.services.get('google_status')
    if reader is None:return {'available':True,'configured':False,'connection_state':'unconnected'}
    value=reader();configured=value.get('reason_code') in {'recent_success','recovery_pending','first_run_pending','timer_stopped','run_in_progress','run_stale','reauth_required'}
    return {'available':True,'configured':configured,'connection_state':'reauth' if value.get('state')=='reauth' else 'connected' if configured else 'unconnected'}

class GoogleCalendarPlugin:
    def __init__(self,context):self.context=context
    def run_existing_sync(self):
        # Existing fixed target, journal, authorization and exact readback are untouched.
        from webapp.integrations.state import IntegrationState, IntegrationError
        if not IntegrationState(self.context.root).enabled("google_calendar"):raise IntegrationError("plugin_disabled")
        from calendar_sync.service import build_default_service
        return build_default_service().run()

def build_plugin(context):return GoogleCalendarPlugin(context)
