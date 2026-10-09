"""Small explicit built-in registry. Lazy factories; no discovery, installs or OAuth."""
from dataclasses import dataclass, field
import importlib
import importlib.util
from pathlib import Path
from typing import Callable, Mapping
from .contracts import CAPABILITIES
from .state import IntegrationError, IntegrationState

@dataclass(frozen=True)
class PluginContext:
    root: Path
    settings: Mapping = field(default_factory=dict)
    services: Mapping = field(default_factory=dict)

@dataclass(frozen=True)
class PluginSpec:
    id: str
    capabilities: frozenset
    factory: Callable
    describe: Callable
    settings_schema: Mapping = field(default_factory=dict)
    label: str = ''
    def __post_init__(self):
        if not self.id or not self.capabilities<=CAPABILITIES:raise ValueError('invalid_plugin')

class Registry:
    def __init__(self,root,*,services=None,options=None,availability=None,specs=None):
        self.root=Path(root);self.state=IntegrationState(root);self.services=services or {};self.options=options or {};self.availability=availability or {};self.specs={}
        for spec in specs if specs is not None else builtins():self.register(spec)
    def register(self,spec):
        if spec.id in self.specs:raise ValueError('duplicate_plugin')
        self.specs[spec.id]=spec
    def context(self,plugin_id):return PluginContext(self.root,self.options.get(plugin_id,{}),self.services)
    def status(self,plugin_id):
        if plugin_id not in self.specs:raise IntegrationError('unknown_plugin')
        spec=self.specs[plugin_id];enabled=self.state.enabled(plugin_id)
        if self.availability.get(plugin_id) is False:description={'available':False,'configured':False,'connection_state':'unavailable'}
        else:
            try:description=dict(spec.describe(self.context(plugin_id)))
            except Exception:description={'available':False,'configured':False,'connection_state':'error'}
        available=description.get('available') is True;configured=description.get('configured') is True
        connection=description.get('connection_state','unconnected')
        if connection not in {'connected','unconnected','reauth','error','unavailable'}:connection='error'
        if not available:connection='unavailable'
        if not enabled:connection='disabled'
        return {'id':plugin_id,'label':spec.label or plugin_id,'capabilities':sorted(spec.capabilities),'enabled':enabled,'available':available,'configured':configured,'connection_state':connection}
    def statuses(self):return {'revision':self.state.flags()['revision'],'plugins':[self.status(name) for name in self.specs]}
    def require(self,plugin_id,capability):
        status=self.status(plugin_id)
        if capability not in status['capabilities']:raise IntegrationError('unsupported_capability')
        if not status['enabled']:raise IntegrationError('plugin_disabled')
        if not status['available']:raise IntegrationError('plugin_unavailable')
        if not status['configured']:raise IntegrationError('plugin_unconnected')
        return self.specs[plugin_id].factory(self.context(plugin_id))

def _spec(plugin_id,capabilities,label):
    module='webapp.integration_plugins.'+plugin_id
    def available():return importlib.util.find_spec(module) is not None
    def describe(context):
        if not available():return {'available':False,'configured':False,'connection_state':'unavailable'}
        return importlib.import_module(module).describe(context)
    def factory(context):return importlib.import_module(module).build_plugin(context)
    return PluginSpec(plugin_id,frozenset(capabilities),factory,describe,label=label)

def builtins():
    return [_spec('outlook',{'calendar.read'},'Outlook'),_spec('google_calendar',{'calendar.read','calendar.write'},'Google Calendar'),_spec('timetracker_nx',{'actual.read','actual.write'},'TimeTracker NX')]
