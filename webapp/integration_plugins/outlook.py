"""Outlook-specific acquisition entrypoint; saved annotations remain core state."""
from webapp.integrations.actuals import external_actual

def describe(context):
    events=context.services.get('outlook_events')
    configured=bool(events and (events.handoff or events.collector))
    return {'available':events is not None,'configured':configured,'connection_state':'connected' if configured else 'unconnected'}

class OutlookPlugin:
    def __init__(self,context):self.events=context.services['outlook_events']
    def fetch_calendar(self,source,first,last):return self.events.run(source,first,last)

def build_plugin(context):return OutlookPlugin(context)

def saved_actuals(events):
    result=[]
    for event in events.status()['events']:
        actual=event['local'].get('actual')
        if actual:
            result.append(external_actual(source_id=event['key'],provider='outlook',title=event['title'],actual=actual,completed=event['local']['done']))
    return result

def saved_events(events):
    return [{**event,'provider_id':'outlook','provider_label':'Outlook'} for event in events.status()['events']]
